const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'web_templates/dashboard.html'), 'utf8');
const scripts = [...html.matchAll(/src="\/assets\/([^\"]+\.js)"/g)]
    .map(match=>match[1]).filter(name=>!name.startsWith('vendor/'));

function harness() {
    const nodes = new Map();
    const listeners = new Map();
    const requests = [];
    const makeNode = () => ({
        textContent:'', innerHTML:'', style:{}, dataset:{}, children:[], isConnected:true,
        classList:{add(){},remove(){},contains(){return false;}},
        setAttribute(){}, removeAttribute(){}, addEventListener(){},
        querySelector(){return null;}, querySelectorAll(){return [];},
        append(...items){this.children.push(...items);}, appendChild(item){this.children.push(item);},
        prepend(item){this.children.unshift(item);}, remove(){},
    });
    const doc = {
        body:makeNode(),
        getElementById(id) {if(!nodes.has(id)) nodes.set(id,makeNode()); return nodes.get(id);},
        createElement:makeNode,
        querySelector(){return null;}, querySelectorAll(){return [];},
        addEventListener(kind, callback){if(!listeners.has(kind))listeners.set(kind,[]);listeners.get(kind).push(callback);},
    };
    doc.getElementById('dashboard-bootstrap').textContent = JSON.stringify({signal_labels:{}});
    const storage = new Map();
    const sandbox = {
        document:doc, console, location:{hostname:'test'},
        localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
        MutationObserver:class{observe(){}}, requestAnimationFrame(){},
        setTimeout(){}, clearTimeout(){}, setInterval(){},
        fetch:async url=>{requests.push(url);return {ok:true,json:async()=>
            url.startsWith('/prints?') ? {items:[],total:0}
            : (url === '/imports' || url.startsWith('/maintenance/')) ? [] : {}};},
    };
    sandbox.window=sandbox;
    const context=vm.createContext(sandbox);
    for(const script of scripts) vm.runInContext(fs.readFileSync(path.join(root,'web_assets',script),'utf8'),context,{filename:script});
    return {context,nodes,listeners,requests,makeNode};
}

test('all modules initialize without Chart/Three; the 10-card catalog still loads', async()=>{
    const h=harness();
    for(const name of ['showDesignPage','openPrintCard','estimateRecord','uploadArchiveLogs','loadHomeStats']) {
        assert.equal(typeof h.context[name],'function',name);
    }
    await h.context.loadHomeStats();
    assert(h.requests.includes('/prints?skip=0&limit=10'));
    assert.equal(h.nodes.get('home-prints-title').textContent,'ПЕЧАТИ · 0');
    assert.equal(h.nodes.get('home-load-more').hidden,true);
    assert(!h.requests.some(url=>url.startsWith('/dashboard/history/')));
});

test('missing chart library yields a local notice, not a global exception',()=>{
    const h=harness();
    const canvas=h.makeNode();canvas.parentElement=h.makeNode();
    assert.equal(h.context.createDashboardChart(canvas,{}),null);
    assert.equal(canvas.parentElement.children.length,1);
    assert.match(canvas.parentElement.children[0].textContent,/карточки остаются доступны/);
});

test('history is fetched only on opening its panel, with bounded page size',async()=>{
    const h=harness();
    h.context.fetch=async url=>{h.requests.push(url);return {ok:true,json:async()=>({items:[],total:0,has_more:false,table_rows:'empty'})};};
    await h.context.loadHistoryPanel('home');
    assert(!h.requests.some(url=>url.startsWith('/dashboard/history/')));
    await h.context.loadHistoryPanel('sessions');
    assert.equal(h.requests.filter(url=>url.startsWith('/dashboard/history/')).length,1);
    assert(h.requests.includes('/dashboard/history/sessions?skip=0&limit=50'));
    await h.context.loadHistoryPanel('sessions');
    assert.equal(h.requests.filter(url=>url.startsWith('/dashboard/history/')).length,1);
});

function legacySessionsHarness(pages) {
    const nodes = new Map(), requests = [];
    const makeNode = () => ({
        textContent:'', children:[], hidden:true, disabled:false, listeners:{},
        appendChild(node){this.children.push(node);},
        addEventListener(kind, callback){this.listeners[kind]=callback;},
        set innerHTML(value){throw new Error('Session data must not use an HTML sink');},
    });
    const document = {
        getElementById(id){if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);},
        createElement:makeNode,
    };
    const context = vm.createContext({document, fetch:async url=>{
        requests.push(url);
        const page=pages.shift();
        return {ok:!page.status, status:page.status || 200, json:async()=>page};
    }});
    const template=fs.readFileSync(path.join(root,'web_templates/sessions.html'),'utf8');
    const script=template.match(/<script>([\s\S]*?)<\/script>/)[1];
    return {context,nodes,requests,initial:vm.runInContext(script,context)};
}

test('legacy sessions use same-origin published items, feature fields and bounded pages',async()=>{
    const attack='<img src=x onerror="globalThis.pwned=true">';
    const h=legacySessionsHarness([
        {items:[{session_id:attack,start_ts:'2026-03-23',classification:'REAL_PRINT',
                 features:{material:attack,duration_sec:0}},
                {session_id:'unknown',features:{duration_sec:null}}],total:3},
        {items:[{session_id:'last',features:{material:'AlSi10Mg',duration_sec:3600}}],total:3},
    ]);
    await h.initial;
    const rows=h.nodes.get('session-rows').children;
    const more=h.nodes.get('session-more');
    assert.deepEqual(h.requests,['/sessions?skip=0&limit=50']);
    assert.equal(rows[0].children[0].textContent,attack);
    assert.equal(rows[0].children[3].textContent,attack);
    assert.equal(rows[0].children[4].textContent,'0 мин');
    assert.equal(rows[1].children[3].textContent,'-');
    assert.equal(rows[1].children[4].textContent,'-');
    assert.equal(h.context.pwned,undefined);
    assert.equal(more.hidden,false);
    assert.equal(h.nodes.get('session-status').textContent,'Показано 2 из 3.');
    await more.listeners.click();
    assert.deepEqual(h.requests,['/sessions?skip=0&limit=50','/sessions?skip=2&limit=50']);
    assert.equal(rows.length,3);
    assert.equal(rows[2].children[4].textContent,'60 мин');
    assert.equal(more.hidden,true);
});

test('legacy sessions distinguish HTTP or invalid-contract failures from empty data and retry',async()=>{
    const h=legacySessionsHarness([{status:503},{sessions:[],total:0},{items:[],total:0}]);
    await h.initial;
    const more=h.nodes.get('session-more'), status=h.nodes.get('session-status');
    assert.match(status.textContent,/HTTP 503/);
    assert.equal(more.disabled,false);
    assert.equal(more.textContent,'Повторить');
    await more.listeners.click();
    assert.match(status.textContent,/Некорректный ответ сервера/);
    assert.equal(more.hidden,false);
    await more.listeners.click();
    assert.equal(status.textContent,'Нет опубликованных сессий.');
    assert.equal(more.hidden,true);
    assert.equal(h.nodes.get('session-rows').children.length,0);
    assert.equal(h.requests.length,3);
    assert(h.requests.every(url=>url==='/sessions?skip=0&limit=50'));
});
