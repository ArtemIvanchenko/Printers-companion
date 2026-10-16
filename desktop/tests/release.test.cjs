'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const yaml = require('js-yaml');
const { collectRelease } = require('../scripts/collect-release.cjs');

const VERSION = '2.3.4';
const SHA = 'a'.repeat(40);
const IMAGE_REPO = 'ghcr.io/artemivanchenko/printers-companion';
const hash = (bytes, algorithm = 'sha256', encoding = 'hex') =>
  crypto.createHash(algorithm).update(bytes).digest(encoding);
const readJSON = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const writeJSON = (file, value) => fs.writeFileSync(file, JSON.stringify(value));
const readFeed = file => yaml.load(fs.readFileSync(file, 'utf8'));
const writeFeed = (file, value) => fs.writeFileSync(file, yaml.dump(value));

function fixture(t, signed) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pc-release-fixture-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const installers = path.join(directory, 'installers');
  fs.mkdirSync(installers);
  const images = Object.fromEntries(['api', 'worker', 'watcher'].map((name, i) =>
    [name, IMAGE_REPO + '@sha256:' + String(i + 4).repeat(64)]));
  const imagesFile = path.join(directory, 'operator-images.json');
  writeJSON(imagesFile, { version: VERSION, sha: SHA, update_protocol: 1,
    platform: 'linux/amd64', images });
  const artifacts = [];
  const natives = new Map();
  for (const [index, suffixes] of [
    ['1', ['mac-arm64.dmg', 'mac-arm64.zip']],
    ['2', ['mac-x64.dmg', 'mac-x64.zip']],
    ['3', ['win-x64.exe']],
  ]) {
    const artifact = path.join(installers, 'artifact-' + index);
    fs.mkdirSync(artifact);
    const resources = index === '2' ? path.join(artifact, 'runtime-resources') : artifact;
    fs.mkdirSync(resources, { recursive: true });
    const manifestFile = path.join(resources, 'runtime-manifest.json');
    writeJSON(manifestFile, { schema_version: 1, update_protocol: 1,
      version: VERSION, sha: SHA, source_state: 'clean', code_fingerprint: index.repeat(64),
      desktop_updates_enabled: signed, images,
      files: { 'docker-compose.yml': hash(Buffer.from('compose')),
        'deploy/nas/docker-compose.operator.yml': hash(Buffer.from('operator-compose')) } });
    const installedVersion = signed ? VERSION : VERSION + '-test.' + SHA.slice(0, 12) + '.' + index.repeat(8);
    const files = suffixes.map(suffix => {
      const name = 'Printer-Companion-' + installedVersion + '-' + suffix;
      const bytes = Buffer.from('real fixture bytes for ' + name + '\n'.repeat(Number(index)));
      fs.writeFileSync(path.join(artifact, name), bytes);
      natives.set(name, bytes);
      return { url: name, sha512: hash(bytes, 'sha512', 'base64'), size: bytes.length };
    });
    // Mac metadata contains both the ZIP and the DMG, with the legacy path on ZIP.
    if (files.length === 2) files.reverse();
    const feedFile = path.join(artifact, index === '3' ? 'latest.yml' : 'latest-mac.yml');
    if (signed) writeFeed(feedFile, { version: VERSION, files, path: files[0].url,
      sha512: files[0].sha512, releaseDate: '2026-10-14T00:00:00.000Z' });
    else fs.writeFileSync(feedFile, 'malformed: [not a public unsigned feed');
    artifacts.push({ artifact, manifestFile, feedFile, files });
  }
  const blockmap = artifacts[2].files[0].url + '.blockmap';
  const blockmapBytes = Buffer.from('optional blockmap');
  fs.writeFileSync(path.join(artifacts[2].artifact, blockmap), blockmapBytes);
  const output = path.join(directory, 'output');
  return { directory, installers, imagesFile, output, artifacts, natives, blockmap, blockmapBytes,
    options: { installers, imagesFile, output, version: VERSION, sha: SHA, signed } };
}

function verifyOutputs(f, signed) {
  const extra = signed ? [f.blockmap, 'latest-mac.yml', 'latest.yml'] : [];
  assert.deepEqual(fs.readdirSync(f.output).sort(),
    [...f.natives.keys(), 'operator-images.json', 'SHA256SUMS.txt', ...extra].sort());
  for (const [name, bytes] of f.natives) assert.deepEqual(fs.readFileSync(path.join(f.output, name)), bytes);
  if (signed) assert.deepEqual(fs.readFileSync(path.join(f.output, f.blockmap)), f.blockmapBytes);
  else assert.ok(!fs.existsSync(path.join(f.output, f.blockmap)));
  assert.deepEqual(readJSON(path.join(f.output, 'operator-images.json')), readJSON(f.imagesFile));
  const sums = fs.readFileSync(path.join(f.output, 'SHA256SUMS.txt'), 'utf8').trim().split('\n');
  const seen = new Set();
  for (const line of sums) {
    const match = /^([a-f0-9]{64})  ([^/\\]+)$/.exec(line);
    assert.ok(match, 'Each checksum must describe a flattened output file');
    assert.ok(!seen.has(match[2]));
    seen.add(match[2]);
    assert.equal(match[1], hash(fs.readFileSync(path.join(f.output, match[2]))));
  }
  assert.deepEqual([...seen].sort(), fs.readdirSync(f.output).filter(name => name !== 'SHA256SUMS.txt').sort());
}

test('unsigned native artifacts keep different platform fingerprints and never publish feeds', async t => {
  const f = fixture(t, false);
  await collectRelease(f.options);
  verifyOutputs(f, false);
  assert.equal(new Set(f.artifacts.map(item => readJSON(item.manifestFile).code_fingerprint)).size, 3);
  assert.ok(!fs.existsSync(path.join(f.output, 'latest-mac.yml')));
  assert.ok(!fs.existsSync(path.join(f.output, 'latest.yml')));
});

test('signed manifest gates merge all four Mac files and retain a separate Windows feed', async t => {
  // Small real files exercise hashes and IO, not Developer ID/Authenticode signatures.
  const f = fixture(t, true);
  await collectRelease(f.options);
  verifyOutputs(f, true);
  const mac = readFeed(path.join(f.output, 'latest-mac.yml'));
  const windows = readFeed(path.join(f.output, 'latest.yml'));
  assert.equal(mac.version, VERSION);
  assert.equal(windows.version, VERSION);
  assert.deepEqual(mac.files.map(item => item.url).sort(),
    [...f.natives.keys()].filter(name => /-mac-/.test(name)).sort());
  assert.deepEqual(windows.files.map(item => item.url),
    [...f.natives.keys()].filter(name => /-win-/.test(name)));
  for (const feed of [mac, windows]) {
    assert.equal(new Set(feed.files.map(item => item.url)).size, feed.files.length);
    for (const item of feed.files) {
      const bytes = f.natives.get(item.url);
      assert.ok(bytes);
      assert.equal(item.size, bytes.length);
      assert.equal(item.sha512, hash(bytes, 'sha512', 'base64'));
    }
    const legacy = feed.files.find(item => item.url === feed.path);
    assert.ok(legacy);
    assert.equal(feed.sha512, legacy.sha512);
  }
});

function mutateJSON(file, change) {
  const value = readJSON(file); change(value); writeJSON(file, value);
}
function mutateFeed(file, change) {
  const value = readFeed(file); change(value); writeFeed(file, value);
}

for (const [label, mutate, signed = true] of [
  ['array instead of code fingerprint', f => mutateJSON(f.artifacts[0].manifestFile, value => { value.code_fingerprint = [value.code_fingerprint]; })],
  ['array instead of resource hash', f => mutateJSON(f.artifacts[0].manifestFile, value => { value.files['docker-compose.yml'] = [value.files['docker-compose.yml']]; })],
  ['dirty native manifest', f => mutateJSON(f.artifacts[0].manifestFile, value => { value.source_state = 'dirty'; })],
  ['unsigned manifest declares signed channel', f => mutateJSON(f.artifacts[0].manifestFile, value => { value.desktop_updates_enabled = true; }), false],
  ['wrong full source SHA', f => mutateJSON(f.artifacts[0].manifestFile, value => { value.sha = 'b'.repeat(40); })],
  ['image manifest disagrees with bundled digests', f => mutateJSON(f.imagesFile, value => {
    value.images.api = IMAGE_REPO + '@sha256:' + '9'.repeat(64);
  })],
  ['missing native installer', f => fs.unlinkSync(path.join(f.artifacts[0].artifact, f.artifacts[0].files[1].url))],
  ['duplicate flattened native installer', f => fs.copyFileSync(
    path.join(f.artifacts[0].artifact, f.artifacts[0].files[1].url),
    path.join(f.artifacts[2].artifact, f.artifacts[0].files[1].url))],
  ['wrong feed SHA512', f => mutateFeed(f.artifacts[0].feedFile, value => { value.files[1].sha512 = hash(Buffer.from('wrong'), 'sha512', 'base64'); })],
  ['wrong feed size', f => mutateFeed(f.artifacts[0].feedFile, value => { value.files[1].size += 1; })],
  ['foreign feed path', f => mutateFeed(f.artifacts[0].feedFile, value => { value.files[1].url = '../' + value.files[1].url; })],
  ['duplicate feed item', f => mutateFeed(f.artifacts[0].feedFile, value => { value.files.push({ ...value.files[0] }); })],
  ['nonempty release output', f => {
    fs.mkdirSync(f.output); fs.writeFileSync(path.join(f.output, 'keep-existing'), 'never replace');
  }],
  ['duplicate flattened blockmap', f => fs.writeFileSync(path.join(f.artifacts[0].artifact, f.blockmap), f.blockmapBytes)],
]) test('collector refuses ' + label, async t => {
  const f = fixture(t, signed);
  mutate(f);
  await assert.rejects(collectRelease(f.options));
  if (label === 'nonempty release output') {
    assert.equal(fs.readFileSync(path.join(f.output, 'keep-existing'), 'utf8'), 'never replace');
    assert.deepEqual(fs.readdirSync(f.output), ['keep-existing']);
  }
});

test('collector refuses a symlink to an owned fixture file', { skip: process.platform === 'win32' }, async t => {
  const f = fixture(t, true);
  fs.symlinkSync(path.join(f.artifacts[0].artifact, f.artifacts[0].files[0].url),
    path.join(f.artifacts[0].artifact, 'unsafe-link'));
  await assert.rejects(collectRelease(f.options));
});

test('collector CLI resolves all relative inputs under its explicit working directory', async t => {
  const { spawnSync } = require('node:child_process');
  const f = fixture(t, true);
  const result = spawnSync(process.execPath, [require.resolve('../scripts/collect-release.cjs'),
    '--installers', 'installers', '--images', 'operator-images.json', '--output', 'output',
    '--version', VERSION, '--sha', SHA, '--signed'],
  { cwd: f.directory, encoding: 'utf8', shell: false, timeout: 30000 });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { installers: 5, signed: true, feeds: 2 });
  verifyOutputs(f, true);
});
