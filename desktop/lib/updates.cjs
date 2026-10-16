'use strict';
// Native package updater only; Docker convergence happens after relaunch using
// resources inside that exact installed package, not an unsigned remote script.
const INSTALL_TIMEOUT_MS = 5 * 60 * 1000;
class Updates {
  constructor({ updater, installEvents, enabled, notify, closeForInstall, runtimeBusy = () => false,
    checkPublishedRelease, backendPending = () => false }) {
    Object.assign(this, { updater, installEvents, enabled, notify, closeForInstall, runtimeBusy, checkPublishedRelease, backendPending });
    this.ready = false; this.busy = false;
    this.manualInstaller = null;
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    updater.allowPrerelease = false;
    updater.allowDowngrade = false;
    // Persistent library events carry artifact/progress information, not
    // ownership of the main window's current lifecycle operation.
    updater.on('download-progress', value => {
      if (this.busy) notify({ message: `Загружаю обновление: ${Math.round(value.percent)}%` });
    });
    updater.on('update-downloaded', () => {
      this.ready = true;
      notify({ updateReady: true, ...(this.busy || !this.runtimeBusy()
        ? { message: 'Обновление готово к установке.' } : {}) });
    });
    updater.on('error', () => {
      this.ready = false;
      notify({ updateReady: false, ...(this.busy || !this.runtimeBusy()
        ? { message: 'Обновление недоступно или не прошло проверку. Текущая версия сохранена.' } : {}) });
    });
  }
  async check() {
    if (this.runtimeBusy()) return;
    if (this.busy) return;
    this.busy = true;
    this.ready = false;
    this.manualInstaller = null;
    this.notify({ busy: true, updateReady: false, manualUpdateAvailable: false, message: 'Проверяю опубликованную версию на GitHub…' });
    try {
      if (!this.enabled) {
        const release = await this.checkPublishedRelease();
        if (release?.phase !== 'release_checked' || typeof release.latest_version !== 'string') throw new Error('Invalid release response');
        const changed = release.update_available || release.version_unknown || release.identity_conflict || release.source_state !== 'clean';
        this.manualInstaller = !release.ahead_of_release && changed ? release.installer : null;
        let message;
        if (release.ahead_of_release) message = `Ваша сборка новее публичного релиза ${release.latest_version}.`;
        else if (this.manualInstaller) {
          const context = release.identity_conflict ? 'Локальная сборка отличается от публичной. ' : release.version_unknown ? 'Локальная версия не подтверждена. ' : release.source_state !== 'clean' ? 'Локальная сборка изменена. ' : '';
          message = `${context}Доступна версия ${release.latest_version}${this.manualInstaller.test_build ? ' · тестовый установщик' : ''}. Установка вручную.`;
        }
        else if (changed && !release.installer) message = `Релиз ${release.latest_version} опубликован без установщика для этого компьютера.`;
        else if (changed) message = `Локальная сборка отличается от публичной ${release.latest_version}.`;
        else message = `Установлена публичная версия ${release.latest_version}.`;
        if (this.backendPending()) message += ' Вычислительная часть ещё не обновлена.';
        this.notify({ manualUpdateAvailable: Boolean(this.manualInstaller), message });
        return;
      }
      const result = await this.updater.checkForUpdates();
      if (!result || !result.isUpdateAvailable) {
        this.notify({ message: this.backendPending() ? 'Пакет актуален; вычислительная часть ещё не обновлена.' : 'Установлена актуальная версия приложения.' });
        return;
      }
      this.notify({ busy: true, message: 'Загружаю обновление…' });
      await this.updater.downloadUpdate();
    } catch {
      this.ready = false;
      this.manualInstaller = null;
      this.notify({ updateReady: false, manualUpdateAvailable: false, message: 'Не удалось проверить обновление. Текущая версия сохранена.' });
    } finally { this.busy = false; this.notify({ busy: false }); }
  }
  install() {
    if (!this.enabled || !this.ready || this.busy || this.runtimeBusy()) throw new Error('Проверенное обновление ещё не готово или приложение занято.');
    this.busy = true;
    this.ready = false;
    this.notify({ busy: true, updateReady: false, message: 'Ожидаю подтверждения перезапуска для обновления…' });
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.installEvents.removeListener('before-quit-for-update', onQuit);
        this.updater.removeListener('error', onError);
        this.busy = false;
        this.ready = false;
        this.notify({ busy: false, updateReady: false });
        if (error) reject(error);
        else resolve();
      };
      const onError = () => finish(new Error('Не удалось запустить установку проверенного обновления. Текущая версия сохранена.'));
      const onQuit = () => {
        if (settled) return;
        try { this.closeForInstall(); }
        catch { onError(); return; }
        finish();
      };
      // electron-updater reports installation errors as events, not throws.
      // Its native Electron emitter confirms the quit on both macOS and Windows.
      // A missing confirmation must release the operation and the restart gate;
      // removing these listeners also prevents a late event from allowing quit.
      this.installEvents.once('before-quit-for-update', onQuit);
      this.updater.once('error', onError);
      const timer = setTimeout(() => finish(new Error('Перезапуск для обновления не подтверждён.')), INSTALL_TIMEOUT_MS);
      try { this.updater.quitAndInstall(false, true); }
      catch { onError(); }
    });
  }
}
module.exports = { Updates };
