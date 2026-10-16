const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const elements = new Map(), requests = [];
    for (const id of ['page-print-card', 'qo-save-status', 'qo-save-button', 'qo-result',
        'qo-inspection-type', 'qo-inspection-result', 'qo-notes', 'qo-layer-start', 'qo-layer-end']) {
        elements.set(id, {value: '', style: {}, disabled: false, textContent: ''});
    }
    elements.get('qo-result').value = 'accepted';
    elements.get('qo-inspection-type').value = 'visual';
    elements.get('qo-inspection-result').value = 'Поверхность проверена';
    const context = vm.createContext({document: {getElementById: id => elements.get(id)},
        _pcRecord: {record_id: 'A', quality_outcomes: []}, _pcLoading: false,
        _pcLoadGeneration: 1, _pcEditGeneration: 1, _pcQualityDirty: true,
        _jsonHeaders: () => ({}), _refreshOpenPrintCardFromSync: async () => {},
        fetch(url, options) {
            let resolve, reject;
            const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
            requests.push({url, options, resolve, reject});
            return promise;
        }});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/card-analysis.js'), 'utf8'), context);
    return {context, elements, requests};
}

test('repeated quality submission while saving publishes only one outcome', async () => {
    const h = harness();
    const pending = h.context.savePrintQualityOutcome();
    const repeated = h.context.savePrintQualityOutcome();
    assert.equal(h.requests.length, 1);
    assert.equal(h.elements.get('qo-save-button').disabled, true);
    h.requests[0].resolve({ok: true, json: async () => ({outcome_id: 'saved'})});
    await Promise.all([pending, repeated]);
    assert.equal(h.elements.get('qo-save-button').disabled, false);
    assert.equal(h.context._pcQualityDirty, false);
});

test('failed quality submission re-enables retry and preserves the draft', async () => {
    const h = harness();
    const pending = h.context.savePrintQualityOutcome();
    h.requests[0].reject(new Error('offline'));
    await pending;
    assert.equal(h.elements.get('qo-save-button').disabled, false);
    assert.equal(h.context._pcQualityDirty, true);
    assert.equal(h.elements.get('qo-inspection-result').value, 'Поверхность проверена');
    assert.match(h.elements.get('qo-save-status').textContent, /offline/);
    const retry = h.context.savePrintQualityOutcome();
    assert.equal(h.requests.length, 2);
    h.requests[1].resolve({ok: true, json: async () => ({outcome_id: 'saved'})});
    await retry;
});

test('completion of another card save cannot unlock the current card save', async () => {
    const h = harness();
    const first = h.context.savePrintQualityOutcome();
    h.context._pcRecord = {record_id: 'B', quality_outcomes: []};
    h.context._pcLoadGeneration++;
    const button = {disabled: false};
    h.elements.set('qo-save-button', button);
    const second = h.context.savePrintQualityOutcome();
    h.requests[0].resolve({ok: true, json: async () => ({outcome_id: 'A-saved'})});
    await first;
    assert.equal(button.disabled, true);
    h.requests[1].resolve({ok: true, json: async () => ({outcome_id: 'B-saved'})});
    await second;
    assert.equal(button.disabled, false);
});

function verdict(id, parent = null, result = 'accepted') {
    return {outcome_id: id, supersedes_outcome_id: parent, is_final: true,
        print_record_id: 'A', result, timestamp: id === 'old' ? '2026-10-14T10:00:00Z' : '2026-10-14T09:55:00Z'};
}

test('quality correction uses causal head despite reversed server clocks', async () => {
    const h = harness(), old = verdict('old'), corrected = verdict('corrected', 'old', 'rejected');
    h.context._pcRecord.quality_outcomes = [old, corrected];
    const pending = h.context.savePrintQualityOutcome();
    assert.equal(JSON.parse(h.requests[0].options.body).supersedes_outcome_id, 'corrected');
    h.requests[0].resolve({ok: true, json: async () => verdict('next', 'corrected')});
    await pending;
});

test('ambiguous lineage keeps history and draft but never posts a guessed correction', async () => {
    for (const history of [[verdict('a'), verdict('b')],
        [verdict('a'), verdict('b', 'a'), verdict('c', 'a')],
        [verdict('valid'), verdict('cycle-a', 'cycle-b'), verdict('cycle-b', 'cycle-a')]]) {
        const h = harness();
        h.context._pcRecord.quality_outcomes = history;
        assert.equal(h.context._finalQualityOutcome(history), null);
        await h.context.savePrintQualityOutcome();
        assert.equal(h.requests.length, 0);
        assert.equal(h.elements.get('qo-save-button').disabled, false);
        assert.equal(h.context._pcQualityDirty, true);
        assert.match(h.elements.get('qo-save-status').textContent, /неоднозначна/);
        assert.equal(h.context._pcRecord.quality_outcomes, history);
    }
});

test('quality overview reuses complete-chain selector and does not timestamp-sort head', async () => {
    const h = harness();
    for (const id of ['design-quality-progress', 'design-quality-counts', 'design-quality-rows',
        'design-model-status', 'design-model-progress', 'design-model-progress-label']) h.elements.set(id, {style: {}});
    h.context._esc = value => String(value);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/navigation.js'), 'utf8'), h.context);
    const history = [verdict('old'), verdict('corrected', 'old', 'rejected'),
        {...verdict('ambiguous-a'), print_record_id: 'B'}, {...verdict('ambiguous-b'), print_record_id: 'B'}];
    h.context._designFetch = async url => ({'/quality-outcomes': history,
        '/models/defect-risk': {policy: {min_labels: 20}}, '/analysis/defect-risk': {n_labeled: 1},
        '/prints?limit=100': {total: 2, items: [{record_id: 'A', name: 'Corrected'}, {record_id: 'B', name: 'Ambiguous'}]}}[url]);
    h.context._homeDate = () => '—';
    await h.context.loadDesignQuality();
    assert.equal(h.elements.get('design-quality-progress').textContent, 'размечено 1 из 2 печатей');
    assert.match(h.elements.get('design-quality-rows').innerHTML, /Corrected.*БРАК/s);
    assert.match(h.elements.get('design-quality-rows').innerHTML, /Ambiguous.*ОЦЕНИТЬ/s);
});

test('per-card QC rejects cross-card lineage refs but survives shared-session ambiguity', () => {
    const h = harness();
    const first = {...verdict('old'), print_record_id: 'A', session_id: 'S'};
    const cross = {...verdict('cross', 'old'), print_record_id: 'B', session_id: 'S'};
    assert.equal(h.context._finalQualityOutcomes([first, cross]).length, 0);
    const ambiguous = {...verdict('other-root'), print_record_id: 'A', session_id: 'S'};
    const other = {...verdict('valid-other'), print_record_id: 'B', session_id: 'S'};
    const unaffected = h.context._finalQualityOutcomes([first, ambiguous, other]);
    assert.equal(unaffected.length, 1);
    assert.equal(unaffected[0].outcome_id, 'valid-other');
    assert.equal(h.context._finalQualityOutcomes([first, other]).length, 2);
});
