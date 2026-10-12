# Durable Olist investigation service

This service makes one bounded Olist investigation resumable across worker and API
process restarts. It combines a LangGraph stage graph with a SQLite action ledger on
one host. The ledger preserves remote-call reservations, completed provider responses,
the validated database evidence bundle, and the final response. It does not make a
remote HTTP call exactly-once: a provider may have completed a call just before the
process died, leaving the outcome unknown locally.

## Execution and recovery model

The graph runs three stages: `plan` asks the model to choose from the fixed intent
catalog, `evidence` runs the allowed read-only queries and validates their outputs,
and `render` builds the bounded result from verified findings. The planner does not
receive database rows, SQL, or connection details.

LangGraph checkpoints record stage progress. They are not the source of truth for
external side effects. Before each hosted call, the action ledger reserves one call;
after a complete response arrives, it stores that response and its status before the
graph advances its checkpoint. A resumed graph can reuse a committed response if
checkpoint progress lagged behind the ledger. The final result is likewise committed
before the terminal checkpoint, so a completed result can be replayed if the last
checkpoint write did not finish.

If the process stops after reservation but before the response is safely committed,
the attempt remains `pending`. Its provider outcome is ambiguous, so the service fails
closed instead of sending the same call again. That reservation still consumes budget.
This avoids an automatic duplicate charge at the cost of requiring operator review or
a new idempotency key for a fresh attempt. A process crash is not proof that the
provider did no work.

The action ledger and graph checkpoints share one private SQLite database. A
non-blocking `flock` permits only one process to execute or replay investigations
against that file at a time; a competing process receives a busy error. The database
and lock file are owner-only regular files. This is a single-host coordination design,
not a distributed lock or multi-host database. Keep the SQLite file on the instance's
persistent encrypted EBS volume for the AWS lab; do not put it on an ephemeral
container layer or treat a shared network filesystem as a supported HA backend.

## Idempotency, configuration, and budget

The public API uses the single stable principal `operator`. An investigation is keyed
by that principal and the client-supplied `Idempotency-Key`; rotating the bearer token
does not create a new principal or bypass replay protection. For a key already used:

- The same question and execution binding replay the saved terminal result without
  another model call.
- A different accepted question, version, or bound configuration conflicts with
  HTTP 409; fields outside the request schema are rejected during validation.
- A run that has not safely completed resumes from its durable stages, or fails
  closed if it contains an unknown pending external attempt.

The binding records the request and execution-relevant configuration, including
prompt and policy versions, model identity, source-file content fingerprints, and
configured remote-call budget. A code/configuration change under an existing key is
therefore not silently treated as the same request. These are content hashes, not a
claim that the running image matches a Git commit.

The default remote-call budget is 32 reservations in the durable ledger. A potential
provider call is reserved before dispatch; completed and ambiguous attempts both
remain charged against that service-level limit. Replays and idempotency conflicts do
not reserve more calls. The configured limit is stored in the SQLite schema, so
changing it while reusing the same database fails closed with a configuration conflict.
Do not replace or restore an older ledger merely to regain budget.

## Dataset snapshot and version pinning

Evidence collection uses the read-only PostgreSQL role and a repeatable-read
transaction. It validates the requested tool outputs, dataset version, and quality
gate, then stores the complete evidence bundle and its SHA-256 as one durable action.
The service will reuse that complete bundle on resume. If the process stops while
evidence collection is pending, the result is treated as unknown and is not silently
collected again from a possibly different database snapshot.

Pin `version_id` for reproducible investigations. When it is omitted, the durable
binding records that no version was requested; the active published version is chosen
when the first evidence read opens its database snapshot, not when planning begins.
Once a complete bundle is stored, resumes use that bundle. A request that must be
reproducible across later runs should supply the 64-character published version ID.

## Public ingress and privacy

Public mode still binds the API listener to `127.0.0.1`; a trusted HTTPS reverse
proxy is required. The boundary accepts only the exact configured `Host`, rejects
requests with any `Origin`, and trusts `X-Forwarded-Proto: https` only when the TCP
peer is loopback. It rejects duplicate Host, Authorization, and forwarded-protocol
headers. The bearer token is loaded from an owner-only file or `OLIST_API_TOKEN`,
must be at least 32 bytes, and is compared using a constant-time digest comparison.
The token is not included in configuration representations, access events, or error
responses.

Authentication and route checks run before the request body is read. Public mode
exposes only `POST /v1/investigations`, `GET /health/live`, `GET /health/ready`, and
`GET /metrics`. The liveness endpoint may be anonymous and returns only a minimal
alive status; readiness and metrics require authentication. Investigation requests
must include an 8–128 character ASCII idempotency key. Request bodies remain capped
at 24 KiB with a five-second read timeout, and total in-flight HTTP work is capped at
32. The per-process token bucket defaults to 30 requests/minute with a burst capacity
of five; only investigation POSTs consume it. The worker queue remains separately
bounded.

Public-boundary and audited API responses include `Cache-Control: no-store` and the
configured security headers.
Access events record request ID, route, status, timing, and bounded outcome fields;
they do not record the body, question, authorization header, token, or idempotency
key. Keep reverse-proxy access logging disabled or configure it to omit request
headers and query strings. Operational logs should not include DSNs, provider bodies,
or exception text that may contain credentials.

The rate bucket is process-local: restarting the API resets its short-window rate
state. Authentication has one service-wide principal, not per-user authorization or
per-user quotas. This release does not provide alerting, an on-call policy, a response
SLA, or multi-instance shared rate limiting.

## Run and verify locally

Prepare a private token file owned by the service user with mode `0600` or stricter,
and provide the NVIDIA key and read-only PostgreSQL configuration through the
existing secret mechanisms. Do not put credentials in command-line arguments or
repository files. From the project root, a public-mode local service can be started
with:

```bash
uv run --locked llm-lab-serve \
  --port 8011 \
  --olist-root "$PWD" \
  --output-dir runs/public-service \
  --olist-engine intent-v1 \
  --olist-provider nvidia-super \
  --remote-call-budget 32 \
  --request-timeout 60 \
  --durable-db runs/public-service/investigations.sqlite \
  --public-host api.example.test \
  --public-token-file work/olist/secrets/public-api-token.txt \
  --rate-limit-per-minute 30
```

The server itself is still loopback-only. For a local boundary smoke test, the
verifier's `--proxy-test` mode sends synthetic trusted-proxy headers to a loopback
HTTP endpoint; that verifies application behavior, not TLS or a real reverse proxy.
For example:

```bash
uv run --locked python scripts/verify_durable_http.py \
  --url http://127.0.0.1:8011 \
  --token-file work/olist/secrets/public-api-token.txt \
  --output-dir runs/public-verification \
  --proxy-test \
  --proxy-host api.example.test \
  --olist-root "$PWD"
```

Against a real HTTPS proxy, omit `--proxy-test` and `--proxy-host`, set `--url` to
the HTTPS origin, and supply either `--olist-root` for an independent local
read-only count check or both `--expected-coverage NUM/DEN` and
`--expected-origin NUM/DEN`. The verifier checks anonymous liveness, unauthenticated
metrics rejection, authorized readiness, independent counts, terminal replay,
same-key conflict, and that replay/conflict do not spend additional durable budget.
It writes a redacted receipt and never starts or restarts the service.

To verify a prior passed smoke after restarting the parent/API process, use its
receipt as the baseline:

```bash
uv run --locked python scripts/verify_durable_http.py \
  --url https://api.example.test \
  --token-file work/olist/secrets/public-api-token.txt \
  --output-dir runs/public-restart-verification \
  --replay-only runs/public-verification/report.json
```

This replay-only mode checks that the saved investigation IDs and semantic result
hashes persist and that the budget did not decrease. Restarting the API process is
not required by this client; perform the service restart separately and only after
the baseline receipt has been saved.

## Backup and recovery boundary

The AWS lab design places SQLite on encrypted EBS and backup artifacts in the
stack-private versioned S3 bucket. Quiesce the API before taking a backup so the
ledger and graph checkpoint state are captured at a known point. The release
template provisions a backup operation that uses SQLite's backup API, checks
`PRAGMA integrity_check`, and writes a SHA-256 receipt. Once that stack is deployed,
the scoped command is:

```bash
scripts/aws-release/operate-instance.sh \
  --stack-name llm-ops-lab-release-20261012 \
  --action backup \
  --run
```

The isolated verifier checks the restored copy against the receipt, SQLite integrity,
and table counts:

```bash
scripts/aws-release/operate-instance.sh \
  --stack-name llm-ops-lab-release-20261012 \
  --action verify-backup \
  --backup-key backups/sqlite/<UTC-stamp>/investigations.sqlite \
  --run
```

That command verifies an isolated restored copy; it does not switch the service to
that copy or prove a complete application recovery. Before reopening writes after a
restore, choose the latest verified quiesced backup, confirm ledger and dataset
bindings, and reconcile the budget conservatively against calls that may have reached
the provider after that backup. Never lower the reserved-call count to the snapshot's
older value. If the escaped-call count cannot be established, keep the service
closed to new investigations until an operator records a safe nondecreasing budget
reconciliation. Do not describe this exercise as high availability: host or AZ loss
interrupts service until restore, and no automatic failover or RPO/RTO objective is
provided.

## Verification evidence and current limits

The local PostgreSQL integration gate completed with **731 tests passed, zero
skipped**, and Ruff passed. Two real hosted-model HTTP smoke questions against the
published local PostgreSQL snapshot matched independent counts: **462/842**
unlinked contracts and **60/8,000** leads with missing origin. Those are two known
smoke questions, not a general quality benchmark; the evidence does not claim every
source row was hashed. The durable parent/worker restart replay also preserved the
saved investigation results and its reserved budget (2 of 8 before and after replay).

The 2026-10-12 AWS exercise also passed trusted HTTPS smoke, parent restart replay,
worker SIGKILL recovery with no blind retry, and S3-backed restoration. A logical
PostgreSQL dump was restored into a separate database on the same RDS instance;
all 14 checked tables/views matched row counts and deterministic content digests.
The restored SQLite ledger preserved its full logical contents, 4/32 reserved calls,
and one unknown pending attempt. Replays and two fresh hosted-model investigations
then passed against the restored PostgreSQL and SQLite targets.

See the [release report](olist-durable-release-20261012.md) and
[sanitized receipts](../evidence/olist-durable-aws-20261012/). This is a short-lived,
single-host recovery exercise. It does not establish host-loss recovery, automatic
AZ failover, production uptime, an RPO/RTO objective, or an SLA.
