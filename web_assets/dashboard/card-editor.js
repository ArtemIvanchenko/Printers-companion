// PRINT CARD — one print, everything about it
// =========================================================
let _pcRecord = null;   // the record being viewed
let _pcSession = null;  // its linked log session, if any
let _pcOperatorReport = null; // compact deterministic conclusion
let _pcDefectRisk = null; // explainable model/heuristic output for this session
let _pcDirty = false;   // unsaved local edits must never be overwritten by sync
let _printSyncPrimed = false;
let _printSyncTimer = null;

function _pcTimeMetrics(session, prediction = {}, summary = {}) {
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
    const predictedHours = compatible ? hours(summary.predicted_hours, 1) : hours(prediction.machine_cycle_hours, 1);
    return {
        subtotalHours: hours(features.machine_min, 60),
        wallHours: hours(features.duration_min, 60),
        measuredHours, actualHours, predictedHours, scope: compatible ? scope : null,
        comparisonReason: typeof summary.comparison_reason_ru === 'string' ? summary.comparison_reason_ru : '',
        predictedSubtotalHours: hours(prediction.print_hours, 1),
        normalLayers: Number.isInteger(normalLayers) && normalLayers >= 0 ? normalLayers : null,
        explicitPauseHours: hours(features.explicit_pause_seconds ?? accounting.explicit_pause_seconds, 3600),
        unattributedHours: hours(features.unattributed_elapsed_seconds, 3600),
        errorPct: predictedHours != null && actualHours != null && typeof summary.error_pct === 'number'
            && Number.isFinite(summary.error_pct) ? summary.error_pct : null,
    };
}

async function openPrintCard(recordId) {
    _hideAllPages();
    _showSubnav(null);
    _setMainActive('prints');
    document.getElementById('page-print-card').style.display = 'block';
    window.scrollTo(0, 0);

    document.getElementById('pc-title').textContent = 'Загрузка…';
    document.getElementById('pc-subtitle').textContent = '';
    try {
        _pcRecord = await fetch(`/prints/${recordId}`).then(r => r.json());
        _pcDirty = false;
        // The outcome lives on the session, so it is fetched alongside —
        // "Как прошло" and "Процесс" are unanswerable without it.
        [_pcSession, _pcOperatorReport, _pcDefectRisk] = await Promise.all([
            _pcRecord.session_id
                ? fetch(`/sessions/${_pcRecord.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
            fetch(`/prints/${recordId}/operator-report`).then(r => r.ok ? r.json() : null).catch(() => null),
            _pcRecord.session_id
                ? fetch(`/analysis/defect-risk/${_pcRecord.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
                : Promise.resolve(null),
        ]);
    } catch (e) {
        document.getElementById('pc-title').textContent = 'Не удалось загрузить карточку';
        return;
    }
    _renderPrintCard();
    showPrintCardTab('what');
    if (_pcRecord.estimate_job) {
        window._pendingEstimateRequests ||= new Set();
        if (!window._pendingEstimateRequests.has(recordId)) {
            window._pendingEstimateRequests.add(recordId);
            _pollEstimate(recordId, _pcRecord.estimate_job.job_id, true);
        }
    }
}

function _renderPrintCard() {
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
}

function _renderPcWhat() {
    const rec = _pcRecord;
    const materials = (window._printMaterials || []);
    const opts = materials.length
        ? materials.map(m => `<option value="${_esc(m)}" ${m === rec.material ? 'selected' : ''}>${_esc(_materialRu(m))}</option>`).join('')
        : `<option value="${_esc(rec.material)}" selected>${_esc(_materialRu(rec.material))}</option>`;

    const pred = (rec.metadata_json || {}).prediction || {};
    const timing = _pcTimeMetrics(_pcSession, pred, rec.summary);
    const actualHours = timing.actualHours;
    const predictedHours = timing.predictedHours;
    const predictionError = timing.errorPct;
    const defectRisk = _pcDefectRisk?.risk_score ?? _pcDefectRisk?.probability ?? null;
    const layers = _pcSession?.features?.layers ?? pred.layer_count;
    const previewFile = (rec.files || []).find(f => f.file_type === 'stl')
        || (rec.files || []).find(f => f.file_type === 'stl_supports');
    const fileRows = (rec.files || []).map(file => `<div class="pc-file-row"><span>${_esc(file.file_name)}</span><span>${file.file_type === 'stl' ? 'МОДЕЛЬ' : file.file_type === 'photo' ? 'ФОТО' : rec.session_id ? 'OK' : '—'}</span></div>`).join('') || '<div class="pc-file-row"><span>Файлы не приложены</span><span>—</span></div>';
    document.getElementById('pcpane-what').innerHTML = `
        <div class="pc-hero-layout"><div class="pc-hero-main"><div class="pc-preview-host" id="pc-preview-host"><div class="home-print-preview"><span class="home-preview-fallback">${_homePreviewFallback(0)}</span><span class="pc-preview-caption">ВИД 45° · ПЕРЕД + ЛЕВО + ВЕРХ<br>${_esc(previewFile?.file_name || 'STL не приложен')}</span><div class="pc-camera-switch"><button class="active" type="button">45°</button><button type="button">СВЕРХУ</button><button type="button">СПЕРЕДИ</button></div></div></div><div class="pc-metrics"><div class="pc-metric"><div class="pc-metric-label">ОЦЕНКА</div><div class="pc-metric-value">${predictedHours != null ? _formatHours(predictedHours) : '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">ФАКТ</div><div class="pc-metric-value">${actualHours != null ? _formatHours(actualHours) : '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">ОШИБКА</div><div class="pc-metric-value">—</div></div><div class="pc-metric"><div class="pc-metric-label">СЛОЁВ</div><div class="pc-metric-value">${layers ?? '—'}</div></div><div class="pc-metric"><div class="pc-metric-label">РИСК БРАКА</div><div class="pc-metric-value" style="color:#f6a06b">—</div></div></div></div><aside class="pc-hero-side"><div><h3 class="pc-section-label">ДЕЙСТВИЯ</h3><button class="pc-side-action primary" type="button" onclick="openNewPrintRecord()">Приложить логи</button><button class="pc-side-action" type="button" onclick="showPrintCardTab('files')">Загрузить фото пластины</button><button class="pc-side-action" type="button" onclick="showPrintCardTab('outcome')">Оценить качество</button><button class="pc-side-action" type="button" onclick="estimateRecord('${rec.record_id}',true)">Пересчитать оценку времени</button></div><div><h3 class="pc-section-label">СОСТАВ ПЕЧАТИ</h3>${fileRows}</div></aside></div>
        <div class="pc-grid" style="display:none;">
            <div class="pc-preview-host" id="pc-preview-host">
                <div class="home-print-preview">
                    <span class="home-preview-fallback">${_homePreviewFallback(0)}</span>
                    <span class="home-preview-type">${(rec.files || []).some(f => f.file_type === 'stl') ? 'STL · 3D' : 'ПРЕВЬЮ НЕДОСТУПНО'}</span>
                    <span class="pc-preview-caption">ВИД 45° · ПЕРЕД + ЛЕВО + ВЕРХ</span>
                </div>
            </div>
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
    if (metrics[0]) metrics[0].title = `Прогноз: ${comparisonLabel}`;
    if (metrics[1]) metrics[1].title = actualHours == null
        ? 'Нет сопоставимого факта с достаточным покрытием; частичная сумма и время по часам не используются.'
        : `Сопоставимый факт: ${comparisonLabel}; допуск проверен сервером.`;
    if (metrics[2]) metrics[2].textContent = predictionError == null ? '—' : `${predictionError > 0 ? '+' : ''}${predictionError.toFixed(1)}%`;
    if (metrics[4]) metrics[4].textContent = defectRisk == null ? '—' : `${Math.round(defectRisk * 100)}%`;
    const actionBox = document.querySelector('#pcpane-what .pc-hero-side > div');
    if (actionBox) actionBox.innerHTML = `<h3 class="pc-section-label">ДЕЙСТВИЯ</h3><label class="pc-side-action primary">Приложить логи<input type="file" multiple accept=".log,.zip" hidden onchange="uploadArchiveLogs('${rec.record_id}',this.files)"></label><label class="pc-side-action">Загрузить фото пластины<input type="file" accept="image/*" hidden onchange="uploadArchiveFile('${rec.record_id}',this.files[0])"></label><button class="pc-side-action" type="button" onclick="showPrintCardTab('outcome')">Оценить качество</button><button class="pc-side-action" type="button" onclick="estimateRecord('${rec.record_id}',true)">Пересчитать оценку времени</button>`;
    const previewHost = document.getElementById('pc-preview-host');
    _renderHomeStlPreview(previewHost, rec, previewFile);
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
    ['pc-name', 'pc-material', 'pc-thickness', 'pc-powder', 'pc-notes'].forEach(id => {
        const field = document.getElementById(id);
        field?.addEventListener(id === 'pc-material' ? 'change' : 'input', () => { _pcDirty = true; });
    });
}

function _pcPlatformRows() {
    const files = _pcRecord.files || [];
    const n = t => files.filter(f => f.file_type === t).length;
    const pred = (_pcRecord.metadata_json || {}).prediction || {};
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
    const result = document.getElementById('pc-save-result');
    const thickness = document.getElementById('pc-thickness').value;
    const powder = document.getElementById('pc-powder').value;
    try {
        const r = await fetch(`/prints/${_pcRecord.record_id}`, {
            method: 'PATCH',
            headers: _jsonHeaders(),
            body: JSON.stringify({
                expected_revision: _pcRecord.revision,
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
        _pcRecord = { ..._pcRecord, ...(await r.json()) };
        _pcDirty = false;
        result.innerHTML = '<span style="color:var(--status-success);">Сохранено</span>';
        document.getElementById('pc-title').textContent = _pcRecord.name;
        setTimeout(() => { result.textContent = ''; }, 2500);
    } catch (e) {
        result.innerHTML = `<span style="color:var(--status-danger);">Ошибка: ${_esc(e.message)}</span>`;
    }
}

async function _refreshOpenPrintCardFromSync() {
    if (!_pcRecord || document.getElementById('page-print-card').style.display === 'none') return;
    try {
        const fresh = await fetch(`/prints/${_pcRecord.record_id}`).then(r => r.ok ? r.json() : null);
        if (!fresh || fresh.revision === _pcRecord.revision) return;
        const result = document.getElementById('pc-save-result');
        if (_pcDirty) {
            if (result) result.innerHTML = '<span style="color:var(--status-warning);">Карточка изменена на другом ПК. Ваши поля не перезаписаны — обновите карточку перед сохранением.</span>';
            return;
        }
        const activeTab = document.querySelector('#pc-tabs a.active')?.id?.replace('pctab-', '') || 'what';
        _pcRecord = fresh;
        _pcSession = fresh.session_id
            ? await fetch(`/sessions/${fresh.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
            : null;
        _pcOperatorReport = await fetch(`/prints/${fresh.record_id}/operator-report`)
            .then(r => r.ok ? r.json() : null).catch(() => null);
        _pcDefectRisk = fresh.session_id
            ? await fetch(`/analysis/defect-risk/${fresh.session_id}`).then(r => r.ok ? r.json() : null).catch(() => null)
            : null;
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
    const fullCycle = Object.prototype.hasOwnProperty.call(pred, 'machine_cycle_hours');
    const machineHours = fullCycle ? pred.machine_cycle_hours : pred.print_hours;
    const recoatSource = pred.recoat_time_source === 'calibrated'
        ? 'по фактическим логам'
        : pred.recoat_time_source === 'manual' ? 'задано оператором' : 'значение по умолчанию';
    pane.innerHTML = `
        <div class="pc-grid">
            <div class="pc-box">
                <h4>Прогноз</h4>
                <div class="pc-kv"><span>${fullCycle ? 'Нормальный машинный цикл' : 'Прожиг + нанесение (старый подытог)'}</span><span>${machineHours != null ? _esc(machineHours) + ' ч' : '—'}</span></div>
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
                <div class="pc-kv"><span>Машина</span><span>${pred.printer_id ? _esc(pred.printer_id) : 'текущая машина из настроек'}</span></div>
                <div class="pc-kv"><span>Материал</span><span>${_materialRu(pred.material || _pcRecord.material)}</span></div>
                <div class="pc-kv"><span>Толщина / лазеры</span><span>${pred.layer_thickness_mm ?? '—'} мм / ${pred.laser_count ?? '—'}</span></div>
                <div class="pc-kv"><span>Нанесение порошка</span><span>${pred.recoat_time_ms != null ? (pred.recoat_time_ms / 1000).toFixed(2) + ' с/слой' : '—'} · ${recoatSource}</span></div>
                <div class="pc-kv"><span>Базовая задержка контроллера</span><span>${pred.layer_overhead_ms != null ? (pred.layer_overhead_ms / 1000).toFixed(3) + ' с/слой · ' + (pred.layer_overhead_n_prints || 0) + ' печатей / ' + (pred.layer_overhead_n_layers || 0) + ' слоёв' : 'ещё не откалибровано'}</span></div>
                <div class="pc-kv"><span>Минимальный цикл слоя</span><span>${pred.minimum_layer_cycle_ms != null ? (pred.minimum_layer_cycle_ms / 1000).toFixed(3) + ' с · сработал на ' + (pred.minimum_cycle_active_layers || 0) + ' слоях' : 'не выявлен по истории'}</span></div>
                <div class="pc-kv"><span>Рассчитано</span><span>${pred.estimated_at ? new Date(pred.estimated_at).toLocaleString('ru') : '—'}</span></div>
                ${fitted ? '' : `<div style="color:var(--text-muted);font-size:12px;margin-top:10px;">
                    Модель по логам появится после печатей как минимум двух разных компоновок этого режима (материал + толщина).</div>`}
            </div>
        </div>`;
}

function _renderPredictionWarnings() {
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
