# Printers-companion — Claude Code context

## Memory

Project memory is in `.claude/memory/MEMORY.md` — read it at the start of every session.

## Project

M350 SLM analytics system: FastAPI + PostgreSQL + Redis + MinIO, Docker Compose.

- Start: `docker compose up -d`
- Docs: `http://localhost:8000/docs`
- Tests: `DATABASE_URL="sqlite:////tmp/pla-test.db" APP_ENV=test .venv/bin/python -m pytest tests/`
- Update (Windows): `.\deploy\update.ps1`
