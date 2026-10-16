'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function fixture() {
  const elements = new Map(); const calls = []; let render;
  const element = id => {
    if (!elements.has(id)) elements.set(id, { hidden: false, textContent: '', listeners: {},
      addEventListener(name, callback) { this.listeners[name] = callback; },
      getAttribute(name) { return this[name] || null; }, removeAttribute(name) { delete this[name]; } });
    return elements.get(id);
  };
  const initial = { phase: 'setup', configured: false, version: '1.7.0', url: 'http://127.0.0.1:8000' };
  const context = { document: { getElementById: element, querySelector: () => element('progress'), querySelectorAll: () => [element('setup-input'), element('setup-submit')] },
    window: { companion: { state: () => Promise.resolve(initial), onState: callback => { render = callback; },
      openUpdate: async () => { calls.push('open-update'); },
      installUpdate: async () => { calls.push('install-update'); },
      launch: async () => { calls.push('launch'); },
      checkUpdate: async () => { calls.push('check-update'); } } } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8'), context);
  return { element, render, initial, calls };
}
test('first-setup error leaves editable connection form and import action', () => {
  const { element, render, initial } = fixture();
  render({ ...initial, phase: 'error', message: 'Неверный адрес NAS' });
  assert.equal(element('onboarding').hidden, false);
  assert.equal(element('operation').hidden, true);
  assert.equal(element('setup-error').hidden, false);
  assert.equal(element('setup-error').textContent, 'Неверный адрес NAS');
});
test('UI-only test bundle does not collect NAS credentials for an unavailable installation', () => {
  const { element, render, initial } = fixture();
  render({ ...initial, deploymentAvailable: false });
  assert.equal(element('test-notice').hidden, false);
  assert.equal(element('setup-input').disabled, true);
  assert.equal(element('setup-submit').disabled, true);
  assert.equal(element('import').disabled, false);
});
test('saved-setup failure offers retry but never shows an unready dashboard', () => {
  const { element, render, initial } = fixture();
  render({ ...initial, configured: true, phase: 'error', canRollback: false, message: 'Docker не запущен' });
  assert.equal(element('onboarding').hidden, true);
  assert.equal(element('operation').hidden, false);
  assert.equal(element('dashboard').hidden, true);
  assert.equal(element('recover').hidden, true);
});
test('backend replacement reloads the iframe even at the same URL', () => {
  const { element, render, initial } = fixture();
  let reloads = 0; let src;
  Object.defineProperty(element('dashboard'), 'src', { get: () => src, set: value => { reloads++; src = value; } });
  render({ ...initial, configured: true, phase: 'ready', backendSHA: 'old' });
  render({ ...initial, configured: true, phase: 'ready', backendSHA: 'old', message: 'Нет обновлений' });
  assert.equal(reloads, 1);
  render({ ...initial, configured: true, phase: 'ready', backendSHA: 'new' });
  assert.equal(reloads, 2);
});
test('manual installer takes priority over backend retry and preserves the ready iframe draft', async () => {
  const { element, render, initial, calls } = fixture();
  await Promise.resolve(); // Let the genuine initial state callback settle first.
  const dashboard = element('dashboard');
  const draft = { recordId: 'selected-print', tab: 'control', note: 'unsaved inspection' };
  dashboard.contentWindow = { draft };
  let reloads = 0; let src;
  Object.defineProperty(dashboard, 'src', { get: () => src, set: value => { reloads++; src = value; } });
  const ready = { ...initial, configured: true, phase: 'ready', backendSHA: 'same-sha', backendUpdatePending: true };
  render(ready);
  assert.equal(element('update').textContent, 'Завершить обновление');
  render({ ...ready, busy: true, message: 'Проверяю GitHub…' });
  assert.equal(element('update').disabled, true);
  render({ ...ready, busy: false, manualUpdateAvailable: true });
  assert.equal(element('update').textContent, 'Скачать установщик');
  assert.equal(element('update').disabled, false);
  element('update').listeners.click();
  await Promise.resolve();
  assert.deepEqual(calls, ['open-update']);
  render({ ...ready, manualUpdateAvailable: true, message: 'Скачайте установщик перед установкой.' });
  assert.equal(reloads, 1);
  assert.equal(dashboard.hidden, false);
  assert.equal(dashboard.contentWindow.draft, draft);
  assert.equal(draft.note, 'unsaved inspection');
});
test('packaged preload exposes manual installer IPC only to the main frame and sends no renderer URL', async () => {
  const calls = [];
  for (const isMainFrame of [false, true]) {
    let bridge;
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../preload.cjs'), 'utf8'), {
      process: { isMainFrame },
      require: name => {
        assert.equal(name, 'electron');
        return { contextBridge: { exposeInMainWorld: (key, api) => { assert.equal(key, 'companion'); bridge = api; } },
          ipcRenderer: { invoke: async (...args) => { calls.push(args); }, on() {}, removeListener() {} } };
      },
    });
    if (!isMainFrame) assert.equal(bridge, undefined);
    else {
      assert.equal(Object.isFrozen(bridge), true);
      await bridge.openUpdate('https://untrusted.invalid/renderer-supplied.exe');
    }
  }
  assert.deepEqual(calls, [['companion:open-update']]);
});
