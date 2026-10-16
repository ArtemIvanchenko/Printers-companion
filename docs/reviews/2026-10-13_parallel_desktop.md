# Desktop: последовательные изолированные проверки 13 октября 2026

Область: `desktop/`, `core/updating/`, `checks/updating/`. Общие AGENTS.md,
implementation plan и night review прочитаны и не изменялись. Существующие
незакоммиченные исправления сохранены. Рабочий stack, Docker engine, native
install, NAS/БД и исходные данные не запускались и не менялись.

## Ошибка остановки не оставляет ложный ready

Обычный путь «Остановить и выйти»: штатный `PackagedUpdater.stop` успевает
остановить frozen worker/watcher, затем Docker-команда остановки API отказывает.
Журнал правильно остаётся `switching`, maintenance token сохраняется, а main
раньше сбрасывал только busy и оставлял `phase=ready`. Dashboard продолжал
показываться, хотя вычислители остановлены и новые записи закрыты gate.

В `desktop/main.cjs` catch остановки теперь публикует `phase=error`. Существующий
экран ошибки и «Повторить» используют прежний launch/recovery; никакого нового
updater, протокола или автоматического переключения нет. Даже при отказе до
изменения контейнеров готовность консервативно требует повторного launch.

Новый VM-сценарий на исходном коде: 1 fail (`ready != error`), после: pass.
Python-сценарий вызывает настоящий stop/launch с существующим FakeCommands:
подтверждены остановленные consumers, работающий API, сохранённый token/журнал,
затем прежний snapshot и работающие consumers после launch, gate снят.
Это воспроизведение общей последовательности команд, не настоящий Docker.

Проверки: 62 desktop Node tests; 78 tests в test_runtime.py/test_packaged.py.
Pytest только APP_ENV=test, mktemp `/private/tmp/pc-desktop-stop.*`, отдельные
SQLite/outbox/operator identity. Scoped Ruff и diff-check проходят.
Подписанная установка macOS/Windows и реальные Docker-команды не проверены.

В той же цепочке воспроизведён второй сбой: после отложенного из-за расчёта
обновления уже стоял 30-секундный retry. Принятое «Остановить и выйти» при ошибке
stop оставляло таймер; он автоматически выполнял launch/status/resume вопреки
решению остановить приложение. Существующий clearTimeout перенесён перед stop,
после fresh busy guard и проверки «Отмена». Теперь успешный/неуспешный stop
отменяют этот retry, а отмена диалога и чужая активная операция его не отменяют.
Новый VM-тест до исправления наблюдал лишние launch/status/resume, после —
только stop. Весь desktop после обеих правок: 63 passed, node syntax/diff clean.

## Неполный список сервисов не подтверждает готовность

При первом подключении существующего проекта `_old_snapshot` сохраняет список
фактически Running-сервисов. Если объявленный estimator отсутствует или сразу
выходит после `docker start`, он выпадал из списка. `_verify` проверял только
этот список и исправный API; обычный launch сохранял `current` без обработчика
расчёта. Впоследствии такой сокращённый журнал тоже проходил проверку.

Общий `_verify` теперь проверяет, что snapshot включает все REQUIRED-сервисы,
которые объявлены и включены в собственной сохранённой Compose-конфигурации.
Исторический состав не достраивается из сегодняшней конфигурации: отсутствующие
в legacy Compose и явно выключенные replicas=0 остаются прежними. Не добавлено
автоматическое создание контейнеров из mutable aliases, новый health protocol
или heartbeat. Проверка подтверждает состав и прежние Running/image/API gates,
не выполнение реальной задачи вычислителем.

До исправления оба сценария первого launch (`missing`, `exited_after_start`)
давали ложный успех. После исправления: отказ без current и без pause/kill/up;
после восстановления исходного контейнера обычный повторный launch успешен.
Отдельно проверены ранее сохранённый неполный список и два legacy/disabled
контроля. Сокращённый исторический журнал не переписывается автоматически;
при его обнаружении требуется проверка установки.

83 checks runtime/packaged passed; Ruff и diff-check чистые. Все Python-вызовы
в APP_ENV=test с отдельными временными SQLite/outbox/operator identity под
`/private/tmp/pc-desktop-register*`. Только штатный FakeCommands, без Docker
engine, операторских workers, NAS, native install и внешних запросов.

## Кириллические пути не декодируются через локальную ANSI code page

`Commands.run` использовал `subprocess.run(text=True)` без encoding. В обычной
Windows-конфигурации без UTF-8 mode это locale encoding. UTF-8 JSON от Docker
успешно разбирался, но `C:/Печати/оператор` превращался в mojibake. Затем сравнение
working_dir/mount с настоящим путём могло ошибочно отклонить свою установку.
UTF-8 настройка stdout встроенного CLI в desktop_entry не меняет декодирование
труб его дочерних процессов.

В existing subprocess.run явно задан `encoding='utf-8'`, как уже в изолированной
Compose dotenv-проверке. Без изменения кодировки ОС, fallback/replace для путей,
новой зависимости или вывода сырого stderr в окно.

Два теста используют реальные короткие Python child processes, отдающие UTF-8
JSON, и подменяют только `locale.getencoding` на cp1251/cp1252. До правки оба
теряли точные Source/Name, после — сохраняют. Совместно 85 runtime/packaged/command
checks passed, scoped Ruff и diff-check чистые. APP_ENV=test, отдельные временные
SQLite/outbox/operator identity `/private/tmp/pc-desktop-encoding*`.
Это проверка реальных subprocess pipes на Mac с контролируемой code page,
не выполненный запуск Docker или frozen host на Windows.

## Read-only: отложенный повтор теряется при проверке пакета из меню

Новая source/test правка в этой итерации **не выполнялась**. Воспроизведён
обычный доступный workflow:

1. Launch получает busy из-за текущего расчёта; resume подтверждает прежний
   backend, `backendUpdatePending=true`, main ставит повтор через 30 секунд.
2. Оператор выбирает «Проверить обновления» в native menu. Это существующий
   доступный путь даже при backendUpdatePending; check/download может ждать сеть.
3. На 30-й секунде таймер видит updates.busy/state.busy и просто возвращает.
   Это одноразовый setTimeout: следующего таймера больше нет.
4. Check завершается; состояние снова ready/busy=false, backend всё ещё старый.
   Даже через 120 секунд launch не вызван, отложенное обновление само не повторится.
   «Завершить обновление» вручную по-прежнему работает.

Причина — callback deferredUpdate в `desktop/main.cjs`: условный launch без
перепланирования занятого окна. Это не подтверждение ошибки durable gate или
повреждения journal: до переключения на таймере host-команды вообще не вызваны.
Следует сохранить уже ожидающий повтор при временной занятости окна; принятые
stop/install/ручной launch должны по-прежнему отменять прежний таймер. Новый
сервис, второй updater или принудительный launch поверх busy не нужны.

Read-only probe использует настоящий main.cjs и существующий VM fixture из
main-updates.test.cjs; сторонние процессы/сеть/Docker не запускаются. Временные
таймеры контролируются в probe, production timers/source не переписываются.
Три последовательности: idle → launch на 30s; отмена close dialog → launch на
30s; check-in-flight → ноль launch и ноль timers на 120s, pending=true. Последний
контроль вручную вызывает launch и получает pending=false.

Воспроизведение: `node /private/tmp/pc-desktop-readonly-probe.cjs`.
Полные состояния/вызовы: `/private/tmp/pc-desktop-readonly-probe.json`.
Никаких новых постоянных tests/source guards не добавлено. Это VM-доказательство
потери планирования в main, не native macOS/Windows или реальный network proof.

## Продолжение: ожидающий повтор сохраняется до принятого нового действия

После согласования read-only причины исправлен только existing callback:
именованный callback того же setTimeout при временной busy перепланирует себя
через 30 секунд. В каждый момент остаётся один ожидающий timer; launch поверх
runtime/update busy запрещён, allowQuit завершает ожидание. Принятый recover
теперь вызывает существующий clearTimeout после busy guard: явный rollback
не отменяется старым auto-launch, в том числе когда rollback сам отказал.
Принятые manual launch/install/stop сохраняют своё прежнее снятие таймера.
Rejected recover и отмена close dialog чужое ожидание не отменяют.

8 новых VM-сценариев до правки: 4 fail / 4 pass. Check success/error после
трёх 30-секундных busy интервалов раньше теряли retry; rollback success/error
раньше получали лишний launch. После правки все 8 проходят. Idle и cancel-quit
дают один launch; check-in-flight не вызывает host до освобождения, после него
ровно один launch, дальнейший tick не создаёт дублей. Manual launch и failed
package install/cancel-restart отменяют прежний retry. Existing stop/close/install
ownership guards сохранены и входят в общий прогон.

Весь desktop: 71 passed. Node syntax и scoped diff-check чистые. Production
изменения только desktop/main.cjs; тесты — existing main-updates.test.cjs.
Host journal, maintenance, lease/protocol, версии и сборка не менялись.
Без Docker, frozen app, NAS, настоящей сети или native installer. Старый
read-only probe сохраняет baseline-assertions и после исправления уже не обязан
завершаться успешно; действующие before/after assertions находятся в тестах.
