// HOME STATS
// =========================================================
// Everything the operator has to act on, gathered from the places that
// would otherwise each have to be visited to notice a problem.
async function loadHomeAttention() {
    const box = document.getElementById('home-attention');
    const rows = document.getElementById('home-attention-rows');
    if (!box || !rows) return;
    const items = [];

    const [unlinked, maint, powder] = await Promise.all([
        fetch('/prints/unlinked-sessions').then(r => r.json()).catch(() => null),
        fetch('/maintenance/status').then(r => r.json()).catch(() => null),
        fetch('/powder/status').then(r => r.json()).catch(() => null),
    ]);

    if (unlinked && unlinked.n_prints > 0) {
        items.push({
            color: 'var(--status-warning)',
            // Phrased so it agrees for any count: a verb here would need
            // to change with the number ("не привязана" / "не привязаны").
            text: `${unlinked.n_prints} ${_plural(unlinked.n_prints, 'печать', 'печати', 'печатей')} с логами без карточки — не считается ни точность прогноза, ни себестоимость`,
            action: () => showMainTab('prints', null),
            label: 'Привязать',
        });
    }
    for (const m of (maint || []).filter(i => i.grade !== 'ok')) {
        items.push({
            color: m.grade === 'critical' ? 'var(--status-danger)' : 'var(--status-warning)',
            text: `${m.icon || '🔧'} ${m.label}: ${m.grade === 'critical' ? 'ресурс исчерпан' : `осталось ${m.remaining} ${m.unit}`}`,
            action: () => { showMainTab('machine', null); showMachinePage('maint', null); },
            label: 'Открыть ТО',
        });
    }
    if (powder && !powder.has_batch) {
        items.push({
            color: 'var(--text-muted)',
            text: 'Партия порошка не заведена — расход и себестоимость по порошку не считаются',
            action: () => { showMainTab('machine', null); showMachinePage('powder', null); },
            label: 'Завести',
        });
    }

    if (!items.length) { box.classList.remove('is-visible'); return; }
    box.classList.add('is-visible');
    rows.innerHTML = `${_esc(items[0].text)}`;
}

function _homePrintStatus(record) {
    if (record.session_id) return ['ЛОГИ OK', 'ok'];
    return ['НЕТ ЛОГОВ', 'pending'];
}

function _homeMaterial(material) {
    const labels = { steel: 'Сталь', stainless_steel: 'Нержавейка', aluminium: 'Алюминий', aluminum: 'Алюминий', titanium: 'Титан', other: 'Материал не указан' };
    return labels[String(material || '').toLowerCase()] || material || 'Материал не указан';
}

function _homeDate(value) {
    if (!value) return 'Дата не указана';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value).slice(0, 10) : date.toLocaleDateString('ru', { day: 'numeric', month: 'short' });
}

function _formatHours(hours) {
    const totalMinutes = Math.max(0, Math.round(Number(hours) * 60));
    return `${Math.floor(totalMinutes / 60)}:${String(totalMinutes % 60).padStart(2, '0')}`;
}

function _homePreviewFallback(index) {
    const shapes = ['◈', '⬡', '◆', '▰'];
    return shapes[index % shapes.length];
}

function _renderHomeStlPreview(card, record, stlFile) {
    if (!stlFile) return;
    const preview = card.querySelector('.home-print-preview');
    if (!preview) return;
    fetch(`/prints/${encodeURIComponent(record.record_id)}/files/${encodeURIComponent(stlFile.file_id)}/download`)
        .then(response => response.ok ? response.arrayBuffer() : Promise.reject(new Error('preview unavailable')))
        .then(buffer => {
            if (typeof THREE === 'undefined' || !card.isConnected) return;
            const canvas = document.createElement('canvas');
            canvas.className = 'home-preview-canvas';
            preview.appendChild(canvas);
            const width = preview.clientWidth || 420;
            const height = preview.clientHeight || 184;
            const scene = new THREE.Scene();
            scene.add(new THREE.AmbientLight(0xffffff, .72));
            const key = new THREE.DirectionalLight(0xf3ece0, 1.1);
            key.position.set(2, 3, 4); scene.add(key);
            const fill = new THREE.DirectionalLight(0xd67f48, .25);
            fill.position.set(-3, 1, -2); scene.add(fill);
            const aspect = width / height;
            const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
            renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
            renderer.setSize(width, height, false);
            const geometry = new THREE.STLLoader().parse(buffer);
            geometry.computeBoundingBox(); geometry.center();
            const size = new THREE.Vector3(); geometry.boundingBox.getSize(size);
            const maxDim = Math.max(size.x, size.y, size.z) || 1;
            const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: 0x6f6a61, roughness: .7, metalness: .08 }));
            scene.add(mesh);
            const frustum = maxDim * .72;
            const camera = new THREE.OrthographicCamera(-frustum * aspect, frustum * aspect, frustum, -frustum, .001, 100000);
            const viewDistance = maxDim * 3;
            const setCamera = view => {
                camera.up.set(0, 1, 0);
                if (view === 'top') camera.position.set(0, 0, viewDistance);
                else if (view === 'front') { camera.position.set(0, -viewDistance, 0); camera.up.set(0, 0, 1); }
                else camera.position.set(-viewDistance, -viewDistance, viewDistance);
                camera.lookAt(0, 0, 0);
                renderer.render(scene, camera);
            };
            setCamera('45');
            preview.querySelectorAll('.pc-camera-switch button').forEach((button, buttonIndex) => button.addEventListener('click', event => {
                event.stopPropagation();
                preview.querySelectorAll('.pc-camera-switch button').forEach(item => item.classList.toggle('active', item === button));
                setCamera(['45', 'top', 'front'][buttonIndex]);
            }));
            preview.classList.add('has-model');
            card._homePreviewCleanup = () => { geometry.dispose(); mesh.material.dispose(); renderer.dispose(); };
        })
        .catch(() => {});
}

function _homePrintCard(record, index) {
    const [statusLabel, statusTone] = _homePrintStatus(record);
    const files = record.files || [];
    const stlFile = files.find(file => file.file_type === 'stl')
        || files.find(file => file.file_type === 'stl_supports');
    const summary = record.summary || {};
    const actual = summary.actual_hours != null ? _formatHours(summary.actual_hours) : '—';
    const layers = summary.layers != null ? summary.layers : '—';
    const anomalies = record.session_id ? 'АНОМ. —' : '— АНОМ.';
    const date = _homeDate(record.printed_at || record.created_at);
    const card = document.createElement('article');
    card.className = 'home-print-card';
    if (!record.session_id) card.classList.add('needs-logs');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Открыть печать ${record.name || 'без названия'}`);
    card.innerHTML = `<div class="home-print-preview">
            <span class="home-preview-fallback">${_homePreviewFallback(index)}</span>
            <span class="home-view-note">ВИД 45° · ПЕРЕД+ЛЕВО+ВЕРХ</span>
            <span class="home-print-status ${statusTone}">${_esc(statusLabel)}</span>
        </div>
        <div class="home-print-body">
            <h3 class="home-print-name" title="${_esc(record.name || 'Без названия')}">${_esc(record.name || 'Без названия')}</h3>
            <div class="home-print-topline">${_esc(String(record.record_id || '').replace(/^pr_/, 'S-').slice(0, 8))} · ${_esc(date)} · ${_esc(_homeMaterial(record.material))}</div>
            <div class="home-print-facts"><span class="home-metric">${_esc(actual)} Ф</span><span class="home-metric">${_esc(layers)} СЛ</span><span class="home-metric ${record.session_id ? '' : 'anomaly'}">${_esc(anomalies)}</span></div>
            <div class="home-print-footer"><button class="home-card-action primary" type="button">${record.session_id ? 'Открыть' : 'Приложить логи'}</button><button class="home-card-action" type="button">${record.session_id ? 'Логи' : 'Открыть'}</button>${record.session_id ? '<button class="home-card-action" type="button">Отчёт</button>' : ''}</div>
        </div>`;
    const open = () => openPrintCard(record.record_id);
    card.addEventListener('click', open);
    card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
    const actions = card.querySelectorAll('.home-card-action');
    actions.forEach((button, actionIndex) => button.addEventListener('click', event => {
        event.stopPropagation();
        if (!record.session_id && actionIndex === 0) { openNewPrintRecord(); return; }
        open();
    }));
    _renderHomeStlPreview(card, record, stlFile);
    return card;
}

function _homeAddCard() {
    const addCard = document.createElement('button');
    addCard.type = 'button';
    addCard.className = 'home-add-card';
    addCard.innerHTML = '<span class="home-add-icon">＋</span><strong>Добавить печать</strong><span>Модель и логи с флешки — разберём вместе</span>';
    addCard.addEventListener('click', () => showDesignPage('add', null));
    return addCard;
}

let _homePager = null;
let _homeLoadGeneration = 0;
let _homeLoading = false;
async function loadHomeStats(reset = true) {
    const grid = document.getElementById('home-recent-sessions');
    if (!grid) return;
    if (!reset && _homeLoading) return;
    if (reset) {
        _homeLoadGeneration++;
        loadHomeAttention();
        grid.querySelectorAll('.home-print-card').forEach(card => card._homePreviewCleanup?.());
        grid.innerHTML = '<div class="home-empty-state">Загружаем карточки…</div>';
        window._homeRecords = [];
        _homePager = null;
    }
    const generation = _homeLoadGeneration;
    const more = document.getElementById('home-load-more');
    _homeLoading = true;
    more.disabled = true;
    more.textContent = 'Загрузка…';
    try {
        const activeFilter = document.querySelector('[data-home-filter].active')?.dataset.homeFilter || 'all';
        const get = url => fetch(url).then(response => response.ok ? response.json() : Promise.reject(new Error(response.status)));
        if (!_homePager) {
            const [quality, patterns] = await Promise.all([
                activeFilter === 'without-quality' ? get('/quality-outcomes') : [],
                activeFilter === 'with-anomalies' ? get('/analysis/patterns') : {anomalies:[]},
            ]);
            if (generation !== _homeLoadGeneration) return;
            const qualityByPrint = new Set((quality || []).filter(item => item.is_final).map(item => item.print_record_id));
            const anomalySessions = new Set((patterns.anomalies || []).map(item => item.session_id));
            const suffix = activeFilter === 'with-logs' ? '&has_logs=true' : activeFilter === 'without-logs' ? '&has_logs=false' : '';
            _homePager = new CatalogPager((skip, limit) => get(`/prints?skip=${skip}&limit=${limit}${suffix}`), record =>
                activeFilter === 'without-quality' ? !qualityByPrint.has(record.record_id)
                : activeFilter === 'with-anomalies' ? anomalySessions.has(record.session_id) : true);
        }
        const pager = _homePager;
        const records = await pager.next();
        if (generation !== _homeLoadGeneration) return;
        grid.querySelectorAll('.home-empty-state, .home-add-card').forEach(node => node.remove());
        const offset = window._homeRecords.length;
        window._homeRecords.push(...records);
        records.forEach((record, index) => grid.appendChild(_homePrintCard(record, offset + index)));
        if (!window._homeRecords.length) grid.innerHTML = '<div class="home-empty-state">По этому фильтру карточек нет</div>';
        grid.appendChild(_homeAddCard());
        document.getElementById('home-prints-title').textContent = `ПЕЧАТИ · ${pager.total}`;
        document.getElementById('home-shown-count').textContent = `Показано карточек: ${window._homeRecords.length} `;
        more.hidden = !pager.hasMore;
    } catch (error) {
        if (generation !== _homeLoadGeneration) return;
        if (!window._homeRecords.length) grid.innerHTML = '<div class="home-empty-state">Не удалось загрузить карточки. Нажмите «Повторить».</div>';
        showToast('Не удалось загрузить карточки', 'error');
        more.hidden = false;
        more.textContent = 'Повторить';
    } finally {
        if (generation === _homeLoadGeneration) {
            _homeLoading = false;
            more.disabled = false;
            if (more.textContent !== 'Повторить') more.textContent = 'Ещё';
        }
    }
}

document.querySelectorAll('[data-home-filter]').forEach(button => {
    button.addEventListener('click', () => {
        document.querySelectorAll('[data-home-filter]').forEach(item => item.classList.toggle('active', item === button));
        loadHomeStats();
    });
});

// =========================================================
