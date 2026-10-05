'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
function dockerPath(environment, platform = process.platform) {
  const candidates = platform === 'darwin' ? ['/Applications/Docker.app/Contents/Resources/bin', '/Applications/OrbStack.app/Contents/MacOS/xbin', '/usr/local/bin', '/opt/homebrew/bin', path.join(require('node:os').homedir(), '.docker/bin')] :
    platform === 'win32' ? [path.join(environment.ProgramFiles || 'C:\\Program Files', 'Docker/Docker/resources/bin')] : [];
  const valid = candidates.filter(directory => fs.existsSync(path.join(directory, platform === 'win32' ? 'docker.exe' : 'docker')));
  return [environment.PATH || environment.Path || '', ...valid].filter(Boolean).join(path.delimiter);
}
const ACTIONS = new Set(['configure', 'finish-setup', 'adopt', 'launch', 'resume', 'stop', 'status', 'rollback', 'prepare-restart', 'cancel-restart']);
class Runtime {
  constructor({ root, bundle, binary, python, source, notify = () => {}, spawnProcess = spawn }) {
    Object.assign(this, { root, bundle, binary, python, source, notify, spawnProcess });
    this.busy = false;
  }
  async run(action, input = null) {
    if (!ACTIONS.has(action) || this.busy) throw new Error('Другая операция ещё выполняется.');
    this.busy = true;
    try {
      let command = this.binary;
      let args = [];
      if (this.python) { command = this.python; args.push(path.join(this.source, 'scripts/maintenance/desktop_entry.py')); }
      args.push(action, '--root', this.root, '--bundle', this.bundle);
      return await new Promise((resolve, reject) => {
        const environment = { ...process.env };
        // Never inherit a remote daemon or unrelated compose project.
        for (const key of ['DOCKER_HOST', 'DOCKER_CONTEXT', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'PYTHONPATH']) delete environment[key];
        environment.PATH = dockerPath(environment);
        const child = this.spawnProcess(command, args, { shell: false, windowsHide: true,
          env: environment, stdio: ['pipe', 'pipe', 'pipe'] });
        let result; let message = 'Операция не подтверждена. Данные и настройки сохранены.';
        let protocolError = false; let errorCode = 'unconfirmed';
        const reader = readline.createInterface({ input: child.stdout });
        reader.on('line', line => {
          if (line.length > 32768) { protocolError = true; return; }
          try {
            const event = JSON.parse(line);
            if (event.type === 'progress' && typeof event.message === 'string') this.notify(event.message.slice(0, 1000));
            if (event.type === 'error' && typeof event.message === 'string') {
              message = event.message.slice(0, 1000);
              if (event.code === 'busy') errorCode = 'busy';
            }
            if (event.type === 'result') result = event.state;
          } catch { protocolError = true; }
        });
        child.stderr.resume(); // Raw stderr can contain credentials: never show it in UI.
        child.on('error', () => reject(new Error('Встроенный модуль запуска недоступен. Проверьте целостность установки.')));
        child.on('close', code => {
          if (code === 0 && result && !protocolError) resolve(result);
          else { const error = new Error(message); error.code = errorCode; reject(error); }
        });
        child.stdin.end(input === null ? '' : JSON.stringify(input));
        child.stdin.on('error', () => {});
      });
    } finally { this.busy = false; }
  }
}
function readInstallation(file) {
  if (!fs.existsSync(file)) return null;
  if (fs.statSync(file).size > 8192) throw new Error('Повреждена привязка установки. Исходные данные не изменялись.');
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (value.schema_version !== 1 || typeof value.root !== 'string' || !path.isAbsolute(value.root)) {
    throw new Error('Повреждена привязка установки. Исходные данные не изменялись.');
  }
  return value;
}
function saveInstallation(file, root) {
  if (fs.existsSync(file)) throw new Error('Привязка установки уже существует; автоматическая замена запрещена.');
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = file + '.pending-' + require('node:crypto').randomUUID();
  try {
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify({ schema_version: 1, root: path.resolve(root) })); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    // link is an atomic, no-clobber commit on both NTFS and macOS. A second
    // instance cannot overwrite a previously saved root. No secret is stored.
    fs.linkSync(temporary, file);
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}
module.exports = { Runtime, readInstallation, saveInstallation, dockerPath };
