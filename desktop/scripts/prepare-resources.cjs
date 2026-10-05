// Build-time only. Only explicit non-secret resources can enter an installer.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '../..');
const output = path.resolve(__dirname, '../runtime-resources');
const files = ['docker-compose.yml', 'deploy/nas/docker-compose.operator.yml'];
const development = process.argv.includes('--development');
const imagesIndex = process.argv.indexOf('--images');
const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
const sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], { cwd: root, encoding: 'utf8' }).trim();
if (!/^\d+\.\d+\.\d+$/.test(version) || !/^[a-f0-9]{40}$/.test(sha)) throw new Error('Invalid build identity');
if (dirty && !development) throw new Error('Release resources require a clean commit. Use --development for a test package only.');
let images = {};
if (imagesIndex !== -1) {
  const imageFile = process.argv[imagesIndex + 1];
  if (!imageFile || imageFile.startsWith('--') || fs.statSync(imageFile).size > 16384) throw new Error('Missing or oversized image manifest');
  const captured = JSON.parse(fs.readFileSync(imageFile, 'utf8'));
  if (captured.sha !== sha || captured.version !== version || captured.update_protocol !== 1 || captured.platform !== 'linux/amd64') throw new Error('Image manifest identity mismatch');
  images = captured.images;
  if (!images || Object.keys(images).sort().join(',') !== 'api,watcher,worker' ||
      Object.values(images).some(value => !/^ghcr\.io\/artemivanchenko\/printers-companion@sha256:[a-f0-9]{64}$/.test(value))) throw new Error('Invalid pinned images');
}
if (!development && process.env.COMPANION_SIGNED_RELEASE === 'true' && !Object.keys(images).length) throw new Error('Signed packages require immutable container digests first');
fs.mkdirSync(output, { recursive: true });
const hashes = {};
for (const file of files) {
  const bytes = fs.readFileSync(path.join(root, file));
  hashes[file] = crypto.createHash('sha256').update(bytes).digest('hex');
  const target = path.join(output, file);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, bytes);
}
const codeHash = crypto.createHash('sha256');
function includeCode(relative) {
  const location = path.join(root, relative);
  if (fs.statSync(location).isDirectory()) {
    for (const name of fs.readdirSync(location).sort()) if (!name.startsWith('.') && name !== '__pycache__') includeCode(relative + '/' + name);
  } else {
    codeHash.update(relative + '\0'); codeHash.update(fs.readFileSync(location));
  }
}
for (const source of ['core/updating', 'core/maintenance.py', 'desktop/main.cjs', 'desktop/preload.cjs', 'desktop/lib', 'desktop/ui', 'scripts/maintenance/desktop_entry.py']) includeCode(source);
const manifest = { schema_version: 1, update_protocol: 1, version, sha,
  code_fingerprint: codeHash.digest('hex'), images,
  source_state: dirty ? 'dirty' : 'clean', built_at: new Date().toISOString(), files: hashes,
  desktop_updates_enabled: !development && process.env.COMPANION_SIGNED_RELEASE === 'true' };
fs.writeFileSync(path.join(output, 'runtime-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`Prepared ${version} ${sha.slice(0, 8)} (${manifest.source_state}); no operator data included.`);
