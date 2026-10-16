const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
    const nodes = new Map();
    const node = () => ({innerHTML:'', textContent:'', value:'', style:{}, dataset:{}, listeners:{},
        addEventListener(name,handler){this.listeners[name]=handler;}, querySelectorAll(){return [];}});
    const metrics = Array.from({length:5}, node);
    const labels = Array.from({length:5}, node);
    const context = vm.createContext({
        document:{
            getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},
            querySelectorAll(selector){return selector.endsWith('.pc-metric-value') ? metrics
                : selector.endsWith('.pc-metric-label') ? labels : [];},
            querySelector(){return null;},
        },
        _materialRu:value=>value || '', _formatHours:value=>`${value} ч`,
        _homePreviewFallback:()=>'', _renderHomeStlPreview(){}, _FILE_ICON:{stl:'model'},
    });
    context.window=context;
    const runtime=fs.readFileSync(path.join(__dirname,'../../web_assets/dashboard/runtime.js'),'utf8');
    vm.runInContext(runtime.slice(runtime.indexOf('function _esc('),runtime.indexOf('// Non-blocking')),context);
    for(const name of ['archive','card-editor','card-analysis','card-files','estimate-upload']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname,'../../web_assets/dashboard',name+'.js'),'utf8'),context);
    }
    function render(session, prediction, summary, fields={}, risk=null) {
        context.session=session;
        context.record={record_id:'one',session_id:'session',files:[],metadata_json:{prediction},summary,
            prediction_is_current:true,...fields};
        context.risk=risk;
        vm.runInContext('_pcSession=session; _pcRecord=record; _pcDefectRisk=risk; _renderPcWhat(); _renderPcOutcome(); _renderPcPlan();',context);
    }
    return {context,nodes,metrics,labels,render,node};
}

function summary(scope='machine_cycle') {
    return {comparison_scope:scope, coverage:{complete:true}, predicted_hours:10,actual_hours:12,error_pct:-16.7,
        actual_source:scope==='machine_cycle'?'normal_machine_log':'subtotal_machine_log'};
}
const session={features:{machine_min:60,duration_min:900,idle_min:840,idle_pct:93,
    normal_machine_cycle_seconds:21600,explicit_pause_seconds:1800,unattributed_elapsed_seconds:600},
    log_insights:{time_accounting:{normal_layer_count:75,normal_time_scope:'eligible_measured_layers_only'}}};

function capturePlanActions(h) {
    const pane=h.context.document.getElementById('pcpane-plan'), actions=[];
    let html='';
    Object.defineProperty(pane,'innerHTML',{get:()=>html,set:value=>{
        html=value;
        actions.length=0;
        for(const match of value.matchAll(/<button[^>]+data-pc-prediction-action="([^"]+)"[^>]*>([^<]+)<\/button>/g)) {
            const button=h.node();
            button.dataset.pcPredictionAction=match[1];button.textContent=match[2];
            actions.push(button);
        }
    }});
    pane.querySelectorAll=selector=>selector==='[data-pc-prediction-action]'?actions:[];
    return actions;
}

test('card consumes the actual risk API field without treating unknown as zero',()=>{
    const h=harness();
    for(const risk of [null,undefined,NaN,Infinity,-1,2,true]) {
        h.render(session,{},summary(),{}, {risk,method:'heuristic'});
        assert.equal(h.metrics[4].textContent,'—');
    }
    h.render(session,{},summary(),{}, {risk:0,method:'heuristic'});
    assert.equal(h.metrics[4].textContent,'0%');
    assert.equal(h.labels[4].textContent,'ИНДЕКС РИСКА');
    h.render(session,{},summary(),{}, {risk:.8,method:'model',in_sample:true});
    assert.equal(h.metrics[4].textContent,'80%');
    assert.equal(h.labels[4].textContent,'ОЦЕНКА МОДЕЛИ');
    assert.match(h.metrics[4].title,/не независимый прогноз/);
});

test('opening the editor does not choose a material for the operator',()=>{
    const h=harness();
    h.context._printMaterials=['steel','Ti64'];
    h.render(null,{}, {},{material:null});
    const empty=h.nodes.get('pcpane-what').innerHTML;
    assert.match(empty,/<option value="" selected>Материал не указан<\/option>/);
    assert(!empty.includes('value="steel" selected'));
    h.render(null,{}, {},{material:'custom-alloy'});
    const custom=h.nodes.get('pcpane-what').innerHTML;
    assert.match(custom,/<option value="custom-alloy" selected>/);
    assert(!custom.includes('value="steel" selected'));
    h.render(null,{}, {},{material:'steel'});
    assert.match(h.nodes.get('pcpane-what').innerHTML,/<option value="steel" selected>/);
});

test('normal-cycle fact and percent come from admitted server comparison, not session subtotal',()=>{
    const h=harness();
    const value=h.context._pcTimeMetrics(session,{machine_cycle_hours:10,print_hours:8},summary(),true);
    assert.equal(value.actualHours,12);
    assert.equal(value.errorPct,-16.7);
    assert.equal(value.measuredHours,6);
    assert.equal(value.subtotalHours,1);
    h.render(session,{machine_cycle_hours:10,print_hours:8},summary());
    assert.equal(h.metrics[2].textContent,'-16.7%');
    assert.equal(h.labels[1].textContent,'ФАКТ ЦИКЛА');
    assert.match(h.nodes.get('pcpane-what').innerHTML,/>12 ч</);
});

test('legacy subtotal is compared and labelled only as burn plus pour',()=>{
    const h=harness();
    h.render(session,{print_hours:10},summary('burn_plus_pour'));
    assert.equal(h.labels[0].textContent,'ОЦЕНКА ФАЗ');
    assert.equal(h.labels[1].textContent,'ФАКТ ФАЗ');
    assert.match(h.nodes.get('pcpane-outcome').innerHTML,/Сопоставляем<\/span><span>Прожиг \+ нанесение/);
    assert.match(h.nodes.get('pcpane-plan').innerHTML,/Прожиг \+ нанесение<\/span>/);
});

test('a raw stored cycle is not a full-cycle prediction without server admission',()=>{
    const h=harness();
    for(const raw of [10,null,NaN]) {
        const pred={machine_cycle_hours:raw,print_hours:8};
        assert.equal(h.context._pcTimeMetrics(null,pred,{}).predictedHours,null);
        h.render(null,pred,{});
        const html=h.nodes.get('pcpane-plan').innerHTML;
        assert.match(html,/Прожиг \+ нанесение<\/span><span>8 ч/);
        assert(!html.includes('Нормальный машинный цикл'));
    }
    h.render(null,{machine_cycle_hours:99,print_hours:8},summary());
    assert.match(h.nodes.get('pcpane-plan').innerHTML,/Нормальный машинный цикл<\/span><span>10 ч/);
});

test('STL deletion leaves an unlinked saved prediction only in a labelled historical disclosure',()=>{
    const h=harness();
    const pred={input_revision:2,print_hours:8,cost_total_rub:1000,layer_count:123,n_parts:1,
        prediction_warnings:['Старое ограничение <снимка>']};
    const before=JSON.stringify(pred);
    h.render(null,pred,{...summary(),comparison_status:'no_session',actual_hours:null},
        {session_id:null,revision:5,prediction_is_current:false});
    const what=h.nodes.get('pcpane-what').innerHTML;
    assert.doesNotMatch(what,/>10 ч</);
    assert.doesNotMatch(what,/>123</);
    assert.match(what,/Прогноз устарел/);
    const plan=h.nodes.get('pcpane-plan').innerHTML;
    const historicalAt=plan.indexOf('<details');
    assert(historicalAt>=0,'the saved prediction must remain accessible as history');
    assert.doesNotMatch(plan.slice(0,historicalAt),/8 ч|1\s?000 ₽|>123</);
    assert.match(plan,/Сохранённый прогноз · устарел/);
    assert.match(plan.slice(historicalAt),/8 ч/);
    assert.match(plan.slice(historicalAt),/>123</);
    assert.match(plan,/Старое ограничение &lt;снимка&gt;/);
    assert.match(plan,/Пересчитать оценку времени/);
    assert.match(plan,/Файлы/);
    assert.equal(JSON.stringify(pred),before,'rendering must not delete or rewrite the stored snapshot');
});

test('missing freshness and unversioned legacy snapshots are unknown rather than current or proven stale',()=>{
    const h=harness();
    for(const fields of [{prediction_is_current:undefined,revision:5},
        {prediction_is_current:false,revision:5}]) {
        h.render(null,{print_hours:8,layer_count:123},summary(),{session_id:null,...fields});
        assert.doesNotMatch(h.nodes.get('pcpane-what').innerHTML,/>10 ч<|>123</);
        const plan=h.nodes.get('pcpane-plan').innerHTML;
        assert.match(plan,/Актуальность прогноза не подтверждена/);
        assert.doesNotMatch(plan,/Прогноз устарел|Сохранённый прогноз · устарел/);
        assert.match(plan,/Сохранённый прогноз/);
    }
});

test('freshness hides prediction and error without changing admitted actual or measured diagnostics',()=>{
    const h=harness();
    for(const current of [false,undefined,null]) {
        const timing=h.context._pcTimeMetrics(session,{print_hours:8},summary(),current);
        assert.equal(timing.predictedHours,null);
        assert.equal(timing.predictedSubtotalHours,null);
        assert.equal(timing.errorPct,null);
        assert.equal(timing.actualHours,12);
        assert.equal(timing.measuredHours,6);
        assert.equal(timing.subtotalHours,1);
    }
    h.render({...session,features:{...session.features,layers:321}},
        {print_hours:8,layer_count:123,input_revision:2},summary(),{revision:5,prediction_is_current:false});
    assert.match(h.nodes.get('pcpane-what').innerHTML,/>321</);
    assert.match(h.nodes.get('pcpane-outcome').innerHTML,/Факт \(допущен к сравнению\)<\/span><span>12\.00 ч/);
    assert.match(h.nodes.get('pcpane-outcome').innerHTML,/Прогноз<\/span><span>—/);
});

test('archive caller applies freshness while preserving admitted actual time',async()=>{
    const h=harness();
    h.context._archiveFilters=()=>'';
    h.context.loadUnlinkedSessions=async()=>{};
    h.context.fetch=async()=>({ok:true,json:async()=>({total:1,items:[{
        record_id:'one',revision:5,files:[],metadata_json:{prediction:{print_hours:8,input_revision:2}},
        prediction_is_current:false,summary:{...summary(),predicted_cost_rub:1000},
    }]})});
    await h.context.loadArchive();
    const html=h.nodes.get('archive-table-body').innerHTML;
    assert.doesNotMatch(html,/>10 ч</);
    assert.doesNotMatch(html,/1\s?000 ₽/);
    assert.match(html,/>12 ч</);
    assert.doesNotMatch(html,/-16\.7%/);
});

test('historical prediction actions invoke the existing calculation and files callbacks for that card',()=>{
    const h=harness(), actions=capturePlanActions(h), calls=[];
    h.context.estimateRecord=(...args)=>calls.push(['estimate',...args]);
    h.context.showPrintCardTab=(...args)=>calls.push(['tab',...args]);
    h.render(null,{input_revision:2,print_hours:8},summary(),
        {record_id:'existing-card',session_id:null,revision:5,prediction_is_current:false});
    assert.deepEqual(calls,[],'rendering history must not enqueue a calculation');
    assert.deepEqual(actions.map(button=>button.textContent),['Пересчитать оценку времени','Файлы']);
    actions.forEach(button=>button.listeners.click());
    assert.deepEqual(calls,[['estimate','existing-card',true],['tab','files']]);
});

test('changed calculation configuration labels the saved plan without hiding historical numbers',()=>{
    const h=harness();
    const pred={input_revision:1,print_hours:8,cost_total_rub:1000,layer_count:123};
    const before=JSON.stringify(pred);
    h.render(session,pred,summary(),{revision:2,prediction_configuration_is_current:false});
    const plan=h.nodes.get('pcpane-plan').innerHTML;
    assert.match(plan,/<h4>Сохранённый прогноз<\/h4>/);
    assert.match(plan,/Настройки расчёта изменились/);
    assert.doesNotMatch(plan,/Прогноз по текущим настройкам|Настройки машины изменились/);
    assert.match(plan,/Нормальный машинный цикл<\/span><span>10 ч/);
    assert.match(plan,/1\s?000 ₽/);
    assert.match(plan,/>123</);
    assert.equal(h.metrics[2].textContent,'-16.7%');
    assert.match(h.nodes.get('pcpane-outcome').innerHTML,/Прогноз<\/span><span>10\.00 ч/);
    assert.match(h.nodes.get('pcpane-outcome').innerHTML,/Факт \(допущен к сравнению\)<\/span><span>12\.00 ч/);
    assert.equal((plan.match(/Настройки расчёта изменились/g)||[]).length,1);
    assert.doesNotMatch(h.nodes.get('pcpane-what').innerHTML,/Настройки расчёта изменились/);
    assert.doesNotMatch(h.nodes.get('pcpane-outcome').innerHTML,/Настройки расчёта изменились/);
    assert.equal(JSON.stringify(pred),before);
});

test('unknown or missing configuration evidence never claims settings changed or match',()=>{
    const h=harness();
    for(const current of [undefined,null,'false','<img src=x onerror="pwned=1">']) {
        h.render(null,{input_revision:1,print_hours:8},summary(),
            {revision:2,prediction_configuration_is_current:current});
        const plan=h.nodes.get('pcpane-plan').innerHTML;
        assert.match(plan,/<h4>Сохранённый прогноз<\/h4>/);
        assert.match(plan,/Совпадение с текущими настройками не подтверждено/);
        assert.doesNotMatch(plan,/Настройки расчёта изменились|Прогноз по текущим настройкам|<img/);
        assert.match(plan,/>10 ч</);
        assert.doesNotMatch(plan,/текущая машина из настроек/);
    }
});

test('plan formats exact captured hours only at the display boundary',()=>{
    const h=harness(), exact=1.23456789123;
    const prediction={print_hours:exact,machine_cycle_hours:exact};
    for(const fields of [{}, {prediction_is_current:false}]) {
        for(const scope of ['machine_cycle','burn_plus_pour']) {
            const comparison={...summary(scope),predicted_hours:exact};
            h.render(null,prediction,comparison,fields);
            const plan=h.nodes.get('pcpane-plan').innerHTML;
            assert.match(plan,/>1\.235 ч</);
            assert.doesNotMatch(plan,/1\.23456789123 ч/);
            assert.equal(prediction.print_hours,exact);
            assert.equal(comparison.predicted_hours,exact);
            if(fields.prediction_is_current!==false) {
                assert.equal(h.context._pcTimeMetrics(null,prediction,comparison,true).predictedHours,exact);
            }
        }
    }
});

test('an explicitly matching configured recipe does not revive stale card geometry',()=>{
    const h=harness();
    h.render(session,{input_revision:1,print_hours:8},summary(),
        {revision:2,prediction_configuration_is_current:true});
    assert.match(h.nodes.get('pcpane-plan').innerHTML,/<h4>Прогноз по текущим настройкам<\/h4>/);
    assert.equal(h.metrics[2].textContent,'-16.7%');
    h.render(session,{input_revision:1,print_hours:8},summary(),
        {revision:4,prediction_is_current:false,prediction_configuration_is_current:true});
    const plan=h.nodes.get('pcpane-plan').innerHTML;
    assert.match(plan,/Сохранённый прогноз · устарел/);
    assert.match(plan,/<h4>Сохранённый прогноз<\/h4>/);
    assert.doesNotMatch(plan,/Прогноз по текущим настройкам/);
    assert.equal(h.metrics[2].textContent,'—');
});

test('configuration notices retain the real manual recalculation callback for the selected card',()=>{
    const h=harness(), actions=capturePlanActions(h), calls=[];
    const id="existing'card/?<";
    h.context.estimateRecord=(...args)=>calls.push(args);
    for(const current of [false,null,true]) {
        h.render(null,{input_revision:1,print_hours:8},summary(),
            {record_id:id,revision:2,prediction_configuration_is_current:current});
        const action=actions.find(button=>button.dataset.pcPredictionAction==='recalculate');
        assert(action,'the plan must retain the existing manual recalculation action');
        assert.equal(action.textContent,'Пересчитать оценку времени');
        assert.equal(calls.length,0,'reading a plan must not enqueue a recalculation');
        action.listeners.click();
        assert.deepEqual(calls.pop(),[id,true]);
    }
});

test('saved plan sources remain escaped under changed or unknown configuration',()=>{
    const h=harness(), attack='<img src=x onerror="pwned=1">&';
    for(const current of [false,null,true]) {
        h.render(null,{input_revision:1,print_hours:8,printer_id:attack,material:attack},summary(),
            {revision:2,prediction_configuration_is_current:current});
        const plan=h.nodes.get('pcpane-plan').innerHTML;
        assert.doesNotMatch(plan,/<img/);
        assert.equal((plan.match(/&lt;img/g)||[]).length,2);
        assert.doesNotMatch(plan,/&amp;lt;/);
        assert.equal(h.context.pwned,undefined);
    }
});

test('a card with no saved prediction keeps its ordinary empty state',()=>{
    const h=harness();
    h.render(null,null,{}, {prediction_is_current:false,revision:5,session_id:null});
    const html=h.nodes.get('pcpane-plan').innerHTML;
    assert.match(html,/приложите STL деталей/);
    assert.doesNotMatch(html,/устарел|не подтверждена|Сохранённый прогноз/);
});

test('single-model preview labels its phase subtotal and never prints unknown days',()=>{
    const h=harness();
    const pred={available:true,print_hours:8,total_days:null,cost_total_rub:1000};
    const subtotal=h.context._renderPrediction(pred);
    assert(subtotal.includes('прожиг + нанесение'));
    assert(!subtotal.includes('null сут'));
    assert(!subtotal.includes('время печати'));
    const cycle=h.context._renderPrediction({...pred,total_days:.5,
        time_breakdown:{prediction_time_scope:'machine_cycle',machine_cycle_hours:12}});
    assert.match(cycle,/12 <span[^>]+>ч/);
    assert(cycle.includes('нормальный машинный цикл (0.5 сут)'));
});

test('preview renders messages, material and breakdown labels as data',()=>{
    const h=harness();
    const attack='<img src=x onerror="pwned=1">';
    const html=h.context._renderPrediction({available:true,print_hours:8,cost_total_rub:1000,
        material:attack,method:attack,warnings:[attack],cost_breakdown:{[attack]:1}});
    assert(!html.includes('<img'));
    assert.match(html,/&lt;img/);
    assert(!h.context._renderPrediction({available:false,reason:attack}).includes('<img'));
});

test('partial measured sums and legacy wall spans never become whole-print facts',()=>{
    const h=harness();
    for(const invalid of [undefined, {}, {...summary(),coverage:{complete:false}},
        {...summary(),actual_source:'wall_span'}, {...summary(),actual_source:'subtotal_machine_log'},
        {...summary(),comparison_scope:null}]) {
        const value=h.context._pcTimeMetrics(session,{machine_cycle_hours:10},invalid);
        assert.equal(value.actualHours,null);
        assert.equal(value.errorPct,null);
    }
    h.render(session,{machine_cycle_hours:10},{...summary(),coverage:{complete:false},
        comparison_reason_ru:'Покрытие неполное <не HTML>'});
    assert.equal(h.metrics[2].textContent,'—');
    const html=h.nodes.get('pcpane-outcome').innerHTML;
    assert.match(html,/допустимые измеренные слои/);
    assert.match(html,/6\.00 ч/);
    assert.match(html,/Покрытие неполное &lt;не HTML&gt;/);
    assert.match(html,/Факт \(допущен к сравнению\)<\/span><span>—/);
    assert(!html.includes('<span>Простой</span>'));
    assert(!html.includes('14.00 ч'));
    assert.match(html,/Явные завершённые паузы из лога<\/span><span>0\.50 ч/);
});

test('missing, invalid or zero full-cycle facts cannot create an error percentage',()=>{
    const h=harness();
    for(const actual of [null,undefined,'',true,NaN,Infinity,-1,0]) {
        const value=h.context._pcTimeMetrics(session,{}, {...summary(),actual_hours:actual});
        assert.equal(value.actualHours,null);
        assert.equal(value.errorPct,null);
    }
    const value=h.context._pcTimeMetrics({features:{machine_min:Infinity,duration_min:true}},null,null);
    assert.equal(value.subtotalHours,null);
    assert.equal(value.wallHours,null);
    assert.equal(value.explicitPauseHours,null);
    assert.equal(h.context._pcTimeMetrics({features:{explicit_pause_seconds:null},
        log_insights:{time_accounting:{explicit_pause_seconds:0}}}).explicitPauseHours,null);
});

test('unlinked-session banner distinguishes subtotal and wall-clock without legacy idle claim',async()=>{
    const h=harness();
    h.context.fetch=async url=>({json:async()=>url.includes('unlinked-sessions')
        ? {items:[{session_id:'one',is_print:true,machine_min:60,duration_min:900,idle_min:840,layers:100}]}
        : {items:[]}});
    await h.context.loadUnlinkedSessions();
    const html=h.nodes.get('unlinked-rows').innerHTML;
    assert.match(html,/1\.0 ч прожига \+ нанесения \(измеренная часть\)/);
    assert.match(html,/15\.0 ч по часам/);
    assert(!html.includes('простой'));
    assert(!html.includes('ч машинного'));
});

test('card filenames are escaped data and preview/delete callbacks receive data, not source',()=>{
    const h=harness();
    const filename=`');globalThis.pwned=1;//<img src=x onerror="pwned=2">&.stl`;
    h.render(null,{},null,{record_id:'card/?',files:[{file_id:'file/?',file_name:filename,file_type:'stl',size_bytes:100}]});
    const preview=h.node(), remove=h.node();
    preview.dataset.pcPreview='0';remove.dataset.pcDelete='0';
    const pane=h.context.document.getElementById('pcpane-files');
    pane.querySelectorAll=selector=>selector==='[data-pc-preview]'?[preview]:[remove];
    const calls=[];
    h.context.previewArchiveStl=(...args)=>calls.push(args);
    h.context.deleteArchiveFile=(...args)=>calls.push(args);
    h.context._renderPcFiles();
    assert(!pane.innerHTML.includes('<img'));
    assert(!/\son(?:click|change)=/.test(pane.innerHTML));
    assert.match(pane.innerHTML,/&lt;img/);
    assert.match(pane.innerHTML,/\/prints\/card%2F%3F\/files\/file%2F%3F\/download/);
    const event={preventDefault(){}};
    preview.listeners.click(event);remove.listeners.click(event);
    assert.deepEqual(calls,[['/prints/card%2F%3F/files/file%2F%3F/download',filename],['card/?','file/?',filename]]);
    assert.equal(h.context.pwned,undefined);
});

test('editable card fields cannot close textarea or inject option/input attributes',()=>{
    const h=harness();
    const attack=`</textarea><img src=x onerror="pwned=1">&'`;
    h.context._printMaterials=[attack];
    h.render(null,{},null,{name:attack,notes:attack,material:attack});
    const html=h.nodes.get('pcpane-what').innerHTML;
    assert(!html.includes('<img'));
    assert(!html.includes('value="</textarea>'));
    assert.match(html,/&lt;\/textarea&gt;&lt;img/);
    assert.match(html,/&quot;pwned=1&quot;/);
    assert.equal((html.match(/<\/textarea>/g)||[]).length,1);
    assert.equal((html.match(/id="pc-preview-host"/g)||[]).length,1);
    assert(!/<div class="pc-grid"[^>]*display\s*:\s*none/.test(html));
    assert.match(html,/onclick="savePrintCard\(\)"/);
});

test('unlinked card picker escapes card name and option value',async()=>{
    const h=harness();
    h.context.fetch=async url=>({json:async()=>url.includes('unlinked-sessions')
        ? {items:[{session_id:'one',is_print:true}]}
        : {items:[{record_id:'one" onclick="pwned=1',name:'</option><img src=x>'}]}});
    await h.context.loadUnlinkedSessions();
    const html=h.nodes.get('unlinked-rows').innerHTML;
    assert(!html.includes('<img'));
    assert(!html.includes('value="one" onclick='));
    assert.match(html,/&lt;\/option&gt;&lt;img/);
    assert.match(html,/one&quot; onclick=&quot;pwned=1/);
});

test('archive chips and card labels use escaped text plus data-bound actions',async()=>{
    const h=harness();
    const attack=`');globalThis.pwned=1;//<img src=x onerror="pwned=2">&`;
    const record={record_id:'card/?',name:attack,notes:attack,material:attack,status:attack,
        files:[{file_id:'file/?',file_name:attack,file_type:'stl'}]};
    h.context._archiveFilters=()=>'';
    h.context.loadUnlinkedSessions=async()=>{};
    h.context.fetch=async()=>({ok:true,json:async()=>({items:[record],total:1})});
    const controls=['preview','delete-file','delete-record','open'].map(action=>{
        const control=h.node();control.dataset={arAction:action,arRecord:'0',arFile:'0'};return control;
    });
    const body=h.context.document.getElementById('archive-table-body');
    body.querySelectorAll=()=>controls;
    await h.context.loadArchive();
    assert(!body.innerHTML.includes('<img'));
    assert(!/\son(?:click|change)=/.test(body.innerHTML));
    assert(!body.innerHTML.includes('Ошибка загрузки'));
    assert.match(body.innerHTML,/&lt;img/);
    assert.match(body.innerHTML,/\/prints\/card%2F%3F\/files\/file%2F%3F\/download/);
    const calls=[];
    for(const name of ['previewArchiveStl','deleteArchiveFile','deletePrintRecord','openPrintCard']) {
        h.context[name]=(...args)=>calls.push([name,...args]);
    }
    controls.forEach(control=>control.listeners.click({preventDefault(){}}));
    assert.deepEqual(calls,[
        ['previewArchiveStl','/prints/card%2F%3F/files/file%2F%3F/download',attack],
        ['deleteArchiveFile','card/?','file/?',attack],['deletePrintRecord','card/?',attack],['openPrintCard','card/?']]);
    assert.equal(h.context.pwned,undefined);
});

test('archive status and default material helpers escape once and preserve selectable values',async()=>{
    const h=harness();
    const attack='</option><img src=x onerror="pwned=1">&';
    const badge=h.context._statusBadge(attack);
    assert(!badge.includes('<img'));
    assert.match(badge,/&lt;img/);
    assert(!badge.includes('&amp;lt;'));
    h.context.fetch=async()=>({json:async()=>({materials:[attack]})});
    h.context.document.getElementById('ar-material').value=attack;
    await h.context.loadPrintDefaults();
    for(const id of ['ar-material','ar-filter-material']) {
        const html=h.nodes.get(id).innerHTML;
        assert(!html.includes('<img'));
        assert.match(html,/&lt;\/option&gt;&lt;img/);
        assert.match(html,/&quot;pwned=1&quot;/);
    }
    assert.equal(h.nodes.get('ar-material').value,attack);
});
