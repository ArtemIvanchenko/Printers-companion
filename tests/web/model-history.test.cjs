const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function harness(prediction) {
    const nodes=new Map(), saved=[];
    const ctx=vm.createContext({URL,location:{origin:'http://127.0.0.1'},
        FormData:class {append(){}},localStorage:{getItem(){return '1';},setItem(){}},
        document:{getElementById(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',value:'steel'});return nodes.get(id);}},
        _esc:value=>String(value??'').replaceAll('<','&lt;'),
        fetch:async()=>({ok:true,json:async()=>({filename:'part.stl',volume_cm3:2,prediction,
            estimate:{avg_session_hours:99,sessions_used:10,note:'historical span'}})}),
    });
    for(const file of ['models','estimate-upload']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname,'../../web_assets/dashboard',file+'.js'),'utf8'),ctx);
    }
    ctx._saveModelEntry=(...args)=>saved.push(args);
    ctx.loadModelHistory=()=>{};
    ctx.openViewer=()=>{};
    return {ctx,saved,nodes};
}

test('historical session mean never becomes an uploaded model prediction',async()=>{
    const h=harness({available:false,reason:'No geometry parameters'});
    const file={name:'part.stl',arrayBuffer:async()=>new ArrayBuffer(0)};
    await h.ctx.uploadStl(file);
    await h.ctx.addModelFromFile(file);
    assert.equal(h.saved.length,2);
    assert(h.saved.every(row=>row[2]===null));
    assert(h.nodes.get('stl-result').innerHTML.includes('99'));
});

test('calculated phase subtotal remains available in model history',async()=>{
    const h=harness({available:true,print_hours:2,cost_total_rub:0});
    const file={name:'part.stl',arrayBuffer:async()=>new ArrayBuffer(0)};
    await h.ctx.uploadStl(file);
    await h.ctx.addModelFromFile(file);
    assert.equal(h.saved.length,2);
    assert(h.saved.every(row=>row[2]===2));
});
