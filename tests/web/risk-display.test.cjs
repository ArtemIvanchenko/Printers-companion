const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function render(session) {
    const nodes = new Map();
    const context = vm.createContext({
        dashboardBootstrap: {signal_labels:{}},
        document: {getElementById(id) {
            if (!nodes.has(id)) nodes.set(id, {innerHTML:'',textContent:''});
            return nodes.get(id);
        }},
        _esc: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
        fetch: async () => ({json: async () => ({sessions:[session],n_labeled:0})}),
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../../web_assets/dashboard/patterns.js'),'utf8'),context);
    await context.loadDefectRisk();
    return nodes.get('defect-risk-list').innerHTML;
}

test('unknown risk has no green zero-percent claim',async()=>{
    for (const risk of [null,undefined,NaN,Infinity,true,-1,2]) {
        const html = await render({session_id:'test',risk,grade:'low',method:'heuristic'});
        assert.match(html,/>—<\/div>/);
        assert.match(html,/нет измерений/);
        assert(!html.includes('>0%</div>'));
        assert(!html.includes('#10b981'));
    }
});

test('measured zero risk and method provenance remain visible',async()=>{
    const html = await render({session_id:'test',risk:0,grade:'low',method:'heuristic'});
    assert(html.includes('>0%</div>'));
    assert.match(html,/индекс факторов/);
    const trained = await render({session_id:'test',risk:.8,grade:'high',method:'model',in_sample:true});
    assert(trained.includes('>80%</div>'));
    assert.match(trained,/известный исход/);
});

test('risk identifiers and explanations are data, not HTML',async()=>{
    const html = await render({session_id:'<img src=x>',risk:.8,grade:'high',method:'heuristic',
        top_factors:[{factor:'<img src=x>'}]});
    assert(!html.includes('<img'));
    assert.match(html,/&lt;img/);
});
