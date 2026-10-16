# Ночная стабилизация — 13 октября 2026

## Исходная точка и границы

После очистки диска прежняя папка проекта отсутствовала. С разрешения владельца
восстановлен публичный main в `/Users/admin/Desktop/Printer's companion`:
`587b16b53331ce1dec5bb858704019a39e8432de`, опубликованный 5 октября, VERSION1.8.0.
Июньская копия и осиротевший сентябрьский worktree не изменялись.
Неопубликованные изменения 7–8 октября не восстановлены целиком; нельзя выдавать эту
установку за прежнюю локальную1.8.1. Корпус моделей/логов остаётся в Печати.zip.

Задача владельца: только подтверждённые ошибки, дебаггинг и рефакторинг,
без новых функций, зависимостей проекта, сервисов и обходных путей. Прежние
предоставленные AGENTS.md инструкции отменены владельцем. Сохранение возможностей,
владения вычислениями, fencing, revision и provenance остаётся целью корректности.
Рабочие БД/NAS/сеть принтера/секреты и исходные файлы не изменяются. Публикации нет.

## Первая завершённая итерация

### Сохранение настроек и автокалибровка

Обычный dashboard PUT отправляет все поля, включая прежние/null поправки.
Роутер включал correction_locked по наличию ключа, даже если оператор не менял
калибровку. Теперь валидированные значения сравниваются со свежей строкой
MachineParams под существующей estimation writer guard и SELECT FOR UPDATE.
Изменение вручную по-прежнему фиксирует поправку; явный lock/unlock имеет приоритет.

Исходный роутер из HEAD, загруженный отдельно в память: семь регрессий падают.
После исправления: 17 focused settings и 10 существующих API settings проходят.
Проверены неизменные/null/нормализованные значения и устаревшая ORM-строка после
другого писателя. Живой PostgreSQL и реальный межпроцессный NAS race не проверены.

### Persistence и NAS downtime в импорте

Начальные fence.verify перед source/event writes находились вне обработки
persistence-ошибок. Перенесены в существующий try: SQL outage получает
retryable ImportPersistenceError, StaleImportLeaseError сохраняет свою категорию;
транзакция откатывается. Защита публикации и lease не ослаблены.

RawArchiveUnavailableError больше не увеличивает stability_check_attempts.
Backoff использует существующий monotonic lease_generation с положительным
минимумом и ограниченным максимумом; audit details.attempt сохранён.
Исходник провалил пять новых случаев; исправленный прошёл их, включая
четыре NAS outage, изменение файла и успешный импорт после восстановления.
74 adjacent import/publication/API/affinity проходят; после сохранения audit-ключа
ещё 4 raw archive проходят. Наборы пересекаются, их нельзя складывать как уникальные.
SQL outage внедрён в authoritative fence при настоящих SQLite транзакциях,
не проверен на настоящем PostgreSQL. Старые раздутые счётчики не переписывались.

### Desktop install lifecycle

Исторического await-зависания в восстановленном install() нет: он синхронный.
Реальная ошибка: quitAndInstall сообщает отказ событием error, не throw;
main считала install успешным и оставляла prepare-restart gate/allowQuit.
Теперь установка ждёт native before-quit-for-update либо error, с пределом
пять минут, очищает temporary listeners/timer/busy. Close разрешается только
по подтверждению. Main await/cancel-restart сохраняет штатную защиту выхода;
ошибка отмены не затирается исходной ошибкой install.

Emitter перепроверен по полным cached исходникам electron-updater6.8.9:
NSIS/BaseUpdater и MacUpdater подтверждают один native electron.autoUpdater
before-quit-for-update. VM выполнила реальные cached классы, заглушив только
native/process/network/installer действия. Платформенные main-path тесты
Windows/macOS, error/throw/no-event/late-event/cancel failure: desktop33/33.
Настоящий подписанный installer/Electron на обоих ОС не запускался.

### Окно прожига и неизвестная длительность

`"burn" in event_type` включало wait_for_burn; любая ссылка на слой также
растягивала burn-окно до подготовки и после него. Один общий predicate теперь
определяет и окно, и счётчик actual burn activity. Окно всей сессии сохранено
отдельно; исходные события не меняются. Неизвестная длительность остаётся null,
не нулём, даже при известных суммах отдельных фаз.

Проверка настоящих main-логов из Печати.zip production EventLogParser:

| Лог | Начало прежнего окна | Первое burn-activity | SHA256 |
|---|---|---|---|
|26.05.2026.log|10:12:45|13:11:09|b90d3d00a30ec7634a20a6584873631a3ee821199595158b2c82546431fd47d7|
|19.05.2026.log|11:43:19|13:54:14|a1bfd3fa1305dbee6c445750c4a76565ca177614206fb0e1f6a056b55a9c8275|
|23.03.2026.log|16:30:54|17:17:32|54dac4663c830a575cdada81eefb571df9e42266ab126bf67795c8915780fdac|

Только три выбранных файла <2MB извлечены во временный каталог, без массовой
распаковки 16GB архива. Это проверка интерпретации имеющихся маркеров, не доказание
полноты выгрузки, фактической годности или точности времени печати.
Overview/snapshot/analysis-filter34 проходят. ANALYSIS_VERSION0.5.6 не использует
утраченные локальные номера0.5.3–0.5.5; старые JSON/модели не переименовываются.

## Окружение и проверки

Восстановлена отдельная `.venv`, Python3.12.13. Установлены существующие base/dev
и необходимые численные/geometry зависимости по нативным диапазонам pyproject;
декларация зависимостей не изменена. В первой итерации PySLM/pymeshfix не были
установлены; ниже отдельно записана их последующая установка и native проверка.
Нейросетевые веса, lightgbm и необязательный showcase не загружались.
Это не полный cross-platform lock.

Все pytest: APP_ENV=test, отдельные temporary SQLite/outbox/operator identity.
Root: `/private/tmp/companion-night-root.XcxKb6`; импорт:
`/private/tmp/printer-import-review.ssy26a`. Никаких операторских DB или startup
worker процессов не запускалось. Scoped Ruff, Node syntax и diff-check чистые.

## Вторая завершённая итерация

### Раздельные бюджеты импорта и совместимость схемы

Generic worker failures используют execution_attempts, file stability — прежний
stability_check_attempts; infrastructure downtime не расходует ни один бюджет.
Automatic claim/reclaim и deferral сохраняют execution; successful publication
обнуляет его в той же транзакции. Явный operator retry начинает новый бюджет.
Настройки предела/задержки остаются существующими. Непервичные recovery receipts
с историей попыток отвергаются; counters уже зарегистрированного job сохраняются.

Из истории собственной работы найден точный Add File для утраченной
0022_import_execution_attempts.py и восстановлен byte-equivalent: это прежний
revision0022 после0021, не новая несовместимая migration с тем же номером.
Upgrade/downgrade проверялись только на disposable SQLite. Старые смешанные
stability counters не пересчитывались. До исправления NAS/file change/generic
failure при лимите2 давали failed; после — postponed, stability1/execution1.
8 новых случаев;101 adjacent проверки и финальные7 focused прошли.

### Паузы не подменяются всем остатком печати

pause→resume — точный интервал. restart_attempt/finish/burn дают максимум
верхнюю границу, не угаданное возобновление. Unknown full pause total остаётся
null; confirmed_explicit_pause_seconds сохраняет точный подытог. Неразрешённая
пауза больше не поглощает последующие явно наблюдавшиеся burn windows.
Read API переносит pause accounting только из текущего analysis snapshot,
согласованного с текущей timing publication; legacy/mixed summaries не становятся
нулевыми паузами. Старые JSON не изменены.

20 regression failures на исходнике;58 focused проходят. Реальные пары main/time:
26May burn windows422→422,19May38→2424,23Mar25→6841. В последнем наборе2,
в майском3 незавершённых интервала; общая пауза неизвестна. Прежние162921s
для19May были выдуманным интервалом до finish, а не измеренной паузой.
Исходные архивы не менялись. Это не доказательство полноты всех логов.

### Полный цикл, сумма фаз и себестоимость

plate/calculation и read comparison больше не выдают burn+pour за полный цикл.
При отсутствующей модели controller machine_cycle_hours/total_days неизвестны,
print_hours сохраняет сумму фаз. Один существующий timing-validation helper
проверяет captured v2 evidence; field presence/numeric legacy total недостаточны.
Почасовые статьи стоимости не рассчитываются по неполному циклу; прочие статьи
сохранены, исключение поясняется warning. Старые записанные costs не переписаны.
13 baseline regressions;126 focused проходят. Нет доказанной точности по
реальным независимым компоновкам. PySLM-зависимый соседний test не запускался.

Одиночный preview теперь использует те же existing per-layer physics/fitted
functions и floor-before-sum, учитывает support speed и explicit build origin.
Два cycle admission/resolver перенесены в layer_engine без изменения AST.
Независимый пример: phase38.236s, cycle45.074s; поднятая деталь от explicit
datum даёт12 machine layers вместо2 body-height layers.11 baseline failures
и отдельная origin regression;47 focused проходят. Проверены реальные STL
bytes/mesh loading и synthetic layer math, но не построение PySLM траекторий.

Frontend карточки берёт cycle admission из server summary, не из старого
machine_cycle_hours. Preview не пишет «null сут» или «время печати» для суммы фаз.
HTTP projection сохраняет null days и фактическое N калькулятора; B+P calibration
interval не приклеивается к полному циклу. Неизвестная часть не заполнена догадкой.

### Общий календарь и структура телеметрии

Invalid explicit dates не заменяются датой имени файла; invalid filename date
остаётся unknown. Explicit timestamps не двигаются при clock-only rollover;
tracker хранит forward calendar anchor. Микросекунды сохранены. Digit boundaries
не позволяют вырезать часы из numeric ID. Unknown raw text сохраняется.
Time/Date StateFlow соединяются независимо от порядка; отрицательная duration
unknown, repeated headers исключены, Timeout/Update сохранены. Empty error table
header больше не становится записью ошибки.35 исходных failures;36 focused
проходят.17 небольших настоящих error logs больше не дают fake errors.
Огромные StateFlow файлы в этой проверке не читались.

Full sensor reader и sampled parser используют общий split/header/width logic.
Коллизия SO1/SO1/SO1_2 не перезаписывает колонки. Read diagnostics содержат
оригинальные имена и индексы; malformed/unparsed/invalid/overlap не дают complete.
21 исходная regression failure;77 focused проходят. Настоящий08.06.2026_sensors.log
10,672,444B, SHA25607cbda022100aaa193486b41d2d9c832833353291968d7a08f21544d94ea63a2:
43,731 raw rows,22 columns; analysis43,597 timestamped rows,134 overlaps,
complete=False.7 chart points отделены от полного анализа. Старые timestamps
не переписаны. Parser versions bumped только у изменившихся форматов.

### Отсутствие данных не означает исправный датчик или чистый процесс

Constant signals и sparse event intervals остаются diagnostics без штрафа и
без выдуманного «датчик завис/потеря измерений». Sparse constant channel при этом
не скрывает настоящую нехватку пригодных отсчётов. Empty observations дают
score=null/grade=unknown, а не100; continuity checks отражают фактический input.
Operator report не объявляет чистую печать при явно неполной/нулевой телеметрии
и не подменяет explicit null score старым feature. Inspection accepted/rejected
сохраняет приоритет как отдельный факт. Проверено before/after из HEAD и focused
quality/overview/operator сценариями.100 означает отсутствие penalized findings,
не полноту выгрузки и не вероятность годности.

### Робастные тренды с реальными координатами

Один существующий CI helper готовит finite pairs, point helper использует его.
Unknown/degenerate slope=null, measured flat=0;2 observations не дают fake exact CI.
Фильтрация не сжимает raw-row/layer/session X. Maintenance/deviation не строят
прогноз по отсутствующему последнему значению.31 из38 исходных cases падали;
43 новые и106 focused проходят.2 прежних shift cases требуют отсутствующий
ruptures, не учитываются как успешные. Независимо: pairs(0,1),(1,3),(3,7)
даютslope2; layers1/3/8 иtime10/30/80 дают10s/layer вместо35/сжатый отсчёт;
maintenance1/null/3/4/null/6 — slope1,4prints до10. Saved models/snapshots не тронуты.

### Release metadata, без выпуска

pyproject.version1.7.0 расходилась с VERSION1.8.0; исправлена на1.8.0.
release.sh проверяет/обновляет четыре существующих version files согласованно,
сохраняет dry-run/no-commit same-version gates.7 исходных failures;24 проверки
с fake commands проходят; реальные release/Git-tag/build не запускались.
Desktop builder уже брал installer version из VERSION: старая package metadata
ломала CI invariant, но не доказано, что она задавала неверный installer version.

Root integrated parser/log-insights/quality run:147 passed; затем все37 Node web
checks прошли. Наборы перекрываются с agent runs, их суммы не являются числом
уникальных проверок. ANALYSIS_VERSION0.5.6, LOG_INSIGHTS_VERSION1.0.8;
phase/soft method versions bumped. Все проверки — isolated temporary state.

## Третья завершённая итерация

### SQL outage — не синоним любой SQL ошибки

Один classifier в существующем storage/db/session.py используется import
publication, worker retry, attachments, log registration/recovery. SQLSTATE08,
shutdown57P01–03, connection_invalidated, реальная psycopg connection failure
и исчерпание соединений53300 сохраняют offline retry. Constraint/programming/
syntax/auth и произвольный OperationalError не получают бесконечный NAS retry.
Остальные class53 ошибки не считаются автоматически временной недоступностью.

Настоящие SQLite NOT NULL/missing SQL function прошли оба upload route:
HTTP500 и needs_attention вместо ложного deferred; payload/SHA/владелец/
confirmation/counters сохранены. Подтверждённый outage —503 и recovery того же job.
Lost commit ACK attachment подтверждается по уже опубликованной записи без
повторной передачи/revision. Новые policy tests,129 import-adjacent,286 attachment-
adjacent и121 log-upload/recovery прошли; наборы перекрываются. PostgreSQL сервер
не поднимался, его native error types/SQLSTATE проверены локально.

### Полнота текущего импортируемого batch

Ожидаемый архивированный manifest сверяется с parsed и структурированными
intentional policy exclusions текущей попытки до staging writes. Исчезнувший/
нечитаемый файл не даёт successful partial import; parser_exception/parse_failed
получают ограниченный failure path. Unsupported/skipped_large_file/profile
exclusions и диагностированные malformed partial logs не запрещены.
Первоначальный upload manifest не заморожен навечно: файлы могут штатно дозаписаться.

5 новых regressions падали на исходнике;10 focused и106 adjacent прошли.
Обнаружен старый private-ZIP test, возвращавший фиктивный empty parse при двух
архивных файлах. Исправлена только fixture: реальный parse/checksum и изолированная
grouping policy, без production исключения для конкретной папки.
Затем161 disjoint private/completeness/archive/adjacent проверки прошли.

### Process health: математически неизвестное остаётся неизвестным

Analog boolean/nonfinite исключаются; знак низкого spike не теряется.
Нулевая средняя не создаёт fake100 для variation; измеренный constant-zero канал
может иметь constant score100. Неверная длина/неизвестный timestamp axis не
сопоставляется с высотой, naive/aware clocks приводятся только при известном времени.
Burn slope требует3 distinct physical layers, degenerate/overflow остаются unknown.
Повторные попытки не дедуплицируются догадкой. Исходные weights/formulas сохранены.

14 исходных regressions;22 новые и60 adjacent проверки прошли. На полном доступном
08June sensor stream43,597 rows:6 spikes и1 threshold finding, readiness74.6.
Это оценка пригодности наблюдений, не вероятность годности/доказательство дефекта;
без burn scope нельзя честно назначить высоты всем sensor findings.

### Одна карточка — один согласованный async bundle

Fetch record/session/report/risk готовит локальный bundle и публикует только для
последней видимой карточки. Navigation generation, loading и edit generation
не позволяют ответу карточки A перезаписать B. Sync после await проверяет ID,
revision и dirty edits, не возвращает старую revision. Save фиксирует исходные
ID/revision; новые правки во время save остаются dirty. Текущая вкладка сохраняется.
12 regressions падали на HEAD;14 focused и51 Node web проверки прошли.
Это controlled promises/EventSource/DOM VM, не живая Safari проверка.

Удалён скрытый duplicate preview host, а существующий editor material/thickness/
powder/notes/save перестал быть display:none. Возможность редактирования сохранена,
новой формы/слоя нет. Unknown explicit pause больше не показывается нулём.

### Воспроизводимый native geometry path

Установлены только уже объявленные PythonSLM0.6.1,pymeshfix0.18.1,ruptures1.1.10,
rtree1.4.1 с необходимыми transitive wheels:19 файлов,37,465,933B. pip check и
imports прошли. VTK/showcase/нейросетевые веса не добавлены. Native cube/sheet/
raised-part checks действительно выполняют trimesh/PySLM, не synthetic substitute.

Single-model estimate теперь использует явный transient SliceResult.prepared_mesh,
то есть ту же MeshFix-подготовленную геометрию, а не повторно исходный дырявый STL.
Legacy unprepared input остаётся допустим; repaired=True без подготовленной сетки
не притворяется корректной геометрией. На10-face box scan0.3→13.776447s,
recoat180s неизменна. Это исправление ошибок геометрии, не коэффициент под лог.

Open tracks выделяются native graph topology, включая attached/backtracking
branches. Буферизация больше не создаёт phantom open length; holes/arcs сохранены,
невалидный closed contour даёт explicit failure.13 исходных regressions;
96 native targeted прошли. Root integrated native run:167 passed/1 failed;
failure — старый all-items cost test без controller evidence, а не потеря статьи.
Его fixture теперь явно даёт идентифицированную тестовую v2 model и проверяет
формулу стоимости фильтра; неполный цикл по-прежнему не авторизует hourly costs.

Реальная архивная деталь Prishchepka pod vint 1 pod metall - 4 sht.STL,
23,884B/476faces/32layers, SHA256
6260051660ad8df91351ba44334755ef19f253aa50b6f8d9153c737c0bcca861:
scan15.800990727s,recoat288s; phantom open0.0001382812→0mm, ложное warning убрано.
Single/plate совпадают; datum/minZ as-run не подтверждён, full cycle/days=null.
Smoke parameters не выдаются за настоящую recipe, точность времени не доказана.
12 sections,5 повторов:11.52→23.70ms median (+~1ms/section); whole slice+single+
plate0.826s. Это измеренная цена корректности, не ускорение. Большой case не проверен.
Scan scope bumped configured-scan-v3, geometry cache6: старые fitted scope/cache
не подменяют новый algorithm; история и independent log-only cycle model сохранены.

### Исторический reference и допуск plan-vs-fact

Historical reference читает только scalar duration JSON, streaming yield_per500,
а не тяжёлый context каждого session. Invalid/bool/nonfinite/missing исключаются,
legacy numeric strings и порог10min сохраняются. SQL вынесен из async event loop.
Это длительность log window с возможными паузами, не модельный прогноз новой детали.
Frontend больше не заполняет отсутствующий geometry forecast историческим средним.

20 focused reference cases прошли. Local SQLite contexts1,378,055B:
peak8,652,730→386,193B;0.108→0.0221s. Это не измерение NAS/PostgreSQL.
Preview interval B+P относится к phase point, не к полному machine cycle.
Read comparison использует существующий calibration_input_exclusion: unconfirmed
model/log, stale revision, unconfirmed datum/geometry не авторизуют actual/error.
Independent normal fact требует current analysis provenance; прежняя цифра хранится
как история, не валидирует сама себя.59 comparison/timing targets прошли.

### Conservation на настоящем небольшом corpus

Из Печати.zip read-only прочитаны26 unique dated main/time files (13+13),
5,332,632B. Production parser дал162,292 events и16,639 time summary events.
Во всех main каждый nonblank line сохранён как event; число OLD_STATS строк во
всех time равно числу summaries.13 filename-date aggregate sums B+P+residual
conserve recorded attempt cycle до1e-6. Unknown raw lines не удалялись.

Диагностика:26May/9June out-of-order,19May repeated layers. В4/8June,19May,
23Mar open pause intervals оставляют exact total=null, confirmed subtotal0.
Filename-date grouping не доказано как physical print pairing/expected layer count.
Artifact: /private/tmp/companion-night-corpus.wdQvaC/audit.json. Архив не изменён.
Большие StateFlow/full sensor corpus не прочитаны; физическая точность не доказана.

## Четвёртая завершённая итерация

### Fencing файловой очереди и физическая идентичность объекта

Каждый outbox claim получает UUID token. release/fail/complete требуют именно
текущий token: stale claimant не меняет, не карантинирует и не удаляет чужой payload.
verify проверяет token до/после hash, SQL publication/commit держит краткую existing
локальную lock guard. Transfer остаётся вне SQL/lock. Receipt после commit может
подтвердить следующая попытка по checksum, без ложного acknowledgement старого owner.
18 новых claim scenarios и250 adjacent прошли; не настоящий межмашинный NAS race.
TTL — crash recovery, не hard upload deadline: recovery может вызвать лишнюю
передачу при очень долгом transfer; публикация устаревшим token запрещена.

Отдельно доказан общий exact-URI delete/reupload race: после reference check новая
ссылка могла коммититься на старый deterministic object key, затем cleanup удалял
её bytes. Случай lost commit ACK/pending retry также воспроизведён; UUID только
при enqueue его не решает. Теперь физический object key включает fresh claim token.
Operation/file IDs/checksum/payload остаются прежними, старые SQL URI/JSON читаются
без переписывания. Два race cases до2failed, после2passed;254 combined проходят.
Operator delete/revision unchanged; implicit orphans сохраняются. Цена корректности:
успешный transfer перед failed publication может оставить новый remote orphan.
Новая cleanup queue/таблица/generation не добавлена.

Root дополнительно устранил O(history) work каждого queue lock: recovery читает
receipts только для существующих payload dirs, не все выполненные когда-либо файлы.
Сохранена проверка valid matching completed receipt перед удалением leftovers.
Reopen completed receipt fsync'ит unlink до приёма нового payload: crash не должен
воскресить старый receipt и удалить новую очередь как якобы completed.
2 исходных regression failures;71 queue/worker/card tests прошли. Native Windows
directory fsync по-прежнему недоступен, прежний platform guard сохранён.
Own POSIX benchmark5000 completed receipts,7 alternating runs:
idle queue median0.282038→0.000267s;20 live payload dirs0.237936→0.000530s.
История5000 receipts и все20 pending dirs сохранены. Это local filesystem, не NAS.

### Высота отклонения не зависит от числа точек на графике

Process-health хранит source sample_index/count, а geometry_context ошибочно делил
его на bounded chart time length. Настоящие production helpers на101 source rows/
5 display points отправляли отклонение строки50 в конец детали вместо середины.
Теперь progress использует исходную frame/count; несовместимый/out-of-range/bool
index не зажимается до первого/последнего слоя. Timestamp без burn bracket не
превращается в progress guess; unmapped endpoint не превращает whole threshold
range в точку peak. Legacy aligned arrays без timestamps по-прежнему поддержаны.
12 исходных failures;133 geometry/card/API tests прошли. Limits2/5/17/101 дают
одинаковый приблизительный layer51/height5.05mm. Приблизительная привязка остаётся
приблизительной, не доказательством координат/физического дефекта.

### Измеренное упрощение geometry и process-health

Native graph с degree2 у каждой вершины состоит из циклов и не имеет bridges:
ненужные повторный traversal/пустые masks пропускаются. Tiny/open/attached/
backtracking ветви и failures используют прежний exact algorithm.
WKB, open tracks и все LayerGeometrySeries поля побитно совпали;84 targets прошли.
3 alternating whole PySLM runs: actual476-face STL/90samples0.34168→0.28169s;
synthetic4096-face annulus/91samples4.59013→3.42886s. На кольце с открытой ветвью
измеримого gain нет. Семантика unchanged, дополнительные version bumps не нужны.

Process-health готовит naive/aware burn frame и cadence один раз, не для каждого
отклонения; диапазоны за existing20 output bound не локализуются зря. Synthetic
300 sensor rows/3000 burn points/150 alarm ranges: median0.354629→0.020355s,
301→1 cadence calculations. Full output JSON SHA одинаков:
7a8d00da751cb6c3c954d07be4d172e9d45579650425a0ac4999099164788475.
147 adjacent tests и независимые nearest tie/gap bracket расчёты прошли.
Настоящий08June full stream без burn time points даёт0 height mappings, а не
придуманные слои. Это CPU benefit, не доказанная точность/активный physical scope.

### Нет измерений — нет «низкого риска»

Defect-risk feature row сохраняет unknown/null и отбрасывает bool/nonfinite/
overflow, а не подставляет0 для неизвестных drift/anomalies/duration/layers.
Известный sample_count0 не авторизует sensor features. Quality score сам по себе
не доказывает состояние физического процесса: risk=null/grade=unknown.
Unknown factors не разбавляют known risk: readiness20 без прочих измерений даёт
эвристический индекс0.8, а не0.3294. Это индекс, не вероятность брака.
11 initial failures;68 risk/contracts/registry/snapshot tests прошли.

Shadow evaluation требует сопоставимые finite scores обоих методов: unknown
heuristic baseline не превращается в0 и не падает на float(None); fallback
эвристика действующей модели не называется оценкой champion.2 baseline failures,
54 risk/shadow/snapshot checks прошли. Promotion gates не ослаблены.
Actual isolated API single/all empty observations возвращают200 с null risk;
5 data-route checks прошли. Frontend читает реальный API key risk вместо отсутствующих
risk_score/probability, unknown не зелёный0%; known0 сохранён. Method/in-sample
подписи отделяют индекс/оценку известного исхода от независимого прогноза.
Все57 Node web tests проходят; native browser пока не проверен.

### Desktop IPC не освобождает чужую текущую операцию

Generic rejected IPC сбрасывал busy даже когда первая install ещё ждала native
событие. Следующий launch снимал restart gate до выхода, разрешая новую работу.
Catch чужого запроса теперь сохраняет busy/phase; configure/import/recover сами
освобождают собственные ошибки. Launch/install/close учитывают updates.busy.
Нового state machine нет; updates.cjs не изменён в этой итерации.
6 failing baseline cases;42 desktop и49 host updater/gate checks прошли.
VM/fake commands/temp state, не настоящий Docker/Electron installer.
Отсутствие persistent backend child и намеренно оставленные Docker services —
не orphan bugs; readiness честно говорит no_worker_heartbeat.

## Пятая завершённая итерация

### BackgroundJob: свежая lease, сохранённые правки

Повторно используемый SQLAlchemy Session мог получить прежний BackgroundJob из
identity map после чужого committed reclaim. Даже SELECT FOR UPDATE не обновлял
owner/generation; old complete/fail/renew/defer были приняты против нового claim.
Все5 locked mutation reads теперь selectively refresh'ят authoritative lease/state.
Без нового Session/blanket flush, pending payload/result и правки карточки не
сбрасываются. populate_existing/load_only был отвергнут: реальный autoflush=False
probe показал потерю dirty-tracking до commit. Дополнительный короткий SELECT —
осознанная цена correctness. Main publish_estimate уже имел fresh revision/fence,
он не ослаблен.10 regressions+3controls до;13 после;98 adjacent прошли.

Estimate/model/calibration/shadow executors используют общий SQL classifier.
Настоящие SQLite missing-column/function и psycopg auth идут в existing bounded
attempts;08006/53300/invalidated — infrastructure deferral без расхода бюджета.
Новой retry hierarchy/counter/runner нет. Для трёх других executors39-case matrix
до18failed/21passed;196 combined после. Rollback/publication/stale claim/owner-
local controls сохранены. Native PostgreSQL concurrency не проверен.

### StateFlow: повторная работа не превращена в угаданную схему

Exact repeated raw state cells не преобразуются/не собираются заново после
обязательных per-row time/diagnostic checks. NaN reflexivity, anchors, changed
numeric spelling, unknown cells, MAX rows/transitions/size policies сохранены.
На настоящем1MiB prefix0.094063→0.043748s,100280→780 coerce calls;
полный JSON SHA одинаков79561b4933a1dccaadb47c6e708c485844ef2fb7ae79e932dafa25bdb33eb694.
88 parser/profile/completeness tests прошли. Память8.406→8.407MB: улучшения нет.
Version0.2.1 пока unchanged, поскольку это только equivalent CPU path.
Будущая bounded diagnostics iteration описана ниже как не завершённая.

Root независимо прочитал целиком smallest08June StateFlow member без extraction,
через bounded ZIP stream,99,372,623B, CRC проверен EOF. SHA256
3745c204a8d0a95d403767702e747a4aafeffa844f5080a4b33a0c22dcb19603.
475467lines=4identicalheaders+475463data; все data19cells vs header20.
Header имеет лишнюю unnamed пустую tail cell; named/interior cells этим аудитом
не потеряны.10160 raw spelling state changes — не typed semantic transitions.
Clocks20:26:37→08:39:45 не доказывают дату, physical session или enum meaning.
13.39s/65,726B traced stream peak после чтения central directory.
Архив inode/size/mtime unchanged. Artifact:
/private/tmp/companion-night-stateflow-stream.Xa9Gti/audit.json.
10MB import gate не обходился:32 nonempty genuine full StateFlow members по-
прежнему skipped. Нельзя выдать stream audit за full production parser import.

### Не перепутать график, источник и неизвестный отчёт

Один selection counter для session/all-time guards success/error; поздний ответ
или ошибка A не меняет выбор B, в том числе повторный запрос одного ID. Новый
выбор очищает прежние chart/health/alarm/context до await: failureB не оставляет
график A с заголовком B. Late list response/error не перехватывает явный выбор.
Существующие графики/null/real zero/label-array alignment сохраняются, cache нет.
Missing state-bearing operator report больше не даёт зелёное «отклонений нет/
действия не нужны». Пополненные healthy/warning reports и QC form unchanged.
11 initial failures;72 Node web tests прошли. Не живая Safari проверка.

Материал существующей карточки также не выбирается браузером за оператора:
unset остаётся явным empty option, legacy/custom alloy сохраняется selected,
а не молча превращается в первый preset при Save названия/заметки.
Одна structural baseline failure;29 card/time/lifecycle checks прошли.

### Классификация печати и результат контроля — разные факты

REAL_PRINT подтверждается start/burn/layer, не finish. Report не пишет «завершена»
только из классификации; использует existing completion_status=finished.
Четыре failures на отсутствующем/unknown finish;58 report/quality/snapshot checks
после. Headline замечаний сокращён до «Есть замечания по процессу».
Подтверждённое accepted inspection не перекрывается low log score: качество
контроля сохраняется, замечания/низкая уверенность логов отдельно. Один baseline
failure;44 report/quality checks прошли. Rejected по-прежнему имеет приоритет.

Machine setting laser_count1.5 больше не молча округляется до1: HTTP422 без записи;
целые2/2.0/"2" остаются допустимыми. Один baseline failure/3 controls.
Prediction description заданных скоростей/коэффициента не утверждает passport
origin или «выучено/нет истории», когда такой provenance не передан. Это описание,
не смена формулы/source enum. Два baseline failures на original HEAD wrapper;
47 settings/contracts/single-cycle checks прошли. Ошибка collection нового
parametrized class test исправлена в fixture, не production.

### Desktop: async selector и библиотечные события тоже имеют владельца

Import держит busy от начала native picker до validation/adopt/binding/launch.
Поздний selector больше не подменяет runtime после configure другой папки;
double dialog и premature quit исключены. Cancel возвращает captured prior
phase/message, без новой state machine.9 dialog scenarios до6failed/3passed,
после9passed. Сохранение binding по-прежнему no-clobber.

Persistent updater progress/downloaded/error не публикуют чужой lifecycle busy.
Late progress вне accepted update operation игнорируется; accepted check owner
освобождает UI busy в finally даже без downloaded event. Artifact ready/error
metadata сохранены; поздняя signature error продолжает запрещать installation.
3 исходных failures;весьdesktop54/54 прошёл после согласования stale-event control
с новым invariant. Native before-quit/error/timeout/listener cleanup retained.
Только actual library/VM fake process/native emitters, не реальный installer.

## Итерация 6 — ограниченная диагностика и подтверждённое восстановление

### StateFlow: все строки считаются, примеры не размножаются

Сохраняются20 точных malformed examples и aggregate total/examples_retained;
events/transitions/таймлайн/пределы размера и строк не сокращены. Общий
ParseResult.malformed_row_count берёт table counters, иначе точный неотрицательный
integer metadata count; таблица и metadata не суммируются дважды. Это property,
не дополнительное serialized поле. Quality/report больше не теряют StateFlow
malformed count и не принимают bounded examples за весь объём. Aggregate code
нормализуется к прежней категории, не даёт второй penalty. Текст говорит о
несовпадении числа колонок, а не доказанной поломке принтера. Parser0.2.2.

Реальный1,048,568B prefix: traced peak8,412,195→146,550B,
5014→21 diagnostic records. Synthetic550,019B/50k rows:
73,145,208→131,826B,50001→22 records (включая no-transition info).
Все transitions и остальные факты JSON совпадают при одинаковой parser version;
первые20 raw/line/byte offsets точны. Count5014/50000 сохранён; quality92→84 —
исправление пропущенного count, bounded/unbounded варианты дают одинаковый84.
192 expanded parser/profile/quality/overview/API/report/publication tests прошли;
isolated SQLite сохранила SHA/bytes/URI/current snapshot и21 diagnostics с total.
Full StateFlow10MB gate не обходился; filename-date hint не стал explicit date.

### ZIP: читаемое имя отдельно от имени чтения и provenance

Для unflagged UTF-8 bytes используется только strict CP437→UTF-8 roundtrip.
Flagged names/не-UTF8 legacy/native ZipInfo interpretation сохраняются, включая
explicit Unicode Path0x7075 metadata. Logical path проходит прежние safety и
collision checks; archive reading и archive_member_path используют native name.
21 byte-boundary cases, исходно2 failures плюс отдельный Unicode-extra failure;
129 adjacent checks после прошли. Central directory настоящего архива:
69,351/69,351 names восстановлены, raw byte roundtrip/getinfo identity подтверждены,
logical full-path collisions не найдены. Payload не распаковывался; лимит числа
members не расширен. Если legacy bytes одновременно valid UTF-8, намерение
автора неоднозначно: это не универсальное доказательство кодировки.

### Runtime recovery не подтверждается одним снятием pause

Прежний cancel_restart мог освободить maintenance gate после остановки worker:
Running=false/Paused=false не обнаруживалось _unfreeze. Перед release применяются
существующие resume/verify сохранённой версии; failed start/wrong image/readiness
failure оставляют recovery незавершённым и повторяемым. Release owned=false
теперь существующий UpdateError: journal token и чужой gate остаются, ложного
success нет. Четыре строки runtime, без нового протокола/state/token hierarchy.
10 baseline failures→10 passes;72 host/admission/packaged/releases и54 desktop
checks прошли. Normal accepted POST держит drain через body+background task,
late POST503. Docker/NAS не запускались. Verify доказывает существующий контракт
Running/image/API/schema/SHA, не выполнение реальной worker задачи. Forced
ASGI task cancellation и последовательные CLI ownership сценарии не менялись.

### Повторы заголовков и physical layer coordinate contract

StateFlow явно сохраняет repeated_headers; общий ParseResult property принимает
точные неотрицательные counters, таблицы authoritative, без duplicate sum или
угадывания старого header_rows. На genuine1MiB prefix report0→2 при3 headers;
malformed5014/transitions38/все остальные JSON facts bit-identical (новый metadata
key исключён из сравнения).216 adjacent tests прошли, версия0.2.2 unchanged.

Geometry mapping больше не растягивает restarted/partial physical layer numbers
по рангу и не прижимает out-of-range к краям детали. Одна 1-based1..N→ordinal
формула; неизвестная координата остаётся unmappable, исходные process observations
не удаляются. Неверный range endpoint не становится fabricated height range.
8 baseline failures/1 valid partial-log control;152 geometry/process/card API
checks после прошли. Старый valid partial40/60/80→physical60/5.95mm сохраняется.
Это не доказательство физических дефектов или подтверждение datum.
Один старый timing-admission fixture приведён к реальному current origin/link/
revision contract, не добавлен production fallback;31 timing checks прошли.

### Диалог выхода не отменяет владельца async retry

Обычный UpdateBusy→verified old runtime→30s retry мог начать launch, пока native
quit dialog ждёт ответа. Теперь после ответа проверяется fresh state/runtime/
updater busy до stop/allowQuit; используется прежнее wait message. Новых flags/
generations/helpers нет.7 VM scenarios до3failed/4passed, после7passed;61 desktop
checks прошли, включая normal leave/stop/cancel и install handoff. Рабочие
процессы не запускались; это не реальный native installer validation.

## Итерация 7 — привязка импорта, измеренные координаты и владение UI

### Поздний импорт не отменяет ручное решение оператора

Обычный сценарий: загрузка лога A → оператор выбирает B или отвязывает сессию →
анализ A заканчивается. Прежде публикация могла вернуть A и перенести QC. Теперь
до копирования сохраняется immutable card_link_intent_captured в существующем
audit trail. В записи и recovery используется один fingerprint только identity
связи (session/confirmation/evidence), не всей revision/геометрии/заметок.
Fresh card lock и CAS допускают замену только при неизменном намерении. Отказ
привязки не уничтожает успешно сохранённый анализ; причина остаётся в audit.
Legacy replay консервативен: empty/same только, explicit unlink не отменяется,
same-session не переписывает evidence/date/QC. Нового состояния/миграции нет.
14 новых actual-flow cases;256 соседних import/link/publication checks прошли.
SQL race проверена на отдельной SQLite, не на реальном PostgreSQL/NAS.

### Метки физического слоя и время лазера имеют общий источник

Telemetry предпочитает canonical main burn_event после whole-file нормализации,
а table-only fallback проходит тот же TimestampQualityTracker в порядке строк.
OLD и same-source-row NEW попытки допускаются единым calibration predicate;
разные попытки/перезагрузки не склеиваются. Разница первой и последней строки
таблицы больше не выдаётся за работу лазера; исходные строки и часы сохраняются.
На13 реальных датированных парах все16,640 (layer,clock) соответствуют main.
08.06:1132 строки burn; прежде926 ошибочно оставались в предыдущих сутках.
Контролируемая метка09.06 00:00:24: старое207/42s → правильное208/0s. Это проверка
координат, не заявление об обнаруженном физическом дефекте. 23.03:6842 исходных
строки,5000 sampled;4698 прежде с неверной датой при4 переходах суток.
19.05 table-only layer40 span6132s не подменяет реальные попытки147.156/105.859s;
false trend5715.4s → insufficient_data/null,2505 строки/метки не потеряны.
27 новых cases;285 expanded checks прошли. Cold projection импорт тяжёлого
calibration module → общий pure helper:263.894→0.338ms (не steady-state claim).
Файл08.06 burn237,215B, SHA3d0de4e0c6a7b046a3148b524a7ee5e9e6309880ebb477bac6e6a23b00b76614.
Sampled fallback не восстанавливает невидимые переходы суток; это ограничение.

### Кнопки и завершение async операций принадлежат выбранной карточке

Логи открывают «Процесс», отчёт — «Как прошло», приложение логов — существующие
«Файлы», а не новую печать. Enter/Space вложенных controls не открывают карточку
второй раз. Loading скрывает прежние panes; ошибка нового accepted load очищает
старую identity, stale error не отменяет более новую. Перед заменой preview host
вызывается существующий resource cleanup, без нового manager/cache.
Завершение estimate больше не уводит с B обратно в A и не стирает draft/tab A:
accepted generation передаётся до terminal polling, refresh через существующий
sync path. QC POST A больше не перечитывает/пересоздаёт выбранную позднее B;
локальные record/generation и dirty guard действуют на success/error/refresh.
108 web checks прошли; native WebGL lifetime не доказана VM тестом.

Реальный скрытый браузер на собственной SQLite/18013 подтвердил «Логи» →
«Процесс», «Файлы», вкладки и переход в телеметрию;5 synthetic chart points видны.
Playwright click anchor один раз не менял страницу, но AX click с fresh locator
сразу открыл графики: source bug не объявлен. Fixture21 карточка явно подписана
«ТЕХНИЧЕСКАЯ ПРОВЕРКА — не данные печати»; рабочая БД/stack не запускались.

### Копирование событий: меньше работы без изменения фактов

Canonical DTO отдельно копирует mutable source и deep payload, а не повторно
строит immutable datetime/scalars. На66,029 events реального23.03 source JSON и
полный итоговый SHA58285629c631dd4afb3d515b6f17a136cca07e8b99ce30259b0eb4800758456b
совпадают.3 alternating runs median4.55631→3.93821s (-13.6%); traced peak
160,740,397→159,919,053B — значимого снижения памяти нет.72 соседних checks прошли.
Другой prototype bucket-copy отклонён:4.26406→4.95704s (+16.3%) при лишь5.6%
снижении peak. Production bucket algorithm не заменён; не claim total-import gain.

### Final QC принимает физический диапазон, а не произвольный truthy dict

Поддержанный start_layer/end_layer требует1 <= start <= end и целые конечные
координаты. Partial/reversed/bool/zero/negative отклоняются. Unknown annotations
сохраняются, но без text location не удовлетворяют gate расположения дефекта.
Interim и прежние ORM records не переписываются, expected N не угадывается.
Actual API100→50:200→422 без QC/revision/job публикации;100 соседних checks прошли.
Это correction location validation, не опровержение заключения инспектора.

## Редизайн по дополнению владельца

Основная ночная задача сохраняется. По новому запросу дизайн-реализация начинается
13.10 в05:00MSK/02:00UTC, до этого продолжаются общие исправления. Прочитаны
Emil Design Engineering и Apple Design; новые packages не нужны. Цель existing UI:
оператор сначала видит участки для контроля и графики, затем evidence/action/QC;
технические подробности доступны по раскрытию, функции и provenance не теряются.
Нельзя превращать indirect signals в доказанный брак/вероятность или скрывать
неполноту измерений. Existing vanilla JS/Chart/Three, без нового framework/service.
В том же heartbeat сохранён delayed start; финальный срок09:00MSK не изменён.

## Следующие приоритеты, не готовые пункты

### Завершение текущей безопасной итерации

Deterministic import hint recovery: все SID считаются до выбора authority;
единственный SID использует matching текущему fingerprint captured intent,
эквивалентные jobs — stable job-id tie-break. Один CAS, без пересъёмки/fallback
после отказа.6 order/reupload controls: baseline2fail/4pass →41focused/262adjacent
pass. Временное освобождение конкурирующей карточки и настоящий reupload
сохраняют QC/owner/report/job completion. Реальный PostgreSQL не проверен.

Abort после quiescing/restart_ready переиспользует тот же runtime recovery,
а не только unpause. До снятия gate выполняются exact-container resume и
existing readiness/image/SHA/schema checks. Failure сохраняет journal/token;
успешный cleanup повторно выбрасывает original error. Preparation-only cleanup
не изменён.12 baseline failures →18focused/90adjacent checks passed; настоящий
Docker/runtime не запускался. После этого новых backend итераций не начиналось.

### Операторский UI: сохранённый редизайн текущего сценария

Использованы прочитанные Emil Design Engineering/Apple Design: hierarchy,
agency, предсказуемые действия, focus/labels/targets и responsive disclosure.
Новые libraries/services/алгоритмы не добавлены. Сохранены модель/параметры,
оценка/файлы/все исходные рекомендации и история, а не только первые6–8 строк.

| Before | After | Why |
| --- | --- | --- |
| Открытие печати ведёт к модели; «Логи/Отчёт» не объясняют действие | Привязанная печать открывает контроль; прямые «Контроль/Графики/Модель» | Оператор сначала решает задачу качества |
| Шесть пустых аналитических секций и длинные оговорки на первом экране | Участки для контроля сначала, расчёты и диагностика по раскрытию | Пустые расчёты не заслоняют действие; информация не удалена |
| Графики только на отдельной странице с повторным выбором печати | Из уже загруженной bounded telemetry: прожиг/кислород в контроле, остальные каналы во вкладке графиков | Нет дополнительного API fetch, скрытых canvas или нулей вместо пропусков |
| Высоты и детали спрятаны в diagnostics | Физические слои/подтверждённые Z и возможные детали рядом с контролем | Indirect evidence не выдаётся за найденный дефект или координату X/Y |
| Коричневые поверхности, невнятные «Ф/СЛ/АНОМ.—», фиктивный preview без STL | Slate/mint palette, понятные подписи, явное отсутствие STL | Более читаемые состояния и отсутствие ложной модели/«логи OK» |
| QC form не участвует в dirty guard | Отдельный QC dirty flag + существующий edit generation; успешный POST очищает только unchanged draft | Sync/estimate/save не стирает незаконченный контроль |

QC labels связаны с полями, min1/step1 соответствует физическим слоям. После
accepted POST известный audit head остаётся доступен без перезаписи другого
несохранённого form. Canvas создаются только при показе pane, уничтожаются перед
заменой; null gaps не превращены в0. Log-insights запрашиваются только при
первом показе контроля данного DOM-host. Глобальная телеметрия остаётся доступна.
Неподтверждённое начало Z прямо помечено, approximate mapping не стал exact.

110 web checks прошли, включая два actual QC input-event сценария, не только
ручную установку dirty flag. Dashboard composition/labels/template: исправлена
устаревшая source-only expected_revision assertion на local record, соответствующую
ранее исправленному async contract. До неё общий132-case прогон:131pass/1source
failure; повторный dashboard-only прогон прошёл. QC/runtime предыдущие checks
оставались зелёными. Git diff --check чистый.

Настоящий браузер собственной fixture SQLite подтвердил control/tab transitions,
рисование обоих графиков и пустое состояние без ложного «дефектов нет»; console
errors отсутствовали.1440x900 и390x844; на узком экране body.scrollWidth=viewport
390, горизонтального переполнения нет. Финальные tabs сделаны одной прокручиваемой
строкой. Снимки `assets/2026-10-13-ui/outcome-before.jpg` и `outcome-after.jpg`.
Это synthetic техническая карточка, не доказательство качества реальной печати.
Полная визуальная проверка всех legacy screens/нативного WebGL не выполнена.
Повторяющаяся историческая CSS cascade ещё не полностью консолидирована:
палитра согласована механически, новый workflow имеет собственные семантические
классы; не объявляем весь stylesheet чистым или весь проект завершённым.

### Незавершённая находка, не включать в релиз без исправления

Read-only public estimate_plate доказал cold/warm divergence вычислительного
geometry cache: compact coordinates округляют1.000200033mm до1.000mm; при
thickness0.1mm independentceil11, cold11layers/110s recoat, hit10layers/100s,
BodyEstimate остаётся11. План exact computational snapshot при unchanged compact
published snapshot и существующем cache-version bump согласован, но агент
прерван transport failure до production patch: version6/to_snapshot сохранены.
Нельзя считать это исправленным или подменять prepared bounds сырыми STL.

Только собственные временные UI процессы PID66372(uvicorn18013/lifespan off)
и PID28034(caffeinate) проверены по точным argv и завершены TERM без удаления
fixture/artifacts. Временная браузерная вкладка закрыта; viewport override сброшен.
Рабочее приложение/БД/NAS/GitHub/модели/логи не изменялись и не запускались.
Плановый срок ночи не выдержан: безопасное завершение уже начатой UI итерации
продолжилось после09:00MSK; это не запланированное продление автономной работы.

1. Общие ошибки процесса import/attachments и desktop lifecycle: сначала реальный
   call graph и воспроизведение, без частных compatibility путей и новых функций.
2. Whole-file/canonical timestamps и measured-only burn projections для quality
   localization; таблица наблюдений не подменяет время работы лазера.
3. Signed updater и desktop runtime на macOS/Windows не проверены. Полный sensor/
   StateFlow corpus пока не прочитан; независимая точность полного времени
   и физические дефекты детали не доказаны тестами или сохранностью результатов.

План ночи: продолжения в этом чате до09:00 13 октября по Москве; после срока
только безопасное завершение текущей итерации и итог, без новых изменений.
Ночная heartbeat-задача printer-s-companion подтверждена ACTIVE в этом чате,
каждые20мин до указанного срока. Проект снова распознан приложением как Git.
Для работы включён временный idle-sleep inhibitor: own caffeinate PID28034,
запущен13.10 в00:47:48MSK, аргументы `-i -t 29531`; завершится сам около09:00.
Закрытие крышки/выключение Mac или выход из Codex это не предотвращает.

## Продолжение по прямому запросу владельца

13 октября владелец написал «продолжай работу пока не скажу остановиться» и
повторно предоставил AGENTS.md. Это новый допуск продолжения, отменяющий старый
предел09:00; не исправление истории о пропущенном сроке. Прочитан полный текущий
AGENTS.md. Существующие незакоммиченные изменения сохранены. Та же heartbeat
printer-s-companion подтверждена ACTIVE без UNTIL; failed_runs_only сохранён.
Рабочие БД/NAS/stack/операторские процессы и GitHub не изменялись.

### Точный вычислительный кэш, компактное опубликованное представление

Нативный public estimate_plate на бинарном STL684B даёт подготовленную высоту
1.0002000331878662мм, слой0.1мм: независимо ceil=11. Старый cache-hit округлял
верх до1.000мм, получались10слоёв и100s нанесения вместо11/110s. Ранее описанная
находка теперь исправлена: LayerGeometrySeries.to_snapshot(compact=False)
сохраняет точные подготовленные координаты/признаки/body intervals; default
compact=True не изменяет опубликованный округлённый формат. Существующий ключ
переведён наv7 без fallback старых записей. Нового кэша и восстановления prepared
bounds из сырого STL нет. Старые записи/прогнозы не удалены.

Physics/no-cycle, physics/cycle, fitted/cycle: холодный/повторный PlateEstimate
целиком совпадают, включая время,11слоёв,143s при floor и доли двух тел.
Контролируемый baseline7cases:6fail/1pass; после исправления7pass и165соседних
checks pass. Артефакты /private/tmp/pc-cache-precision.GcTDpJ/. Это исправление
повторяемости, не независимое доказательство физической точности времени.

Проверен и следующий переход public combined_prediction → опубликованный
scan_geometry → map_anomalies_to_geometry. Даже при правильном верхнем
prediction.layer_count=11 компактный снимок раньше не содержал собственногоN:
округлённыйz_max=1.0 давал10 и последний физический слой11 выпадал из карты
контроля. Теперь существующий снимок несёт точное целое layer_count из расчёта;
компактные массивы не раздуваются и недостающие слои не выдумываются. Нативный
STL-тест сверяет независимый ceil, слой11 и исключение12;75целевых checks pass.
Точность пространственной формы по-прежнему ограничена sampling/datum/поддержками.

### Принятый расчёт не теряется при загрузке следующего вложения

Обычный воспроизводимый путь: загрузитьSTL → accepted estimate → сразу фото/doc.
Последнее повышает revision; старое задание правильно запрещает публикацию, но
раньше замены не было: реальный process_next_estimate завершалсяfailed ещё до
скачивания модели, прогноз отсутствовал. Общий publisher HTTP/NASsync теперь
переставляет уже pending/running estimate на новуюrevision с тем же originowner.
Фото с другого ПК не переносит вычисления. Старый lease/fence остаётся действовать;
completed/photo-only сценарий не запускает новое вычисление, duplicate — no-op.

Fault injection в той же обычной цепочке выявил первый SQLite SAVEPOINT без
physical outer BEGIN: RELEASE сохранял вложение, хотя последующая очередь
падала и revision откатывалась. Publisher теперь берёт существующий общий
configuration→card guard до чтения/вставки: fingerprint стабилен, физическая
транзакция включает file/revision/job. Без нового механизма блокировки или
транзакции вокруг передачи MinIO.13focused checks pass;161общих HTTP/card/
comparison/attachment checks pass после явного current revision в двух fixtures.
Нативный PostgreSQL здесь не запускался. Общая SQLite policy отдельно проверяется,
поскольку корректность одного publisher не доказывает всех savepoint путей.

### Текущий прогноз и история — разные состояния

Реальная карточка с2STL: опубликованный расчёт inputrevision3/card4; удалить
тело→card5, последнее→card6. Старый JSON с n_parts2/time/cost остаётся, но ранее
UI показывал его как текущий. GET/list/PATCH теперь отдают общий existing
prediction_is_current; comparison не показывает unverified time/cost/error как
актуальные. Legacy без inputrevision тоже не считается подтверждённым. История
JSON не переписана. PATCH отдельно проверен: publishedrev2→изменение слоя→rev3,
ответFalse, сохранённый прогноз тот же, GET согласован.

UI допускает текущие predicted time/cost/error и geometry fallback только при
explicitTrue, не скрывая измеренные факты/число слоёв из логов. Старый расчёт
доступен по раскрытию с существующими действиями перерасчёта/файлов; отсутствие
доказательства не подписывается как доказанная устарелость.32focused/123full
web checks pass, syntax/diff clean. Это консервативный existing revision gate,
не завершённое разделение семантической свежести и fencing: фото/notes/QC после
completed могут снять допуск, а machine-settings не меняют cardrevision.
Этот общий архитектурный вопрос остаётся открытым; частных исключений не добавлено.

### Реальная повторная загрузка аналитики после сетевой ошибки

Lazy host раньше получал loaded до await, поэтому после503 повторное открытие
не отправляло запрос. Теперь lifecycle marker принадлежит log-insights.load:
pending/success не дублируют запрос, failure освобождает marker и предоставляет
работающую «Повторить». Detached host не получает запоздалый response/error и
не запускает retry.6focused cases pass, включены в общий web прогон выше.

Попытка повторить это в настоящем браузере остановлена до запуска fixture:
CUA inventory browsers=[]; IAB и Chrome сообщают Browser is not available.
Ни сервер, ни БД, ни вкладка этой проверки не запускались. Браузерное подтверждение
новых retry/history/graphs остаётся невыполненным; Node callbacks его не заменяют.
Предыдущая настоящая synthetic UI проверка выше не объявляется повторённой.

### Проверяемый запуск schema-check без импортного обхода

На отдельно созданной /private/tmp/companion-resumed-migration.VTSqHo/migrations.sqlite
в APP_ENV=test прошли22миграции. Direct scripts/check_migration_drift.py не видел
core (sys.path начинался scripts/); прежний CI обходил это PYTHONPATH. Стандартный
`python -m scripts.check_migration_drift` работает из корня и подтверждает
OK: migrations reproduce the ORM models. CI/AGENTS/docstring используют эту
единую команду без ad hoc PYTHONPATH. PostgreSQL CI изменён только локально,
не запущен и не опубликован. Dependencies export-check прошёл; внешних пакетов
для этих исправлений не добавлено.

### Чтение геометрической привязки без полного контекста сессии

get_card для mapper читает две scalar JSON-проекции health и telemetry вместо
материализации BuildSession со всем runtime/report/source context. После короткого
чтения rollback по-прежнему закрывает соединение до локального mapping; доступные
аномалии, bounded/legacy telemetry и выход не сокращены. Общий pure admission
helper импортирован один раз, геометрическое unknown-состояние использует ту же
причину, что comparison, без заявления об изменившейся модели при missing revision.
80целевых card/comparison/geometry checks pass, включая настоящий mapper и
проверку отсутствия удерживаемого SQL соединения. ORM session/job payload не
материализуются для этого чтения.

Отдельная synthetic SQLite /private/tmp/companion-card-projection.4GKx1C/:
context3,031,614B → нужная JSON-проекция337B; точное равенство health/telemetry.
5alternating runs под tracemalloc: median88.30→29.43ms, Python-traced peak
8,356,328→22,952B. Это ~3x для одного чтения на искусственном тяжёлом контексте,
не заявленное ускорение всего экрана или сети NAS; память SQLite/RSS не измерена.
Полный web прогон повторно123pass. Dependency export соответствует pyproject.

Read-only freshness audit воспроизвёл13repository/service flows. notes/photo/QC:
physical inputs совпадают, revision-based gate снимается. Machine hatch/recoat/
default-preset hatch: cardrevision прежняя, физические входы изменились, gate
сохраняется. Body/strategy/datum/thickness корректно меняют оба. Старый full hash
содержит revision и params.updated_at; нельзя просто удалить revision и объявить
его семантическим recipe hash. Нового неполного fingerprint или обновления
provenance задним числом не внедрено. Artifact freshness.json и воспроизводящий
audit_prediction_freshness.py в /private/tmp/pc-cache-precision.GcTDpJ/.

### Один допуск автоматического импорта для всех входов

При REQUIRE_OPERATOR_IMPORT_CONFIRMATION=false detect раньше оставлялdetected,
а owner-local claim выбирает только checking_stability/postponed. Watcher сам
делал второйHTTPconfirm, но startup/browser/rescan не делали: поддержанный
автоматический режим зависал без выполнения. Теперь общая регистрация применяет
существующий mark_import_job_confirmed с actor automatic_policy, timestamp/audit
и checking_stability. Второй watcher HTTPconfirm удалён, claim/status/owner/lease
не расширены. Приtrue остаётся ручное подтверждение.

Новый explicit /sessions/ingest публикует созданное задание до accepted response;
раньше initialqueued попадал в earlyreturn _enqueue, не сохранялся. Recoveryv2
принимает только исходное queuedautomatic receipt с подтверждением и нулевым
execution/stability/lease state. Старые detected/manual квитанции и policyflip
не переинтерпретируются как новое разрешение.22новых и267соседних checks pass
(289total). Настоящие parser/worker в изолированнойSQLite опубликовали2слоя и
согласованный report одного поколения; sourcebytes unchanged. Проверены foreign
worker, activelease, postponedresume, повторная регистрация и explicitintake.
Операторский импорт/stack не запускался.

### Общая атомарность первой вложенной SQLite записи

READ-ONLY actual API proof на /private/tmp/pc-sqlite-policy.GG9ez2/ показал:
manual retrain/recalibrate, первый SAVEPOINT → RELEASE, ошибка внешнего commit →
rollback, но pendingjob сохранялся и выполнялся настоящимconsumer. QC после
импорта безопасен благодаря предшествующемуDML; это не оправдывает общую policy.

В storage/db/session.py один public ConnectionEvents.savepoint hook открывает
physical BEGIN до первого SAVEPOINT только для SQLite/not in_transaction.
Нет отдельныхBEGIN вкаждомAPIcaller, connect/isolation overrides или глобального
BEGINIMMEDIATE длячтений. Существующие short configurationguard иlegacy чтения
сохранены; полноценный nonlegacy SELECT/DDLtransactioncontrol не заявлен.
12transaction checks pass: outerrollback/sessionclose/dependencyexception,
невидимость доcommit, nestedrollback, singleBEGIN SQLtrace, существующийIMMEDIATE
writerreservation, refresh после конкурирующей записи, отказBEGIN без частичного
SAVEPOINT.188adjacent checks pass (Jobs/estimate/settings/calibration/retraining/
importpublication/attachments), до отдельной HTTPtruthfulness поправки.
Python3.11.15 smoke public hook сSQLAlchemy2.1.4 повторил тот же trace/visibility/
rollback/IMMEDIATE, не полный запуск приложения на3.11. Артефакты
/private/tmp/pc-sqlite-savepoint.CcyagO/; нативный PostgreSQL не проверялся.

### Ограниченная сверка реального источника и локализации

Прочитаны4лога08.06 из read-only Печати.zip; извлечено11,273,518B, SHA копий
независимо совпал с членами архива. sensors
07cbda022100aaa193486b41d2d9c832833353291968d7a08f21544d94ea63a2:
raw2157 SO2=0.15/21:06:22 → healthapprox layer4/nearest_burn_log_timestamp/21s.
main1c2b3665753ba88aa0a53d68eb104f137b3f3e17ceb061666f896e636c046cda:
raw44 layer4/21:06:43. sensorsraw4704 SP4=1.0140/21:48:50 → approx49/158s;
mainraw174 layer49/21:46:12 и175 ПАУЗА21:46:56. Это наблюдение в паузе, не
нормальный прожиг и не доказанный дефект.32,117active-window samples восстановлены.

Sourcecoords остаются вhealth/analysis_snapshot/APIсессии/_pcSession; compactreport
намеренно не дублирует их. Новая потеря парсером на этой выборке не доказана;
добавление координат в ещё одинsummary отклонено. БезSTL дополнительные generic
peaks отдельно не показаны;81участокFlowH сохранил unknownZ/emptybodies, пороги
confirmedfalse, итогinsufficient_data. Никаких guessedMonitor100 meanings/height.
Repro/manifest/rawmatches/overview: /private/tmp/pc-log-localization.cWtkUV/.
Это не весь16GBcorpus и не физическая проверка качества детали.

### HTTP подтверждает запись до отправки ответа

К общей SQLite проблеме добавился отдельный ASGI lifecycle дефект: default
yield dependency завершается после response. Отказ Session.commit возвращал
HTTP200 retrain/202recalibrate вместо ошибки; первый SAVEPOINT ещё и оставлялjob
до описанного исправления. После hook данные правильно откатывались, но HTTP
успех всё ещё был ложным. Важны обе разные границы, а не только тест отсутствияjob.

Прямые12Depends(get_db) и две общие repository dependencies используют public
scope=function. Trivial repository yield wrappers убраны: обычный return Repo(db).
Нет нового middleware, lifecycle adapter или per-endpoint commit обходов.
Нужный существующему механизму minimum FastAPI0.121 отражён вpyproject и штатном
requirements export; установленная0.143 не менялась, пакеты не скачивались.
Механика проверена по [официальной документации FastAPI](https://fastapi.tiangolo.com/advanced/advanced-dependencies/).

13actual ASGI controls pass: наблюдение http.response.start подтверждает commit
и Session.close доheaders; failedcommit даёт500 и отсутствиезаписи, успешные
200/202 сохраняютjob/card. Direct import confirmation сохраняет прежний manual
state при отказеcommit. MinIO fake stream читаетбайты послеclose, failedcommit
не начинаетstream; delete cleanup выполняется толькопослеcommit. JSONexport
detached, SSE имеет свои короткие poller sessions, request session отсутствует.
Streaming background tasks не получают живойrequestDB. Реальный NAS не проверен.

Совместный SQLite/API/Jobs/settings/estimate/calibration/retraining/import/
attachments прогон202pass, включая17SQLitecases с actual500 безослабленияassertions.
Дополнительный broaderAPI прогон401pass (API/import/card/QC/calibration/LLM/
update admission/dependency projection); это не полныйtest-suite или проверенный
нативныйdesktop/Windows/подписанныйinstaller. Whole-checkout Ruff, requirements
export-check и git diff--check также чистые после объединения этих изменений.

### Контроль качества не теряет подтверждённую ML-разметку

Проверен actual HTTP finalQC → create_quality_outcome → enqueue_retraining →
prepare_retraining на отдельной /private/tmp/pc-qc-dataset-audit.zzKkPq/SQLite,
без modelworker/обучения. Техническая acceptedметка повышает cardrevision2→3 и
снимает свежесть времени, но prepared dataset сохраняет ровно1label0, returned
outcomeID иserverUTC. Audited correction rejected:revision3→4, ровно1label1
последнего исхода; обаimmutableoutcome и дваowner-local запроса остаются.
Это синтетические результаты контроля, НЕ результат реальной инспекции.

Входом была скопированная без изменений реальная аналитика08.06, snapshotv1/
ANALYSIS_VERSION0.5.6; groupSHA
1471c559a52e66c2190afe670ece05b3f4a5b6605b3b2f985477547bb22d9a85 иfeaturevector
не изменились после QC. Fixturefreshness содержиттолькоinputrevision, без
выдуманногоSTL/прогноза времени. _labelled_sessions проверяет strictsessionlabel
иcurrentanalysis, неPrintRecordrevision. Новой ошибки потериразметки не выявлено;
код не менялся. Один исход не доказывает объём/оба класса/held-out качество ML.

### Конфигурация будущего расчёта отдельно от исторического режима

Подтверждены разные обязанности: revision/input_hash защищают публикацию,
настроенная конфигурация нужна для будущего перерасчёта, а историческая печать
должна сохранять собственные входы. Заметка/фото/QC могут снять revision admission
без изменения физики; машина/пресет могут измениться без revision карточки.
Сведение этих проверок к одному хешу отклонено как небезопасное.

Расчёт прежде не вызывал общий build_provenance и не сохранял полного config hash.
Теперь configuration schema1 собирается из detached resolved params, material,
printer_id, powder_cost и ANALYSIS_VERSION. Исключён только верхний updated_at,
не вложенные метаданные моделей. Snapshot содержит config hash, build/git/
source_state и точную применённую scan-reference identity, без дублирования
полных карт моделей. Неизвестный build не объявляется clean. Для применённого
controller cycle сохраняется версия контракта и явное ограничение отсутствующего
artifact ID, а не ложное подтверждение точной модели. Полный correction factor
1.123456789 больше не превращается в1.123 до capture physics beta.

GET/list добавляют отдельный prediction_configuration_is_current true/false/null:
актуальные настройки читаются пакетно (10карточек — одинSELECT пресетов), SQL
закрывается до локального hash/resolve. Оценщик и чтение используют общий чистый
parameter resolver, прежние формулы и revision/input-hash fencing сохранены.
Legacy без capture и PATCH без полной read-проекции имеют null, не сегодняшний
пресет как выдуманный прошлый режим. Смена hatch/recoat/cost/modelmap даёт false,
а повторное сохранение тех же параметров — true. Исторический JSON, численные
значения и comparison summary при этом не переписываются.

| До | После | Основание |
| --- | --- | --- |
| Сохранённый расчёт не отличался от расчёта по нынешним настройкам | В плане одна метка true/false/unknown и короткая строка возле существующего перерасчёта | Совпадение настроек не подменяет подтверждение реального режима |

Emil Design Engineering/Apple Design использованы для одной предсказуемой
метки/кнопки: без нового макета, анимаций или библиотеки. При false сохраняются
время/стоимость/слои/историческая ошибка. Исчезло подставление нынешнего printer_id
в старый прогноз. Проверены escaping и настоящий callback существующего
estimateRecord, без автоматического enqueue и без нового пути вычисления.

17producer checks: baseline17fail →17pass, включая настоящий локальный native
STL calculator для physics/fitted. Reader9baselinefail →42pass всего модуля.
153совместных checks после объединения (producer/cycle/cards/comparison/estimation/
provenance); старый cycle fixture получил явные input_revision1/recordrevision2,
не fallback в production.128web checks pass. Whole-checkout Ruff, requirements
export и diff-check чистые. Артефакты /private/tmp/pc-prediction-provenance.k60FvX/.
CUA browsers=[]: native browser/установщик не запускались; Node проверки не
выдаются за реальные клики в Safari.

Независимый исторический proof /private/tmp/pc-history-scope.XRjoOx/ использовал
3synthetic карточки по200слоёв, native TimeLogParser, проверенный timing manifest,
actual HTTP → owner-local calibration consumer дважды. Оба заданияdone/published;
по3time/recoat/scanrecords и одинNNLS artifact с597acceptedpairs сохранились,
liveconfig изменился, historicaldata fingerprint и старые прогнозы — нет.
91calibration checks pass. Поэтому prospective configuration flag сознательно
НЕ передан в calibration admission. Это не доказательство as-run рецепта или
held-out физической точности.

Остались source-revision binding для физических входов вместо всей карточки,
отделение prospective forecast от post-hoc reconstruction в accuracy и полная
identity controller artifact. Новый flag обновляется при GET/list, не обещает
SSE-обновления уже открытого экрана после смены настроек. Full params hash
консервативен: смена даже неиспользованной model map снимает configuration match,
но не уничтожает историю. Отдельно найден возможный общий конфликт приоритета
active preset и глобальной hatch_speeds_by_mat; ещё проверяется, не исправлен
изменением комментария. Рабочая БД/NAS и публикация не затрагивались.

### Исправлен реальный приоритет пресета, preview без второго merge

Подтверждён обычный конфликт: машинаscalar1000/mapsteel2000/пресетscalar4000
давал фактические2000мм/с в plate, preview, scope и capture. Ошибка в разрешении
входов, не в provenance. После всех overrides общий resolver убирает только
текущий материал из копии карты, когда preset/card явно задаёт скорость.
Null/no-preset и другие материалы не меняются. Preview вызывает тот же resolver,
лишний _merge_preset/список полей удалены; defaults, hatch/powder overrides и
отсутствие параметров сохраняют прежний ответ.

Независимая арифметика одной reference-строки: hatch/contour/travel/delay/support
0.737→0.487s. NativeSTL5×5×1мм безкэша: прожиг0.9931439499433392→
0.4965719749716696s при4000мм/с, preview=plate. Старый scan artifact со скоростью
2000 уже отвергается существующим scope hash; совместимый4000 сохраняется.
Нет широкого version/cache bump или подгонки под логи. Это математическая
согласованность заданных параметров, не физически проверенная точность.
23focused (baseline5fail/18pass),241совместных checks pass. Артефакты:
/private/tmp/pc-preset-scan-fix.dhrQoC/ и /private/tmp/pc-preset-speed-audit.xAO4d3/.

### Сохранённая карточка сразу обновляет отображение, не перезагружает STL

Обычный material PATCH менял API revision/flags, но plan DOM оставался с прежней
меткой текущего прогноза. Same-revision GET/SSE не мог исправить уже adopted
revision. Теперь guarded save обновляет существующие what/plan проекции сразу,
не меняя tab/QC/process/files. Все5 livefields, более новые правки и независимый
QC draft сохраняются. PATCH config=null остаётся unknown, не ложнымtrue.

| До | После | Основание |
| --- | --- | --- |
| Material save сохранял старый актуальный план до повторного открытия | Committed response сразу обновляет план/метрики, stale остаётся историей | Оператор видит результат именно сохранённой карточки |
| Полный re-render заново загружал STL и сбрасывал ракурс | Тот же DOM previewhost/renderer переносится без cleanup/load; новые поля сохраняют focus/selection | Изменение заметки не требует новой нарезки/загрузки модели |

Actual существующий loader с контролируемой DOM/Three границей: дваsave →
ровно1download/1parse/1renderer/0disposal; следующая карточкаB →1disposal.
Registered front-camera callback сохраняет позицию[0,-12,0], newer note
backwardselection3..14/focuspreventScroll сохраняются. Это проверка настоящих
callback/loader путей, не нативные WebGL pixels.5новых checks: baselinefail→pass.

Изолированный actual API /private/tmp/pc-ui-save-browser.L0aOPg/:
GETrev4 current/configtrue → materialPATCH200rev5 sourcefalse/confignull →
GETтойжеrev sourcefalse/configfalse. ШтатныйestimatePOST200 pending возвращает
owner-local job task_14022d948c8b432885505599bd443856, повторныйPOST присоединяется.
Технический fixture не выдаётся за реальную печать; consumer в этом proof не
запускался. CUAinventory позже стал показывать iab, но создание tab вернуло
точный Browser is not available: iab. Own lifespan-off API18867 остановлен,
порт свободен; новый native browser результат не заявляется.

### Принятый расчёт переживает приём и привязку логов

Actual workflow baseline /private/tmp/pc-import-continuity.EQO6Z8/:
STL→rev2/pendingestimate; приём datedtime.log→rev3; operatorconfirm/import→rev4/
linked/done,2verifiedLayerSnapshots+report того же manifest. Старый единственный
estimate отвергался как stale; prediction=null/estimate_job=null. Ручной запрос
потом считался, значит причиной был lifecycle, а не STL/численный калькулятор.

Теперь единая consumer continuation обрабатывает доказанно изменившиеся входы,
а не конкретный uploader. Configuration→linked session→card→job locks, fresh
strict owner/lease/generation, атомарный failстарого + enqueue/joinнового.
Текущие входы готовятся ровноодинраз; после расчёта нужен отличный full inputhash,
до него — положительная целая отличающаяся requestrevision. Старыйpayload не
переписывается. Отказenqueue откатывает староеfail; expiry между strictcheck и
fail запрещает successor. Corruptedsnapshot при неизменных входах, отсутствующая
геометрия/параметры, foreignowner, потеряннаяlease и завершённыйworkflow не
начинают новый расчёт. Immediate attachment renewal оставлен: его producer-
visible момент новой очереди является прежним контрактом, а consumer-path
объединяет такой запрос вместо создания дубликата.

Publisher тоже брал joblock доconfiguration, вопреки своему порядку родителей.
Строгая перваяleaseпроверка перенесена послеconfiguration/session/card locks,
доinputsread; финальнаяfreshпроверка сохранена. Orderbaseline[lease,config,lease]
→[config,lease,lease];46соседнихservicechecks pass. Это локальная проверка порядка,
не выполненный PostgreSQL concurrent/deadlock тест.

UI immutablefailedjob не выдаёт за успех и не делает ещё одинPOST: при его отказе
читает существующий GETcard и следует активному successor в том же bounded
polling/generation. Если successor уже завершился, учитывается только отдельный
published/current/configmatched ownerquote с inputrevision>=старойrequested.
Более ранний прогноз не скрывает failure; навигация/dirtydraft защищены. История
старогоfailedjob не меняется.4baselinewebfail→4pass, всего137web checks pass.

25новыхcontinuation checks;329broader worker/owner/import/attachment/card/
calibration checks pass. После финального объединения108checks pass (continuity/
preset/provenance/comparison/order), включая nativeSTL→logs→confirm→verified
archive/timings/report→link→автоматическиdonefreshestimate. GETcurrent/source и
configuration flags обаtrue. Legacyidempotency fingerprint не включаетobjectURI:
URI-only directchange с тем жеrevision возвращает прежнийterminaljob, который
не объявляется активным successor. Force/manual-ключ как обход НЕ добавлялся;
это отдельное ограничение общего request identity, не приоритетный частныйfix.

Рабочие данные/NAS/исходники/GitHub не менялись. Полная physicalaccuracy,
as-runrecipe, cycleheld-out и nativePG/Desktop остаются непроверенными.

### Три параллельных локальных чата

По прямому запросу владельца созданы и первый раз проверены active:
логовый01a13668-3f24-7712-9bb9-7f23c4776cd0,
desktop01a13668-2d0a-76e3-bdc6-110c121886d5,
UI01a13668-1e0a-7e20-9c65-8edfb03e1926. Непересекающиеся области и отдельные
review перечислены в текущем плане. Общие документы и estimate/import остаются
у основногочата. Их будущие результаты ещё не проверены и не включены в эти
утверждения о готовых исправлениях. Дополнительные ветки/релизы/сервисы не созданы.

### Первый согласованный результат логового чата: аналитика0.5.7

Проверены peer source/review и прямой userreply «Да, включить; общую версию
согласовать в основном чате». TimeLogParser0.5.1 добавляет уже посчитанный
malformed_rows в ParseResult.metadata. До этого diagnosticcount20 превращался
в reportcount0 иqualityissuecount1/score92; теперь count20/score84 по прежней
формуле, не новой шкале брака. OLD/NEW события и измерения не меняются.

Основной чат поднял ANALYSIS_VERSION0.5.6→0.5.7 и обобщил описания ошибок:
«строк с нарушенным форматом или значениями», а не только width mismatch.
Feature-schemahash включает общую версию; прежние snapshot/model admission
остались строгими. Старые отчёты и registry не переписывались, для новых
аналитических признаков нужен реальный reanalysis, не замена строки версии.
129совместных SQLite checks parser/quality/snapshot/retraining/defectrisk/
provenance/comparison pass. WholeRuff/export/diff-check чистые на этом шаге.

Логовый чат также сохранил полный поток sensor_samples при вынесении неизменных
индексов/правил из построчного цикла: в проверенном real08.06 источнике43,731
строка иdiagnostics до/после совпадают полностью, source/outputSHA записаны
в собственном review. Median0.673530→0.591353s на пяти чередующихся запусках
(примерно12.2% меньше для данного чтения).90target checks. Это измерение этой
операции, не всей программы/NAS и не подтверждение физической точности.

Отдельная source-only правка thermal_nonuniformity.inputs перечисляет только
каналы, реально участвовавшие в расчёте (минимум5пригодных значений), в прежнем
порядкеST3/ST4/ST5; ST4 с меньшей выборкой раньше показывался лишним источником.
Method soft-sensors0.1.2, числа/формула/пороги неизменны.3focused/56adjacent
checks peer; root source/review прочитаны. Включено в ещё не опубликованное
поколение0.5.7, не выдаётся за найденный физический дефект или новый алгоритм.
Детали иSHA-сверка13time.log/16,639summaries: отдельный
docs/reviews/2026-10-13_parallel_logs.md; исходники/рабочаяБД не изменены.

На передаче текущей безопасной итерации повторный whole-checkout Ruff,
requirements export-check и diff-check чистые. Собственный temporary18867
listener отсутствует; root pytest sessions завершены. Три отдельные задачи
пользователя продолжают работу в своих областях, не остановлены. Работа проекта
не объявлена завершённой и публикация/проверка операторского NAS не заявлены.

### Интеграция параллельных чатов: стабильность и аналитика0.5.8

Прочитаны их source/review; области записи сохранены. Monitor100/2000.4.4
отмечает partial_recovery при уже существующем out-of-order/prefix warning,
не меняя события, часы или quality score. Табличные parsers/full sensor/StateFlow
выбирают прежний delimiter один раз: `Time SO1 SO2` + `12:00:00 0,1 0,2`
больше не превращает0.1/0.2 вnull/2.0 без malformed flag. Sensors/Burn/TableTemp
0.2.2, StateFlow0.2.3; quoted CSV и неизвестные ячейки сохранены.
ANALYSIS0.5.8/log-insights1.0.9 согласованы основным чатом, не переименованием
старого JSON.99 root parser/snapshot/retraining/provenance checks pass.
Real43,731sensor/1,132burn records и ограниченный SHA-проверенный1MiB StateFlow
префикс имеют прежние значения; полный99MB StateFlow не проверен/не разрешён
size gate. ПолныеSHA/детали — parallel_logs review, не новый аудит всегоZIP.

Desktop: ошибка stop больше не показывает ready с остановленными consumers;
принятое stop отменяет deferred-update таймер. Readiness требует всего enabled
REQUIRED состава из собственного Compose snapshot; отсутствие estimator не
регистрирует успешную установку. Subprocess явно читает UTF-8 Docker paths,
не ANSI locale Windows.85 root runtime/packaged/command checks pass; desktop
Node также проходят. Это FakeCommands и реальные короткие Python pipes наMac,
не выполненный native Docker/Windows/installer сценарий.

UI: QC повторный POST до ответа запрещён только busy-кнопкой, поля/черновики
не блокируются; архив применяет последний ответ и проверяет HTTP status;
503 панелиТО не уничтожает доступные сведения о печатях. Один generation/
cleanup существующего viewer покрывает open/close/upload/reload. Late ответ не
открывает закрытую модель, RAF/controls/mesh/grid/renderer освобождаются.
После upload используется guarded sync-refresh вместо принудительного открытия
карточки: навигация, обычный иQC drafts сохраняются. Повтор списка телеметрии
не пропускает страницу, native button заменяет onclick div без нового дизайна.
Peer162web checks, root226combined web+desktop после display-case; Node/Three
границы моделированы. Browser/WebGL/responsive/native keyboard не проверены.
Две старые Response fixtures получили ok:true, HTTP guard не ослаблен.

### Оценщику не нужен весь контекст сессии ради идентификатора

prepare_prediction_inputs загружал BuildSession целиком только для printer_id;
publication parent-lock тоже материализовал telemetry JSON. Теперь scalar
printer_id и SELECT session_id FOR UPDATE, тот же parent/lock order/finalfence.
Missing/null printer сохраняет прежний metadata fallback; контекст не меняется.
Baseline2fail/1control→3pass;104 adjacent root checks, затем117 с continuation/
snapshot/cards в изолированной SQLite. Native STL/import consumer пути сохранены.

Read-only benchmark на одной и той же synthetic SQLite fixture (12 повторов):
context3,624,905bytes; prepare median50.168→3.312ms, около15.1×.
Python tracemalloc peak10,351,026→30,575bytes. Полный preparedSHA
637fe0ee023e6c717b597b6270e9cd9f9298f9a1fb95f285f7fd7e6d721edece иinputhash
32fc7e6131428eb1a6b9dcc08eb6b3ab2a0797555452a85397f88288c42ffa2d равны.
ContextSHA6d8e0a7dadd410d774beaf78ba084ddaced98c07e4122db9592f28b6bb976be7
равен. Это SQL/Python операция под tracemalloc, не RSS, NAS, весь расчёт или UI.
Артефакты: /private/tmp/companion-projection-before.9QjQH2/test.sqlite и
/private/tmp/estimate_projection_benchmark.py; рабочие данные не использовались.

### Уже выполненная цель не запускает третий расчёт после reclaim

Native before: STLrev2 → настоящая lease60s старого запроса → docrev3/successor
→ done/inputrev3/cardrev4 → реальное истечениеTTL → reclaimgen2 → лишний
thirdpending/rev4 → identical prediction/cardrev5. Ни status/payload/lease/часы
не подменялись. After в отдельнойSQLite: failed-old +done-successor, cardrev4 и
прогноз неизменны; третьего запроса/расчёта нет.

После strictfence и атомарного retirement общий consumer сверяет positive
captureinputrev>=requestedrev, известный publication+1, полный existinginputhash
с восстановлением только записанной revision в detached-копии и existing
configuration provenance/schema1. Config hash включает поколение аналитики:
старый расчёт не закрывает новую цель. Нет whitelist полей, force-key, нового
helper/cache или изменения history/calibration admission. Более новый manual,
изменившиеся настройки/геометрия/машина/порошок, legacy/corrupt capture, owner/
lease loss и failedcommit не подавляются.39 focused/368 adjacent peerchecks;
117 rootcombined checks в текущем поколении. Контролируемые clock fixtures
edge-тестов отдельно обозначены, не выданы за естественное истечениеTTL.
Proof2.sqlite/after.sqlite иscript: /private/tmp/pc-completed-estimate-audit.2bUpgl/.

### Computational capture не округляется для отображения: аналитика0.5.9

Общий native STL→estimate→snapshot→accuracy путь подтвердил потерю точности:
5×5×5мм body +support,100layers, rawscan20.9726255603s сохранялся как21.6s,
rawrecoat1000s как1000.8s. Три confirmed synthetic пары с полным проверенным
timing manifest/integer-ms фактами давали ложный factor0.972 вместо независимого
21/20.9726255603=1.001305→1.001. Дополнительная геометрия показала ту же причину,
не подбор коэффициента под конкретную модель.

combined_prediction теперь сохраняет точные существующие phase-hour поля,
normalcycle/base/floor иdatum с реальными численными consumers. Формулы, gates,
keys/schema, controllerunknown=null истоимость не менялись. Displaygeometry,
recoat_time_ms/layer_overhead_hours остались компактными; graphreference-сетка
из compactscan_geometry не стала точной от правки top-leveldatum. План/архив
форматируют часы только при HTML-выводе, _pcTimeMetrics иsource не округляют.
3старых serializer assertions стали22/3600 и40/3600, не productionfallback;
nested displayprediction.value остаётся0.011.

Выявлена соседняя граница: округление interval hours превращало точку,
равнуюunit-ratio истории, в falsely outside. Только два existing returnexpression
calibration_interval_hours теперь точные; strictcomparison, выборка/коэффициенты
и предупреждение при действительноoutside сохранены, tolerance не расширялся.
Общее поколение0.5.9/log-insights1.0.10; restoredAPP1.8.0 не новый релиз.
234 rootproducer/accuracy/cycle/log/geometry checks перед финальной interval
правкой прошли; последующая проверка interval/consumer отмечена далее.
История не переписана: ранее округлённые snapshots остаются округлёнными и
legacy ratio reader сохраняет прежний допуск. Их прежнюю точность/коэффициенты
этот producer-fix не восстанавливает. Отдельный долг — numerical-capture
admission автокалибровки без потери истории и без current-config запрета её
собственной обучающей выборки. Физическая/held-out точность, as-runrecipe и
nativePG/Desktop по-прежнему не доказаны.

### Завершённая интеграция и возврат палитры — 14 октября

Новый manifest reupload после completed outbox receipt теперь получает новый
file_id; повторы текущего manifest и deterministic operation_id сохранены.
До: удаление STL → reupload → старый DELETE удалял восстановленную геометрию.
После: старый DELETE404, новое вложение/URI/revision/estimate сохранены.
13 новых и общий attachment/outbox набор101 checks pass. Источники не тронуты.
Lost-ACK pending manifest по-прежнему восстанавливается с собственным прежнимID:
это ограничение recovery, а не универсальный delete-cancellation протокол.

ANALYSIS_VERSION0.5.10: общий numerical-capture gate требует current version
snapshot и print-estimate provenance только для нового ratio/NNLS обучения.
Native HTTP→calibration consumer: старый roundedcapture больше не применяет
0.972; текущий сохраняет1.001 в двух последовательных калибровках, даже когда
configurationmatch=False после self-apply. История/измерения/log-only cycle и
recoat остаются. Mixed/unknown/conflicting поколения покрыты12 новыми checks.
Current synthetic builders четырёх старых тестовых модулей получили явный
producer, старый phase-precision capture помечен0.5.8 и сохраняет history3/usable0.
Root combined run:107passed/1failed; единственный отказ — отсутствующий
необязательный lightgbm и ожидаемый logreg fallback, не calibration regression.
Тяжёлая зависимость не скачана; полный suite зелёным не объявляется.
Изоляция: /private/tmp/pc-calibration-integrated.kmYXMd, APP_ENV=test.
Уже сохранённые поправки/карточки не переписаны, физическая/as-run/held-out
точность этими synthetic проверками не доказана.

Интервал/consumer после предыдущего producerfix: root116checks pass, peer281;
наборы пересекаются. Финальные UI и desktop проверки: root237 Node checks pass,
native installer/WebGL не доказаны. Логовый чат read-only проверил8 небольших
Monitor копий:1452 events→897 canonical,92 groups, provenance всех1452 сохранён;
ограниченный набор не объявляется аудитом всех исходников.

По запросу «запусти проект в браузере» запущен только API текущего checkout
на127.0.0.1:8002, PID48019/session70024, отдельная новая SQLite в
/private/tmp/companion-browser.1c8ZvV.22 миграции прошли, dashboard/health HTTP200.
Каталог пустой, readiness503 (Redis/MinIO отсутствуют): это просмотр интерфейса,
не полнофункциональная операторская установка. Docker/NAS/worker не запускались.
Пользовательский browser tab открыт и оставлен; процессы проверок завершены,
API оставлен для запрошенного просмотра, данные не удалены.

По прямому запросу владельца восстановлены точные прежние цвета HEAD,
без git checkout/reset и отката существующих UI исправлений. Emil/Apple навыки
применены для предсказуемости/сохранения компоновки, новых анимаций/слоёв нет.

| Before | After | Why |
| --- | --- | --- |
| Slate/mint поверхности и акцент | Тёплый тёмный фон #201e1d, акцент #f6a06b, CTA #c67139 | Прямой выбор владельца, знакомая производственная палитра |
| Mint линия прожига в контроле | Прежний тёплый акцент | Согласованность графика и интерфейса без изменения данных |

Изменены цвета в существующем CSS и один Chart dataset color; typography/layout/
responsive/QC/source/async contracts сохранены.47 целевых Node UI checks pass,
syntax/diff-check чистые. Native IAB после reload(ignoreCache=true) подтвердил
computed palette/accent/primary и видимый результат; обычный reload сначала
показывал cached CSS. Скриншот: /private/tmp/companion-restored-colors.png.
Это empty preview, не реальная карточка/QC/полный график. Рабочая БД и NAS
не менялись, публикации нет.

Отдельный read-only аудит подтвердил programmatic Alembic fileConfig потерю
structured application/uvicorn logging. Минимальный патч и proof3checks лежат
в /private/tmp/pc-startup-logging.LybAwa, ещё не применены: следующая безопасная
итерация, не готовое исправление. Причина прежнего sandbox exit3 не доказана.

### Programmatic миграция сохраняет диагностику приложения

Предыдущая read-only находка теперь исправлена. `migrations/env.py` безусловно
вызывал `logging.config.fileConfig` после application configure_logging:
rootINFO→WARNING, два StructuredFormatter handlers→один genericstderr,
прежние handlers закрывались, api.main/preflight/storage.db.migrate/uvicorn
loggers disabled=True. ERROR и сообщение готовности исчезали; это общая потеря
диагностики local startup, не доказанная причина sandbox exit3.

Programmatic caller задаёт Config.attributes.configure_logger=False; env
проверяет этот атрибут с defaultTrue. СамостоятельныйCLI использует прежнийINI.
Не добавлены adapter/reconfigure-after-upgrade/migrationversion или dependencies.
Одно disable_existing_loggers=False было бы недостаточно: root handlers всё
равно заменяются. Config.attributes — штатный механизм передачи env-контекста:
https://alembic.sqlalchemy.org/en/latest/api/config.html.

Два subprocess regression cases проверяют настоящий изменённый source:
cold/repeat upgrade сохраняют точные handler/filter identities, rootlevel,
enabled application/uvicorn loggers и structured request ID; CLI cold/repeat
сохраняет genericstderr и единственную цепочку, installedhead совпадает.
Вместе с существующим execution migration сохранением:3checks pass2.97s.
State /private/tmp/pc-migration-logging-integrated.fHs7yf, APP_ENV=test.
Ruff/diff-check, SQLite migrationdrift и requirements export-check чистые.
DeprecationWarning отсутствующего Alembic path_separator отмечен, не выдан
за failure и не скрыт ослаблением assertions.

Отдельный настоящий local API на18867 и новой SQLite
/private/tmp/pc-logging-api.of2k6Q выполнил22миграции: startup MinIO warning
сохранён structuredJSON в app.log, Uvicorn Application startup complete и
running видны, dashboard/health HTTP200. Реальных NAS/Docker/worker нет;
ожидаемая недоступность loopback objectstore остаётся недоступностью, не успехом.
OwnPID50462 проверен поargv, завершёнTERM; session32270 подтвердил shutdown,
артефакты/SQLite сохранены. Пользовательский просмотр8002 и его БД не тронуты:
он не перезапускался и использует уже инициализированный logging до следующего
запуска. Это не nativePG/Windows/полный operator-stack тест или accuracy proof.

Остаток: семантическая physical-source freshness вместо всей revision,
подтверждённый as-run recipe и held-out cycle gate; нативная operator/update
приёмка и полноразмерный corpus остаются прежними неподтверждёнными задачами.
Необязательный LightGBM действительно отсутствует (find_spec=False), fallback
logreg в прежнем phase3 тесте не является новым math regression; пакет не скачан.

### Отсутствие журнала пауз не является измеренными нулевыми паузами

Pure time_accounting уже различал pauses=None и []; producer всегда передавал
результат pause_intervals(events), включая пустой список при time-only импорте.
В overview/analysis_snapshot/HTTP появлялись ложные explicit/confirmed/open0.
Теперь emptystream допускается только при наличии пригодных всех доступных
main источников: parsed/nonempty/timestamped и qualityok. При отсутствии,
пустом/неразобранном/недатированном/partial main поток неизвестен. Найденные
явные интервалы сохраняют прежнюю observed-семантику и измеренный подытог.
Новых полей/алгоритмов/порогов нет; полнота выгрузки не выводится из parserok.

ANALYSIS0.5.11/log-insights1.0.11: schema/version admission остаётся строгим,
старые JSON/registry не переименованы. Current numerical producers/fixtures
используют новую общую версию, старые snapshots не обучают новую поправку;
это консервативная граница, не восстановление исторического as-run сегодняшними
параметрами. Parser versions/formулы burn/pour/cycle не менялись, APP1.8.0.

Native03June источники267712B, копии проверены независимымSHA:
time53184ea7c356d2cca4f7ea5baded172cd96e4a07b77f1882255c5f3911dfe7de,
maincc3653345dc7d252c1835666c3d5370c0c9a4acaf61ec2238f56e59f8d50f5cb.
Actual parser→prepare_session_analysis→overview/measuredsnapshot→833 tagged
LayerSnapshots→HTTP GET4×200 подтверждает timeonly(None,None,None), healthy
main(0,0,0), explicit10s(10,10,0), unresolved(None,0,1). Измеренное время всех
записанных попыток30435.953s сохраняется во всех случаях, нормальное полное
время/отсутствующие слои не выдуманы. Saved/reference projections согласованы.
DTOs записаны в новую временнуюSQLite для проверки чтения, importworker не
запускался; complete timingmanifest означает согласованность доступных строк,
не полное физическое покрытие печати. Код не подменялся memoryproposal.
PipelineSHAf9bf9fb80b32b4e3c3598cbd5dffc06c8f5163999a60ac45169961aee04e3e58.
Артефакты /private/tmp/pc-log-accounting-integrated.tLVs70/result.json,
verify_integrated.py/verification.sqlite. Исходники и sourceSHA не менялись.

7 regressionfailures/4controls до; новые11 и общий scoped119 checks pass16.95s
в /private/tmp/pc-pause-source-integrated.Kw1RcB.39 дополнительных ML/version
admission checks pass1.75s в /private/tmp/pc-pause-version-guards.70GnoG.
Наборы не являются полным suite/physicalaccuracy proof. ScopedRuff/diffcheck
чистые. Положительные закрытые интервалы partial источника по-прежнему являются
observed итогом известных интервалов, не гарантией отсутствия других пауз.
Рабочие БД/NAS/публикация/открытый preview8002 не менялись/не перезапускались.

### Подтверждённый незакрытый баг межповторной полноты импорта

Native browser uploadmain+time→confirm→consumer: после архивирования временно
недоступный time даёт правильный postpone первой попытки. Следующая stability
заменяет full file_snapshot уменьшенным набором, третья попытка публикуетdone,
manifest/source_rows толькоmain, no_time_log и0 измеренных слоёв. Файл сохранялся
в собственном temporary path, originals/операторская БД не тронуты. Полный набор,
восстановление файла, добавление и дозапись являются успешными controls.

Prototypeunion прежних/currentимён предотвращает ложныйdone и сохраняет bounded
stability budget, но не является безопасным fix: исчезновение .DS_Store и
profile-excludedStateFlow штатно проходит в baseline, а prototype ломает оба.
Даже145 adjacent pass не перекрывают эти две реальных регрессии. Патч намеренно
НЕ применён, новый список частных исключений или browser-prefix обход не добавлен.

Raw/intake snapshot и архив владеют physical names; после expandZIP manifest
содержит logical names. Сравнивать эти пространства напрямую нельзя. Existing
IngestionService._should_skip владеет общей metadata/profile/name/size policy;
size ветка требует verifiable size, а selected profile приходит в confirm/execute.
Следующая итерация — согласовать эту одну policy со stability для missing
meaningful input, сохранив широкую identity, дозапись/добавления и bounded retries.
Не замораживать содержимое и не выдавать unsafeunion за защиту всех импортов.
Доказательство: /private/tmp/pc-import-retry-audit.TEmKP8/audit_retry.py,
test_retry_source_completeness.py, POLICY_REVIEW.md. proposal.patch — unsafe draft.
Отдельный read-side кандидат _load_sessions пока лишь inspected: загружает весь
sessioncontext ради stats/health; оптимизация и измерения ещё не выполнены.

### Одна source policy и сохранение принятого набора между повторами

Подтверждённый выше баг исправлен для сохранённого accepted/stability снимка.
`source_exclusion_reason` извлечён из прежнего private метода в том же
ingestion.py, без alias/нового слоя. Ingestion, missing-manifest gate и stability
используют одну selected-profile metadata/name/size policy. Missing значимые
physical пути остаются в file_snapshot между retries, пока не восстановлены;
исключённые по имени/известному размеру не становятся обязательными. Текущие
размеры/mtime присутствующих обновляются, добавления и дозапись разрешены.
Широкие snapshot_source, регистрационная identity и raw archival не фильтруются;
physical ZIP и expanded logical namespace не объединяются.

Первый extraction переставил stat перед name policy. Independent review
воспроизвёл новую регрессию: исчезнувшие между is_file/stat metadata/profile
exclusions ошибочно становились source_inspection_error и откладывали весь
batch. До завершения итерации порядок восстановлен: чистая name policy с
unknown size, затем stat только для не исключённых, тот же stat у inspection.
Нового списка исключений или обхода конкретной папки нет. Два native случая
до/после подтверждены и добавлены в существующий completeness test module.

Actual browser upload/folder registration → confirm → штатный one-shot consumer
в новой SQLite проверяет missing main/time/ZIP: bounded failed, без session/
report/source/layer публикации, owner/confirmation/fencing сохранены. Restore,
new file, append, metadata/profile-name/size exclusions, broad raw archive и
folder ZIP physical/logical пути проходят. Размеры bool/float/string/unknown
не разрешают size-exclusion; точная граница существующего лимита сохранена.
Сохранённые timing manifest/report одного поколения согласованы. Здесь это
полнота доступных строк технического fixture, не полный физический цикл печати.

19 новых checks прошли на настоящем source. Отдельный baseline реконструировал
только прежнее тело check_source_stability в памяти поверх остальных текущих
компонентов:3 failures (main/time/ZIP postponed→postponed→done),9 controls pass.
Это не byte-exact исторический checkout; исходный main+time before остаётся
в предыдущем audit. Root после adoption и восстановления порядка:94 checks
pass21.21s на /private/tmp/pc-import-policy-integrated.xnS2xY/test.sqlite.
Scoped Ruff/diff-check чистые. Source/protocol/test artifacts:
/private/tmp/pc-import-retry-audit.TEmKP8/FINAL_PROOF.md и
/private/tmp/pc-source-policy-review.sMxtEW/FINDINGS.md.

Незакрытая общая граница: timing source впервые появляется ПОСЛЕ stability,
checksum/raw archival успевают принять main+time, затем time недоступен.
Первая попытка правильно отказана, но worker failure path перечитывает stored
job и сохраняет только error/retry, теряя новое detached evidence. Native
upload→confirm→consumer на отдельной late.sqlite доказал следующий main-only
done/no_time_log/0 layers. Это существующий checkpoint gap, не новая регрессия;
текущая защита принятого снимка не объявляется универсальной. Следующий шаг —
один fenced physical-source checkpoint до дальнейшей обработки, не слепое
смешение snapshot и expanded manifest либо сохранение всего failed result.
Proof: /private/tmp/pc-source-policy-review.sMxtEW/proof_late_archived_source.py.

ANALYSIS_VERSION остаётся0.5.11: формулы, schema и semantics признаков не менялись.
Исторические JSON/поправки/рабочая БД/NAS не переписаны. Persistent worker/Docker
не запускались, открытый preview8002 не перезапускался, новые процессы проверки
не оставлены. Native PostgreSQL/NAS и физическая точность этим не подтверждены;
публикации и релиза нет.

### Durable состав до IO, один путь выбора исходников — 14 октября

Последняя checkpoint-находка закрыта для принятого physical состава. Coordinator
больше не разрешает execute без настоящего claim в APP_ENV=test. Четыре прежних
direct-execute/preparation фикстуры переведены на сохранение снимка и реальный
claim; standalone raw-archive/expansion primitives не получили SQL или test-only
checkpoint bypass. Второй счётчик/проверка стабильности не добавлены.

Confirm один раз собирает ordered source paths; его прежняя stability policy
остаётся единственным bounded local-file defer. Перед hash/NAS/expand узкая
checkpoint-функция записывает только file_snapshot (штатный ORM onupdate меняет
updated_at), не status/lease/generation/audit/card intent. Существующий ImportFence
проверяется после row lock и после flush, затем commit/close. Flush обязателен:
populate_existing/autoflush=False иначе мог стереть ожидающую JSON запись.
Detached job содержит тот же снимок и не возвращает прежний состав при done.

Hash, raw archive, ZIP decision и expansion получают эту же physical выборку,
не новый walker. Файл, пришедший позже, не принимается архивированием незаметно:
в non-ZIP пути unexpected checksum требует retry, следующая stability допускает
добавление своим прежним бюджетом. Explicit выбранный missing input не пропускается
expander молча; name/known-size intentional exclusions и symlink rejection
сохранены. Physical ZIP keys и expanded logical manifest не смешиваются.

Intermediate helper выявил новую ошибку обработки lost ACK: after_commit callback
оставляет transaction COMMITTED, явный rollback подменял исходную ошибку
InvalidRequestError и приводил к failed. Helper упрощён: context close владеет
rollback незавершённой работы, translation сохраняет исходную причину. Native
SQLAlchemy simulation подтверждает committed membership, postponed и отсутствие
NAS IO после сообщённого commit failure. Это не воспроизведённая потеря реального
NAS соединения. FileNotFound/Permission retry ограничен подготовкой до yield;
ошибка самого анализа/отчёта после yield остаётся его ошибкой, не вечным file retry.

17 native cases pass4.00s: late time/folder ZIP, loss/restore/current SHA growth,
bounded missing, simulated crash/reclaim, cached guard против owner/generation/
expiry, late expiry, rejected commit/lost ACK и no-fence coordinator. Проверочный
connection counter пуст во всех archive/report IO. Контролируемые clock writes
не являются измерением физической длительности. Before drafts12/14 failures
с2 controls были дополнительно исправлены/адаптированы к held-list наблюдению:
это не идентичный17-case differential. Original late-source before сохранён.
Артефакты: /private/tmp/pc-source-checkpoint-review.C5DD8R/README.md и XML/SQLite.

Root121 scoped checks pass21.65s до последнего commit-error упрощения; после
adoption/current source129 checks pass6.45s в
/private/tmp/pc-checkpoint-final.b8u46w/test.sqlite. Отдельные3 preparation/error
scope cases pass0.71s в /private/tmp/pc-preparation-error-scope.EETU0v; наборы
пересекаются, не суммируются как уникальные. Scoped Ruff/diff-check чистые.
Source SHA: import_jobs6778f52ece714dfc266a5bc58ca8886f72c21b397000abe070d8a0602ed68011,
persistence73f6145440367e4d7263c60e100c0f8788c14694373a966178653f79d238d342,
log_archives12be067c58154e0c56f38421d68c8364ec93094e0a66a5ffeeddd5f969e0c6ab.
Worker/failure handler не менялся; записывать весь failed detached job не пришлось.

Новая подтверждённая граница содержания, пока НЕ исправленная: после успешного
verified archive ZIP212B/H9693e4a0 с timing62B/2rows был заменён ZIP244B/H60e789b4
с timing94B/3rows до expansion. Native consumer публикует done/3 LayerSnapshots,
SourceFile checksum1f2954a3 от нового member, но URI указывает на прежний212B raw
container с member SHA2ef6f0da. Третий слой из ссылки на исходник не воспроизводится.
Это обычная видимая замена размера, не искусственное сохранение size/mtime.
Expected rejection1fail/unchanged control1pass2.17s: source не менялся, полный17
набор не повторялся. Полные SHA, bytes и XML:
/private/tmp/pc-source-checkpoint-review.C5DD8R/ZIP_BINDING_FINDING.md,
test_zip_archive_binding.py/zip-binding.sqlite. Следующий приоритет — archive
и expansion одних проверенных байтов, не частичный stat guard или fitted поправка.
Checkpoint подтверждает состав, не закрывает этот content-binding gap.

ANALYSIS остаётся0.5.11; формулы, schema, historical JSON и модели не переписаны.
Все данные и one-shot checks временные; working DB/NAS/Docker/операторские workers
и preview8002 не запускались/не перезапускались. Публикации нет. Реальный NAS/PG,
native Windows и физическая accuracy/QC не доказаны, импорт полностью готовым
к релизу не объявлен. Временных постоянных проверочных процессов не оставлено.

### Один raw byte-view вместо повторного чтения внешнего пути — 14 октября

Подтверждённый ZIP212→244B/2→3rows дефект исправлен общей подготовкой источников,
не stat-only проверкой конкретного ZIP. В существующем import_jobs.py
_capture_import_sources копирует выбранные physical файлы по1MiB и одновременно
считает SHA. Один owned TemporaryDirectory содержит source/expanded; архивирование,
распаковка, ingestion, полный telemetry и mirror работают с этой копией до конца
анализа. Original файлы не меняются; cleanup очищает только собственное дерево.
CRC, логические/native member имена и лимиты прежнего expander не переписаны.
Перед каждым файлом проверяется свободное место+256MiB, по блокам lease;
размер открытого файла ограничивает copy и обнаруживает рост/усечение. Точный
mtime_ns переносится через fstat/utime. Это не atomic filesystem snapshot при
same-size concurrent overwrite, а согласованность принятого byte-view и raw URI.

Non-ZIP SourceFile.original_path и runtime files.path/classification.path сохраняют
исходное местоположение; live computational DTO остаётся staged до конца анализа.
Hash-copy заменил прежний отдельный checksum scan, не добавил второй обход/кеш.
Missing meaningful original ДО capture по-прежнему блокирует partial publish и
сохраняется между retries. Удаление/замена ПОСЛЕ успешного capture/archive уже не
уничтожает принятые байты: публикуется исходное поколение. Позднее добавление после
единственной stability проверки не архивируется/парсится в этом поколении;
timing publication остаётся no_time_log, а не выдуманным полным измерением.
Новый файл требует следующего приёма/повтора, автоматический отдельный intake
этими двумя controls не доказан. Неполный/недоступный staged набор запрещён.

Прежние missing/retry fixtures переведены на реальный checkpoint commit/close
до capture; staged-read/missing injection на настоящий computational path.
Source deletion после archive теперь отдельный success control. Это изменение
проверяемой границы, не identical before/after17-case differential. Строгая6-case
API→confirm→one-shot consumer матрица требует done ORIGINAL generation для ZIP,
loose-in-folder+ZIP и прямого time-лога с unchanged/replacement controls:
member SHA/size/URI,2LayerSnapshots и runtime projection совпадают. Старый ZIP
baseline1fail/1control сохранён; loose-in-ZIP до фикса native не прогонялся.
Артефакты: /private/tmp/pc-source-checkpoint-review.C5DD8R/BYTE_GENERATION_FINAL.md;
две staged integration фикстуры — /private/tmp/pc-import-byte-tests.hciQjE/README.md.

Root итог после adoption:227 checks pass29.28s,1 существующее httpx deprecation
warning, /private/tmp/pc-byte-final.d3K9dm/final.xml. Отдельные44/122/19/6/36
прогоны пересекаются, не суммируются как уникальные. Scoped Ruff и diff-check
чистые. Capture controls отдельно проверяют nested names/SHA/mtime, рост/усечение
и отказ при нехватке места; post-yield ошибки остаются исходными ошибками анализа.
Source SHA: import_jobs7c5d6486a0335225a8ee105a3ef8fef69ae24d25e5922263b13a703bfbfbc031;
persistence4e2d008a015b6597021e46725a818b7bc01af47088b5d8eb58b36835bccd2a31;
expander12be067c и worker a3b5841c не менялись. ANALYSIS остаётся0.5.11.

Цена исправления измерена честно: actual64MiB warm median5repeats SHA30.171ms,
copy+SHA40.084ms (+9.912ms/1.329x). First pass после buffered generation не cold;
cold/NAS/full import/fsync throughput не измерены. Дополнительное raw дерево
требует места вместе с expanded; это correctness fix, не обещание ускорения.
Generated benchmark binaries удалены своим TemporaryDirectory; script/JSON
остались /private/tmp/pc-import-retry-audit.TEmKP8/CAPTURE_REVIEW_AND_BENCHMARK.md.
Все проверки временные, без operator DB/NAS/Docker/рабочих consumers/preview restart,
без GitHub публикации. Full corpus, PG concurrency, remote content readback,
native Windows и физическая accuracy/QC по-прежнему не доказаны.

Следующий прямой приоритет владельца: к утру простой update с GitHub для Mac/Win
без лишних демонов. Official [electron-builder auto update](https://www.electron.build/v26/docs/features/auto-update/)
поддерживает GitHub Releases и требует Mac подпись; [Electron](https://www.electronjs.org/docs/latest/api/auto-updater/)
также фиксирует эту platform boundary. Текущая публичная [страница релизов](https://github.com/ArtemIvanchenko/Printers-companion/releases)
показывает1.7.0, не restored baseline1.8.0. Existing unsigned check вообще не
запрашивает GitHub; workflow всегда development/publish-never и не выгружает
latest*.yml. Нужен реальный read-only release/native asset check без unsigned
auto-install, затем coherent build/feed путь. Drain/owner-local/journal/digests
не переписываются, новые службы и платные действия не добавляются.

### GitHub update без службы — 14 октября, реальная проверка тестового пакета

Unsigned no-op заменён настоящим on-demand `check-release` существующего frozen
host. Он до PackagedUpdater/настроек проверяет allowlisted bundle, публичный
stable tag/full SHA и exact uploaded DMG/EXE нужной платформы. Test asset
допускается только с SHA-prefix тега и существующим fingerprint-форматом;
duplicate/foreign/source ZIP не установщик. Renderer не задаёт URL: privileged
main frame открывает выбранную ссылку только на HTTPS fixed GitHub repository.
Manual action не скачивает/не запускает файл, не делает prepare/quit/install.
Подписанный штатный electron-updater и его restart gate остались отдельными.

Check до NAS настройки не создаёт operator root, не читает секреты/binding,
не запускает Docker/SQL/MinIO. Read-only child ограничен120s, HTTP15s и2MiB;
late progress не возвращает busy после завершения. State.busy исключает native
selector/manual-open конкуренцию. UI различает updateReady и manual installer;
dirty/unknown/conflict/ahead и pending backend не выдаются за полный успех.
Новых daemon, сервисов, auto-run или периодического updater poller нет.

Обнаружена общая переносимая ошибка TLS: build Python/OpenSSL использует
Homebrew CA path, которого нет на операторском Mac. SSLContext сохраняет
системное доверие и добавляет certifi CA через обычный импорт+штатный PyInstaller
hook; certifi уже имеется в проекте транзитивно и теперь явно pinned в host
build requirements2026.7.22. Verify/check_hostname не отключены. Изолированный
frozen probe с отсутствующими SSL_CERT_FILE/DIR:0→121CA, PEM240216B.
Причина, SSL references и артефакт — /private/tmp/pc-frozen-tls-review.MdgPW8/TLS_REVIEW.md.

Root scoped Python137 pass1.94s на отдельной SQLite/outbox/identity; Node90
pass153.37ms. Peer44/82 и33/126 пересекаются, не складываются как уникальные.
Ruff/diff-check чистые. Existing npm lock восстановлен npm ci, без audit fix
или смены библиотек: runtime audit0 advisory, dev/build9 (8moderate/1high)
остаются отдельным долгом, не full security approval.

Реальная сборка ARM Mac: Python3.12.13/PyInstaller6.22.3, Electron44.5.1,
builder26.15.3/updater6.8.9. Host20MiB содержит публичный PEM, без аналитических
NumPy/SQLAlchemy/Torch. Native DMG/ZIP в desktop/dist:
Printer-Companion-1.8.0-test.587b16b53331.4f875a62-mac-arm64.dmg и.zip;
DMG SHA256 f6e06b7811ad91b8ca081825535a1a379a687a084bca28fb9d665ebe9080f0bc;
ZIP8b470effa421840d6c67f505e8630a121ae7338985c5d717f62a82edbf3f19cf.
Unsigned, manifest dirty/images={}, NOT новый полнофункциональный compute release.
App: /private/var/folders/6j/xqrh_s190njbgwkbpdj3l8sw0000gn/T/pc-desktop-build-TtldVY/mac-arm64/Printer’s Companion Test.app.
Frozen host offline protocol smoke и настоящий packaged --smoke прошли:
mainframe preload/state, unsigned install refusal, собственный профиль без
операторской привязки. Screenshot /private/tmp/pc-github-native-smoke.WZX6sU/desktop.png.

Тот же native пакет проверен настоящим кликом DOM «Проверить обновления»:
mainframe IPC→bundled frozen child→живой GitHub HTTPS без build-machine CA.
Ответlatest1.7.0/6996542e222fb6e3abb19d5a5911ba49820fc486, ahead=true,
installer=null; busy снят, downgrade не предлагается, operator root не создан.
Evidence: /private/var/folders/6j/xqrh_s190njbgwkbpdj3l8sw0000gn/T/pc-native-github-click-cxjxoa/result.json иgithub-check.png.
Внешний временный inspector удержал только собственное smoke окно; несколько
начальных harness-ошибок (opaque paused URL/double resume/ожидание setup после
negative unsigned install) исправлены в tmp, не workaround в production.
Phase:error в evidence — предыдущая штатная negative install smoke, не GitHub
failure и не normal startup доказательство. Собственное окно/inspector/child
завершились. Независимый byte review5/5 JS +полный runtime fingerprint совпал
с .app: /private/tmp/pc-github-update-review.V2RVuj/REPORT.md.

Отдельный реальный /usr/bin/time -l frozen HTTPS check:1.27s, peakRSS38,502,400B
(36.7MiB), peak footprint21,775,032B. Это одно наблюдение короткого child, НЕ
память всего Electron/Docker и не обещание скорости любой сети. Persistent
updater process не создаётся. Публичный release1.8.0/compatible installer ещё
не существует; main/source version не заменяет такую публикацию.

Signed A→B/Developer ID/notarization/Windows certificate, Windows native данной
правки, рабочий backend/NAS/full deploy не доказаны. Working DB/NAS/containers/
sources/preview8002 не трогались; GitHub branches/tags/releases не публиковались.
Следующий safe шаг — coherent opt-in signed build+combined Mac feed, оставив
--publish never и unsigned defaults; реальные сертификаты/публикация/допуск
останутся внешней границей, без вопросов спящему владельцу.

### Opt-in signed CI/feed подготовлен — 14 октября

Read-only upstream review подтвердил: pinned builder26.15.3 генерирует metadata
при publish-never; этот флаг не разрешает upload. PR/default по-прежнемуtest,
reusable signed_release=false. Только explicit vars.COMPANION_SIGNED_RELEASE
передаёт signed opt-in от tag release. Root дополнительно проверяет refs/tags/
vVERSION до доступа package step к signing env. Existing builder требует
signing/notarization + pinnedimages и не делает unsigned fallback. Secrets
передаются только соответствующему package step, значения не читались/не
использовались. Ни variable, ни secrets на GitHub не создавались.

В финальном release job collect-release.cjs проверяет3native manifests (full
SHA/version/clean/enabled/resources/images),5exact installers и реальные
streaming SHA512/size противfeeds. Послеcopy installer bytes снова проверяются.
MacARM и Intel создают одинаковоеlatest-mac.yml: файлы обоих jobs объединяются
в4DMG/ZIP records, Windows latest.yml отдельно. Legitimate record metadata,
включаяblockMapSize, сохраняется; duplicate/foreign path/symlink/занятыйoutput
отклоняются. Platform fingerprints могут различаться, manifest/filename должны
совпасть. Unsigneddefault ниfeed, ниblockmaps публично не выгружает.
Используется locked js-yaml4.3.2, уже dependency electron-updater6.8.9;
финальный CI устанавливает только existing runtime deps (--omit=dev,
--ignore-scripts). Collector — одноразовый build-time, не operator процесс.

Root adopted five-file stage и выполнил108 Node cases pass3.42s (18новых real
filesystem collector controls,90existing), YAML parses/JS syntax/diff-clean.
Peer18stage cases пересекаются. Файлыfixtures настоящие, но маленькие и не
подписаны; режимsigned manifest НЕ signature proof. YAML parse не Actions
service validation, actionlint отсутствует. НиremoteCI, ниsigning/publish не
запускались. Stage/report: /private/tmp/pc-signed-build-stage.TysIwI/README.md.
Первый root summary apply закончился context mismatch AGENTS после применения
других четырёх файлов; фактические изменения проверены и только оставшийся
AGENTS дописан отдельным patch. Никакие существующие изменения не откатывались.

Developer/оператор инструкции обновлены, но existing test→public appId/profile
переход, actual certificates/notarization, signed native A→B на трёх targets и
точный backend readiness остаются отдельным допуском. Публичный1.8.0 не создан,
локальныйnativepackage по-прежнему dirty UI/protocol test сimages={}. Drain,
NAS-only storage, owner-local и source/data preservation не переписаны.
Постоянных проверочных процессов не оставлено; existing preview8002 не менялся.

### Проверка SDK и physical/parse manifest identity — 14 октября

Read-only probe проверил настоящий builder 26.15.3 metadata writer → current
collector → updater 6.8.9 parseUpdateInfo/resolveFiles. ARM/Intel выбирают свои
DMG/ZIP; NSIS выбирает x64 EXE, сохраняя blockMapSize и isAdminRightsRequired:false.
Исполнение остановлено перед executeDownload. minimumSystemVersion сохранён;
DMG-only/no-SHA controls отклоняются. js-yaml 4.3.2 подтверждён как уже имеющаяся
production dependency. Три целевых пути проверены одним probe, без подписи,
скачивания или установки Windows. Evidence: /private/tmp/pc-feed-sdk-probe.l8VSBb/REPORT.md.

Подтверждён общий процессный сбой: после expansion координатор заменял physical
ImportJob.checksum_manifest на logical ZIP members. Browser receipt и copy
deduplication сравнивают physical upload SHA. Поэтому завершённый ZIP с receipt,
оставшейся после ошибки cleanup, ошибочно получал needs_attention при тех же
owner/card/job/path, хотя анализ и слои уже были done. Loose control проходил.
Это не сценарий сохранения размера/mtime или подгонка под известную печать.

Исправление разделяет существующие значения: job сохраняет physical capture
manifest; тот же context helper возвращает отдельный local parse_manifest для
completeness и независимой ingestion SHA-проверки. Новых schema/DTO fields,
cache или recovery fallback нет; _matches_job не ослаблен. Logical SourceFile
SHA/member/raw URI и checksum audit count прежние. JSON-тип сохранён; старые
записи не переписаны, потерянная physical identity из members не угадывается.
ANALYSIS 0.5.11 / log-insights 1.0.11 прежние: формулы и измерения не менялись.

Native fixture исполняет upload → confirm → one-shot consumer → parsers/
checkpoint/publication → recovery. Только sidecar cleanup контролируемо отказал
после commit; NAS boundary заменена существующим verified in-memory store.
До: ZIP 1 fail / loose 1 pass, 10.96 s, /private/tmp/pc-receipt-before.aZxZrf/results.xml.
После: 134 scoped checks pass, 67.00 s / 1 existing httpx warning;
XML /private/tmp/pc-receipt-after.uwQXo3/results.xml. ZIP checks отдельно требуют
physical job SHA, logical parse SHA и отказ при изменении member; CRC, policy,
широкий raw, lease, cleanup и raw-byte-generation проверки сохранены.

Дополнительные два native controls (11.42 s) проверяют копию completed batch с
тем же именем/байтами в другом временном пути: тот же job, ровно два слоя, без
повторного анализа. Recovery сначала ищет SQL ID; повторное хеширование запрещено
тестом. Complete job DTO до/после целиком равен, receipt/исходники сохраняются.
Evidence /private/tmp/pc-receipt-copy.oMIXBf/results.xml. Эти два пересекаются со
134, не суммируются. Scoped Ruff/diff clean. Source SHA:
import_jobs e4b9286880c3f783db586f42ecbbf1e8c69f5c7275b0aafc75d7ee72f34702e6;
contracts 9301fd9ea420dad68603f7983df1f95c7f05b440c4f5ce04b569ad1095edc5b1.

Independent numerical audit не подтвердил нового math defect: per-layer max
210 s против 180 s при той же scan-сумме, correction-before-floor 300 s; retry
сохраняет 240 s диагностики / 130 s subtotal, ambiguous layer исключается до
training/comparison. Без independent normal target — unknown/error:null,
partial/mixed generation отклоняются. 73 timing/comparison и 91 scan/cycle
checks прошли; synthetic equations/admission не доказывают held-out accuracy.
Отчёты /private/tmp/pc-prediction-chain-review.wbbRxB/REPORT.md и
/private/tmp/pc-scan-fit-review.XwXFCL/REPORT.md. As-run recipe, cycle held-out
и physical freshness остаются известным долгом, не новыми findings.

Все проверки — APP_ENV=test и отдельные SQLite/outbox/identity. Working DB/NAS,
исходники/Печати.zip, Docker/операторские consumers и preview8002 не менялись.
GitHub ветки/релизы/секреты не затронуты. Проверочные процессы завершились.
Signed A→B, реальный PostgreSQL/NAS и физическая точность не доказаны.

### Общие ошибки lease и QC correction → ML — 14 октября

Heartbeat import и generic jobs снимали now до locked SELECT. Задержка SQL
могла пересечь expiry, но прежний timestamp разрешал продление той же generation.
В generic selective ORM refresh не исправлял устаревшие часы. Теперь current
time читается после locked read/refresh перед сравнением, explicit now сохранён.
Owner/generation/attempts, транзакции и worker consumers не менялись. Complete,
fail/defer и ImportFence уже читали часы после ожидания, их не переписывали.

Before actual repository: import 1 fail / 5 controls; generic locked-read и
refresh 2 fail / 2 controls. После 6 и 4 соответственно pass. Root независимо
проверил оба repository набора: 22 pass, 7.67 s; соседний peer import прогон
70 pass пересекается с ними, числа не суммируются. SQLAlchemy задерживал
целевой настоящий SELECT, не заменял алгоритм. Это не PostgreSQL блокировки.
Отчёт /private/tmp/pc-import-lease-audit.gTzdfH/REPORT.md.

QC final rows уже имели append-only supersedes pointer, но repository, report,
risk и ML выбирали последнее timestamp/UUID. В распределённой установке
acknowledged correction с более ранними часами сохранялась, затем терялась
из выборки; valid continuation получала 409, ветвление от старой головы — 200.
Root настоящий HTTP→strict QC→commit→prepare baseline: 3 fail / 1 monotonic
control, 13.98 s. Owner-local jobs, immutable rows, revision и snapshot были
настоящими; контролировались только серверные наблюдения UTC, не payload.

Исправление использует одну полную supersedes-цепочку в существующем quality
модуле. Один корень, одна голова и полное покрытие строк; fork/disconnected
история остаётся доступной, не становится guessed ground truth. Сначала
выбирается целая карточка, потом её session; timestamp не зажимается и не
переписывается. Repository write/read, report/risk/ML/for-session и два UI
потребителя используют один смысл. Старый singleton presentation без флага
не допускается к ML; новые поля, схемы и compatibility пути не созданы.
Native HTTP clock after: 4 pass, 11.12 s. Подробности и исходные proof:
/private/tmp/pc-qc-chain-audit.UnxoqP/REPORT.md.

Независимый serial сценарий обнаружил ещё одну потерю исправленного итога:
20 текущих labelled prints → shadow с 9 rejected → accepted→rejected correction
→ queued worker done/wait, но прежний кандидат и fingerprint сохранялись.
Только numerical train return был явным deterministic artifact double;
QC service, SQL, jobs, registry и worker исполнялись настоящими.

В существующем calculate_retraining теперь сверяется текущая subset исходных
training_session_ids с сохранённым fingerprint в исходном порядке. Изменение
label/outcome/time/features или исчезновение допустимой строки отвергает shadow
до future evaluation и разрешает прежний fresh-training путь. Новые future rows
не перезапускают окно проверки. Старый model row/artifact сохраняется rejected,
новый остаётся shadow, не active. Staged 25 pass (18 existing + 7 new) относятся
к proposal; независимый текущий checkout прогон указан отдельно ниже.
Stage /private/tmp/pc-shadow-training-guard.AqJbll/README.md.

Независимый root прогон текущего checkout без overlay: 90 pass, 17.65 s,
/private/tmp/pc-qc-final.8niBUm/results.xml; native serial correction входит
в этот набор. Первоначальные 86 pass / 4 fail относились к неполному canonical
snapshot новой fixture, не production bug: добавлены настоящие measured keys,
guard не ослаблен. Node quality-submit/card-lifecycle: 58 pass, 0.896 s,
включая настоящие JS handlers, цепочку correction и сохранение unsaved draft.
Это VM/HTTP-проверки, не native browser click или full operator deployment.
Scoped Ruff и git diff --check проходят; версии и OpenAPI поля прежние.

Разграничение scope сохранено: QC физической карточки не исчезает при правке
связи с журналами; quality overview считает подтверждённые карточки, а не
единственные ML session labels. Два результата карточек по общим логам могут
оставаться известными оператору и одновременно быть неоднозначными для ML.
API-only pre-link fallback из текущей SQL связи сохранён, но не распространён
на strict ML без отдельного подтверждения. Это известное различие допуска,
не подтверждение всей цепочки pre-log QC → training; её аудит остаётся отдельно.

Confirmed prepare→CPU→publish freshness остаётся отдельным долгом: correction
во время CPU может опередить публикацию. Serial invalidation исправляет следующий
queued расчёт, но не отменяет эту гонку. Для её устранения нужен согласованный
короткий lock protocol QC/feature/model writers и повторный lease fence; простой
read-only hash перед commit без writer locks недостаточен. Ни held-out ML
превосходство, ни native PostgreSQL concurrency этими проверками не доказаны.

Все проверки изолированы APP_ENV=test/temp SQLite/outbox/identity. Working DB,
NAS, оригиналы, Docker, preview8002 и GitHub не менялись. Версии аналитики и
численные признаки прежние, сохранённые snapshots/artifacts не переименованы.

Финальная scope-проверка после bounded session SQL и per-card overview:
root 40 pass, 17.80 s, /private/tmp/pc-qc-projection.NfwDn2/results.xml;
root quality-submit 7 pass, 0.409 s, включая две valid карточки с общей сессией
и valid B рядом с неоднозначной A. Предыдущие 90/58 проверки пересекаются с
ними; это не сумма новых тестов и не полный suite. Полный template включён:
устаревшие ожидания двух цветов исправлены на уже восстановленные пользователем
#201e1d/#c67139, CSS не менялся. Peer 130 scoped / 21 scope / 19 template и
12 Node проверки также пересекаются; /private/tmp/pc-qc-chain-audit.UnxoqP/IMPLEMENTATION.md
содержит команды, финальные SHA и границы. Собственные проверочные команды
вернули exit 0; постоянные серверы/worker не запускались. Дополнительный pgrep
недоступен: «sysmond service not found / Cannot get process list»; системную
инвентаризацию этим не подтверждаем. Операторский preview не менялся.
Долг publication-lock protocol
остаётся следующим подтверждённым пунктом, а не объявленным исправлением.

## 14 октября — завершён атомарный prepare→publish fence модели

Прямой запрос владельца: закрыть подтверждённую гонку, проверить и отправить
на GitHub. Автономная heartbeat-задача остановлена (PAUSED), новых субагентов
нет. Рабочий preview, операторские БД/NAS, оригиналы и Docker не менялись.

Причина: job complete выполнялся до registry SQL, а исходная lease не доказывала
свежесть QC/признаков или модели-соперника после локального обучения. Коррекция
QC во время CPU могла зарегистрировать старый кандидат; ожидание registry/job
lock могло истратить lease; конкурирующий ПК мог связать чужой model ID со
старым fingerprint задания.

Исправление без новых алгоритмов/сервисов/кэшей: отдельный короткий publisher
берёт lifecycle advisory guard до registry row locks, затем SHARE NOWAIT на
sessions/quality_outcomes. Это блокирует DML и новые исходы до commit, но не
обычные SELECT. В новой транзакции сверяются полный training fingerprint и
raw active/shadow IDs, включая устаревший artifact. Registry flush выполняется
до финального JobsRepository.complete; актуальная lease/generation проверяется
после ожиданий. Отказ бросает исключение и откатывает и кандидат, и promotion,
и job result. SQLite использует BEGIN IMMEDIATE. Изменение входов/55P03/SQLite
BUSY переносит задачу с прежним monotonic backoff, не расходуя error budget;
обычные SQL/схемные ошибки не выдаются за NAS outage. Все численные стадии
остаются вне SQL. Shared defer_infrastructure переименован в defer_retry вместе
со всеми consumers, без дополнительного compatibility alias.

Root итог: 91 pass, 5.16 s, /private/tmp/pc-model-checks.IRyKBj/results.xml.
Публикация проверена независимо от остальных dirty изменений: отдельный
checkout от main587b16b, baseline ANALYSIS_VERSION0.5.2, 77 pass, 5.87 s,
/private/tmp/pc-pr-checks.LqtVC5/results.xml. Наборы пересекаются, это не сумма.
12 новых regressions включают QC/features/unlink/new labels во время CPU,
двух владельцев, старый active ID, lease119/121, generation, fresh deferred
recalculation и реальную SQLite writer contention. Явный numerical artifact
double не объявляется подтверждённым ML-обучением/качеством.

Отдельный настоящий PostgreSQL16.14: собственный temporary cluster только
127.0.0.1:55473, shared_buffers16MB, max_connections12, отдельная БД
pc_model_publication_test. Публикуемый checkout: 8 pass, 7.62 s,
/private/tmp/pc-model-pg.bxCUDY/release-results.xml. UPDATE обеих таблиц и первый
QC INSERT получают быстрый NOWAIT отказ; writer после сверки блокируется,
SELECT доступен; реальный job row wait + детерминированное продвижение часов
откатывают регистрацию и promotion; lifecycle→row порядок проверен независимо.
Первоначальные ошибки fixtures (неполный snapshot и пропущенные client-side
QC defaults в raw SQL) исправлены в проверке, production admission не ослаблен.
Регрессии сохранены checks/ml_publication/test_postgres.py с explicit opt-in
и жёстким isolated DB guard, отдельно от destructive shared SQLite fixture.
CI проверяет их в отдельной БД временного service, не на операторском NAS.

Полный Ruff и diff --check публикуемой ветки проходят. Математические формулы,
качество модели, физическое время, родной NAS и installer этими проверками
не подтверждены. Для публикации выбрана codex/model-publication-fencing:
только указанная защита и необходимые queue/SQL changes; остальные dirty
локальные изменения сохранены, но не объявлены опубликованными. main/теги/
релизы не меняются этим PR. Итог GitHub/CI фиксируется ниже после проверки.

GitHub публикация подтверждена: commit1ef3a7c4fc7ab7f836d0e574f3348f3cb8ca249d,
ветка codex/model-publication-fencing, PR27
https://github.com/ArtemIvanchenko/Printers-companion/pull/27. PR прикреплён к чату.
Это не merge в main: основной checkout по-прежнему main587b16b, 247 dirty paths
сохранены. Старый main CI был success; это не результат CI нового SHA.
Дополнительный native PostgreSQL прогон с заблокированными optional numerical
imports: 8 pass, 4.57 s; migrations CI использует base/dev без тяжёлого fitting.
Собственный pg_ctl stop подтвердил server stopped; временные DB/probe данные
сохранены, операторские процессы не остановлены. Heartbeat остаётся PAUSED.

GitHub Actions нового SHA: run38507666894, migrations job115668775554 success;
новый отдельный native publication step success на Python3.11/Linux/PostgreSQL16.
Это дополнительная проверка SQL-протокола на runner, не working NAS. Test и
container-smoke на момент записи ещё running; full CI не объявлен зелёным.

Финальный GitHub CI этого же SHA завершён успешно: test, migrations и
container-smoke — SUCCESS; PR27 MERGEABLE. Общий lint/test, реальный отдельный
PostgreSQL publication step, миграции/drift и packaged API smoke прошли.
https://github.com/ArtemIvanchenko/Printers-companion/actions/runs/38507666894
Smoke подтверждает revision/liveness и честную degraded readiness без NAS,
не полный рабочий stack. Main не объединялся, release/installer не выпускался.
Работа по этому прямому запросу завершена; запланированная задача остаётся
выключенной, следующие автономные итерации не начинаются.
