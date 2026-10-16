const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the actual registered callbacks. This is not a browser/keyboard
// default-action emulator; nested key events and button clicks are independent.
function harness() {
    const opened = []; let created = 0;
    function element() {
        const classes = new Set(); let html = ''; let buttons = [];
        return {
            listeners: {}, classList: {add(name) {classes.add(name);}}, setAttribute() {},
            addEventListener(name, callback) {this.listeners[name] = callback;},
            querySelectorAll(selector) {return selector === '.home-card-action' ? buttons : [];},
            get innerHTML() {return html;},
            set innerHTML(value) {
                html = value;
                buttons = [...value.matchAll(/<button class="home-card-action[^>]*>([^<]*)<\/button>/g)]
                    .map(match => Object.assign(element(), {textContent: match[1]}));
            },
        };
    }
    const context = vm.createContext({
        document: {createElement: element, querySelectorAll: () => []},
        _esc: value => String(value ?? ''),
        openPrintCard(recordId, tab = 'what') {opened.push({recordId, tab});},
        openNewPrintRecord() {created++;},
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../web_assets/dashboard/catalog.js'), 'utf8'), context);
    function card(linked, fields = {}) {
        return context._homePrintCard({record_id: 'existing-card', name: 'Existing card',
            session_id: linked ? 'existing-session' : null, files: [], ...fields}, 0);
    }
    function click(button) {
        let stopped = false;
        button.listeners.click({target: button, stopPropagation() {stopped = true;}});
        assert.equal(stopped, true);
    }
    function key(card, target, value) {
        let prevented = false;
        card.listeners.keydown({key: value, target, currentTarget: card, preventDefault() {prevented = true;}});
        return prevented;
    }
    return {card, click, key, opened, created: () => created};
}

for (const [index, label, tab] of [[0, 'Контроль', 'outcome'], [1, 'Графики', 'process'], [2, 'Модель', 'what']]) {
    test(`linked catalog action ${label} opens its existing ${tab} tab`, () => {
        const h = harness(), card = h.card(true);
        const button = card.querySelectorAll('.home-card-action')[index];
        assert.equal(button.textContent, label);
        h.click(button);
        assert.deepEqual(h.opened, [{recordId: 'existing-card', tab}]);
        assert.equal(h.created(), 0);
    });
}

for (const [index, label, tab] of [[0, 'Приложить логи', 'files'], [1, 'Открыть', 'what']]) {
    test(`unlinked catalog action ${label} uses the existing card, not new-record creation`, () => {
        const h = harness(), card = h.card(false);
        const button = card.querySelectorAll('.home-card-action')[index];
        assert.equal(button.textContent, label);
        h.click(button);
        assert.deepEqual(h.opened, [{recordId: 'existing-card', tab}]);
        assert.equal(h.created(), 0);
    });
}

for (const key of ['Enter', ' ']) {
    test(`catalog article ignores ${JSON.stringify(key)} from a nested action button`, () => {
        const h = harness(), card = h.card(true);
        const button = card.querySelectorAll('.home-card-action')[1];
        assert.equal(h.key(card, button, key), false);
        assert.deepEqual(h.opened, []);
        h.click(button);
        assert.deepEqual(h.opened, [{recordId: 'existing-card', tab: 'process'}]);
    });
    test(`focused catalog article still opens once with ${JSON.stringify(key)}`, () => {
        const h = harness(), card = h.card(true);
        assert.equal(h.key(card, card, key), true);
        assert.deepEqual(h.opened, [{recordId: 'existing-card', tab: 'what'}]);
    });
}

test('catalog article click keeps the default what route', () => {
    const h = harness(), card = h.card(true);
    card.listeners.click({target: card});
    assert.deepEqual(h.opened, [{recordId: 'existing-card', tab: 'what'}]);
});

test('catalog measured time and layers remain visible when the saved prediction is stale', () => {
    const h = harness();
    const card = h.card(true, {prediction_is_current: false,
        metadata_json: {prediction: {print_hours: 8, layer_count: 123}},
        summary: {actual_hours: 12, layers: 321}});
    assert.match(card.innerHTML, /Время: 12:00/);
    assert.match(card.innerHTML, /Слоёв: 321/);
    assert.doesNotMatch(card.innerHTML, /Слоёв: 123/);
});
