const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('maintenance HTTP failure does not reject catalog attention or hide known unlinked prints', async () => {
    const visible = new Set(), rows = {innerHTML: ''};
    const box = {classList: {add: name => visible.add(name), remove: name => visible.delete(name)}};
    const context = vm.createContext({document: {
        getElementById: id => id === 'home-attention' ? box : rows, querySelectorAll: () => [],
    }, _esc: String, _plural: () => 'печати', fetch: async url => ({
        ok: url !== '/maintenance/status', status: url === '/maintenance/status' ? 503 : 200,
        json: async () => url === '/maintenance/status' ? {detail: 'Storage unavailable'}
            : url === '/powder/status' ? {has_batch: true} : {n_prints: 2},
    })});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/catalog.js'), 'utf8'), context);
    await context.loadHomeAttention();
    assert(visible.has('is-visible'));
    assert.match(rows.innerHTML, /2 печати с логами без карточки/);
});
