// PRINT CARD — one print, everything about it
// =========================================================
let _pcRecord = null;   // the record being viewed
let _pcSession = null;  // its linked log session, if any
let _pcOperatorReport = null; // compact deterministic conclusion
let _pcDefectRisk = null; // explainable model/heuristic output for this session
let _pcDirty = false;   // unsaved local edits must never be overwritten by sync
let _pcQualityDirty = false; // inspection form is independent of card settings
let _pcLoadGeneration = 0;
let _pcLoading = false;
let _pcEditGeneration = 0;
let _printSyncPrimed = false;
let _printSyncTimer = null;

function _pcTimeMetrics(session, prediction = {}, summary = {}, predictionIsCurrent) {
    prediction = prediction || {};
    summary = summary || {};
    const features = session?.features || {};
    const accounting = session?.log_insights?.time_accounting || {};
    const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
    const hours = (value, unit) => finite(value) == null ? null : value / unit;
    const normalLayers = accounting.normal_layer_count;
    // Coverage and admission belong to the backend's shared timing rules.
    // Never reconstruct a complete-print fact from a partial session sum.
    const scope = summary.comparison_scope;
    const compatible = scope === 'machine_cycle' || scope === 'burn_plus_pour';
    const admitted = compatible && summary.coverage?.complete === true
        && summary.actual_source === (scope === 'machine_cycle' ? 'normal_machine_log' : 'subtotal_machine_log');
    const measuredHours = hours(features.normal_machine_cycle_seconds, 3600);
    const actualHours = admitted && finite(summary.actual_hours) > 0 ? summary.actual_hours : null;
    const predictedHours = predictionIsCurrent === true && compatible ? hours(summary.predicted_hours, 1) : null;
    return {
        subtotalHours: hours(features.machine_min, 60),
        wallHours: hours(features.duration_min, 60),
        measuredHours, actualHours, predictedHours, scope: compatible ? scope : null,
        comparisonReason: typeof summary.comparison_reason_ru === 'string' ? summary.comparison_reason_ru : '',
        predictedSubtotalHours: predictionIsCurrent === true ? hours(prediction.print_hours, 1) : null,
        normalLayers: Number.isInteger(normalLayers) && normalLayers >= 0 ? normalLayers : null,
        explicitPauseHours: hours(Object.prototype.hasOwnProperty.call(features, 'explicit_pause_seconds')
            ? features.explicit_pause_seconds : accounting.explicit_pause_seconds, 3600),
        unattributedHours: hours(features.unattributed_elapsed_seconds, 3600),
        errorPct: predictedHours != null && actualHours != null && typeof summary.error_pct === 'number'
            && Number.isFinite(summary.error_pct) ? summary.error_pct : null,
    };
}

function _pcPredictionFreshnessLabel(record) {
    const inputRevision = record.metadata_json?.prediction?.input_revision;
    const knownMismatch = record.prediction_is_current === false
        && Number.isInteger(inputRevision) && inputRevision > 0 && Number.isInteger(record.revision)
        && record.revision !== inputRevision && record.revision !== inputRevision + 1;
    return knownMismatch ? 'Прогноз устарел' : 'Актуальность прогноза не подтверждена';
}

async function openPrintCard(recordId, initialTab = null) {
    const generation = ++_pcLoadGeneration;
    _pcLoading = true;
    _hideAllPages();
    _showSubnav(null);
    _setMainActive('prints');
    document.getElementById('page-print-card').style.display = 'block';
    document.querySelectorAll('.pc-pane').forEach(pane => {
        pane.style.display = 'none';
        pane.setAttribute('aria-hidden', 'true');
    });
    window.scrollTo(0, 0);

    document.getElementById('pc-title').textContent = 'Загрузка…';
    document.getElementById('pc-subtitle').textContent = '';
    try {
        const record = await fetch(`/prints/${recordId}`).then(r => {
            if (!r.ok) throw new Error(r.status);
            return r.json();
        });
        if (generation !== _pcLoadGeneration || document.getElementById('page-print-card').style.display === 'none') return;
        // The outcome lives on the session, so it is fetched alongside —
        // "Как прошло" and "Процесс" are unanswerable without it.
        const [session, operatorReport, defectRisk] = await Promise.all([
            record.session_id
                ? fetch(`/sessions/${record.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
            fetch(`/prints/${recordId}/operator-report`).then(r => r.ok ? r.json() : null).catch(() => null),
            record.session_id
                ? fetch(`/analysis/defect-risk/${record.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
        ]);
        if (generation !== _pcLoadGeneration || document.getElementById('page-print-card').style.display === 'none') return;
        [_pcRecord, _pcSession, _pcOperatorReport, _pcDefectRisk] = [record, session, operatorReport, defectRisk];
        _pcDirty = false;
        _pcQualityDirty = false;
        _renderPrintCard();
        _pcLoading = false;
        showPrintCardTab(initialTab || (record.session_id ? 'outcome' : 'what'));
        if (record.estimate_job) {
            window._pendingEstimateRequests ||= new Set();
            if (!window._pendingEstimateRequests.has(recordId)) {
                window._pendingEstimateRequests.add(recordId);
                _pollEstimate(recordId, record.estimate_job.job_id, true);
            }
        }
    } catch (e) {
        if (generation === _pcLoadGeneration && document.getElementById('page-print-card').style.display !== 'none') {
            [_pcRecord, _pcSession, _pcOperatorReport, _pcDefectRisk] = [null, null, null, null];
            _pcDirty = false;
            _pcQualityDirty = false;
            document.getElementById('pc-title').textContent = 'Не удалось загрузить карточку';
        }
    } finally {
        if (generation === _pcLoadGeneration) _pcLoading = false;
    }
}

function _renderPrintCard() {
    if (typeof Chart !== 'undefined') {
        document.querySelectorAll('#page-print-card canvas').forEach(canvas => Chart.getChart(canvas)?.destroy());
    }
    const rec = _pcRecord, s = _pcSession;
    const shortId = String(rec.record_id || '').replace(/^pr_/, 'S-').slice(0, 8);
    document.getElementById('pc-title').textContent = shortId;

    const bits = [];
    const when = rec.printed_at || rec.created_at;
    if (when) bits.push(new Date(when).toLocaleDateString('ru'));
    bits.push(rec.session_id ? 'логи привязаны' : 'логи не привязаны');
    const f = ((s || {}).features) || {};
    if (f.layers) bits.push(`${f.layers} ${_plural(f.layers, 'слой', 'слоя', 'слоёв')}`);
    document.getElementById('pc-subtitle').textContent = rec.name || 'Без названия';
    document.getElementById('pc-header-status').textContent = rec.session_id ? 'ЛОГИ ПРИВЯЗАНЫ' : 'НЕТ ЛОГОВ';
    document.getElementById('pc-header-status').style.color = rec.session_id ? '#aebf92' : '#f6a06b';

    document.getElementById('pc-actions').innerHTML = `<button class="home-btn" type="button" onclick="showMainTab('home',null)">← Печати</button><button class="home-btn primary" type="button" onclick="showPrintCardTab('files')">Файлы</button>`;

    _renderPcWhat(); _renderPcPlan(); _renderPcOutcome(); _renderPcProcess(); _renderPcFiles();
    _renderPredictionWarnings();
}

function showPrintCardTab(tab) {
    if (_pcLoading || !_pcRecord) return;
    document.querySelectorAll('.pc-pane').forEach(p => {
        p.style.display = 'none';
        p.setAttribute('aria-hidden', 'true');
    });
    document.querySelectorAll('#pc-tabs a').forEach(a => {
        const selected = a.id === 'pctab-' + tab;
        a.classList.toggle('active', selected);
        a.setAttribute('role', 'tab');
        a.setAttribute('aria-selected', String(selected));
        a.setAttribute('tabindex', selected ? '0' : '-1');
    });
    const pane = document.getElementById('pcpane-' + tab);
    pane.style.display = 'block';
    pane.setAttribute('role', 'tabpanel');
    pane.setAttribute('aria-hidden', 'false');
    pane.setAttribute('aria-labelledby', 'pctab-' + tab);
    if (typeof _pcShowCharts === 'function') _pcShowCharts(pane);
    const insights = pane.querySelector('#pc-log-insights');
    if (insights && !insights.dataset.loaded && window.PrinterLogInsights) {
        window.PrinterLogInsights.load(insights, _pcRecord.record_id, _pcRecord.geometry_analysis || {});
    }
}

function _renderPcWhat(preserveInputs = false) {
    const fieldIds = ['pc-name', 'pc-material', 'pc-thickness', 'pc-powder', 'pc-notes'];
    const liveValues = preserveInputs
        ? fieldIds.map(id => [id, document.getElementById(id)?.value]) : [];
    const active = preserveInputs && fieldIds.includes(document.activeElement?.id)
        ? document.activeElement : null;
    const focus = active ? {id: active.id, start: active.selectionStart,
        end: active.selectionEnd, direction: active.selectionDirection} : null;
    const retainedPreview = preserveInputs ? document.getElementById('pc-preview-host') : null;
    const rec = _pcRecord;
    const materials = [...(window._printMaterials || [])];
    if (rec.material && !materials.includes(rec.material)) materials.unshift(rec.material);
    const opts = `<option value="" ${rec.material ? '' : 'selected'}>Материал не указан</option>`
        + materials.map(m => `<option value="${_esc(m)}" ${m === rec.material ? 'selected' : ''}>${_esc(_materialRu(m))}</option>`).join('');

    const pred = rec.prediction_is_current === true ? (rec.metadata_json || {}).prediction || {} : {};
    const timing = _pcTimeMetrics(_pcSession, pred, rec.summary, rec.prediction_is_current);
    const actualHours = timing.actualHours;
    const predictedHours = timing.predictedHours;
    const predictionError = timing.errorPct;
    const risk = _pcDefectRisk?.risk;
    const defectRisk = typeof risk === 'number' && Number.isFinite(risk)
        && risk >= 0 && risk <= 1 ? risk : null;
    const layers = _pcSession?.features?.layers ?? pred.layer_count;
    const previewFile = (rec.files || []).find(f => f.file_type === 'stl')
        || (rec.files || []).find(f => f.file_type === 'stl_supports');
    const fileRows = (rec.files || []).map(file => `<div class="pc-file-row"><span>${_esc(file.file_name)}</span><span>${file.file_type === 'stl' ? 'МОДЕЛЬ' : file.file_type === 'photo' ? 'ФОТО' : rec.session_id ? 'OK' : '—'}</span></div>`).join('') || '<div class="pc-file-row"><span>Файлы не приложены</span><span>—</span></div>';
    if (retainedPreview) retainedPreview.remove();
    else document.getElementById('pc-preview-host')?._homePreviewCleanup?.();
    document.getElementById('pcpane-what').innerHTML = `
        ${rec.metadata_json?.prediction && rec.prediction_is_current !== true
            ? `<p class="pc-source-note">${_pcPredictionFreshnessLabel(rec)}. Нужен новый расчёт; сохранённый прогноз доступен на вкладке «Оценка времени».</p>` : ''}
        <div class="pc-hero-layout"><div class="pc-hero-main"><div class="pc-preview-host" id="pc-preview-host"><div class="home-print-preview"><span class="home-preview-fallback">${_homePreviewFallback(0)}</span><span class="pc-preview-caption">ВИД 45° · ПЕРЕД + ЛЕВО + ВЕРХ<br>${_esc(previewFile?.file_name || 'STL не приложен')}</span><div class="pc-camera-switch"><button class="active" type="button">45°</button><button type="button">СВЕРХУ</button><button type="button">СПЕРЕДИ</button></div></div></div><div class="pc-metrics"><div class="pc-metric"><div class="pc-metric-label">ОЦЕНКА</div><div class="pc-metric-value">${predictedHours != null ? _formatHours(predictedHours) : '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">ФАКТ</div><div class="pc-metric-value">${actualHours != null ? _formatHours(actualHours) : '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">ОШИБКА</div><div class="pc-metric-value">—</div></div><div class="pc-metric"><div class="pc-metric-label">СЛОЁВ</div><div class="pc-metric-value">${layers ?? '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">РИСК БРАКА</div><div class="pc-metric-value" style="color:#f6a06b">—</div></div></div></div><aside class="pc-hero-side"><div><h3 class="pc-section-label">ДЕЙСТВИЯ</h3><button class="pc-side-action primary" type="button" onclick="showPrintCardTab('files')">Приложить логи</button><button class="pc-side-action" type="button" onclick="showPrintCardTab('files')">Загрузить фото пластины</button><button class="pc-side-action" type="button" onclick="showPrintCardTab('outcome')">Оценить качество</button><button class="pc-side-action" type="button" onclick="estimateRecord('${rec.record_id}',true)">Пересчитать оценку времени</button></div><div><h3 class="pc-section-label">СОСТАВ ПЕЧАТИ</h3>${fileRows}</div></aside></div>
        <div class="pc-grid">
            <div>
                <div class="pc-field">
                    <label>Название печати</label>
                    <input id="pc-name" class="mp-input" value="${_esc(rec.name)}">
                </div>
                <div class="pc-field">
                    <label>Материал</label>
                    <select id="pc-material" class="mp-input">${opts}</select>
                    <div class="hint important">Принтер не пишет материал в логи — это единственное место, где он задаётся. Без него не считается себестоимость.</div>
                </div>
                <div class="pc-field">
                    <label>Толщина слоя, мм</label>
                    <input id="pc-thickness" class="mp-input" type="number" min="0" max="1" step="0.005"
                           value="${_esc(rec.layer_thickness_mm)}" placeholder="по умолчанию — из параметров машины">
                    <div class="hint">Модель времени обучается отдельно на каждую пару «материал + толщина» и между ними не переносится.</div>
                </div>
                <div class="pc-field">
                    <label>Цена порошка, руб/кг</label>
                    <input id="pc-powder" class="mp-input" type="number" min="0" step="any"
                           value="${_esc(rec.powder_cost_rub_per_kg)}" placeholder="последняя использованная">
                </div>
                <div class="pc-field">
                    <label>Примечание</label>
                    <textarea id="pc-notes" class="mp-input" rows="2">${_esc(rec.notes)}</textarea>
                </div>
                <button class="quick-btn primary" onclick="savePrintCard()">Сохранить</button>
                <span id="pc-save-result" style="margin-left:10px;font-size:13px;"></span>
            </div>
            <div>
                <div class="pc-box">
                    <h4>Что на подложке</h4>
                    ${_pcPlatformRows()}
                </div>
            </div>
        </div>`;
    const metrics = document.querySelectorAll('#pcpane-what .pc-metric-value');
    const metricLabels = document.querySelectorAll('#pcpane-what .pc-metric-label');
    const comparisonLabel = timing.scope === 'burn_plus_pour' ? 'прожиг + нанесение' : 'нормальный машинный цикл';
    if (metricLabels[0]) metricLabels[0].textContent = timing.scope === 'burn_plus_pour' ? 'ОЦЕНКА ФАЗ' : 'ОЦЕНКА ЦИКЛА';
    if (metricLabels[1]) metricLabels[1].textContent = timing.scope === 'burn_plus_pour' ? 'ФАКТ ФАЗ' : 'ФАКТ ЦИКЛА';
    if (metricLabels[4]) metricLabels[4].textContent = _pcDefectRisk?.method === 'heuristic'
        ? 'ИНДЕКС РИСКА' : 'ОЦЕНКА МОДЕЛИ';
    if (metrics[0]) metrics[0].title = `Прогноз: ${comparisonLabel}`;
    if (metrics[1]) metrics[1].title = actualHours == null
        ? 'Нет сопоставимого факта с достаточным покрытием; частичная сумма и время по часам не используются.'
        : `Сопоставимый факт: ${comparisonLabel}; допуск проверен сервером.`;
    if (metrics[2]) metrics[2].textContent = predictionError == null ? '—' : `${predictionError > 0 ? '+' : ''}${predictionError.toFixed(1)}%`;
    if (metrics[4]) metrics[4].textContent = defectRisk == null ? '—' : `${Math.round(defectRisk * 100)}%`;
    if (metrics[4]) metrics[4].title = _pcDefectRisk?.in_sample
        ? 'Известный исход входил в обучение; это не независимый прогноз.'
        : _pcDefectRisk?.prediction?.explanation || 'Нет достаточных измерений для оценки риска.';
    const actionBox = document.querySelector('#pcpane-what .pc-hero-side > div');
    if (actionBox) actionBox.innerHTML = `<h3 class="pc-section-label">ДЕЙСТВИЯ</h3><label class="pc-side-action primary">Приложить логи<input type="file" multiple accept=".log,.zip" hidden onchange="uploadArchiveLogs('${rec.record_id}',this.files)"></label><label class="pc-side-action">Загрузить фото пластины<input type="file" accept="image/*" hidden onchange="uploadArchiveFile('${rec.record_id}',this.files[0])"></label><button class="pc-side-action" type="button" onclick="showPrintCardTab('outcome')">Оценить качество</button><button class="pc-side-action" type="button" onclick="estimateRecord('${rec.record_id}',true)">Пересчитать оценку времени</button>`;
    const previewHost = document.getElementById('pc-preview-host');
    if (retainedPreview) previewHost.replaceWith(retainedPreview);
    else _renderHomeStlPreview(previewHost, rec, previewFile);
    if (rec.metadata_json?.desktop_import_source) {
        const context = document.createElement('div');
        context.className = 'pc-box';
        const heading = document.createElement('h4');
        heading.textContent = 'Источники и ограничения';
        const description = document.createElement('div');
        description.style.whiteSpace = 'pre-wrap';
        description.textContent = rec.notes || 'Источники сохранены на вкладке «Файлы».';
        context.append(heading, description);
        if (rec.metadata_json.desktop_previous_prediction && !rec.metadata_json.prediction) {
            const warning = document.createElement('p');
            warning.textContent = 'Прежний прогноз сохранён отдельным документом. После импорта он не пересчитывался и не используется для оценки ошибки.';
            context.append(warning);
        }
        document.getElementById('pcpane-what').prepend(context);
    }
    liveValues.forEach(([id, value]) => {
        if (value !== undefined) document.getElementById(id).value = value;
    });
    fieldIds.forEach(id => {
        const field = document.getElementById(id);
        field?.addEventListener(id === 'pc-material' ? 'change' : 'input', () => { _pcDirty = true; _pcEditGeneration += 1; });
    });
    if (focus) {
        const field = document.getElementById(focus.id);
        field.focus({preventScroll: true});
        if (typeof focus.start === 'number' && typeof focus.end === 'number') {
            field.setSelectionRange(focus.start, focus.end, focus.direction);
        }
    }
}

function _pcPlatformRows() {
    const files = _pcRecord.files || [];
    const n = t => files.filter(f => f.file_type === t).length;
    const pred = _pcRecord.prediction_is_current === true ? (_pcRecord.metadata_json || {}).prediction || {} : {};
    const rows = [
        ['Деталей (STL)', n('stl') || '—'],
        ['Поддержек (STL)', n('stl_supports') || '—'],
        ['Magics-компоновок', n('magics') || '—'],
        ['Фото', n('photo') || '—'],
        ['Слоёв по геометрии', pred.layer_count ?? '—'],
    ];
    return rows.map(([k, v]) => `<div class="pc-kv"><span>${k}</span><span>${v}</span></div>`).join('');
}

async function savePrintCard() {
    if (!_pcRecord || _pcLoading) return;
    const record = _pcRecord, generation = _pcLoadGeneration, editGeneration = _pcEditGeneration;
    let result = document.getElementById('pc-save-result');
    const thickness = document.getElementById('pc-thickness').value;
    const powder = document.getElementById('pc-powder').value;
    try {
        const r = await fetch(`/prints/${record.record_id}`, {
            method: 'PATCH',
            headers: _jsonHeaders(),
            body: JSON.stringify({
                expected_revision: record.revision,
                name: document.getElementById('pc-name').value.trim(),
                material: document.getElementById('pc-material').value,
                layer_thickness_mm: thickness === '' ? null : Number(thickness),
                powder_cost_rub_per_kg: powder === '' ? null : Number(powder),
                notes: document.getElementById('pc-notes').value.trim(),
            }),
        });
        if (r.status === 409) {
            const conflict = (await r.json()).detail || {};
            throw new Error(`${conflict.message || 'Карточка уже изменена на другом ПК'}. Обновите карточку и повторите правку.`);
        }
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        const saved = await r.json();
        if (generation !== _pcLoadGeneration || _pcRecord?.record_id !== record.record_id
                || document.getElementById('page-print-card').style.display === 'none'
                || saved.revision < _pcRecord.revision) return;
        _pcRecord = { ..._pcRecord, ...saved };
        _pcDirty = editGeneration !== _pcEditGeneration;
        // Publish the committed projection now: same-revision SSE won't render
        // it again. Retain live edits and leave QC/process/files DOM untouched.
        _renderPcWhat(true);
        _renderPcPlan();
        _renderPredictionWarnings();
        result = document.getElementById('pc-save-result');
        result.innerHTML = `<span style="color:var(--status-success);">${_pcDirty ? 'Сохранено; есть новые несохранённые правки' : 'Сохранено'}</span>`;
        document.getElementById('pc-title').textContent = _pcRecord.name;
        setTimeout(() => { result.textContent = ''; }, 2500);
    } catch (e) {
        if (generation === _pcLoadGeneration && _pcRecord?.record_id === record.record_id
                && document.getElementById('page-print-card').style.display !== 'none') {
            result.innerHTML = `<span style="color:var(--status-danger);">Ошибка: ${_esc(e.message)}</span>`;
        }
    }
}

async function _refreshOpenPrintCardFromSync() {
    if (!_pcRecord || _pcLoading || document.getElementById('page-print-card').style.display === 'none') return;
    const recordId = _pcRecord.record_id, generation = _pcLoadGeneration;
    const dirtyMessage = '<span style="color:var(--status-warning);">Карточка изменена на другом ПК. Ваши поля не перезаписаны — обновите карточку перед сохранением.</span>';
    try {
        const fresh = await fetch(`/prints/${recordId}`).then(r => r.ok ? r.json() : null);
        if (generation !== _pcLoadGeneration || _pcLoading || _pcRecord?.record_id !== recordId
                || document.getElementById('page-print-card').style.display === 'none') return;
        if (!fresh || fresh.revision <= _pcRecord.revision) return;
        let result = document.getElementById('pc-save-result');
        if (_pcDirty || _pcQualityDirty) {
            if (result) result.innerHTML = dirtyMessage;
            return;
        }
        const [session, operatorReport, defectRisk] = await Promise.all([
            fresh.session_id
                ? fetch(`/sessions/${fresh.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
            fetch(`/prints/${recordId}/operator-report`).then(r => r.ok ? r.json() : null).catch(() => null),
            fresh.session_id
                ? fetch(`/analysis/defect-risk/${fresh.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
        ]);
        if (generation !== _pcLoadGeneration || _pcLoading || _pcRecord?.record_id !== recordId
                || document.getElementById('page-print-card').style.display === 'none'
                || fresh.revision <= _pcRecord.revision) return;
        if (_pcDirty || _pcQualityDirty) {
            result = document.getElementById('pc-save-result');
            if (result) result.innerHTML = dirtyMessage;
            return;
        }
        const activeTab = document.querySelector('#pc-tabs a.active')?.id?.replace('pctab-', '') || 'what';
        [_pcRecord, _pcSession, _pcOperatorReport, _pcDefectRisk] = [fresh, session, operatorReport, defectRisk];
        _renderPrintCard();
        showPrintCardTab(activeTab);
        const refreshedResult = document.getElementById('pc-save-result');
        if (refreshedResult) refreshedResult.innerHTML = '<span style="color:var(--status-success);">Обновлено с другого рабочего места</span>';
    } catch (e) { /* EventSource переподключится; ручное обновление остаётся доступно. */ }
}

function _startPrintSync() {
    if (!globalThis.EventSource) return;
    const source = new EventSource('/prints/events');
    source.addEventListener('print-records', () => {
        if (!_printSyncPrimed) { _printSyncPrimed = true; return; }
        clearTimeout(_printSyncTimer);
        _printSyncTimer = setTimeout(() => {
            if (document.getElementById('page-archive').style.display !== 'none') loadArchive(_archiveSkip);
            _refreshOpenPrintCardFromSync();
        }, 250);
    });
}

_startPrintSync();

function _renderPcPlan() {
    const pred = (_pcRecord.metadata_json || {}).prediction;
    const pane = document.getElementById('pcpane-plan');
    if (!pred) {
        const hasStl = (_pcRecord.files || []).some(f => f.file_type === 'stl');
        pane.innerHTML = `<div class="pc-empty">${_pcRecord.metadata_json?.desktop_previous_prediction
            ? 'Прежний прогноз сохранён в файлах карточки. Для нового расчёта подтвердите материал, режим, поддержки и начало координат построения.' : hasStl
            ? 'Прогноз ещё не рассчитан — нажмите «Пересчитать прогноз».'
            : 'Чтобы получить прогноз, приложите STL деталей на вкладке «Файлы».'}</div>`;
        return;
    }
    // scan_source says whether the time came from a model fitted on this
    // shop's own logs or from passport physics, so provenance is visible.
    const fitted = pred.scan_source === 'fitted';
    const current = _pcRecord.prediction_is_current === true;
    const configurationCurrent = _pcRecord.prediction_configuration_is_current;
    const configurationNote = configurationCurrent === true
        ? 'Настройки расчёта совпадают с текущими.'
        : configurationCurrent === false
            ? 'Настройки расчёта изменились. Для новой оценки выполните перерасчёт.'
            : 'Совпадение с текущими настройками не подтверждено.';
    const recalculateButton = '<button class="quick-btn primary" type="button" data-pc-prediction-action="recalculate">Пересчитать оценку времени</button>';
    const timing = _pcTimeMetrics(_pcSession, pred, _pcRecord.summary, _pcRecord.prediction_is_current);
    const fullCycle = timing.scope === 'machine_cycle' && timing.predictedHours != null;
    const machineHours = current ? (fullCycle ? timing.predictedHours : timing.predictedSubtotalHours)
        : Number.isFinite(pred.print_hours) && pred.print_hours >= 0 ? pred.print_hours : null;
    const recoatSource = pred.recoat_time_source === 'calibrated'
        ? 'по фактическим логам'
        : pred.recoat_time_source === 'manual' ? 'задано оператором' : 'значение по умолчанию';
    const plan = `
        <div class="pc-grid">
            <div class="pc-box">
                <h4>${current && configurationCurrent === true ? 'Прогноз по текущим настройкам' : 'Сохранённый прогноз'}</h4>
                ${current ? `<p class="pc-source-note">${_esc(configurationNote)}</p>${recalculateButton}` : ''}
                <div class="pc-kv"><span>${fullCycle ? 'Нормальный машинный цикл' : 'Прожиг + нанесение'}</span><span>${machineHours != null ? Number(machineHours.toFixed(3)) + ' ч' : '—'}</span></div>
                <div style="color:var(--text-muted);font-size:11px;margin:6px 0 10px;">Операторские паузы и перезапуски в прогноз не входят.</div>
                <div class="pc-kv"><span>Себестоимость</span><span>${pred.cost_total_rub != null ? Math.round(pred.cost_total_rub).toLocaleString('ru') + ' ₽' : '—'}</span></div>
                <div class="pc-kv"><span>Слоёв</span><span>${pred.layer_count ?? '—'}</span></div>
                <div class="pc-kv"><span>Деталей / поддержек</span><span>${pred.n_parts ?? '—'} / ${pred.n_supports ?? '—'}</span></div>
            </div>
            <div class="pc-box">
                <h4>Откуда цифра</h4>
                <div class="pc-kv"><span>Модель скана</span><span style="color:${fitted ? 'var(--status-success)' : 'var(--status-warning)'};">
                    ${fitted ? 'подогнана по логам' : 'паспортная физика'}</span></div>
                <div class="pc-kv"><span>Поправка по материалу</span><span>×${pred.correction_factor ?? 1}</span></div>
                <div class="pc-kv"><span>Машина</span><span>${pred.printer_id ? _esc(pred.printer_id) : 'не указана в сохранённом расчёте'}</span></div>
                <div class="pc-kv"><span>Материал</span><span>${_esc(_materialRu(pred.material || _pcRecord.material))}</span></div>
                <div class="pc-kv"><span>Толщина / лазеры</span><span>${pred.layer_thickness_mm ?? '—'} мм / ${pred.laser_count ?? '—'}</span></div>
                <div class="pc-kv"><span>Нанесение порошка</span><span>${pred.recoat_time_ms != null ? (pred.recoat_time_ms / 1000).toFixed(2) + ' с/слой' : '—'} · ${recoatSource}</span></div>
                <div class="pc-kv"><span>Базовая задержка контроллера</span><span>${pred.layer_overhead_ms != null ? (pred.layer_overhead_ms / 1000).toFixed(3) + ' с/слой · ' + (pred.layer_overhead_n_prints || 0) + ' печатей / ' + (pred.layer_overhead_n_layers || 0) + ' слоёв' : 'ещё не откалибровано'}</span></div>
                <div class="pc-kv"><span>Минимальный цикл слоя</span><span>${pred.minimum_layer_cycle_ms != null ? (pred.minimum_layer_cycle_ms / 1000).toFixed(3) + ' с · сработал на ' + (pred.minimum_cycle_active_layers || 0) + ' слоях' : 'не выявлен по истории'}</span></div>
                <div class="pc-kv"><span>Рассчитано</span><span>${pred.estimated_at ? new Date(pred.estimated_at).toLocaleString('ru') : '—'}</span></div>
                ${fitted ? '' : `<div style="color:var(--text-muted);font-size:12px;margin-top:10px;">
                    Модель по логам появится после печатей как минимум двух разных компоновок этого режима (материал + толщина).</div>`}
            </div>
        </div>`;
    if (current) {
        pane.innerHTML = plan;
    } else {
        const label = _pcPredictionFreshnessLabel(_pcRecord);
        const warnings = (pred.prediction_warnings || []).map(text => `<p>${_esc(text)}</p>`).join('');
        pane.innerHTML = `<div class="pc-box"><h3>${label}</h3>
            <p class="pc-source-note">Нужен новый расчёт. Сохранённые время, стоимость и геометрия не используются как текущая оценка.</p>
            <p class="pc-source-note">${_esc(configurationNote)}</p>
            ${recalculateButton}
            <button class="quick-btn" type="button" data-pc-prediction-action="files">Файлы</button></div>
            <details class="pc-disclosure"><summary>Сохранённый прогноз · ${label === 'Прогноз устарел' ? 'устарел' : 'актуальность не подтверждена'}</summary>
                <p class="pc-source-note">Исторический снимок расчёта; значения относятся к сохранённым входам.</p>
                ${plan}${warnings ? `<div class="pc-box"><h4>Ограничения сохранённого расчёта</h4>${warnings}</div>` : ''}
            </details>`;
    }
    const recordId = _pcRecord.record_id;
    pane.querySelectorAll('[data-pc-prediction-action]').forEach(button => {
        button.addEventListener('click', () => button.dataset.pcPredictionAction === 'recalculate'
            ? estimateRecord(recordId, true) : showPrintCardTab('files'));
    });
}

function _renderPredictionWarnings() {
    if (_pcRecord?.prediction_is_current !== true) return;
    const warnings = _pcRecord?.metadata_json?.prediction?.prediction_warnings || [];
    if (!warnings.length) return;
    const box = document.createElement('div');
    box.className = 'pc-box';
    const title = document.createElement('h4');
    title.textContent = 'Ограничения расчёта';
    box.append(title);
    warnings.forEach(text => { const line = document.createElement('p'); line.textContent = text; box.append(line); });
    document.getElementById('pcpane-plan').append(box);
}
