const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const nodes = new Map(), requests = [], selected = [];
    function element(tagName) {
        return {tagName, children: [], textContent: '',
            append(...children) {this.children.push(...children);}, appendChild(child) {this.children.push(child);},
            after(child) {nodes.set(child.id, child);}, querySelectorAll() {return [];}};
    }
    const context = vm.createContext({document: {createElement: element,
        getElementById(id) {if (!nodes.has(id) && id !== 'telemetry-load-more') nodes.set(id, element('div')); return nodes.get(id);}},
        _telCharts: {currentSessionId: null}, ensureTelemetryCharts() {},
        _designFetch(url) {return new Promise((resolve, reject) => requests.push({url, resolve, reject}));}});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/telemetry.js'), 'utf8'), context);
    context.loadTelemetryForSession = async id => selected.push(id);
    return {context, nodes, requests, selected};
}

test('retry of failed initial telemetry list still selects its first session', async () => {
    const h = harness();
    const first = h.context.loadTelemetrySessions();
    h.requests[0].reject(new Error('offline')); await first;
    const button = h.nodes.get('telemetry-load-more');
    const retry = button.onclick();
    h.requests[1].resolve({total: 2, has_more: true, items: [{session_id: 'A'}]}); await retry;
    assert.deepEqual(h.selected, ['A']);
    assert.equal(button.textContent, 'Ещё сессии');
    assert.equal(button.disabled, false);
});

test('retry of failed list refresh repeats the same page rather than appending another page', async () => {
    const h = harness();
    vm.runInContext("_telSessionList = [{session_id: 'Old'}]", h.context);
    const refresh = h.context.loadTelemetrySessions();
    h.requests[0].reject(new Error('offline')); await refresh;
    const retry = h.nodes.get('telemetry-load-more').onclick();
    assert.match(h.requests[1].url, /skip=0$/);
    h.requests[1].resolve({total: 1, has_more: false, items: [{session_id: 'New'}]}); await retry;
    assert.equal(vm.runInContext('_telSessionList.length', h.context), 1);
});

test('generated telemetry choice uses a native button and retains its session action', async () => {
    const h = harness();
    const pending = h.context.loadTelemetrySessions();
    h.requests[0].resolve({total: 1, has_more: false, items: [{session_id: 'A'}]}); await pending;
    const card = h.nodes.get('tel-session-cards').children[0];
    assert.equal(card.tagName, 'button');
    assert.equal(card.type, 'button');
    await card.onclick();
    assert.deepEqual(h.selected, ['A', 'A']);
});
