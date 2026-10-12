# Short-lived AWS release stack

This package describes a bounded public-service and recovery exercise in an explicitly verified AWS account, region `eu-west-1`, with an approved maximum budget of USD 5 for the test window. It uses one dedicated VPC and an ARM64 EC2 host, not an existing VPC or any persistent shared service. The release is removed after API, database, and backup verification; one day is the maximum planned test window, not a minimum run time.

## Architecture and boundaries

```text
Internet ── 80/443 ──> Elastic IP / Caddy on t4g.small EC2
                                  │ host network, API bound to 127.0.0.1:8011
                                  ├── SSM SecureString: three scoped runtime values
                                  ├── encrypted EBS: SQLite durable investigation ledger
                                  └── 5432 from EC2 SG only ──> private single-AZ RDS PostgreSQL
                                                                     │
SQLite quiesce + integrity check ──> private, versioned, encrypted S3 recovery bucket
```

CloudFormation creates a new VPC, one public subnet and two private subnets across two AZs. RDS uses the private subnets and is not publicly accessible. There is no NAT gateway, load balancer, SSH ingress, GPU, or shared VPC mutation. The EC2 instance has an instance profile for Systems Manager, ECR pull, GetParameter access under its stack-specific SSM prefix, scoped S3 recovery paths, and its CloudWatch log stream. SSM Session Manager is the operator path.

Caddy is pinned to the official ARM64 image `2.11.7-alpine` by platform digest (the official image tag is documented at [Docker Hub](https://hub.docker.com/_/caddy)). It terminates HTTPS and forwards to the loopback API. Proxy access logging is disabled, so request headers and query strings are not emitted as access events; operational errors still go to CloudWatch. API logs should follow the same secret-redaction rule. The API retains its public-host and bearer-token boundary; the token and hosted NVIDIA key never enter CloudFormation parameters or instance user data. Configure the supplied host's DNS-only A record to the stack EIP. Keep `PublicHost` configurable so a dedicated hostname can replace the default. A literal IP is not the default HTTPS route: Caddy's docs say IP addresses receive a locally trusted certificate by default. A public IP SAN certificate is possible in principle with Caddy's explicit ACME `shortlived` profile, but that Caddy profile feature is experimental and the Let's Encrypt certificate lasts only 160 hours. The template is DNS-hostname constrained and does not configure or validate that IP-certificate path. For this lab, a domain A record plus Caddy automatic HTTPS is the simpler route.

The app command is installed by the user-data script after an immutable image tag and runtime parameters exist:

```text
uv run --locked --no-dev llm-lab-serve --port 8011 --olist-root /app --output-dir /state/service --olist-engine intent-v1 --olist-provider nvidia-super --durable-db /state/investigations.sqlite --remote-call-budget 32 --request-timeout 60 --public-host <PublicHost> --public-token-file /run/secrets/api-token --rate-limit-per-minute 30
```

The Docker entrypoint supplies the locked `uv` environment. The EC2 unit mounts encrypted EBS at `/state`, reads `NVIDIA_API_KEY` and `OLIST_READER_DSN` from the scoped runtime env file, and mounts the API token as a separate read-only file. The graph worker owns ledger idempotency, monotonic budget reconciliation, and snapshot semantics; this AWS package does not rewrite those policies.

## Cost and time bound

The direct 24-hour compute and storage estimate is about USD 1.10–1.60 before data transfer, logs, larger-than-planned backups, ECR storage, or hosted inference. It includes one `t4g.small` EC2 instance, its 20 GB gp3 root volume, one public IPv4 address, one `db.t4g.micro` PostgreSQL instance, and 20 GB encrypted RDS storage. There is no hourly NAT gateway or ALB charge. The USD 5 cap is a review boundary rather than an AWS billing hard stop; stop work and delete the stack before the 24-hour window if measured usage approaches the cap. NVIDIA-hosted inference usage is separate from AWS and must remain inside the already-approved provider budget.

## Release sequence

Prerequisites: AWS CLI v2, `jq`, Docker Buildx, an authenticated `develope-test` profile for the expected account, a DNS record you control, and three runtime values held in local protected files. Export `AWS_EXPECTED_ACCOUNT` to the verified 12-digit account ID; the scripts refuse to operate without an explicit expected account. Do not put secrets in shell history, command output, CloudFormation parameters, repository files, or user data.

```bash
export AWS_EXPECTED_ACCOUNT=<expected-account-id>
```

1. Have the operator review this template, then create the named stack (this script requires explicit `--apply`):

   ```bash
   scripts/aws-release/deploy.sh \
     --stack-name llm-ops-lab-release-20261012 \
     --public-host marketing-api.jungseongheon.org \
     --apply
   ```

   The script verifies account and region, resolves the current Amazon Linux 2023 ARM64 AMI from AWS's public SSM parameter, and uses a stack-unique runtime parameter prefix. Record the EIP from stack outputs. Create a DNS-only A record for `PublicHost` pointing to that EIP and wait until public DNS resolves before expecting Caddy to obtain a trusted certificate.

2. Build and push an immutable ARM64 image for the reviewed source revision:

   ```bash
   scripts/aws-release/build-push.sh \
     --stack-name llm-ops-lab-release-20261012 \
     --source-revision <reviewed-commit-sha> \
     --push
   ```

3. Create the three `SecureString` parameters at the exact `ParameterPrefix` output: `nvidia-api-key`, `olist-reader-dsn`, and `public-api-token`. Use short-lived local files with mode `0600` and the AWS CLI `file://` input form, then remove those files. The runtime role can read names under this stack-specific prefix; it cannot read other prefixes. A temporary bootstrap parameter, if used, must be removed before starting the API. The RDS master secret is generated and managed by RDS; the EC2 role cannot read it. Bootstrap and service control use the allowlisted SSM helper; data migration remains an explicit operator procedure over SSM. Start the API only after data and parameters are verified:

   ```bash
   scripts/aws-release/operate-instance.sh \
     --stack-name llm-ops-lab-release-20261012 \
     --action install-image \
     --image-tag release-<reviewed-commit-sha> \
     --run
   ```

4. Verify from both sides of the boundary: `GET /health/ready` through the public HTTPS hostname; expected host and token enforcement; database-readiness and remote-budget readiness; app/Caddy logs; and a controlled investigation whose ledger state survives a service restart. Verify the PostgreSQL source counts/checksums with the existing Olist verifier. Quiesce the service before taking a SQLite backup. The installed backup command uses SQLite's backup API, runs `PRAGMA integrity_check`, writes a SHA-256 receipt, and uploads under the stack's S3 recovery prefixes. Invoke the backup and isolated restore verifier with these scoped operations:

   ```bash
   scripts/aws-release/operate-instance.sh --stack-name llm-ops-lab-release-20261012 --action backup --run
   scripts/aws-release/operate-instance.sh --stack-name llm-ops-lab-release-20261012 --action verify-backup --backup-key backups/sqlite/<UTC-stamp>/investigations.sqlite --run
   ```

   The verifier compares the restored file's SHA-256 to the uploaded receipt, then checks SQLite integrity and reports table counts. Save the command result with the evidence. Do not claim recovery verification until the restored copy passes. Budget usage from calls that might have reached the provider is reconciled conservatively; restoring an older SQLite file must not refund consumed remote-call budget.

5. Delete every release resource only after the evidence has been copied to the approved local evidence directory. The explicit cleanup script first removes all current S3 objects, object versions, and delete markers, then deletes the stack and removes the three external runtime parameters:

   ```bash
   scripts/aws-release/delete-stack.sh \
     --stack-name llm-ops-lab-release-20261012 \
     --apply
   ```

   The stack uses `DeletionPolicy: Delete` for RDS, S3, and CloudWatch; ECR is configured `EmptyOnDelete`; EC2 root EBS has `DeleteOnTermination`; the EIP and VPC resources belong to this stack. RDS automated backups are configured for one day and deleted with the DB. No final snapshot, retained bucket, or RDS snapshot is intended. Confirm CloudFormation stack deletion completed, the bucket and ECR repository are absent, no RDS instance/automated backup or manual snapshot remains for this stack, the generated RDS secret is gone, and the EIP is released. If deletion fails, stop and inspect the exact stack's events; do not broaden cleanup to account-wide resources.

## Validation and current operational limits

The template and scripts are designed to be reviewed and linted locally before any AWS apply. A successful `cloudformation deploy` only proves resource creation, not a functioning public API, TLS, data migration, or recovery. A live result must be reported separately from local checks. This one-host/single-AZ setup is a low-cost release exercise, not a highly available production architecture: an EC2 or AZ failure interrupts the service, and the app's durable graph is restored from a verified SQLite backup. Production evolution would add multi-AZ database availability, monitoring/alarms, controlled rolling deployment, and tested restore automation after usage and recovery objectives are known.

## Official references

- [Caddy HTTPS quick start](https://caddyserver.com/docs/quick-starts/https) explains public DNS and external reachability on ports 80/443.
- [Caddy address concepts](https://caddyserver.com/docs/caddyfile/concepts) documents locally trusted certificates for literal IP addresses.
- [Caddy TLS/ACME issuer](https://caddyserver.com/docs/caddyfile/directives/tls) documents ACME profile selection and marks it experimental.
- [Let's Encrypt IP and six-day certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) states IP certificates are short-lived and require the `shortlived` profile.
- [Let's Encrypt certificate profiles](https://letsencrypt.org/docs/profiles/) documents 160-hour validity and DNS/IP identifier support for `shortlived`.
- [EC2 On-Demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/), [RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/), and [VPC pricing](https://aws.amazon.com/vpc/pricing/) are the pricing sources for the estimate; confirm current regional rates before creating resources.
- [Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) documents the no-SSH administration path.
- [CloudFormation RDS DB instance behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbinstance.html) documents deletion policy and automated-backup behavior; [RDS-managed secrets](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html) states the managed secret is removed when its DB instance is deleted.

## Completed recovery exercise (2026-10-12)

The [release report](olist-durable-release-20261012.md) records actual HTTPS,
restart, injected worker failure, read-only/TLS database, backup, and restored-API
checks. The deployed application came from commit `57ec671`; source publication
adds the operational scripts and sanitized receipts afterwards.

Two explicit operator scripts supplement the installed backup verifier:

- `scripts/aws-release/verify-postgres-recovery.sh` runs on the selected EC2 host
  over SSM with the API stopped. It performs pg_dump → private S3 → verified
  download → a fresh `olist_restore` database on the same RDS instance. It refuses
  to overwrite a target and compares counts and sorted row digests for 14 relations.
  Its temporary bootstrap SSM parameter must be removed before the API starts.
- `scripts/aws-release/restore-sqlite.sh <stack> <bucket> <backup-key>` runs as root
  on that host while the API is stopped. It requires a fresh local target and an
  identical quiesced backup, compares the complete logical SQLite dump, call budget
  and pending attempts, then selects the restored ledger path. It leaves the API
  stopped. An older backup is rejected and needs explicit conservative reconciliation.

These are operator-driven recovery procedures, not automatic failover. Verification
switched the reader DSN to the restored database and the ledger to the restored file,
then tested saved replay and two fresh investigations. The exercise did not replace
an EC2 host, restore an RDS snapshot into a new instance, or measure RPO/RTO.
