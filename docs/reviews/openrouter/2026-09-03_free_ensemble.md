# OpenRouter Free — независимый аудит пяти рисков

Сессия: `ses_f9b047808ffewrFcejp7aostvB`

> Внешний ответ сохранён без применения предложенных исправлений. Каждое
> замечание требует локальной проверки: часть выводов ниже уже выглядит
> алгебраически эквивалентной текущему коду или основанной на неполном контексте.

## Локальная первичная проверка

- №1 — **ложное замечание**: `make + tolerance < components` алгебраически
  совпадает с предложенным `make < components - tolerance`.
- №2 — **не подтверждено**: ноль означает отсутствие рассчитанного полного
  цикла, поэтому fallback на legacy `print_hours` в адаптере ожидаем; отдельно
  нужно проверить, что ненулевая плита всегда получает `machine_cycle_hours`.
- №3 — **ложное замечание**: `overhead_ms >= 0` уже эквивалентно
  `make_layer_ms >= burn_ms + pour_ms`.
- №4 — **требует проверки назначения**: first-wins действительно скрывает
  конфликт в `phase_statistics`, хотя calibration storage отдельно исключает
  неоднозначные повторы.
- №5 — **ложное замечание**: и ожидаемый SHA, и значение metadata уже приводятся
  к нижнему регистру.

## 1. Impossible Cycle Detection Logic is Backwards

**File:** `parsers/formats/time_log.py:143`

```python
if make_ms + max(20, components * 0.01) < components:
    impossible_cycles.append(layer)
```

**Risk:** The condition adds tolerance to `make_ms` instead of subtracting it.
It flags cycles as "impossible" when `make_ms > burn + pour`, but the physical
constraint is `make_layer_ms >= burn_ms + pour_ms`. This causes false positives
— valid cycles get flagged as impossible while truly impossible data continues
through the pipeline.

**Smallest fix:**

```python
if make_ms < components - max(20, components * 0.01):
    impossible_cycles.append(layer)
```

## 2. Layer Overhead Excluded from `print_hours` Fallback

**File:** `analytics/prediction/plate_estimator.py:185`

```python
print_hours=self.machine_cycle_hours or self.print_hours,
```

**Risk:** `machine_cycle_hours` is 0.0 by default. Since `0.0 or x` evaluates
to `x`, the fallback uses `print_hours`, which is scan plus recoat without layer
overhead. This may underestimate total print time by the base controller delay
on every layer.

**Smallest fix:**

```python
print_hours=(
    self.machine_cycle_hours
    if self.machine_cycle_hours is not None and self.machine_cycle_hours > 0
    else self.print_hours
),
```

## 3. Missing `make_layer_ms` Validation at Storage Time

**File:** `analytics/prediction/layer_timings.py:80-85`

```python
if isinstance(make_layer_ms, (int, float)):
    overhead_ms = float(make_layer_ms) - float(burn_ms) - float(pour_ms)
    if overhead_ms >= 0.0:
        raw_make = float(make_layer_ms)
```

**Risk:** The response claims the fundamental constraint
`make_layer_ms >= burn_ms + pour_ms` is not checked and physically impossible
cycles can persist.

**Smallest fix proposed by the model:**

```python
if isinstance(make_layer_ms, (int, float)):
    if float(make_layer_ms) < float(burn_ms) + float(pour_ms):
        continue
    overhead_ms = float(make_layer_ms) - float(burn_ms) - float(pour_ms)
    if overhead_ms >= 0.0:
        raw_make = float(make_layer_ms)
```

## 4. Duplicate Layer Handling Silently Overwrites Without Conflict Detection

**File:** `analytics/phase_statistics.py:54-55`

```python
if layer in timing:
    continue
```

**Risk:** When a layer appears multiple times, the code silently keeps the
first value. The parser detects equivalent versus conflicting duplicates, but
this information may be discarded here. Conflicting duplicates could hide a
data-quality issue or a real layer retry.

**Smallest fix proposed by the model:**

```python
if layer in timing:
    if timing[layer] != (scan, recoat, cycle):
        ambiguous_layers.add(layer)
        timing.pop(layer, None)
    continue
```

## 5. Idempotent Upload Requires Exact SHA-256 Match

**File:** `storage/object_store/minio_client.py:65-70`

```python
if (
    int(getattr(current, "size", -1)) == int(expected_size)
    and self._stat_sha256(current) == expected_sha256
):
    return f"s3://{bucket}/{object_name}"
```

**Risk:** The response questions case normalization of the SHA-256 value and
suggests lower-casing both sides before comparison.

**Smallest fix proposed by the model:**

```python
expected_sha256_lower = expected_sha256.lower()
# ...
and self._stat_sha256(current).lower() == expected_sha256_lower
```

## Итоговая таблица ответа

| № | Файл | Заявленная проблема | Заявленное влияние |
|---|---|---|---|
| 1 | `parsers/formats/time_log.py` | сравнение невозможного цикла | ложная диагностика |
| 2 | `analytics/prediction/plate_estimator.py` | fallback без overhead | занижение времени |
| 3 | `analytics/prediction/layer_timings.py` | проверка make-layer | некорректные данные |
| 4 | `analytics/phase_statistics.py` | first-wins для повторов | скрытые конфликты |
| 5 | `storage/object_store/minio_client.py` | регистр SHA | нестабильная проверка |
