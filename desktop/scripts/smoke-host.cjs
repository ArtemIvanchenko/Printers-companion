'use strict';
// Exercises the frozen executable, not the source Python interpreter. Does not
// require Docker, access NAS, import logs, or modify any operator installation.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const bundle = path.resolve(process.argv[2] || path.join(__dirname, '../runtime-resources'));
const binary = path.join(bundle, 'host/operator-runtime', process.platform === 'win32' ? 'operator-runtime.exe' : 'operator-runtime');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'companion-frozen-check-'));
function run(action, input) {
  const raw = execFileSync(binary, [action, '--root', root, '--bundle', bundle],
    { input: input ? JSON.stringify(input) : '', encoding: 'utf8', shell: false });
  assert.ok(!raw.includes('TEST_ONLY_PRIVATE'));
  return JSON.parse(raw.trim().split('\n').at(-1)).state;
}
try {
  assert.equal(run('status').phase, 'unconfigured');
  assert.equal(run('configure', { host: 'fixture.example.test', db_password: 'TEST_ONLY_PRIVATE_кириллица', access_key: 'test-key', secret_key: 'TEST_ONLY_PRIVATE_кириллица' }).phase, 'configured');
  assert.equal(run('finish-setup').phase, 'configured');
  assert.ok(fs.readFileSync(path.join(root, 'deploy/nas/.env.operator'), 'utf8').includes('operator-'));
  assert.ok(fs.readFileSync(path.join(root, 'deploy/nas/.env.operator'), 'utf8').includes('кириллица'));
  console.log('Frozen host smoke passed: private config, recovery, bounded JSON; no Docker/NAS access.');
} finally {
  // Exact mkdtemp fixture only. Never app userData or a selected project.
  fs.rmSync(root, { recursive: true });
}
