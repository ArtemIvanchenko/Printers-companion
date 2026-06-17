---
name: m450-print-time-project
description: SLM print-time/cost prediction — machine (LaserStudio M350, 2×YLR-500), the ~8× scan-overhead problem, validated magics+log pairs, calibration facts
metadata: 
  node_type: memory
  type: project
  originSessionId: 1c4430e4-e07a-4883-9055-dea193ea02a7
---

User is building a print-time + cost prediction algorithm for their SLM printer. The prediction code now lives IN the Printers-companion repo (`analytics/prediction/`), not a separate project. Machine confirmed 2026-06-15 from operator screenshots: ONE printer, controlled by **LaserStudio M350 ver. 2.0.6.32**, **two IPG YLR-500 lasers** (500 W each, YLR-500-1/-2). N=2 always; cross-section split ~50/50 by volume between lasers. Layers 30/60 µm per mode. See [[printers-companion]].

Data (as of 2026-06-12): logs on `/Volumes/SANDISK/Логи`, models+cost Excel on `/Volumes/SANDISK/Модели с печати` (file names have a trailing space before `.xlsx`; folder spelled «компановка»). STL viewer repo: `/Users/admin/Documents/GitHub/stl-analyzer` (React/Three.js). Notes: `/Users/admin/Documents/CODEX/OPENCODE/algorithm_print_time.md`; my full review: `algorithm_print_time_review.md` (same folder).

Key established facts:
- 23.03+27.03.2026 are the SAME print (layers 2–7016, 82.32 h, 30 µm); 06.04/11.04 logs empty. **Update 2026-06-15: «exactly one print» NO LONGER holds** — once .magics files became readable (see recipe below), many usable magics+log pairs were validated (04.06, 03.06, 19.05, …).
- Calibrated constants: t_recoat median 8953 ms (30 µm), per-layer overhead γ₀ ≈ 0.4 s; burn linear in cross-section area (tail CV 1.2%).
- Excel slice export (mean 3653 mm², not 2485 — 2485 was just slice #1) does NOT correspond to the logged print; no STL on disk matches its 210.45 mm height → geometry↔log pairing impossible until they save a slice export + print passport per print.
- Excel cost model: powder = part mass + 0.5 kg (recycling accounted), gas fixed 3500 ₽/print, filter life 48 h (user thought 30000 h — unresolved), labor not monetized, energy absent. Excel time formula has a 4× scan-length error partially compensated by ignoring recoat.
- Headline insight (PARTLY SUPERSEDED 2026-06-15): the «no slicing, T = α·V/t + …» volume model fails — see «Диагноз» below. Volume/area-from-mesh captures the theoretical minimum but misses scanning overhead (~8×).

## Машина и софт (2026-06-15, скриншоты оператора)

- Принтер один → **LaserStudio M350 ver. 2.0.6.32**, 2× IPG **YLR-500** (500 Вт). N=2 всегда, деление сечения ~50/50 по объёму.
- В софте есть кнопка **«расчёт времени»** (раздел «Параметры прожига»), НО **его оценка совершенно неправильная** (подтверждено пользователем 2026-06-15) — ИМЕННО поэтому строится своя система прогноза. НЕ использовать машинный «расчёт времени» как эталон/решение. Полезное оттуда: **время отсыпки = 10.0 с/слой** (recoat; подтверждает калибровку ~9.5 с), поля «Время задержки», «Время изготовления слоя».
- Параметры режима лежат в проекте, разделы: **ШТРИХОВКА** (шаг+скорость), **КОНТУРНЫЕ ЛИНИИ**, **ПОДДЕРЖКИ** (свои параметры). Список «Проект» хранит печати с датами как у логов (03.06/04.06 SIU RS-300, 27.05 RS320, 08.06 «8 деталей»…) — оператор открывает любую и читает точные параметры.
- Процесс (подтверждено пользователем): шаг штриховки в основном **120 мкм**; v_hatch ~1000 (сталь), до 1528 (последний режим); контуров у деталей «на заказ» пока нет (`contour_speed=0`); recoat ~10 с постоянно.
- **2026-06-17**: параметры верифицированы по РЭ M350 + реальным скриншотам LaserStudio — полный набор для алюминия (РС-300 60 мкм) зафиксирован в [[m350-machine-params]]. Параметры стали (12Х18Н10Т 32 мкм) пока не сфотографированы.

## Диагноз неточности прогноза (2026-06-15) — ГЛАВНОЕ

- Формула `scan = V/(lt·hd·v·N)` занижает реальный прожиг в **~8 раз** даже с ТОЧНЫМ объёмом деталей. Причина — НЕ поддержки, а **скважность сканирования**: формула считает непрерывный прожиг, игнорируя холостые перескоки (jump), разгон/торможение, контуры, задержки (skywriting). По литературе jump+delay часто > самого прожига. (Источники: lukeparry.uk PySLM build-time, researching.cn multi-galvanometer, ASTM STP1644.)
- **Калибровать по объёму нельзя** — эффективный темп см³/ч зависит от ФОРМЫ: 04.06 «стандарт» 6.6 см³/ч, 03.06 «качество» 12.5, 19.05 12.6 (перевёрнуто относительно скорости лазера). Время определяется ПЛОЩАДЬЮ сечения, не объёмом.
- Правильный путь — **векторный** (exposure + jump + delays + recoat); в проекте уже есть pyslm-режим. Нужны параметры сканера (jump speed, задержки) — есть в LaserStudio. Единого коэффициента-костыля быть НЕ может (перескоки ∝ числу векторов → зависят от геометрии). Машинный «расчёт времени» как решение НЕ годится — он неточен (см. выше), эталон = только факт из логов (Σburn).

## Проверенные пары magics↔лог (2026-06-15)

Сверка высоты сборки (из magics) = слои×lt (из лога) совпала точно → пары верны, масштаб magics подтверждён:
| печать | детали (magics, все копии) | высота | слои×lt | прожиг |
|---|---|---|---|---|
| 04.06 SIU стандарт | 203.5 см³ (16 инст.) | 119 мм | 1983×0.06 | 30.7 ч |
| 03.06 SIU качество | 77.1 см³ | 48 мм | 833×0.06 | 6.2 ч |
| 19.05 | 436 см³ | 148 мм | 2466×0.06 | 34.5 ч |
| 27.05 RS320 | Hvostovik 27.9 / Korpus 10.4 / Box 2.6 см³ (по 1, совпало с STL) | 85 мм | лог частичный (→28.05) | — |

Лог даёт точную площадь по слоям: burn = `Burn_End−Burn_Start` (NEW_STATS), `scanned_vol = lt·hd·v·N·Σburn_s`. Чтение `.magics` — см. [[magics-file-format]] (или проще: оператор экспортирует компоновку в STL).

## Изменения в коде этой сессии (2026-06-15, не закоммичено)

- `analytics/prediction/print_time.py`: режим "excel"/fast БОЛЬШЕ не использует приближение `4√A` (завышает на полых деталях ×2.2) → заменён на физическую формулу `A/(hd·v)+P/v_c`. `_excel_section_times` оставлена мёртвым кодом. recoat по умолчанию (None) = 9500 мс/слой.
- `/upload/stl-estimate` принимает override `hatch_distance_mm`; UI: поле «Шаг штриховки (мкм)» дефолт 120 при выборе STL. Тесты 25/25 в test_print_prediction.py.

## Калибровка на реальных данных (2026-06-16) — ИТОГ

**Источник геометрии = STL-файлы с диска, НЕ magics.** В папке печати лежат `Деталь_N.stl` (по файлу на каждую размещённую копию, с реальными XY) + `s_Деталь_N.stl` (поддержки, по копии). Декод magics **недосчитывал** (04.06: magics 203 см³ vs диск 831 см³ ×4 — пропускал инстансы). На 19.05 диск(435)=magics(436) совпало. Вывод: бери диск, magics не нужен.
- Фильтр поддержек: `s_*` БЕЗ префикса `s_s_` и без `_ex` (это дубли). `s_Деталь_N.stl` — основные.
- Поддержки негерметичны → **объём мусор, но площадь/длину сечения режет**. ВАЖНО: `polygons_full` ПАДАЕТ на открытых сетках («unable to recover polygon») → для поддержек брать только `section.length`, площадь/периметр (polygons_full) — только для тела.

**Разгадка ×8:** ≈ ×4 (неполная геометрия: считали 1 копию, а на платформе их по 10) × ≈×2 (реальная скважность сканирования). Мистики нет, оба множителя из данных.

**Калибровка `burn_слоя = a·площадь_тела + b·периметр + d·длина_поддержек + c`** (a=1/(hd·v·N)), точные сэмплы по серединам слоёв:
| режим | R² | v_тело |
|---|---|---|
| 04.06 станд (бруски) | 0.97 | ~630 мм/с |
| 03.06 качество (поддержко-ёмкая) | 0.86 | ~492 мм/с |
| 19.05 станд (тонкие трубы) | 0.83 | ~560 мм/с |
- recoat 9.2–9.3 с/слой и **толщина 60 мкм** — подтверждены по 5 печатям (опускание стола `LIR` в burn.log = 60 мкм/слой). Скорость поддержек ≠ тела (~646–950).
- **Предел регрессии:** площадная модель пасует на тонкостенных (трубы 19.05 упёрлись в 0.83) — площадь плохо описывает прожиг стенок.

**РЕШЕНИЕ (выбрано с пользователем): векторный PySLM.** `pyslm.analysis.getLayerTime` = path + jump + delays (учитывает jump speed, jump delay, point exposure delay) — уже стоит в проекте (pyslm 0.6.1). Кормить дисковой геометрией (тело+поддержки, своя build-style/скорость поддержкам), параметры сканера (jump speed, задержки) брать из LaserStudio или калибровать из логов. Регрессия выше — baseline/перекрёстная проверка. `.magics` читать не нужно (см. [[magics-file-format]]); открытой библиотеки для него нет, только платные SDK/экспорт в STL.
