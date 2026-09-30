// ── Machine params ──
const _MP_FIELDS = ['hatch_speed_mm_s','contour_speed_mm_s','hatch_distance_mm','layer_thickness_mm','laser_count',
                    'recoat_time_ms','powder_cost_rub_per_kg','gas_cost_rub_per_atm','gas_atm_per_print',
                    'filter_cost_rub','filter_lifetime_hours','platform_cost_rub','build_area_cm2',
                    'time_correction_factor'];
const _MP_BASE_MATERIALS = ['steel','aluminum','titanium'];

function _renderMpMaterials(densities, hatches) {
    const names = [...new Set([..._MP_BASE_MATERIALS, ...Object.keys(densities), ...Object.keys(hatches)])];
    document.getElementById('mp-materials').innerHTML = names.map(m => `
        <tr data-material="${m}">
            <td style="padding:6px 10px;color:#e2e8f0;">${_materialRu(m)}</td>
            <td style="padding:4px 10px;"><input class="mp-input mp-dens" type="number" min="0" step="any" value="${densities[m] ?? ''}"></td>
            <td style="padding:4px 10px;"><input class="mp-input mp-hatch" type="number" min="0" step="any" value="${hatches[m] ?? ''}"></td>
        </tr>`).join('');
}

function addMpMaterial() {
    const input = document.getElementById('mp-new-material');
    const name = input.value.trim().toLowerCase();
    if (!name) return;
    if (!document.querySelector(`#mp-materials tr[data-material="${name}"]`)) {
        const row = document.createElement('tr');
        row.dataset.material = name;
        row.innerHTML = `<td style="padding:6px 10px;color:#e2e8f0;">${_materialRu(name)}</td>
            <td style="padding:4px 10px;"><input class="mp-input mp-dens" type="number" min="0" step="any"></td>
            <td style="padding:4px 10px;"><input class="mp-input mp-hatch" type="number" min="0" step="any"></td>`;
        document.getElementById('mp-materials').appendChild(row);
    }
    input.value = '';
}

function _collectMpMaterials() {
    const densities = {}, hatches = {};
    document.querySelectorAll('#mp-materials tr').forEach(tr => {
        const m  = tr.dataset.material;
        const dv = tr.querySelector('.mp-dens').value;
        const hv = tr.querySelector('.mp-hatch').value;
        if (dv !== '') densities[m] = Number(dv);
        if (hv !== '') hatches[m]   = Number(hv);
    });
    return { densities, hatches };
}

async function loadMachineParams() {
    try {
        const d = await fetch('/settings/machine').then(r => r.json());
        const p = d.params || {};
        _MP_FIELDS.forEach(f => {
            const el = document.getElementById('mp-' + f);
            if (el) el.value = p[f] ?? '';
        });
        _renderMpMaterials(p.material_densities || {}, p.hatch_speeds_by_mat || {});
        document.getElementById('mp-status').textContent = d.configured
            ? '✅ Параметры заданы' : '⚠️ Заполните параметры для расчёта времени и стоимости';
        loadCalibration();
    } catch (e) { /* форма остаётся пустой */ }
}

async function loadCalibration() {
    const el = document.getElementById('mp-calibration');
    try {
        const [d, mp] = await Promise.all([
            fetch('/prints/prediction-accuracy').then(r => r.json()),
            fetch('/settings/machine').then(r => r.json()),
        ]);
        const locked = !!(mp.params && mp.params.correction_locked);
        const byMat = (mp.params && mp.params.time_correction_by_mat) || {};
        if (!d.n_pairs) {
            el.innerHTML = `📐 Прогноз vs факт: пока нет пар «прогноз + привязанные логи».
                Свяжите карточку печати с логами — поправка по каждому материалу
                подстроится автоматически (нужно ≥${d.min_pairs_for_calibration} печатей на материал).`;
            return;
        }
        let html = `📐 Прогноз vs факт: <b style="color:#e2e8f0;">${d.n_pairs}</b> пар(ы). `;
        html += locked
            ? `🔒 Поправки зафиксированы вручную — автокалибровка отключена.
               <a onclick="setCorrectionLock(false)" style="color:#60a5fa;cursor:pointer;">включить авто</a>`
            : `Калибруется автоматически по каждому материалу.
               <a onclick="recalibrateNow()" style="color:#60a5fa;cursor:pointer;">пересчитать сейчас</a>
               · <a onclick="setCorrectionLock(true)" style="color:#9ca3af;cursor:pointer;">зафиксировать вручную</a>`;
        // Per-material breakdown: applied factor + recommendation + pair count
        const mats = new Set([...Object.keys(d.by_material || {}), ...Object.keys(byMat)]);
        if (mats.size) {
            html += `<div style="margin-top:8px;display:flex;flex-direction:column;gap:3px;">`;
            for (const m of mats) {
                const info = (d.by_material || {})[m] || {};
                const applied = byMat[m];
                const sugg = info.suggested_factor;
                const appliedTxt = applied != null
                    ? `применено <b style="color:#10b981;">×${applied}</b>` : 'применено ×1.00 (нет данных)';
                const suggTxt = sugg != null
                    ? ` · рекоменд. ×${sugg} по ${info.n_pairs} печ.`
                    : (info.n_pairs ? ` · ${info.n_pairs} печ. (нужно ≥${d.min_pairs_for_calibration})` : '');
                html += `<div style="font-size:12px;color:#a0aec0;">• ${_materialRu(m)}: ${appliedTxt}${suggTxt}</div>`;
            }
            html += `</div>`;
        }
        el.innerHTML = html;
    } catch (e) { el.innerHTML = ''; }
}

let _calibrationRequestActive = false;
async function recalibrateNow() {
    if (_calibrationRequestActive) {
        showToast('Калибровка уже в очереди или выполняется на этом ПК.', 'info');
        return;
    }
    _calibrationRequestActive = true;
    try {
        const response = await fetch('/prints/recalibrate', { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось поставить расчёт в очередь');
        if (data.contract_version === 2 && data.job_id) {
            showToast('Калибровка в очереди на этом ПК. Можно продолжать работу.', 'info');
            // A bounded UI wait does not cancel durable work. A slow or
            // offline workstation continues when its worker recovers.
            for (let attempt = 0; attempt < 60; attempt++) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const statusResponse = await fetch('/background-analysis/jobs/' + encodeURIComponent(data.job_id));
                if (!statusResponse.ok) throw new Error('Не удалось проверить расчёт. Он остаётся в фоновой очереди.');
                const job = await statusResponse.json();
                if (job.status === 'failed') throw new Error(job.error || 'Калибровка не завершилась');
                if (job.status !== 'done') continue;
                const result = job.result || {};
                showToast(result.locked ? 'Настройки зафиксированы — калибровка ничего не изменила.'
                    : result.status === 'no_machine_params' ? 'Сначала задайте параметры машины.'
                    : 'Калибровка завершена. Применены только допустимые результаты.', 'info');
                await Promise.all([loadCalibration(), loadMachineParams(), loadMachineAccuracy()]);
                return;
            }
            showToast('Расчёт продолжается в фоне. Результат появится после завершения.', 'info');
        } else {
            // Compatibility with a PC still running the synchronous API.
            await Promise.all([loadCalibration(), loadMachineParams(), loadMachineAccuracy()]);
        }
    } catch (error) {
        showToast(error.message || 'Не удалось запустить калибровку', 'error');
    } finally {
        _calibrationRequestActive = false;
    }
}

async function setCorrectionLock(locked) {
    try {
        const response = await fetch('/settings/machine', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ correction_locked: locked }),
        });
        if (!response.ok) throw new Error('Не удалось изменить фиксацию настроек');
        if (!locked) void recalibrateNow();
        loadCalibration(); loadMachineParams();
    } catch (error) { showToast(error.message, 'error'); }
}

// =========================================================
