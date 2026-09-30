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
