/* Read-only diagnostic panel. All data is rendered as text, never HTML. */
window.PrinterLogInsights = (() => {
    const number = (v, digits = 1) => Number.isFinite(v) ? v.toLocaleString('ru-RU', {maximumFractionDigits: digits}) : 'нет данных';
    const metricNames = {burn_ms: 'Прожиг', pour_ms: 'Нанесение порошка', make_layer_ms: 'Фактический цикл'};
    const make = (tag, text, className) => {
        const el = document.createElement(tag);
        if (text != null) el.textContent = text;
        if (className) el.className = className;
        return el;
    };
    function section(host, title, rows, empty) {
        const box = make('section', null, 'pc-box');
        box.append(make('h4', title));
        for (const text of rows.length ? rows : [empty || 'Недостаточно данных.']) {
            const line = make('p', text);
            line.style.cssText = 'font-size:12px;line-height:1.6;margin:6px 0;color:var(--text-muted)';
            box.append(line);
        }
        host.append(box);
    }
    function inspectionPanel(host, data, geometry) {
        const mapped = data.inspection_map?.items || [];
        const fallback = geometry.items || [];
        const rows = mapped.length ? mapped : fallback;
        const section = make('section', null, 'pc-box');
        section.append(make('h3', 'Где проверить деталь'));
        if (!rows.length) {
            section.append(make('p', 'Высоты для проверки пока не определены. Это не подтверждает отсутствие дефектов.', 'pc-empty-state'));
            if (data.reason_ru) section.append(make('p', data.reason_ru, 'pc-source-note'));
        } else {
            section.append(make('p', 'Проверьте отмеченные участки и сохраните результат контроля.', 'pc-source-note'));
            const list = make('div', null, 'pc-inspection-list');
            let more;
            rows.forEach((item, index) => {
                if (index === 6) {
                    more = make('details', null, 'pc-disclosure');
                    more.append(make('summary', `Остальные участки · ${rows.length - 6}`));
                    list.append(more);
                }
                const row = make('div', null, 'pc-inspection-item');
                const precise = mapped.length || item.mapping_precision === 'exact_layer';
                const location = item.layer_range
                    ? `Слои ${item.layer_range.join('–')}` : `${precise ? 'Слой' : 'Около слоя'} ${item.layer}`;
                row.append(make('strong', location, 'pc-inspection-location'));
                let height = 'Высота не подтверждена';
                if (mapped.length && item.z_range_mm?.every(Number.isFinite)) {
                    height = `Z ${number(item.z_range_mm[0], 3)}–${number(item.z_range_mm[1], 3)} мм`;
                } else if (!mapped.length && Number.isFinite(item.height_mm)) {
                    height = `${number(item.height_mm, 3)} мм от начала модели`;
                    if (!geometry.geometry_confidence?.build_origin_confirmed) height += ' · начало печати не подтверждено';
                }
                row.append(make('span', height, 'pc-source-note'));
                const observations = item.observations || [];
                for (const observation of observations) {
                    const text = observation.kind === 'scan_residual'
                        ? `Время прожига отличается от расчёта на ${number(observation.residual_seconds)} с`
                        : `${typeof sigLabel === 'function' ? sigLabel(observation.signal) : observation.signal}: превышение ${number(observation.exceedance_seconds)} с`;
                    row.append(make('span', text, 'pc-inspection-signal'));
                }
                if (!observations.length && item.detail) row.append(make('span', item.detail, 'pc-inspection-signal'));
                const bodies = (item.active_bodies || []).map(body => body.name).filter(Boolean);
                if (bodies.length) row.append(make('span', 'Возможные детали: ' + bodies.join(', '), 'pc-source-note'));
                (more || list).append(row);
            });
            section.append(list);
            section.append(make('p', 'Это участки для проверки, не обнаруженные дефекты. Общие датчики не определяют место по X/Y.', 'pc-source-note'));
            if (data.inspection_map?.sample_count > mapped.length) section.append(make('p',
                `Источник содержит ${data.inspection_map.sample_count} участков; в этом ответе доступны ${mapped.length}.`, 'pc-source-note'));
        }
        host.append(section);
    }
    async function load(host, recordId, geometry = {}) {
        if (!host || !host.isConnected || host.dataset.loaded) return;
        // Reuse both pending and successful loads for this card's DOM host.
        host.dataset.loaded = 'true';
        host.replaceChildren(make('p', 'Сопоставляем данные логов и модели…'));
        try {
            const response = await fetch(`/analysis/prints/${encodeURIComponent(recordId)}/log-insights`);
            if (!response.ok) throw new Error(`Ошибка загрузки (${response.status})`);
            const data = await response.json();
            if (!host.isConnected) return;
            host.replaceChildren();
            inspectionPanel(host, data, geometry);
            const grid = make('div', null, 'pc-grid');
            grid.style.gap = '16px';
            const env = data.environment || {};
            section(grid, 'Среда во время прожига', (env.metrics || []).map(m =>
                `${m.name_ru}: превышение ${number(m.exceedance_seconds)} с; интеграл ${number(m.excess_integral)} ${m.integral_unit}; покрытие ${number(m.coverage_ratio == null ? null : m.coverage_ratio*100)}%. ${m.threshold?.confirmed ? 'Подтверждённый порог.' : 'Порог профиля предварительный.'}`), data.reason_ru);
            section(grid, 'Восстановление после остановок', (data.recovery?.items || []).slice(0, 5).map(r =>
                `${r.resume_timestamp}: ${r.recovery_seconds == null ? 'устойчивость не установлена' : `устойчивые показания через ${number(r.recovery_seconds)} с`}; отсчётов ${r.observed_samples}.`).concat(
                (data.recovery?.layer_comparison?.items || []).slice(0, 5).flatMap(r => Object.entries(r.metrics).map(([k,v]) =>
                    `${metricNames[k]} первых слоёв после возобновления: ${number(v.change_pct)}% к предыдущим; влияние геометрии не исключено.`))
            ), 'Нет подтверждённых возобновлений с достаточной телеметрией.');
            const residual = data.geometry_residuals || {};
            section(grid, 'Прожиг с учётом геометрии', residual.sample_count ? [
                `Сравнено слоёв: ${residual.sample_count}; медианное отличие от расчёта ${number(residual.median_bias_pct)}%; необычных слоёв ${residual.atypical_layer_count}.`,
                ...(residual.items || []).filter(r => r.atypical).slice(0, 5).map(r => `Слой ${r.layer}: ${number(r.observed_seconds)} с при расчёте ${number(r.expected_seconds)} с.`),
                residual.in_sample ? 'Эта печать входила в обучение: это не проверка точности модели.' : 'Отклонение расчёта не доказывает дефект.'
            ] : [], residual.reason_ru);
            section(grid, 'Повторяемость компоновки', (data.repeatability?.items || []).slice(0, 5).map(r =>
                `${r.reference_session_id}: общих слоёв ${r.common_layers}, покрытие ${number(r.coverage_ratio*100)}%. ` + Object.entries(r.metrics).map(([k,v]) => `${metricNames[k]}: ${number(v.median_change_pct)}%`).join('; ') + ' ' +
                (r.environment_changes || []).map(m => `${m.name_ru}: изменение среднего ${number(m.mean_difference, 3)} ${m.unit}`).join('; ')), data.repeatability?.reason_ru || 'Подтверждённых повторов этой компоновки и режима пока нет.');
            const accounting = data.normal_time_reference?.source === 'calibrated' ? data.normal_time_reference : data.time_accounting;
            section(grid, 'Из чего сложилось время', (accounting?.components || []).map(c => `${c.name_ru}: ${number(c.seconds/60)} мин.`).concat(accounting ? [
                accounting.scope === 'validated_unique_layers_only' ? 'Разбивка только однозначных слоёв; повторы исключены.' : 'Разбивка всех различимых записанных попыток.',
                `Явные паузы: ${number((data.time_accounting?.explicit_pause_seconds ?? null) == null ? null : data.time_accounting.explicit_pause_seconds/60)} мин. Не добавляются в прогноз.`,
                `Время повторных попыток: ${number(data.time_accounting?.repeat_attempt_seconds == null ? null : data.time_accounting.repeat_attempt_seconds/60)} мин; оно уже входит в фактические циклы.`
            ] : []));
            section(grid, 'Участки для контроля', (data.inspection_map?.items || []).slice(0, 10).map(r =>
                `Слой ${r.layer}; ${r.z_range_mm ? `Z ${number(r.z_range_mm[0], 3)}–${number(r.z_range_mm[1], 3)} мм` : 'начало Z не подтверждено'}; возможные тела: ${(r.active_bodies || []).map(b => b.name).join(', ') || 'не определены'}.`), 'Нет участков с наблюдениями для привязки.');
            const details = make('details', null, 'pc-disclosure');
            details.append(make('summary', 'Источники и подробные расчёты'));
            details.append(grid);
            details.append(make('p', 'Привязка среды по общей метке прожига приблизительна; расчётное отклонение не устанавливает причину дефекта.', 'pc-source-note'));
            host.append(details);
        } catch (error) {
            delete host.dataset.loaded;
            if (host.isConnected) {
                const retry = make('button', 'Повторить', 'quick-btn');
                retry.type = 'button';
                retry.addEventListener('click', () => load(host, recordId, geometry));
                host.replaceChildren(make('p', error.message), retry);
            }
        }
    }
    return {load};
})();
