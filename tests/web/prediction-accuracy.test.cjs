const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

// Execute the real template functions, without booting unrelated dashboard code.
const html = require('./dashboard-source.cjs').dashboardSource();
const source = html.slice(html.indexOf('function _hasMeasuredAccuracyPair('), html.indexOf('async function loadDesignEstimate('));
const host = {innerHTML: ''};
const context = vm.createContext({document: {getElementById: () => host}, _esc: value => String(value)});
vm.runInContext(source, context);

test('missing readings cannot become zero-time points', () => {
    for (const actual of [null, undefined, '', true, NaN, Infinity, -1, 0]) {
        assert.equal(context._hasMeasuredAccuracyPair({predicted_hours: 10, actual_hours: actual}), false);
    }
    context._designAccuracyChart([{predicted_hours: 10, actual_hours: null}]);
    assert(!host.innerHTML.includes('<circle'));
    assert(host.innerHTML.includes('Нет проверенных пар'));
});

test('a valid observation remains visible beside missing data', () => {
    context._designAccuracyChart([
        {name: 'missing', predicted_hours: 10, actual_hours: null},
        {name: 'measured', predicted_hours: 10, actual_hours: 12, error_pct: -16.7},
    ]);
    assert.equal((host.innerHTML.match(/<circle /g) || []).length, 1);
    assert(host.innerHTML.includes('measured'));
    assert(!host.innerHTML.includes('missing'));
});

test('even-sized error history uses the middle average and reports missing facts', async () => {
    const nodes = new Map();
    const rows = [
        {name: 'first', predicted_hours: 9, actual_hours: 10, error_pct: -10},
        {name: 'second', predicted_hours: 7, actual_hours: 10, error_pct: -30},
        {name: 'missing', predicted_hours: 5, actual_hours: null, error_pct: null,
         excluded_reason_ru: 'Нет полного набора измерений'},
    ];
    const ctx = vm.createContext({
        document: {getElementById(id) {if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id);}},
        _esc: value => String(value), _formatHours: value => `${value} ч`,
        _designFetch: async url => url.includes('accuracy') ? {pairs: rows} : {items: [], params: {}},
    });
    const estimate = html.slice(html.indexOf('async function loadDesignEstimate('), html.indexOf('async function loadDesignAnomalies('));
    vm.runInContext(source + '\n' + estimate, ctx);
    await ctx.loadDesignEstimate();
    assert.equal(nodes.get('design-median-error').textContent, '20.0%');
    const table = nodes.get('design-accuracy-rows').innerHTML;
    assert(table.includes('Нет данных'));
    assert(table.includes('Нет полного набора измерений'));
    assert(!table.includes('null'));
});
