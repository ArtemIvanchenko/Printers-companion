// ── STL estimate ─────────────────────────────────────────────────────
function handleStlDrop(e) {
    e.preventDefault();
    document.getElementById('stl-drop').style.borderColor = '#4a5568';
    const f = e.dataTransfer.files[0];
    if (f) uploadStlAndSave(f);
}
let _pendingStlFile = null;

async function uploadStlAndSave(file) {
    if (!file) return;
    // Read into memory for 3D preview
    const buf = await file.arrayBuffer();
    _stlFileBuffers[file.name] = buf;
    _pendingStlFile = file;
    // Файл выбран — оператор выбирает метод расчёта
    document.getElementById('stl-result').innerHTML = `
        <div style="background:#1e2433;border:1px solid #2d3748;border-radius:12px;padding:20px;">
            <div style="font-size:13px;color:#e2e8f0;margin-bottom:4px;">🧊 ${file.name}</div>
            <div style="font-size:12px;color:#6b7280;margin-bottom:16px;">${(file.size / 1024 / 1024).toFixed(1)} МБ · расчёт по реальным траекториям лазера (PySLM)</div>
            <div style="margin-bottom:16px;">
                <label style="font-size:12px;color:#a0aec0;display:block;margin-bottom:5px;">Шаг штриховки (мкм) <span title="Зависит от режима печати. По умолчанию 120 мкм." style="color:#6b7280;cursor:help;">ⓘ</span></label>
                <input id="stl-hatch-um" type="number" min="1" step="1" value="120"
                    style="width:120px;background:#0f1420;border:1px solid #2d3748;border-radius:8px;padding:8px 10px;color:#e2e8f0;font-size:14px;">
            </div>
            <div style="font-size:11px;color:#6b7280;margin-bottom:12px;">
                💡 Для точного времени по детали с поддержками экспортируйте всю компоновку плиты из Magics в STL и считайте её через карточку печати.</div>
            <div style="display:flex;gap:12px;flex-wrap:wrap;">
                <button onclick="runStlEstimate()"
                    style="flex:1;min-width:200px;background:#10b981;color:white;border:none;border-radius:10px;padding:14px;font-size:14px;font-weight:600;cursor:pointer;">
                    🎯 Рассчитать время и стоимость
                    <div style="font-weight:400;font-size:11px;opacity:.8;margin-top:3px;">PySLM — реальные траектории лазера</div>
                </button>
            </div>
        </div>`;
}

async function runStlEstimate() {
    if (!_pendingStlFile) return;
    const um = parseFloat(document.getElementById('stl-hatch-um')?.value);
    const hatchMm = (um && um > 0) ? um / 1000 : null;
    await uploadStl(_pendingStlFile, null, hatchMm);
}
function _renderPrediction(p) {
    if (!p) return '';
    if (!p.available) {
        return `<div style="background:#1a2332;border:1px solid #2d3748;border-radius:8px;padding:10px 12px;font-size:12px;color:#a0aec0;margin-top:12px;">
            ℹ️ Расчёт по геометрии недоступен: ${p.reason}</div>`;
    }
    const cost = Object.entries(p.cost_breakdown || {})
        .map(([k, v]) => `<span style="margin-right:14px;">${k}: <b style="color:#e2e8f0;">${v.toLocaleString('ru')} ₽</b></span>`)
        .join('');
    const warns = (p.warnings || []).map(w =>
        `<div style="color:#fcd34d;font-size:11px;margin-top:4px;">⚠️ ${w}</div>`).join('');
    const methodLabel = {
        pyslm: 'PySLM — реальные траектории лазера',
    }[p.method] || p.method;
    const corr = (p.correction_factor && Math.abs(p.correction_factor - 1) > 0.001)
        ? ` · калибровка ×${p.correction_factor}` : '';
    return `
        <div style="background:#0f2438;border:1px solid #1a5276;border-radius:12px;padding:16px;margin-top:14px;">
            <div style="color:#60a5fa;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;">
                Расчёт по геометрии · ${p.material} · ${methodLabel}</div>
            <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:10px;">
                <div>
                    <div style="font-size:26px;font-weight:700;color:#10b981;">${p.print_hours} <span style="font-size:13px;">ч</span></div>
                    <div style="color:#6b7280;font-size:12px;">время печати (${p.total_days} сут)</div>
                </div>
                <div>
                    <div style="font-size:26px;font-weight:700;color:#f59e0b;">${p.cost_total_rub.toLocaleString('ru')} <span style="font-size:13px;">₽</span></div>
                    <div style="color:#6b7280;font-size:12px;">себестоимость</div>
                </div>
                <div>
                    <div style="font-size:26px;font-weight:700;color:#8b5cf6;">${p.layer_count}</div>
                    <div style="color:#6b7280;font-size:12px;">слоёв (${p.height_mm} мм)</div>
                </div>
                ${p.powder_kg != null ? `<div>
                    <div style="font-size:26px;font-weight:700;color:#06b6d4;">${p.powder_kg} <span style="font-size:13px;">кг</span></div>
                    <div style="color:#6b7280;font-size:12px;">порошок (${p.powder_cost_rub_per_kg ?? '—'} ₽/кг)</div>
                </div>` : ''}
            </div>
            <div style="color:#6b7280;font-size:12px;">
                сканирование ${p.scan_hours} ч · нанесение слоёв ${p.recoat_hours} ч${p.time_breakdown?.hatch_distance_mm ? ` · шаг штриховки ${Math.round(p.time_breakdown.hatch_distance_mm * 1000)} мкм` : ''}${corr}</div>
            <div style="color:#6b7280;font-size:12px;margin-top:4px;">
                🧭 построение вдоль оси Z (${p.build_axis || 'Z'}), плита = низ модели · ${p.layer_count} слоёв · высота ${p.height_mm} мм</div>
            ${cost ? `<div style="color:#6b7280;font-size:12px;margin-top:6px;">${cost}</div>` : ''}
            ${warns}
        </div>`;
}

async function loadStlMaterials() {
    const sel = document.getElementById('stl-material');
    if (!sel || sel.options.length) return;
    try {
        const d = await fetch('/prints/defaults').then(r => r.json());
        sel.innerHTML = d.materials.map(m => `<option value="${m}">${_materialRu(m)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = '<option value="steel">Сталь</option>';
    }
}

async function uploadStl(file, _buf, hatchMm = null) {
    const res = document.getElementById('stl-result');
    if (!file) return;
    res.innerHTML = '<div style="color:#60a5fa;">Генерируем траектории лазера (PySLM)… до ~30 сек для крупных моделей</div>';
    const fd = new FormData();
    fd.append('file', file);
    const material = document.getElementById('stl-material')?.value || 'steel';
    try {
        let url = `/upload/stl-estimate?material=${encodeURIComponent(material)}`;
        if (hatchMm && hatchMm > 0) url += `&hatch_distance_mm=${hatchMm}`;
        const r = await fetch(url, { method: 'POST', body: fd });
        const d = await r.json();
        const vol = d.volume_cm3;
        const est = d.estimate;
        const pred = d.prediction;
        const warns = (d.warnings || []).map(w =>
            `<div style="background:#451a03;border:1px solid #92400e;border-radius:6px;padding:8px 10px;font-size:12px;color:#fcd34d;margin-top:8px;">⚠️ ${w}</div>`
        ).join('');
        res.innerHTML = `
            <div style="background:#1e2433;border:1px solid #2d3748;border-radius:12px;padding:20px;">
                <div style="font-size:13px;color:#6b7280;margin-bottom:12px;">${d.filename}</div>
                <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px;">
                    <div>
                        <div style="font-size:28px;font-weight:700;color:#10b981;">${vol} <span style="font-size:14px;">см³</span></div>
                        <div style="color:#6b7280;font-size:12px;">объём модели</div>
                    </div>
                    ${est.avg_session_hours != null ? `<div>
                        <div style="font-size:28px;font-weight:700;color:#60a5fa;">${est.avg_session_hours} <span style="font-size:14px;">ч</span></div>
                        <div style="color:#6b7280;font-size:12px;">средн. по истории (справочно)</div>
                    </div>` : ''}
                </div>
                <div style="color:#a0aec0;font-size:12px;">${est.note} · ${est.sessions_used} сессий</div>
                ${_renderPrediction(pred)}
                ${warns}
                <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;">
                    <button onclick="showMainTab('estimate',null)"
                        style="background:#2d3748;color:#e2e8f0;border:1px solid #4a5568;border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;">
                        📦 Открыть в истории моделей
                    </button>
                </div>
            </div>`;
        // Save to model history (prefer geometry-based hours)
        const histHours = (pred && pred.available) ? pred.print_hours : est.avg_session_hours;
        _saveModelEntry(file.name, vol, histHours);
    } catch(e) {
        res.innerHTML = `<div style="color:#ef4444;">Ошибка загрузки: ${e.message || e}<br><small style="color:#6b7280;">Убедитесь что API запущен (docker compose up)</small></div>`;
    }
}

async function loadImportStatus() {
    try {
        const d = await fetch('/admin/import/status').then(r => r.json());
        const el = id => document.getElementById(id);
        el('isc-sessions').textContent = d.session_count ?? '—';
        el('isc-jobs').textContent     = d.import_job_count ?? '—';
        if (d.last_import_at) {
            const ago = _fmtAgo(new Date(d.last_import_at));
            const name = d.last_import_name ? ` · ${d.last_import_name}` : '';
            const st   = d.last_import_status ? ` (${d.last_import_status})` : '';
            el('isc-last').textContent = `${ago}${st}${name}`;
            const sidebar = el('sidebar-last-import');
            if (sidebar) sidebar.textContent = ago;
        }
    } catch(e) { /* не блокирует */ }
}

const _LOG_COLORS = {ERROR:'#ef4444', WARNING:'#f59e0b', INFO:'#60a5fa', DEBUG:'#6b7280'};
async function loadLogs() {
    const viewer = document.getElementById('log-viewer');
    const level  = document.getElementById('log-level-filter').value;
    viewer.innerHTML = '<div style="color:#6b7280;">Загрузка…</div>';
    try {
        const url = '/admin/logs?n=300' + (level ? `&level=${level}` : '');
        const logs = await fetch(url).then(r => r.json());
        if (!logs.length) {
            viewer.innerHTML = '<div style="color:#6b7280;">Нет записей. Проверьте что volume app_logs смонтирован.</div>';
            return;
        }
        viewer.innerHTML = logs.map(e => {
            const col = _LOG_COLORS[e.level] || '#a0aec0';
            const ts  = e.ts ? e.ts.replace('T',' ').slice(0,19) : '';
            const lvl = (e.level || '').padEnd(7);
            const log = (e.logger || '').padEnd(30).slice(0,30);
            const msg = (e.msg || '').replace(/</g,'&lt;');
            const exc = e.exception ? `<div style="color:#ef4444;margin-left:8px;white-space:pre-wrap;">${e.exception.replace(/</g,'&lt;')}</div>` : '';
            return `<div style="margin-bottom:2px;"><span style="color:#4a5568;">${ts}</span> <span style="color:${col};font-weight:600;">${lvl}</span> <span style="color:#6b7280;">${log}</span> ${msg}${exc}</div>`;
        }).join('');
        viewer.scrollTop = viewer.scrollHeight;
    } catch(e) {
        viewer.innerHTML = '<div style="color:#ef4444;">Ошибка загрузки логов: ' + e + '</div>';
    }
}

// =========================================================
