// PATTERNS — cross-session intelligence
// =========================================================
const _signalLabel = dashboardBootstrap.signal_labels;
const _groupColor = {
    oxygen:'#ef4444', temperature:'#f59e0b',
    pressure:'#8b5cf6', humidity:'#06b6d4',
    temp_gas:'#f59e0b', flow:'#3b82f6',
};

function sigLabel(s)  { return _signalLabel[s] || s; }
function grpColor(g)  { return _groupColor[g] || '#60a5fa'; }

// Traffic-light score coloring — thresholds are per-metric and meaningful,
// callers pass their own warnAt/dangerAt rather than sharing one scale.
function scoreColor(score, warnAt, dangerAt) {
    return score >= warnAt ? 'var(--status-success)' : score >= dangerAt ? 'var(--status-warning)' : 'var(--status-danger)';
}

function _confBadge(c) {
    const pct = Math.round(c * 100);
    const col = c >= 0.7 ? '#10b981' : c >= 0.5 ? '#f59e0b' : '#6b7280';
    return `<span style="background:${col};color:white;font-size:10px;padding:2px 7px;border-radius:10px;font-weight:600;">${pct}%</span>`;
}

function _dirArrow(dir) {
    return dir === 'increasing'
        ? '<span style="color:#ef4444;">▲</span>'
        : '<span style="color:#10b981;">▼</span>';
}

function renderTrends(trends) {
    const el = document.getElementById('patterns-trends');
    if (!trends.length) {
        el.innerHTML = '<div style="color:#6b7280;font-size:13px;">Недостаточно данных. Нужно ≥ 3 сессий с телеметрией.</div>';
        return;
    }
    el.innerHTML = trends.map(t => `
        <div style="border:1px solid #2d3748;border-radius:10px;padding:14px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <span style="color:${grpColor(t.group)};font-weight:600;">${sigLabel(t.signal)}</span>
                ${_confBadge(t.confidence)}
            </div>
            <div style="font-size:20px;font-weight:700;margin-bottom:4px;">
                ${_dirArrow(t.direction)} ${t.slope_pct_of_mean}% / сессию
            </div>
            <div style="color:#a0aec0;font-size:12px;">
                Среднее: ${t.mean_overall} · ${t.n_sessions} сессий
            </div>
        </div>
    `).join('');
}

function _anomalyDescription(signal, direction) {
    const desc = {
        'Flow H': { high: 'Влажность продувочного газа была выше нормы', low: 'Влажность продувочного газа была ниже нормы' },
        'Flow T': { high: 'Температура газового потока была выше нормы', low: 'Температура газового потока была ниже нормы' },
        LIR: { high: 'Положение платформы по оси Z было выше обычного', low: 'Положение платформы по оси Z было ниже обычного' },
        SF1: { high: 'Расход газа был выше нормы', low: 'Расход газа был ниже нормы' },
        SO1: { high: 'Содержание кислорода было повышено', low: 'Содержание кислорода было понижено' },
        SO2: { high: 'Содержание кислорода (SO2) было повышено', low: 'Содержание кислорода (SO2) было понижено' },
        SP2: { high: 'Давление в системе было выше нормы', low: 'Давление в системе было ниже нормы' },
        SP8: { high: 'Давление SP8 было выше нормы', low: 'Давление SP8 было ниже нормы' },
        SP14: { high: 'Давление SP14 было выше нормы', low: 'Давление SP14 было ниже нормы' },
        SP15: { high: 'Давление SP15 было выше нормы', low: 'Давление SP15 было ниже нормы' },
        ST3: { high: 'Температура ST3 была выше нормы', low: 'Температура ST3 была ниже нормы' },
        ST5: { high: 'Температура стола была выше нормы', low: 'Температура стола была ниже нормы' },
    };
    return (desc[signal] || {})[direction] || `${sigLabel(signal)}: аномальное значение (${direction})`;
}

function renderAnomalies(anomalies, sessionsDetail) {
    const el = document.getElementById('patterns-anomalies');
    if (!anomalies.length) {
        el.innerHTML = '<div style="color:#6b7280;font-size:13px;">Аномалий не обнаружено.</div>';
        return;
    }
    const dateMap = {};
    (sessionsDetail || []).forEach(s => { dateMap[s.session_id] = (s.start_ts || '').slice(0, 10); });
    const bySession = {};
    anomalies.forEach(a => { (bySession[a.session_id] = bySession[a.session_id] || []).push(a); });
    el.innerHTML = Object.entries(bySession).map(([sid, anoms]) => {
        const date = dateMap[sid] || sid.slice(9, 19);
        return `
        <div style="border:1px solid #2d3748;border-radius:10px;padding:14px;margin-bottom:12px;">
            <div style="font-weight:600;margin-bottom:8px;font-size:13px;">${date}</div>
            ${anoms.map(a => `
                <div style="margin-bottom:8px;padding:8px;background:#1e2433;border-radius:8px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                        <span style="color:${grpColor(a.group)};font-size:13px;font-weight:500;">${sigLabel(a.signal)}</span>
                        <span style="color:${a.direction==='high'?'#ef4444':'#60a5fa'};font-weight:600;">
                            z=${a.z_score.toFixed(1)}
                        </span>
                    </div>
                    <div style="color:#e2e8f0;font-size:13px;">${_anomalyDescription(a.signal, a.direction)}</div>
                </div>
            `).join('')}
        </div>`;
    }).join('');
}

function renderBeforeAfter(items) {
    const el = document.getElementById('patterns-before-after');
    if (!items.length) {
        el.innerHTML = '<div style="color:#6b7280;font-size:13px;">Нет данных о техобслуживании или недостаточно сессий до/после.</div>';
        return;
    }
    el.innerHTML = items.map(b => `
        <div style="border:1px solid #2d3748;border-radius:10px;padding:14px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span style="font-weight:600;font-size:13px;">${b.event_type.replace(/_/g,' ')}</span>
                ${_confBadge(b.confidence)}
            </div>
            <div style="color:${grpColor(b.group)};margin-bottom:6px;">${sigLabel(b.signal)}</div>
            <div style="display:flex;gap:20px;font-size:13px;">
                <div><div style="color:#6b7280;font-size:11px;">ДО</div><b>${b.before_mean}</b></div>
                <div style="align-self:center;">→</div>
                <div><div style="color:#6b7280;font-size:11px;">ПОСЛЕ</div><b>${b.after_mean}</b></div>
                <div style="align-self:center;color:${b.delta_pct<0?'#10b981':'#ef4444'};font-weight:700;">
                    ${b.delta_pct > 0 ? '+' : ''}${b.delta_pct}%
                </div>
            </div>
        </div>
    `).join('');
}

function renderSessionsTable(sessions) {
    const el = document.getElementById('patterns-sessions-table');
    if (!sessions.length) {
        el.innerHTML = '<div style="color:#6b7280;font-size:13px;">Нет сессий с телеметрией.</div>';
        return;
    }
    // Collect all signal names across sessions
    const allSigs = new Set();
    sessions.forEach(s => Object.keys(s.signal_stats || {}).forEach(k => allSigs.add(k)));
    const sigs = [...allSigs].sort();

    const header = ['Сессия', 'Дата', 'Качество', ...sigs.map(sigLabel)].map(h =>
        `<th>${h}</th>`).join('');
    const rows = sessions.map(s => {
        const stats = s.signal_stats || {};
        const date = s.start_ts ? s.start_ts.slice(0,10) : '—';
        const readiness = s.health?.readiness_score;
        const readCol = readiness != null ? scoreColor(readiness, 80, 50) : '';
        const readStr = readiness != null ? `<span style="color:${readCol};font-weight:600;">${readiness}</span>` : '—';
        const sigCells = sigs.map(sig => {
            const st = stats[sig];
            if (!st) return '<td style="color:var(--border-hover);">—</td>';
            const col = grpColor(st.group);
            return `<td style="color:${col};">${st.mean.toFixed(3)}<span style="color:var(--border-hover);font-size:11px;"> ±${st.std.toFixed(3)}</span></td>`;
        }).join('');
        return `<tr><td style="font-size:12px;color:#a0aec0;">${s.session_id}</td><td>${date}</td><td>${readStr}</td>${sigCells}</tr>`;
    }).join('');

    el.innerHTML = `<table><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table>`;
}

let _patternsLoaded = false;
async function loadPatterns(force = false) {
    if (_patternsLoaded && !force) return;
    document.getElementById('patterns-summary').textContent = 'Анализ...';

    try {
        const url = force ? '/analysis/patterns?force=true' : '/analysis/patterns';
        const r = await fetch(url);
        const data = await r.json();

        if (data.error) {
            document.getElementById('patterns-summary').innerHTML =
                `<span style="color:#ef4444;">Ошибка: ${data.error}</span>`;
            return;
        }

        document.getElementById('patterns-summary').innerHTML =
            `<b>${data.summary}</b>&nbsp;&nbsp;<span style="color:#4a5568;font-size:12px;">` +
            `обновлено ${(data.computed_at||'').slice(11,19)}</span>`;

        renderTrends(data.trends || []);
        renderAnomalies(data.anomalies || [], data.sessions_detail || []);
        renderBeforeAfter(data.before_after || []);
        renderSessionsTable(data.sessions_detail || []);
        loadDefectRisk();
        loadMaintenanceForecast();
        _patternsLoaded = true;
    } catch(e) {
        document.getElementById('patterns-summary').innerHTML =
            `<span style="color:#ef4444;">Ошибка загрузки: ${e}</span>`;
    }
}

const _riskColor = g => g === 'high' ? '#ef4444' : g === 'medium' ? '#f59e0b' : '#10b981';

async function loadDefectRisk() {
    try {
        const d = await fetch('/analysis/defect-risk').then(r => r.json());
        const meta = document.getElementById('defect-risk-meta');
        const shadowN = (((d.shadow_model || {}).shadow_metrics || {}).candidate || {}).sample_size || 0;
        meta.textContent = d.model_trained
            ? `Проверенная активная модель · разметок: ${d.n_labeled}`
            : d.shadow_model
                ? `Эвристика · новая модель проверяется в тени (${shadowN} новых исходов)`
                : `Эвристика · подтверждённых разметок: ${d.n_labeled}; кандидат требует ≥20 и оба класса`;
        const list = document.getElementById('defect-risk-list');
        if (!d.sessions || !d.sessions.length) {
            list.innerHTML = '<div style="color:#6b7280;font-size:13px;">Нет сессий.</div>';
            return;
        }
        list.innerHTML = d.sessions.slice(0, 12).map(s => {
            const pct = Math.round(s.risk * 100);
            const col = _riskColor(s.grade);
            const factors = (s.top_factors || []).map(f => f.factor).slice(0, 2).join(', ');
            return `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #232a3a;">
                <div style="width:46px;text-align:right;color:${col};font-weight:700;">${pct}%</div>
                <div style="flex:1;">
                    <div style="height:6px;background:#232a3a;border-radius:3px;overflow:hidden;">
                        <div style="height:100%;width:${pct}%;background:${col};"></div>
                    </div>
                    <div style="color:#6b7280;font-size:11px;margin-top:3px;">${s.session_id.slice(0,28)} · ${factors || '—'}</div>
                </div>
            </div>`;
        }).join('');
    } catch(e) {
        document.getElementById('defect-risk-list').innerHTML =
            `<span style="color:#ef4444;font-size:13px;">Ошибка: ${e}</span>`;
    }
}

async function loadMaintenanceForecast() {
    try {
        const d = await fetch('/analysis/maintenance-forecast').then(r => r.json());
        const el = document.getElementById('maintenance-forecast');
        if (!d.forecasts || !d.forecasts.length) {
            el.innerHTML = `<div style="color:#6b7280;font-size:13px;">Деградации не обнаружено (анализ ${d.n_sessions} сессий, нужно ≥4).</div>`;
            return;
        }
        el.innerHTML = d.forecasts.map(f => {
            const urgent = f.sessions_to_threshold <= 5;
            const col = urgent ? '#ef4444' : '#f59e0b';
            const eta = f.sessions_to_threshold === 0 ? 'СЕЙЧАС' : `~${f.sessions_to_threshold} печат.`;
            return `<div style="padding:8px 0;border-bottom:1px solid #232a3a;">
                <div style="display:flex;justify-content:space-between;">
                    <b style="color:#e2e8f0;">${f.signal}</b>
                    <span style="color:${col};font-weight:700;">${eta}</span>
                </div>
                <div style="color:#85929e;font-size:12px;margin-top:2px;">${f.recommendation}</div>
            </div>`;
        }).join('');
    } catch(e) {
        document.getElementById('maintenance-forecast').innerHTML =
            `<span style="color:#ef4444;font-size:13px;">Ошибка: ${e}</span>`;
    }
}
// =========================================================
