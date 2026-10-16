'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function deferred() {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function harness() {
    const nodes = new Map(), requests = [];
    const classes = () => {
        const values = new Set();
        return { add: v => values.add(v), remove: v => values.delete(v), contains: v => values.has(v),
            toggle(v, on) { if (on) values.add(v); else values.delete(v); } };
    };
    const makeNode = () => ({ textContent: '', innerHTML: '', children: [], style: {},
        classList: classes(), parentElement: { classList: classes() },
        scrollIntoView() {}, querySelectorAll() { return []; },
        append(...items) { this.children.push(...items); },
        appendChild(item) { this.children.push(item); }, after() {},
        replaceChildren() { this.innerHTML = ''; this.children = []; } });
    const document = { createElement: makeNode, querySelectorAll: () => [],
        getElementById(id) { if (!nodes.has(id)) nodes.set(id, makeNode()); return nodes.get(id); } };
    const charts = { currentSessionId: null };
    for (const key of ['o2', 'temp', 'hum', 'press', 'burn']) {
        charts[key] = { data: { labels: [], datasets: [{ data: [] }] }, update() {} };
    }
    const context = vm.createContext({ document, window: {}, console, _telCharts: charts,
        ensureTelemetryCharts() {}, sigLabel: value => value,
        dashboardBootstrap: { thresholds: { o2: 1 } },
        _designFetch(url) { const request = { url, ...deferred() }; requests.push(request); return request.promise; } });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/telemetry.js'), 'utf8'), context);
    return { context, nodes, charts, requests, request: index => requests[index],
        notice: () => nodes.get('telemetry-notice').textContent,
        subtitle: () => nodes.get('tel-subtitle').textContent,
        health: () => nodes.get('telemetry-health').innerHTML };
}

function session(id, value, day = 2) {
    return { session_id: id, start_ts: `2026-01-0${day}T00:00:00`,
        telemetry: { time: [`${id}-time`], oxygen: { SO1: [value] },
            layer_burn_times: [{ layer: 1, duration_sec: value }] },
        health: { readiness: { score: 55 }, anomalies: [] } };
}

function summary(id, value) {
    return { total: 1, has_more: false, items: [{ session_id: id, start_ts: '2026-01-01T00:00:00',
        signal_stats: { SO1: { group: 'oxygen', mean: value } }, mean_burn_seconds: value }] };
}

function assertSession(h, id, value) {
    assert.equal(h.charts.currentSessionId, id);
    assert.deepEqual(h.charts.o2.data.labels, [`${id}-time`]);
    assert.deepEqual(h.charts.o2.data.datasets[0].data, [value]);
    assert.deepEqual(h.charts.burn.data.datasets[0].data, [value]);
    assert.equal(h.notice(), '');
    assert.match(h.health(), /55/);
}

test('late all-time response cannot replace the selected session or mix its health', async () => {
    const h = harness();
    const all = h.context.loadAllTimeTelemetry();
    const latest = h.context.loadTelemetryForSession('B');
    h.request(1).resolve(session('B', 2)); await latest;
    const subtitle = h.subtitle();
    h.request(0).resolve(summary('A', 99)); await all;
    assertSession(h, 'B', 2);
    assert.equal(h.subtitle(), subtitle);
});

test('late session and all-time errors do not overwrite a newer successful session', async () => {
    for (const mode of ['session', 'all-time']) {
        const h = harness();
        const stale = mode === 'session' ? h.context.loadTelemetryForSession('A') : h.context.loadAllTimeTelemetry();
        const latest = h.context.loadTelemetryForSession('B');
        h.request(1).resolve(session('B', 2)); await latest;
        const subtitle = h.subtitle();
        h.request(0).reject(new Error('stale failure')); await stale;
        assertSession(h, 'B', 2);
        assert.equal(h.subtitle(), subtitle);
    }
});

test('newest request owns the same session even if an earlier response arrives last', async () => {
    const h = harness();
    const old = h.context.loadTelemetryForSession('A');
    const latest = h.context.loadTelemetryForSession('A');
    h.request(1).resolve(session('A', 2, 3)); await latest;
    const subtitle = h.subtitle();
    h.request(0).resolve(session('A', 99, 1)); await old;
    assertSession(h, 'A', 2);
    assert.equal(h.subtitle(), subtitle);
});

test('newest all-time request owns its summary despite both having null session id', async () => {
    const h = harness();
    const old = h.context.loadAllTimeTelemetry();
    const latest = h.context.loadAllTimeTelemetry();
    h.request(1).resolve(summary('B', 2)); await latest;
    h.request(0).resolve(summary('A', 99)); await old;
    assert.equal(h.charts.currentSessionId, null);
    assert.deepEqual(h.charts.o2.data.datasets[0].data, [2]);
    assert.deepEqual(h.charts.burn.data.datasets[0].data, [2]);
});

test('accepted failed session selection does not retain the previous chart, alarm, or health', async () => {
    const h = harness();
    const a = h.context.loadTelemetryForSession('A');
    h.request(0).resolve(session('A', 99)); await a;
    assert.match(h.health(), /55/);
    assert.equal(h.nodes.get('o2Chart').parentElement.classList.contains('alarm-active'), true);
    const b = h.context.loadTelemetryForSession('B');
    assert.equal(h.charts.currentSessionId, 'B');
    assert.equal(h.charts.o2.data.labels.length, 0);
    assert.equal(h.charts.o2.data.datasets.length, 0);
    assert.equal(h.charts.burn.data.datasets[0].data.length, 0);
    assert.equal(h.health(), '');
    assert.equal(h.nodes.get('o2Chart').parentElement.classList.contains('alarm-active'), false);
    h.request(1).reject(new Error('B unavailable')); await b;
    assert.equal(h.charts.o2.data.datasets.length, 0);
    assert.equal(h.health(), '');
    assert.match(h.notice(), /B unavailable/);
    assert.doesNotMatch(h.subtitle(), /Загрузка|1 янв/);
});

test('all-time selection clears prior process context before loading and keeps it cleared on failure', async () => {
    const h = harness();
    const a = h.context.loadTelemetryForSession('A');
    h.request(0).resolve(session('A', 99)); await a;
    const all = h.context.loadAllTimeTelemetry();
    assert.equal(h.charts.o2.data.labels.length, 0);
    assert.equal(h.charts.burn.data.datasets[0].data.length, 0);
    assert.equal(h.health(), '');
    h.request(1).reject(new Error('summary unavailable')); await all;
    assert.equal(h.charts.o2.data.datasets.length, 0);
    assert.equal(h.health(), '');
    assert.match(h.subtitle(), /summary unavailable/);
    assert.doesNotMatch(h.notice(), /Загрузка/);
});

test('late session-list auto-select cannot replace an explicit all-time selection', async () => {
    const h = harness();
    const listing = h.context.loadTelemetrySessions();
    const all = h.context.loadAllTimeTelemetry();
    assert.equal(vm.runInContext('_telemetryListLoading', h.context), true);
    h.request(0).resolve(summary('A', 99));
    await new Promise(resolve => setImmediate(resolve));
    // Settle an incorrect legacy auto-select too, so failing baseline has no
    // unfinished request and clearly exposes the wrong selection.
    if (h.requests.length > 2) h.request(2).resolve(session('A', 99));
    await listing;
    h.request(1).resolve(summary('B', 2)); await all;
    assert.equal(h.charts.currentSessionId, null);
    assert.equal(h.requests.length, 2);
    assert.equal(vm.runInContext('_telemetryListLoading', h.context), false);
    assert.deepEqual(h.charts.o2.data.datasets[0].data, [2]);
});

test('late list failure cannot overwrite the selected session status', async () => {
    const h = harness();
    const listing = h.context.loadTelemetrySessions();
    const b = h.context.loadTelemetryForSession('B');
    h.request(1).resolve(session('B', 2)); await b;
    h.request(0).reject(new Error('old list failure')); await listing;
    assertSession(h, 'B', 2);
    assert.equal(vm.runInContext('_telemetryListLoading', h.context), false);
});

test('null gaps and real zero values keep their matching labels without coercion', async () => {
    const h = harness();
    const loading = h.context.loadTelemetryForSession('A');
    const payload = session('A', 0);
    payload.telemetry.time = ['a', 'b', 'c'];
    payload.telemetry.oxygen.SO1 = [0, null, 2];
    h.request(0).resolve(payload); await loading;
    assert.deepEqual(h.charts.o2.data.labels, ['a', 'b', 'c']);
    assert.deepEqual(h.charts.o2.data.datasets[0].data, [0, null, 2]);
    const all = h.context.loadAllTimeTelemetry();
    const page = summary('A', 0);
    page.items.push({ session_id: 'B', signal_stats: {}, mean_burn_seconds: null });
    h.request(1).resolve(page); await all;
    assert.deepEqual(h.charts.o2.data.datasets[0].data, [null, 0]);
    assert.deepEqual(h.charts.burn.data.datasets[0].data, [null, 0]);
});
