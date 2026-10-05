'use strict';
// Native package updater only; Docker convergence happens after relaunch using
// resources inside that exact installed package, not an unsigned remote script.
class Updates {
  constructor({ updater, enabled, notify, closeForInstall, runtimeBusy = () => false }) {
    Object.assign(this, { updater, enabled, notify, closeForInstall, runtimeBusy });
    this.ready = false; this.busy = false;
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    updater.allowPrerelease = false;
    updater.allowDowngrade = false;
    updater.on('download-progress', value => notify({ message: `Загружаю обновление: ${Math.round(value.percent)}%`, busy: true }));
    updater.on('update-downloaded', () => { this.ready = true; notify({ updateReady: true, busy: false, message: 'Обновление готово к установке.' }); });
    updater.on('error', () => { this.ready = false; notify({ updateReady: false, busy: false, message: 'Обновление недоступно или не прошло проверку. Текущая версия сохранена.' }); });
  }
  async check() {
    if (this.runtimeBusy()) return;
    if (!this.enabled) {
      this.notify({ message: 'Тестовая сборка: подписанный канал обновлений ещё не включён.' });
      return;
    }
    if (this.busy) return;
    this.busy = true;
    this.ready = false;
    this.notify({ busy: true, updateReady: false, message: 'Проверяю стабильные обновления…' });
    try {
      const result = await this.updater.checkForUpdates();
      if (!result || !result.isUpdateAvailable) {
        this.notify({ busy: false, message: 'Установлена актуальная версия приложения.' });
        return;
      }
      this.notify({ busy: true, message: 'Загружаю обновление…' });
      await this.updater.downloadUpdate();
    } catch {
      this.ready = false;
      this.notify({ updateReady: false, busy: false, message: 'Не удалось получить проверенное обновление. Текущая версия сохранена.' });
    } finally { this.busy = false; }
  }
  install() {
    if (!this.enabled || !this.ready || this.busy || this.runtimeBusy()) throw new Error('Проверенное обновление ещё не готово или приложение занято.');
    this.closeForInstall();
    this.updater.quitAndInstall(false, true);
  }
}
module.exports = { Updates };
