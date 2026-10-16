'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { Updates } = require('../lib/updates.cjs');
function fixture(enabled, release = {}) {
  const updater = new EventEmitter();
  const installEvents = new EventEmitter();
  const events = []; const calls = [];
  updater.checkForUpdates = async () => { calls.push('check'); return { isUpdateAvailable: true }; };
  updater.downloadUpdate = async () => { calls.push('download'); updater.emit('update-downloaded'); };
  updater.quitAndInstall = (...args) => { calls.push(args); installEvents.emit('before-quit-for-update'); };
  const manager = new Updates({ updater, installEvents, enabled, notify: value => events.push(value), closeForInstall: () => calls.push('close'),
    checkPublishedRelease: async () => {
      calls.push('published-check');
      return { phase: 'release_checked', latest_version: '1.8.0', source_state: 'clean',
        update_available: false, version_unknown: false, identity_conflict: false, ahead_of_release: false, installer: null, ...release };
    } });
  return { updater, installEvents, events, calls, manager };
}
test('unsigned test packages cannot silently install updates', async () => {
  const f = fixture(false); await f.manager.check();
  assert.deepEqual(f.calls, ['published-check']); assert.throws(() => f.manager.install());
  assert.match(f.events.at(-2).message, /Установлена публичная версия/);
  assert.equal(f.manager.ready, false); assert.equal(f.manager.busy, false);
});
test('one explicit download then explicit restart, no quit-time implicit install', async () => {
  const f = fixture(true); await f.manager.check(); await f.manager.install();
  assert.deepEqual(f.calls, ['check', 'download', [false, true], 'close']);
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
test('updater events cannot release another runtime operation', async () => {
  const f = fixture(true);
  await f.manager.check();
  f.manager.runtimeBusy = () => true;
  f.events.length = 0;
  f.updater.emit('download-progress', { percent: 90 });
  f.updater.emit('update-downloaded');
  f.updater.emit('error', new Error('late signature error'));
  assert.ok(f.events.every(value => !Object.hasOwn(value, 'busy')));
  assert.equal(f.manager.busy, false);
  assert.equal(f.manager.ready, false);
});
test('late download progress cannot create an ownerless busy state', async () => {
  const f = fixture(true);
  await f.manager.check();
  f.events.length = 0;
  f.updater.emit('download-progress', { percent: 100 });
  assert.equal(f.events.length, 0);
  assert.equal(f.manager.busy, false);
});
test('check owner releases UI busy even without a downloaded event', async () => {
  const f = fixture(true);
  f.updater.downloadUpdate = async () => [];
  await f.manager.check();
  assert.equal(f.events.at(-1).busy, false);
  assert.equal(f.manager.busy, false);
  assert.equal(f.manager.ready, false);
  assert.throws(() => f.manager.install());
});
test('installation error events reject even when quitAndInstall returns normally', async () => {
  const f = fixture(true);
  await f.manager.check();
  f.updater.quitAndInstall = () => { f.updater.emit('error', new Error('SECRET URI')); };
  await assert.rejects(f.manager.install(), /Не удалось запустить установку/);
  assert.equal(f.manager.busy, false); assert.equal(f.manager.ready, false);
  assert.ok(!f.calls.includes('close'));
  assert.equal(f.installEvents.listenerCount('before-quit-for-update'), 0);
  assert.equal(f.updater.listenerCount('error'), 1);
  assert.ok(f.events.every(item => !JSON.stringify(item).includes('SECRET')));
});
test('installation exceptions release busy and remove temporary listeners', async () => {
  const f = fixture(true);
  await f.manager.check();
  f.updater.quitAndInstall = () => { throw new Error('SECRET PATH'); };
  await assert.rejects(f.manager.install(), /Не удалось запустить установку/);
  assert.equal(f.manager.busy, false); assert.equal(f.manager.ready, false);
  assert.equal(f.installEvents.listenerCount('before-quit-for-update'), 0);
  assert.equal(f.updater.listenerCount('error'), 1);
  assert.ok(!f.calls.includes('close'));
});
test('only a native quit confirmation allows closing for installation', async () => {
  const f = fixture(true);
  await f.manager.check();
  f.updater.quitAndInstall = () => {};
  const installing = f.manager.install();
  assert.equal(f.manager.busy, true);
  assert.ok(!f.calls.includes('close'));
  assert.throws(() => f.manager.install());
  await f.manager.check();
  assert.deepEqual(f.calls, ['check', 'download']);
  // electron-updater itself does not emit this native lifecycle event.
  f.updater.emit('before-quit-for-update');
  assert.equal(f.manager.busy, true);
  f.installEvents.emit('before-quit-for-update');
  await installing;
  assert.equal(f.calls.filter(item => item === 'close').length, 1);
  assert.equal(f.manager.busy, false); assert.equal(f.manager.ready, false);
  assert.equal(f.installEvents.listenerCount('before-quit-for-update'), 0);
  assert.equal(f.updater.listenerCount('error'), 1);
});
test('missing install confirmation times out and late events cannot allow quit', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(true);
  await f.manager.check();
  f.updater.quitAndInstall = () => {};
  const rejected = assert.rejects(f.manager.install(), /Перезапуск для обновления не подтверждён/);
  t.mock.timers.tick(5 * 60 * 1000);
  await rejected;
  assert.equal(f.manager.busy, false); assert.equal(f.manager.ready, false);
  assert.equal(f.events.at(-1).busy, false);
  assert.equal(f.installEvents.listenerCount('before-quit-for-update'), 0);
  assert.equal(f.updater.listenerCount('error'), 1);
  f.installEvents.emit('before-quit-for-update');
  f.updater.emit('error', new Error('late failure'));
  assert.ok(!f.calls.includes('close'));
  assert.throws(() => f.manager.install());
});

test('unsigned update offers a manual compatible installer without native download or restart', async () => {
  const installer = { url: 'https://github.com/ArtemIvanchenko/Printers-companion/releases/download/v1.9.0/Printer-Companion-1.9.0-mac-arm64.dmg', test_build: true };
  const f = fixture(false, { latest_version: '1.9.0', update_available: true, installer });
  await f.manager.check();
  assert.deepEqual(f.calls, ['published-check']);
  assert.deepEqual(f.manager.manualInstaller, installer);
  assert.equal(f.manager.ready, false);
  assert.equal(f.events.at(-2).manualUpdateAvailable, true);
  assert.match(f.events.at(-2).message, /тестовый установщик/);
  assert.throws(() => f.manager.install());
});

for (const [kind, release, message] of [
  ['ahead', { ahead_of_release: true }, /новее публичного/],
  ['unknown', { version_unknown: true }, /без установщика/],
  ['conflict', { identity_conflict: true, installer: {} }, /отличается/],
  ['dirty', { source_state: 'dirty' }, /без установщика/],
]) test(`${kind} package is not shown as verified current`, async () => {
  const f = fixture(false, release);
  await f.manager.check();
  assert.match(f.events.at(-2).message, message);
  assert.ok(!f.events.at(-2).message.includes('Установлена публичная'));
  assert.equal(f.manager.ready, false);
});

test('read-only network failure releases busy and cannot expose credentials or stale installer', async () => {
  const f = fixture(false);
  f.manager.manualInstaller = { url: 'old' };
  f.manager.checkPublishedRelease = async () => { throw new Error('SECRET URI'); };
  await f.manager.check();
  assert.equal(f.manager.manualInstaller, null);
  assert.equal(f.manager.busy, false);
  assert.equal(f.events.at(-1).busy, false);
  assert.ok(f.events.every(item => !JSON.stringify(item).includes('SECRET')));
});

test('a published package check never hides pending backend convergence', async () => {
  const f = fixture(false);
  f.manager.backendPending = () => true;
  await f.manager.check();
  assert.match(f.events.at(-2).message, /Вычислительная часть ещё не обновлена/);
});
