'use strict';
const el = id => document.getElementById(id);
let current = {};
function render(state) {
  const previous = current;
  current = state;
  el('version').textContent = state.version ? 'v' + state.version + (state.testBuild ? ' · тест' : '') : '';
  el('status').textContent = state.message || '';
  el('update').disabled = Boolean(state.busy);
  el('update').textContent = state.updateReady ? 'Обновить и перезапустить' : state.backendUpdatePending ? 'Завершить обновление' : 'Проверить обновления';
  el('onboarding').hidden = Boolean(state.configured || state.busy);
  el('operation').hidden = (!state.configured && !state.busy) || (state.phase === 'ready');
  el('dashboard').hidden = state.phase !== 'ready';
  if (state.phase === 'ready' && (previous.phase !== 'ready' || previous.backendSHA !== state.backendSHA || el('dashboard').getAttribute('src') !== state.url + '/')) el('dashboard').src = state.url + '/';
  if (state.phase === 'error') el('dashboard').removeAttribute('src');
  el('operation-title').textContent = state.phase === 'error' ? 'Запуск не завершён' : 'Подготовка приложения';
  el('operation-message').textContent = state.message || 'Проверяю готовность…';
  el('setup-error').hidden = state.configured || state.phase !== 'error';
  el('test-notice').hidden = state.deploymentAvailable !== false || state.configured;
  el('setup-error').textContent = state.message || '';
  el('error-actions').hidden = state.phase !== 'error';
  el('recover').hidden = !state.canRollback;
  document.querySelector('progress').hidden = state.phase === 'error';
  document.querySelectorAll('#setup-form input,#setup-form button').forEach(item => { item.disabled = Boolean(state.busy || state.deploymentAvailable === false); });
  el('import').disabled = Boolean(state.busy);
}
async function action(call) {
  try { await call(); }
  catch (error) {
    // Main process is authoritative about saved setup/ready state.
    try { render(await window.companion.state()); }
    catch { render({ ...current, phase: current.phase === 'ready' ? 'ready' : 'error', busy: false, message: error.message }); }
  }
}
el('setup-form').addEventListener('submit', event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  action(() => window.companion.configure(values));
  for (const field of ['db_password', 'secret_key']) event.target.elements[field].value = '';
});
el('import').addEventListener('click', () => action(() => window.companion.importInstallation()));
el('retry').addEventListener('click', () => action(() => window.companion.launch()));
el('recover').addEventListener('click', () => action(() => window.companion.recover()));
el('update').addEventListener('click', () => action(() => current.updateReady ? window.companion.installUpdate() : current.backendUpdatePending ? window.companion.launch() : window.companion.checkUpdate()));
window.companion.onState(render);
window.companion.state().then(render);
