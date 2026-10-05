'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Runtime, readInstallation, saveInstallation } = require('../lib/runtime.cjs');
function fakeChild(result, code = 0) {
  const child = new EventEmitter();
  for (const stream of ['stdin', 'stdout', 'stderr']) child[stream] = new PassThrough();
  process.nextTick(() => { child.stdout.end(JSON.stringify(result) + '\n'); setImmediate(() => child.emit('close', code)); });
  return child;
}
test('portable paths, no shell, secrets only through stdin', async () => {
  let call; let input = '';
  const runtime = new Runtime({ root: '/tmp/оператор с пробелами', bundle: '/app resources', binary: '/app/bin/runtime',
    spawnProcess: (command, args, options) => {
      call = { command, args, options };
      const child = fakeChild({ type: 'result', state: { phase: 'configured' } });
      child.stdin.on('data', bytes => { input += bytes; }); return child;
    } });
  const result = await runtime.run('configure', { db_password: 'PRIVATE SECRET' });
  assert.equal(result.phase, 'configured'); assert.equal(call.options.shell, false);
  assert.ok(!JSON.stringify(call.args).includes('PRIVATE')); assert.ok(input.includes('PRIVATE'));
  assert.equal(call.args[2], '/tmp/оператор с пробелами');
});
test('unknown host action rejected before process execution', async () => {
  const runtime = new Runtime({});
  await assert.rejects(runtime.run('delete-data'));
});
test('failed process does not claim success', async () => {
  const runtime = new Runtime({ root: '/tmp/test', bundle: '/bundle', binary: '/binary',
    spawnProcess: () => fakeChild({ type: 'error', message: 'Настройки сохранены' }, 1) });
  await assert.rejects(runtime.run('launch'), /Настройки сохранены/); assert.equal(runtime.busy, false);
});
test('typed busy response can defer a switch without hiding verified old backend', async () => {
  const runtime = new Runtime({ root: '/tmp/test', bundle: '/bundle', binary: '/binary',
    spawnProcess: () => fakeChild({ type: 'error', message: 'Расчёт продолжается', code: 'busy' }, 1) });
  await assert.rejects(runtime.run('prepare-restart'), error => error.code === 'busy');
  assert.equal(runtime.busy, false);
});
test('durable binding is private, read back and never overwritten', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'companion-binding-'));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const file = path.join(directory, 'installation.json');
  saveInstallation(file, directory);
  assert.equal(readInstallation(file).root, directory);
  const saved = fs.readFileSync(file);
  assert.throws(() => saveInstallation(file, path.dirname(directory)));
  assert.deepEqual(fs.readFileSync(file), saved);
  if (process.platform !== 'win32') assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});
