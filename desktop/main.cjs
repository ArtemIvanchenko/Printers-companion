'use strict';
const { app, BrowserWindow, ipcMain, dialog, Menu, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { trustedSender, localURL, settingsInput } = require('./lib/security.cjs');
const { Runtime, readInstallation, saveInstallation } = require('./lib/runtime.cjs');
const { Updates } = require('./lib/updates.cjs');

app.setName("Printer's Companion");
const smoke = process.argv.includes('--smoke');
const smokeDir = process.env.PRINTER_COMPANION_SMOKE_DIR;
if (smoke && smokeDir && path.isAbsolute(smokeDir)) app.setPath('userData', smokeDir);
let window; let runtime; let allowQuit = false; let updates; let closing = false; let deferredUpdate;
const page = path.join(__dirname, 'ui/index.html');
const bundle = app.isPackaged ? path.join(process.resourcesPath, 'operator-runtime') : path.join(__dirname, 'runtime-resources');
let manifest;
try { manifest = JSON.parse(fs.readFileSync(path.join(bundle, 'runtime-manifest.json'), 'utf8')); }
catch { manifest = { version: app.getVersion(), source_state: 'dirty' }; }
const testBuild = manifest.desktop_updates_enabled !== true;
if (testBuild && !smoke) {
  const testData = path.join(app.getPath('appData'), 'Printer Companion Test');
  fs.mkdirSync(testData, { recursive: true, mode: 0o700 });
  app.setPath('userData', testData);
}
const primaryInstance = app.requestSingleInstanceLock();
if (!primaryInstance) app.quit();
const state = { version: manifest.version, phase: 'setup', configured: false, busy: false,
  testBuild, deploymentAvailable: manifest.source_state === 'clean' && Object.keys(manifest.images || {}).length === 3,
  message: '', url: 'http://127.0.0.1:8000', updateReady: false, canRollback: false };
function notify(value) {
  Object.assign(state, value);
  if (window && !window.isDestroyed()) window.webContents.send('companion:state-changed', { ...state });
}
function installationFile() { return path.join(app.getPath('userData'), 'installation.json'); }
function makeRuntime(root) {
  return new Runtime({ root, bundle,
    binary: path.join(bundle, 'host/operator-runtime', process.platform === 'win32' ? 'operator-runtime.exe' : 'operator-runtime'),
    python: !app.isPackaged && process.env.PRINTER_COMPANION_DEV_PYTHON || undefined,
    source: path.resolve(__dirname, '..'), notify: message => notify({ message, busy: true }) });
}
async function launch() {
  if (!runtime || state.busy) return;
  clearTimeout(deferredUpdate);
  notify({ phase: 'starting', busy: true, message: 'Проверяю Docker, хранилище и версию приложения…' });
  try {
    const result = await runtime.run('launch');
    notify({ phase: 'ready', busy: false, backendVersion: result.version, backendUpdatePending: false,
      backendSHA: result.sha, url: localURL(result.url),
      message: result.version === manifest.version && result.sha === manifest.sha ? 'Приложение готово' : `Работает предыдущая версия ${result.version}. Обновление не завершено.`, canRollback: result.can_rollback });
  } catch (error) {
    let saved = {};
    try { saved = await runtime.run('status'); } catch { /* error stays bounded */ }
    if (saved.version && ['rolled_back', 'blocked', 'interrupted', 'current', 'completed'].includes(saved.phase)) {
      try {
        const restored = await runtime.run('resume');
        notify({ phase: 'ready', busy: false, backendVersion: saved.version, backendUpdatePending: true,
          backendSHA: restored.sha, url: localURL(restored.url),
          message: `Обновление не завершено. Работает подтверждённая версия ${saved.version}. ${error.message}`, canRollback: saved.can_rollback });
        if (error.code === 'busy') deferredUpdate = setTimeout(() => { if (!allowQuit) launch(); }, 30000);
        return;
      } catch { /* Never pretend failed recovery succeeded. */ }
    }
    notify({ phase: 'error', busy: false, message: error.message, canRollback: saved.can_rollback });
  }
}
function handler(name, callback) {
  ipcMain.handle('companion:' + name, async (event, ...args) => {
    if (!trustedSender(event, window, page)) throw new Error('Недоверенный источник команды.');
    try { return await callback(...args); }
    catch (error) {
      // An updater/settings error is not evidence the running API failed.
      notify({ phase: state.phase === 'ready' ? 'ready' : 'error', busy: false, message: error.message });
      throw error;
    }
  });
}
function setupHandlers() {
  handler('state', () => ({ ...state }));
  handler('launch', launch);
  handler('configure', async input => {
    if (state.busy || state.configured || fs.existsSync(installationFile())) throw new Error('Установка уже настроена или занята.');
    if (!state.deploymentAvailable) throw new Error('В тестовом пакете ещё нет проверенной вычислительной сборки. Новая установка не запускается; реквизиты NAS вводить не нужно.');
    const values = settingsInput(input);
    const root = path.join(app.getPath('userData'), 'operator');
    runtime = makeRuntime(root);
    notify({ busy: true, phase: 'starting', message: 'Сохраняю настройки на этом компьютере…' });
    await runtime.run('configure', values);
    saveInstallation(installationFile(), root);
    notify({ configured: true, busy: false });
    await launch();
  });
  handler('import', async () => {
    if (state.busy || state.configured || fs.existsSync(installationFile())) throw new Error('Установка уже подключена или занята.');
    const selection = await dialog.showOpenDialog(window, { title: 'Папка существующего Printer’s Companion', properties: ['openDirectory'] });
    if (selection.canceled) return;
    const root = path.resolve(selection.filePaths[0]);
    if (!fs.existsSync(path.join(root, 'docker-compose.yml'))) throw new Error('В выбранной папке нет установки Printer’s Companion.');
    // Validate BEFORE committing the binding; a wrong folder remains retryable.
    runtime = makeRuntime(root);
    notify({ busy: true, phase: 'starting', message: 'Проверяю существующую установку без переноса данных…' });
    await runtime.run('adopt');
    saveInstallation(installationFile(), root);
    notify({ configured: true, busy: false });
    await launch();
  });
  handler('recover', async () => {
    if (!runtime || state.busy) throw new Error('Установка недоступна или занята.');
    notify({ phase: 'starting', busy: true, message: 'Восстанавливаю подтверждённую предыдущую версию…' });
    const result = await runtime.run('rollback');
    notify({ phase: 'ready', busy: false, message: `Восстановлена версия ${result.version}.`, backendVersion: result.version,
      backendSHA: result.sha, url: localURL(result.url), canRollback: result.can_rollback });
  });
  handler('check-update', () => updates.check());
  handler('install-update', async () => {
    if (runtime?.busy || state.busy || !updates.enabled || !updates.ready) throw new Error('Проверенное обновление ещё не готово или приложение занято.');
    clearTimeout(deferredUpdate);
    const previousPhase = state.phase;
    notify({ phase: 'starting', busy: true, message: 'Завершаю принятые загрузки перед перезапуском…' });
    try {
      if (runtime) await runtime.run('prepare-restart');
      // The short-lived preparation child has exited; install's busy guard
      // must now pass. The durable gate remains until the next launch.
      notify({ phase: previousPhase, busy: false });
      updates.install();
    } catch (error) {
      allowQuit = false;
      let recovered = true;
      if (runtime) { try { await runtime.run('cancel-restart'); } catch { recovered = false; } }
      notify({ phase: recovered ? previousPhase : 'error', busy: false, canRollback: !recovered || state.canRollback,
        message: recovered ? error.message : 'Отмена перезапуска не подтверждена. Нужна проверка восстановления; журнал сохранён.' });
      throw error;
    }
  });
}
async function closeWindow(event) {
  if (allowQuit || smoke) return;
  event.preventDefault();
  if (closing) return;
  closing = true;
  try {
  if (state.busy || runtime?.busy) {
    await dialog.showMessageBox(window, { type: 'info', message: 'Операция ещё выполняется', detail: 'Дождитесь завершения переключения. Данные и журнал обновления сохраняются.', buttons: ['Продолжить ожидание'] });
    return;
  }
  if (state.phase !== 'ready' || !runtime) { allowQuit = true; app.quit(); return; }
  const answer = await dialog.showMessageBox(window, { message: 'Закрыть Printer’s Companion?',
    detail: 'Можно оставить вычисления работать или остановить приложение. NAS и данные не затрагиваются.',
    buttons: ['Оставить работать и закрыть окно', 'Остановить и выйти', 'Отмена'], defaultId: 0, cancelId: 2 });
  if (answer.response === 2) return;
  if (answer.response === 1) {
    notify({ busy: true, message: 'Проверяю возможность безопасной остановки…' });
    try { await runtime.run('stop'); }
    catch (error) { notify({ busy: false, message: error.message }); return; }
  }
  clearTimeout(deferredUpdate);
  allowQuit = true;
  app.quit();
  } finally { closing = false; }
}
app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
app.on('before-quit', event => { if (!allowQuit && !smoke && window && !window.isDestroyed()) { event.preventDefault(); window.close(); } });
app.on('window-all-closed', () => { allowQuit = true; app.quit(); });
app.whenReady().then(async () => {
  if (!primaryInstance) return;
  window = new BrowserWindow({ width: 1320, height: 900, minWidth: 880, minHeight: 680,
    title: 'Printer’s Companion', backgroundColor: '#1b1917', show: false,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), sandbox: true,
      contextIsolation: true, nodeIntegration: false, webSecurity: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-frame-navigate', event => {
    if (event.isMainFrame) { event.preventDefault(); return; }
    try { if (new URL(event.url).origin !== state.url) event.preventDefault(); }
    catch { event.preventDefault(); }
  });
  window.webContents.on('will-redirect', (event, url, _inPlace, isMainFrame) => {
    try { if (isMainFrame || new URL(url).origin !== state.url) event.preventDefault(); }
    catch { event.preventDefault(); }
  });
  window.on('close', closeWindow);
  updates = new Updates({ updater: require('electron-updater').autoUpdater,
    enabled: app.isPackaged && manifest.desktop_updates_enabled === true,
    runtimeBusy: () => Boolean(runtime?.busy || state.phase === 'starting'),
    notify, closeForInstall: () => { allowQuit = true; } });
  setupHandlers();
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Printer’s Companion', submenu: [{ label: 'Проверить обновления', click: () => updates.check() },
      { type: 'separator' }, { label: 'Выйти', accelerator: process.platform === 'darwin' ? 'Cmd+Q' : 'Alt+F4', click: () => window.close() }] },
    { label: 'Правка', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: 'Вид', submenu: [{ role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
    { label: 'Помощь', submenu: [{ label: 'Документация', click: () => shell.openExternal('https://github.com/ArtemIvanchenko/Printers-companion') }] },
  ]));
  await window.loadFile(page);
  window.show();
  if (smoke) {
    // Exercise real packaged preload/IPC, not only screenshot static HTML.
    const bridge = await window.webContents.executeJavaScript('window.companion.state()');
    if (bridge.phase !== 'setup' || bridge.configured) throw new Error('Packaged IPC smoke failed');
    if (testBuild) {
      await window.webContents.executeJavaScript('window.companion.checkUpdate()');
      if (!state.message.includes('Тестовая сборка')) throw new Error('Unsigned channel smoke failed');
    }
    notify({ message: 'Тестовая сборка · без подключения к рабочим данным' });
    if (smokeDir) {
      await new Promise(resolve => setTimeout(resolve, 350));
      fs.writeFileSync(path.join(smokeDir, 'desktop.png'), (await window.webContents.capturePage()).toPNG());
      const previewURL = process.env.PRINTER_COMPANION_SMOKE_URL;
      if (previewURL) {
        notify({ phase: 'ready', configured: true, url: localURL(previewURL), message: 'Изолированный стенд просмотра · не рабочая база', backendSHA: 'preview' });
        const deadline = Date.now() + 15000;
        let frame;
        while (Date.now() < deadline) {
          frame = window.webContents.mainFrame.frames.find(item => item.url === state.url + '/');
          if (frame && await frame.executeJavaScript('document.readyState') === 'complete') break;
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        if (!frame) throw new Error('Dashboard iframe did not load');
        if (await frame.executeJavaScript('typeof window.companion') !== 'undefined') throw new Error('Untrusted iframe has a native bridge');
        const catalog = await frame.executeJavaScript("fetch('/prints?limit=10').then(async response => ({ok: response.ok, body: await response.json()}))");
        if (!catalog.ok || !Array.isArray(catalog.body.items)) throw new Error('Dashboard catalog API is not usable');
        await new Promise(resolve => setTimeout(resolve, 700));
        fs.writeFileSync(path.join(smokeDir, 'dashboard.png'), (await window.webContents.capturePage()).toPNG());
      }
    }
    allowQuit = true; app.quit(); return;
  }
  try {
    const preview = !app.isPackaged && process.env.PRINTER_COMPANION_PREVIEW_URL;
    if (preview) { notify({ configured: true, phase: 'ready', url: localURL(preview), message: 'Просмотр интерфейса · Docker и рабочие данные не изменяются' }); return; }
    let installation = readInstallation(installationFile());
    const pendingRoot = path.join(app.getPath('userData'), 'operator');
    if (!installation && fs.existsSync(path.join(pendingRoot, '.update-state/setup.json'))) {
      runtime = makeRuntime(pendingRoot);
      notify({ busy: true, phase: 'starting', message: 'Восстанавливаю завершённую первую настройку…' });
      await runtime.run('finish-setup');
      saveInstallation(installationFile(), pendingRoot);
      installation = { root: pendingRoot };
      notify({ busy: false });
    }
    if (installation) { runtime = makeRuntime(installation.root); notify({ configured: true }); await launch(); }
  } catch (error) { notify({ phase: 'error', busy: false, message: error.message }); }
}).catch(error => {
  // Bounded diagnostics; not arbitrary process/SQL/environment output.
  console.error('Desktop startup/smoke failed: ' + (smoke ? error.message : 'unconfirmed'));
  allowQuit = true; app.exit(1);
});
