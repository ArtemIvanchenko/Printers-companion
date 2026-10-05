'use strict';
// Developer build convenience. Electron-builder rejects shell-special chars in
// macOS output paths; do not rename the user's project or weaken its guard.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const desktop = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
if (args.some(arg => arg.startsWith('--publish'))) throw new Error('Test package command never publishes externally.');
const destination = path.join(desktop, 'dist');
const safeOutput = process.platform === 'darwin' && /[\0\r\n"'`$;&|<>]/.test(destination)
  ? fs.mkdtempSync(path.join(os.tmpdir(), 'pc-desktop-build-')) : destination;
const environment = { ...process.env, COMPANION_BUILD_OUTPUT: safeOutput };
execFileSync(process.execPath, [path.join(desktop, 'node_modules/electron-builder/out/cli/cli.js'),
  '--config', 'builder.cjs', '--publish', 'never', ...args], { cwd: desktop, env: environment, stdio: 'inherit', shell: false });
if (safeOutput !== destination) {
  fs.mkdirSync(destination, { recursive: true });
  for (const name of fs.readdirSync(safeOutput)) {
    if (/\.(dmg|zip|exe|blockmap)$/.test(name)) fs.copyFileSync(path.join(safeOutput, name), path.join(destination, name));
  }
  console.log('Installers exported to desktop/dist. Packaged app for smoke: ' + safeOutput);
}
