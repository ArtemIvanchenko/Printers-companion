# MiniMax M3 — аудит аналитики и ML

Сессия: `ses_f9b04848dffepAZfrGJKf3M2D9`

## Важное ограничение доверия

Несмотря на прямой запрет, один из внутренних OpenCode-агентов прочитал локальный
`.env.secrets` и включил значение `TELEGRAM_BOT_TOKEN` в ответ. Значение здесь
намеренно удалено. Ночное внешнее ревью после этого остановлено; токен требуется
отозвать и выпустить заново. Git-проверка не обнаружила `.env.secrets` среди
отслеживаемых файлов или в истории этого репозитория.

Все технические выводы ниже являются внешними гипотезами и должны проходить
локальную проверку.

## Высокий приоритет по версии модели

1. `analytics/prediction/print_time.py`: одиночный STL-путь может не применять
   отдельную модель полного цикла `max(scan + recoat + base, floor)`, тогда как
   plate estimator применяет её.
2. Там же одиночный путь может не передавать `build_origin_z_mm`, из-за чего одна
   геометрия получает разные гипотезы слоёв в двух API-путях.
3. `migrations/versions/0004_machine_presets.py` использует `create_all()` внутри
   миграции; предложена явная Alembic-операция без зависимости от актуальной ORM.
4. `scripts/maintenance/import_logs.py` вызывает `create_all()` и может обойти
   Alembic при запуске против общей БД.
5. Проверить, что operator compose не запускает `alembic upgrade head` при каждом
   старте API, а использует только отдельный `migrate` service.
6. Dashboard может не показывать `source`, размер выборки, CV AUC/folds,
   `in_sample` и предупреждения defect-risk, хотя API их возвращает.
7. `PredictionResult.warnings` может оставаться пустым при heuristic fallback,
   малой выборке и слабой валидации.
8. Shadow evaluation фильтрует будущее по `session_id`; исправление final-метки
   после обучения требует временной отсечки и нового training fingerprint.

## Средний приоритет по версии модели

- Проверить независимые geometry groups в accuracy и recoat calibration.
- Включить код/версию feature builder и гиперпараметры в ML fingerprint.
- Не допускать обучения при одном экземпляре миноритарного класса.
- Добавить в provenance prediction/calibration версии кода, конфигурации и
  применённой модели.
- Включить origin, ориентацию, толщину, hatch, число лазеров и версию анализа в
  geometry fingerprint/cache contract.
- Ограничить изменяемые PATCH-поля operator events/journal/knowledge.
- Проверить ownership для удаления карточки и вложения.
- Показывать слой вместе с активной геометрией/Z при диагностике выбросов.

## Положительно отмеченные свойства

- per-layer cycle floor применяется после scan correction в plate estimator;
- паузы и конфликтующие повторные попытки исключаются из калибровки;
- wall-clock отделён от machine-time;
- scan/cycle CV группируется по повторяющейся геометрии;
- defect-risk использует временную CV и shadow promotion gate;
- job completion защищён lease owner + generation;
- карточки используют optimistic concurrency;
- pairing не использует совпадение фактического времени с прогнозом;
- quality final labels образуют append-only supersedes-цепочку;
- MinIO-загрузка и ссылки используют SHA-256 и идемпотентность.
