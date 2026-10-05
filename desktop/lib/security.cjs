'use strict';
const { pathToFileURL } = require('node:url');
function trustedSender(event, window, page) {
  return Boolean(window && !window.isDestroyed() && event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === pathToFileURL(page).href);
}
function localURL(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname) ||
      parsed.username || parsed.password || !['', '/'].includes(parsed.pathname) || parsed.search || parsed.hash) {
    throw new Error('Разрешён только локальный адрес приложения.');
  }
  return parsed.origin;
}
function settingsInput(value) {
  const fields = ['host', 'db_user', 'db_password', 'database', 'access_key', 'secret_key'];
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !fields.includes(key))) throw new Error('Некорректные настройки.');
  const result = {};
  for (const key of fields) {
    const item = value[key];
    if (item !== undefined && (typeof item !== 'string' || item.length > 2048 || /[\x00\r\n]/.test(item))) {
      throw new Error('Некорректное значение настройки.');
    }
    if (item !== undefined) result[key] = item;
  }
  return result;
}
module.exports = { trustedSender, localURL, settingsInput };
