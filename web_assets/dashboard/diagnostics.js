// TEST METRICS — ruptures / PyOD / tsfresh / LightGBM+SHAP / River
// =========================================================
const _tmCharts = [];

async function loadTestMetrics(refresh) {
    const meta = document.getElementById('tm-meta');
    const wrap = document.getElementById('tm-sections');
    meta.textContent = (refresh ? 'Пересчитываем' : 'Загрузка')
        + ' — на больших сессиях первый расчёт может занимать до минуты (данные не прореживаются, обрабатываются полностью); дальше результат из кэша, мгновенно…';
    _tmCharts.forEach(c => c.destroy());
    _tmCharts.length = 0;
    wrap.innerHTML = '';
    try {
        const url = '/test-metrics/latest' + (refresh ? '?refresh=true' : '');
        const r = await fetch(url);
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            meta.innerHTML = `<span style="color:#ef4444;">Ошибка: ${d.detail || r.status}</span>`;
            return;
        }
        const data = await r.json();
        meta.textContent = `Файл: ${data.session_file} · строк использовано: ${data.rows_used.toLocaleString('ru')} · сессия: ${data.session_id}`;
        data.sections.forEach((s, i) => renderTestMetricSection(wrap, s, i));
    } catch(e) {
        meta.innerHTML = `<span style="color:#ef4444;">Ошибка загрузки: ${e}</span>`;
    }
}

function renderTestMetricSection(wrap, s, i) {
    const card = document.createElement('div');
    card.className = 'section';

    if (s.error) {
        card.innerHTML = `
            <h2>⚠️ ${s.library}</h2>
            <p style="color:#ef4444;font-size:13px;">Не удалось посчитать: ${s.error}</p>`;
        wrap.appendChild(card);
        return;
    }

    const canvasId = `tm-chart-${i}`;
    card.innerHTML = `
        <h2>${s.title} <span style="color:#60a5fa;font-size:12px;font-weight:400;">(${s.library})</span></h2>
        <p style="color:#a0aec0;font-size:13px;margin:10px 0 16px;">${s.message}</p>
        ${s.chart ? `<div style="position:relative;height:280px;margin-bottom:16px;"><canvas id="${canvasId}"></canvas></div>` : ''}
        <div id="${canvasId}-table"></div>`;
    wrap.appendChild(card);

    if (s.chart) {
        const ctx = document.getElementById(canvasId);
        const markerSet = new Set(s.chart.marker_indices || []);
        const datasets = s.chart.series.map((series, si) => ({
            label: series.label,
            data: series.data,
            borderColor: [chartColors.blue, chartColors.red, chartColors.green][si % 3],
            backgroundColor: 'transparent',
            borderWidth: si === 0 ? 1.5 : 2,
            pointRadius: series.data.map((_, idx) => si === 0 && markerSet.has(idx) ? 4 : 0),
            pointBackgroundColor: chartColors.red,
            tension: 0.15,
        }));
                const chart = createDashboardChart(ctx, {
            type: 'line',
            data: { labels: s.chart.labels, datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                scales: { x: { ticks: { maxTicksLimit: 12 } } },
                plugins: { legend: { display: datasets.length > 1 } },
            },
                });
                if (chart) _tmCharts.push(chart);
    }

    if (s.table && s.table.rows.length) {
        const tableEl = document.getElementById(`${canvasId}-table`);
        const head = s.table.columns.map(c => `<th>${c}</th>`).join('');
        const rows = s.table.rows.map(row =>
            `<tr>${row.map(v => `<td>${v}</td>`).join('')}</tr>`
        ).join('');
        tableEl.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
    }
}

async function loadVersionInfo() {
    try {
        const [v, h] = await Promise.all([
            fetch('/admin/version').then(r => r.json()),
            fetch('/admin/update/history').then(r => r.json()),
        ]);
        const el = id => document.getElementById(id);
        el('vc-version').textContent = v.version    || '—';
        el('vc-commit').textContent  = v.git_commit || '—';
        el('vc-started').textContent = v.started_at
            ? new Date(v.started_at).toLocaleString('ru') : '—';
        if (h && h.at) {
            const ago = _fmtAgo(new Date(h.at));
            const src = h.source === 'dashboard' ? 'из дашборда' : h.source === 'script' ? 'скрипт' : 'авто';
            el('vc-last-update').textContent = `${ago} (${src})`;
        }
    } catch(e) { /* не блокирует работу */ }
}

// =========================================================
// UPDATE — read-only status. Updates are NEVER triggered from here:
// the container has no Docker access and can't perform one anyway.
// The only place an update actually happens is the desktop icon
// (deploy/launch.ps1 -> update.ps1), on every launch. This section
// just tells the operator whether a newer version exists on GitHub,
// so they know it's worth relaunching.
// =========================================================

let _updateDismissed = false;

async function refreshUpdateCheck() {
    const dot   = document.getElementById('upd-status-dot');
    const label = document.getElementById('upd-status-label');
    if (dot) { dot.style.background = '#6b7280'; label.textContent = 'Проверка…'; }
    try {
        const d = await fetch('/admin/update/check').then(r => r.json());
        if (d.latest_commit) {
            const el = id => document.getElementById(id);
            el('upd-latest-commit').textContent = d.latest_commit;
            el('upd-latest-msg').textContent    = d.latest_message || '';
        }
        if (d.update_available) {
            dot.style.background   = '#f59e0b';
            label.textContent      = `Доступно обновление → ${d.latest_commit}`;
            if (!_updateDismissed) {
                const banner = document.getElementById('update-banner');
                const msg    = document.getElementById('update-banner-msg');
                const dt     = d.latest_date ? new Date(d.latest_date).toLocaleString('ru') : '';
                msg.textContent      = `${d.latest_commit} · ${dt} — ${d.latest_message || ''}`;
                banner.style.display = 'flex';
            }
        } else if (d.error) {
            dot.style.background = '#ef4444';
            label.textContent    = 'Проверка версии недоступна';
        } else if (d.version_unknown) {
            dot.style.background = '#6b7280';
            label.textContent    = 'Запущенная версия неизвестна';
        } else {
            dot.style.background = '#10b981';
            label.textContent    = 'Установлена последняя версия';
        }
    } catch(e) {
        if (dot) { dot.style.background = '#ef4444'; label.textContent = 'Проверка версии недоступна'; }
    }
}

function dismissUpdateBanner() {
    _updateDismissed = true;
    document.getElementById('update-banner').style.display = 'none';
}

// Check for update on load, then every 5 minutes (purely informational).
if (!_updateDismissed) refreshUpdateCheck();
setInterval(() => { if (!_updateDismissed) refreshUpdateCheck(); }, 5 * 60 * 1000);

// =========================================================
