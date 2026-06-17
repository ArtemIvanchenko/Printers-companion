---
name: pla-test-recipe
description: How to run the printer-log-analytics test suite locally without Docker
metadata: 
  node_type: memory
  type: project
  originSessionId: d2ae555d-424c-4bce-85d5-2b4d71e94006
---

Project `printer-log-analytics` (GitHub repo `ArtemIvanchenko/Printers-companion`) lives at `/Users/admin/Documents/CODEX/Printers-companion` (as of 2026-06-10; previously `~/Desktop/printer-log-analytics`). Its `.env` forces a Postgres `DATABASE_URL`, and the system Python is 3.14 (too new for the pinned deps).

To run tests without the full Docker stack:
- Use a Python **3.11** venv (`/Users/admin/.local/bin/python3.11`); deps: pytest, fastapi, pydantic, pydantic-settings, sqlalchemy, httpx, PyYAML, charset-normalizer, python-multipart, networkx, scipy, scikit-learn, statsmodels, plotly, polars, **redis, minio** (the last two are needed just to import `api.main`).
- Run with `DATABASE_URL="sqlite:////tmp/pla-test.db"` (a **file**, not `:memory:` — engine uses NullPool so in-memory isn't shared across connections) and `APP_ENV=test`.
- The tests mock the DB but integration tests need a real schema; `tests/conftest.py` has a session-scoped autouse `_ensure_schema` fixture that runs `create_all()` for sqlite.
- `APP_ENV=test` also skips the `_discover_llm` network probing and default-token warnings in `core/config/settings.py` — without it, settings construction does blocking HTTP and the suite takes ~13s instead of <1s.

Baseline: **164 passed, 1 xfailed** as of the security-hardening pass on 2026-06-10 (was 132 after the analytics refactor, 84 before that). The `-q` run sometimes fails to flush its final summary line under output redirect — grep for `passed|failed|xfail` (or trust the exit code) to confirm counts. Note: `.claude/launch.json` in the repo is continually re-modified by a background process — exclude it when staging (`git add` explicit paths, never `-A`).
