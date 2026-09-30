/* Optional historical panels. No history request is made at page startup. */
const _historyPanels = new Map();
const _historyKinds = new Set(['sessions', 'timeline', 'quality', 'consumption']);

function _historyChart(id, labels, values, label, type = 'bar') {
    const color = {burnChart:'#ef4444', defectsChart:'#ef4444', gasChart:'#ec4899',
        powderChart:'#06b6d4', sessionLinesChart:'#06b6d4'}[id] || '#60a5fa';
    const dataset = {label, data:values, backgroundColor:color, borderColor:color, borderRadius:8};
    if (type === 'doughnut') Object.assign(dataset, {backgroundColor:['#10b981','#ef4444','#f59e0b'], borderWidth:0});
    if (type === 'line') Object.assign(dataset, {fill:true,tension:.4,
        backgroundColor:id === 'sessionLinesChart' ? 'rgba(6,182,212,.1)' : 'rgba(96,165,250,.1)'});
    if (id === 'timelineChart') Object.assign(dataset, {pointRadius:6,pointBackgroundColor:color});
    if (id === 'pausesChart') dataset.backgroundColor = values.map(v=>v>0 ? '#f59e0b' : '#60a5fa');
    return createDashboardChart(document.getElementById(id), {
        type, data: {labels, datasets:[dataset]},
        options: {responsive: true, plugins: {legend: {display: type === 'doughnut',position:'bottom'}},
            ...(id === 'defectsChart' ? {indexAxis:'y'} : {}),
            ...(type === 'doughnut' ? {cutout:'60%'} : {scales:{y:{beginAtZero:true}}})},
    });
}

function _renderHistoryCharts(panel, data, rows) {
    if (panel === 'timeline') {
        const real = rows.filter(row => row.type === 'REAL_PRINT');
        _historyChart('timelineChart', real.map(r=>r.date), real.map(r=>r.duration_min == null ? null : r.duration_min/60), 'Часы', 'line');
        const dates = rows.map(row=>row.date);
        _historyChart('pausesChart', dates, rows.map(r=>r.pause_count), 'Паузы');
        _historyChart('burnChart', dates, rows.map(r=>r.burn_events), 'События прожига');
        _historyChart('sessionLinesChart', dates, rows.map(r=>r.total_lines), 'Строк', 'line');
    } else if (panel === 'quality') {
        const names = {accepted:'Годная', rejected:'Брак', unknown:'Неизвестно'};
        _historyChart('qualityChart', Object.keys(data.result_counts).map(k=>names[k]||k), Object.values(data.result_counts), 'Записи контроля', 'doughnut');
        _historyChart('defectsChart', Object.keys(data.defect_counts), Object.values(data.defect_counts), 'Записи дефектов');
    } else if (panel === 'consumption') {
        for (const [kind, id, label] of [['gas','gasChart','Бар'],['powder','powderChart','Кг']]) {
            const values = rows.filter(r=>r.event_type === `${kind}_consumption_recorded`);
            _historyChart(id, values.map(r=>String(r.timestamp||'—').slice(0,10)), values.map(r=>r.value), label);
        }
    }
}

async function loadHistoryPanel(panel, more = false, refresh = false) {
    if (!_historyKinds.has(panel)) return;
    const host = document.getElementById(`page-${panel}`);
    if (!host) return;
    let state = _historyPanels.get(panel);
    if (!state) {
        const controls = document.createElement('div');
        controls.className = 'history-controls';
        const notice = document.createElement('span');
        notice.setAttribute('role', 'status');
        const retry = document.createElement('button');
        retry.className = 'quick-btn'; retry.textContent = 'Обновить';
        retry.onclick = () => loadHistoryPanel(panel, false, true);
        const next = document.createElement('button');
        next.className = 'quick-btn'; next.textContent = 'Ещё'; next.hidden = true;
        next.onclick = () => loadHistoryPanel(panel, true);
        controls.append(notice, retry, next); host.prepend(controls);
        state = {rows: [], notice, next, retry, loaded:false, pending:false};
        _historyPanels.set(panel, state);
    }
    if (state.pending || (state.loaded && !more && !refresh)) return;
    state.pending = true; state.retry.disabled = true; state.next.disabled = true;
    state.notice.textContent = 'Загрузка истории…';
    const skip = more ? state.rows.length : 0;
    try {
        const response = await fetch(`/dashboard/history/${panel}?skip=${skip}&limit=50`);
        if (!response.ok) throw Error(`HTTP ${response.status}`);
        const data = await response.json();
        state.rows = more ? state.rows.concat(data.items) : data.items;
        const body = host.querySelector('tbody');
        if (body) {
            if (more) body.insertAdjacentHTML('beforeend', data.table_rows);
            else body.innerHTML = data.table_rows;
        }
        state.notice.textContent = data.total
            ? `Показано ${state.rows.length} из ${data.total}${panel === 'quality' ? ' записей контроля; диаграммы — по всей истории, до 20 типов дефекта' : ''}`
            : 'История пока пуста';
        if (panel === 'sessions') document.getElementById('history-session-count').textContent = `(${data.total})`;
        state.next.hidden = !data.has_more;
        _renderHistoryCharts(panel, data, state.rows);
        state.loaded = true;
    } catch (error) {
        state.notice.textContent = `Не удалось загрузить историю: ${error.message}. Повторите загрузку.`;
    } finally {
        state.pending = false; state.retry.disabled = false; state.next.disabled = false;
    }
}
