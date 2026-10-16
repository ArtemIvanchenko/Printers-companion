'use strict';
// Build-time only. Publication stays in the final release job after all gates.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const yaml = require('js-yaml');
const IMAGE_REPOSITORY = 'ghcr.io/artemivanchenko/printers-companion';
const RESOURCE_FILES = ['deploy/nas/docker-compose.operator.yml', 'docker-compose.yml'];

function requireValue(condition, message) { if (!condition) throw new Error(message); }
function readMetadata(file, format = 'json') {
  const info = fs.lstatSync(file);
  requireValue(info.isFile() && !info.isSymbolicLink() && info.size <= 1024 * 1024, 'Invalid release metadata file');
  const text = fs.readFileSync(file, 'utf8');
  const data = format === 'yaml' ? yaml.load(text, { schema: yaml.JSON_SCHEMA }) : JSON.parse(text);
  requireValue(data && typeof data === 'object' && !Array.isArray(data), 'Invalid release metadata object');
  return data;
}
function filesIn(directory) {
  const info = fs.lstatSync(directory);
  requireValue(info.isDirectory() && !info.isSymbolicLink(), 'Foreign or symlink artifact directory');
  const files = [];
  for (const name of fs.readdirSync(directory).sort()) {
    const file = path.join(directory, name); const entry = fs.lstatSync(file);
    requireValue(!entry.isSymbolicLink(), 'Symlink release artifact');
    if (entry.isDirectory()) files.push(...filesIn(file));
    else { requireValue(entry.isFile(), 'Invalid release artifact'); files.push(file); }
  }
  return files;
}
async function hashes(file) {
  const before = fs.lstatSync(file);
  requireValue(before.isFile() && !before.isSymbolicLink(), 'Invalid native artifact');
  const sha256 = crypto.createHash('sha256'); const sha512 = crypto.createHash('sha512'); let size = 0;
  for await (const block of fs.createReadStream(file)) { size += block.length; sha256.update(block); sha512.update(block); }
  requireValue(size === before.size, 'Native artifact changed during collection');
  return { size, sha256: sha256.digest('hex'), sha512: sha512.digest('base64') };
}
function verifiedImages(value, version, sha) {
  requireValue(value.version === version && value.sha === sha && value.update_protocol === 1 && value.platform === 'linux/amd64', 'Image manifest identity mismatch');
  const images = value.images;
  requireValue(images && Object.keys(images).sort().join(',') === 'api,watcher,worker', 'Incomplete pinned images');
  for (const image of Object.values(images)) requireValue(typeof image === 'string' && new RegExp('^' + IMAGE_REPOSITORY.replaceAll('.', '\\.') + '@sha256:[0-9a-f]{64}$').test(image), 'Invalid pinned image');
  return images;
}
function sameImages(left, right) {
  return left && Object.keys(left).sort().join(',') === 'api,watcher,worker' && ['api', 'worker', 'watcher'].every(name => left[name] === right[name]);
}
function verifyFeed(file, native, version) {
  const feed = readMetadata(file, 'yaml');
  requireValue(feed.version === version && Array.isArray(feed.files) && feed.files.length === native.size, 'Feed version or native composition mismatch');
  const names = new Set();
  for (const entry of feed.files) {
    requireValue(entry && typeof entry.url === 'string' && native.has(entry.url) && !names.has(entry.url), 'Duplicate or foreign feed path');
    const actual = native.get(entry.url).hashes;
    requireValue(Number.isSafeInteger(entry.size) && entry.size > 0 && entry.size === actual.size && entry.sha512 === actual.sha512, 'Feed checksum or size mismatch');
    names.add(entry.url);
  }
  if (feed.path !== undefined || feed.sha512 !== undefined) {
    requireValue(native.has(feed.path) && feed.sha512 === native.get(feed.path).hashes.sha512, 'Legacy feed checksum or path mismatch');
  }
  return feed;
}

async function collectRelease({ installers, imagesFile, output, version, sha, signed = false }) {
  requireValue(/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version) && /^[0-9a-f]{40}$/.test(sha), 'Invalid stable release identity');
  requireValue(typeof signed === 'boolean', 'Invalid release channel');
  installers = path.resolve(installers); imagesFile = path.resolve(imagesFile); output = path.resolve(output);
  requireValue(output !== installers && !output.startsWith(installers + path.sep), 'Release output cannot contain input artifacts');
  const captured = readMetadata(imagesFile); const images = verifiedImages(captured, version, sha);
  const artifacts = filesIn(installers);
  const manifests = artifacts.filter(file => path.basename(file) === 'runtime-manifest.json');
  requireValue(manifests.length === 3, 'Expected three native runtime manifests');
  const native = new Map(); const blockmaps = new Map(); const feeds = new Map(); const kinds = new Set();
  const top = path.resolve(installers);
  for (const manifestFile of manifests) {
    const relative = path.relative(top, manifestFile).split(path.sep);
    requireValue(relative.length > 1, 'Runtime manifest must belong to a native artifact');
    const artifactRoot = path.join(top, relative[0]);
    const files = artifacts.filter(file => file.startsWith(artifactRoot + path.sep));
    requireValue(files.filter(file => path.basename(file) === 'runtime-manifest.json').length === 1, 'Duplicate runtime manifest in artifact');
    const manifest = readMetadata(manifestFile);
    requireValue(manifest.schema_version === 1 && manifest.update_protocol === 1 && manifest.version === version && manifest.sha === sha && manifest.source_state === 'clean' && manifest.desktop_updates_enabled === signed, 'Native runtime identity mismatch');
    requireValue(sameImages(manifest.images, images), 'Native runtime pinned images mismatch');
    requireValue(manifest.files && Object.keys(manifest.files).sort().join(',') === RESOURCE_FILES.join(',') && Object.values(manifest.files).every(value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)), 'Invalid bundled resource hashes');
    requireValue(typeof manifest.code_fingerprint === 'string' && /^[0-9a-f]{64}$/.test(manifest.code_fingerprint), 'Invalid native code fingerprint');
    const installedVersion = signed ? version : `${version}-test.${sha.slice(0, 12)}.${manifest.code_fingerprint.slice(0, 8)}`;
    const expected = new Map([
      [`Printer-Companion-${installedVersion}-mac-arm64.dmg`, 'mac-arm64'],
      [`Printer-Companion-${installedVersion}-mac-arm64.zip`, 'mac-arm64'],
      [`Printer-Companion-${installedVersion}-mac-x64.dmg`, 'mac-x64'],
      [`Printer-Companion-${installedVersion}-mac-x64.zip`, 'mac-x64'],
      [`Printer-Companion-${installedVersion}-win-x64.exe`, 'win-x64'],
    ]);
    const ownNative = new Map(); const ownKinds = new Set();
    for (const file of files.filter(file => /\.(dmg|zip|exe)$/.test(file))) {
      const name = path.basename(file);
      requireValue(expected.has(name) && !native.has(name), 'Foreign or duplicate native installer');
      const item = { file, hashes: await hashes(file) };
      requireValue(item.hashes.size > 0, 'Empty native installer');
      ownKinds.add(expected.get(name)); ownNative.set(name, item); native.set(name, item);
    }
    requireValue(ownKinds.size === 1, 'Mixed or missing native architecture');
    const kind = [...ownKinds][0];
    requireValue(!kinds.has(kind) && ownNative.size === (kind === 'win-x64' ? 1 : 2), 'Incomplete or duplicate native architecture');
    kinds.add(kind);
    for (const file of files.filter(file => file.endsWith('.blockmap'))) {
      const name = path.basename(file);
      requireValue(ownNative.has(name.slice(0, -'.blockmap'.length)) && !blockmaps.has(name), 'Foreign or duplicate blockmap');
      blockmaps.set(name, file);
    }
    if (signed) {
      const name = kind === 'win-x64' ? 'latest.yml' : 'latest-mac.yml';
      const candidates = files.filter(file => /^latest.*\.yml$/.test(path.basename(file)));
      requireValue(candidates.length === 1 && path.basename(candidates[0]) === name, 'Missing or foreign native feed');
      feeds.set(kind, verifyFeed(candidates[0], ownNative, version));
    }
  }
  requireValue(native.size === 5 && [...kinds].sort().join(',') === 'mac-arm64,mac-x64,win-x64', 'Expected five native installers');
  requireValue(artifacts.filter(file => /\.(dmg|zip|exe)$/.test(file)).length === native.size && artifacts.filter(file => file.endsWith('.blockmap')).length === blockmaps.size, 'Unowned native release artifact');
  const selected = new Map([...native].map(([name, item]) => [name, item.file]));
  if (signed) for (const [name, file] of blockmaps) selected.set(name, file);
  const generated = new Map();
  if (signed) {
    const arm = feeds.get('mac-arm64'); const intel = feeds.get('mac-x64');
    const keys = new Set([...Object.keys(arm), ...Object.keys(intel)]);
    for (const key of keys) if (!['files', 'path', 'sha512', 'releaseDate'].includes(key)) requireValue(JSON.stringify(arm[key]) === JSON.stringify(intel[key]), 'Mac feed metadata mismatch');
    const files = [...arm.files, ...intel.files].sort((a, b) => Number(!a.url.endsWith('.zip')) - Number(!b.url.endsWith('.zip')) || a.url.localeCompare(b.url, 'en'));
    generated.set('latest-mac.yml', yaml.dump({ ...arm, files, path: files[0].url, sha512: files[0].sha512 }));
    generated.set('latest.yml', yaml.dump(feeds.get('win-x64')));
  }
  selected.set('operator-images.json', imagesFile);
  if (fs.existsSync(output)) {
    requireValue(fs.lstatSync(output).isDirectory() && !fs.lstatSync(output).isSymbolicLink() && fs.readdirSync(output).length === 0, 'Release output must be an empty owned directory');
  }
  fs.mkdirSync(output, { recursive: true });
  const sums = [];
  for (const [name, file] of [...selected].sort(([a], [b]) => a.localeCompare(b, 'en'))) {
    const destination = path.join(output, name);
    fs.copyFileSync(file, destination, fs.constants.COPYFILE_EXCL);
    const actual = await hashes(destination);
    if (native.has(name)) requireValue(actual.sha512 === native.get(name).hashes.sha512 && actual.size === native.get(name).hashes.size, 'Copied installer changed');
    sums.push(`${actual.sha256}  ${name}`);
  }
  for (const [name, text] of generated) {
    const file = path.join(output, name); fs.writeFileSync(file, text, { flag: 'wx' });
    sums.push(`${(await hashes(file)).sha256}  ${name}`);
  }
  fs.writeFileSync(path.join(output, 'SHA256SUMS.txt'), sums.sort().join('\n') + '\n', { flag: 'wx' });
  return { installers: native.size, signed, feeds: generated.size };
}

if (require.main === module) {
  const args = process.argv.slice(2); const options = {};
  for (let index = 0; index < args.length; index++) {
    const name = args[index];
    if (name === '--signed') { options.signed = true; continue; }
    const keys = { '--installers': 'installers', '--images': 'imagesFile', '--output': 'output', '--version': 'version', '--sha': 'sha' };
    requireValue(Object.hasOwn(keys, name) && args[index + 1] && !args[index + 1].startsWith('--') && !Object.hasOwn(options, keys[name]), 'Invalid collector arguments');
    options[keys[name]] = args[++index];
  }
  collectRelease(options).then(value => console.log(JSON.stringify(value))).catch(() => { console.error('Release artifact validation failed; nothing was published.'); process.exitCode = 1; });
}
module.exports = { collectRelease };
