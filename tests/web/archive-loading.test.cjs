const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const elements = new Map(), requests = [];
    for (const id of ['archive-table-body', 'archive-empty', 'archive-list-wrap', 'archive-pagination']) {
        elements.set(id, {style: {}, innerHTML: '', querySelectorAll: () => []});
    }
    const context = vm.createContext({document: {getElementById: id => elements.get(id)},
        _archiveSkip: 0, _ARCHIVE_PAGE_SIZE: 10, _archiveFilters: () => '&material=titanium',
        _esc: String, fetch(url) {
            let resolve, reject;
            const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
            requests.push({url, resolve, reject});
            return promise;
        }});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/card-files.js'), 'utf8'), context);
    context.loadUnlinkedSessions = () => {};
    const answer = (index, status = 200) => requests[index].resolve({ok: status === 200,
        status, json: async () => ({total: 0, items: []})});
    return {context, elements, requests, answer};
}

test('late archive success cannot replace the latest request error', async () => {
    const h = harness();
    const old = h.context.loadArchive(0), latest = h.context.loadArchive(10);
    h.requests[1].reject(new Error('NAS offline'));
    await latest;
    h.answer(0);
    await old;
    assert.match(h.elements.get('archive-table-body').innerHTML, /NAS offline/);
});

test('late archive failure cannot replace the latest filtered empty result', async () => {
    const h = harness();
    const old = h.context.loadArchive(0), latest = h.context.loadArchive(10);
    h.answer(1);
    await latest;
    h.requests[0].reject(new Error('old error'));
    await old;
    assert.match(h.elements.get('archive-table-body').innerHTML, /Ничего не найдено/);
    assert.doesNotMatch(h.elements.get('archive-table-body').innerHTML, /old error/);
});

test('HTTP failure is shown as an error even with an empty-looking JSON body', async () => {
    const h = harness();
    const pending = h.context.loadArchive();
    h.answer(0, 503);
    await pending;
    assert.match(h.elements.get('archive-table-body').innerHTML, /503/);
    assert.doesNotMatch(h.elements.get('archive-table-body').innerHTML, /Ничего не найдено/);
});
