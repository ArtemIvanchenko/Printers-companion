function _renderPcFiles() {
    const rec = _pcRecord;
    const files = rec.files || [];
    const fileUrl = file => `/prints/${encodeURIComponent(rec.record_id)}/files/${encodeURIComponent(file.file_id)}/download`;
    const rows = files.length ? files.map((f, index) => {
        const dl = fileUrl(f);
        const preview = (f.file_type === 'stl' || f.file_type === 'stl_supports')
            ? ` <a href="#" data-pc-preview="${index}" style="cursor:pointer;" title="3D-просмотр">👁</a>` : '';
        return `<div class="pc-kv">
            <span>${_FILE_ICON[f.file_type] || '📎'} <a href="${_esc(dl)}" style="color:var(--color-accent);">${_esc(f.file_name)}</a>${preview}</span>
            <span style="font-weight:400;">
                <span style="color:var(--text-muted);font-size:12px;">${(f.size_bytes / 1048576).toFixed(1)} МБ</span>
                <a href="#" data-pc-delete="${index}" style="cursor:pointer;color:var(--text-muted);margin-left:10px;" title="Удалить">✕</a>
            </span></div>`;
    }).join('') : '<div style="color:var(--text-muted);font-size:13px;">Файлов пока нет.</div>';

    const pane = document.getElementById('pcpane-files');
    pane.innerHTML = `
        <div class="pc-box" style="margin-bottom:16px;">
            <h4>Файлы печати (${files.length})</h4>
            ${rows}
        </div>
        <label class="quick-btn" style="cursor:pointer;">＋ Приложить файл
            <input id="pc-attach-files" type="file" multiple style="display:none"></label>
        ${rec.session_id ? '' : `<label class="quick-btn" style="cursor:pointer;margin-left:10px;">📋 Загрузить логи
            <input id="pc-attach-logs" type="file" multiple accept=".log,.zip" style="display:none"></label>`}`;
    // File names are data, never JavaScript source in an inline handler.
    pane.querySelectorAll('[data-pc-preview]').forEach(link => link.addEventListener('click', event => {
        event.preventDefault();
        const file = files[Number(link.dataset.pcPreview)];
        if (file) previewArchiveStl(fileUrl(file), file.file_name);
    }));
    pane.querySelectorAll('[data-pc-delete]').forEach(link => link.addEventListener('click', event => {
        event.preventDefault();
        const file = files[Number(link.dataset.pcDelete)];
        if (file) deleteArchiveFile(rec.record_id, file.file_id, file.file_name);
    }));
    document.getElementById('pc-attach-files')?.addEventListener('change', event => uploadFilesToCard(rec.record_id, event.target.files));
    document.getElementById('pc-attach-logs')?.addEventListener('change', event => uploadArchiveLogs(rec.record_id, event.target.files));
}

async function uploadFilesToCard(recordId, fileList) {
    if (!fileList || !fileList.length) return;
    for (const file of [...fileList]) await uploadArchiveFile(recordId, file, true);
    openPrintCard(recordId);
}

// Russian plurals need the last two digits, not a "< 5" shortcut:
// 174 → "слоя", 11 → "слоёв", 21 → "слой".
function _plural(n, one, few, many) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 14) return many;
    if (mod10 === 1) return one;
    if (mod10 >= 2 && mod10 <= 4) return few;
    return many;
}

function _pcUnlinkedTimeFacts(session) {
    const facts = [];
    // Legacy machine_min is only burn + pour. It excludes normal overhead
    // and minimum-cycle waiting, and may cover only part of the print.
    if (typeof session.machine_min === 'number' && Number.isFinite(session.machine_min) && session.machine_min >= 0) {
        facts.push(`${(session.machine_min / 60).toFixed(1)} ч прожига + нанесения (измеренная часть)`);
    }
    if (typeof session.duration_min === 'number' && Number.isFinite(session.duration_min) && session.duration_min >= 0) {
        facts.push(`${(session.duration_min / 60).toFixed(1)} ч по часам`);
    }
    // Do not display historical idle_min as a proven operator pause: older
    // payloads computed it by subtracting this subtotal from wall-clock time.
    return facts;
}

// Log sessions belonging to no print card. Loaded alongside the list
// because an unlinked session is work in progress, not a detail of the
// archive: it produces no cost, no predicted-vs-actual pair and no
// calibration input until it is claimed.
async function loadUnlinkedSessions() {
    const banner = document.getElementById('unlinked-banner');
    if (!banner) return;
    try {
        const [d, prints] = await Promise.all([
            fetch('/prints/unlinked-sessions').then(r => r.json()),
            fetch('/prints?limit=100').then(r => r.json()),
        ]);
        // Preparation runs are excluded from the count and the list.
        // The banner claims something needs doing and states the cost of
        // not doing it — neither is true for a pre-burn run, which has
        // no print to cost and no estimate to check. Listing them made
        // the banner argue with itself ("не привязано" next to
        // "привязка обычно не нужна") and buried the rows that matter.
        const realPrints = (d.items || []).filter(s => s.is_print);
        const prep = (d.items || []).filter(s => !s.is_print);
        if (!realPrints.length) { banner.style.display = 'none'; return; }
        banner.style.display = 'block';

        document.getElementById('unlinked-title').textContent =
            `⚠ ${realPrints.length} ${_plural(realPrints.length, 'печать', 'печати', 'печатей')} с логами без карточки`;

        // Only unlinked cards can receive a session, so the picker is
        // built once from them rather than per row.
        const free = (prints.items || []).filter(r => !r.session_id);
        const options = free.map(r => `<option value="${_esc(r.record_id)}">${_esc(r.name)}</option>`).join('');

        document.getElementById('unlinked-rows').innerHTML = realPrints.map(s => {
            const when = s.start_ts ? new Date(s.start_ts).toLocaleDateString('ru') : '—';
            const facts = _pcUnlinkedTimeFacts(s);
            if (s.layers)                    facts.push(`${s.layers} ${_plural(s.layers, 'слой', 'слоя', 'слоёв')}`);

            // Creating a card from the log is always offered, not just as
            // a fallback: most logs arrive without one waiting for them,
            // and without this the operator dead-ends on "no free cards"
            // with nothing to click.
            const create = `<button class="btn-link" style="background:transparent;border:1px solid var(--status-warning);color:#fde68a;"
                onclick="createRecordFromSession('${s.session_id}', '${when}')"
                title="Создать карточку печати с датой из лога и сразу привязать">+ карточку</button>`;
            const action = free.length
                ? `<select id="link-target-${s.session_id}">${options}</select>
                   <button class="btn-link" onclick="linkSessionToRecord('${s.session_id}')">Привязать</button>${create}`
                : create;
            return `<div class="unlinked-row">
                <span class="when">${when}</span>
                <span class="facts">${facts.join(' · ') || 'нет данных'}</span>
                ${action}
            </div>`;
        }).join('')
        // Preparation runs stay reachable but out of the way: they are
        // not a task, and a count nobody has to act on is noise.
        + (prep.length ? `<div style="margin-top:10px;font-size:12px;color:#fde68a;opacity:.7;">
                Кроме них — ${prep.length} ${_plural(prep.length, 'подготовительный прогон', 'подготовительных прогона', 'подготовительных прогонов')}
                (прожиг, прогрев). Карточка для них обычно не нужна.
           </div>` : '');
    } catch (e) {
        banner.style.display = 'none';
    }
}

// Start a card from whatever arrived first — STL, a Magics layout, a
// photo, a spec. Printer logs go through the ingest path instead, since
// they create a session that then needs claiming.
async function createRecordFromFiles(fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;

    const logs = files.filter(f => /\.(log|zip)$/i.test(f.name));
    const rest = files.filter(f => !/\.(log|zip)$/i.test(f.name));

    // The name comes from the first non-log file; a date inside it is
    // picked up server-side and becomes the print date.
    const source = (rest[0] || logs[0]).name;
    const name = source.replace(/\.[^.]+$/, '').replace(/^s_/, '').trim() || 'Без названия';
    try {
        const created = await fetch('/prints', {
            method: 'POST',
            headers: _jsonHeaders(),
            body: JSON.stringify({ name }),
        }).then(async r => {
            if (!r.ok) throw new Error((await r.json()).detail || r.status);
            return r.json();
        });

        for (const file of rest) await uploadArchiveFile(created.record_id, file, true);
        if (logs.length) await uploadArchiveLogs(created.record_id, logs);

        openPrintCard(created.record_id);
    } catch (e) { showToast('Не удалось создать карточку: ' + e.message, 'error'); }
}

// Create a card for a log that has none and claim the session in one go.
// The name is a placeholder the operator renames — what matters is that
// the session stops being orphaned, so its time and cost start counting.
async function createRecordFromSession(sessionId, whenLabel) {
    try {
        const created = await fetch('/prints', {
            method: 'POST',
            headers: _jsonHeaders(),
            body: JSON.stringify({ name: `Печать ${whenLabel}` }),
        }).then(async r => {
            if (!r.ok) throw new Error((await r.json()).detail || r.status);
            return r.json();
        });
        const linked = await fetch(`/prints/${created.record_id}`, {
            method: 'PATCH',
            headers: _jsonHeaders(),
            body: JSON.stringify({ session_id: sessionId, expected_revision: created.revision }),
        });
        if (!linked.ok) throw new Error((await linked.json()).detail || linked.status);
        loadArchive(_archiveSkip);
    } catch (e) { showToast('Не удалось создать карточку: ' + e.message, 'error'); }
}

async function linkSessionToRecord(sessionId) {
    const recordId = document.getElementById(`link-target-${sessionId}`).value;
    if (!recordId) return;
    try {
        const record = await fetch(`/prints/${recordId}`).then(async r => {
            if (!r.ok) throw new Error((await r.json()).detail || r.status);
            return r.json();
        });
        const r = await fetch(`/prints/${recordId}`, {
            method: 'PATCH',
            headers: _jsonHeaders(),
            body: JSON.stringify({ session_id: sessionId, expected_revision: record.revision }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        loadArchive(_archiveSkip);
    } catch (e) { showToast('Ошибка привязки: ' + e.message, 'error'); }
}

async function loadArchive(skip = 0) {
    _archiveSkip = skip;
    loadUnlinkedSessions();
    const body  = document.getElementById('archive-table-body');
    const empty = document.getElementById('archive-empty');
    const wrap  = document.getElementById('archive-list-wrap');
    const filters = _archiveFilters();
    try {
        const d = await fetch(`/prints?skip=${skip}&limit=${_ARCHIVE_PAGE_SIZE}${filters}`).then(r => r.json());
        if (!d.total && !filters) {
            empty.style.display = 'block'; wrap.style.display = 'none';
            return;
        }
        empty.style.display = 'none'; wrap.style.display = 'block';
        body.innerHTML = d.total === 0
            ? '<tr><td colspan="8" style="color:#6b7280;padding:14px;">Ничего не найдено</td></tr>'
            : d.items.map((rec, recordIndex) => {
            const rawDate = rec.printed_at || rec.created_at;
            const date = rawDate ? new Date(rawDate).toLocaleDateString('ru') : '—';
            const dateHint = rec.printed_at ? '' : ' <span style="color:var(--border-hover);" title="Дата создания карточки — дата печати неизвестна">≈</span>';
            const s = rec.summary || {};
            const timing = _pcTimeMetrics(null, rec.metadata_json?.prediction, s);
            const timeScope = timing.scope === 'burn_plus_pour' ? 'прожиг + нанесение' : 'нормальный цикл';

            // Plan and outcome sit side by side: the whole point of the
            // list is answering "did the estimate hold?" without opening
            // the record.
            const plan = timing.predictedHours != null
                ? `<span title="${timeScope}">${timing.predictedHours} ч</span>${s.predicted_cost_rub != null
                    ? `<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${Math.round(s.predicted_cost_rub).toLocaleString('ru')} ₽</div>` : ''}`
                : '<span style="color:var(--text-muted);">—</span>';

            let fact = '<span style="color:var(--text-muted);">—</span>';
            if (timing.actualHours != null) {
                fact = `<span title="${timeScope}; допуск проверен сервером">${timing.actualHours} ч</span>`;
            }

            let accuracy = '<span style="color:var(--text-muted);">—</span>';
            if (timing.errorPct != null) {
                const col = Math.abs(timing.errorPct) <= 10 ? 'var(--status-success)'
                          : Math.abs(timing.errorPct) <= 25 ? 'var(--status-warning)' : 'var(--status-danger)';
                accuracy = `<span style="color:${col};font-weight:600;">${timing.errorPct > 0 ? '+' : ''}${timing.errorPct}%</span>`;
            }

            const session = rec.session_id
                ? `<span style="color:var(--status-success);" title="${_esc(rec.session_id)}">✓ привязаны</span>`
                : `<span id="session-cell-${_esc(rec.record_id)}"><a href="#" data-ar-action="candidates" data-ar-record="${recordIndex}"
                    style="color:var(--color-accent);cursor:pointer;font-size:12px;" title="Найти сессии логов рядом с датой печати">🔗 привязать</a></span>`;
            const thickness = rec.layer_thickness_mm
                ? `<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${_esc(rec.layer_thickness_mm)} мм</div>` : '';
            const chips = rec.files.map((f, fileIndex) => {
                const dl = `/prints/${encodeURIComponent(rec.record_id)}/files/${encodeURIComponent(f.file_id)}/download`;
                const indices = `data-ar-record="${recordIndex}" data-ar-file="${fileIndex}"`;
                const preview = (f.file_type === 'stl' || f.file_type === 'stl_supports')
                    ? ` <a href="#" data-ar-action="preview" ${indices} title="3D-просмотр">👁</a>` : '';
                return `<span class="file-chip">${_FILE_ICON[f.file_type] || '📎'} <a href="${_esc(dl)}" title="Скачать">${_esc(f.file_name)}</a>${preview} <a href="#" data-ar-action="delete-file" ${indices} title="Удалить файл" style="color:#6b7280;">✕</a></span>`;
            }).join('');
            const upload = `<label class="file-chip" style="cursor:pointer;" title="Прикрепить файл (STL, Magics, фото, документ)">＋
                <input type="file" style="display:none" data-ar-action="upload-file" data-ar-record="${recordIndex}"></label>`;
            const logsUpload = rec.session_id ? '' : `<label class="file-chip" style="cursor:pointer;" title="Загрузить логи печати — сессия привяжется автоматически">📋 логи
                <input type="file" multiple accept=".log,.zip" style="display:none" data-ar-action="upload-logs" data-ar-record="${recordIndex}"></label>`;
            return `<tr class="archive-row">
                <td>
                    <div style="color:var(--text-body);font-weight:600;">
                        <a href="#" data-ar-action="open" data-ar-record="${recordIndex}" style="color:inherit;cursor:pointer;text-decoration:none;border-bottom:1px dotted var(--border-hover);"
                           title="Открыть карточку печати">${_esc(rec.name)}</a>${rec.notes ? ` <span title="${_esc(rec.notes)}" style="cursor:help;color:var(--border-hover);">💬</span>` : ''}</div>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${date}${dateHint}${s.layers ? ` · ${_esc(s.layers)} ${_plural(s.layers, 'слой', 'слоя', 'слоёв')}` : ''} · ${_statusBadge(rec.status)}</div>
                </td>
                <td>${_esc(_materialRu(rec.material))}${thickness}</td>
                <td>${plan}</td>
                <td>${fact}</td>
                <td>${accuracy}</td>
                <td>${session}</td>
                <td>${chips}${upload}${logsUpload}</td>
                <td style="white-space:nowrap;">${rec.files.some(f => f.file_type === 'stl')
                    ? `<a href="#" data-ar-action="estimate" data-ar-record="${recordIndex}" title="Рассчитать и сохранить прогноз времени/стоимости" style="cursor:pointer;margin-right:8px;">📐</a>` : ''}<a href="#" data-ar-action="delete-record" data-ar-record="${recordIndex}" title="Удалить карточку" style="cursor:pointer;color:#6b7280;">🗑</a></td>
            </tr>`;
        }).join('');
        _bindArchiveRowActions(body, d.items);

        const pager = document.getElementById('archive-pagination');
        const pages = Math.ceil(d.total / _ARCHIVE_PAGE_SIZE);
        pager.innerHTML = pages <= 1 ? '' : Array.from({ length: pages }, (_, i) => {
            const active = i * _ARCHIVE_PAGE_SIZE === skip;
            return `<a onclick="loadArchive(${i * _ARCHIVE_PAGE_SIZE})"
                style="cursor:pointer;padding:4px 10px;border-radius:6px;font-size:13px;
                       ${active ? 'background:#2d3748;color:#e2e8f0;' : 'color:#60a5fa;'}">${i + 1}</a>`;
        }).join('');
    } catch (e) {
        body.innerHTML = `<tr><td colspan="8" style="color:#ef4444;padding:14px;">Ошибка загрузки архива: ${_esc(e.message)}</td></tr>`;
        empty.style.display = 'none'; wrap.style.display = 'block';
    }
}

function _bindArchiveRowActions(body, records) {
    body.querySelectorAll('[data-ar-action]').forEach(control => {
        const action = control.dataset.arAction;
        control.addEventListener(action.startsWith('upload-') ? 'change' : 'click', event => {
            event.preventDefault();
            const record = records[Number(control.dataset.arRecord)];
            if (!record) return;
            const file = (record.files || [])[Number(control.dataset.arFile)];
            if (action === 'open') openPrintCard(record.record_id);
            else if (action === 'candidates') showSessionCandidates(record.record_id);
            else if (action === 'estimate') estimateRecord(record.record_id);
            else if (action === 'delete-record') deletePrintRecord(record.record_id, record.name);
            else if (action === 'delete-file' && file) deleteArchiveFile(record.record_id, file.file_id, file.file_name);
            else if (action === 'preview' && file) previewArchiveStl(
                `/prints/${encodeURIComponent(record.record_id)}/files/${encodeURIComponent(file.file_id)}/download`, file.file_name);
            else if (action === 'upload-file') uploadArchiveFile(record.record_id, event.target.files[0]);
            else if (action === 'upload-logs') uploadArchiveLogs(record.record_id, event.target.files);
        });
    });
}

function _guessFileType(name) {
    const n = name.toLowerCase();
    if (n.endsWith('.stl'))    return n.startsWith('s_') ? 'stl_supports' : 'stl';
    if (n.endsWith('.magics') || n.endsWith('.mgx')) return 'magics';
    if (/\.(png|jpe?g|gif|webp|heic)$/.test(n))      return 'photo';
    return 'doc';
}

async function uploadArchiveLogs(recordId, fileList, strict = false) {
    if (!fileList || !fileList.length) return;
    const files = [...fileList];
    const fd = new FormData();
    files.forEach(file => fd.append('files', file));
    let savedCount = 0, skippedCount = 0;
    let result;
    try {
        const r = await fetch(`/prints/${recordId}/import-logs`, { method: 'POST', body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.status);
        savedCount = (d.saved || []).length;
        skippedCount = (d.skipped || []).length;
        result = d;
        if (strict && skippedCount) throw new Error(d.skipped.map(row => `${row.name}: ${row.reason}`).join('; '));
    } catch (e) {
        showToast(`Ошибка загрузки набора из ${files.length} ${_plural(files.length, 'файла', 'файлов', 'файлов')}: ${e.message || e}`, 'error');
        if (strict) throw e;
        return;
    }
    const parts = [];
    if (savedCount) parts.push(`Загружено файлов: ${savedCount}`);
    if (skippedCount) parts.push(`пропущено: ${skippedCount}`);
    const summary = parts.length ? parts.join(', ') : 'Файлы не загружены';
    showToast(summary + (savedCount ? '. Подтвердите импорт в верхней панели.' : '.'), savedCount ? 'success' : 'info');
    if (savedCount) _pollRecordLink(recordId);
    return result;
}

// Привязка идёт в фоне — опрашиваем карточку до появления session_id (≤60 с)
async function _pollRecordLink(recordId, attempt = 0) {
    if (attempt >= 12) { loadArchive(_archiveSkip); return; }
    await new Promise(res => setTimeout(res, 5000));
    try {
        const rec = await fetch(`/prints/${recordId}`).then(r => r.json());
        if (rec.session_id) { loadArchive(_archiveSkip); return; }
    } catch (e) { /* продолжаем опрос */ }
    _pollRecordLink(recordId, attempt + 1);
}

async function showSessionCandidates(recordId) {
    const cell = document.getElementById(`session-cell-${recordId}`);
    cell.innerHTML = '<span style="color:#6b7280;font-size:12px;">поиск…</span>';
    try {
        const d = await fetch(`/prints/${recordId}/session-candidates`).then(r => r.json());
        if (!d.candidates.length) {
            cell.innerHTML = '<span style="color:#6b7280;font-size:12px;" title="Нет непривязанных сессий рядом с датой печати — загрузите логи">нет кандидатов</span>';
            return;
        }
        const opts = d.candidates.map(c => {
            const start = new Date(c.start_ts).toLocaleString('ru');
            const dur = c.duration_min != null ? ` · ${(c.duration_min / 60).toFixed(1)} ч` : '';
            return `<option value="${c.session_id}">${start}${dur}</option>`;
        }).join('');
        cell.innerHTML = `<select id="cand-${recordId}" class="mp-input" style="max-width:200px;display:inline-block;font-size:12px;padding:4px 6px;">${opts}</select>
            <a onclick="confirmSessionLink('${recordId}')" style="color:#10b981;cursor:pointer;margin-left:6px;" title="Привязать">✓</a>`;
    } catch (e) {
        cell.innerHTML = '<span style="color:#ef4444;font-size:12px;">ошибка</span>';
    }
}

async function confirmSessionLink(recordId) {
    const sessionId = document.getElementById(`cand-${recordId}`).value;
    try {
        const record = await fetch(`/prints/${recordId}`).then(async r => {
            if (!r.ok) throw new Error((await r.json()).detail || r.status);
            return r.json();
        });
        const r = await fetch(`/prints/${recordId}`, {
            method: 'PATCH',
            headers: _jsonHeaders(),
            body: JSON.stringify({ session_id: sessionId, expected_revision: record.revision }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        loadArchive(_archiveSkip);
    } catch (e) { showToast('Ошибка привязки: ' + e.message, 'error'); }
}

async function uploadArchiveFile(recordId, file, skipReload = false) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('file_type', _guessFileType(file.name));
    try {
        const r = await fetch(`/prints/${recordId}/files`, { method: 'POST', body: fd });
        const result = await r.json();
        if (!r.ok) throw new Error(result.detail || r.status);
        if (result.queued) {
            showToast(result.message || 'Файл сохранён локально и будет отправлен на NAS автоматически.', 'success');
        }
        // The card uploads several files in a row and refreshes once at
        // the end — reloading per file would fight the loop.
        if (!skipReload) loadArchive(_archiveSkip);
        return result;
    } catch (e) {
        showToast('Ошибка загрузки файла: ' + e.message, 'error');
        if (skipReload) throw e;
    }
}

async function previewArchiveStl(url, name) {
    document.getElementById('viewer-title').textContent = name;
    document.getElementById('viewer-meta').innerHTML = '';
    document.getElementById('viewer-overlay').classList.add('open');
    const wrap = document.getElementById('viewer-canvas-wrap');
    wrap.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#6b7280;">Загрузка…</div>';
    try {
        const buf = await fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); });
        wrap.innerHTML = '<canvas id="viewer-canvas" style="width:100%;height:100%;display:block;"></canvas>';
        _initThreeViewer(buf);
    } catch (e) {
        wrap.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#ef4444;">Не удалось загрузить файл: ${_esc(e.message)}</div>`;
    }
}
