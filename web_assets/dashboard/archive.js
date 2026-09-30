// ARCHIVE — print records + machine params
// =========================================================
const _MATERIAL_RU  = { steel: 'Сталь', aluminum: 'Алюминий', titanium: 'Титан', other: 'Другое' };
const _STATUS_LABEL = { draft: 'Черновик', active: 'Печатается', completed: 'Завершена' };
const _STATUS_COLOR = { draft: '#6b7280', active: '#f59e0b', completed: '#10b981' };
const _FILE_ICON    = { stl: '🧊', stl_supports: '🏗', magics: '🧩', photo: '📷', doc: '📄' };

function _statusBadge(status) {
    const label = _esc(_STATUS_LABEL[status] || status);
    const color = _STATUS_COLOR[status] || '#4a5568';
    return `<span style="background:${color}22;color:${color};border:1px solid ${color}55;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap;">${label}</span>`;
}
const _ARCHIVE_PAGE_SIZE = 20;
let _archiveSkip = 0;

function _materialRu(m) { return _MATERIAL_RU[m] || m; }

function toggleArchiveForm() {
    const el = document.getElementById('archive-form');
    const opening = el.style.display === 'none';
    el.style.display = opening ? 'block' : 'none';
    if (opening) loadPrintDefaults();
}

function toggleMachineParams() {
    const el = document.getElementById('machine-params-form');
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function loadPrintDefaults() {
    try {
        const d = await fetch('/prints/defaults').then(r => r.json());
        // Shared with the print card, which builds its own material
        // select from the same list rather than refetching.
        window._printMaterials = d.materials || [];
        const sel = document.getElementById('ar-material');
        const current = sel.value;
        sel.innerHTML = d.materials.map(m => `<option value="${_esc(m)}">${_esc(_materialRu(m))}</option>`).join('');
        if (current && d.materials.includes(current)) sel.value = current;
        // Filter select shares the material list
        const filt = document.getElementById('ar-filter-material');
        const filtCurrent = filt.value;
        filt.innerHTML = '<option value="">Все</option>' +
            d.materials.map(m => `<option value="${_esc(m)}">${_esc(_materialRu(m))}</option>`).join('');
        filt.value = filtCurrent;
        const cost = document.getElementById('ar-powder-cost');
        if (!cost.value && d.powder_cost_rub_per_kg != null) cost.value = d.powder_cost_rub_per_kg;
    } catch (e) { /* форма работает и без дефолтов */ }
}

async function createPrintRecord() {
    const name = document.getElementById('ar-name').value.trim();
    const result = document.getElementById('ar-form-result');
    if (!name) { result.innerHTML = '<span style="color:#ef4444;">Введите название изделия</span>'; return; }
    const printedAt = document.getElementById('ar-printed-at').value;
    const powderCost = document.getElementById('ar-powder-cost').value;
    const thickness = document.getElementById('ar-layer-thickness').value;
    try {
        const r = await fetch('/prints', {
            method: 'POST',
            headers: _jsonHeaders(),
            body: JSON.stringify({
                name,
                material: document.getElementById('ar-material').value,
                layer_thickness_mm: thickness === '' ? null : Number(thickness),
                notes: document.getElementById('ar-notes').value.trim(),
                printed_at: printedAt || null,
                powder_cost_rub_per_kg: powderCost === '' ? null : Number(powderCost),
            }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        result.innerHTML = '<span style="color:#10b981;">Карточка создана</span>';
        document.getElementById('ar-name').value = '';
        document.getElementById('ar-notes').value = '';
        document.getElementById('ar-printed-at').value = '';
        document.getElementById('ar-layer-thickness').value = '';
        loadArchive();
    } catch (e) {
        result.innerHTML = `<span style="color:#ef4444;">Ошибка: ${_esc(e.message)}</span>`;
    }
}

// The estimate now runs server-side in the background: co-hatching a real
// plate takes minutes, and holding the request open let a browser or
// proxy timeout decide whether the result survived. The endpoint returns
// "started" immediately, so the result is polled for.
//
// stayOnCard: called from the print card, where reloading the list would
// navigate the operator away from what they were looking at.
async function estimateRecord(recordId, stayOnCard = false) {
    if (window._pendingEstimateRequests?.has(recordId)) return;
    window._pendingEstimateRequests ||= new Set();
    window._pendingEstimateRequests.add(recordId);
    let queued = false;
    _setEstimateStatus('Проверяем данные для расчёта…');
    try {
        const r = await fetch(`/prints/${recordId}/estimate`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : d.detail?.message || JSON.stringify(d.detail || r.status));
        showToast('Расчёт поставлен в очередь на этом ПК', 'success');
        queued = true;
        _pollEstimate(recordId, d.job_id, stayOnCard);
    } catch (e) {
        _setEstimateStatus('Расчёт не выполнен: ' + e.message);
        showToast('Ошибка расчёта: ' + e.message, 'error');
    } finally { if (!queued) window._pendingEstimateRequests.delete(recordId); }
}

// Poll the durable job itself: timestamp-only polling could wait ten
// minutes on an idempotently returned completed job or hide a failure.
async function _pollEstimate(recordId, jobId, stayOnCard, attempt = 0) {
    const refresh = () => stayOnCard ? openPrintCard(recordId) : loadArchive(_archiveSkip);
    if (attempt === 0) _setEstimateStatus('Расчёт в очереди на этом ПК. Ожидаем вычислительный процесс…');
    if (attempt >= 120) {          // 120 × 5 s = 10 минут
        window._pendingEstimateRequests?.delete(recordId);
        _setEstimateStatus('Ожидание длится больше 10 минут. Проверьте состояние задания; оно сохранено в очереди.');
        return;
    }
    if (attempt > 0) await new Promise(res => setTimeout(res, 5000));
    try {
        const response = await fetch(`/background-analysis/jobs/${jobId}`);
        if (!response.ok) throw new Error(response.status);
        const job = await response.json();
        if (job.status === 'done') {
            window._pendingEstimateRequests?.delete(recordId);
            _setEstimateStatus('Расчёт готов. Результат сохранён в карточке.');
            refresh();
            return;
        }
        if (job.status === 'failed') {
            window._pendingEstimateRequests?.delete(recordId);
            _setEstimateStatus(`❌ Расчёт не выполнен: ${job.error || 'неизвестная ошибка'}`);
            return;
        }
        _setEstimateStatus(job.status === 'running'
            ? 'Вычисляем время печати по геометрии на этом ПК…'
            : 'Расчёт в очереди. Ожидаем освобождения вычислительного процесса.');
    } catch (e) { /* сеть моргнула — продолжаем опрос */ }
    _pollEstimate(recordId, jobId, stayOnCard, attempt + 1);
}

function _setEstimateStatus(text) {
    const el = document.getElementById('estimate-status');
    if (el) { el.textContent = text || ''; el.hidden = !text; }
}

async function deletePrintRecord(recordId, name) {
    if (!confirm(`Удалить карточку «${name}» со всеми файлами? Это действие необратимо.`)) return;
    try {
        const r = await fetch(`/prints/${encodeURIComponent(recordId)}`, { method: 'DELETE' });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        loadArchive(_archiveSkip);
    } catch (e) { showToast('Ошибка удаления: ' + e.message, 'error'); }
}

async function deleteArchiveFile(recordId, fileId, name) {
    if (!confirm(`Удалить файл «${name}»?`)) return;
    try {
        const r = await fetch(`/prints/${encodeURIComponent(recordId)}/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        loadArchive(_archiveSkip);
    } catch (e) { showToast('Ошибка удаления файла: ' + e.message, 'error'); }
}

function _archiveFilters() {
    const q  = document.getElementById('ar-search').value.trim();
    const m  = document.getElementById('ar-filter-material').value;
    const df = document.getElementById('ar-date-from').value;
    const dt = document.getElementById('ar-date-to').value;
    let qs = '';
    if (q)  qs += `&q=${encodeURIComponent(q)}`;
    if (m)  qs += `&material=${encodeURIComponent(m)}`;
    if (df) qs += `&date_from=${df}`;
    if (dt) qs += `&date_to=${dt}T23:59:59`;
    return qs;
}

// Predicted-vs-actual, per material. Three separate calibrations feed
// this: the blanket scan factor, recoat time, and the fitted per-mode
// scan model — they are shown apart because they fail apart.
async function loadMachineAccuracy() {
    const el = document.getElementById('machine-accuracy-body');
    try {
        const [d, mp] = await Promise.all([
            fetch('/prints/prediction-accuracy').then(r => r.json()),
            fetch('/settings/machine').then(r => r.json()),
        ]);
        const locked = !!(mp.params && mp.params.correction_locked);
        const byMat = (mp.params && mp.params.time_correction_by_mat) || {};

        if (!d.n_pairs) {
            el.innerHTML = `<div class="pc-empty">
                Пока нет ни одной пары «прогноз + факт».<br>
                Нужны печати, у которых есть и рассчитанный прогноз, и привязанные логи —
                минимум ${d.min_pairs_for_calibration} на материал, чтобы поправка начала применяться.</div>`;
            return;
        }

        const rows = (d.pairs || []).slice(0, 12).map(p => {
            const col = p.error_pct == null ? 'var(--text-muted)'
                      : Math.abs(p.error_pct) <= 10 ? 'var(--status-success)'
                      : Math.abs(p.error_pct) <= 25 ? 'var(--status-warning)' : 'var(--status-danger)';
            // A wall-span actual carries operator pauses the estimate
            // never modelled — flagged rather than silently averaged in.
            const src = p.actual_source === 'wall_span'
                ? ' <span style="color:var(--status-warning);" title="Факт по интервалу сессии, включая паузы">≈</span>' : '';
            return `<tr>
                <td>${_esc(p.name || p.record_id)}</td>
                <td>${_esc(_materialRu(p.material))}</td>
                <td>${_formatHours(p.predicted_hours)}</td>
                <td>${p.actual_hours != null ? _formatHours(p.actual_hours) : 'Нет данных'}${src}</td>
                <td style="color:${col};font-weight:600;">${p.error_pct != null ? `${p.error_pct > 0 ? '+' : ''}${_esc(p.error_pct)}%` : '—'}</td>
                <td style="font-size:12px;color:var(--text-muted);">${p.used_for_calibration ? 'учтена' : _esc(p.excluded_reason_ru || 'Недостаточно подтверждений')}</td>
            </tr>`;
        }).join('');

        const mats = new Set([...Object.keys(d.by_material || {}), ...Object.keys(byMat)]);
        const matRows = [...mats].map(m => {
            const info = (d.by_material || {})[m] || {};
            const applied = byMat[m];
            return `<div class="pc-kv">
                <span>${_materialRu(m)}</span>
                <span>${applied != null ? `×${applied}` : '×1.00'}
                    <span style="font-weight:400;color:var(--text-muted);font-size:12px;">
                    ${info.n_pairs ? `· ${info.n_pairs} печ.` : ''}${info.suggested_factor != null ? ` · рекоменд. ×${info.suggested_factor}` : ''}</span>
                </span></div>`;
        }).join('');

        const scan = d.scan || {};
        const scanRows = Object.entries(scan.candidates || {}).map(([mode, m]) => `
            <div class="pc-kv"><span>${mode}</span>
            <span style="color:${m.status === 'ok' ? 'var(--status-success)' : 'var(--text-muted)'};">
                ${m.status === 'ok' ? `R²=${m.r2}, ${m.n_layers} слоёв` : (m.status || '—')}</span></div>`).join('')
            || '<div style="color:var(--text-muted);font-size:13px;">Нет подогнанных моделей — нужны печати с логами по каждому режиму.</div>';

        el.innerHTML = `
            <div class="pc-grid" style="margin-bottom:20px;">
                <div class="pc-box">
                    <h4>Поправка по материалам</h4>
                    ${matRows || '<div style="color:var(--text-muted);font-size:13px;">Данных пока нет.</div>'}
                    <div style="margin-top:12px;font-size:12px;">
                        ${locked
                            ? `🔒 Поправки зафиксированы вручную. <a onclick="setCorrectionLock(false)" style="color:var(--color-accent);cursor:pointer;">включить авто</a>`
                            : `<a onclick="recalibrateNow()" style="color:var(--color-accent);cursor:pointer;">пересчитать сейчас</a>
                               · <a onclick="setCorrectionLock(true)" style="color:var(--text-muted);cursor:pointer;">зафиксировать</a>`}
                    </div>
                </div>
                <div class="pc-box">
                    <h4>Модели скана по режимам</h4>
                    <p class="muted">${_esc(scan.status_ru || '')}</p>
                    ${scanRows}
                    <div style="margin-top:10px;font-size:12px;color:var(--text-muted);">
                        Модель обучается на реальном времени прожига слоёв и применяется только к своему режиму
                        «материал + толщина» — на чужой режим она не переносится.</div>
                </div>
            </div>
            <p class="muted">${_esc(d.comparison_basis_ru || 'Сравнение прожига и нанесения порошка.')} Это история расчётов, не проверка точности на новых печатях.</p>
            <div style="overflow-x:auto;">
                <table>
                    <thead><tr><th>Печать</th><th>Материал</th><th>Прогноз</th><th>Факт</th><th>Расхождение</th><th>В калибровке</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    } catch (e) {
        el.innerHTML = `<div class="pc-empty" style="color:var(--status-danger);">Не удалось загрузить: ${_esc(e.message)}</div>`;
    }
}

// =========================================================
