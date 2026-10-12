# Olist durable service release — 2026-10-12

Status: implementation in progress; no deployment or completion claim.

The user's goal is durable Olist resume, public-service controls, AWS deployment
and recovery verification, GitHub publication, and an evidence-bounded portfolio update.
The generated Ouroboros Seed `seed_8fd31fdc6a2e` captured the scope but supplied only
a generic acceptance criterion. Its A label is not implementation evidence. This
addendum supplies the concrete engineering checks; the original Seed is preserved.

## Architecture and failure contract

- Preserve the existing local CLI/API and the uncommitted Super HTTP implementation.
- One API supervisor and one investigation worker; hosted Super needs no MLX/GPU.
- Authenticated principal + client idempotency key bind a canonical request. Changed
  payload/config under an existing key conflicts; terminal results can be replayed.
- LangGraph execution and a durable SQLite action ledger live on one persistent,
  encrypted host volume. This release is single-host, not distributed/high-availability.
- Reserve model budget before dispatch, including repair. A pending attempt after
  process death is unknown and cannot be retried automatically. Committed responses
  are reused even if the following graph checkpoint was not committed.
- PostgreSQL tools keep the existing read-only role and repeatable-read transaction.
  Persist a complete validated evidence bundle atomically; never combine partial
  evidence from different database snapshots. Pin version and history cutoff.
- Public ingress is HTTPS through a trusted reverse proxy. Authentication, explicit
  Host/Origin rules, bounded body/concurrency/rate, private metrics and redacted errors
  precede model dispatch. Durable call budget survives parent and worker restarts.
- Proposed AWS lab: dedicated VPC, small EC2/SSM, private Single-AZ PostgreSQL RDS,
  encrypted storage and private S3 artifacts/backup. No GPU, NAT or ALB. The user
  approved a USD 5 budget and deletion of all resources/backups created for this
  release after verification. Existing local data and unrelated resources remain outside scope.
- Backup/restore must quiesce execution and preserve/check ledger + dataset bindings.
  Never regain spent model budget by restoring an older backup; restored service is
  initially read-only/replay-only until budget reconciliation.

## Acceptance ledger

| ID | Required proof |
|---|---|
| G1 | Subprocess crash tests at pre-dispatch reservation, provider return, durable response commit and final result commit; no blind remote replay; actual call counters checked |
| G2 | Identical authenticated key replays; changed request/config conflicts; concurrent processes cannot share ownership; restart preserves budget; source/version mismatch fails closed |
| S1 | HTTP tests: missing/wrong auth, duplicate auth, invalid host/origin, oversized/malformed body, rate/queue limits, protected metrics, same-key conflict and replay, no secret/prompt leakage |
| S2 | Existing regression suite plus PostgreSQL integration and Ruff pass; Linux container starts without MLX; real hosted model + actual published PostgreSQL data produce verified counts |
| A1 | Reviewed IaC plan, scoped identity/resource inventory and approved cost/cleanup policy; public HTTPS authenticated smoke, anonymous rejection and restricted DB/SSH exposure |
| A2 | Actual worker/parent stop-restart exercise and backup restoration to a separate target; business counts/hashes, saved result and budget state compared; restoration limitations recorded |
| A3 | Cleanup according to user policy, post-cleanup resource inventory; historical billing separated from resource absence |
| P1 | Public secret/artifact review; commit/push to existing project dev/main, CI success on published SHA, no unrelated files/repos changed |
| P2 | Existing portfolio source updated only with successful evidence, build/tests and desktop/mobile preview; deploy existing site and verify live content |

Canonical local gate: `uv run --locked pytest -q` with isolated PostgreSQL admin
DSN supplied privately, then `uv run --locked ruff check .`. Exact cloud and HTTP
commands and receipts will accompany the implementation. Mocks do not prove hosted
model quality; one-day recovery exercises do not establish production uptime/SLA.

## Publication scope

Project source and target: `masondev1024/agentic-marketing-data-pipeline`, current
`dev`, release to `main` after verification. User explicitly authorized publication.
Portfolio: existing `jungseongheon.org` deployment; preserve other projects/content.
No raw Olist source, DB dumps, credentials, private AWS account inventory, Terraform
state or internal execution logs may enter Git or public portfolio assets.
