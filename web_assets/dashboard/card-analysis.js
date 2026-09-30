function _renderPcOutcome() {
    const pane = document.getElementById('pcpane-outcome');
    const f = (_pcSession || {}).features || {};
    const pred = (_pcRecord.metadata_json || {}).prediction || {};
    const timing = _pcTimeMetrics(_pcSession, pred, _pcRecord.summary);
    const actual = timing.actualHours;
    const predictedMachine = timing.predictedHours;
    const err = timing.errorPct;
    const comparisonLabel = timing.scope === 'burn_plus_pour' ? 'Прожиг + нанесение' : 'Нормальный машинный цикл';
    const timeText = hours => hours != null ? hours.toFixed(2) + ' ч' : '—';
    const errCol = err == null ? '' : Math.abs(err) <= 10 ? 'var(--status-success)'
                  : Math.abs(err) <= 25 ? 'var(--status-warning)' : 'var(--status-danger)';

    const report = _pcOperatorReport || {};
    const state = report.state || {};
    const confidence = report.confidence || {};
    const stateColor = state.severity === 'critical' || state.severity === 'high'
        ? 'var(--status-danger)'
        : state.severity === 'medium' ? 'var(--status-warning)' : 'var(--status-success)';
    const deviations = (report.deviations || []).slice(0, 6);
    const deviationRows = deviations.length
        ? deviations.map(item => `<div class="pc-kv">
            <span style="color:${item.severity === 'high' || item.severity === 'critical' ? 'var(--status-danger)' : 'var(--status-warning)'};">${_esc(item.title)}</span>
            <span style="font-weight:400;font-size:12px;color:var(--text-muted);">${_esc(item.detail)}</span>
          </div>`).join('')
        : '<div style="color:var(--status-success);font-size:13px;">Существенных отклонений не обнаружено.</div>';
    const recommendationRows = (report.recommendations || []).slice(0, 6)
        .map(item => `<li>${_esc(item)}</li>`).join('');

    const outcomes = _pcRecord.quality_outcomes || [];
    const outcomeRows = outcomes.length
        ? outcomes.slice(0, 8).map(item => {
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
        <div class="pc-grid" style="margin-top:16px;">
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
        <div class="pc-grid">
            <div class="pc-box">
                <h4>Итог анализа</h4>
                <div style="font-size:18px;font-weight:700;color:${stateColor};">${_esc(state.label_ru || 'Анализ ещё не выполнен')}</div>
                <div style="color:var(--text-muted);font-size:12px;margin-top:6px;">
                    Уверенность: ${_esc(confidence.level_ru || 'не определена')}${confidence.score != null ? ` (${confidence.score})` : ''}.
                    Это надёжность данных, а не вероятность годности детали.
                </div>
                <div style="margin-top:12px;">${deviationRows}</div>
            </div>
            <div class="pc-box">
                <h4>Рекомендуемые действия</h4>
                <ul style="margin:0;padding-left:18px;color:var(--text-secondary);font-size:13px;line-height:1.55;">${recommendationRows || '<li>Дополнительных действий не требуется.</li>'}</ul>
                <div style="color:var(--text-muted);font-size:11px;margin-top:10px;">Возможные причины в отчёте — проверяемые гипотезы, а не доказанная причинность.</div>
            </div>
        </div>
        ${timeBlock}
        <div class="pc-grid" style="margin-top:16px;">
            <div class="pc-box">
                <h4>Зафиксировать результат контроля</h4>
                <div class="pc-field"><label>Итог детали</label>
                    <select id="qo-result" class="mp-input" onchange="_toggleQualityDefectFields()">
                        <option value="accepted">Годная</option><option value="rejected">Брак</option>
                    </select></div>
                <div class="pc-field"><label>Метод контроля</label>
                    <select id="qo-inspection-type" class="mp-input">
                        <option value="visual">Визуальный</option><option value="dimensional">Размерный</option>
                        <option value="ct">Компьютерная томография</option><option value="metallography">Металлография</option>
                        <option value="tensile">Испытание на растяжение</option><option value="density">Плотность</option>
                        <option value="hardness">Твёрдость</option><option value="surface_roughness">Шероховатость</option>
                        <option value="other">Другой</option>
                    </select></div>
                <div class="pc-field"><label>Результат контроля</label>
                    <textarea id="qo-inspection-result" class="mp-input" rows="2" placeholder="Измеренные значения или заключение"></textarea></div>
                <div id="qo-defect-fields" style="display:none;">
                    <div class="pc-field"><label>Тип дефекта</label>
                        <select id="qo-defect-type" class="mp-input">
                            <option value="porosity">Пористость</option><option value="lack_of_fusion">Несплавление</option>
                            <option value="crack">Трещина</option><option value="warping">Деформация</option>
                            <option value="surface_defect">Поверхностный дефект</option><option value="delamination">Расслоение</option>
                            <option value="oxidation">Окисление</option><option value="powder_inclusion">Включение порошка</option>
                            <option value="dimensional_deviation">Отклонение размеров</option><option value="other">Другой</option>
                        </select></div>
                    <div class="pc-field"><label>Расположение дефекта</label>
                        <input id="qo-defect-location" class="mp-input" placeholder="Деталь, зона или описание места"></div>
                    <div style="display:flex;gap:10px;">
                        <div class="pc-field" style="flex:1;"><label>Начальный слой</label><input id="qo-layer-start" type="number" min="0" class="mp-input"></div>
                        <div class="pc-field" style="flex:1;"><label>Конечный слой</label><input id="qo-layer-end" type="number" min="0" class="mp-input"></div>
                    </div>
                </div>
                <div class="pc-field"><label>Заметка</label><textarea id="qo-notes" class="mp-input" rows="2"></textarea></div>
                <button class="quick-btn primary" onclick="savePrintQualityOutcome()">Сохранить результат</button>
                <span id="qo-save-status" style="margin-left:10px;font-size:13px;"></span>
            </div>
            <div class="pc-box"><h4>История контроля</h4>${outcomeRows}</div>
        </div>`;
}

function _toggleQualityDefectFields() {
    const fields = document.getElementById('qo-defect-fields');
    if (fields) fields.style.display = document.getElementById('qo-result')?.value === 'rejected' ? 'block' : 'none';
}

async function savePrintQualityOutcome() {
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
    const latestFinalOutcome = (_pcRecord.quality_outcomes || []).find(item => item.is_final);
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
    try {
        const response = await fetch(`/prints/${_pcRecord.record_id}/quality-outcomes`, {
            method: 'POST', headers: _jsonHeaders(), body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error((await response.json()).detail || response.status);
        [_pcRecord, _pcOperatorReport] = await Promise.all([
            fetch(`/prints/${_pcRecord.record_id}`).then(r => r.json()),
            fetch(`/prints/${_pcRecord.record_id}/operator-report`).then(r => r.json()),
        ]);
        _renderPrintCard();
        showPrintCardTab('outcome');
        const freshStatus = document.getElementById('qo-save-status');
        freshStatus.textContent = 'Сохранено';
        freshStatus.style.color = 'var(--status-success)';
    } catch (error) {
        status.textContent = `Ошибка: ${error.message}`;
        status.style.color = 'var(--status-danger)';
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
        ? anomalies.slice(0, 8).map(a => `<div class="pc-kv">
                <span style="color:var(--status-warning);">${a.title || a.kind || 'аномалия'}</span>
                <span style="font-weight:400;font-size:12px;color:var(--text-muted);">${a.detail || a.description || ''}</span>
           </div>`).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Аномалий не найдено.</div>';

    const softRows = soft.length
        ? soft.slice(0, 8).map(metric => `<div class="pc-kv">
                <span>${metric.name_ru}</span>
                <span title="${metric.method || ''}">${metric.value ?? '—'} ${metric.unit || ''}</span>
           </div>`).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Недостаточно совместных каналов.</div>';
    const phaseOrder = ['laser_scan', 'powder_recoat', 'controller_overhead'];
    const phaseRows = phaseOrder.filter(key => phases[key]).map(key => {
        const phase = phases[key];
        return `<div class="pc-kv"><span>${phase.name_ru}</span><span>${(phase.total_sec / 3600).toFixed(2)} ч · ${phase.share_pct ?? '—'}%</span></div>`;
    }).join('') || '<div style="color:var(--text-muted);font-size:13px;">Нет точных времён по слоям.</div>';
    const geometryRows = geometryItems.length
        ? geometryItems.slice(0, 10).map(item => {
            const bodies = (item.active_bodies || []).slice(0, 3)
                .map(body => safeGeometryText(body.name)).join(', ');
            const layerLabel = item.layer_range
                ? `Слои ${item.layer_range[0]}–${item.layer_range[1]}`
                : `${item.mapping_precision === 'exact_layer' ? 'Слой' : 'Около слоя'} ${Number(item.layer)}`;
            const signal = item.signal ? `${safeGeometryText(item.signal)} · ` : '';
            return `<div class="pc-kv">
                <span>${signal}${layerLabel} · ${Number(item.height_mm).toFixed(2)} мм</span>
                <span style="font-weight:400;font-size:12px;color:var(--text-muted);">${safeGeometryText(item.detail || item.geometry?.largest_path_component_ru || 'геометрия слоя')}${bodies ? ` · ${bodies}` : ''}</span>
           </div>`;
        }).join('')
        : '<div style="color:var(--text-muted);font-size:13px;">Нет аномалий, которые можно связать с геометрией.</div>';

    pane.innerHTML = `
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
            <div style="color:var(--text-muted);font-size:11px;margin-top:8px;">${geometryAnalysis.limitation_ru || 'Для привязки нужны одновременно STL и логи печати.'}</div>
        </div>
        <div style="margin-top:16px;">
            <div id="pc-log-insights" style="margin-bottom:16px;"></div>
            <a onclick="showSessionsPage('telemetry'); setTimeout(() => loadTelemetryForSession('${_pcRecord.session_id}'), 300);"
               style="color:var(--color-accent);cursor:pointer;font-size:13px;">Открыть графики телеметрии этой печати →</a>
        </div>`;
    if (window.PrinterLogInsights) {
        window.PrinterLogInsights.load(pane.querySelector('#pc-log-insights'), _pcRecord.record_id);
    }
}

function _scoreColorFor(v) {
    if (v == null) return 'var(--text-muted)';
    return scoreColor(v, 80, 50);
}
