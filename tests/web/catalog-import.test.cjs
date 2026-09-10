const {test} = require('node:test');
const assert = require('node:assert/strict');
const {CatalogPager} = require('../../web_assets/catalog-pager.js');
const {plan} = require('../../web_assets/folder-import.js');

test('pages contain 10 records, final page 2, no repeats', async () => {
    const rows = Array.from({length:132}, (_,i) => ({record_id:String(i)}));
    const calls = [];
    const pager = new CatalogPager(async (skip, limit) => {
        calls.push([skip,limit]); return {items:rows.slice(skip,skip+limit),total:rows.length};
    });
    const seen = [];
    while (pager.hasMore) { const page = await pager.next(); assert.equal(page.length, seen.length === 130 ? 2 : 10); seen.push(...page); }
    assert.equal(new Set(seen.map(x=>x.record_id)).size,132);
    assert.deepEqual(calls[1],[10,10]);
});
test('filter can find records after offset 100', async () => {
    const rows = Array.from({length:132}, (_,i) => ({record_id:String(i), matched:i>=120}));
    const pager = new CatalogPager(async (skip,limit)=>({items:rows.slice(skip,skip+limit),total:132}), x=>x.matched);
    assert.equal((await pager.next()).length,10);
    assert.equal((await pager.next()).length,2);
    assert.equal(pager.hasMore,false);
});
test('a failed page can be retried without losing buffered records', async () => {
    let fail=true;
    const pager = new CatalogPager(async ()=>{if(fail) throw Error('network'); return {items:[{record_id:'1'}],total:1};});
    await assert.rejects(pager.next()); fail=false;
    assert.equal((await pager.next())[0].record_id,'1');
});
const sha='a'.repeat(64);
const file=(name,path,size=3)=>({name,webkitRelativePath:path,size});
const manifest=(value,path='plate/print-bundle.json')=>({name:'print-bundle.json',webkitRelativePath:path,size:100,text:async()=>JSON.stringify(value)});
const bundle={schema_version:1,kind:'print',files:[{path:'model.stl',role:'model',size:3,sha256:sha},{path:'logs.zip',role:'logs',size:3,sha256:'b'.repeat(64)}]};
test('root manifest selects only listed files, ignores per-model archive copies', async()=>{
    const result=await plan([manifest(bundle),file('model.stl','plate/model.stl'),file('logs.zip','plate/logs.zip'),file('copy.zip','plate/models/copy.zip')]);
    assert.equal(result.files.length,2);
});
test('catalog and part-only reference cannot become a false whole print', async()=>{
    await assert.rejects(plan([manifest({schema_version:1,kind:'catalog'})]),/весь каталог/);
    await assert.rejects(plan([manifest({schema_version:1,kind:'model_reference'})]),/всей плите/);
});
test('missing files and traversal are rejected', async()=>{
    await assert.rejects(plan([manifest(bundle)]),/отсутствует/);
    await assert.rejects(plan([manifest({...bundle,files:[{path:'../escape',role:'model',sha256:sha,size:3}]})]),/Некорректный/);
});
test('plain files need explicit operator confirmation and several layouts are rejected', async()=>{
    const result=await plan([file('one.stl','folder/one.stl')]);
    assert.equal(result.manifest,null);
    await assert.rejects(plan([file('one.magics','folder/one.magics'),file('two.magics','folder/two.magics')]),/несколько/);
});
