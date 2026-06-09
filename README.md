# Printer Log Analytics

Production-oriented backend framework for long-lived industrial analysis of metal 3D printer logs.

The first profile is **Laser Systems M-450-M**, with legacy **M-350** treated as the same printer class. The primary profile name is not M450-S.

---

## 🚀 Установка на компьютер принтера (инструкция для оператора / агента)

Пошагово, чтобы всё заработало с нуля. Все команды — в PowerShell.

### 0. Что нужно заранее
- **Docker Desktop** установлен и запущен (значок кита в трее «зелёный»).
- Папка проекта скопирована с флешки на диск, например в `C:\printer-log-analytics`.

### 1. Папка для логов принтера
**Складывай новые логи сюда:**

```
C:\PrinterLogs
```

> ⚠️ Именно в `C:\PrinterLogs` напрямую — **без** подпапки `\incoming` (раньше была `incoming`, теперь не нужна).
> Если папки нет — создай её: `New-Item -ItemType Directory C:\PrinterLogs`.
> Можно класть файлы как есть (`23.03.2026.log`, `23.03.2026_sensors.log`, `23.03.2026_time.log` и т.д.)
> или сразу всю папку с флешки. Система сама сгруппирует файлы по печатям.
> Альтернатива: перетащить файлы прямо в дашборде на странице «Загрузка» — попадут в ту же папку.

### 2. Настроить `.env` (один раз)

```powershell
cd C:\printer-log-analytics
Copy-Item .env.example .env
```

В `.env` для Windows оставить:

```env
RAW_LOGS_HOST_PATH=C:\PrinterLogs
RAW_LOGS_CONTAINER_PATH=/mnt/raw_logs
LLM_BASE_URL=http://host.docker.internal:1234/v1
```

### 3. Запуск

```powershell
cd C:\printer-log-analytics
docker compose up -d
```

Первый запуск качает образы (несколько минут). Дальше — секунды.
Для автозапуска при включении ПК положи ярлык `deploy\autostart-windows.bat` в `shell:startup`.

### 4. Открыть дашборд

```
http://localhost:8000
```

Через ~15 секунд после старта система сама просканирует `C:\PrinterLogs` и подтянет
новые печати. Графики и таблицы заполнятся автоматически.

### 5. Как добавлять новые логи потом
Просто **докладывай новые файлы** в `C:\PrinterLogs` (или перетаскивай в дашборде).
Система раз в час и при каждом старте проверяет папку и импортирует **только новое**.
Ничего вручную запускать не нужно.

### 📌 Важно: как работает дозагрузка (ответ на частый вопрос)
При добавлении новых логов система:
- **НЕ удаляет** старые сессии и графики;
- **НЕ переанализирует** заново всё подряд;
- анализирует **только новые печати** и **дополняет** дашборд.

Это работает за счёт детерминированного идентификатора сессии
(`session_<дата>_<хеш имён файлов>`): одна и та же печать всегда получает один и тот же
ID, поэтому уже загруженные печати при повторном сканировании пропускаются, а не дублируются.

> Разовая операция после обновления версии: если старые сессии были импортированы
> ещё до исправления подсчёта времени — один раз выполни пересчёт:
> `docker compose exec api python recompute_session_times.py` (можно с `--dry-run` для предпросмотра).

---

## Architecture

The system separates the stable framework from printer-specific interpretation:

- `domain/`: profile-agnostic models for printers, sessions, files, events, transitions, operator context, material/powder/gas/maintenance lifecycle, quality outcomes, anomalies, hypotheses, insights, knowledge, reports, and LLM runs.
- `parsers/`: reusable parser framework plus tolerant file-family parsers.
- `profiles/m350/`: M-350 plugin, signal seeds, stateFlow mapping seeds, phases, and rule seeds.
- `analytics/`: timestamp normalization, event deduplication, phase segmentation, feature extraction, rules, statistical anomaly helpers, causal/dependency graph, signal dictionary, and quality correlation.
- `operator_journal/`: structured operator event parsing and auditable production context updates.
- `background_reanalysis/`: daily review support and bounded historical pattern discovery.
- `reporting/`: deterministic JSON/Markdown reports, Plotly artifacts, and optional LLM narrative layer.
- `api/`, `worker/`, `scheduler/`, `worker/watcher.py`, `cli/`: service entrypoints.

Core analytics works without any LLM. LM Studio/Qwen is only a downstream narrative layer that receives compact structured evidence JSON, never raw duplicated logs or direct filesystem paths.

## Docker Compose

Copy `.env.example` to `.env` and adjust secrets and paths:

```powershell
Copy-Item .env.example .env
```

For Windows Docker Desktop, keep:

```env
RAW_LOGS_HOST_PATH=C:\PrinterLogs
RAW_LOGS_CONTAINER_PATH=/mnt/raw_logs
LLM_BASE_URL=http://host.docker.internal:1234/v1
```

Start core services:

```powershell
docker compose up --build
```

## Routine Operator Import

Routine log import does not require terminal commands.

Normal workflow:

1. Operator copies a log folder or ZIP from USB into `C:\PrinterLogs\incoming`.
2. The `watcher` service sees the new folder/ZIP and creates an `ImportJob`.
3. The watcher sends a restricted API notification for Telegram/OpenClaw:

   `Найдена новая папка логов: <name>. Начать импорт?`

   Buttons:

   - `Импортировать`
   - `Игнорировать`
   - `Проверить позже`

4. Nothing is parsed or analyzed until the operator presses `Импортировать`.
5. After confirmation, the system checks that files are readable and stable, calculates checksums, ingests logs, groups sessions, runs analysis, generates reports, and sends a summary with report links and missing-context questions.
6. If files are still copying, the operator receives:

   `Файлы еще копируются. Повторю проверку через N секунд.`

Import configuration:

```env
INCOMING_PATH=/mnt/raw_logs/incoming
WATCH_MODE=filesystem_events
REQUIRE_OPERATOR_IMPORT_CONFIRMATION=true
FILE_STABILITY_SECONDS=60
FILE_STABILITY_RETRY_SECONDS=30
IMPORT_CONFIRMATION_TIMEOUT_HOURS=24
```

Import job statuses:

`detected`, `awaiting_operator_confirmation`, `postponed`, `ignored`, `checking_stability`, `importing`, `analyzing`, `reporting`, `done`, `failed`, `needs_operator_context`.

Import API:

```http
GET /imports
GET /imports/{id}
POST /imports/{id}/confirm
POST /imports/{id}/ignore
POST /imports/{id}/postpone
POST /imports/{id}/retry
```

Restricted Telegram/OpenClaw callbacks use `X-API-Token: <AGENT_API_TOKEN>`:

```http
POST /agent/import-detected
POST /agent/import-callback
POST /agent/imports/{id}/send-confirmation
POST /agent/imports/{id}/send-summary
```

CLI commands remain available for administrator/debug workflows only.

Optional services:

```powershell
docker compose --profile telegram up --build
docker compose --profile openclaw up --build
```

During local development, `docker-compose.override.yml` mounts the source tree into
`telegram-bot`. After code-only bot changes, restart it without rebuilding dependencies:

```powershell
docker compose --profile telegram up -d --no-deps telegram-bot
```

Rebuild the bot image only when `Dockerfile.telegram-bot` or
`requirements.telegram-runtime.txt` changes.

## Telegram Operator Voice Input

The Telegram bot accepts operator context as text or voice. Buttons are optional: an operator can simply send a voice message such as:

`Поставили новый баллон аргона AG-042, давление 180 бар`

The bot downloads the Telegram voice note into temporary container storage, transcribes it locally with `faster-whisper`, and first sends the transcript back to the operator for confirmation.

Confirmation buttons:

- `Отправить`: parse the confirmed transcript into a structured `OperatorEvent`.
- `Перезаписать`: discard the pending transcript and ask the operator to record the message again.

Every operator text or voice input is also stored as a separate `OperatorJournalEntry` in `/operator-journal`. This journal entry preserves the original operator information, voice metadata, transcript, duplication group, and export payload independently from the structured analytical event. That gives the system a portable operator log that can be duplicated or migrated when the project/platform changes.

The bot also has a `Тест` button. Test mode shows the same operator menu and confirmation flow, including voice confirmation buttons, but does not write to `/operator-journal`, does not create `OperatorEvent` records, does not call import APIs, and does not send voice to the STT model. A `Выйти из теста` button returns the chat to normal operation.

Voice transcription configuration:

```env
VOICE_TRANSCRIPTION_ENABLED=true
VOICE_TRANSCRIPTION_PROVIDER=faster_whisper
VOICE_TRANSCRIPTION_MODEL=small
VOICE_TRANSCRIPTION_LANGUAGE=ru
VOICE_TRANSCRIPTION_DEVICE=cpu
VOICE_TRANSCRIPTION_COMPUTE_TYPE=int8
VOICE_TRANSCRIPTION_MODEL_CACHE=/models/stt
```

The model cache is stored in the Docker named volume `stt_model_cache`. If transcription is disabled or unavailable, the voice note is still saved as an unverified operator event and the bot asks for a short text clarification.

If HuggingFace downloads are unstable, preload `medium` from ModelScope into the same Docker volume:

```powershell
.\deploy\download_faster_whisper_medium.ps1
```

The bot automatically prefers `/models/stt/preloaded/faster-whisper-medium` when all required model files are present, so it will not call HuggingFace for the model after preload.

Security boundaries in Compose:

- no privileged containers;
- no Docker socket mounts;
- API has no raw log mount by default;
- worker receives raw logs read-only;
- watcher receives raw logs read-only and only creates ImportJobs/notifications;
- PostgreSQL, Redis, MinIO, Telegram, OpenClaw, and LM Studio receive no raw log mount;
- Telegram/OpenClaw can only call restricted API endpoints;
- LM Studio runs on the Windows host outside Docker.

## LM Studio / Qwen3.6

Start LM Studio on the Windows host, enable the OpenAI-compatible server, and load `qwen3.6`.

Default connection:

```env
LLM_PROVIDER=lmstudio
LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_MODEL=qwen3.6
LLM_API_KEY=lm-studio
LLM_TIMEOUT_SEC=300
LLM_TEMPERATURE=0.1
LLM_TOP_P=0.9
LLM_MAX_TOKENS=8192
```

If LM Studio is unavailable, deterministic JSON and Markdown reports still work. LLM request metadata is represented by `llm_runs`.

## API

Health:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Ingest a folder:

```powershell
Invoke-RestMethod http://localhost:8000/sessions/ingest `
  -Method Post `
  -ContentType application/json `
  -Body '{"folder":"/mnt/raw_logs"}'
```

Generate a report:

```powershell
Invoke-RestMethod http://localhost:8000/sessions/auto_group_1/reports/generate -Method Post
```

Restricted agent endpoints require:

```http
X-API-Token: <AGENT_API_TOKEN>
```

## CLI

From inside the app container or local Python environment:

```powershell
pla ingest-session C:\PrinterLogs
pla generate-report auto_session --folder C:\PrinterLogs
pla add-operator-event "Поставили новый баллон аргона, баллон AG-042, давление 180 бар"
pla run-background-analysis --window 90d --max-iterations 10
pla llm-test
```

## M-450-M / M-350 File Families

Supported initial file families:

- `*.log`: main event log with cp1251/utf-8 fallback and Russian text support.
- `*_burn.log`: layer-oriented process data, repeated header tolerance, unknown column preservation.
- `*_time.log`: layer timing and phase concept extraction.
- `*_sensors.log`: chunk-friendly telemetry with startup garbage diagnostics.
- `*_Monitor100.log`: primary discrete state transitions; scans embedded timestamps in glued lines.
- `*_Monitor200.log`: auxiliary rare-code source; unknown codes preserved.
- `*_stateFlow.log`: primary automation-state source; streaming run-length transition compression.
- `*_stateFlowData.log`: empty/text/binary/unsupported classifier.
- `table_temp.log` / similar: chunk-friendly table temperature source.
- `*_error.log`: low-trust auxiliary source; empty file is data-quality evidence, not proof of no errors.

## Historical Reanalysis

Historical discovery is bounded and auditable:

- default daily window;
- configurable date range;
- up to 10 iterations;
- early stop on no data, convergence, insufficient sample size, missing context, or budget exhaustion;
- generated insights start as draft/needs_review and require human confirmation.

Default causal thresholds:

- `n < 3`: observation only;
- `3 <= n < 10`: weak hypothesis;
- `n >= 10`: candidate insight if effect and data quality are sufficient.

Correlation is never presented as proven causation.

## Backups

PostgreSQL backup:

```powershell
.\deploy\backup\backup_postgres.ps1
```

PostgreSQL restore:

```powershell
.\deploy\backup\restore_postgres.ps1 -SqlFile .\backups\postgres\printer_logs-YYYYMMDD-HHMMSS.sql
```

MinIO backup:

```powershell
.\deploy\backup\backup_minio.ps1
```

MinIO restore:

```powershell
.\deploy\backup\restore_minio.ps1 -Archive backups\minio\minio-data.tgz
```

Configure application log retention with `LOG_RETENTION_DAYS`.

## Adding Future Printer Profiles

Create a new directory under `profiles/<profile_id>/` with:

- `profile.py` implementing `PrinterProfilePlugin`;
- signal dictionary seeds;
- mappings and state-machine metadata;
- phase/rule seeds;
- parser registration.

Framework parsers can be reused when file families are compatible. Profile-specific enrichment must layer on top of canonical events and transitions rather than changing the generic domain model.

## Development

Run tests:

```powershell
python -m pytest
```

Validate Compose:

```powershell
docker compose config
```
