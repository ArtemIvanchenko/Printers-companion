---
name: m350-machine-params
description: M350 verified machine parameters from manuals + real LaserStudio screens — ready for PUT /settings/machine
metadata: 
  node_type: memory
  type: project
  originSessionId: 0489ce00-88e4-4bf6-872a-8fb29ca03cd2
---

Параметры машины M350, извлечённые из руководств и реальных скриншотов LaserStudio (2026-06-17).

## Технические характеристики машины (РЭ M350, стр. 4–5)

- **Размеры колодца**: 350 × 350 × 390 мм → `build_area_cm2` = **1225**
- **Лазеры**: 2 × 500 Вт, иттербиевый волоконный → `laser_count` = **2**
- **Толщина слоя**: 16…92 мкм (типовые режимы: 30 и 60 мкм)
- **Макс. скорость сканирования**: 10 000 мм/с
- **Рабочий ход по Z**: 390 мм
- **Рабочий газ**: аргон или азот, расход ≤ 3 л/мин в режиме выращивания
- **Формат файлов**: STL

## Параметры пресета РС-300 СТД 60 мкм (Алюминий) — с экрана машины

Пресет «РС-300 СТД 60 мкм (поддержки в виде печенья)», скриншоты LaserStudio 2026-06-17:

| Поле machine_params | Значение | Источник |
|---|---|---|
| `hatch_speed_mm_s` | **1528** | MarkSpeed штриховки (вкладка ШТРИХОВКИ) |
| `contour_speed_mm_s` | **600** | MarkSpeed контурных линий (строки 1 и 2) |
| `hatch_distance_mm` | **0.12** | Step штриховки = 120 мкм |
| `layer_thickness_mm` | **0.06** | Пресет 60 мкм |
| `jump_speed_mm_s` | **3000** | JumpSpeed во всех секциях |
| `laser_count` | **2** | Технические характеристики |
| `build_area_cm2` | **1225** | 350×350 мм |
| `material_densities` | `{"aluminum": 2.7}` | Справочник материалов |

### Дополнительно из того же экрана

- Поддержки: MarkSpeed=2000, Мощность=210, JumpSpeed=3000
- Тепловые мосты: MarkSpeed=1600, Мощность=250, Defocus=5
- Тип штриховки: Полосы (Stripe length 10 мм, Merge 2 мм, Overlapping 10 мкм)
- Угол штриховки: 67°, смена угла: Изменяется

## Параметры пресета стальных режимов (НЕ ЗАПОЛНЕНО)

Из предыдущих данных (2026-06-15, скриншоты оператора):
- Сталь (нержавейка): `hatch_speed_mm_s` ≈ 1000, `hatch_distance_mm` = 0.12, `layer_thickness_mm` = 0.06
- Параметры из реального пресета `12Х18Н10Т (Нержавеющая сталь) 32 мкм – 400 Вт` — **пока не сфотографированы**

## Параметры из руководства (примерные, НЕ для продакшна)

Рис. 4.88 Руководства технолога показывает шаблонные значения (не реальный пресет):
- MarkSpeed = 200, JumpSpeed = 3000, Мощность = 50, Step = 150 мкм

## Незаполненные поля (не блокируют расчёт)

- `recoat_time_ms` — засечь секундомером (~9200 мс по калибровке); встроенный расчёт машины показывал 10 с/слой
- `powder_cost_rub_per_kg` — из закупочных документов
- `gas_cost_rub_per_atm` — из договора с поставщиком аргона (расход ≤ 3 л/мин)
- `filter_cost_rub`, `filter_lifetime_hours` — Excel оператора: срок фильтра 48 ч
- `platform_cost_rub` — из закупочных документов
- `time_correction_factor` — null до накопления ≥3 пар прогноз/факт

## Curl-команда для заполнения (алюминий)

```bash
curl -X PUT http://localhost:8000/settings/machine \
  -H "Content-Type: application/json" \
  -d '{
    "hatch_speed_mm_s": 1528,
    "contour_speed_mm_s": 600,
    "hatch_distance_mm": 0.12,
    "layer_thickness_mm": 0.06,
    "jump_speed_mm_s": 3000,
    "laser_count": 2,
    "build_area_cm2": 1225,
    "material_densities": {"aluminum": 2.7}
  }'
```

See also: [[m450-print-time-project]], [[printers-companion]]
