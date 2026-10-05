'use strict';
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'runtime-resources/runtime-manifest.json'), 'utf8'));
if (manifest.version !== version) throw new Error('Bundled backend version differs from VERSION. Rebuild resources.');
const signed = manifest.desktop_updates_enabled === true;
if (signed && (!manifest.images || Object.keys(manifest.images).length !== 3)) throw new Error('Public package requires pinned containers.');
if (signed && (!process.env.CSC_LINK || !process.env.CSC_KEY_PASSWORD)) {
  throw new Error('Public update packages require a signing identity. No unsigned fallback.');
}
if (signed && process.platform === 'win32' && !process.env.COMPANION_PUBLISHER_NAME) {
  throw new Error('Windows update channel requires the expected certificate publisher name.');
}
if (signed && process.platform === 'darwin') {
  const notarize = (process.env.APPLE_ID && process.env.APPLE_APP_SPECIFIC_PASSWORD && process.env.APPLE_TEAM_ID) ||
    (process.env.APPLE_API_KEY && process.env.APPLE_API_KEY_ID && process.env.APPLE_API_ISSUER) || process.env.APPLE_KEYCHAIN_PROFILE;
  if (!notarize) throw new Error('Public Mac packages require notarization credentials. No silent skip.');
}
const installedVersion = signed ? version : `${version}-test.${manifest.sha.slice(0, 12)}.${manifest.code_fingerprint.slice(0, 8)}`;
module.exports = {
  appId: signed ? 'ru.printerscompanion.desktop' : 'ru.printerscompanion.desktop.test',
  productName: signed ? 'Printer’s Companion' : 'Printer’s Companion Test',
  directories: { output: process.env.COMPANION_BUILD_OUTPUT || 'dist' },
  extraMetadata: { version: installedVersion },
  // Build scripts, test fixtures, package cache and operator files cannot enter
  // the application. The resource directory is generated from an allowlist.
  files: ['main.cjs', 'preload.cjs', 'ui/**', 'lib/**', 'package.json'],
  extraResources: [{ from: 'runtime-resources', to: 'operator-runtime', filter: ['runtime-manifest.json', 'docker-compose.yml', 'deploy/nas/docker-compose.operator.yml', 'host/**'] }],
  asar: true,
  artifactName: 'Printer-Companion-${version}-${os}-${arch}.${ext}',
  mac: { category: 'public.app-category.productivity', target: ['dmg', 'zip'],
    identity: signed ? undefined : null, notarize: signed,
    hardenedRuntime: signed, gatekeeperAssess: false },
  win: { target: 'nsis', signAndEditExecutable: true, signExecutable: signed,
    verifyUpdateCodeSignature: true,
    ...(signed ? { signtoolOptions: { publisherName: process.env.COMPANION_PUBLISHER_NAME } } : {}) },
  nsis: { oneClick: true, perMachine: false, deleteAppDataOnUninstall: false },
  publish: signed ? [{ provider: 'github', owner: 'ArtemIvanchenko', repo: 'Printers-companion', releaseType: 'release' }] : null,
};
