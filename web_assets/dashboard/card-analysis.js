// Shared by card correction and the quality overview. Strict whole lineage,
// not wall-clock order; immutable history remains available when ambiguous.
function _finalQualityOutcome(outcomes) {
    const finals = outcomes.filter(row => row.is_final === true), nodes = new Map(), children = new Map();
    if (!finals.length) return null;
    for (const row of finals) {
        if (typeof row.outcome_id !== 'string' || !row.outcome_id || nodes.has(row.outcome_id)) return null;
        nodes.set(row.outcome_id, row);
    }
    const roots = [];
    for (const row of finals) {
        const parent = row.supersedes_outcome_id;
        if (parent == null) roots.push(row.outcome_id);
        else if (typeof parent !== 'string' || !nodes.has(parent) || children.has(parent)) return null;
        else children.set(parent, row.outcome_id);
    }
    const heads = finals.filter(row => !children.has(row.outcome_id));
    if (roots.length !== 1 || heads.length !== 1) return null;
    const visited = new Set(); let row = heads[0];
    while (row) {
        if (visited.has(row.outcome_id)) return null;
        visited.add(row.outcome_id);
        row = row.supersedes_outcome_id == null ? null : nodes.get(row.supersedes_outcome_id);
    }
    return visited.size === nodes.size ? heads[0] : null;
}

function _finalQualityOutcomes(outcomes) {
    const groups = new Map(), identities = new Map(), invalid = new Set();
    for (const row of outcomes) {
        if (row.is_final !== true) continue;
        const scope = JSON.stringify(row.print_record_id ? ['card', row.print_record_id] : ['session', row.session_id ?? null]);
        if (!groups.has(scope)) groups.set(scope, []);
        groups.get(scope).push(row);
        if (typeof row.outcome_id === 'string' && row.outcome_id) {
            if (identities.has(row.outcome_id)) {invalid.add(scope); invalid.add(identities.get(row.outcome_id));}
            identities.set(row.outcome_id, scope);
        }
    }
    for (const [scope, history] of groups) for (const row of history) {
        const parent = row.supersedes_outcome_id, parentScope = identities.get(parent);
        if (parent != null && parentScope !== scope) {
            invalid.add(scope); if (parentScope != null) invalid.add(parentScope);
        }
    }
    const heads = [];
    for (const [scope, history] of groups) {
        const head = _finalQualityOutcome(history);
        if (!invalid.has(scope) && head) heads.push(head);
    }
    // Physical-card QC is independent of ambiguity in shared log associations.
    // Only the backend ML projection merges truth by session.
    return heads;
}

function _renderPcOutcome() {
    const pane = document.getElementById('pcpane-outcome');
    const f = (_pcSession || {}).features || {};
    const pred = (_pcRecord.metadata_json || {}).prediction || {};
    const timing = _pcTimeMetrics(_pcSession, pred, _pcRecord.summary, _pcRecord.prediction_is_current);
    const actual = timing.actualHours;
    const predictedMachine = timing.predictedHours;
    const err = timing.errorPct;
    const comparisonLabel = timing.scope === 'burn_plus_pour' ? 'Прожиг + нанесение' : 'Нормальный машинный цикл';
    const timeText = hours => hours != null ? hours.toFixed(2) + ' ч' : '—';
    const errCol = err == null ? '' : Math.abs(err) <= 10 ? 'var(--status-success)'
                  : Math.abs(err) <= 25 ? 'var(--status-warning)' : 'var(--status-danger)';

    const report = _pcOperatorReport || {};
    const state = report.state || {};
    const hasReport = typeof state.label_ru === 'string' && state.label_ru.trim().length > 0;
    const confidence = report.confidence || {};
    const stateColor = !hasReport ? 'var(--text-muted)' : state.severity === 'critical' || state.severity === 'high'
        ? 'var(--status-danger)'
        : state.severity === 'medium' ? 'var(--status-warning)' : 'var(--status-success)';
    const deviations = report.deviations || [];
    const deviationRows = deviations.length
        ? deviations.map(item => `<div class="pc-kv">
            <span style="color:${item.severity === 'high' || item.severity === 'critical' ? 'var(--status-danger)' : 'var(--status-warning)'};">${_esc(item.title)}</span>
            <span style="font-weight:400;font-size:12px;color:var(--text-muted);">${_esc(item.detail)}</span>
          </div>`).join('')
        : hasReport
            ? '<div style="color:var(--status-success);font-size:13px;">Существенных отклонений не обнаружено.</div>'
            : '<div style="color:var(--text-muted);font-size:13px;">Заключение анализа не загружено; отклонения неизвестны.</div>';
    const recommendationRows = (report.recommendations || [])
        .map(item => `<li>${_esc(item)}</li>`).join('');

    const outcomes = _pcRecord.quality_outcomes || [];
    const outcomeRows = outcomes.length
        ? outcomes.map(item => {
            const accepted = item.result === 'accepted';
            const label = accepted ? 'Годная' : item.result === 'rejected' ? 'Брак' : item.result;
            return `<div class="pc-kv">
                <span style="color:${accepted ? 'var(--status-success)' : 'var(--status-danger)'};">${_esc(label)} · ${_esc(item.inspection_type)}</span>
                <span style="font-weight:400;font-size:12px;color:var(--text-muted);">
                    ${_esc(item.inspection_result || '')}${item.defect_location ? ` · ${_esc(item.defect_location)}` : ''}<br>
                    ${item.timestamp ? new Date(item.timestamp).toLocaleString('ru') : '—'} · ${_esc(item.created_by || 'operator')}
                </span>
            </div>`;
        }).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Итог контроля ещё не зафиксирован.</div>';

    const timeBlock = _pcSession ? `
        <div class="pc-grid">
            <div class="pc-box">
                <h4>Время</h4>
                <div class="pc-kv"><span>Прожиг + нанесение (измеренная часть)</span><span>${timeText(timing.subtotalHours)}</span></div>
                <div class="pc-kv"><span>Нормальный цикл (допустимые измеренные слои)</span><span>${timeText(timing.measuredHours)}</span></div>
                <div class="pc-kv"><span>Явные завершённые паузы из лога</span><span>${timeText(timing.explicitPauseHours)}</span></div>
                <div class="pc-kv"><span>Неатрибутированный остаток по часам</span><span>${timeText(timing.unattributedHours)}</span></div>
                <div class="pc-kv"><span>Всего по часам</span><span>${timeText(timing.wallHours)}</span></div>
                <div class="pc-kv"><span>Слоёв</span><span>${f.layers || '—'}</span></div>
                <div style="color:var(--text-muted);font-size:12px;margin-top:10px;">
                    Суммы измеренной части не равны всей печати при пропусках. Нормальный прогноз включает штатные задержки и минимальный цикл, но не паузы и повторы. Остаток по часам сам по себе не доказывает простой.${timing.normalLayers != null ? ` Допустимых измеренных слоёв: ${timing.normalLayers}.` : ''}</div>
            </div>
            <div class="pc-box">
                <h4>Прогноз против факта</h4>
                <div class="pc-kv"><span>Сопоставляем</span><span>${comparisonLabel}</span></div>
                <div class="pc-kv"><span>Прогноз</span><span>${timeText(predictedMachine)}</span></div>
                <div class="pc-kv"><span>Факт (допущен к сравнению)</span><span>${timeText(actual)}</span></div>
                <div class="pc-kv"><span>Расхождение</span><span style="color:${errCol};">${err != null ? (err > 0 ? '+' : '') + err.toFixed(1) + '%' : '—'}</span></div>
                ${actual == null ? `<div style="color:var(--text-muted);font-size:12px;margin-top:10px;">${_esc(timing.comparisonReason || 'Нет сопоставимого факта с достаточным покрытием.')} Прожиг + нанесение и длительность по часам не подменяют полный нормальный цикл.</div>` : ''}
                <div class="pc-kv"><span>Сессия</span><span style="font-size:12px;color:var(--text-muted);">${_esc(_pcRecord.session_id || '—')}</span></div>
            </div>
        </div>` : '<div class="pc-empty" style="margin-top:16px;">Логи ещё не привязаны. Результат контроля можно сохранить сейчас; анализ процесса появится после загрузки логов.</div>';

    pane.innerHTML = `
        <section class="pc-verdict" aria-label="Итог анализа">
            <div><span class="pc-source-note">По логам и результатам контроля</span>
                <h2 style="color:${stateColor};">${_esc(hasReport ? state.label_ru : 'Анализ недоступен')}</h2>
                <p class="pc-source-note">Сигналы в логах — повод для проверки, не заключение о браке.</p>
            </div>
            <div><span class="pc-source-note">Достоверность данных</span>
                <strong>${_esc(confidence.level_ru || 'не определена')}</strong></div>
        </section>
        <div class="pc-priority-grid">
            <div>
                <section id="pc-log-insights" aria-label="Участки для контроля"><p class="pc-source-note">Загружаем участки для контроля…</p></section>
                ${_pcChartMarkup('outcome', ['burn', 'oxygen'])}
                <details class="pc-disclosure">
                    <summary>Подробности анализа · ${deviations.length} ${_plural(deviations.length, 'замечание', 'замечания', 'замечаний')}</summary>
                    <div class="pc-box"><h4>Наблюдения в логах</h4>${deviationRows}</div>
                    ${recommendationRows ? `<div class="pc-box"><h4>Что проверить в оборудовании</h4><ul>${recommendationRows}</ul></div>` : ''}
                    <p class="pc-source-note">Возможные причины — гипотезы. Уверенность ${confidence.score != null ? _esc(confidence.score) : 'не определена'} относится к надёжности данных, не к вероятности годности.</p>
                </details>
                <details class="pc-disclosure"><summary>Время печати и сравнение с прогнозом</summary>${timeBlock}</details>
            </div>
            <div class="pc-box pc-qc-form">
                <h3>Результат контроля детали</h3>
                <p class="pc-source-note">Проверили отмеченный участок? Сохраните заключение.</p>
                <div class="pc-field"><label for="qo-result">Итог детали</label>
                    <select id="qo-result" class="mp-input" onchange="_toggleQualityDefectFields()">
                        <option value="accepted">Годная</option><option value="rejected">Брак</option>
                    </select></div>
                <div class="pc-field"><label for="qo-inspection-type">Метод контроля</label>
                    <select id="qo-inspection-type" class="mp-input">
                        <option value="visual">Визуальный</option><option value="dimensional">Размерный</option>
                        <option value="ct">Компьютерная томография</option><option value="metallography">Металлография</option>
                        <option value="tensile">Испытание на растяжение</option><option value="density">Плотность</option>
                        <option value="hardness">Твёрдость</option><option value="surface_roughness">Шероховатость</option>
                        <option value="other">Другой</option>
                    </select></div>
                <div class="pc-field"><label for="qo-inspection-result">Заключение или измерения</label>
                    <textarea id="qo-inspection-result" class="mp-input" rows="2" placeholder="Измеренные значения или заключение"></textarea></div>
                <div id="qo-defect-fields" style="display:none;">
                    <div class="pc-field"><label for="qo-defect-type">Тип дефекта</label>
                        <select id="qo-defect-type" class="mp-input">
                            <option value="porosity">Пористость</option><option value="lack_of_fusion">Несплавление</option>
                            <option value="crack">Трещина</option><option value="warping">Деформация</option>
                            <option value="surface_defect">Поверхностный дефект</option><option value="delamination">Расслоение</option>
                            <option value="oxidation">Окисление</option><option value="powder_inclusion">Включение порошка</option>
                            <option value="dimensional_deviation">Отклонение размеров</option><option value="other">Другой</option>
                        </select></div>
                    <div class="pc-field"><label for="qo-defect-location">Расположение дефекта</label>
                        <input id="qo-defect-location" class="mp-input" placeholder="Деталь, зона или описание места"></div>
                    <div class="pc-form-grid">
                        <div class="pc-field"><label for="qo-layer-start">Слой от</label><input id="qo-layer-start" type="number" min="1" step="1" class="mp-input"></div>
                        <div class="pc-field"><label for="qo-layer-end">Слой до</label><input id="qo-layer-end" type="number" min="1" step="1" class="mp-input"></div>
                    </div>
                </div>
                <details class="pc-disclosure"><summary>Добавить заметку</summary><div class="pc-field"><label for="qo-notes">Заметка</label><textarea id="qo-notes" class="mp-input" rows="2"></textarea></div></details>
                <button id="qo-save-button" class="quick-btn primary" type="button" onclick="savePrintQualityOutcome()">Сохранить результат контроля</button>
                <p id="qo-save-status" role="status" aria-live="polite"></p>
                <details class="pc-disclosure"><summary>История контроля · ${outcomes.length}</summary>${outcomeRows}</details>
            </div>
        </div>`;
    for (const id of ['qo-result', 'qo-inspection-type', 'qo-inspection-result', 'qo-defect-type',
            'qo-defect-location', 'qo-layer-start', 'qo-layer-end', 'qo-notes']) {
        document.getElementById(id)?.addEventListener(id === 'qo-result' || id === 'qo-inspection-type'
            || id === 'qo-defect-type' ? 'change' : 'input', () => {
            _pcQualityDirty = true;
            _pcEditGeneration += 1;
        });
    }
}

function _toggleQualityDefectFields() {
    const fields = document.getElementById('qo-defect-fields');
    if (fields) fields.style.display = document.getElementById('qo-result')?.value === 'rejected' ? 'block' : 'none';
}

async function savePrintQualityOutcome() {
    if (!_pcRecord || _pcLoading) return;
    const saveButton = document.getElementById('qo-save-button');
    if (saveButton?.disabled) return;
    const record = _pcRecord, generation = _pcLoadGeneration, editGeneration = _pcEditGeneration;
    const current = () => generation === _pcLoadGeneration && !_pcLoading && _pcRecord?.record_id === record.record_id
        && document.getElementById('page-print-card').style.display !== 'none';
    const status = document.getElementById('qo-save-status');
    const rejected = document.getElementById('qo-result').value === 'rejected';
    const layerStart = document.getElementById('qo-layer-start')?.value;
    const layerEnd = document.getElementById('qo-layer-end')?.value;
    const payload = {
        result: document.getElementById('qo-result').value,
        inspection_type: document.getElementById('qo-inspection-type').value,
        inspection_result: document.getElementById('qo-inspection-result').value.trim(),
        notes: document.getElementById('qo-notes').value.trim() || null,
    };
    const outcomes = record.quality_outcomes || [];
    const latestFinalOutcome = _finalQualityOutcome(outcomes);
    if (!latestFinalOutcome && outcomes.some(item => item.is_final === true)) {
        status.textContent = 'Цепочка итогов контроля неоднозначна; история сохранена.';
        status.style.color = 'var(--status-danger)';
        return;
    }
    if (latestFinalOutcome) {
        payload.supersedes_outcome_id = latestFinalOutcome.outcome_id;
    }
    if (rejected) {
        payload.defect_type = document.getElementById('qo-defect-type').value;
        payload.defect_location = document.getElementById('qo-defect-location').value.trim() || null;
        if (layerStart !== '' || layerEnd !== '') {
            payload.layer_range = {
                start_layer: layerStart === '' ? Number(layerEnd) : Number(layerStart),
                end_layer: layerEnd === '' ? Number(layerStart) : Number(layerEnd),
            };
        }
    }
    status.textContent = 'Сохранение…';
    status.style.color = 'var(--text-muted)';
    if (saveButton) saveButton.disabled = true;
    try {
        const response = await fetch(`/prints/${record.record_id}/quality-outcomes`, {
            method: 'POST', headers: _jsonHeaders(), body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error((await response.json()).detail || response.status);
        const saved = await response.json();
        if (!current()) return;
        if (saved?.outcome_id) _pcRecord.quality_outcomes = [saved, ...(record.quality_outcomes || [])];
        if (editGeneration === _pcEditGeneration) _pcQualityDirty = false;
        await _refreshOpenPrintCardFromSync();
        if (!current()) return;
        const freshStatus = document.getElementById('qo-save-status');
        freshStatus.textContent = 'Сохранено';
        freshStatus.style.color = 'var(--status-success)';
    } catch (error) {
        if (current()) {
            status.textContent = `Ошибка: ${error.message}`;
            status.style.color = 'var(--status-danger)';
        }
    } finally {
        if (saveButton) saveButton.disabled = false;
    }
}

function _renderPcProcess() {
    const pane = document.getElementById('pcpane-process');
    if (!_pcSession) {
        pane.innerHTML = '<div class="pc-empty">Логи не привязаны — данных о процессе нет.</div>';
        return;
    }
    const f = _pcSession.features || {};
    const health = _pcSession.health || {};
    const anomalies = health.anomalies || [];
    const soft = ((_pcSession.soft_sensors || {}).metrics || []);
    const phases = ((_pcSession.phase_statistics || {}).phases || {});
    const readiness = (health.readiness || {}).score;
    const dq = f.data_quality_score;
    const geometryAnalysis = _pcRecord.geometry_analysis || {};
    const geometryItems = geometryAnalysis.items || [];
    const safeGeometryText = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[ch]);

    const anomalyRows = anomalies.length
        ? anomalies.map(a => `<div class="pc-kv">
                <span style="color:var(--status-warning);">${_esc(a.title || a.kind || 'Наблюдение')}</span>
                <span>${_esc(a.detail || a.description || '')}</span>
           </div>`).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Аномалий не найдено.</div>';

    const softRows = soft.length
        ? soft.map(metric => `<div class="pc-kv">
                <span>${_esc(metric.name_ru)}</span>
                <span title="${_esc(metric.method || '')}">${_esc(metric.value ?? '—')} ${_esc(metric.unit || '')}</span>
           </div>`).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Недостаточно совместных каналов.</div>';
    const phaseOrder = ['laser_scan', 'powder_recoat', 'controller_overhead'];
    const phaseRows = phaseOrder.filter(key => phases[key]).map(key => {
        const phase = phases[key];
        return `<div class="pc-kv"><span>${phase.name_ru}</span><span>${(phase.total_sec / 3600).toFixed(2)} ч · ${phase.share_pct ?? '—'}%</span></div>`;
    }).join('') || '<div style="color:var(--text-muted);font-size:13px;">Нет точных времён по слоям.</div>';
    const geometryRows = geometryItems.length
        ? geometryItems.map(item => {
            const bodies = (item.active_bodies || [])
                .map(body => safeGeometryText(body.name)).join(', ');
            const layerLabel = item.layer_range
                ? `Слои ${item.layer_range[0]}–${item.layer_range[1]}`
                : `${item.mapping_precision === 'exact_layer' ? 'Слой' : 'Около слоя'} ${Number(item.layer)}`;
            const signal = item.signal ? `${safeGeometryText(item.signal)} · ` : '';
            return `<div class="pc-kv">
                <span>${signal}${layerLabel} · ${Number.isFinite(item.height_mm) ? item.height_mm.toFixed(2) + ' мм' : 'высота не подтверждена'}</span>
                <span style="font-weight:400;font-size:12px;color:var(--text-muted);">${safeGeometryText(item.detail || item.geometry?.largest_path_component_ru || 'геометрия слоя')}${bodies ? ` · ${bodies}` : ''}</span>
           </div>`;
        }).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Нет аномалий, которые можно связать с геометрией.</div>';

    pane.innerHTML = `
        <p class="pc-source-note">Измерения из логов этой печати. Разрывы и отсутствующие каналы не заменены нулями.</p>
        ${_pcChartMarkup('process', ['burn', 'oxygen', 'temperatures', 'pressure', 'humidity'])}
        <details class="pc-disclosure"><summary>Диагностика датчиков и расчётные показатели</summary>
        <div class="pc-grid">
            <div class="pc-box">
                <h4>Атмосфера и качество</h4>
                <div class="pc-kv"><span>Готовность атмосферы</span><span style="color:${_scoreColorFor(readiness)}">${readiness ?? '—'}</span></div>
                <div class="pc-kv"><span>Качество данных</span><span style="color:${_scoreColorFor(dq)}">${dq ?? '—'} ${f.data_quality_grade ? `(${f.data_quality_grade})` : ''}</span></div>
                <div class="pc-kv"><span>Событий в логах</span><span>${(f.total_events || 0).toLocaleString('ru')}</span></div>
                <div class="pc-kv"><span>Пауз зафиксировано</span><span>${f.pause_count ?? 0}</span></div>
            </div>
            <div class="pc-box">
                <h4>Аномалии процесса (${anomalies.length})</h4>
                ${anomalyRows}
            </div>
        </div>
        <div class="pc-grid" style="margin-top:16px;">
            <div class="pc-box">
                <h4>Расчётные датчики</h4>
                ${softRows}
                <div style="color:var(--text-muted);font-size:11px;margin-top:8px;">Диагностические оценки из нескольких физических каналов; формула доступна в подсказке.</div>
            </div>
            <div class="pc-box">
                <h4>Из чего сложилось машинное время</h4>
                ${phaseRows}
            </div>
        </div>
        <div class="pc-box" style="margin-top:16px;">
            <h4>Где отклонение находится в модели</h4>
            ${geometryRows}
            ${geometryAnalysis.geometry_confidence?.note_ru ? `<div style="color:var(--status-warning);font-size:11px;margin-top:8px;">${safeGeometryText(geometryAnalysis.geometry_confidence.note_ru)}</div>` : ''}
            <div class="pc-source-note">${_esc(geometryAnalysis.limitation_ru || 'Для привязки нужны одновременно STL и логи печати.')}</div>
        </div>
        </details>
        <button class="quick-btn" type="button" onclick="_openPcTelemetry()">Сравнить с другими печатями</button>`;
}

const _PC_CHART_TITLES = {
    burn: 'Время прожига по слоям', oxygen: 'Кислород в камере',
    temperatures: 'Температуры', pressure: 'Давление в камере', humidity: 'Влажность газа',
};

function _pcChartMarkup(prefix, keys) {
    const telemetry = _pcSession?.telemetry || {};
    const available = keys.filter(key => key === 'burn'
        ? (telemetry.layer_burn_times || []).some(row => Number.isFinite(row.duration_sec))
        : Object.values(telemetry[key] || {}).some(values => values.some(Number.isFinite)));
    if (!available.length) return '<p class="pc-empty-state">Графиков пока нет: в доступных логах нет сохранённых измерений.</p>';
    return `<div class="pc-chart-grid">${available.map(key => `<section class="pc-chart-box">
        <h3>${_PC_CHART_TITLES[key]}</h3><div class="pc-chart-frame">
            <canvas id="pc-${prefix}-${key}" data-pc-chart="${key}" role="img" aria-label="${_PC_CHART_TITLES[key]}"></canvas>
        </div><p class="pc-source-note">${key === 'burn' ? 'Физический номер слоя · секунды работы лазера' : 'Время по журналу · исходные показания датчиков'}</p>
        </section>`).join('')}</div>`;
}

function _pcShowCharts(pane) {
    const telemetry = _pcSession?.telemetry || {};
    for (const canvas of pane.querySelectorAll('canvas[data-pc-chart]')) {
        if (typeof Chart !== 'undefined' && Chart.getChart(canvas)) continue;
        const key = canvas.dataset.pcChart, burn = telemetry.layer_burn_times || [];
        const labels = key === 'burn' ? burn.map(row => row.layer) : telemetry.time || [];
        const datasets = key === 'burn'
            ? [{label: 'Прожиг, с', data: burn.map(row => Number.isFinite(row.duration_sec) ? row.duration_sec : null),
                borderColor: '#f6a06b', backgroundColor: '#f6a06b', pointRadius: 0}]
            : _makeDatasets(telemetry[key] || {}).map(dataset => ({...dataset,
                data: dataset.data.map(value => Number.isFinite(value) ? value : null), tension: 0, spanGaps: false}));
        createDashboardChart(canvas, {
            type: 'line', data: {labels, datasets},
            options: {responsive: true, maintainAspectRatio: false, animation: false,
                interaction: {mode: 'index', intersect: false},
                plugins: {legend: {position: 'bottom'}},
                scales: {x: {ticks: {maxTicksLimit: 7}}, y: {beginAtZero: key === 'burn'}}},
        });
    }
}

function _openPcTelemetry() {
    if (!_pcRecord?.session_id) return;
    showSessionsPage('telemetry');
    loadTelemetryForSession(_pcRecord.session_id);
}

function _scoreColorFor(v) {
    if (v == null) return 'var(--text-muted)';
    return scoreColor(v, 80, 50);
}
