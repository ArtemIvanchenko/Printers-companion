const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const uploads = [], opened = [], reads = [], renders = [], tabs = [];
    const page = {style: {display: 'block'}}, status = {innerHTML: ''};
    const context = vm.createContext({window: {}, document: {
        getElementById: id => id === 'page-print-card' ? page : status,
        querySelector: () => ({id: 'pctab-files'}), querySelectorAll: () => [],
    }, fetch: async url => {
        reads.push(url);
        return {ok: true, json: async () => url === '/prints/A'
            ? {record_id: 'A', revision: 2, session_id: null} : {}};
    }});
    for (const file of ['card-editor.js', 'card-files.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard', file), 'utf8'), context);
    }
    vm.runInContext("_pcRecord = {record_id: 'A', revision: 1}; _pcLoading = false; _pcLoadGeneration = 1", context);
    context.openPrintCard = id => opened.push(id);
    context._renderPrintCard = () => renders.push(vm.runInContext('_pcRecord.record_id', context));
    context.showPrintCardTab = tab => tabs.push(tab);
    context.uploadArchiveFile = (recordId, file) => new Promise((resolve, reject) => uploads.push({recordId, file, resolve, reject}));
    return {context, uploads, opened, reads, renders, tabs, status};
}

test('attachment completion after switching cards does not navigate back', async () => {
    const h = harness();
    const pending = h.context.uploadFilesToCard('A', [{name: 'photo.jpg'}]);
    vm.runInContext("_pcRecord = {record_id: 'B', revision: 1}; _pcLoadGeneration++", h.context);
    h.uploads[0].resolve({}); await pending;
    assert.deepEqual(h.opened, []);
    assert.deepEqual(h.reads, []);
});

for (const dirty of ['_pcDirty', '_pcQualityDirty']) {
    test(`attachment completion preserves the actual ${dirty} draft guard`, async () => {
        const h = harness();
        const pending = h.context.uploadFilesToCard('A', [{name: 'photo.jpg'}]);
        vm.runInContext(`${dirty} = true`, h.context);
        h.uploads[0].resolve({}); await pending;
        assert.deepEqual(h.opened, []);
        assert.deepEqual(h.renders, []);
        assert.equal(vm.runInContext(dirty, h.context), true);
        assert.match(h.status.innerHTML, /поля не перезаписаны/);
    });
}

test('clean attachment completion refreshes once while retaining the active files tab', async () => {
    const h = harness();
    const pending = h.context.uploadFilesToCard('A', [{name: 'photo.jpg'}]);
    h.uploads[0].resolve({}); await pending;
    assert.deepEqual(h.opened, []);
    assert.deepEqual(h.renders, ['A']);
    assert.deepEqual(h.tabs, ['files']);
});

test('partial upload failure is handled and refreshes files already accepted', async () => {
    const h = harness();
    const pending = h.context.uploadFilesToCard('A', [{name: 'first.jpg'}, {name: 'second.jpg'}, {name: 'third.jpg'}]);
    h.uploads[0].resolve({});
    await new Promise(resolve => setImmediate(resolve));
    h.uploads[1].reject(new Error('already reported by uploadArchiveFile'));
    await pending;
    assert.equal(h.uploads.length, 2);
    assert.deepEqual(h.renders, ['A']);
    assert.deepEqual(h.opened, []);
});
