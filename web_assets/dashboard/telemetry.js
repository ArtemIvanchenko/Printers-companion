// DYNAMIC TELEMETRY
// =========================================================
function _fmtTelDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return iso.slice(0,10);
    const months = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
    return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()} · ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

// Cache for all-time aggregated data so we don't re-fetch on every visit
// Full session list cache (populated once by loadTelemetrySessions)
let _telSessionList = [];

function _setActiveCard(id) {
    document.querySelectorAll('#tel-session-cards .tel-card').forEach(c => c.classList.remove('active'));
    const card = document.getElementById(id);
    if (card) {
        card.classList.add('active');
        const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        card.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'nearest', inline: 'nearest' });
    }
}

let _telemetryListLoading = false;
async function loadTelemetrySessions(more = false) {
    ensureTelemetryCharts();
    const container = document.getElementById('tel-session-cards');
    if (!container || _telemetryListLoading) return;
    _telemetryListLoading = true;
    const notice = document.getElementById('telemetry-notice');
    notice.textContent = 'Загрузка списка сессий…';
    let next = document.getElementById('telemetry-load-more');
    if (!next) {
        next = document.createElement('button'); next.id = 'telemetry-load-more';
        next.className = 'quick-btn'; next.textContent = 'Ещё сессии';
        next.onclick = () => loadTelemetrySessions(true);
        container.after(next);
    }
    next.disabled = true;
    try {
        const page = await _designFetch('/dashboard/telemetry-sessions?limit=50&skip=' + (more ? _telSessionList.length : 0));
        const list = page.items;
        _telSessionList = more ? _telSessionList.concat(list) : list;
        if (!more) container.querySelectorAll('.tel-card:not(#tel-card-alltime)').forEach(c=>c.remove());
        for (const session of list) {
            const card = document.createElement('div');
            card.className = 'tel-card'; card.id = 'tel-card-' + session.session_id;
            const date = document.createElement('div'); date.className = 'tel-card-date';
            date.textContent = _fmtTelDate(session.start_ts) || session.session_id;
            const duration = document.createElement('div'); duration.className = 'tel-card-dur';
            duration.textContent = session.duration_min == null ? 'Длительность не измерена' : (session.duration_min/60).toFixed(1) + ' ч';
            card.append(date, duration);
            card.onclick = () => loadTelemetryForSession(session.session_id);
            container.appendChild(card);
        }
        next.hidden = !page.has_more;
        notice.textContent = page.total ? 'Сессий в списке: ' + _telSessionList.length + ' из ' + page.total : 'Нет сохранённой телеметрии.';
        if (list.length && !_telCharts.currentSessionId && !more) await loadTelemetryForSession(list[0].session_id);
    } catch(error) {
        notice.textContent = 'Не удалось загрузить сессии: ' + error.message;
        next.hidden = false;
        next.textContent = 'Повторить';
    } finally {
        next.disabled = false;
        _telemetryListLoading = false;
    }
}

const _SIG_COLORS = {
    SO1:'#ef4444', SO2:'#f59e0b',
    ST3:'#60a5fa', ST4:'#8b5cf6', ST5:'#10b981',
    SP4:'#06b6d4', 'Flow H':'#8b5cf6',
};
const _PALETTE = ['#60a5fa','#10b981','#f59e0b','#ef4444','#8b5cf6','#06b6d4'];

function _makeDatasets(series) {
    return Object.entries(series).map(([key, values], i) => ({
        label: sigLabel(key),
        data: values,
        borderColor: _SIG_COLORS[key] || _PALETTE[i % _PALETTE.length],
        backgroundColor: 'transparent',
        borderWidth: 2, pointRadius: 0, tension: 0.3,
    }));
}

function _updateLineChart(chart, time, series) {
    if (!chart) return;
    chart.data.labels = time;
    chart.data.datasets = _makeDatasets(series);
    chart.update('none');
}

async function loadTelemetryForSession(sessionId) {
    if (!sessionId) return;
    ensureTelemetryCharts();
    _telCharts.currentSessionId = sessionId;
    _setActiveCard('tel-card-' + sessionId);
    try {
        const d = await _designFetch('/sessions/' + encodeURIComponent(sessionId) + '/telemetry');
        if (_telCharts.currentSessionId !== sessionId) return;
        const tel  = d.telemetry || {};
        const time = tel.time || [];
        document.getElementById('telemetry-notice').textContent = time.length ? '' : 'В этой сессии нет сохранённой телеметрии.';
        renderTelemetryHealth(d.health || {}, tel);
        document.getElementById('tel-subtitle').textContent = _fmtTelDate(d.start_ts);
        _updateLineChart(_telCharts.o2,   time, tel.oxygen       || {});
        _updateLineChart(_telCharts.temp, time, tel.temperatures || {});
        _updateLineChart(_telCharts.hum,  time, tel.humidity     || {});
        _updateLineChart(_telCharts.press,time, tel.pressure     || {});
        if (_telCharts.burn) {
            const burn = tel.layer_burn_times || [];
            _telCharts.burn.data.labels = burn.map(b => b.layer);
            _telCharts.burn.data.datasets[0].data = burn.map(b => b.duration_sec);
            _telCharts.burn.update('none');
        }
    } catch(e) {
        document.getElementById('telemetry-notice').textContent = 'Не удалось загрузить телеметрию: ' + e.message;
    }
}

function renderTelemetryHealth(health, telemetry) {
    const host = document.getElementById('telemetry-health');
    const score = health.readiness?.score;
    const anomalies = health.anomalies || [];
    const trend = health.burn_drift?.trend;
    const trendLabel = {rising:'↑ растёт',falling:'↓ снижается',stable:'→ стабильно',insufficient_data:'нет данных'}[trend] || '—';
    host.innerHTML = `<div class="stats" style="margin-bottom:20px;">
        <div class="stat-card"><div class="value">${Number.isFinite(score) ? Math.round(score) : '—'}</div><div class="label">Готовность атмосферы (0–100)</div></div>
        <div class="stat-card"><div class="value">${health.anomalies ? anomalies.length : '—'}</div><div class="label">Аномалий процесса</div></div>
        <div class="stat-card"><div class="value" style="font-size:22px;">${trendLabel}</div><div class="label">Тренд времени прожига</div></div>
    </div>`;
    for (const item of anomalies.slice(0,12)) {
        const row = document.createElement('p'); row.textContent = item.detail || sigLabel(item.signal);
        host.appendChild(row);
    }
    const limits = dashboardBootstrap.thresholds || {};
    for (const [id, key, high, low] of [
        ['o2Chart','oxygen',limits.o2,null],['tempChart','temperatures',limits.temp,null],
        ['humChart','humidity',limits.hum,null],['pressChart','pressure',limits.press_high,limits.press_low],
    ]) {
        const values = Object.values(telemetry[key] || {}).flatMap(values=>values.slice(-10)).filter(Number.isFinite);
        const alarm = values.some(value=>(high != null && value > high) || (low != null && value < low));
        document.getElementById(id)?.parentElement.classList.toggle('alarm-active',alarm);
    }
}

async function loadAllTimeTelemetry() {
    ensureTelemetryCharts();
    _setActiveCard('tel-card-alltime');
    _telCharts.currentSessionId = null;
    document.getElementById('tel-subtitle').textContent = 'Загрузка сводки…';
    document.getElementById('telemetry-health').replaceChildren();
    for (const id of ['o2Chart','tempChart','humChart','pressChart']) {
        document.getElementById(id)?.parentElement.classList.remove('alarm-active');
    }
    try {
        // One bounded SQL projection instead of N full sensor-array responses.
        const page = await _designFetch('/dashboard/telemetry-sessions?limit=100');
        const sessions = page.items.slice().reverse();
        const labels = sessions.map(s=>_fmtTelDate(s.start_ts) || s.session_id);
        for (const [key, group] of [['o2','oxygen'],['temp','temperature'],['hum','humidity'],['press','pressure']]) {
            const names = new Set(sessions.flatMap(s=>Object.entries(s.signal_stats).filter(([,v])=>v.group === group).map(([name])=>name)));
            const series = Object.fromEntries([...names].map(name=>[name, sessions.map(s=>s.signal_stats[name]?.mean ?? null)]));
            _updateLineChart(_telCharts[key], labels, series);
        }
        if (_telCharts.burn) {
            _telCharts.burn.data.labels = labels;
            _telCharts.burn.data.datasets[0].data = sessions.map(s=>s.mean_burn_seconds);
            _telCharts.burn.update('none');
        }
        document.getElementById('tel-subtitle').textContent = sessions.length
            ? 'Последние ' + sessions.length + ' из ' + page.total + ' сессий · сохранённые средние; пропуски не заменяются нулями'
            : 'Нет сохранённой телеметрии';
    } catch(error) {
        document.getElementById('tel-subtitle').textContent = 'Ошибка загрузки сводки: ' + error.message;
    }
}

async function exportDatabase() {
    const MAP = {operator:'exp-operator', sessions:'exp-sessions', analytics:'exp-analytics', models:'exp-models', maintenance:'exp-maintenance'};
    const cats = Object.entries(MAP).filter(([, id]) => document.getElementById(id)?.checked).map(([k]) => k);
    if (!cats.length) { document.getElementById('export-status').textContent = 'Выберите хотя бы один тип данных'; return; }
    const status = document.getElementById('export-status');
    status.textContent = '⏳ Формирование файла…';
    try {
        const qs = cats.map(c => 'cat=' + encodeURIComponent(c)).join('&');
        const r = await fetch('/export/download?' + qs);
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = r.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'export.json';
        a.click();
        URL.revokeObjectURL(url);
        status.textContent = '✅ Готово';
        setTimeout(() => { status.textContent = ''; }, 4000);
    } catch(e) {
        status.textContent = '❌ Ошибка: ' + e.message;
    }
}

async function saveMachineParams() {
    const payload = {};
    _MP_FIELDS.forEach(f => {
        const v = document.getElementById('mp-' + f).value;
        payload[f] = v === '' ? null : Number(v);
    });
    const { densities, hatches } = _collectMpMaterials();
    payload.material_densities  = densities;
    payload.hatch_speeds_by_mat = hatches;

    const status = document.getElementById('mp-status');
    try {
        const r = await fetch('/settings/machine', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        const d = await r.json();
        status.textContent = d.configured ? '✅ Сохранено' : '💾 Сохранено (не все параметры заданы)';
        loadPrintDefaults();  // material list may have changed
    } catch (e) {
        status.textContent = '❌ Ошибка: ' + e.message;
    }
}
