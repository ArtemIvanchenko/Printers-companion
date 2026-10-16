'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { Updates } = require('../lib/updates.cjs');

async function fixture(platform = 'darwin', configured = true, { updatesEnabled = true, release } = {}) {
  const handlers = new Map(); const calls = []; const dialogs = [];
  const lifecycle = []; const openDialogs = [];
  const externalURLs = []; const appPaths = { userData: '/fake/user-data', appData: '/fake/app-data' };
  const updater = new EventEmitter(); const nativeUpdater = new EventEmitter();
  updater.checkForUpdates = async () => ({ isUpdateAvailable: true });
  updater.downloadUpdate = async () => { updater.emit('update-downloaded'); };
  const app = Object.assign(new EventEmitter(), {
    isPackaged: true, setName() {}, getVersion: () => '1.8.0', getPath: name => appPaths[name],
    setPath(name, value) { appPaths[name] = value; },
    requestSingleInstanceLock: () => true, whenReady: async () => {},
    quit() {
      lifecycle.push('app-quit');
      app.emit('before-quit', { preventDefault: () => lifecycle.push('app-quit-blocked') });
    },
  });
  const nativeQuit = () => {
    lifecycle.push('native-before-quit-for-update');
    nativeUpdater.emit('before-quit-for-update');
    app.quit();
  };
  nativeUpdater.quitAndInstall = nativeQuit;
  nativeUpdater.checkForUpdates = () => {
    lifecycle.push('native-check');
    setImmediate(() => {
      lifecycle.push('native-update-downloaded');
      nativeUpdater.emit('update-downloaded');
    });
  };
  updater.quitAndInstall = () => {
    calls.push('install');
    if (platform === 'win32') {
      // 6.8.9 NsisUpdater inherits BaseUpdater: after doInstall succeeds,
      // setImmediate emits on electron.autoUpdater, then AppAdapter.quit().
      setImmediate(nativeQuit);
    } else {
      // 6.8.9 MacUpdater with autoInstallOnAppQuit=false fetches through the
      // native updater, then handleUpdateDownloaded calls native quitAndInstall.
      nativeUpdater.on('update-downloaded', () => nativeUpdater.quitAndInstall());
      nativeUpdater.checkForUpdates();
    }
  };
  let window; let runtime; let manager; let gate = false;
  let binding = configured ? '/fake/operator' : null;
  class TrackedUpdates extends Updates {
    constructor(options) { super(options); manager = this; }
  }
  class FakeWindow extends EventEmitter {
    constructor() {
      super(); window = this;
      this.webContents = Object.assign(new EventEmitter(), {
        mainFrame: { url: pathToFileURL(path.join(__dirname, '../ui/index.html')).href },
        send() {}, setWindowOpenHandler() {}, session: { setPermissionRequestHandler() {} },
      });
    }
    isDestroyed() { return false; }
    async loadFile() {}
    show() {}
    close() { this.emit('close', { preventDefault() {} }); }
  }
  const actions = release ? { 'check-release': () => release } : {};
  class FakeRuntime {
    constructor(options) { runtime = this; this.root = options.root; this.busy = false; }
    async run(action) {
      if (this.busy) throw new Error('Другая операция ещё выполняется.');
      calls.push(action); this.busy = true;
      try {
        const result = actions[action] ? await actions[action]() : null;
        if (action === 'prepare-restart') gate = true;
        if (action === 'cancel-restart') gate = false;
        return result || { version: '1.8.0', sha: 'test-sha', url: 'http://127.0.0.1:8000' };
      } finally { this.busy = false; }
    }
  }
  const fakeFS = {
    readFileSync: () => JSON.stringify({ version: '1.8.0', sha: 'test-sha', source_state: 'clean', desktop_updates_enabled: updatesEnabled, images: { a: 1, b: 2, c: 3 } }),
    mkdirSync() {},
    existsSync: file => file.endsWith('installation.json') ? binding !== null : file.endsWith('docker-compose.yml'),
  };
  const mainFile = path.join(__dirname, '../main.cjs');
  const modules = {
    electron: { app, BrowserWindow: FakeWindow, autoUpdater: nativeUpdater,
      ipcMain: { handle: (name, callback) => handlers.set(name, callback) },
      dialog: {
        showMessageBox: async (_window, input) => { dialogs.push(input); return { response: 2 }; },
        showOpenDialog: async (_window, input) => {
          openDialogs.push(input);
          return actions.selectDirectory ? actions.selectDirectory() : { canceled: false, filePaths: ['/fake/imported-operator'] };
        },
      },
      Menu: { buildFromTemplate: value => value, setApplicationMenu() {} },
      shell: { openExternal: async url => { externalURLs.push(url); } } },
    'electron-updater': { autoUpdater: updater },
    'node:fs': fakeFS,
    'node:path': path,
    './lib/security.cjs': require('../lib/security.cjs'),
    './lib/runtime.cjs': { Runtime: FakeRuntime, readInstallation: () => binding === null ? null : { root: binding },
      saveInstallation: (_file, root) => {
        if (binding !== null) throw new Error('Привязка установки уже существует; автоматическая замена запрещена.');
        binding = root; calls.push('save-binding');
      } },
    './lib/updates.cjs': { Updates: TrackedUpdates },
  };
  vm.runInNewContext(fs.readFileSync(mainFile, 'utf8'), {
    __dirname: path.dirname(mainFile), require: name => {
      assert.ok(Object.hasOwn(modules, name), `unexpected require: ${name}`);
      return modules[name];
    },
    process: { argv: [], env: {}, platform, resourcesPath: '/fake/resources' },
    console, URL, setTimeout, clearTimeout,
  }, { filename: mainFile });
  await new Promise(resolve => setImmediate(resolve));
  const invoke = (name, ...args) => handlers.get('companion:' + name)({ sender: window.webContents, senderFrame: window.webContents.mainFrame }, ...args);
  assert.equal((await invoke('state')).phase, configured ? 'ready' : 'setup');
  await invoke('check-update');
  calls.length = 0;
  return { updater, nativeUpdater, manager, actions, calls, dialogs, openDialogs, lifecycle, fakeFS, externalURLs,
    nativeDialog: modules.electron.dialog,
    get runtime() { return runtime; }, get binding() { return binding; }, invoke, gate: () => gate,
    invokeFrom: (name, event, ...args) => handlers.get('companion:' + name)(event, ...args),
    get webContents() { return window.webContents; },
    requestQuit: () => {
      let prevented = false;
      app.emit('before-quit', { preventDefault: () => { prevented = true; } });
      return prevented;
    } };
}

function manualRelease(platform = 'darwin') {
  return { phase: 'release_checked', latest_version: '1.9.0', update_available: true, source_state: 'clean',
    installer: { test_build: true, url: 'https://github.com/ArtemIvanchenko/Printers-companion/releases/download/v1.9.0/' +
      (platform === 'win32' ? 'Printer-Companion-Test-1.9.0-win-x64.exe' : 'Printer-Companion-Test-1.9.0-mac-arm64.dmg') } };
}

for (const platform of ['darwin', 'win32']) {
  test(platform + ' checks GitHub before NAS configuration through only the read-only host action', async () => {
    const f = await fixture(platform, false, { updatesEnabled: false, release: manualRelease(platform) });
    let nativeCalls = 0;
    f.updater.checkForUpdates = f.updater.downloadUpdate = f.updater.quitAndInstall = () => { nativeCalls++; };
    await f.invoke('check-update');
    const state = await f.invoke('state');
    assert.deepEqual(f.calls, ['check-release']);
    assert.equal(nativeCalls, 0);
    assert.equal(f.binding, null);
    assert.equal(state.configured, false);
    assert.equal(state.phase, 'setup');
    assert.equal(state.busy, false);
    assert.equal(state.manualUpdateAvailable, true);
    assert.equal(state.updateReady, false);
    assert.equal(f.gate(), false);
    assert.deepEqual(f.externalURLs, []);
    assert.deepEqual(f.lifecycle, []);
  });
  test(platform + ' unsigned manual update opens only the selected GitHub installer without restart or install', async () => {
    const release = manualRelease(platform);
    const f = await fixture(platform, true, { updatesEnabled: false, release });
    const before = await f.invoke('state');
    await f.invoke('open-update', 'https://untrusted.invalid/renderer-supplied.exe');
    assert.deepEqual(f.externalURLs, [release.installer.url]);
    await assert.rejects(f.invoke('install-update'), /Проверенное обновление/);
    const state = await f.invoke('state');
    assert.equal(state.phase, before.phase);
    assert.equal(state.backendSHA, before.backendSHA);
    assert.equal(state.url, before.url);
    assert.equal(state.manualUpdateAvailable, true);
    assert.equal(state.updateReady, false);
    assert.equal(f.manager.enabled, false);
    assert.equal(f.gate(), false);
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.lifecycle, []);
  });
}
test('manual installer IPC preserves runtime and updater operation ownership', async () => {
  for (const owner of ['runtime', 'updates']) {
    const f = await fixture('darwin', true, { updatesEnabled: false, release: manualRelease() });
    const operation = owner === 'runtime' ? f.runtime : f.manager;
    operation.busy = true;
    try {
      await assert.rejects(f.invoke('open-update'), /занято/);
      assert.equal(operation.busy, true);
      assert.equal((await f.invoke('state')).phase, 'ready');
      assert.deepEqual(f.externalURLs, []);
      assert.deepEqual(f.calls, []);
      assert.deepEqual(f.lifecycle, []);
      assert.equal(f.gate(), false);
    } finally { operation.busy = false; }
  }
});
test('manual installer IPC refuses while the native setup selector owns main busy', async () => {
  const f = await fixture('darwin', false, { updatesEnabled: false, release: manualRelease() });
  let select;
  f.actions.selectDirectory = () => new Promise(resolve => { select = resolve; });
  const importing = f.invoke('import');
  try {
    await assert.rejects(f.invoke('open-update'), /занято/);
    assert.equal((await f.invoke('state')).busy, true);
    assert.deepEqual(f.externalURLs, []);
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.lifecycle, []);
    assert.equal(f.gate(), false);
  } finally { select({ canceled: true, filePaths: [] }); await importing; }
});
test('manual installer IPC rejects untrusted protocols, hosts, credentials and repository paths', async () => {
  const f = await fixture('darwin', true, { updatesEnabled: false, release: manualRelease() });
  for (const url of [
    'http://github.com/ArtemIvanchenko/Printers-companion/releases/download/v1.9.0/installer.exe',
    'https://github.com.invalid/ArtemIvanchenko/Printers-companion/releases/download/v1.9.0/installer.exe',
    'https://user:password@github.com/ArtemIvanchenko/Printers-companion/releases/download/v1.9.0/installer.exe',
    'https://github.com/another/repository/releases/download/v1.9.0/installer.exe',
  ]) {
    f.manager.manualInstaller = { url };
    await assert.rejects(f.invoke('open-update'), /Недоверенный адрес/);
  }
  assert.deepEqual(f.externalURLs, []);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(f.lifecycle, []);
  assert.equal(f.gate(), false);
});
test('dashboard iframe cannot invoke the manual installer IPC', async () => {
  const f = await fixture('darwin', true, { updatesEnabled: false, release: manualRelease() });
  const sender = f.webContents;
  await assert.rejects(f.invokeFrom('open-update', { sender, senderFrame: { url: 'http://127.0.0.1:8000/' } }),
    /Недоверенный источник/);
  assert.deepEqual(f.externalURLs, []);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(f.lifecycle, []);
  assert.equal(f.gate(), false);
});

for (const platform of ['darwin', 'win32']) {
  test(`${platform} native install path permits quit and preserves the prepared restart gate`, async () => {
    const f = await fixture(platform);
    await f.invoke('install-update');
    assert.deepEqual(f.calls, ['prepare-restart', 'install']);
    assert.equal(f.gate(), true);
    assert.deepEqual(f.lifecycle, platform === 'darwin'
      ? ['native-check', 'native-update-downloaded', 'native-before-quit-for-update', 'app-quit']
      : ['native-before-quit-for-update', 'app-quit']);
    assert.equal(f.requestQuit(), false);
    assert.equal(f.nativeUpdater.listenerCount('before-quit-for-update'), 0);
  });
}
test('install error events cancel the durable restart and keep normal quit protection', async () => {
  const f = await fixture();
  f.updater.quitAndInstall = () => { f.calls.push('install'); f.updater.emit('error', new Error('SECRET PATH')); };
  await assert.rejects(f.invoke('install-update'), /Не удалось запустить установку/);
  assert.deepEqual(f.calls, ['prepare-restart', 'install', 'cancel-restart']);
  assert.equal(f.gate(), false);
  const state = await f.invoke('state');
  assert.equal(state.phase, 'ready'); assert.equal(state.busy, false); assert.equal(state.updateReady, false);
  assert.ok(!state.message.includes('SECRET'));
  f.nativeUpdater.emit('before-quit-for-update');
  assert.equal(f.requestQuit(), true);
});
test('prepare-restart failure never calls the package installer', async () => {
  const f = await fixture();
  f.actions['prepare-restart'] = () => { throw new Error('Подготовка не завершена'); };
  await assert.rejects(f.invoke('install-update'), /Подготовка не завершена/);
  assert.deepEqual(f.calls, ['prepare-restart', 'cancel-restart']);
  assert.equal(f.gate(), false);
  assert.equal((await f.invoke('state')).busy, false);
  assert.equal(f.requestQuit(), true);
});
test('quitAndInstall exceptions cancel the prepared restart', async () => {
  const f = await fixture();
  f.updater.quitAndInstall = () => { f.calls.push('install'); throw new Error('SECRET PATH'); };
  await assert.rejects(f.invoke('install-update'), /Не удалось запустить установку/);
  assert.deepEqual(f.calls, ['prepare-restart', 'install', 'cancel-restart']);
  assert.equal(f.gate(), false);
  assert.equal((await f.invoke('state')).busy, false);
  assert.equal(f.requestQuit(), true);
});
test('missing install event releases busy, cancels the gate, and cannot grant quit later', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = await fixture();
  f.updater.quitAndInstall = () => { f.calls.push('install'); };
  const rejected = assert.rejects(f.invoke('install-update'), /Перезапуск для обновления не подтверждён/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await f.invoke('state')).busy, true);
  assert.equal(f.requestQuit(), true);
  assert.match(f.dialogs.at(-1).message, /Операция ещё выполняется/);
  t.mock.timers.tick(5 * 60 * 1000);
  await rejected;
  assert.deepEqual(f.calls, ['prepare-restart', 'install', 'cancel-restart']);
  assert.equal(f.gate(), false);
  assert.equal((await f.invoke('state')).busy, false);
  assert.equal(f.nativeUpdater.listenerCount('before-quit-for-update'), 0);
  f.nativeUpdater.emit('before-quit-for-update');
  assert.equal(f.requestQuit(), true);
  assert.equal(f.dialogs.at(-1).message, 'Закрыть Printer’s Companion?');
});
test('failed cancellation reports unconfirmed recovery instead of ready backend', async () => {
  const f = await fixture();
  f.updater.quitAndInstall = () => { f.updater.emit('error', new Error('install failed')); };
  f.actions['cancel-restart'] = () => { throw new Error('cancellation failed'); };
  await assert.rejects(f.invoke('install-update'));
  const state = await f.invoke('state');
  assert.equal(state.phase, 'error'); assert.equal(state.busy, false); assert.equal(state.canRollback, true);
  assert.match(state.message, /Отмена перезапуска не подтверждена/);
  assert.equal(f.gate(), true);
});

for (const terminal of ['success', 'error', 'timeout']) {
  test(`duplicate IPC preserves native install ownership through ${terminal}`, async t => {
    if (terminal === 'timeout') t.mock.timers.enable({ apis: ['setTimeout'] });
    const f = await fixture();
    f.updater.quitAndInstall = () => { f.calls.push('install'); };
    const settled = f.invoke('install-update').then(() => ({ ok: true }), error => ({ ok: false, error }));
    await new Promise(resolve => setImmediate(resolve));
    try {
      await assert.rejects(f.invoke('install-update'), /занято/);
      const state = await f.invoke('state');
      assert.equal(state.busy, true); assert.equal(state.phase, 'ready');
      await assert.rejects(f.invoke('launch'), /операция|занято/);
      await assert.rejects(f.invoke('recover'), /занята/);
      assert.deepEqual(f.calls, ['prepare-restart', 'install']);
      assert.equal(f.requestQuit(), true);
      assert.match(f.dialogs.at(-1).message, /Операция ещё выполняется/);
      assert.equal(f.gate(), true);
    } finally {
      if (terminal === 'success') f.nativeUpdater.emit('before-quit-for-update');
      else if (terminal === 'error') f.updater.emit('error', new Error('fake install failure'));
      else t.mock.timers.tick(5 * 60 * 1000);
      await settled;
    }
    assert.equal((await settled).ok, terminal === 'success');
    assert.equal((await f.invoke('state')).busy, false);
    assert.equal(f.manager.busy, false);
    assert.equal(f.gate(), terminal === 'success');
    assert.equal(f.requestQuit(), terminal !== 'success');
  });
}

test('downloaded metadata preserves the native install owner and excludes launch and close', async () => {
  const f = await fixture();
  f.updater.quitAndInstall = () => { f.calls.push('install'); };
  const settled = f.invoke('install-update');
  await new Promise(resolve => setImmediate(resolve));
  try {
    f.updater.emit('update-downloaded');
    assert.equal((await f.invoke('state')).busy, true);
    assert.equal(f.manager.busy, true);
    await assert.rejects(f.invoke('launch'), /операция|занято/);
    assert.equal(f.requestQuit(), true);
    assert.match(f.dialogs.at(-1).message, /Операция ещё выполняется/);
    assert.deepEqual(f.calls, ['prepare-restart', 'install']);
  } finally {
    f.nativeUpdater.emit('before-quit-for-update');
    await settled;
  }
});

test('rejected competing call preserves the launch owner until it publishes ready', async () => {
  const f = await fixture();
  let finish;
  f.actions.launch = () => new Promise(resolve => { finish = resolve; });
  const launching = f.invoke('launch');
  try {
    await assert.rejects(f.invoke('recover'), /занята/);
    const state = await f.invoke('state');
    assert.equal(state.phase, 'starting'); assert.equal(state.busy, true);
    await f.invoke('check-update');
    assert.deepEqual(f.calls, ['launch']);
  } finally { finish(); await launching; }
  assert.equal((await f.invoke('state')).phase, 'ready');
  assert.equal((await f.invoke('state')).busy, false);
});

test('rejected lifecycle call preserves the pending update check owner', async () => {
  const f = await fixture();
  let finish;
  f.updater.checkForUpdates = () => new Promise(resolve => { finish = resolve; });
  const checking = f.invoke('check-update');
  try {
    await assert.rejects(f.invoke('install-update'), /занято/);
    const state = await f.invoke('state');
    assert.equal(state.phase, 'ready'); assert.equal(state.busy, true);
    await assert.rejects(f.invoke('launch'), /операция|занято/);
    assert.deepEqual(f.calls, []);
  } finally { finish({ isUpdateAvailable: false }); await checking; }
  assert.equal(f.manager.busy, false);
  assert.equal((await f.invoke('state')).busy, false);
  await f.invoke('launch');
  assert.deepEqual(f.calls, ['launch']);
});

for (const action of ['configure', 'adopt', 'rollback']) {
  test(`${action} releases its own failed lifecycle operation`, async () => {
    const f = await fixture('darwin', action === 'rollback');
    f.actions[action] = () => { throw new Error('fake owner failure'); };
    await assert.rejects(f.invoke({ configure: 'configure', adopt: 'import', rollback: 'recover' }[action], {}), /fake owner failure/);
    const state = await f.invoke('state');
    assert.equal(state.busy, false); assert.equal(state.phase, 'error');
    assert.equal(f.runtime.busy, false);
    assert.ok(!f.calls.includes('save-binding'));
  });
}

test('accepted import owns the delayed native selector and excludes update checks and quit', async () => {
  const f = await fixture('darwin', false);
  let select; let checks = 0;
  f.actions.selectDirectory = () => new Promise(resolve => { select = resolve; });
  f.updater.checkForUpdates = async () => { checks++; return { isUpdateAvailable: false }; };
  const importing = f.invoke('import');
  try {
    const state = await f.invoke('state');
    assert.equal(state.busy, true); assert.equal(state.phase, 'starting');
    assert.equal(f.runtime, undefined); assert.equal(f.binding, null);
    await f.invoke('check-update');
    assert.equal(checks, 0);
    assert.equal(f.requestQuit(), true);
    assert.match(f.dialogs.at(-1).message, /Операция ещё выполняется/);
    assert.deepEqual(f.lifecycle, []);
  } finally {
    select({ canceled: false, filePaths: ['/fake/imported-operator'] });
    await importing;
  }
  assert.deepEqual(f.calls, ['adopt', 'save-binding', 'launch']);
  assert.equal(f.binding, '/fake/imported-operator');
  assert.equal(f.runtime.root, f.binding);
  assert.equal((await f.invoke('state')).phase, 'ready');
  assert.equal((await f.invoke('state')).busy, false);
});

test('double import cannot open a second selector while the accepted selector is pending', async () => {
  const f = await fixture('darwin', false);
  let select;
  f.actions.selectDirectory = () => f.openDialogs.length === 1
    ? new Promise(resolve => { select = resolve; }) : { canceled: true, filePaths: [] };
  const importing = f.invoke('import');
  try {
    await assert.rejects(f.invoke('import'), /занята/);
    assert.equal(f.openDialogs.length, 1);
    assert.equal((await f.invoke('state')).busy, true);
  } finally {
    select({ canceled: false, filePaths: ['/fake/imported-operator'] });
    await importing;
  }
  assert.deepEqual(f.calls, ['adopt', 'save-binding', 'launch']);
});

test('accepted import excludes configure and keeps the selected runtime aligned with its binding', async () => {
  const f = await fixture('darwin', false);
  let select;
  f.actions.selectDirectory = () => new Promise(resolve => { select = resolve; });
  const settled = f.invoke('import').then(() => ({ ok: true }), error => ({ ok: false, error }));
  try {
    await assert.rejects(f.invoke('configure', {}), /занята/);
    assert.equal(f.runtime, undefined); assert.equal(f.binding, null);
  } finally {
    select({ canceled: false, filePaths: ['/fake/imported-operator'] });
    await settled;
  }
  assert.equal((await settled).ok, true);
  assert.equal(f.runtime.root, '/fake/imported-operator');
  assert.equal(f.binding, f.runtime.root);
});

test('pending import prevents quit before a late native folder selection', async () => {
  const f = await fixture('darwin', false);
  let select;
  f.actions.selectDirectory = () => new Promise(resolve => { select = resolve; });
  const importing = f.invoke('import');
  try {
    f.requestQuit();
    assert.deepEqual(f.lifecycle, []);
    assert.match(f.dialogs.at(-1).message, /Операция ещё выполняется/);
  } finally {
    select({ canceled: false, filePaths: ['/fake/imported-operator'] });
    await importing;
  }
  assert.deepEqual(f.calls, ['adopt', 'save-binding', 'launch']);
});

for (const previousPhase of ['setup', 'error']) {
  test(`cancelled import restores the previous ${previousPhase} state without changing its runtime or binding`, async () => {
    const f = await fixture('darwin', false);
    if (previousPhase === 'error') {
      f.actions.configure = () => { throw new Error('previous configure failure'); };
      await assert.rejects(f.invoke('configure', {}), /previous configure failure/);
    }
    const previous = await f.invoke('state'); const previousRuntime = f.runtime;
    const previousCalls = f.calls.slice();
    let select;
    f.actions.selectDirectory = () => new Promise(resolve => { select = resolve; });
    const importing = f.invoke('import');
    try {
      await assert.rejects(f.invoke('configure', {}), /занята/);
    } finally {
      select({ canceled: true, filePaths: [] }); await importing;
    }
    const state = await f.invoke('state');
    assert.equal(state.phase, previous.phase); assert.equal(state.message, previous.message);
    assert.equal(state.busy, false); assert.equal(state.configured, false);
    assert.equal(f.runtime, previousRuntime); assert.equal(f.binding, null);
    assert.deepEqual(f.calls, previousCalls);
    f.actions.selectDirectory = () => ({ canceled: false, filePaths: ['/fake/retry-operator'] });
    await f.invoke('import');
    assert.equal(f.binding, '/fake/retry-operator');
  });
}

for (const failure of ['native-dialog', 'invalid-folder']) {
  test(`${failure} import failure releases its ownership and leaves the binding retryable`, async () => {
    const f = await fixture('darwin', false);
    if (failure === 'native-dialog') f.actions.selectDirectory = () => { throw new Error('native selector failure'); };
    else {
      f.actions.selectDirectory = () => ({ canceled: false, filePaths: ['/fake/wrong-folder'] });
      const exists = f.fakeFS.existsSync;
      f.fakeFS.existsSync = file => file.startsWith('/fake/wrong-folder/') ? false : exists(file);
    }
    await assert.rejects(f.invoke('import'), failure === 'native-dialog' ? /native selector failure/ : /нет установки/);
    const state = await f.invoke('state');
    assert.equal(state.phase, 'error'); assert.equal(state.busy, false);
    assert.equal(f.runtime, undefined); assert.equal(f.binding, null); assert.deepEqual(f.calls, []);
    f.actions.selectDirectory = () => ({ canceled: false, filePaths: ['/fake/retry-operator'] });
    await f.invoke('import');
    assert.deepEqual(f.calls, ['adopt', 'save-binding', 'launch']);
    assert.equal(f.runtime.root, '/fake/retry-operator'); assert.equal(f.binding, f.runtime.root);
  });
}

test('selected import retains ownership through adoption before committing and launching', async () => {
  const f = await fixture('darwin', false);
  let adopted;
  f.actions.adopt = () => new Promise(resolve => { adopted = resolve; });
  const importing = f.invoke('import');
  await new Promise(resolve => setImmediate(resolve));
  try {
    assert.equal((await f.invoke('state')).busy, true);
    await assert.rejects(f.invoke('import'), /занята/);
    await assert.rejects(f.invoke('configure', {}), /занята/);
    assert.equal(f.openDialogs.length, 1); assert.equal(f.binding, null);
    assert.deepEqual(f.calls, ['adopt']);
  } finally { adopted(); await importing; }
  assert.deepEqual(f.calls, ['adopt', 'save-binding', 'launch']);
  assert.equal(f.binding, f.runtime.root);
  assert.equal((await f.invoke('state')).busy, false);
});

for (const response of [0, 1, 2]) {
  test(`quit answer ${response} preserves a deferred launch accepted while the native dialog was open`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const f = await fixture();
    f.actions.launch = () => { const error = new Error('calculation still running'); error.code = 'busy'; throw error; };
    f.actions.status = () => ({ phase: 'blocked', version: '1.7.0', sha: 'old-sha' });
    f.actions.resume = () => ({ version: '1.7.0', sha: 'old-sha', url: 'http://127.0.0.1:8000' });
    await f.invoke('launch');
    assert.equal((await f.invoke('state')).backendUpdatePending, true);
    assert.equal((await f.invoke('state')).phase, 'ready');
    let answer; let finishLaunch;
    f.nativeDialog.showMessageBox = async (_window, input) => {
      f.dialogs.push(input);
      return input.type === 'info' ? { response: 0 } : new Promise(resolve => { answer = resolve; });
    };
    f.requestQuit();
    assert.equal(f.dialogs.at(-1).message, 'Закрыть Printer’s Companion?');
    f.calls.length = 0;
    f.actions.launch = () => new Promise(resolve => { finishLaunch = resolve; });
    try {
      t.mock.timers.tick(30000);
      assert.equal(f.runtime.busy, true);
      answer({ response });
      await new Promise(resolve => setImmediate(resolve));
      const state = await f.invoke('state');
      assert.equal(state.phase, 'starting'); assert.equal(state.busy, true);
      assert.deepEqual(f.calls, ['launch']); assert.deepEqual(f.lifecycle, []);
      if (response !== 2) assert.equal(f.dialogs.at(-1).message, 'Операция ещё выполняется');
      else assert.equal(f.dialogs.length, 1);
    } finally {
      answer({ response: 2 });
      if (finishLaunch) finishLaunch();
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal((await f.invoke('state')).phase, 'ready');
    assert.equal((await f.invoke('state')).busy, false);
    assert.equal((await f.invoke('state')).backendUpdatePending, false);
  });
}

test('late quit answer cannot close during an accepted update check', async () => {
  const f = await fixture();
  let answer; let finishCheck;
  f.nativeDialog.showMessageBox = async (_window, input) => {
    f.dialogs.push(input);
    return input.type === 'info' ? { response: 0 } : new Promise(resolve => { answer = resolve; });
  };
  f.requestQuit();
  f.updater.checkForUpdates = () => new Promise(resolve => { finishCheck = resolve; });
  const checking = f.invoke('check-update');
  try {
    assert.equal(f.manager.busy, true);
    answer({ response: 0 });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.lifecycle, []); assert.deepEqual(f.calls, []);
    assert.equal(f.manager.busy, true); assert.equal((await f.invoke('state')).busy, true);
    assert.equal(f.dialogs.at(-1).message, 'Операция ещё выполняется');
  } finally {
    answer({ response: 2 });
    finishCheck({ isUpdateAvailable: false }); await checking;
  }
  assert.equal((await f.invoke('state')).busy, false);
});

for (const response of [0, 1, 2]) {
  test(`normal ready quit answer ${response} keeps its existing close/stop/cancel behavior`, async () => {
    const f = await fixture();
    f.nativeDialog.showMessageBox = async (_window, input) => { f.dialogs.push(input); return { response }; };
    f.requestQuit();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.calls, response === 1 ? ['stop'] : []);
    assert.deepEqual(f.lifecycle, response === 2 ? [] : ['app-quit']);
    assert.equal(f.dialogs.length, 1);
    assert.equal(f.dialogs[0].message, 'Закрыть Printer’s Companion?');
  });
}

test('failed stop leaves a retryable error instead of an allegedly ready dashboard', async () => {
  const f = await fixture();
  f.nativeDialog.showMessageBox = async () => ({ response: 1 });
  f.actions.stop = () => { throw new Error('Остановка API не подтверждена'); };
  f.requestQuit();
  await new Promise(resolve => setImmediate(resolve));
  const state = await f.invoke('state');
  assert.equal(state.phase, 'error');
  assert.equal(state.busy, false);
  assert.equal(state.configured, true);
  assert.match(state.message, /Остановка API не подтверждена/);
  assert.deepEqual(f.lifecycle, []);
  assert.deepEqual(f.calls, ['stop']);
  await f.invoke('launch');
  assert.equal((await f.invoke('state')).phase, 'ready');
  assert.deepEqual(f.calls, ['stop', 'launch']);
});

test('accepted stop cancels the deferred update retry even when stopping fails', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = await fixture();
  f.actions.launch = () => { const error = new Error('calculation still running'); error.code = 'busy'; throw error; };
  f.actions.status = () => ({ phase: 'blocked', version: '1.7.0', sha: 'old-sha' });
  f.actions.resume = () => ({ version: '1.7.0', sha: 'old-sha', url: 'http://127.0.0.1:8000' });
  await f.invoke('launch');
  assert.equal((await f.invoke('state')).backendUpdatePending, true);
  f.calls.length = 0;
  f.nativeDialog.showMessageBox = async () => ({ response: 1 });
  f.actions.stop = () => { throw new Error('Остановка API не подтверждена'); };
  f.requestQuit();
  await new Promise(resolve => setImmediate(resolve));
  t.mock.timers.tick(30000);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls, ['stop']);
  assert.equal((await f.invoke('state')).phase, 'error');
});

async function deferBackendUpdate(f) {
  f.actions.launch = () => { const error = new Error('calculation still running'); error.code = 'busy'; throw error; };
  f.actions.status = () => ({ phase: 'blocked', version: '1.7.0', sha: 'old-sha' });
  f.actions.resume = () => ({ version: '1.7.0', sha: 'old-sha', url: 'http://127.0.0.1:8000' });
  await f.invoke('launch');
  assert.equal((await f.invoke('state')).backendUpdatePending, true);
  delete f.actions.launch;
  f.calls.length = 0;
}

for (const mode of ['idle', 'cancel-quit', 'check-success', 'check-error']) {
  test(`deferred backend retry survives ${mode} without overlapping operations`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const f = await fixture();
    await deferBackendUpdate(f);
    let finishCheck; let checking;
    if (mode.startsWith('check-')) {
      f.updater.checkForUpdates = () => new Promise((resolve, reject) => {
        finishCheck = () => mode === 'check-error' ? reject(new Error('offline')) : resolve({ isUpdateAvailable: false });
      });
      checking = f.invoke('check-update');
      // A rejected recovery does not own cancellation of the waiting retry.
      await assert.rejects(f.invoke('recover'), /занята/);
      for (let i = 0; i < 3; i++) {
        t.mock.timers.tick(30000);
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(f.calls, []);
        assert.equal((await f.invoke('state')).busy, true);
      }
      finishCheck(); await checking;
    } else if (mode === 'cancel-quit') {
      f.nativeDialog.showMessageBox = async () => ({ response: 2 });
      f.requestQuit();
      await new Promise(resolve => setImmediate(resolve));
    }
    t.mock.timers.tick(30000);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.calls, ['launch']);
    assert.equal((await f.invoke('state')).backendUpdatePending, false);
    t.mock.timers.tick(90000);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.calls, ['launch']);
  });
}

for (const fails of [false, true]) {
  test(`accepted rollback cancels an older deferred update, failure=${fails}`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const f = await fixture();
    await deferBackendUpdate(f);
    f.actions.rollback = () => {
      if (fails) throw new Error('recovery unconfirmed');
      return { version: '1.7.0', sha: 'old-sha', url: 'http://127.0.0.1:8000' };
    };
    if (fails) await assert.rejects(f.invoke('recover'), /recovery unconfirmed/);
    else await f.invoke('recover');
    t.mock.timers.tick(90000);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.calls, ['rollback']);
    assert.equal((await f.invoke('state')).phase, fails ? 'error' : 'ready');
  });
}

for (const action of ['launch', 'install-update']) {
  test(`accepted ${action} cancels the waiting deferred retry`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const f = await fixture();
    await deferBackendUpdate(f);
    if (action === 'install-update') {
      f.updater.quitAndInstall = () => { f.calls.push('install'); f.updater.emit('error', new Error('installer failure')); };
      await assert.rejects(f.invoke(action), /Не удалось запустить установку/);
    } else await f.invoke(action);
    const expected = action === 'launch' ? ['launch'] : ['prepare-restart', 'install', 'cancel-restart'];
    t.mock.timers.tick(90000);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(f.calls, expected);
    assert.equal((await f.invoke('state')).busy, false);
  });
}
