# Working agreement

- This is a mentored beginner LLM project targeting a bounded release by 2026-10-11 KST.
- Explain the data flow and failure mechanism before changing core behavior.
- Distinguish assistant verification from learner execution and comprehension.
- Deliver a working vertical slice at each milestone. Do not implement the whole curriculum
  ahead of the learner unless they explicitly request autonomous implementation.
- Use synthetic or licensed public data. Never import private workplace incident logs.
- Pin dependencies, model revisions, prompts, corpus and evaluation versions.
- Never fabricate quality metrics, real users, uptime or operational experience.
- Do not commit, push, create a PR, deploy publicly or provision paid resources without
  explicit user authorization. Local setup and verification are in scope.
- Run `uv run --locked pytest -q` and `uv run --locked ruff check .` after code changes.
- Real model behavior needs a real inference experiment; mocked/unit tests do not prove it.
- Keep `.omx/`, `work/`, `runs/` and credentials out of Git. Publish reviewed evidence only.
