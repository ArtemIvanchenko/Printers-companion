const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness(fetch) {
    const nodes = {question: {value: '<img src=x onerror=alert(1)>'},
        'chat-messages': {innerHTML: '', scrollTop: 0, scrollHeight: 100}};
    const context = vm.createContext({document: {getElementById: name => nodes[name]}, fetch});
    const root = path.join(__dirname, '../../web_assets/dashboard');
    const runtime = fs.readFileSync(path.join(root, 'runtime.js'), 'utf8');
    vm.runInContext(runtime.slice(runtime.indexOf('function _esc('), runtime.indexOf('// Non-blocking')), context);
    const operations = fs.readFileSync(path.join(root, 'operations.js'), 'utf8');
    vm.runInContext(operations.slice(operations.indexOf('async function ask()'), operations.indexOf('// Import status bar')), context);
    return {nodes, context};
}

test('chat renders operator input and model answers as text, not executable markup', async () => {
    const h = harness(async () => ({ok: true, json: async () => ({answer: '<svg onload=alert(2)>'})}));
    await h.context.ask();
    const html = h.nodes['chat-messages'].innerHTML;
    assert(!html.includes('<img'));
    assert(!html.includes('<svg'));
    assert(html.includes('&lt;img'));
    assert(html.includes('&lt;svg'));
    assert.equal(h.nodes.question.value, '');
});

test('chat does not disguise an HTTP failure as an empty successful answer', async () => {
    const h = harness(async () => ({ok: false, status: 503,
        json: async () => ({detail: '<img src=x onerror=alert(3)>'})}));
    await h.context.ask();
    const html = h.nodes['chat-messages'].innerHTML;
    assert(html.includes('❌'));
    assert(!html.includes('🤖'));
    assert(!html.includes('<img'));
});

test('chat escapes network error messages too', async () => {
    const h = harness(async () => {throw new Error('<script>alert(4)</script>');});
    await h.context.ask();
    assert(!h.nodes['chat-messages'].innerHTML.includes('<script>'));
    assert(h.nodes['chat-messages'].innerHTML.includes('&lt;script&gt;'));
});
