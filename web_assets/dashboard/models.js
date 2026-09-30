// MODELS — history + Three.js 3D preview
// =========================================================
const _MODEL_KEY = 'pla_models_v1';
const _stlFileBuffers = {}; // session memory: filename → ArrayBuffer

// One-time migration: reset stale est_hours that came from avg_session_hours
(function _migrateModelCache() {
    if (localStorage.getItem('pla_models_migrated_v2')) return;
    try {
        const models = JSON.parse(localStorage.getItem(_MODEL_KEY) || '[]');
        models.forEach(m => { m.est_hours = '—'; });
        localStorage.setItem(_MODEL_KEY, JSON.stringify(models));
    } catch {}
    localStorage.setItem('pla_models_migrated_v2', '1');
})();
let _viewerRenderer = null, _viewerScene = null, _viewerCamera = null;
let _viewerControls = null, _viewerAnimId = null;

function _getModels() {
    try { return JSON.parse(localStorage.getItem(_MODEL_KEY) || '[]'); } catch { return []; }
}
function _setModels(arr) { localStorage.setItem(_MODEL_KEY, JSON.stringify(arr)); }

function _saveModelEntry(name, volume_cm3, est_hours, height_mm) {
    const models = _getModels();
    const idx = models.findIndex(m => m.name === name);
    const entry = { name, volume_cm3, est_hours: est_hours ?? '—', height_mm: height_mm ?? null, added_at: new Date().toISOString() };
    if (idx >= 0) models[idx] = entry;
    else models.unshift(entry);
    _setModels(models);
}

function loadModelHistory() {
    const models = _getModels();
    const grid  = document.getElementById('models-grid');
    const empty = document.getElementById('models-empty');
    if (!models.length) {
        empty.style.display = 'block';
        grid.innerHTML = '';
        return;
    }
    empty.style.display = 'none';
    grid.innerHTML = models.map((m, i) => {
        const inMem = !!_stlFileBuffers[m.name];
        const date  = new Date(m.added_at).toLocaleDateString('ru');
        return `<div class="model-card" onclick="openViewer(${i})">
            <div class="model-thumb">${inMem ? '🔵' : '🧊'}</div>
            <div class="model-info">
                <div class="model-name" title="${m.name}">${m.name}</div>
                <div class="model-meta">
                    <span>📦 ${m.volume_cm3} см³</span>
                    ${m.height_mm != null ? `<span>↕ ${m.height_mm} мм</span>` : ''}
                    <span>⏱️ ~${m.est_hours} ч</span>
                    <span>${date}</span>
                </div>
            </div>
        </div>`;
    }).join('');
}

async function addModelFromFile(file) {
    if (!file) return;
    const buf = await file.arrayBuffer();
    _stlFileBuffers[file.name] = buf;
    const fd = new FormData();
    fd.append('file', file);
    try {
        const url = new URL('/upload/stl-estimate', location.origin);
        const r = await fetch(url, { method: 'POST', body: fd });
        const d = await r.json();
        const _estH = (d.prediction?.available && d.prediction?.print_hours != null)
            ? d.prediction.print_hours
            : d.estimate?.avg_session_hours;
        _saveModelEntry(file.name, d.volume_cm3, _estH, d.prediction?.height_mm ?? null);
        loadModelHistory();
        openViewer(0);
    } catch(e) {
        _saveModelEntry(file.name, '?', '?');
        loadModelHistory();
        openViewer(0);
    }
}

function clearModelHistory() {
    if (!confirm('Очистить историю моделей?')) return;
    _setModels([]);
    loadModelHistory();
}

function openViewer(idx) {
    const models = _getModels();
    const m = models[idx];
    if (!m) return;

    document.getElementById('viewer-title').textContent = m.name;
    document.getElementById('viewer-meta').innerHTML =
        `<span>📦 ${m.volume_cm3} см³</span>` +
        `<span>⏱️ ~${m.est_hours} ч</span>` +
        `<span style="color:#4a5568;">${new Date(m.added_at).toLocaleString('ru')}</span>`;

    _lastDialogTrigger = document.activeElement;
    document.getElementById('viewer-overlay').classList.add('open');
    document.querySelector('#viewer-overlay .icon-btn')?.focus();

    const buf = _stlFileBuffers[m.name];
    const wrap = document.getElementById('viewer-canvas-wrap');
    if (buf) {
        wrap.innerHTML = '<canvas id="viewer-canvas" style="width:100%;height:100%;display:block;"></canvas>';
        _initThreeViewer(buf);
    } else {
        wrap.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:16px;color:#6b7280;">
                <div style="font-size:48px;">📂</div>
                <div style="font-size:14px;">Файл не в памяти — загрузите ещё раз для 3D-просмотра</div>
                <label style="background:#3b82f6;color:white;border:none;border-radius:8px;padding:10px 20px;cursor:pointer;font-size:14px;">
                    Открыть файл
                    <input type="file" accept=".stl" style="display:none"
                        onchange="_reloadForViewer(this.files[0])">
                </label>
            </div>`;
    }
}

async function _reloadForViewer(file) {
    if (!file) return;
    const buf = await file.arrayBuffer();
    _stlFileBuffers[file.name] = buf;
    const wrap = document.getElementById('viewer-canvas-wrap');
    wrap.innerHTML = '<canvas id="viewer-canvas" style="width:100%;height:100%;display:block;"></canvas>';
    _initThreeViewer(buf);
}

function closeViewer(e) {
    if (e && e.target !== document.getElementById('viewer-overlay')) return;
    document.getElementById('viewer-overlay').classList.remove('open');
    if (_viewerAnimId) { cancelAnimationFrame(_viewerAnimId); _viewerAnimId = null; }
    if (_viewerRenderer) { _viewerRenderer.dispose(); _viewerRenderer = null; }
    _lastDialogTrigger?.focus?.();
    _lastDialogTrigger = null;
}

function _initThreeViewer(buf) {
    if (typeof THREE === 'undefined') {
        document.getElementById('viewer-canvas-wrap').innerHTML =
            '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#ef4444;">Three.js не загружен (нет интернета?)</div>';
        return;
    }
    const canvas = document.getElementById('viewer-canvas');
    if (!canvas) return;
    const W = canvas.parentElement.clientWidth  || 800;
    const H = canvas.parentElement.clientHeight || 460;

    // Scene + lighting
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f1419);
    scene.add(new THREE.AmbientLight(0xffffff, 0.45));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9);
    sun.position.set(1, 2, 2);
    scene.add(sun);
    const fill = new THREE.DirectionalLight(0x60a5fa, 0.35);
    fill.position.set(-2, -1, -1);
    scene.add(fill);

    // Camera
    const camera = new THREE.PerspectiveCamera(45, W / H, 0.001, 100000);

    // Renderer
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setSize(W, H);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    _viewerRenderer = renderer;

    // Controls
    const controls = new THREE.OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    _viewerControls = controls;

    // Parse STL
    const loader = new THREE.STLLoader();
    const geometry = loader.parse(buf);
    geometry.computeBoundingBox();
    geometry.center();
    const size = new THREE.Vector3();
    geometry.boundingBox.getSize(size);
    const maxDim = Math.max(size.x, size.y, size.z) || 1;

    const mat  = new THREE.MeshPhongMaterial({ color: 0x3b82f6, specular: 0x334155, shininess: 40 });
    const mesh = new THREE.Mesh(geometry, mat);
    // STL is Z-up (build/layer-stacking axis = +Z), three.js is Y-up.
    // Rotate the part so its +Z points up — the viewer now shows the SAME
    // orientation the slicer builds in (layers stack vertically on screen).
    mesh.rotation.x = -Math.PI / 2;
    scene.add(mesh);

    // Build-plate grid: the part's build axis (Z) is now vertical (Y) and
    // centred at the origin, so its bottom face — the real plate (z_min) —
    // sits at y = -size.z/2. The grid lies exactly there, not at an
    // arbitrary cosmetic offset.
    const grid = new THREE.GridHelper(maxDim * 3, 10, 0x2d3748, 0x1e2433);
    grid.position.y = -size.z / 2;
    scene.add(grid);

    camera.position.set(maxDim * 1.5, maxDim * 1.2, maxDim * 1.5);
    camera.lookAt(0, 0, 0);
    controls.update();

    function animate() {
        _viewerAnimId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();
}

// =========================================================
