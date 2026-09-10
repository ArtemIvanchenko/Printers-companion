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
    async function load(host, recordId) {
        if (!host) return;
        host.replaceChildren(make('p', 'Сопоставляем данные логов и модели…'));
        try {
            const response = await fetch(`/analysis/prints/${encodeURIComponent(recordId)}/log-insights`);
            if (!response.ok) throw new Error(`Ошибка загрузки (${response.status})`);
            const data = await response.json();
            if (!host.isConnected) return;
            host.replaceChildren();
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
            host.append(grid);
            host.append(make('p', 'Это диагностические наблюдения, а не заключение о браке. Общие датчики не определяют место дефекта по X/Y. Привязка среды по общей метке прожига приблизительна.', 'pc-empty'));
        } catch (error) {
            if (host.isConnected) host.replaceChildren(make('p', `${error.message}. Можно повторно открыть вкладку.`));
        }
    }
    return {load};
})();
