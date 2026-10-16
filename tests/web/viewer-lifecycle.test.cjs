const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
    return {promise, resolve, reject};
}

function harness() {
    const nodes = new Map(), requests = [], rendered = [], storage = new Map();
    let restored = 0;
    const trigger = {focus() {restored++;}};
    for (const id of ['viewer-title', 'viewer-meta', 'viewer-overlay', 'viewer-canvas-wrap', 'viewer-canvas']) {
        const classes = new Set();
        nodes.set(id, {innerHTML: '', textContent: '', style: {},
            classList: {add: n => classes.add(n), remove: n => classes.delete(n), contains: n => classes.has(n)},
            parentElement: {clientWidth: 800, clientHeight: 460}});
    }
    storage.set('pla_models_migrated_v2', '1');
    const context = vm.createContext({URL, location: {origin: 'http://localhost'},
        FormData: class {append() {}}, window: {devicePixelRatio: 1},
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        document: {activeElement: trigger, getElementById: id => nodes.get(id), querySelector: () => ({focus() {}})},
        _lastDialogTrigger: null, _esc: value => String(value).replaceAll('<', '&lt;'),
        fetch(url) {const request = deferred(); requests.push({url, ...request}); return request.promise;},
    });
    for (const file of ['models.js', 'card-files.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard', file), 'utf8'), context);
    }
    const originalInit = context._initThreeViewer;
    context._initThreeViewer = buffer => rendered.push(buffer);
    context.loadModelHistory = () => {};
    function model(name, buffer) {
        context._saveModelEntry(name, 2, 3, 4);
        if (buffer) vm.runInContext(`_stlFileBuffers[${JSON.stringify(name)}] = ${JSON.stringify(buffer)}`, context);
    }
    function answer(index, buffer) {requests[index].resolve({ok: true, arrayBuffer: async () => buffer});}
    return {context, nodes, requests, rendered, model, answer, originalInit, restored: () => restored};
}

test('archive response after closing never initializes a hidden viewer', async () => {
    const h = harness();
    const pending = h.context.previewArchiveStl('/A', 'A');
    h.context.closeViewer(null);
    h.answer(0, 'A');
    await pending;
    assert.deepEqual(h.rendered, []);
    assert.equal(h.restored(), 1);
});

test('later archive choice wins against both a late response and a late error', async () => {
    for (const fail of [false, true]) {
        const h = harness();
        const first = h.context.previewArchiveStl('/A', 'A');
        const second = h.context.previewArchiveStl('/B', 'B');
        h.answer(1, 'B'); await second;
        const html = h.nodes.get('viewer-canvas-wrap').innerHTML;
        if (fail) h.requests[0].reject(new Error('old failure')); else h.answer(0, 'A');
        await first;
        assert.deepEqual(h.rendered, ['B']);
        assert.equal(h.nodes.get('viewer-title').textContent, 'B');
        assert.equal(h.nodes.get('viewer-canvas-wrap').innerHTML, html);
    }
});

test('history viewer supersedes pending archive download', async () => {
    const h = harness(); h.model('History', 'history');
    const pending = h.context.previewArchiveStl('/A', 'A');
    h.context.openViewer(0);
    h.answer(0, 'A'); await pending;
    assert.deepEqual(h.rendered, ['history']);
});

test('file reread cannot replace a newer archive preview', async () => {
    const h = harness(); h.model('Missing'); h.context.openViewer(0);
    const file = deferred();
    const first = h.context._reloadForViewer({name: 'Reloaded', arrayBuffer: () => file.promise});
    const second = h.context.previewArchiveStl('/B', 'B');
    h.answer(0, 'B'); await second;
    file.resolve('reloaded'); await first;
    assert.deepEqual(h.rendered, ['B']);
});

test('completed model upload preserves history without reopening a dismissed viewer', async () => {
    const h = harness();
    h.model('Previous', 'previous'); h.context.openViewer(0); h.rendered.length = 0;
    const pending = h.context.addModelFromFile({name: 'Uploaded', arrayBuffer: async () => 'uploaded'});
    await new Promise(resolve => setImmediate(resolve));
    h.context.closeViewer(null);
    h.requests[0].resolve({ok: true, json: async () => ({volume_cm3: 2, prediction: {available: true, print_hours: 3}})});
    await pending;
    assert.deepEqual(h.rendered, []);
    assert.equal(h.context._getModels()[0].name, 'Uploaded');
});

function rendererHarness() {
    const h = harness(), resources = [], frames = new Map(); let frameId = 0;
    function resource(kind) {
        const value = {kind, disposed: 0, dispose() {this.disposed++;}};
        resources.push(value); return value;
    }
    class Vector {
        constructor() {this.x = this.y = this.z = 1;}
        set() {}
    }
    class Scene {
        constructor() {this.children = [];}
        add(child) {this.children.push(child);}
        traverse(callback) {this.children.forEach(callback);}
    }
    class Light {constructor() {this.position = new Vector();}}
    h.context.THREE = {
        Scene, Color: class {}, AmbientLight: Light, DirectionalLight: Light, Vector3: Vector,
        PerspectiveCamera: class {constructor() {this.position = new Vector();} lookAt() {}},
        WebGLRenderer: class {constructor() {Object.assign(this, resource('renderer')); resources[resources.length - 1] = this;}
            setSize() {} setPixelRatio() {} render() {}},
        OrbitControls: class {constructor() {Object.assign(this, resource('controls')); resources[resources.length - 1] = this;}
            update() {}},
        STLLoader: class {parse(buffer) {
            if (buffer === 'invalid') throw new Error('invalid <STL>');
            return Object.assign(resource('geometry'), {computeBoundingBox() {}, center() {}, boundingBox: {getSize() {}}});
        }},
        MeshPhongMaterial: class {constructor() {Object.assign(this, resource('material')); resources[resources.length - 1] = this;}},
        Mesh: class {constructor(geometry, material) {Object.assign(this, {geometry, material, rotation: {}});}},
        GridHelper: class {constructor() {this.position = {}; this.geometry = resource('grid-geometry'); this.material = resource('grid-material');}},
    };
    h.context.requestAnimationFrame = callback => {frames.set(++frameId, callback); return frameId;};
    h.context.cancelAnimationFrame = id => frames.delete(id);
    h.context._initThreeViewer = h.originalInit;
    return {...h, resources, frames};
}

test('replacement and close dispose meshes, grid, controls, renderer and the animation loop once', () => {
    const h = rendererHarness(); h.model('A', 'a'); h.context.openViewer(0);
    const old = [...h.resources];
    assert.equal(h.frames.size, 1);
    h.model('B', 'b'); h.context.openViewer(0);
    assert(old.every(resource => resource.disposed === 1));
    assert.equal(h.frames.size, 1);
    h.context.closeViewer(null);
    assert(h.resources.every(resource => resource.disposed === 1));
    assert.equal(h.frames.size, 0);
    h.context.closeViewer(null);
    assert(h.resources.every(resource => resource.disposed === 1));
});

test('opening a history entry without bytes disposes the previous renderer', () => {
    const h = rendererHarness(); h.model('A', 'a'); h.context.openViewer(0);
    h.model('Missing'); h.context.openViewer(0);
    assert(h.resources.every(resource => resource.disposed === 1));
    assert.equal(h.frames.size, 0);
    assert.match(h.nodes.get('viewer-canvas-wrap').innerHTML, /Файл не в памяти/);
});

test('parse failure cleans partially initialized renderer and controls and allows retry', async () => {
    const h = rendererHarness();
    const pending = h.context.previewArchiveStl('/invalid', 'Invalid');
    h.answer(0, 'invalid'); await pending;
    assert(h.resources.every(resource => resource.disposed === 1));
    assert.equal(h.frames.size, 0);
    assert.match(h.nodes.get('viewer-canvas-wrap').innerHTML, /invalid &lt;STL>/);
    const retry = h.context.previewArchiveStl('/valid', 'Valid');
    h.answer(1, 'valid'); await retry;
    assert.equal(h.frames.size, 1);
    h.context.closeViewer(null);
    assert(h.resources.every(resource => resource.disposed === 1));
});

test('two rereads use the last selected file name and bytes, while read errors remain retryable', async () => {
    const h = harness(); h.model('Missing'); h.context.openViewer(0);
    const slow = deferred(), fast = deferred();
    const first = h.context._reloadForViewer({name: 'First', arrayBuffer: () => slow.promise});
    const second = h.context._reloadForViewer({name: 'Second', arrayBuffer: () => fast.promise});
    fast.resolve('second'); await second;
    slow.reject(new Error('late read failure')); await first;
    assert.deepEqual(h.rendered, ['second']);
    assert.equal(h.nodes.get('viewer-title').textContent, 'Second');
    const failure = h.context._reloadForViewer({name: 'Unreadable', arrayBuffer: async () => {throw new Error('read failed');}});
    await failure;
    assert.match(h.nodes.get('viewer-canvas-wrap').innerHTML, /read failed/);
    await h.context._reloadForViewer({name: 'Retry', arrayBuffer: async () => 'retry'});
    assert.deepEqual(h.rendered, ['second', 'retry']);
});

test('re-uploaded existing model opens itself instead of the first history entry', async () => {
    const h = harness(); h.model('Existing', 'old'); h.model('Most recent', 'recent');
    const pending = h.context.addModelFromFile({name: 'Existing', arrayBuffer: async () => 'updated'});
    await new Promise(resolve => setImmediate(resolve));
    h.requests[0].resolve({ok: true, json: async () => ({volume_cm3: 2, prediction: {available: true, print_hours: 3}})});
    await pending;
    assert.deepEqual(h.rendered, ['updated']);
    assert.equal(h.nodes.get('viewer-title').textContent, 'Existing');
});
