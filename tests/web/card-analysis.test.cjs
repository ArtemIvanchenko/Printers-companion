'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function render(report) {
    const pane = { innerHTML: '', addEventListener() {} };
    const context = vm.createContext({ document: { getElementById: () => pane }, window: {},
        _pcRecord: { record_id: 'current-card', metadata_json: {}, summary: {}, quality_outcomes: [] },
        _pcSession: null, _pcOperatorReport: report,
        _pcTimeMetrics: () => ({ actualHours: null, predictedHours: null, errorPct: null }),
        _plural: (_, one, two, many) => many,
        _esc: value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]),
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/card-analysis.js'), 'utf8'), context);
    context._renderPcOutcome();
    return pane.innerHTML;
}

for (const missing of [null, {}, { state: {}, deviations: [], recommendations: [] }]) {
    test(`missing state-bearing report is unknown, not a clean/no-action conclusion: ${JSON.stringify(missing)}`, () => {
        const html = render(missing);
        assert.match(html, /Анализ недоступен/);
        assert.doesNotMatch(html, /Существенных отклонений не обнаружено/);
        assert.doesNotMatch(html, /Дополнительных действий не требуется/);
        assert.doesNotMatch(html, /var\(--status-success\)/);
        assert.match(html, /Заключение анализа не загружено/);
        assert.match(html, /Результат контроля детали/);
    });
}

test('loaded current clean report keeps its explicit conclusion and empty-deviation meaning', () => {
    const html = render({ print_record_id: 'current-card',
        state: { code: 'no_significant_deviations', label_ru: 'Подтверждённое заключение текущей печати', severity: 'info' },
        confidence: { level_ru: 'высокая', score: .9 }, deviations: [], recommendations: [] });
    assert.match(html, /Подтверждённое заключение текущей печати/);
    assert.match(html, /Существенных отклонений не обнаружено/);
    assert.doesNotMatch(html, /Что проверить в оборудовании/);
    assert.match(html, /var\(--status-success\)/);
    assert.doesNotMatch(html, /Анализ недоступен/);
});

test('loaded current report preserves deviations, severity, recommendations and escaping', () => {
    const html = render({ print_record_id: 'current-card',
        state: { code: 'attention_required', label_ru: 'Требуется проверка', severity: 'high' },
        confidence: { level_ru: 'низкая', score: .3 },
        deviations: [{ severity: 'high', title: '<O2>', detail: 'Проверенный факт' }],
        recommendations: ['Проверить исходные измерения'] });
    assert.match(html, /var\(--status-danger\)/);
    assert.match(html, /&lt;O2&gt;/);
    assert.match(html, /Проверенный факт/);
    assert.match(html, /Проверить исходные измерения/);
    assert.doesNotMatch(html, /Существенных отклонений не обнаружено/);
    assert.doesNotMatch(html, /Анализ недоступен/);
});
