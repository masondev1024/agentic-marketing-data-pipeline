# Local API UltraQA report

Result: **ULTRAQA COMPLETE** after implementation/review corrections and final dynamic verification.
Operator: assistant. Date: 2026-10-07 KST. This is a bounded local-service check.

## Goal and success criteria

Serve the existing pinned offline MLX inference through HTTP; distinguish generated/refused/error results;
bound admission and generation; recover by reaping failed workers before replacement; preserve diagnostics.
Stop after code/architecture review, full tests/lint, real HTTP normal/failure/restart checks and cleanup.
No commits, publishing, deployment, secrets, external model API or paid resources were used.
The server remained bound to loopback. Signals targeted only PIDs returned by the test-owned supervisor.

## Scenario matrix

| ID | User/failure model and setup | Harness / expected signal | Actual result | Evidence / cleanup |
|---|---|---|---|---|
| HTTP-01 | Ordinary caller, cached real Qwen3-4B | POST normal question, 200 with source/version/ID | Passed; repeated seed and prompt gave same answer/PID | real_generation, same_seed_repeat; server stopped |
| HTTP-02 | Unanswerable local corpus question | Weather question, no generation | 200 abstained, model_executed=false | refusal_no_generation; records retained |
| HTTP-03 | Adversarial question asks to ignore instructions/read a path | Related-input gate must not execute a tool/file request | Search refused without generation; no tool execution facility exists | injection_like_out_of_scope; one case, not general injection guarantee |
| HTTP-04 | Evidence mode caller | Selected quotes must resolve to original spans | Generated, quote_membership=verified, semantic_quality=unverified | evidence_mode; spans/hashes rechecked |
| INPUT-01 | Invalid JSON, unknown path/model field, oversized body, browser Origin | 422/422/413/403 | Passed through actual TCP HTTP | http_invalid records; no extra worker |
| INPUT-02 | Duplicate keys, NaN, invalid UTF-8 surrogate, bool-as-int, unknown options | Reject before model admission | Passed | tests/test_service.py; fixture processes reaped |
| INPUT-03 | Chunked body without Content-Length; stalled body sender | 413 and 408, no model execution | Passed in ASGI harness | fixture-only timing/byte tests; no open socket |
| INPUT-04 | Remote ASGI peer spoofs loopback Host | 403 before app route | Passed after peer-IP fix | remote-peer regression; transport fixture only |
| INPUT-05 | Valid character length but over model token budget | Both modes 422, same worker PID | Passed with actual model tokenizer | token_limit_direct/evidence-v1; no reload |
| CAP-01 | Burst caller while one active and one queued | Third request 429; admitted requests complete | Passed on real model | bounded_overload/concurrent_active/concurrent_queued |
| CAP-02 | Cancelled HTTP coroutine during active work | Active capacity cannot be reused early | Passed with deterministic sleeping IPC fixture | cancellation regression; fixture reaped |
| CAP-03 | Pending request whose deadline already expired | Never sent to worker | Passed; worker PID unchanged | expired-queue regression; fixture trace retained only in test temp dir |
| FAIL-01 | Real idle worker killed by test | Parent detects, reaps, loads a new worker | Passed; post-recovery generation 200 | worker_crash_recovery/after_crash_generation |
| FAIL-02 | Real worker stopped by test while request admitted | Live stays responsive; active 504; queued 503; kill/reap/reload | Passed; timeout 30.01s with 30s budget, live observed 0.006s | health_during_hang/active_deadline/timeout_recovery |
| FAIL-03 | Fixture startup hangs or corrupts IPC | Bounded timeout/restart budget; failed state or recovery | Passed | startup-hang/invalid-IPC fixtures; no external effects |
| FAIL-04 | Generation fails after partial tokens | Preserve model identity/execution/tokens; no partial answer returned | Passed after trace fix | fake-runtime regression; not real GPU failure evidence |
| STORE-01 | Result write raises OSError | storage_write_failed 500; no healthy-worker reload | Passed after classification fix | storage regression; no user artifact deleted |
| STORE-02 | Audit destination unwritable | Never report successful answer delivery | 500 audit_write_failed | temp path changed into a directory; original temp log preserved |
| STORE-03 | Slow/full audit writer, then caller cancellation | Event loop responsive; writer count stays bounded after cancellation | Passed after audit task admission fix | blocked-writer regression, release event and tasks cleaned |
| LIFE-01 | Normal parent shutdown with active/pending work | Fail pending work and reap child | Passed with IPC fixture | shutdown test, PID existence checks |
| LIFE-02 | Full real-server shutdown then fresh start | Old parent/worker gone, port closed; new server generates | Passed | server_restart and two cleanup records |
| COMPAT-01 | Existing CLI learner command | Same model messages/settings/token hash/output | Passed for one known question; its semantic error remains | 243ce5c1-de06-4001-8cc2-2d75f1357b16 |
| STATE-01 | Old orchestration state or prior run files | Service must not resume stale inference from these files | Service doesn't consume .omx or prior results; new worker/fresh IDs observed | Architecture/read-only inspection plus restart; stale orchestration state not an HTTP input |
| TREE-01 | Pre-existing untracked project | Preserve unrelated files and corpus | Repo was already entirely untracked; only scoped implementation/deps/tests/docs added or edited | cli.py backup in ignored work/; corpus hash lock unchanged |
| MISLEAD-01 | HTTP 200 or console “success” interpreted as model correctness | Inspect status/validation and actual JSON, not success text | Quality explicitly unverified; refusal still 200 | semantic_quality fields and prior day10 quality result |
| FLAKE-01 | Timing and subprocess tests | Recheck changed failure paths and final full suite | Targeted and full suites passed; no quarantines/retries masking failures | 236 passed final; no load/p95 claim |

Stop/continue strings inside a question have no service-control capability. Multi-chat state cancellation
is not part of this HTTP service. Parent SIGKILL while a GPU child is already hung, real disk exhaustion,
power-loss durability, OS reboot, concurrent deployments and network proxies are outside verified conditions.

## Commands and evidence

- `[0] uv run --locked pytest -q` — 236 passed, 1 TestClient deprecation warning, final run 5.34s.
- `[0] uv run --locked ruff check .` — all checks passed.
- `[0] uv run --locked pytest -q tests/test_service.py` — 41 passed after final fixes.
- `uv run --locked llm-lab-serve --port 18765 --output-dir runs/service-validation --queue-size 1 --request-timeout 30` — actual test server, stopped by owned SIGTERM.
- `[0] uv run --locked python work/verify_api_http.py` — real HTTP/MLX, worker kill/stop/recovery probes.
- Real-server restart and invalid-input probes used bounded httpx requests and explicit assertions.
- `[0] uv run --locked python work/finish_api_report.py` — source lock, response/run/request-ID correlation,
  original quote spans, source hashes, existing CLI comparison and final evidence generation.

The local `work/` harnesses and raw `runs/` are intentionally retained as ignored reproduction/debug artifacts.
Committed-style deliverables are the source/tests and reviewed docs/evidence; no actual commit was created.
New tests and `tests/fixtures/service_worker.py` are intentional project test files, not production fallback modes.
No HTTP request can select the fixture backend.

## Failures found and fixes

1. **Evidence input overflow consumed worker restart budget.** The pipeline stored typed input-limit errors,
   but the worker mapped all pipeline errors to 500/restart. It now maps InputLimitError to 422/no restart.
2. **Direct failures lost execution provenance.** The generation trace was merged only on success.
   Identity is now set first and mutable trace is preserved in finally.
3. **Result storage failures looked like broken model workers.** Save errors now have storage_write_failed
   and do not restart an otherwise healthy model.
4. **Host validation alone was insufficient.** Actual peer IP is checked and forwarded-header rewriting disabled.
5. **Synchronous or incompletely bounded audit writes threatened request control.** Writes now run outside
   the event loop with serialized storage and a bounded retained task set; rejected capacity does not recurse
   into more audit writes, and cancellation cannot release a writer slot early.

All five findings have focused regressions and independent rechecks. Final code review APPROVE;
architecture CLEAR within the local-service scope.

Harness correction: after adding actual peer validation, TestClient's synthetic `testclient` address was
rejected. The fixture was changed to an explicit loopback peer, preserving a separate non-loopback rejection
test. The interrupted intermediate suite was not reported as a product pass. Initial lint formatting issues
were corrected before final validation. No quality rubric or expected service error was relaxed to make tests pass.

## Cleanup, limits and next owner

Both real server instances and their workers were reaped; TCP 18765 was closed after final checks.
No background inference server remains from this verification. No unrelated process or file was removed.
Corpus, scope, model revision and prompt variants stayed fixed; source/lock hashes used in final probes match
the reviewed implementation. Orchestration state is closed after report generation.

This milestone establishes local HTTP and process-recovery behavior, not production quality of model answers.
Known remaining work includes independent semantic evaluation, log rotation/disk quota, authentication before
any exposure, an OS service manager, and workload-derived service targets. TestClient emits an upstream
httpx deprecation warning in the current locked versions; test-framework migration remains separate work.

Next owner action: learner starts `llm-lab-serve`, checks live/ready, and compares a normal and refused answer
using the commands in [the API guide](day-11.md). The assistant's verification is not learner execution evidence.

[Full machine-readable verification](../evidence/day11-assistant-local-api.json)
