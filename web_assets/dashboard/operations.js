// POWDER
// =========================================================
async function loadPowder() {
    try {
        const r = await fetch('/powder/status');
        const d = await r.json();
        if (!d.has_batch) {
            document.getElementById('powder-detail').innerHTML = '<span style="color:#6b7280;">Нет активных партий. Добавьте первую партию ниже.</span>';
            return;
        }
        const qColor = d.quality_grade === 'ok' ? '#10b981' : d.quality_grade === 'warning' ? '#f59e0b' : '#ef4444';
        document.getElementById('pow-remaining').textContent = d.remaining_kg.toFixed(2);
        document.getElementById('pow-consumed').textContent = d.consumed_kg.toFixed(2);
        document.getElementById('pow-reuse').textContent = d.reuse_count;
        document.getElementById('pow-quality').textContent = d.quality_pct;
        document.getElementById('pow-quality').style.color = qColor;
        document.getElementById('powder-detail').innerHTML = `
            <b style="color:#e2e8f0;">${d.material} ${d.alloy || ''} · ${d.batch_code}</b><br>
            Начало: ${d.initial_kg} кг &nbsp;→&nbsp; Остаток: <b>${d.remaining_kg} кг</b>
            (${d.remaining_pct}%)
            <div style="margin-top:8px;height:6px;background:#2d3748;border-radius:3px;">
                <div style="width:${d.remaining_pct}%;height:100%;background:${qColor};border-radius:3px;transition:width 0.5s;"></div>
            </div>`;
    } catch(e) { document.getElementById('powder-detail').textContent = 'Ошибка загрузки'; }
}

async function addPowderBatch() {
    const material = document.getElementById('pow-material').value.trim();
    const batch = document.getElementById('pow-batch').value.trim();
    const kg = parseFloat(document.getElementById('pow-kg').value);
    if (!material || !batch || isNaN(kg)) { showToast('Заполните все поля', 'error'); return; }
    await fetch('/powder/batches', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ material, batch_code: batch, initial_mass_kg: kg })
    });
    loadPowder();
}

// =========================================================
// MAINTENANCE
// =========================================================
async function loadMaintenance() {
    try {
        const r = await fetch('/maintenance/status');
        const items = await r.json();
        const gradeColor = { ok: '#10b981', warning: '#f59e0b', critical: '#ef4444' };
        document.getElementById('maintenance-items').innerHTML = items.map(item => {
            const color = gradeColor[item.grade] || '#6b7280';
            const barPct = Math.min(100, item.pct);
            const lastSvc = item.last_service ? new Date(item.last_service).toLocaleDateString('ru') : 'не проводилось';
            return `<div class="section" style="padding:20px;${item.grade !== 'ok' ? 'border:1px solid ' + color + ';' : ''}">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                    <span style="font-size:17px;font-weight:600;">${item.icon} ${item.label}</span>
                    <div style="display:flex;gap:10px;align-items:center;">
                        <span style="color:${color};font-weight:700;font-size:15px;">${item.used} / ${item.max} ${item.unit}</span>
                        <button onclick="resetComponent('${item.key}')" style="padding:6px 14px;background:#2d3748;border:1px solid #4a5568;border-radius:6px;color:#e2e8f0;cursor:pointer;font-size:12px;">✅ ТО выполнено</button>
                    </div>
                </div>
                <div style="height:8px;background:#2d3748;border-radius:4px;margin-bottom:8px;">
                    <div style="width:${barPct}%;height:100%;background:${color};border-radius:4px;transition:width 0.5s;"></div>
                </div>
                <div style="font-size:12px;color:#6b7280;">Осталось: <b style="color:#a0aec0;">${item.remaining} ${item.unit}</b> · Последнее ТО: ${lastSvc} · ${item.note}</div>
            </div>`;
        }).join('');
    } catch(e) { document.getElementById('maintenance-items').innerHTML = '<div style="color:#ef4444;">Ошибка загрузки</div>'; }
}

async function resetComponent(key) {
    await fetch('/maintenance/reset/' + key, { method: 'POST' });
    loadMaintenance();
}

// Chat
async function ask() {
    const input = document.getElementById('question');
    const question = input.value.trim();
    if (!question) return;

    const chat = document.getElementById('chat-messages');
    chat.innerHTML += '<div style="background:#60a5fa;color:white;padding:12px 16px;border-radius:12px;margin-bottom:10px;max-width:80%;margin-left:auto;">👤 ' + _esc(question) + '</div>';
    input.value = '';

    try {
        const resp = await fetch('/chat/ask', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question})
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
        chat.innerHTML += '<div style="background:#2d3748;padding:12px 16px;border-radius:12px;margin-bottom:10px;max-width:80%;">🤖 ' + _esc(data.answer || 'Нет ответа') + '</div>';
    } catch(e) {
        chat.innerHTML += '<div style="background:#ef4444;padding:12px 16px;border-radius:12px;margin-bottom:10px;max-width:80%;">❌ ' + _esc(e.message) + '</div>';
    }
    chat.scrollTop = chat.scrollHeight;
}

// Import status bar
// Terminal statuses — jobs in these states are excluded from "active" and
// never shown in the progress bar. "done" is the backend's success terminal
// (ImportJobStatus.done); "completed" is kept for forward-compat.
const TERMINAL_STATUSES = new Set(['done', 'completed', 'needs_operator_context', 'ignored', 'failed']);
const statusMap = {
    'detected':                      { label: 'Обнаружен',               pct: 5,   icon: '🔎' },
    'awaiting_operator_confirmation':{ label: 'Ожидает подтверждения',   pct: 10,  icon: '⏳' },
    'needs_operator_context':        { label: 'Нужен контекст',           pct: 15,  icon: '❓' },
    'postponed':                     { label: 'Отложен',                 pct: 20,  icon: '🕐' },
    'checking_stability':            { label: 'Проверка файлов',         pct: 30,  icon: '🔍' },
    'pending':                       { label: 'В очереди',               pct: 40,  icon: '⏳' },
    'importing':                     { label: 'Импорт...',               pct: 55,  icon: '⚙️' },
    'processing':                    { label: 'Обработка...',            pct: 60,  icon: '⚙️' },
    'analyzing':                     { label: 'Анализ...',               pct: 75,  icon: '📊' },
    'reporting':                     { label: 'Отчёт...',                pct: 90,  icon: '📝' },
    'done':                          { label: 'Готово',                  pct: 100, icon: '✅' },
    'completed':                     { label: 'Готово',                  pct: 100, icon: '✅' },
    'failed':                        { label: 'Ошибка',                  pct: 0,   icon: '❌' },
    'ignored':                       { label: 'Пропущен',                pct: 0,   icon: '⏭️' },
};
async function updateImportStatus() {
    try {
        const resp = await fetch('/imports');
        const payload = await resp.json();
        // /imports returns a paginated envelope: {items: [...], total, skip, limit}
        const jobs = Array.isArray(payload) ? payload : (payload.items || []);
        const bar = document.getElementById('import-status-bar');
        const active = jobs.filter(j => !TERMINAL_STATUSES.has(j.status));
        if (active.length === 0) { bar.style.display = 'none'; return; }
        bar.style.display = 'block';
        const info = statusMap[active[0].status] || { label: active[0].status, pct: 50, icon: '⏳' };
        document.getElementById('import-icon').textContent = info.icon;
        document.getElementById('import-text').textContent = info.label + ': ' + active[0].source_name;
        document.getElementById('import-progress-bar').style.width = info.pct + '%';
        const actions = document.getElementById('import-actions');
        actions.innerHTML = active[0].status === 'awaiting_operator_confirmation'
            ? `<button onclick="actOnImport('${active[0].import_job_id}','confirm')" class="quick-btn primary" style="padding:4px 10px;">Импортировать</button>
               <button onclick="actOnImport('${active[0].import_job_id}','ignore')" class="quick-btn" style="padding:4px 10px;">Пропустить</button>`
            : '';
        const done = jobs.filter(j => j.status === 'done' || j.status === 'completed').length;
        const total = jobs.filter(j => j.status !== 'ignored').length;
        document.getElementById('import-count').textContent = done + '/' + total;
    } catch(e) { /* ignore */ }
}

async function actOnImport(jobId, action) {
    const r = await fetch(`/imports/${jobId}/${action}`, {
        method: 'POST',
        headers: _jsonHeaders(),
        body: JSON.stringify({}),
    });
    if (!r.ok) {
        const payload = await r.json().catch(() => ({}));
        showToast('Не удалось изменить импорт: ' + (payload.detail || r.status), 'error');
    }
    updateImportStatus();
}
setInterval(updateImportStatus, 3000);
updateImportStatus();

// ── Page initialisation ──────────────────────────────────────────────
fetch('/health').then(r => r.json()).then(d => {
    if (d.version) document.getElementById('hdr-version').textContent = 'v' + d.version;
}).catch(() => {});

document.addEventListener('DOMContentLoaded', () => loadHomeStats());
document.addEventListener('DOMContentLoaded', () => loadImportStatus());

// =========================================================
// UPDATE — Watchtower on-demand update
// =========================================================
function _fmtAgo(date) {
    const s = Math.floor((Date.now() - date) / 1000);
    if (s < 60)    return `${s} сек назад`;
    if (s < 3600)  return `${Math.floor(s / 60)} мин назад`;
    if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
    return date.toLocaleDateString('ru');
}

// ── Upload logs ──────────────────────────────────────────────────────
function dropEnter(e) { document.getElementById('drop-zone').style.borderColor = '#3b82f6'; }
function dropLeave(e) { document.getElementById('drop-zone').style.borderColor = '#4a5568'; }
function handleDrop(e) {
    e.preventDefault();
    document.getElementById('drop-zone').style.borderColor = '#4a5568';
    uploadFiles(e.dataTransfer.files);
}
async function uploadFiles(fileList) {
    if (!fileList || !fileList.length) return;
    const files = [...fileList];
    const uploadPage = document.getElementById('page-upload');
    const res = (uploadPage && uploadPage.style.display !== 'none')
        ? document.getElementById('upload-result')
        : document.getElementById('home-upload-result');
    if (!res) return;

    // One selection is one import batch. Besides avoiding one job per
    // file, this lets the parser see the complementary logs together.
    const fd = new FormData();
    files.forEach(file => fd.append('files', file));
    res.innerHTML = `<div style="color:#60a5fa;">Загрузка набора: ${files.length} ${_plural(files.length, 'файл', 'файла', 'файлов')}…</div>`
        + files.map((f, i) => `<div style="color:#a0aec0;">⏳ ${f.name} (${i + 1}/${files.length})</div>`).join('');

    try {
        const r = await fetch('/upload/logs', { method: 'POST', body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.status);
        const saved = d.saved || [];
        const skipped = d.skipped || [];
        let html = '';
        saved.forEach(s => html += `<div style="color:#10b981;">✅ ${s.name} (${(s.size_bytes/1024/1024).toFixed(1)} МБ)</div>`);
        skipped.forEach(s => html += `<div style="color:#ef4444;">❌ ${s.name} — ${s.reason}</div>`);
        if (saved.length) {
            html += '<div style="color:#a0aec0;margin-top:8px;">Файлы поставлены в очередь. Подтвердите импорт в верхней панели.</div>';
        }
        res.innerHTML = html;
    } catch(e) {
        res.innerHTML = `<div style="color:#ef4444;">❌ Не удалось загрузить набор из ${files.length} ${_plural(files.length, 'файла', 'файлов', 'файлов')}: ${e.message || e}</div>`;
    }
}

async function rescanFolder() {
    // Pick the result div on whichever page is currently visible.
    const uploadPage = document.getElementById('page-upload');
    const res = (uploadPage && uploadPage.style.display !== 'none')
        ? document.getElementById('upload-result')
        : document.getElementById('home-upload-result');
    if (!res) return;
    res.innerHTML = '<span style="color:#60a5fa;">Сканирование папки…</span>';
    try {
        const r = await fetch('/upload/rescan', { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.status);
        res.innerHTML = '<span style="color:#10b981;">✅ ' + d.message + '</span>';
    } catch(e) {
        res.innerHTML = '<span style="color:#ef4444;">Ошибка: ' + e + '</span>';
    }
}

// ── New print form ───────────────────────────────────────────────────
async function submitNewPrint() {
    const res = document.getElementById('np-result');
    const op  = document.getElementById('np-operator').value.trim();
    if (!op) { res.innerHTML = '<span style="color:#ef4444;">Укажите оператора</span>'; return; }
    res.innerHTML = '<span style="color:#60a5fa;">Сохранение…</span>';
    const models = document.getElementById('np-models').value.trim();
    try {
        const r = await fetch('/upload/new-print', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                operator: op,
                material: document.getElementById('np-material').value.trim(),
                models: models ? models.split(',').map(s=>s.trim()) : [],
                note: document.getElementById('np-note').value.trim(),
            }),
        });
        const d = await r.json();
        if (d.ok) {
            res.innerHTML = `<span style="color:#10b981;">✅ Зарегистрировано (${d.event_id})</span>`;
            ['np-operator','np-material','np-models','np-note'].forEach(id => document.getElementById(id).value = '');
        } else {
            res.innerHTML = '<span style="color:#ef4444;">Ошибка сервера</span>';
        }
    } catch(e) {
        res.innerHTML = '<span style="color:#ef4444;">Ошибка: ' + e + '</span>';
    }
}
