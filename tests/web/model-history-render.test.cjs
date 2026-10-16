const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const nodes = new Map(), storage = new Map([['pla_models_migrated_v2', '1']]), rendered = [];
    const context = vm.createContext({document: {
        getElementById(id) {
            if (!nodes.has(id)) {
                const classes = new Set();
                nodes.set(id, {innerHTML: '', textContent: '', style: {},
                    classList: {contains: name => classes.has(name), add: name => classes.add(name)}});
            }
            return nodes.get(id);
        }, querySelector: () => null,
    }, localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        _lastDialogTrigger: null});
    const root = path.join(__dirname, '../../web_assets/dashboard');
    const runtime = fs.readFileSync(path.join(root, 'runtime.js'), 'utf8');
    vm.runInContext(runtime.match(/function _esc\(value\) \{[\s\S]*?\n\}/)[0], context);
    vm.runInContext(fs.readFileSync(path.join(root, 'models.js'), 'utf8'), context);
    context._initThreeViewer = bytes => rendered.push(bytes);
    return {context, nodes, storage, rendered};
}

test('history treats filenames and persisted metadata as text without changing stored data or selected bytes', () => {
    const h = harness();
    const injected = `<img src=x onerror="globalThis.injected=1">`;
    const name = `Bracket & &amp; <left> "quote" 'single' ${injected}.stl`;
    h.context._saveModelEntry(name, injected, injected, injected);
    vm.runInContext(`_stlFileBuffers[${JSON.stringify(name)}] = 'selected bytes'`, h.context);
    const saved = h.storage.get('pla_models_v1');
    h.context.loadModelHistory();
    const html = h.nodes.get('models-grid').innerHTML;
    const escapedName = h.context._esc(name), escapedMetadata = h.context._esc(injected);
    assert(html.includes(`title="${escapedName}">${escapedName}</div>`));
    assert.equal(html.split(escapedMetadata).length - 1, 5);
    assert(!html.includes('<img'));
    const action = html.match(/onclick="([^"]+)"/)[1];
    assert.equal(action, 'openViewer(0)');
    vm.runInContext(action, h.context);
    assert.equal(h.nodes.get('viewer-title').textContent, name);
    assert.equal(h.nodes.get('viewer-meta').innerHTML.split(escapedMetadata).length - 1, 2);
    assert(!h.nodes.get('viewer-meta').innerHTML.includes('<img'));
    assert.deepEqual(h.rendered, ['selected bytes']);
    assert.equal(h.storage.get('pla_models_v1'), saved);
});

test('exact numeric model hours are rounded only for both display locations', () => {
    const h = harness();
    const hours = 1.234567891;
    h.context._saveModelEntry('part.stl', 2, hours, 3);
    h.context.loadModelHistory(); h.context.openViewer(0);
    for (const id of ['models-grid', 'viewer-meta']) {
        assert.match(h.nodes.get(id).innerHTML, /~1\.235 ч/);
        assert(!h.nodes.get(id).innerHTML.includes(String(hours)));
    }
    assert.equal(h.context._getModels()[0].est_hours, hours);
});

test('legacy unknown hours remain placeholders in history and viewer metadata', () => {
    const h = harness();
    for (const placeholder of ['?', '—']) {
        h.context._saveModelEntry('part.stl', 2, placeholder, 3);
        h.context.loadModelHistory(); h.context.openViewer(0);
        for (const id of ['models-grid', 'viewer-meta']) {
            assert(h.nodes.get(id).innerHTML.includes(`~${placeholder} ч`));
        }
        assert.equal(h.context._getModels()[0].est_hours, placeholder);
    }
});
