---
name: printers-companion
description: "Overview of Printers-companion repo — architecture, current version, modules, deployment, development history, and roadmap"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0489ce00-88e4-4bf6-872a-8fb29ca03cd2
---

# Printers-companion

**GitHub:** `ArtemIvanchenko/Printers-companion`
**Description:** Internal analytics and archive system for SLM M-450M metal 3D printer. Internal use only — no customer-facing interface.
**Current version:** v1.3.0 (released 2026-06-10)
**Tests:** 157 passing

## Architecture

Microservices via Docker Compose:
- **api** — FastAPI, serves dashboard + REST endpoints
- **worker** — Celery background tasks (report generation, import jobs)
- **scheduler** — APScheduler (daily review, historical reanalysis, cron from `DAILY_REVIEW_CRON` / `HISTORICAL_REANALYSIS_CRON`)
- **telegram-bot** — frozen since 2026-06-10 (see [[pla-telegram-frozen]])
- **mcp** — MCP chat interface
- **PostgreSQL** — main storage (BuildSession, CanonicalEvent, ImportJob, etc.)
- **Redis** — task queue, update event log
- **MinIO** — large report storage (offloaded when >1 GB jsonb limit)
- **Watchtower** — auto-update from Docker Hub (hourly)

## Log parsers

Target: M-350 SLM printer log files in flat folder `C:\PrinterLogs` (no `/incoming` subfolder).

Parsers: `event_log`, `burn`, `time_log`, `sensors_log`, `monitor100/200`, `stateflow`, `table_temp`, `error`

Key parser facts:
- `time_log` layer numbers are **cumulative across machine lifetime** — use `event_log` timestamps for session duration
- `monitor100` daemon runs all day → excluded from duration calculation
- `s_` prefix STL files (MagicsX) include support structures (volume much larger than part)
- `sensors.log` "Time" column is always expected
- Session `group_id` = `session_<date>_<sha256(sorted filenames)[:8]>` — deterministic, re-import is idempotent

## Dashboard (v1.3.0)

4-tab navigation: **Главная / Аналитика / Модели / Обслуживание**
- Главная: printer status, quick actions, recent sessions
- Аналитика: defect risk card, maintenance forecast card, data quality badge
- Модели: STL history, 3D viewer (Three.js)
- Обслуживание: (maintenance tab)

UI features: version badge + changelog modal, gear icon (Upload/Logs/Update), floating chat button (WIP), hot-reloading HTML templates.

## Key analytics modules

- `analytics/data_quality.py` — 0–100 score: layer/time continuity, sensor stuck/dropout, parser diagnostics
- `analytics/prediction/defect_risk.py` — heuristic (cold start) + logistic regression (≥8 labelled sessions)
- `analytics/prediction/maintenance.py` — Theil-Sen trend per signal, projects sessions-to-alarm-threshold
- `analytics/cross_session.py` — cross-session pattern recognition (Theil-Sen trends, MAD anomalies)
- `analytics/signal_stats.py` — full-resolution signal stats with Polars for large sensor logs
- `analytics/thresholds.py` — shared alarm threshold loader from `profiles/m350/signals.yaml`

## Deployment (Windows)

- Docker Desktop + Docker Compose
- Log drop folder: `C:\PrinterLogs` (flat, no subfolder)
- Auto-start: `deploy/autostart-windows.bat` → shell:startup shortcut
- Auto-update: `update.ps1` + Watchtower (hourly Docker Hub check)
- CI/CD: GitHub Actions self-hosted runner (`fix: bypass PowerShell ExecutionPolicy` — use `-ExecutionPolicy Bypass`)

## Development history highlights

| Date | Milestone |
|------|-----------|
| 2026-05-31 | Initial commit — M-350 monitoring system, all parsers, Docker infra |
| 2026-06-02 | Cross-session analytics, MinIO large-report offload, versioning system |
| 2026-06-04 | Real-log parser audit + complete fixes, auto-update + Watchtower |
| 2026-06-09 | Deep audit: 10 bugs fixed (import dedup, LLM failover, session ordering, etc.); data quality module; defect risk + maintenance prediction; deterministic group_id; CI/CD |
| 2026-06-10 | v1.3.0: Windows stability (psycopg2, Redis URL, rate limits), dashboard 4-tab redesign; self-hosted runner; backfill utility |
| 2026-06-11 | CI fix: PowerShell ExecutionPolicy bypass on self-hosted runner |

## Notable bugs fixed (June 2026)

- `DATABASE_URL` driver: must be `psycopg2` (not psycopg3, images don't have it)
- `REDIS_URL`: must use container hostname, not `localhost`
- Agent rate limit: raised 30→200 req/min (watcher sends 2 req/file × N files)
- Session payload was storing ~96 MB events inline → stripped to ~1.7 KB (events rehydrated from disk on demand)
- `start_ts`/`end_ts` were NULL on all sessions → fixed pipeline to persist timestamps
- Watcher confirmation path didn't call `build_group_overview` → sessions had empty graphs
- `layers` count was `max(layer_numbers)` (machine-lifetime cumulative) → fixed to `len(unique layer numbers)`
- Import job `source_file_id`/`event_id` were random `uuid4` → deterministic sha256 (no duplicates on re-import)
- LLM circuit breaker never tripped (providers returned `LLMResult(success=False)` instead of raising)

## Фаза 0 — реализована (2026-06-12, не закоммичено)

Клон: `/Users/admin/Documents/GitHub/Printers-companion` (venv `.venv`, py3.11).
- `domain/models/prints.py`: PrintRecord, PrintRecordFile, MachineParams (single-row id=1)
- Миграция `0002_print_records_and_machine_params` (Base.metadata.create_all — идемпотентно, паттерн репо)
- `storage/repositories/prints_repo.py` + `get_prints_repository` в api/deps
- `api/routes/prints.py` (CRUD + upload/download файлов, dedupe по sha256, s_-префикс → stl_supports) и `api/routes/machine_settings.py` (GET/PUT /settings/machine, partial update)
- MinIO: 4 новых бакета (stls/magics/photos/docs) в settings + `ObjectStore.ensure_all_buckets()` на старте API
- Rename M-350 → M-450M: signals.yaml `model:`, profile.py legacy_names, тест
- Dashboard: таб «Архив» (таблица карточек, загрузка файлов, 3D-просмотр STL из MinIO через существующий Three.js вьювер, форма параметров машины)
- **Найден и исправлен баг v1.3.0**: 8 JS-функций вызывались из HTML, но не были определены (showAnalyticsPage, showMaintenancePage, toggleGear, showGearPage, openNewPrint, openChangelog, closeChangelog + сломанный showMainTab со ссылкой на несуществующий charts-subnav) — навигация дашборда была полностью сломана; переписан весь nav-блок
- Тесты: 179 passed (+25 новых в tests/test_prints_api.py); 1 pre-existing fail test_docker_security (compose read_only, не связан)
- repo методы для Фазы 2 уже готовы: find_unlinked_records_near(), link_session(record_id, session_id, session_start) — session_start перезаписывает printed_at

### Доработка по фидбэку (2026-06-13)
- `printed_at` на PrintRecord: явно > из имени карточки/файла (reuse `parsers/common/timestamps.py::date_hint_from_filename`) > из start_ts привязанной сессии (авторитетно). Сортировка/фильтры архива по coalesce(printed_at, created_at)
- `powder_cost_rub_per_kg` на PrintRecord — снимок цены на момент регистрации, оператор вводит сам; дефолт = последняя использованная (GET /prints/defaults), фоллбэк MachineParams
- Материалы — свободный текст (не enum); список для UI = ключи material_densities из MachineParams; таблица материалов в настройках динамическая (+ Добавить материал)
- Поиск в архиве: q (имя, ilike — на sqlite кириллица регистрозависима, на PG прода ок), material, date_from/date_to
- DELETE /prints/{id} и /prints/{id}/files/{fid} с best-effort очисткой MinIO (ObjectStore.remove_object добавлен)
- Баг перезаписи файлов закрыт: object key = {record_id}/{checksum[:8]}_{filename}
- Тесты: 198 passed (+19); единственный fail — pre-existing test_docker_security

## Фаза 1 — реализована (2026-06-13, не закоммичено)

- Зависимости: trimesh 4.12.2, shapely 2.1.2, PythonSLM 0.6.1 (ставится без проблем на py3.11) — pyproject + requirements.heavy.txt + Dockerfile.base
- `analytics/prediction/stl_slicer.py`: trimesh section_multiplane + shapely, сэмплинг ≤400 сечений (midpoint слоёв), SliceResult с per-section areas/perimeters; mesh.volume вместо ручного тетраэдра
- `analytics/prediction/print_time.py`: два метода — "pyslm" (реальный hatching 12 сэмпл-слоёв через pyslm.hatching.Hatcher, калибровочный коэффициент measured/analytic) и "formula" (Excel-режим: hatch_speed как площадная мм²/с). Новый параметр `hatch_distance_mm` переключает семантику hatch_speed (задан → реальная скорость лазера мм/с + PySLM; пуст → Excel)
- `analytics/prediction/cost_estimator.py`: порошок (объём×плотность, override ценой из карточки/last_powder_cost), газ, фильтр (по ресурсу), платформа; отсутствующие ставки → warning, не ошибка
- `/upload/stl-estimate?material=X`: новое поле "prediction" (degrades to available:false + reason); старый ответ не сломан
- UI: селектор материала на STL-вкладке, карточка предсказания (часы/₽/слои/порошок, breakdown, метод), hatch_distance_mm в настройках
- Округление только в API-слое, датаклассы хранят полную точность
- Перфоманс: деталь 3600 слоёв → 0.7 с end-to-end
- Тесты: 214 passed (+16: слайсер, формула с точной математикой, pyslm-метод, себестоимость, API integration)

### Два режима расчёта (2026-06-13)

**Настоящая формула Excel оператора расшифрована** («расчёт стоимости Cталь.xlsx», лист «Время работы»):
`t_слоя = (√S/hd) × (4√S) / v × 1.2 × 0.8` — треки × длина трека (приближение периметром квадрата!) / скорость × коэффициенты. hd=0.12 захардкожен в Excel, контурная скорость НЕ используется, на лазеры НЕ делится (N3=1 в той печати). Старая память «t = S/v + P/v_c» была НЕВЕРНА.

- `/upload/stl-estimate?method=fast|accurate`: fast → mode="excel" (формула предприятия, воспроизведена бит-в-бит: 40.36 ч на данных оператора), accurate → mode="pyslm" (реальные траектории, 30 сэмпл-слоёв; деградация к "physics" без stl_bytes)
- `hatch_distance_mm` теперь ОБЯЗАТЕЛЕН в обоих режимах; hatch_speed всегда реальная скорость лазера мм/с — двойная семантика устранена
- UI: после выбора STL две кнопки «⚡ Рассчитать быстро» / «🎯 Рассчитать точно» + кнопка пересчёта другим методом в результате
- Валидация на реальной спирали с флешки: excel 24.3 ч (пропорционально Excel оператора ✓), pyslm 7.3 ч — расхождение 3.3×, ground truth неизвестен (логов 04.03.2026 на флешке нет); Фаза 3 (predicted vs actual) рассудит
- Тесты: 219 passed (+5)

## Фаза 2 — реализована (2026-06-13, не закоммичено)

- `domain/services/print_linking.py::auto_link_print_records(db)` — идемпотентный sweep: непривязанные PrintRecord ↔ сессии с start_ts в ±окне (Settings.print_link_window_hours=24, env-настраиваемо). Линкуются только однозначные 1:1 пары; неоднозначные пропускаются с логом (оператор решает через PATCH session_id). При линке printed_at перезаписывается start_ts сессии. ВАЖНО: db.flush() в конце — SessionLocal с autoflush=False
- Вызывается из всех 4 путей создания сессий: api/main._startup_import, uploads._trigger_rescan, sessions./ingest (возвращает print_record_links в ответе), worker/tasks.process_import_jobs
- `POST /prints/{id}/import-logs` — multipart .log/.zip → raw_logs folder → _trigger_rescan; дата из имени лога заполняет printed_at если пуст
- UI: чип «📋 логи» в строке карточки (только у непривязанных), alert + отложенный refresh 5с
- E2E проверено на реальных логах 06.04.2026 с флешки: карточка «06.04.2026_деталь» → ingest 59МБ sensors.log → автопривязка session_20260406_06a443be → printed_at уточнён до 16:18:50
- Тесты: 228 passed (+9: однозначная пара, обе неоднозначности, окно, занятая сессия, идемпотентность, import-logs endpoint)

## Фаза 3 + доработки Фазы 2 — реализованы (2026-06-13, не закоммичено)

Доработки Фазы 2:
- `GET /prints/{id}/session-candidates` + UI «🔗 найти сессию» (select кандидатов вкл. неоднозначные → ✓ → PATCH session_id)
- import-logs кладёт `metadata_json.log_import_hint = {date}` — явное намерение оператора; `_resolve_import_hints()` в auto_link разрешает хинт даже при неоднозначности между карточками (2 сессии в одну дату — всё ещё skip); хинт хранится до появления сессии
- UI: polling карточки после import-logs (5с × 12 попыток) вместо одного таймера

Фаза 3:
- Deps: ruptures 1.1.10, lightgbm 4.6.0 (requirements.heavy + Dockerfile.base + pyproject)
- `analytics/prediction/accuracy.py::prediction_accuracy(db)` — пары прогноз/факт из metadata_json.prediction + длительность сессии; suggested_correction_factor = median(actual/predicted) точного метода, ≥3 пар
- `POST /prints/{id}/estimate` — считает оба режима по STL карточки из MinIO, снапшот в metadata_json.prediction; `GET /prints/prediction-accuracy`
- MachineParams.time_correction_factor — множитель ТОЛЬКО к pyslm/physics scan time (excel-режим не трогаем, он уже откалиброван оператором); UI: блок калибровки в настройках с кнопкой «применить»
- `analytics/cross_session.py::detect_signal_shifts()` — 4-й детектор: ruptures PELT (l2, pen=3σ²), ступенчатые сдвиги ≥10%, MIN_SESSIONS_FOR_SHIFT=6; включён в run_cross_session_analysis (ключ "shifts")
- defect_risk: ≥20 меток (и ≥5 каждого класса) → LightGBM (shallow: leaves 7, depth 3, 60 rounds), модель сериализуется model_to_string(); 8–19 → логрегрессия; модели теперь имеют "type"
- UI архива: кнопка 📐 (ручной пересчёт прогноза), строка «📐 X ч / Y ч» под сессией
- Авто-прогноз: загрузка STL (file_type=stl) в карточку → BackgroundTasks `_auto_estimate` (свой SessionLocal, best-effort: без параметров машины тихо пропускается); пара прогноз/факт образуется без ручного 📐
- Тесты: 245 passed (+17)

### Автопочинка STL (2026-06-13)
- pymeshfix 0.18.1 (deps: pyproject + requirements.heavy + Dockerfile.base)
- `stl_slicer._repair_mesh()`: при not is_watertight → MeshFix.repair() (API: `fixer.points`/`fixer.faces`, repair() БЕЗ verbose-аргумента), best-effort; SliceResult.was_repaired
- Проверено: дырявый куб (грань удалена, vol 750) → починен до 1000, целый не трогается
- **ВАЖНО**: MeshFix на больших грязных сетках (s_спираль с поддержками: 64МБ/1.35M граней) возвращает 0 граней и работает минутами → защита `_MAX_REPAIR_FACES=300_000`: выше порога починка пропускается с предупреждением «файл с поддержками, загрузите оригинал». Реальная спираль детали (81.9см³) уже watertight, починка не нужна
- Авто-прогноз только для file_type=stl (не stl_supports), так что поддержки в починку обычно не попадают
- Тесты: 247 passed (+2)

## Roadmap (agreed 2026-06-12)

Three core user scenarios, in priority order:

1. **Планирование:** загрузить STL → получить предсказание времени печати + себестоимость
2. **Приём логов:** загрузить логи → система сама разобрала и проанализировала
3. **История:** открыть архив → найти печать → увидеть всё по ней (STL, Magics, логи, результаты)

### Фаза 0 — Архив (2–3 нед.)
- Доменная модель `PrintRecord`: STL + Magics + логи + результаты в одной карточке
- MinIO бакеты по типу артефактов
- Переименование M-350 → M-450M по всему коду
- История печатей с поиском

### Фаза 1 — Предсказание (4–6 нед.)
- STL upload → программная нарезка на слои (trimesh) → площади сечений per-layer
- Расчёт времени: `время_слоя = площадь / скорость_штриховки + периметр / скорость_контуров`, с учётом 2 лазеров
- Калькулятор себестоимости: порошок + газ + фильтр + платформа + постобработка
- **Все параметры настраиваются через UI**, не захардкожены (ставки, скорости, кол-во лазеров и т.д.)

### Фаза 2 — Умный приём логов (2–3 нед., ~70% готово)
- Drag & drop логов → auto-detect → привязка к PrintRecord по дате

### Фаза 3 — Аналитика и тренды (4–5 нед.)
- Predicted vs actual (уточнение модели с каждой печатью)
- Качество порошка по партиям, деградация машины

## Данные с флешки оператора (2026-06-12)

Структура папок оператора:
```
Модели с печати/
  {дата}_{название}/
    деталь.stl, деталь.stp
    s_деталь.stl          ← STL с поддержками (s_ префикс)
    компоновка.magics
    Расчёт стоимости печати.xlsx
    [PDF отчёт геометрии]
Логи/
  {дата}.log, {дата}.zip, {дата}_burn.log, ...
```

### Алгоритм расчёта из Excel (структура листов)

Лист "Сводная ведомость" — статьи затрат (ставки вводятся вручную, **не хардкодить**):
- Порошок (руб/кг), Инертный газ (руб/атм), Фильтр (руб/шт), Обработка платформы (руб/шт)

Лист "Время работы" — формула времени:
- `время_слоя = S_avg / v_hatch + P_avg / v_contour`
- `время_печати = N_layers × время_слоя / N_lasers`
- Постобработка вводится вручную (распаковка, ТО, электроэрозия, мехобработка...)

Лист "Расчёт среднего сечения" — площадь сечения на каждом слое (из Magics).
Программно заменяем нарезкой STL через trimesh.

**ВАЖНО:** данные в конкретных Excel-файлах с флешки могут быть неактуальными или содержать ошибки — использовать только как образец структуры, не как источник констант. Все параметры (ставки, скорости сканирования, плотности, кол-во лазеров) должны быть настраиваемыми через UI и храниться в БД/конфиге.

### Прочие файлы на флешке
- `реалгуиде/RealGUIDE.exe` — dental/хирургическое ПО для планирования имплантов, не связано с расчётом стоимости SLM-печати; хранится на флешке по другим нуждам.
- `printer-log-analytics/` — копия текущего проекта

## Ключевые принципы разработки

**Why:** Production analytics + archive system for SLM metal printing. Correctness and configurability are critical.

**How to apply:**
- Данные из Excel/логов/флешки — только образец структуры, не источник истины для констант
- Все числовые параметры машины и расходников — через UI настройки, не в коде
- Re-import idempotency (deterministic group_id) — hard requirement
- Test with real logs when possible

## Коррекция расчёта времени (2026-06-15, не закоммичено)

- Режим "fast"/excel БОЛЬШЕ не использует Excel-приближение `4√A` — оно завышает прожиг на полых/тонкостенных деталях (×2.2 на спирали). Заменён на физическую формулу `A/(hd·v)+P/v_c`. `_excel_section_times` оставлена мёртвым кодом. **Память Фазы 1/«Два режима расчёта» о «формуле 4√A, воспроизведённой бит-в-бит» — УСТАРЕЛА: формула признана неверной.**
- recoat по умолчанию (recoat_time_ms не задан) = 9500 мс/слой; машина показывает 10.0 с.
- `/upload/stl-estimate` принимает override `hatch_distance_mm`; UI — поле «Шаг штриховки (мкм)» дефолт 120 при выборе STL.
- **ГЛАВНЫЙ ОТКРЫТЫЙ ВОПРОС:** формула (даже физическая) занижает прожиг ~×8 из-за скважности (перескоки/задержки), не из-за поддержек. Лучшее решение — векторный pyslm. Машинный «расчёт времени» в LaserStudio негоден (неточен). Детали и проверенные magics+log пары → [[m450-print-time-project]]; чтение .magics → [[magics-file-format]].
