// NAVIGATION — Обзор / Печати / Расчёт / Станок / ⚙
// =========================================================
// Page ids kept as they are: the screens themselves did not move, only
// which section they hang under. Renaming them would touch every
// renderer for no gain.
const SESSIONS_PAGES = { telemetry: 'telemetry' };
const PRINTS_PAGES   = { archive: 'archive' };
const SETTINGS_PAGES = { params: 'settings', logs: 'logs', update: 'update',
                         testmetrics: 'testmetrics', upload: 'upload', quality: 'quality' };
// "Станок" is the state of the machine and everything that accumulates
// between prints — service life, powder, cross-session drift, and how
// well its own predictions have been holding.
const MACHINE_PAGES  = { maint: 'maintenance', powder: 'powder',
                         patterns: 'patterns', accuracy: 'machine-accuracy', journal: 'logs' };

let _activeSessionsPage = 'telemetry';
let _activePrintsPage   = 'archive';
let _activeSettingsPage = 'params';
let _activeMachinePage  = 'maint';

function _hideAllPages() {
    document.querySelectorAll('[id^="page-"]').forEach(el => el.style.display = 'none');
}

function _setMainActive(tab) {
    document.querySelectorAll('#main-nav > a').forEach(a => {
        a.classList.remove('active');
        a.removeAttribute('aria-current');
    });
    if (tab) {
        const active = document.getElementById('nav-' + tab);
        active?.classList.add('active');
        active?.setAttribute('aria-current', 'page');
    }
}

function _showSubnav(tab) {
    ['prints','sessions','settings','machine'].forEach(t => {
        const el = document.getElementById(t + '-subnav');
        if (el) el.style.display = tab === t ? 'flex' : 'none';
    });
}

function _showSubPage(subnavId, pageId, subId) {
    _hideAllPages();
    document.getElementById('page-' + pageId).style.display = 'block';
    document.querySelectorAll('#' + subnavId + ' a').forEach(a => {
        a.classList.remove('active');
        a.removeAttribute('aria-current');
    });
    const active = document.getElementById('subnav-' + subId);
    active?.classList.add('active');
    active?.setAttribute('aria-current', 'page');
    loadHistoryPanel(pageId);
}

function showMainTab(tab, evt) {
    if (evt) evt.preventDefault();
    _hideAllPages();
    _showSubnav(tab);
    _setMainActive(tab);
    if (tab === 'home') {
        document.getElementById('page-home').style.display = 'block';
        loadHomeStats();
    } else if (tab === 'prints') {
        showPrintsPage(_activePrintsPage, null);
    } else if (tab === 'estimate') {
        // Standalone estimate: geometry in, hours and price out, no card
        // created. The model gallery already did exactly this.
        document.getElementById('page-models').style.display = 'block';
        loadModelHistory();
    } else if (tab === 'sessions') {
        showSessionsPage(_activeSessionsPage, null);
    } else if (tab === 'settings') {
        showSettingsPage(_activeSettingsPage, null);
    } else if (tab === 'machine') {
        showMachinePage(_activeMachinePage, null);
    }
}

function showPrintsPage(page, evt) {
    if (evt) evt.preventDefault();
    _activePrintsPage = page;
    _showSubnav('prints');
    _setMainActive('prints');
    _showSubPage('prints-subnav', 'archive', 'prints-archive');
    loadArchive(); loadPrintDefaults();
}

function showSessionsPage(page, evt) {
    if (evt) evt.preventDefault();
    _activeSessionsPage = page;
    _showSubnav('sessions');
    _setMainActive('sessions');
    _showSubPage('sessions-subnav', SESSIONS_PAGES[page] || page, page);
    if (page === 'patterns') loadPatterns();
    if (page === 'telemetry') loadTelemetrySessions();
}

function showSettingsPage(page, evt) {
    if (evt) evt.preventDefault();
    _activeSettingsPage = page;
    _showSubnav('settings');
    _setMainActive('settings');
    _showSubPage('settings-subnav', SETTINGS_PAGES[page] || page, 'settings-' + page);
    if (page === 'params')  loadMachineParams();
    if (page === 'logs')    loadLogs();
    if (page === 'update')  { loadVersionInfo(); loadImportStatus(); refreshUpdateCheck(); }
    if (page === 'testmetrics') loadTestMetrics(false);
}

function showMachinePage(page, evt) {
    if (evt) evt.preventDefault();
    _activeMachinePage = page;
    _showSubnav('machine');
    _setMainActive('machine');
    _showSubPage('machine-subnav', MACHINE_PAGES[page] || page, page);
    if (page === 'powder')   loadPowder();
    if (page === 'maint')    loadMaintenance();
    if (page === 'journal')  loadLogs();
    if (page === 'patterns') loadPatterns();
    if (page === 'accuracy') loadMachineAccuracy();
}

// Kept because older buttons still call it by name.
function showMaintenancePage(page, evt) {
    showMachinePage(page === 'maint' ? 'maint' : page, evt);
}

// ── New print record shortcut ──
function showDesignPage(page, evt) {
    evt?.preventDefault();
    _hideAllPages(); _showSubnav(null); _setMainActive(page === 'anomalies' ? 'machine' : page === 'settings' ? 'settings' : page);
    const target = document.getElementById(`page-design-${page}`);
    if (!target) return;
    target.style.display = 'block'; window.scrollTo(0, 0);
    ({ add: loadDesignAdd, estimate: loadDesignEstimate, anomalies: loadDesignAnomalies, quality: loadDesignQuality, journal: loadDesignJournal, settings: loadDesignSettings }[page])?.();
}


// High-fidelity implementations for the six handoff screens. These
// declarations intentionally replace the compact compatibility stubs
// above while older buttons keep their public function names.
async function _designFetch(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
        let detail = `${response.status}`;
        try { detail = (await response.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
    }
    return response.json();
}

const _DESIGN_KIND_RU = { operator_input:'ОБЩЕЕ', operator_voice:'ГОЛОС', powder:'ПОРОШОК', service:'СЕРВИС', gas:'ГАЗ' };
function _designFileKind(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    if (ext === 'stl') return ['3D-МОДЕЛЬ', 'деталь или поддержка', 'ok', 'OK'];
    if (['magics', 'mag'].includes(ext)) return ['КОМПОНОВКА', 'проект Magics', 'ok', 'OK'];
    if (['log', 'zip', 'csv', 'txt'].includes(ext)) return ['ЛОГ', 'данные станка', 'partial', 'К ПРОВЕРКЕ'];
    if (['jpg', 'jpeg', 'png', 'heic', 'webp'].includes(ext)) return ['ФОТО', 'контроль пластины', 'ok', 'OK'];
    if (['pdf', 'doc', 'docx'].includes(ext)) return ['ДОКУМЕНТ', 'вложение', 'ok', 'OK'];
    return ['ФАЙЛ', 'не распознано', 'skip', 'ПРОПУСК'];
}

async function renderDesignImportFiles(files) {
    const body = document.getElementById('design-import-files');
    const generation = (window._designImportGeneration || 0) + 1;
    window._designImportGeneration = generation;
    window._designImportPlan = null;
    document.getElementById('design-import-confirm').checked = false;
    const preview = document.getElementById('design-import-preview');
    preview.textContent = 'Проверяем состав папки…';
    try {
        const plan = await PrinterFolderImport.plan(files || []);
        if (generation !== window._designImportGeneration) return;
        window._designImportPlan = plan;
        preview.textContent = plan.description;
        body.innerHTML = plan.files.map(file => {
        const [type, recognized, statusClass, status] = _designFileKind(file.name);
        return `<tr><td>${_esc(PrinterFolderImport.pathOf(file))}</td><td>${type}</td><td>${recognized}</td><td><span class="design-status ${statusClass}">${status}</span></td></tr>`;
        }).join('');
        if (plan.manifest?.name) document.getElementById('design-print-name').value = plan.manifest.name;
    } catch (error) {
        if (generation !== window._designImportGeneration) return;
        preview.textContent = error.message;
        body.innerHTML = '<tr><td colspan="4">Импорт не начат. Исправьте состав выбранной папки.</td></tr>';
    }
}

async function loadDesignAdd() {
    const select = document.getElementById('design-print-material');
    const existing = document.getElementById('design-existing-record');
    try {
        const [defaults, prints] = await Promise.all([_designFetch('/prints/defaults'), _designFetch('/prints?limit=100')]);
        select.innerHTML = (defaults.materials || []).map(x => `<option value="${_esc(x)}">${_esc(_materialRu(x))}</option>`).join('') || '<option value="">Материал не выбран</option>';
        existing.innerHTML = '<option value="">Выберите карточку</option>' + (prints.items || []).map(x => `<option value="${_esc(x.record_id)}">${_esc(x.name)} · ${_esc(_homeDate(x.printed_at || x.created_at))}</option>`).join('');
    } catch (_) {
        select.innerHTML = '<option value="">Не удалось загрузить материалы</option>';
    }
    if (!window._designAddBound) {
        const input = document.getElementById('design-folder-input');
        input.addEventListener('change', () => renderDesignImportFiles(input.files));
        document.querySelectorAll('input[name="design-link"]').forEach(radio => radio.addEventListener('change', () => {
            const attach = document.querySelector('input[name="design-link"]:checked')?.value === 'existing';
            document.getElementById('design-existing-wrap').hidden = !attach;
            document.getElementById('design-import-submit').textContent = attach ? 'Разобрать и приложить' : 'Разобрать и создать';
        }));
        const zone = document.querySelector('#page-design-add .design-dropzone');
        zone.addEventListener('dragover', event => event.preventDefault());
        zone.addEventListener('drop', async event => {
            event.preventDefault();
            const items = [...(event.dataTransfer?.items || [])];
            try { await renderDesignImportFiles(await PrinterFolderImport.droppedFiles(items)); }
            catch (error) { showToast(error.message, 'error'); }
        });
        window._designAddBound = true;
    }
}

async function submitDesignImport() {
    const plan = window._designImportPlan;
    const files = plan?.files || [];
    if (!files.length) { showToast('Сначала выберите папку с файлами', 'error'); return; }
    if (!document.getElementById('design-import-confirm').checked) { showToast('Подтвердите состав папки после проверки', 'error'); return; }
    const button = document.getElementById('design-import-submit');
    button.disabled = true; button.textContent = 'Разбираем…';
    try {
        const attach = document.querySelector('input[name="design-link"]:checked')?.value === 'existing';
        let recordId = document.getElementById('design-existing-record').value;
        if (plan.manifest?.kind === 'logs' && !attach) {
            const fd = new FormData(); files.forEach(file => fd.append('files', file));
            const result = await _designFetch('/upload/logs', {method:'POST', body:fd});
            if (result.skipped?.length) throw new Error('Часть логов не принята: проверьте ограничения размера');
            showToast('Логи приняты без карточки модели. Подтвердите импорт.', 'success');
            return;
        }
        if (attach && !recordId) throw new Error('Выберите карточку печати');
        if (!attach) {
            const fallbackName = files[0].name.replace(/\.[^.]+$/, '').trim() || 'Без названия';
            const powder = document.getElementById('design-powder-batch').value.trim();
            const created = await _designFetch('/prints', { method:'POST', headers:_jsonHeaders(), body:JSON.stringify({ name:document.getElementById('design-print-name').value.trim() || fallbackName, material:document.getElementById('design-print-material').value || 'other', notes:powder ? `Партия порошка: ${powder}` : '' }) });
            recordId = created.record_id;
            const select = document.getElementById('design-existing-record');
            select.add(new Option(created.name, recordId, true, true));
            document.querySelector('input[name="design-link"][value="existing"]').checked = true;
            document.getElementById('design-existing-wrap').hidden = false;
        }
        const logs = files.filter(file => /\.(log|zip)$/i.test(file.name));
        const attachments = files.filter(file => !/\.(log|zip)$/i.test(file.name));
        for (const file of attachments) {
            const result = await uploadArchiveFile(recordId, file, true);
            const expected = plan.expectations.get(file);
            if (expected && !result.queued && result.checksum !== expected) throw new Error(`SHA-256 не совпал: ${file.name}`);
        }
        if (logs.length) {
            const result = await uploadArchiveLogs(recordId, logs, true);
            for (const file of logs) {
                const expected = plan.expectations.get(file);
                if (expected && !result.saved.some(row => row.name === file.name && row.checksum === expected))
                    throw new Error(`SHA-256 лога не совпал: ${file.name}`);
            }
        }
        showToast('Файлы приняты, карточка обновляется', 'success');
        openPrintCard(recordId);
    } catch (error) {
        showToast(`Не удалось импортировать: ${error.message}`, 'error');
    } finally {
        button.disabled = false;
        button.textContent = document.querySelector('input[name="design-link"]:checked')?.value === 'existing' ? 'Разобрать и приложить' : 'Разобрать и создать';
    }
}

function _hasMeasuredAccuracyPair(pair) {
    return [pair.predicted_hours, pair.actual_hours].every(value =>
        typeof value === 'number' && Number.isFinite(value) && value > 0);
}

function _designAccuracyChart(pairs) {
    const host = document.getElementById('design-accuracy-chart');
    const points = pairs.filter(_hasMeasuredAccuracyPair);
    if (!points.length) { host.innerHTML = '<div class="design-chart-empty">Нет проверенных пар «оценка + факт»</div>'; return; }
    const width=500, height=218, pad=34, max=Math.max(1, ...points.flatMap(x => [Number(x.predicted_hours), Number(x.actual_hours)])) * 1.08;
    const sx=v => pad + Number(v) / max * (width-pad*2), sy=v => height-pad-Number(v)/max*(height-pad*2);
    const worst = Math.max(...points.map(x => Math.abs(Number(x.error_pct) || 0)));
    const grid=[.25,.5,.75,1].map(t=>`<line x1="${sx(max*t)}" y1="${pad}" x2="${sx(max*t)}" y2="${height-pad}" stroke="rgba(255,255,255,.05)"/><line x1="${pad}" y1="${sy(max*t)}" x2="${width-pad}" y2="${sy(max*t)}" stroke="rgba(255,255,255,.05)"/>`).join('');
    const dots=points.map(x=>`<circle cx="${sx(x.predicted_hours)}" cy="${sy(x.actual_hours)}" r="5" fill="${Math.abs(Number(x.error_pct)||0)===worst && worst>0?'#f6a06b':'#aebf92'}"><title>${_esc(x.name || x.record_id)}: оценка ${x.predicted_hours} ч, факт ${x.actual_hours} ч</title></circle>`).join('');
    host.innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Диаграмма оценки времени против факта">${grid}<line x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${pad}" stroke="#7a8a5e" stroke-dasharray="5 5"/>${dots}<text x="${width/2}" y="${height-7}" text-anchor="middle" fill="rgba(243,236,224,.42)" font-size="10">ОЦЕНКА, Ч</text><text x="12" y="${height/2}" text-anchor="middle" fill="rgba(243,236,224,.42)" font-size="10" transform="rotate(-90 12 ${height/2})">ФАКТ, Ч</text></svg>`;
}

async function loadDesignEstimate() {
    const body = document.getElementById('design-accuracy-rows');
    try {
        const [accuracy, latestResult] = await Promise.all([_designFetch('/prints/prediction-accuracy'), _designFetch('/prints/latest-prediction')]);
        const pairs = accuracy.pairs || [];
        const latest = latestResult.record;
        const prediction = latest?.metadata_json?.prediction || null;
        if (prediction) {
            const hours = prediction.machine_hours ?? prediction.machine_cycle_hours ?? prediction.print_hours;
            document.getElementById('design-estimate-time').textContent = _formatHours(hours);
            document.getElementById('design-estimate-model').textContent = `${latest.name} · ${_materialRu(latest.material)}`;
            const interval = prediction.prediction_interval || [];
            document.getElementById('design-estimate-interval').textContent = interval.length === 2 ? `± ${_formatHours(Math.abs(interval[1]-interval[0])/2)}` : '';
            const source = {calculated:'расчёт по геометрии', calibrated:'калибровано по истории', model:'модель', heuristic:'эвристика'}[prediction.prediction_source] || prediction.method || 'расчёт';
            document.getElementById('design-estimate-source').textContent = `${source}; операторские паузы исключены`;
            const inputs=prediction.calculation_inputs||{}, thickness=prediction.layer_thickness_mm, layers=prediction.layer_count;
            const cells=[['Слои',layers],['Высота',layers&&thickness?`${(layers*thickness).toFixed(2)} мм`:'—'],['Шаг штриховки',prediction.hatch_distance_mm?`${prediction.hatch_distance_mm} мм`:'—'],['Скорость штриховки',inputs.hatch_speed_mm_s?`${inputs.hatch_speed_mm_s} мм/с`:'Не сохранена'],['Толщина слоя',thickness?`${thickness} мм`:'—'],['Лазеры',prediction.laser_count??'—']];
            document.getElementById('design-estimate-inputs').innerHTML=cells.map(([label,value])=>`<div class="design-cell"><div class="design-cell-label">${label}</div><div class="design-cell-value">${_esc(value)}</div></div>`).join('');
            const scan=Number(prediction.scan_hours)||0, recoat=(Number(prediction.recoat_hours)||0)+(Number(prediction.layer_overhead_hours)||0), total=scan+recoat;
            if(total>0) document.getElementById('design-scan-part').style.width=`${Math.round(scan/total*100)}%`;
            const historicalPair = pairs.find(pair => pair.record_id === latest.record_id);
            const warnings=[historicalPair?.excluded_reason_ru, ...(prediction.prediction_warnings||[])].filter(Boolean);
            document.getElementById('design-estimate-warning').textContent=[...new Set(warnings)].join(' ')||'Сохранённый расчёт по заданным параметрам; паузы оператора не включены.';
        } else {
            document.getElementById('design-estimate-time').textContent='—';
            document.getElementById('design-estimate-source').textContent='Нет сохранённых оценок. Откройте карточку со STL и запустите расчёт.';
        }
        const usable=pairs.filter(_hasMeasuredAccuracyPair);
        _designAccuracyChart(usable);
        const errors=usable.filter(x=>typeof x.error_pct==='number'&&Number.isFinite(x.error_pct)).map(x=>Math.abs(x.error_pct)).sort((a,b)=>a-b);
        const medianError=errors.length?(errors[Math.floor((errors.length-1)/2)]+errors[Math.floor(errors.length/2)])/2:null;
        document.getElementById('design-median-error').textContent=medianError!=null?`${medianError.toFixed(1)}%`:'—';
        document.getElementById('design-worst-error').textContent=errors.length?`${Math.max(...errors).toFixed(1)}%`:'—';
        body.innerHTML=pairs.length?pairs.slice(0,6).map(x=>`<tr><td>${_esc(x.name||x.record_id||'Печать')}${x.excluded_reason_ru?`<div class="muted">${_esc(x.excluded_reason_ru)}</div>`:''}</td><td>${_formatHours(x.predicted_hours)}</td><td>${x.actual_hours!=null?_formatHours(x.actual_hours):'Нет данных'}</td><td class="${Math.abs(x.error_pct)>20?'design-error':''}">${x.error_pct!=null?`${x.error_pct>0?'+':''}${_esc(x.error_pct)}%`:'—'}</td></tr>`).join(''):'<tr><td colspan="4" class="muted" style="text-align:center">Сохранённых пар пока нет</td></tr>';
    } catch (_) {
        body.innerHTML='<tr><td colspan="4" class="design-error">Не удалось загрузить точность</td></tr>';
        document.getElementById('design-accuracy-chart').innerHTML='<div class="design-chart-empty design-error">Ошибка загрузки данных</div>';
    }
}

async function loadDesignAnomalies() {
    const list=document.getElementById('design-anomaly-list');
    try {
        const data=await _designFetch('/analysis/patterns');
        const items=data.anomalies||[];
        window._designAnomalies=items; window._designAnomalySample=data.n_sessions_analyzed||0;
        list.innerHTML=items.length?items.slice(0,12).map((x,i)=>`<button class="design-list-item ${i===0?'active':''}" type="button" onclick="selectDesignAnomaly(${i})"><div class="design-list-meta"><span>${_esc(x.session_id||'СЕССИЯ')}</span><span>${Math.abs(Number(x.z_score)||0).toFixed(1)} MAD</span></div><strong>${_esc(x.signal_name_ru||x.signal||'Отклонение')}</strong></button>`).join(''):'<div class="design-list-item">По доступным сессиям отклонений не найдено</div>';
        document.getElementById('nav-anomaly-count').hidden=!items.length;
        document.getElementById('nav-anomaly-count').textContent=items.length||'';
        if(items[0]) selectDesignAnomaly(0); else {
            document.getElementById('design-anomaly-title').textContent='Отклонений нет';
            document.getElementById('design-anomaly-chart').innerHTML='<div class="design-chart-empty">Новых наблюдений для разбора нет</div>';
        }
    } catch (_) { list.innerHTML='<div class="design-list-item design-error">Не удалось загрузить аномалии</div>'; }
}

function selectDesignAnomaly(index) {
    const item=(window._designAnomalies||[])[index]; if(!item)return;
    document.querySelectorAll('#design-anomaly-list .design-list-item').forEach((node,i)=>node.classList.toggle('active',i===index));
    const name=item.signal_name_ru||item.signal||'Отклонение';
    document.getElementById('design-anomaly-title').textContent=`${name}: ${item.direction==='low'?'ниже':'выше'} обычного уровня`;
    document.getElementById('design-anomaly-context').textContent=`${item.session_id||'—'} · наблюдение между печатями · уверенность ${Math.round((Number(item.confidence)||0)*100)}%`;
    document.getElementById('design-anomaly-evidence').textContent=`Среднее значение ${item.value??'—'}; базовый уровень ${item.baseline_mean??'—'}; робастное отклонение ${item.z_score??'—'} MAD.`;
    const same=(window._designAnomalies||[]).filter(x=>x.signal===item.signal).length, sample=window._designAnomalySample||0;
    document.getElementById('design-anomaly-similar').textContent=`Наблюдается в ${same} из ${sample||'—'} проанализированных сессий. Это частота, а не доказательство причины дефекта.`;
    const value=Number(item.value), baseline=Number(item.baseline_mean), min=Math.min(value,baseline), max=Math.max(value,baseline), span=Math.max(Math.abs(max-min),Math.abs(max)*.12,1e-6), y=v=>168-(v-(min-span*.5))/(span*2)*118;
    document.getElementById('design-anomaly-chart').innerHTML=`<svg viewBox="0 0 500 218" role="img" aria-label="Наблюдаемое значение и базовый уровень"><rect x="356" y="24" width="82" height="160" fill="rgba(214,127,72,.16)"/><line x1="32" y1="${y(baseline)}" x2="468" y2="${y(baseline)}" stroke="#7a8a5e" stroke-dasharray="6 5"/><path d="M32 ${y(baseline)} L162 ${y(baseline)} L292 ${y(baseline)} L397 ${y(value)} L468 ${y(value)}" fill="none" stroke="#aebf92" stroke-width="2"/><circle cx="397" cy="${y(value)}" r="6" fill="#f6a06b"/><text x="38" y="${y(baseline)-8}" fill="rgba(243,236,224,.48)" font-size="10">БАЗОВЫЙ УРОВЕНЬ</text><text x="406" y="${y(value)-10}" fill="#f6a06b" font-size="10">НАБЛЮДЕНИЕ</text></svg>`;
}

async function loadDesignQuality() {
    const progress=document.getElementById('design-quality-progress');
    try {
        const [outcomes,model,risk,prints]=await Promise.all([_designFetch('/quality-outcomes'),_designFetch('/models/defect-risk'),_designFetch('/analysis/defect-risk'),_designFetch('/prints?limit=100')]);
        const latestByPrint=new Map();
        outcomes.filter(x=>x.is_final&&x.print_record_id).sort((a,b)=>String(a.timestamp||'').localeCompare(String(b.timestamp||''))).forEach(x=>latestByPrint.set(x.print_record_id,x));
        const latest=[...latestByPrint.values()], accepted=latest.filter(x=>x.result==='accepted').length, rejected=latest.filter(x=>x.result==='rejected').length, total=prints.total??prints.items.length, unknown=Math.max(0,total-latest.length);
        progress.textContent=`размечено ${latest.length} из ${total} печатей`;
        document.getElementById('design-quality-counts').innerHTML=[['ПРИНЯТО',accepted,'#aebf92'],['БРАК',rejected,'#f6a06b'],['БЕЗ ОЦЕНКИ',unknown,'#f3ece0']].map(x=>`<div class="design-count"><div class="design-section-title">${x[0]}</div><b style="color:${x[2]}">${x[1]}</b></div>`).join('');
        document.getElementById('design-quality-rows').innerHTML=(prints.items||[]).length?(prints.items||[]).slice(0,10).map(p=>{const o=latestByPrint.get(p.record_id),label=o?.result==='accepted'?'ПРИНЯТО':o?'БРАК':null;return `<tr><td>${_esc(p.name)}</td><td>${_esc(_homeDate(p.printed_at||p.created_at))}</td><td>${_esc(o?.defect_type||'—')}</td><td>${label?`<span class="design-status ${label==='ПРИНЯТО'?'ok':'partial'}">${label}</span>`:`<button class="home-btn" type="button" onclick="openPrintCard('${p.record_id}')">ОЦЕНИТЬ</button>`}</td></tr>`}).join(''):'<tr><td colspan="4" class="muted" style="text-align:center">Карточек печати пока нет</td></tr>';
        const active=model.active, n=Number(risk.n_labeled)||0, required=Number(model.policy?.min_labels)||20;
        document.getElementById('design-model-status').textContent=active?`Активная модель ${active.algorithm_version||active.model_version_id||''}. Метрики получены на временной проверке.`:'Сейчас работает объяснимая эвристика. Модель не будет активирована без временной проверки и обоих классов исходов.';
        document.getElementById('design-model-progress').style.width=`${Math.min(100,n/required*100)}%`;
        document.getElementById('design-model-progress-label').textContent=`${n} из минимум ${required} подтверждённых исходов`;
    } catch(_) { progress.textContent='Не удалось загрузить разметку'; document.getElementById('design-quality-rows').innerHTML='<tr><td colspan="4" class="design-error">Ошибка загрузки данных</td></tr>'; }
}

function renderDesignJournalRows(kind='all') {
    const box=document.getElementById('design-journal-rows'), rows=(window._designJournalRows||[]).filter(x=>kind==='all'||x.entry_kind===kind);
    box.innerHTML=rows.length?rows.slice(0,30).map(x=>`<article class="design-list-item"><div class="design-list-meta"><span>${_esc(_homeDate(x.created_at))}</span><span>${_esc(_DESIGN_KIND_RU[x.entry_kind]||'ЗАПИСЬ')}</span></div><div>${_esc(x.normalized_text||x.raw_text||'Без текста')}</div>${x.project_id?`<span class="design-journal-link">СВЯЗАНО С ПЕЧАТЬЮ · ${_esc(x.project_id)}</span>`:''}${x.operator_event_id?'<span class="design-journal-link">СВЯЗАНО С АНОМАЛИЕЙ</span>':''}</article>`).join(''):'<div class="design-list-item">Записей этого типа пока нет</div>';
}

async function loadDesignJournal() {
    const box=document.getElementById('design-journal-rows');
    try { window._designJournalRows=await _designFetch('/operator-journal'); renderDesignJournalRows('all'); } catch(_) { box.innerHTML='<div class="design-list-item design-error">Не удалось загрузить журнал</div>'; }
    if(!window._designJournalBound){document.querySelectorAll('[data-journal-filter]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-journal-filter]').forEach(x=>x.classList.toggle('active',x===button));renderDesignJournalRows(button.dataset.journalFilter);}));window._designJournalBound=true;}
}
function toggleDesignJournalForm(){document.getElementById('design-journal-form').classList.toggle('open');}
async function saveDesignJournalEntry(event){event.preventDefault();const text=document.getElementById('design-journal-text').value.trim();if(!text)return;try{await _designFetch('/operator-journal',{method:'POST',headers:_jsonHeaders(),body:JSON.stringify({source_channel:'dashboard',created_by:'operator',entry_kind:document.getElementById('design-journal-kind').value,raw_text:text,normalized_text:text,status:'confirmed'})});document.getElementById('design-journal-text').value='';document.getElementById('design-journal-form').classList.remove('open');showToast('Запись добавлена','success');loadDesignJournal();}catch(error){showToast(`Не удалось сохранить запись: ${error.message}`,'error');}}

async function loadDesignSettings() {
    const rows=document.getElementById('design-calibration-rows');
    try {
        const [settings,accuracy]=await Promise.all([_designFetch('/settings/machine'),_designFetch('/prints/prediction-accuracy')]); const p=settings.params||{};
        [['ds-hatch-speed','hatch_speed_mm_s'],['ds-hatch-distance','hatch_distance_mm'],['ds-contour-speed','contour_speed_mm_s'],['ds-jump-speed','jump_speed_mm_s'],['ds-laser-count','laser_count'],['ds-recoat-time','recoat_time_ms']].forEach(([id,key])=>document.getElementById(id).value=p[key]??'');
        document.getElementById('ds-correction-locked').checked=Boolean(p.correction_locked);
        const scan=accuracy.by_material||{}, recoat=accuracy.recoat?.by_material||{}, materials=[...new Set([...Object.keys(scan),...Object.keys(recoat)])];
        rows.innerHTML=materials.length?materials.map(material=>{const s=scan[material]||{},r=recoat[material]||{},pairs=Math.max(s.n_pairs||0,r.n_sessions||0);return `<tr><td>${_esc(_materialRu(material))}</td><td>${s.suggested_factor!=null?'×'+_esc(s.suggested_factor):'—'}</td><td>${r.suggested_recoat_ms!=null?`${Math.round(r.suggested_recoat_ms)} мс`:'—'}</td><td class="${pairs<3?'design-error':''}">${pairs}</td></tr>`}).join(''):'<tr><td colspan="4" class="muted" style="text-align:center">Калибровочных пар пока нет</td></tr>';
    } catch(_) { rows.innerHTML='<tr><td colspan="4" class="design-error">Не удалось загрузить параметры</td></tr>'; }
}
async function saveDesignMachineParams() {
    const value=id=>document.getElementById(id).value, number=id=>value(id)===''?null:Number(value(id));
    const payload={hatch_speed_mm_s:number('ds-hatch-speed'),hatch_distance_mm:number('ds-hatch-distance'),contour_speed_mm_s:number('ds-contour-speed'),jump_speed_mm_s:number('ds-jump-speed'),laser_count:number('ds-laser-count'),recoat_time_ms:number('ds-recoat-time'),correction_locked:document.getElementById('ds-correction-locked').checked};
    try { await _designFetch('/settings/machine',{method:'PUT',headers:_jsonHeaders(),body:JSON.stringify(payload)}); showToast('Параметры сохранены','success'); loadDesignSettings(); } catch(error) { showToast(`Не удалось сохранить параметры: ${error.message}`,'error'); }
}

function openNewPrintRecord() {
    showMainTab('prints', null);
    showPrintsPage('archive', null);
    const form = document.getElementById('archive-form');
    if (form) { form.style.display = 'block'; loadPrintDefaults(); }
}

// Legacy alias (used by page-newprint button)
function openNewPrint() {
    _hideAllPages();
    _showSubnav(null);
    _setMainActive(null);
    document.getElementById('page-newprint').style.display = 'block';
}

let _lastDialogTrigger = null;
function openChangelog() {
    const entries = document.querySelector('#page-update .changelog-entry');
    document.getElementById('changelog-content').innerHTML =
        entries ? entries.parentElement.innerHTML : 'Нет данных';
    _lastDialogTrigger = document.activeElement;
    document.getElementById('changelog-modal').classList.add('open');
    document.querySelector('#changelog-modal .icon-btn')?.focus();
}

function closeChangelog() {
    document.getElementById('changelog-modal').classList.remove('open');
    _lastDialogTrigger?.focus?.();
    _lastDialogTrigger = null;
}

// Legacy compat — inline onclick calls
function showGearPage(page, evt) {
    if (evt) { evt.preventDefault(); evt.stopPropagation(); }
    if (page === 'upload') showSettingsPage('upload', null);
    else if (page === 'logs') showSettingsPage('logs', null);
    else if (page === 'update') showSettingsPage('update', null);
}

function showPage(page) {
    if (page in SESSIONS_PAGES)              showSessionsPage(page, null);
    else if (page in MACHINE_PAGES)          showMachinePage(page, null);
    else if (page === 'maintenance')         showMachinePage('maint', null);
    else if (page === 'archive')             showPrintsPage('archive', null);
    else if (page === 'models')              showMainTab('estimate', null);
    else if (page === 'stl' || page === 'estimate') {
        _hideAllPages(); _showSubnav(null); _setMainActive(null);
        document.getElementById('page-stl').style.display = 'block'; loadStlMaterials();
    }
    else if (page === 'newprint')            openNewPrint();
    else if (page === 'settings')            showSettingsPage('params', null);
    else {
        const el = document.getElementById('page-' + page);
                if (el) { _hideAllPages(); el.style.display = 'block'; loadHistoryPanel(page); }
    }
}

// =========================================================
