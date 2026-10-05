'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const { trustedSender, localURL, settingsInput } = require('../lib/security.cjs');
test('only the packaged main frame may control the host', () => {
  const page = path.resolve('/tmp/Printer Companion/ui.html');
  const frame = { url: pathToFileURL(page).href };
  const contents = { mainFrame: frame };
  const window = { isDestroyed: () => false, webContents: contents };
  assert.equal(trustedSender({ sender: contents, senderFrame: frame }, window, page), true);
  assert.equal(trustedSender({ sender: contents, senderFrame: { url: frame.url } }, window, page), false);
  assert.equal(trustedSender({ sender: {}, senderFrame: frame }, window, page), false);
  frame.url = 'http://127.0.0.1:8000/';
  assert.equal(trustedSender({ sender: contents, senderFrame: frame }, window, page), false);
});
for (const url of ['https://example.com', 'http://100.78.114.66:8000', 'file:///etc/passwd',
  'http://127.0.0.1:8000/a', 'http://user:secret@localhost:8000', 'http://localhost:8000/?x=1']) {
  test('reject non-local preview target ' + url, () => assert.throws(() => localURL(url)));
}
test('local preview URL and bounded settings', () => {
  assert.equal(localURL('http://127.0.0.1:8001/'), 'http://127.0.0.1:8001');
  assert.deepEqual(settingsInput({ host: 'nas', db_password: "a$'#" }), { host: 'nas', db_password: "a$'#" });
  assert.throws(() => settingsInput({ command: 'docker kill other' }));
  assert.throws(() => settingsInput({ db_password: 'secret\nDATABASE_URL=evil' }));
  assert.throws(() => settingsInput([]));
});
