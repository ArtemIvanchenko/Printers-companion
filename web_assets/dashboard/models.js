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
let _viewerGeneration = 0;

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
    return idx >= 0 ? idx : 0;
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
                <div class="model-name" title="${_esc(m.name)}">${_esc(m.name)}</div>
                <div class="model-meta">
                    <span>📦 ${_esc(m.volume_cm3)} см³</span>
                    ${m.height_mm != null ? `<span>↕ ${_esc(m.height_mm)} мм</span>` : ''}
                    <span>⏱️ ~${_esc(typeof m.est_hours === 'number' ? Number(m.est_hours.toFixed(3)) : m.est_hours)} ч</span>
                    <span>${date}</span>
                </div>
            </div>
        </div>`;
    }).join('');
}

async function addModelFromFile(file) {
    if (!file) return;
    const generation = ++_viewerGeneration;
    let buf;
    try { buf = await file.arrayBuffer(); }
    catch (error) {
        if (generation === _viewerGeneration) {
            _beginViewer(file.name);
            _viewerError(error);
        }
        return;
    }
    _stlFileBuffers[file.name] = buf;
    const fd = new FormData();
    fd.append('file', file);
    let index;
    try {
        const url = new URL('/upload/stl-estimate', location.origin);
        const r = await fetch(url, { method: 'POST', body: fd });
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        const _estH = (d.prediction?.available && d.prediction?.print_hours != null)
            ? d.prediction.print_hours
            : null;
        index = _saveModelEntry(file.name, d.volume_cm3, _estH, d.prediction?.height_mm ?? null);
    } catch(e) {
        index = _saveModelEntry(file.name, '?', '?');
    }
    loadModelHistory();
    if (generation === _viewerGeneration) openViewer(index);
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

    _beginViewer(m.name);
    document.getElementById('viewer-meta').innerHTML =
        `<span>📦 ${_esc(m.volume_cm3)} см³</span>` +
        `<span>⏱️ ~${_esc(typeof m.est_hours === 'number' ? Number(m.est_hours.toFixed(3)) : m.est_hours)} ч</span>` +
        `<span style="color:#4a5568;">${new Date(m.added_at).toLocaleString('ru')}</span>`;

    const buf = _stlFileBuffers[m.name];
    const wrap = document.getElementById('viewer-canvas-wrap');
    if (buf) {
        _showViewerBuffer(buf);
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
    const generation = _beginViewer(file.name);
    try {
        const buf = await file.arrayBuffer();
        _stlFileBuffers[file.name] = buf;
        if (generation === _viewerGeneration) _showViewerBuffer(buf);
    } catch (error) {
        if (generation === _viewerGeneration) _viewerError(error);
    }
}

function _beginViewer(name) {
    ++_viewerGeneration;
    _disposeViewer();
    const overlay = document.getElementById('viewer-overlay');
    if (!overlay.classList.contains('open')) _lastDialogTrigger = document.activeElement;
    document.getElementById('viewer-title').textContent = name;
    document.getElementById('viewer-meta').innerHTML = '';
    document.getElementById('viewer-canvas-wrap').innerHTML = '<div class="pc-empty">Загрузка…</div>';
    overlay.classList.add('open');
    document.querySelector('#viewer-overlay .icon-btn')?.focus();
    return _viewerGeneration;
}

function _disposeViewer() {
    if (_viewerAnimId != null) cancelAnimationFrame(_viewerAnimId);
    _viewerAnimId = null;
    _viewerControls?.dispose();
    _viewerControls = null;
    _viewerScene?.traverse(object => {
        object.geometry?.dispose();
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        materials.forEach(material => material?.dispose());
    });
    _viewerScene = null;
    _viewerCamera = null;
    _viewerRenderer?.dispose();
    _viewerRenderer = null;
}

function _viewerError(error) {
    _disposeViewer();
    document.getElementById('viewer-canvas-wrap').innerHTML =
        `<div class="pc-empty">Не удалось открыть модель: ${_esc(error.message)}</div>`;
}

function _showViewerBuffer(buf) {
    document.getElementById('viewer-canvas-wrap').innerHTML = '<canvas id="viewer-canvas" style="width:100%;height:100%;display:block;"></canvas>';
    try { _initThreeViewer(buf); }
    catch (error) { _viewerError(error); }
}

function closeViewer(e) {
    if (e && e.target !== document.getElementById('viewer-overlay')) return;
    ++_viewerGeneration;
    document.getElementById('viewer-overlay').classList.remove('open');
    _disposeViewer();
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
    _viewerScene = scene;
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
    _viewerCamera = camera;

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
