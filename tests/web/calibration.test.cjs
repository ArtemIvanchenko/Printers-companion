const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../../web_templates/dashboard.html'), 'utf8');
const source = html.slice(html.indexOf('let _calibrationRequestActive = false;'), html.indexOf('// HOME STATS'));

function harness(fetch) {
    const messages = [];
    const refreshes = [];
    const context = vm.createContext({
        fetch, encodeURIComponent,
        setTimeout: callback => callback(),
        showToast: (message, type) => messages.push({message, type}),
        loadCalibration: async () => refreshes.push('calibration'),
        loadMachineParams: async () => refreshes.push('params'),
        loadMachineAccuracy: async () => refreshes.push('accuracy'),
    });
    vm.runInContext(source, context);
    return {context, messages, refreshes};
}
const response = (data, ok = true) => ({ok, json: async () => data});

test('accepted job does not claim completion before worker finishes', async () => {
    const urls = [];
    const h = harness(async url => {
        urls.push(url);
        if (url.endsWith('/recalibrate')) return response({contract_version: 2, job_id: 'task/one'});
        assert(url.endsWith('task%2Fone'));
        if (urls.length === 2) return response({status: 'running'});
        return response({status: 'done', result: {status: 'published'}});
    });
    await h.context.recalibrateNow();
    assert.equal(urls.length, 3);
    assert(h.messages[0].message.includes('в очереди'));
    assert(h.messages.at(-1).message.includes('завершена'));
    assert.equal(h.refreshes.length, 3);
});

test('failed job is reported without a false success', async () => {
    const h = harness(async url => response(url.endsWith('/recalibrate')
        ? {contract_version: 2, job_id: 'one'} : {status: 'failed', error: 'Данные изменились'}));
    await h.context.recalibrateNow();
    assert.equal(h.messages.at(-1).type, 'error');
    assert.equal(h.refreshes.length, 0);
});

test('network rejection unlocks button for another attempt', async () => {
    let calls = 0;
    const h = harness(async () => {calls++; throw new Error('offline');});
    await h.context.recalibrateNow();
    await h.context.recalibrateNow();
    assert.equal(calls, 2);
    assert.equal(h.messages.filter(message => message.type === 'error').length, 2);
});

test('bounded wait leaves slow durable job running', async () => {
    const h = harness(async url => response(url.endsWith('/recalibrate')
        ? {contract_version: 2, job_id: 'one'} : {status: 'pending'}));
    await h.context.recalibrateNow();
    assert(h.messages.at(-1).message.includes('продолжается в фоне'));
    assert.equal(h.refreshes.length, 0);
});

test('old synchronous API remains compatible', async () => {
    const h = harness(async () => response({applied: {steel: 1.1}}));
    await h.context.recalibrateNow();
    assert.equal(h.refreshes.length, 3);
});

test('failed unlock does not enqueue calibration', async () => {
    const urls = [];
    const h = harness(async url => {urls.push(url); return response({}, false);});
    await h.context.setCorrectionLock(false);
    assert.deepEqual(urls, ['/settings/machine']);
    assert.equal(h.messages.at(-1).type, 'error');
});
