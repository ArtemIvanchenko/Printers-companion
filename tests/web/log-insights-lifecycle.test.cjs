const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
    const requests = [], panes = new Map();
    function element(tag = 'div') {
        return {tagName: tag, textContent: '', style: {}, dataset: {}, children: [], isConnected: true,
            listeners: {}, classList: {toggle() {}}, setAttribute() {},
            append(...children) {this.children.push(...children);},
            replaceChildren(...children) {this.children = children;},
            addEventListener(kind, callback) {this.listeners[kind] = callback;},
            click() {this.listeners.click?.();}, querySelectorAll() {return [];}, querySelector() {return null;}};
    }
    const host = element('section');
    for (const tab of ['what', 'outcome']) panes.set('pcpane-' + tab, element());
    panes.get('pcpane-outcome').querySelector = selector => selector === '#pc-log-insights' ? host : null;
    const context = vm.createContext({window: {}, document: {
        createElement: element, getElementById: id => panes.get(id),
        querySelectorAll: selector => selector === '.pc-pane' ? [...panes.values()] : [],
    }, fetch(url) {
        let resolve, reject;
        const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
        requests.push({url, resolve, reject});
        return promise;
    }});
    for (const file of ['dashboard/card-editor.js', 'log-insights.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets', file), 'utf8'), context, {filename: file});
    }
    vm.runInContext("_pcRecord = {record_id: 'pr-current', geometry_analysis: {}}", context);
    const open = () => context.showPrintCardTab('outcome');
    const reopen = () => {context.showPrintCardTab('what'); open();};
    async function answer(index, data = {}, status = 200) {
        requests[index].resolve({ok: status >= 200 && status < 300, status, json: async () => data});
        await tick();
    }
    return {context, host, requests, open, reopen, answer};
}

test('failed log-insights request can retry by reopening the actual card tab', async () => {
    const h = harness();
    h.open();
    await h.answer(0, {}, 503);
    assert.match(h.host.children[0].textContent, /503/);
    h.reopen();
    assert.equal(h.requests.length, 2);
    assert.equal(h.requests[1].url, '/analysis/prints/pr-current/log-insights');
    await h.answer(1);
    assert.equal(h.host.children[0].children[0].textContent, 'Где проверить деталь');
});

test('generated retry button retries after a network failure without parallel requests', async () => {
    const h = harness();
    h.open();
    h.requests[0].reject(new Error('connection lost'));
    await tick();
    const retry = h.host.children.find(child => child.tagName === 'button');
    assert(retry, 'the error panel must expose a working retry action');
    assert.equal(retry.textContent, 'Повторить');
    retry.click();
    retry.click();
    h.reopen();
    assert.equal(h.requests.length, 2);
    await h.answer(1);
    assert.equal(h.host.children[0].children[0].textContent, 'Где проверить деталь');
});

test('pending and successful log-insights loads are reused on tab reopening', async () => {
    const h = harness();
    h.open();
    h.reopen();
    assert.equal(h.requests.length, 1);
    await h.answer(0);
    const content = h.host.children[0];
    h.reopen();
    assert.equal(h.requests.length, 1);
    assert.equal(h.host.children[0], content);
});

test('a successful response does not render into its detached previous card host', async () => {
    const h = harness();
    h.open();
    const content = h.host.children[0];
    h.host.isConnected = false;
    await h.answer(0);
    assert.equal(h.host.children[0], content);
});

test('a failed response does not render a retry action into its detached previous card host', async () => {
    const h = harness();
    h.open();
    const content = h.host.children[0];
    h.host.isConnected = false;
    await h.answer(0, {}, 503);
    assert.equal(h.host.children[0], content);
});

test('a retry callback from a detached previous card does not request its data again', async () => {
    const h = harness();
    h.open();
    await h.answer(0, {}, 503);
    const retry = h.host.children.find(child => child.tagName === 'button');
    h.host.isConnected = false;
    retry.click();
    assert.equal(h.requests.length, 1);
});
