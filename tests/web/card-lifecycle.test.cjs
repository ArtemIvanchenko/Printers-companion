const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/card-editor.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
    const nodes = new Map(), requests = [], renders = [], events = [], timers = new Map();
    let timerId = 0;
    function node(id = '') {
        const classes = new Set();
        let html = '', predictionActions = [];
        return {
            id, value: '', textContent: '', style: {display: 'none'}, listeners: {},
            isConnected: true, selectionStart: null, selectionEnd: null, selectionDirection: 'none',
            focus(options) {context.document.activeElement = this; this.focusOptions = options;},
            setSelectionRange(start, end, direction) {
                this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
            },
            remove() {if (nodes.get(this.id) === this) nodes.delete(this.id); this.isConnected = false;},
            replaceWith(existing) {this.isConnected = false; existing.isConnected = true; nodes.set(existing.id, existing);},
            classList: {add(name) {classes.add(name);},
                toggle(name, enabled) {enabled ? classes.add(name) : classes.delete(name);},
                contains(name) {return classes.has(name);}},
            setAttribute() {}, append() {}, appendChild() {}, prepend() {},
            querySelectorAll(selector) {return selector === '[data-pc-prediction-action]' ? predictionActions : [];},
            querySelector() {return null;},
            addEventListener(kind, callback) {this.listeners[kind] = callback;},
            get innerHTML() {return html;},
            set innerHTML(value) {
                html = value;
                predictionActions = [...value.matchAll(/<button[^>]+data-pc-prediction-action="([^"]+)"[^>]*>/g)]
                    .map(match => ({dataset: {pcPredictionAction: match[1]}, listeners: {},
                        addEventListener(kind, callback) {this.listeners[kind] = callback;}}));
                if (value.includes('id="qo-save-status"')) {
                    for (const id of ['qo-save-status','qo-result','qo-inspection-type','qo-inspection-result',
                            'qo-defect-type','qo-defect-location','qo-layer-start','qo-layer-end','qo-notes']) {
                        nodes.set(id, node(id));
                    }
                }
                if (!value.includes('id="pc-name"')) return;
                const oldHost = nodes.get('pc-preview-host');
                if (oldHost) oldHost.isConnected = false;
                const previewHost = node('pc-preview-host'), preview = node();
                const cameraButtons = [node(), node(), node()];
                cameraButtons[0].classList.toggle('active', true);
                preview.querySelectorAll = selector => selector === '.pc-camera-switch button' ? cameraButtons : [];
                previewHost.querySelector = selector => selector === '.home-print-preview' ? preview : null;
                nodes.set(previewHost.id, previewHost);
                if (['pc-name','pc-material','pc-thickness','pc-powder','pc-notes'].includes(context.document.activeElement?.id)) {
                    context.document.activeElement = null;
                }
                // Re-rendering the form replaces its live inputs, like the DOM.
                for (const name of ['pc-name', 'pc-thickness', 'pc-powder']) {
                    const input = node(name);
                    input.value = value.match(new RegExp(`id="${name}"[^>]*value="([^"]*)"`))?.[1] || '';
                    nodes.set(name, input);
                }
                const material = node('pc-material');
                material.value = value.match(/<option value="([^"]*)" selected>/)?.[1] || '';
                nodes.set(material.id, material);
                const notes = node('pc-notes');
                notes.value = value.match(/<textarea id="pc-notes"[^>]*>([\s\S]*?)<\/textarea>/)?.[1] || '';
                nodes.set(notes.id, notes);
                nodes.set('pc-save-result', node('pc-save-result'));
            },
        };
    }
    const get = id => {if (!nodes.has(id)) nodes.set(id, node(id)); return nodes.get(id);};
    const tabs = ['what', 'plan', 'outcome', 'process', 'files'].map(name => get('pctab-' + name));
    const panes = ['what', 'plan', 'outcome', 'process', 'files'].map(name => get('pcpane-' + name));
    const context = vm.createContext({
        document: {
            activeElement: null,
            getElementById: get, createElement: () => node(),
            querySelectorAll: selector => selector === '.pc-pane' ? panes : selector === '#pc-tabs a' ? tabs : [],
            querySelector: selector => selector === '#pc-tabs a.active' ? tabs.find(tab => tab.classList.contains('active')) : null,
        },
        fetch(url, options = {}) {
            let resolve, reject;
            const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
            requests.push({url, options, resolve, reject, claimed: false, answered: false});
            return promise;
        },
        EventSource: class {
            constructor(url) {this.url = url; this.listeners = {}; events.push(this);}
            addEventListener(kind, callback) {this.listeners[kind] = callback;}
        },
        setTimeout(callback) {timers.set(++timerId, callback); return timerId;},
        clearTimeout(id) {timers.delete(id);}, scrollTo() {},
        _hideAllPages() {for (const [id, element] of nodes) if (id.startsWith('page-')) element.style.display = 'none';},
        _showSubnav() {}, _setMainActive() {}, _renderHomeStlPreview() {},
        _homePreviewFallback: () => '', _materialRu: value => value || '', _formatHours: value => `${value} ч`,
        _plural: (_, one, two, many) => many,
        _esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
        _jsonHeaders: () => ({'Content-Type': 'application/json'}),
        _renderPcOutcome() {}, _renderPcFiles() {},
        _renderPcProcess() {renders.push(state());},
        _pollEstimate() {}, loadArchive() {}, _archiveSkip: 0,
    });
    context.window = context;
    vm.runInContext(source, context, {filename: 'card-editor.js'});
    function state() {
        return JSON.parse(vm.runInContext(`JSON.stringify({
            id:_pcRecord?.record_id, revision:_pcRecord?.revision, dirty:_pcDirty,
            session:_pcSession?.session_id ?? null,
            report:_pcOperatorReport?.record_id ?? null,
            reportRevision:_pcOperatorReport?.revision ?? null,
            risk:_pcDefectRisk?.session_id ?? null
        })`, context));
    }
    function take(url, method = 'GET') {
        const request = requests.find(item => !item.claimed && item.url === url && (item.options.method || 'GET') === method);
        assert(request, `Missing ${method} ${url}`);
        request.claimed = true;
        return request;
    }
    async function answer(request, data, status = 200) {
        request.answered = true;
        request.resolve({ok: status >= 200 && status < 300, status, json: async () => data});
        await tick();
    }
    async function details(record, {status = 200} = {}) {
        const payloads = [
            [`/sessions/${record.session_id}`, {session_id: record.session_id, features: {}}],
            [`/prints/${record.record_id}/operator-report`, {record_id: record.record_id, revision: record.revision}],
            [`/analysis/defect-risk/${record.session_id}`, {session_id: record.session_id}],
        ];
        for (const [url, payload] of payloads) {
            const pending = requests.find(item => !item.claimed && item.url === url);
            if (pending) await answer(take(url), payload, status);
        }
    }
    async function open(record, tab) {
        const pending = context.openPrintCard(record.record_id, tab);
        await answer(take(`/prints/${record.record_id}`), record);
        await details(record);
        await pending;
    }
    function editName(value) {const input = get('pc-name'); input.value = value; input.listeners.input();}
    function incomingSync() {
        events[0].listeners['print-records'](); // Initial EventSource snapshot.
        events[0].listeners['print-records']();
        for (const [id, callback] of [...timers]) {timers.delete(id); callback();}
    }
    function completionModules() {
        for (const name of ['archive', 'card-analysis']) {
            vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard', name + '.js'), 'utf8'), context);
        }
        const renderProcess = context._renderPcProcess;
        context._renderPcProcess = () => {renderProcess(); renders.push(state());};
        context.showToast = () => {};
        context.scoreColor = () => 'muted';
    }
    return {context, nodes, get, requests, renders, timers, state, take, answer, details, open, editName, incomingSync, completionModules};
}

function record(id, revision = 1) {
    return {record_id: id, revision, session_id: `s-${id}-${revision}`, name: `${id}-${revision}`,
        material: 'steel', layer_thickness_mm: .1, files: [], metadata_json: {}};
}

function coherent(value, rec) {
    assert.equal(value.id, rec.record_id);
    assert.equal(value.revision, rec.revision);
    assert.equal(value.session, rec.session_id);
    assert.equal(value.report, rec.record_id);
    assert.equal(value.reportRevision, rec.revision);
    assert.equal(value.risk, rec.session_id);
}

test('a slower earlier card cannot replace the last opened card', async () => {
    const h = harness(), a = record('A'), b = record('B');
    const first = h.context.openPrintCard('A');
    const firstRequest = h.take('/prints/A');
    await h.open(b);
    await h.answer(firstRequest, a);
    await h.details(a);
    await first;
    coherent(h.state(), b);
    assert.deepEqual(h.renders.map(value => value.id), ['B']);
});

test('related responses publish with their local card, never a newer global card', async () => {
    const h = harness(), a = record('A'), b = record('B');
    const first = h.context.openPrintCard('A');
    await h.answer(h.take('/prints/A'), a);
    const second = h.context.openPrintCard('B');
    await h.answer(h.take('/prints/B'), b);
    await h.details(a);
    await first;
    await h.details(b);
    await second;
    assert.equal(h.renders.length, 1);
    coherent(h.renders[0], b);
});

test('navigation away invalidates unfinished card publication', async () => {
    const h = harness(), a = record('A');
    const opening = h.context.openPrintCard('A');
    h.get('page-print-card').style.display = 'none';
    await h.answer(h.take('/prints/A'), a);
    await h.details(a);
    await opening;
    assert.equal(h.renders.length, 0);
    assert.equal(h.state().id, undefined);
});

test('an old load failure does not replace the newer card title', async () => {
    const h = harness(), b = record('B');
    const first = h.context.openPrintCard('A');
    const old = h.take('/prints/A');
    await h.open(b);
    const title = h.get('pc-title').textContent;
    old.reject(new Error('old request failed'));
    await first;
    assert.equal(h.get('pc-title').textContent, title);
    coherent(h.state(), b);
});

test('an incoming NAS sync cannot overwrite edits made during related fetches', async () => {
    const h = harness(), a = record('A'), fresh = record('A', 2);
    await h.open(a);
    h.incomingSync();
    await h.answer(h.take('/prints/A'), fresh);
    h.editName('unsaved draft');
    await h.details(fresh);
    await tick();
    assert.equal(h.get('pc-name').value, 'unsaved draft');
    assert.equal(h.state().revision, 1);
    assert.equal(h.state().dirty, true);
    assert.equal(h.renders.length, 1);
    assert.match(h.get('pc-save-result').innerHTML, /не перезаписаны/);
});

test('sync started on A cannot publish its related data after navigation to B', async () => {
    const h = harness(), a = record('A'), fresh = record('A', 2), b = record('B');
    await h.open(a);
    const sync = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), fresh);
    await h.open(b);
    await h.details(fresh);
    await sync;
    coherent(h.state(), b);
    assert.equal(h.renders.length, 2);
});

test('incoming sync does not fetch the previous card while a new card is loading', async () => {
    const h = harness(), b = record('B');
    await h.open(record('A'));
    const opening = h.context.openPrintCard('B');
    const refresh = h.context._refreshOpenPrintCardFromSync();
    const stale = h.requests.find(item => !item.claimed && item.url === '/prints/A');
    if (stale) {
        await h.answer(h.take('/prints/A'), record('A', 2));
        await h.details(record('A', 2));
    }
    await refresh;
    const requestsForA = h.requests.filter(item => item.url === '/prints/A').length;
    await h.answer(h.take('/prints/B'), b);
    await h.details(b);
    await opening;
    assert.equal(requestsForA, 1);
    coherent(h.state(), b);
});

test('sync preserves the tab selected while its related data is loading', async () => {
    const h = harness(), fresh = record('A', 2);
    await h.open(record('A'));
    const sync = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), fresh);
    h.context.showPrintCardTab('files');
    await h.details(fresh);
    await sync;
    assert.equal(h.get('pctab-files').classList.contains('active'), true);
    coherent(h.state(), fresh);
});

test('an older overlapping sync cannot replace the latest published revision', async () => {
    const h = harness(), first = record('A', 2), latest = record('A', 3);
    await h.open(record('A'));
    const earlier = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), first);
    await h.answer(h.take(`/sessions/${first.session_id}`), {session_id: first.session_id, features: {}});
    const earlierReport = h.take('/prints/A/operator-report');
    const newer = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), latest);
    await h.answer(h.take(`/sessions/${latest.session_id}`), {session_id: latest.session_id, features: {}});
    await h.answer(h.take('/prints/A/operator-report'), {record_id: 'A', revision: 3});
    await h.answer(h.take(`/analysis/defect-risk/${latest.session_id}`), {session_id: latest.session_id});
    await newer;
    await h.answer(earlierReport, {record_id: 'A', revision: 2});
    await h.answer(h.take(`/analysis/defect-risk/${first.session_id}`), {session_id: first.session_id});
    await earlier;
    coherent(h.state(), latest);
    assert.equal(h.renders.length, 2);
});

test('sync does not advance the revision behind the still visible old form', async () => {
    const h = harness(), fresh = record('A', 2);
    await h.open(record('A'));
    const sync = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), fresh);
    h.editName('local edit');
    const saving = h.context.savePrintCard();
    const request = h.take('/prints/A', 'PATCH');
    const revision = JSON.parse(request.options.body).expected_revision;
    await h.answer(request, {detail: {message: 'revision conflict'}}, 409);
    await saving;
    await h.details(fresh);
    await sync;
    assert.equal(revision, 1);
    assert.equal(h.get('pc-name').value, 'local edit');
    assert.equal(h.state().dirty, true);
});

test('a save reply for A cannot mutate card B or clear its unsaved edits', async () => {
    const h = harness(), b = record('B');
    await h.open(record('A'));
    h.editName('saved A');
    const saving = h.context.savePrintCard();
    const request = h.take('/prints/A', 'PATCH');
    await h.open(b);
    h.editName('draft B');
    await h.answer(request, {...record('A', 2), name: 'saved A'});
    await saving;
    coherent(h.state(), b);
    assert.equal(h.state().dirty, true);
    assert.equal(h.get('pc-name').value, 'draft B');
});

test('edits made while a save awaits its response remain unsaved and protected', async () => {
    const h = harness();
    await h.open(record('A'));
    h.editName('submitted');
    const saving = h.context.savePrintCard();
    const request = h.take('/prints/A', 'PATCH');
    h.editName('next unsaved edit');
    await h.answer(request, {...record('A', 2), name: 'submitted'});
    await saving;
    assert.equal(h.get('pc-name').value, 'next unsaved edit');
    assert.equal(h.state().revision, 2);
    assert.equal(h.state().dirty, true);
});

test('optional linked-data failures still open the requested card coherently', async () => {
    const h = harness(), a = record('A');
    const opening = h.context.openPrintCard('A');
    await h.answer(h.take('/prints/A'), a);
    await h.details(a, {status: 503});
    await opening;
    assert.equal(h.state().id, 'A');
    assert.equal(h.state().session, null);
    assert.equal(h.state().report, null);
    assert.equal(h.state().risk, null);
    assert.equal(h.renders.length, 1);
});

test('unchanged sync and a successful save retain revision and dirty semantics', async () => {
    const h = harness(), a = record('A');
    await h.open(a);
    const sync = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), a);
    await sync;
    assert.equal(h.renders.length, 1);
    h.editName('saved name');
    const saving = h.context.savePrintCard();
    const request = h.take('/prints/A', 'PATCH');
    assert.equal(JSON.parse(request.options.body).expected_revision, 1);
    await h.answer(request, {...record('A', 2), name: 'saved name'});
    await saving;
    assert.equal(h.state().revision, 2);
    assert.equal(h.state().dirty, false);
    assert.match(h.get('pc-save-result').innerHTML, /Сохранено/);
});

function predictedRecord(id, revision = 2) {
    return {...record(id, revision), session_id: null,
        prediction_is_current: true, prediction_configuration_is_current: true,
        summary: {comparison_scope: 'burn_plus_pour', predicted_hours: 10},
        metadata_json: {prediction: {input_revision: 1, print_hours: 10,
            cost_total_rub: 1000, layer_count: 123, material: 'steel'}}};
}

function submitRenderedSave(h) {
    const callback = h.get('pcpane-what').innerHTML.match(/onclick="(savePrintCard\(\))"/)?.[1];
    assert(callback, 'The generated form must expose its real save action');
    return vm.runInContext(callback, h.context);
}

test('saving a changed material publishes stale plan and hero immediately, even when sync has the same revision', async () => {
    const h = harness(), original = predictedRecord('saved-material');
    const recalculations = [];
    h.context.estimateRecord = (id, stayOnCard) => recalculations.push({id, stayOnCard});
    await h.open(original, 'what');
    assert.match(h.get('pcpane-plan').innerHTML, /Прогноз по текущим настройкам/);
    assert.match(h.get('pcpane-what').innerHTML, />10 ч</);
    const material = h.get('pc-material');
    material.value = 'titanium'; material.listeners.change();
    const saving = submitRenderedSave(h);
    const request = h.take('/prints/saved-material', 'PATCH');
    assert.equal(JSON.parse(request.options.body).material, 'titanium');
    const committed = {...original, revision: 3, material: 'titanium',
        prediction_is_current: false, prediction_configuration_is_current: null};
    await h.answer(request, committed); await saving;
    assert.equal(h.state().dirty, false);
    assert.match(h.get('pc-save-result').innerHTML, /Сохранено/);
    assert.equal(h.get('pctab-what').classList.contains('active'), true);
    h.context.showPrintCardTab('plan');
    const plan = h.get('pcpane-plan').innerHTML;
    assert(!plan.includes('Прогноз по текущим настройкам'));
    assert.match(plan, /Прогноз устарел/);
    assert.match(plan, /Исторический снимок/);
    assert.match(plan, /Прожиг \+ нанесение<\/span><span>10 ч/);
    assert(!h.get('pcpane-what').innerHTML.includes('>10 ч<'));
    assert.match(h.get('pcpane-what').innerHTML, /Слоёв по геометрии<\/span><span>—/);
    const sync = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/saved-material'), committed); await sync;
    assert.equal(h.get('pcpane-plan').innerHTML, plan);
    assert.equal(h.renders.length, 1, 'Saving must not rebuild QC, process or files');
    assert.equal(recalculations.length, 0, 'Publishing saved state must not auto-enqueue a calculation');
    const actions = h.get('pcpane-plan').querySelectorAll('[data-pc-prediction-action]');
    actions.find(button => button.dataset.pcPredictionAction === 'recalculate').listeners.click();
    assert.deepEqual(recalculations, [{id: 'saved-material', stayOnCard: true}]);
    actions.find(button => button.dataset.pcPredictionAction === 'files').listeners.click();
    assert.equal(h.get('pcpane-files').style.display, 'block');
});

test('a partial PATCH honestly clears the displayed configuration match without hiding the saved estimate', async () => {
    const h = harness(), original = predictedRecord('saved-configuration', 1);
    await h.open(original, 'plan');
    h.editName('new name');
    const saving = submitRenderedSave(h);
    await h.answer(h.take('/prints/saved-configuration', 'PATCH'), {...original, revision: 2,
        name: 'new name', prediction_configuration_is_current: null});
    await saving;
    const plan = h.get('pcpane-plan').innerHTML;
    assert(!plan.includes('Прогноз по текущим настройкам'));
    assert(!plan.includes('Настройки расчёта изменились'));
    assert.match(plan, /Сохранённый прогноз/);
    assert.match(plan, /Совпадение с текущими настройками не подтверждено/);
    assert.match(plan, /Прожиг \+ нанесение<\/span><span>10 ч/);
    assert.match(h.get('pcpane-what').innerHTML, />10 ч</);
    assert.equal(h.get('pctab-plan').classList.contains('active'), true);
    assert.equal(h.requests.filter(item => item.url === '/prints/saved-configuration'
        && (item.options.method || 'GET') === 'GET').length, 1, 'Save adds no replacement read/poll mechanism');
});

test('saved projections preserve every newer live card field and the independent real QC draft', async () => {
    const h = harness(), original = predictedRecord('saved-drafts');
    h.completionModules();
    await h.open(original, 'outcome');
    h.editName('submitted');
    const saving = submitRenderedSave(h);
    const request = h.take('/prints/saved-drafts', 'PATCH');
    const values = {'pc-name': 'new name draft', 'pc-material': 'titanium',
        'pc-thickness': '0.060', 'pc-powder': '1700', 'pc-notes': ' untrimmed newer note '};
    for (const [id, value] of Object.entries(values)) {
        const input = h.get(id); input.value = value;
        input.listeners[id === 'pc-material' ? 'change' : 'input']();
    }
    const qualityNote = h.get('qo-notes');
    qualityNote.value = 'QC note while save was pending'; qualityNote.listeners.input();
    await h.answer(request, {...original, revision: 3, name: 'submitted',
        prediction_is_current: false, prediction_configuration_is_current: null});
    await saving;
    for (const [id, value] of Object.entries(values)) assert.equal(h.get(id).value, value);
    assert.equal(h.get('qo-notes'), qualityNote, 'QC form must not be rebuilt');
    assert.equal(qualityNote.value, 'QC note while save was pending');
    assert.equal(vm.runInContext('_pcQualityDirty', h.context), true);
    assert.equal(h.state().dirty, true);
    assert.match(h.get('pc-save-result').innerHTML, /новые несохранённые правки/);
    assert.equal(h.get('pctab-outcome').classList.contains('active'), true);
    assert.match(h.get('pcpane-plan').innerHTML, /Исторический снимок/);
    h.get('pc-name').value = 'still editable'; h.get('pc-name').listeners.input();
    assert.equal(h.state().dirty, true);
});

test('successful saves retain the same loaded preview, selected camera and its single actual download', async () => {
    const h = harness(), renderedCameras = [];
    let parsed = 0, renderers = 0, disposed = 0;
    class Vector {set(...values) {this.values = values;}}
    const geometry = {computeBoundingBox() {}, center() {}, dispose() {disposed++;},
        boundingBox: {getSize(size) {Object.assign(size, {x: 2, y: 3, z: 4});}}};
    h.context.THREE = {
        Vector3: Vector, Scene: class {add() {}}, AmbientLight: class {},
        DirectionalLight: class {constructor() {this.position = new Vector();}},
        OrthographicCamera: class {constructor() {this.position = new Vector(); this.up = new Vector();} lookAt() {}},
        STLLoader: class {parse() {parsed++; return geometry;}},
        Mesh: class {constructor(shape, material) {this.material = material;}},
        MeshStandardMaterial: class {dispose() {}},
        WebGLRenderer: class {constructor() {renderers++;} setPixelRatio() {} setSize() {} dispose() {}
            render(scene, camera) {renderedCameras.push([...camera.position.values]);}},
    };
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/catalog.js'), 'utf8'), h.context);
    const original = {...predictedRecord('preview-save'), files: [{file_id: 'model', file_type: 'stl', file_name: 'body.stl'}]};
    await h.open(original, 'what');
    const download = h.take('/prints/preview-save/files/model/download');
    download.resolve({ok: true, arrayBuffer: async () => new ArrayBuffer(1)}); await tick();
    const host = h.get('pc-preview-host');
    const buttons = host.querySelector('.home-print-preview').querySelectorAll('.pc-camera-switch button');
    buttons[2].listeners.click({stopPropagation() {}});
    assert.deepEqual(renderedCameras.at(-1), [0, -12, 0]);
    for (const revision of [3, 4]) {
        h.editName('saved preview ' + revision);
        const saving = submitRenderedSave(h);
        await h.answer(h.take('/prints/preview-save', 'PATCH'), {...original, revision,
            name: 'saved preview ' + revision, prediction_is_current: false, prediction_configuration_is_current: null});
        await saving;
        assert.equal(h.get('pc-preview-host'), host);
        assert.equal(host.isConnected, true);
        assert.equal(buttons[2].classList.contains('active'), true);
        assert.deepEqual(renderedCameras.at(-1), [0, -12, 0]);
    }
    assert.equal(h.requests.filter(item => item.url.endsWith('/files/model/download')).length, 1);
    assert.equal(parsed, 1); assert.equal(renderers, 1); assert.equal(disposed, 0);
    await h.open(record('another-card'), 'what');
    assert.notEqual(h.get('pc-preview-host'), host);
    assert.equal(disposed, 1, 'Ordinary navigation must still dispose the retained preview');
});

test('save completion restores the focused newer text draft and its selection without moving tabs', async () => {
    const h = harness(), original = predictedRecord('focused-save');
    await h.open(original, 'what');
    h.editName('submitted');
    const saving = submitRenderedSave(h);
    const input = h.get('pc-notes');
    input.value = 'newer note being edited'; input.listeners.input();
    input.focus(); input.setSelectionRange(3, 14, 'backward');
    await h.answer(h.take('/prints/focused-save', 'PATCH'), {...original, revision: 3,
        name: 'submitted', prediction_is_current: false, prediction_configuration_is_current: null});
    await saving;
    const restored = h.get('pc-notes');
    assert.equal(h.context.document.activeElement, restored);
    assert.equal(restored.value, 'newer note being edited');
    assert.deepEqual([restored.selectionStart, restored.selectionEnd, restored.selectionDirection], [3, 14, 'backward']);
    assert.equal(restored.focusOptions.preventScroll, true);
    assert.equal(h.state().dirty, true);
    assert.equal(h.get('pctab-what').classList.contains('active'), true);
});

for (const tab of ['what', 'process', 'outcome', 'files']) {
    test(`opening a card selects the requested existing ${tab} tab after publication`, async () => {
        const h = harness(), a = record('A');
        await h.open(a, tab);
        coherent(h.state(), a);
        assert.equal(h.get('pctab-' + tab).classList.contains('active'), true);
        assert.equal(h.get('pcpane-' + tab).style.display, 'block');
    });
}

test('opening the next card hides the previous visible panes while its record loads', async () => {
    const h = harness(), b = record('B');
    await h.open(record('A'), 'what');
    const opening = h.context.openPrintCard('B', 'outcome');
    try {
        for (const tab of ['what', 'plan', 'outcome', 'process', 'files']) {
            assert.equal(h.get('pcpane-' + tab).style.display, 'none');
        }
    } finally {
        await h.answer(h.take('/prints/B'), b); await h.details(b); await opening;
    }
});

test('public tab selection cannot display the previous card while the next card loads', async () => {
    const h = harness(), b = record('B');
    await h.open(record('A'));
    const opening = h.context.openPrintCard('B', 'process');
    try {
        h.context.showPrintCardTab('files');
        assert.equal(h.get('pcpane-files').style.display, 'none');
    } finally {
        await h.answer(h.take('/prints/B'), b); await h.details(b); await opening;
    }
    assert.equal(h.get('pctab-process').classList.contains('active'), true);
    coherent(h.state(), b);
});

test('an earlier requested initial tab cannot replace the latest card tab', async () => {
    const h = harness(), a = record('A'), b = record('B');
    const earlier = h.context.openPrintCard('A', 'outcome');
    await h.answer(h.take('/prints/A'), a);
    await h.open(b, 'files');
    await h.details(a); await earlier;
    coherent(h.state(), b);
    assert.equal(h.get('pctab-files').classList.contains('active'), true);
    assert.equal(h.renders.length, 1);
});

test('each sync disposes the existing preview before replacing its host', async () => {
    const h = harness(), previews = [], disposed = [];
    h.context._renderHomeStlPreview = host => {
        previews.push(host);
        host._homePreviewCleanup = () => {
            assert.equal(h.get('pc-preview-host'), host);
            disposed.push(host);
        };
    };
    await h.open(record('A'));
    for (const revision of [2, 3]) {
        const fresh = record('A', revision);
        const sync = h.context._refreshOpenPrintCardFromSync();
        await h.answer(h.take('/prints/A'), fresh); await h.details(fresh); await sync;
    }
    assert.equal(previews.length, 3);
    assert.deepEqual(disposed, previews.slice(0, -1));
    assert.equal(h.state().revision, 3);
});

test('what-pane fallback attach action never starts new-card creation', async () => {
    const h = harness();
    await h.open(record('A'));
    const html = h.get('pcpane-what').innerHTML;
    assert(!html.includes('onclick="openNewPrintRecord()"'));
    assert.match(html, /onclick="showPrintCardTab\('files'\)"[^>]*>Приложить логи/);
});

test('the requested files tab retains the existing log picker bound to that card', async () => {
    const h = harness(), uploads = [];
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/card-files.js'), 'utf8'), h.context);
    h.context._FILE_ICON = {};
    h.context.uploadArchiveLogs = (recordId, files) => uploads.push({recordId, files});
    const existing = {...record('existing-card'), session_id: null};
    await h.open(existing, 'files');
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.match(h.get('pcpane-files').innerHTML, /id="pc-attach-logs"/);
    const files = [{name: 'existing-print.log'}];
    h.get('pc-attach-logs').listeners.change({target: {files}});
    assert.deepEqual(uploads, [{recordId: 'existing-card', files}]);
    assert.equal(h.state().id, 'existing-card');
});

test('latest B failure clears A context and cannot reopen or save its hidden form', async () => {
    const h = harness();
    await h.open(record('A'));
    h.editName('abandoned A draft');
    const opening = h.context.openPrintCard('B', 'files');
    await h.answer(h.take('/prints/B'), {detail: 'unavailable'}, 503);
    await opening;
    h.context.showPrintCardTab('files');
    const saving = h.context.savePrintCard();
    const wrongSave = h.requests.find(request => request.options.method === 'PATCH' && !request.answered);
    // Settle the baseline's unwanted request too; no hanging mocked work.
    if (wrongSave) await h.answer(wrongSave, {detail: {message: 'wrong card context'}}, 409);
    await saving;
    assert.equal(h.state().id, undefined);
    assert.equal(h.state().session, null);
    assert.equal(h.state().report, null);
    assert.equal(h.state().risk, null);
    assert.equal(h.state().dirty, false);
    assert.equal(wrongSave, undefined);
    assert.equal(h.get('pc-title').textContent, 'Не удалось загрузить карточку');
    for (const tab of ['what', 'plan', 'outcome', 'process', 'files']) {
        assert.equal(h.get('pcpane-' + tab).style.display, 'none');
    }
    assert.equal(h.renders.length, 1);
});

test('a stale A failure cannot clear valid B or its local edits', async () => {
    const h = harness(), b = record('B');
    const earlier = h.context.openPrintCard('A', 'process');
    const failed = h.take('/prints/A');
    await h.open(b, 'outcome');
    h.editName('B draft');
    failed.reject(new Error('stale A unavailable')); await earlier;
    coherent(h.state(), b);
    assert.equal(h.state().dirty, true);
    assert.equal(h.get('pc-name').value, 'B draft');
    assert.equal(h.get('pcpane-outcome').style.display, 'block');
});

test('B can be retried successfully after its own load failure without retaining A data', async () => {
    const h = harness(), b = record('B');
    await h.open(record('A'));
    const opening = h.context.openPrintCard('B', 'process');
    await h.answer(h.take('/prints/B'), {detail: 'unavailable'}, 503); await opening;
    assert.equal(h.state().id, undefined);
    await h.open(b, 'process');
    coherent(h.state(), b);
    assert.equal(h.state().dirty, false);
    assert.equal(h.get('pcpane-process').style.display, 'block');
    assert.deepEqual(h.renders.map(value => value.id), ['A', 'B']);
});

async function startEstimate(h, id = 'A') {
    const callback = h.get('pcpane-what').innerHTML.match(/onclick="(estimateRecord\('[^']+',true\))"/)[1];
    const accepted = vm.runInContext(callback, h.context);
    await h.answer(h.take(`/prints/${id}/estimate`, 'POST'), {job_id: 'estimate-' + id});
    await accepted;
    return h.take('/background-analysis/jobs/estimate-' + id);
}

async function settleCardRefresh(h, fresh) {
    const pending = h.requests.find(request => !request.claimed && request.url === `/prints/${fresh.record_id}`);
    if (pending) {await h.answer(h.take(pending.url), fresh); await h.details(fresh);}
    await tick();
}

function startQualitySave(h) {
    h.get('qo-result').value = 'accepted'; h.get('qo-inspection-type').value = 'visual';
    h.get('qo-inspection-result').value = 'visual inspection passed';
    const callback = h.get('pcpane-outcome').innerHTML.match(/onclick="(savePrintQualityOutcome\(\))"/)[1];
    return vm.runInContext(callback, h.context);
}

test('estimate completion for A cannot navigate away from edited B', async () => {
    const h = harness(), a = record('A'), b = record('B'); h.completionModules();
    await h.open(a); const job = await startEstimate(h);
    await h.open(b, 'files'); h.editName('B draft');
    h.get('estimate-status').textContent = 'B status';
    await h.answer(job, {status: 'done'});
    await settleCardRefresh(h, {...a, revision: 2});
    coherent(h.state(), b);
    assert.equal(h.get('pc-name').value, 'B draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.get('estimate-status').textContent, 'B status');
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
});

test('estimate completion preserves same-card unsaved inputs and selected tab', async () => {
    const h = harness(), a = record('A'); h.completionModules();
    await h.open(a); const job = await startEstimate(h);
    h.editName('A draft'); h.context.showPrintCardTab('files');
    await h.answer(job, {status: 'done'});
    await settleCardRefresh(h, {...a, revision: 2});
    assert.equal(h.get('pc-name').value, 'A draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.state().revision, 1);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
});

test('clean current-card estimate completion publishes coherently without resetting its tab', async () => {
    const h = harness(), a = record('A'), fresh = {...a, revision: 2}; h.completionModules();
    await h.open(a); const job = await startEstimate(h);
    h.context.showPrintCardTab('process');
    await h.answer(job, {status: 'done'}); await settleCardRefresh(h, fresh);
    coherent(h.state(), fresh);
    assert.equal(h.get('pcpane-process').style.display, 'block');
    assert.equal(h.state().dirty, false);
});

test('estimate POST accepted after navigating away retains its original view generation', async () => {
    const h = harness(), a = record('A'), b = record('B'); h.completionModules();
    await h.open(a);
    const accepted = h.context.estimateRecord('A', true);
    const post = h.take('/prints/A/estimate', 'POST');
    await h.open(b); await h.open(a, 'files'); h.editName('new A draft');
    h.get('estimate-status').textContent = 'new A status';
    await h.answer(post, {job_id: 'old-view-job'}); await accepted;
    await h.answer(h.take('/background-analysis/jobs/old-view-job'), {status: 'done'});
    await settleCardRefresh(h, {...a, revision: 2});
    assert.equal(h.get('pc-name').value, 'new A draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.get('estimate-status').textContent, 'new A status');
});

test('late failed estimate cannot overwrite the newer card status', async () => {
    const h = harness(), a = record('A'), b = record('B'); h.completionModules();
    await h.open(a); const job = await startEstimate(h);
    await h.open(b); h.get('estimate-status').textContent = 'B status';
    await h.answer(job, {status: 'failed', error: 'A failed'});
    assert.equal(h.get('estimate-status').textContent, 'B status'); coherent(h.state(), b);
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
});

test('quality POST for A cannot reload, mark saved, or overwrite edited B', async () => {
    const h = harness(), a = record('A'), b = record('B'); h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    const post = h.take('/prints/A/quality-outcomes', 'POST');
    await h.open(b, 'files'); h.editName('B draft');
    h.get('qo-save-status').textContent = 'B status';
    await h.answer(post, {outcome_id: 'quality-A'});
    // Settle baseline's wrong-context B reads, without hiding their effects.
    await settleCardRefresh(h, b); await saving;
    coherent(h.state(), b);
    assert.equal(h.get('pc-name').value, 'B draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.get('qo-save-status').textContent, 'B status');
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.requests.filter(request => request.url === '/prints/B').length, 1);
});

test('quality completion preserves same-card unsaved inputs while confirming accepted outcome', async () => {
    const h = harness(), a = record('A'); h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    const post = h.take('/prints/A/quality-outcomes', 'POST');
    h.editName('A draft'); h.context.showPrintCardTab('files');
    await h.answer(post, {outcome_id: 'quality-A'});
    await settleCardRefresh(h, {...a, revision: 2}); await saving;
    assert.equal(h.get('pc-name').value, 'A draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.state().revision, 1);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.match(h.get('qo-save-status').textContent, /Сохранено/);
});

test('quality completion preserves the tab selected during its related-data refresh', async () => {
    const h = harness(), a = record('A'), fresh = {...a, revision: 2}; h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    await h.answer(h.take('/prints/A/quality-outcomes', 'POST'), {outcome_id: 'quality-A'});
    await h.answer(h.take('/prints/A'), fresh);
    h.context.showPrintCardTab('files');
    await h.details(fresh); await saving;
    coherent(h.state(), fresh);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.get('qo-save-status').textContent, 'Сохранено');
});

test('quality related-data continuation cannot publish after navigation to B', async () => {
    const h = harness(), a = record('A'), b = record('B'), fresh = {...a, revision: 2}; h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    await h.answer(h.take('/prints/A/quality-outcomes', 'POST'), {outcome_id: 'quality-A'});
    await h.answer(h.take('/prints/A'), fresh);
    await h.open(b, 'files'); h.editName('B draft');
    const oldReport = h.requests.find(request => !request.claimed && request.url === '/prints/A/operator-report');
    if (oldReport) await h.answer(h.take(oldReport.url), {record_id: 'A', revision: 2});
    await h.details(fresh); await saving;
    coherent(h.state(), b);
    assert.equal(h.get('pc-name').value, 'B draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.get('pcpane-files').style.display, 'block');
});

test('late quality failure cannot write into the newer card status', async () => {
    const h = harness(), a = record('A'), b = record('B'); h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    const post = h.take('/prints/A/quality-outcomes', 'POST');
    await h.open(b); h.get('qo-save-status').textContent = 'B status';
    post.reject(new Error('A unavailable')); await saving;
    assert.equal(h.get('qo-save-status').textContent, 'B status'); coherent(h.state(), b);
});

test('recursive estimate polling keeps the originally accepted view generation', async () => {
    const h = harness(), a = record('A'); h.completionModules();
    await h.open(a); const first = await startEstimate(h);
    await h.open(record('B')); await h.open(a, 'files');
    h.get('estimate-status').textContent = 'new A status';
    await h.answer(first, {status: 'running'});
    for (const [id, callback] of [...h.timers]) {h.timers.delete(id); callback();}
    await tick();
    await h.answer(h.take('/background-analysis/jobs/estimate-A'), {status: 'done'});
    await settleCardRefresh(h, {...a, revision: 2});
    assert.equal(h.get('estimate-status').textContent, 'new A status');
    assert.equal(h.state().revision, 1);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
});

test('estimate completion from the catalog keeps its existing archive refresh', async () => {
    const h = harness(), refreshed = []; h.completionModules();
    h.context.loadArchive = skip => refreshed.push(skip);
    h.context._pendingEstimateRequests = new Set(['archive-card']);
    const polling = h.context._pollEstimate('archive-card', 'archive-job', false);
    await h.answer(h.take('/background-analysis/jobs/archive-job'), {status: 'done'}); await polling;
    assert.deepEqual(refreshed, [0]);
    assert.equal(h.context._pendingEstimateRequests.has('archive-card'), false);
    assert.match(h.get('estimate-status').textContent, /Расчёт готов/);
});

test('quality refresh cannot overwrite edits made during its linked-data requests', async () => {
    const h = harness(), a = record('A'), fresh = {...a, revision: 2}; h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    await h.answer(h.take('/prints/A/quality-outcomes', 'POST'), {outcome_id: 'quality-A'});
    await h.answer(h.take('/prints/A'), fresh);
    h.editName('new local draft'); h.context.showPrintCardTab('files');
    await h.details(fresh); await saving;
    assert.equal(h.get('pc-name').value, 'new local draft'); assert.equal(h.state().dirty, true);
    assert.equal(h.state().revision, 1);
    assert.equal(h.get('pcpane-files').style.display, 'block');
    assert.match(h.get('qo-save-status').textContent, /Сохранено/);
});

function staleEstimateJob(revision = 2) {
    return {status: 'failed', error: 'Changed inputs', job_type: 'print_estimate',
        entity_type: 'print_record', entity_id: 'A', owner_node_id: 'owner-A',
        payload: {record_id: 'A', owner_node_id: 'owner-A', record_revision: revision}};
}

test('accepted estimate follows the current owner-local successor without another POST', async () => {
    const h = harness(), a = {...record('A', 2), origin_compute_node_id: 'owner-A'};
    h.completionModules(); await h.open(a, 'plan');
    const original = await startEstimate(h);
    await h.answer(original, staleEstimateJob());
    await h.answer(h.take('/prints/A'), {...a, revision: 4,
        estimate_job: {job_id: 'fresh-estimate-A', status: 'pending'}});
    assert.equal(h.context._pendingEstimateRequests.has('A'), true);
    assert.doesNotMatch(h.get('estimate-status').textContent, /не выполнен/);
    for (const [id, callback] of [...h.timers]) {h.timers.delete(id); callback();}
    await tick();
    await h.answer(h.take('/background-analysis/jobs/fresh-estimate-A'), {status: 'done'});
    await settleCardRefresh(h, {...a, revision: 5});
    assert.equal(h.state().revision, 5);
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
    assert.equal(h.requests.filter(item => item.options.method === 'POST').length, 1);
    assert.equal(h.get('pcpane-plan').style.display, 'block');
});

test('a successor completed before the next poll is recognized from its current published quote', async () => {
    const h = harness(), a = {...record('A', 2), origin_compute_node_id: 'owner-A'};
    h.completionModules(); await h.open(a);
    const original = await startEstimate(h);
    await h.answer(original, staleEstimateJob());
    const fresh = {...a, revision: 5, estimate_job: null,
        prediction_is_current: true, prediction_configuration_is_current: true,
        metadata_json: {prediction: {input_revision: 4, computed_by: 'owner-A', print_hours: 10}}};
    await h.answer(h.take('/prints/A'), fresh);
    await settleCardRefresh(h, fresh);
    assert.equal(h.state().revision, 5);
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
    assert.match(h.get('estimate-status').textContent, /Расчёт готов/);
});

test('an older quote cannot hide the failed accepted estimate', async () => {
    const h = harness(), a = {...record('A', 2), origin_compute_node_id: 'owner-A'};
    h.completionModules(); await h.open(a);
    const original = await startEstimate(h);
    await h.answer(original, staleEstimateJob());
    await h.answer(h.take('/prints/A'), {...a, estimate_job: null,
        prediction_is_current: true, prediction_configuration_is_current: true,
        metadata_json: {prediction: {input_revision: 1, computed_by: 'owner-A', print_hours: 10}}});
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
    assert.match(h.get('estimate-status').textContent, /не выполнен/);
    assert.equal(h.state().revision, 2);
});

test('successor polling retains the accepted view generation after navigating away', async () => {
    const h = harness(), a = {...record('A', 2), origin_compute_node_id: 'owner-A'};
    h.completionModules(); await h.open(a);
    const original = await startEstimate(h);
    await h.answer(original, staleEstimateJob());
    await h.answer(h.take('/prints/A'), {...a, revision: 4,
        estimate_job: {job_id: 'fresh-estimate-A', status: 'pending'}});
    await h.open(record('B')); h.editName('B draft');
    h.get('estimate-status').textContent = 'B status';
    for (const [id, callback] of [...h.timers]) {h.timers.delete(id); callback();}
    await tick();
    await h.answer(h.take('/background-analysis/jobs/fresh-estimate-A'), {status: 'done'});
    assert.equal(h.state().id, 'B'); assert.equal(h.state().dirty, true);
    assert.equal(h.get('pc-name').value, 'B draft');
    assert.equal(h.get('estimate-status').textContent, 'B status');
    assert.equal(h.context._pendingEstimateRequests.has('A'), false);
});

test('actual QC input event protects its unsaved form from incoming sync', async () => {
    const h = harness(), a = record('A'); h.completionModules();
    await h.open(a, 'outcome');
    const field = h.get('qo-inspection-result');
    field.value = 'unfinished inspection'; field.listeners.input();
    const syncing = h.context._refreshOpenPrintCardFromSync();
    await h.answer(h.take('/prints/A'), {...a, revision: 2}); await syncing;
    assert.equal(h.get('qo-inspection-result').value, 'unfinished inspection');
    assert.equal(h.state().revision, 1);
    assert.equal(vm.runInContext('_pcQualityDirty', h.context), true);
});

test('QC edits made after its POST remain unsaved when the earlier result finishes', async () => {
    const h = harness(), a = record('A'); h.completionModules();
    await h.open(a, 'outcome'); const saving = startQualitySave(h);
    const field = h.get('qo-inspection-result');
    field.value = 'new inspection detail'; field.listeners.input();
    await h.answer(h.take('/prints/A/quality-outcomes', 'POST'), {outcome_id:'quality-A',is_final:true,result:'accepted'});
    await settleCardRefresh(h, {...a, revision:2}); await saving;
    assert.equal(h.get('qo-inspection-result').value, 'new inspection detail');
    assert.equal(vm.runInContext('_pcQualityDirty', h.context), true);
    assert.equal(h.state().revision, 1);
});
