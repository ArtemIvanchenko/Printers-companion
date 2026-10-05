'use strict';
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '../..');
// Build-machine dependency only: operator machines receive a frozen executable.
const python = process.env.COMPANION_BUILD_PYTHON || 'python';
execFileSync(python, ['-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'operator-runtime',
  '--distpath', 'desktop/runtime-resources/host', '--workpath', 'desktop/.build/pyinstaller',
  '--specpath', 'desktop/.build', '--paths', '.', 'scripts/maintenance/desktop_entry.py'],
{ cwd: root, stdio: 'inherit', shell: false });
