'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { Updates } = require('../lib/updates.cjs');
function fixture(enabled) {
  const updater = new EventEmitter();
  const events = []; const calls = [];
  updater.checkForUpdates = async () => { calls.push('check'); return { isUpdateAvailable: true }; };
  updater.downloadUpdate = async () => { calls.push('download'); updater.emit('update-downloaded'); };
  updater.quitAndInstall = (...args) => calls.push(args);
  const manager = new Updates({ updater, enabled, notify: value => events.push(value), closeForInstall: () => calls.push('close') });
  return { updater, events, calls, manager };
}
test('unsigned test packages cannot silently install updates', async () => {
  const f = fixture(false); await f.manager.check();
  assert.deepEqual(f.calls, []); assert.throws(() => f.manager.install());
  assert.match(f.events[0].message, /Тестовая сборка/);
});
test('one explicit download then explicit restart, no quit-time implicit install', async () => {
  const f = fixture(true); await f.manager.check(); f.manager.install();
  assert.deepEqual(f.calls, ['check', 'download', 'close', [false, true]]);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  assert.equal(f.updater.allowDowngrade, false); assert.equal(f.updater.allowPrerelease, false);
});
test('network or signature error keeps current package', async () => {
  const f = fixture(true);
  f.updater.downloadUpdate = async () => { throw new Error('SECRET URI'); };
  await f.manager.check(); assert.throws(() => f.manager.install());
  assert.ok(f.events.every(item => !JSON.stringify(item).includes('SECRET')));
});
test('lifecycle operation excludes downloading and package installation', async () => {
  const f = fixture(true);
  f.manager.runtimeBusy = () => true;
  await f.manager.check(); assert.deepEqual(f.calls, []);
  f.manager.ready = true;
  assert.throws(() => f.manager.install());
});
test('a later signature error invalidates an earlier ready update', async () => {
  const f = fixture(true);
  await f.manager.check(); assert.equal(f.manager.ready, true);
  f.updater.emit('error', new Error('signature failed'));
  assert.equal(f.manager.ready, false);
  assert.throws(() => f.manager.install());
});
