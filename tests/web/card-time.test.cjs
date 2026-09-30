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
    for(const name of ['archive','card-editor','card-analysis','card-files']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname,'../../web_assets/dashboard',name+'.js'),'utf8'),context);
    }
    function render(session, prediction, summary, fields={}) {
        context.session=session;
        context.record={record_id:'one',session_id:'session',files:[],metadata_json:{prediction},summary,...fields};
        vm.runInContext('_pcSession=session; _pcRecord=record; _renderPcWhat(); _renderPcOutcome(); _renderPcPlan();',context);
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

test('normal-cycle fact and percent come from admitted server comparison, not session subtotal',()=>{
    const h=harness();
    const value=h.context._pcTimeMetrics(session,{machine_cycle_hours:10,print_hours:8},summary());
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
    assert.match(h.nodes.get('pcpane-plan').innerHTML,/Прожиг \+ нанесение \(старый подытог\)/);
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
    h.context.fetch=async()=>({json:async()=>({items:[record],total:1})});
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
