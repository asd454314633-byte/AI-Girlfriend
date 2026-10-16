// Exercise the real module against independent per-response output observations.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(process.env.READING_SOURCE||path.join(__dirname,'../ui/app/ui/reading.js'),'utf8').replace(/export /g,'');
let boundaries=[],calls=[],expected=new Set();
const avatar={interrupt:async()=>{},expectReading:k=>expected.add(k),forgetReading:k=>expected.delete(k),readingPlayback:async()=>boundaries};
const c={console,performance,Date,document:{getElementById:()=>null},window:{AvatarSync:avatar,setTimeout:()=>1,clearTimeout:()=>{}},fetch:async()=>{throw Error('unexpected network')}};
vm.createContext(c);vm.runInContext(source+'\nglobalThis.t={st,S,tick,halt,nextSubmitIndex,submitChunk,play};',c);
c.mockApi=async(url,body)=>{calls.push({url,body});return {ok:true,json:url.endsWith('/status')?{pending_queue_items:0,response_open:true,generation:0}:{ok:true,generation:1}}};
vm.runInContext('api=mockApi;avatarSpeaking=async()=>true;',c);
const t=c.t;
function entry(index,end=index){return {readingKey:'k'+index,chapter:t.st.chapter,chunks:t.st.chunks,index,checkpointIndex:index,unitEnd:end,heard:false,submittedAt:Date.now()};}
function reset(){calls=[];boundaries=[];expected.clear();Object.assign(t.st,{docId:'fixture',chapter:0,chunk:0,chunks:[{text:'first'},{text:'second'},{text:'third'},{text:'last'}],chapters:[{}],state:t.S.READING,ledger:[],plannerNext:null,ticking:false,speakingRequest:null,failures:0,waiting:0,resumePending:false});}
(async()=>{
 reset();t.st.ledger=[entry(0),entry(1)];boundaries=[{key:'k0',responseId:'r0',started:true,finished:false},{key:'k1',responseId:'r1',started:true,finished:false}];
 await t.tick();assert.equal(t.st.chunk,0);assert.equal(t.st.ledger[0].heard,true);
 boundaries[0].finished=true;await t.tick();assert.equal(t.st.chunk,1);assert.equal(t.st.ledger.length,1);
 assert.equal(calls.filter(x=>x.url.endsWith('/progress'))[0].body.chunk_index,1);
 console.log('PASS independent boundaries commit segment 0 while shared speaking remains true');
 reset();t.st.ledger=[entry(0),entry(1)];c.mockApi=async(url,body)=>{calls.push({url,body});return {ok:true,json:{pending_queue_items:0,response_open:false,generation:0}}};vm.runInContext('api=mockApi;',c);
 boundaries=[{key:'k0',responseId:'r0',started:true,finished:false},{key:'k1',responseId:'r1',started:false,finished:false}];await t.tick();
 assert.equal(calls.some(x=>x.url.endsWith('/speak')),false);assert.equal(t.st.ledger.length,2);
 console.log('PASS two outstanding segments block a third even when TTS is idle');
 reset();t.st.ledger=[entry(0),entry(1)];boundaries=[{key:'k0',responseId:'r0',started:true,finished:false,inputConsumed:true},{key:'k1',responseId:'r1',started:false,finished:false}];
 c.mockApi=async(url,body)=>{calls.push({url,body});return {ok:true,json:url.endsWith('/status')?{pending_queue_items:0,response_open:false,generation:0}:{ok:true,completed_chunk_index:2,generation:0,submitted:3,strategy:'B',chars:20}}};vm.runInContext('api=mockApi;',c);
 await t.tick();assert.equal(calls.filter(x=>x.url.endsWith('/speak')).length,1);assert.equal(t.st.ledger.length,3);assert.equal(t.st.chunk,0);
 assert.equal(t.st.ledger.filter(e=>!e.inputConsumed).length,2);
 await t.tick();assert.equal(calls.filter(x=>x.url.endsWith('/speak')).length,1);
 console.log('PASS consumed input frees synthesis capacity without advancing playback; three-entry ledger is bounded');
 c.mockApi=async(url,body)=>{calls.push({url,body});return {ok:true,json:url.endsWith('/status')?{pending_queue_items:0,response_open:true,generation:0}:{ok:true,generation:1}}};vm.runInContext('api=mockApi;',c);

 reset();t.st.ledger=[entry(0,1)];assert.equal((await t.nextSubmitIndex()).index,2);
 console.log('PASS merged source unit advances submission past every covered source');
 reset();t.st.ledger=[entry(3)];boundaries=[{key:'k3',responseId:'last',started:true,finished:true}];await t.tick();
 assert.equal(t.st.state,t.S.FINISHED);assert.equal(t.st.chunk,3);assert.equal(calls.at(-1).body.status,'DONE');
 console.log('PASS last segment finishes only on its own observed output end');
 reset();t.st.ledger=[entry(0)];t.st.ledger[0].submittedAt=Date.now()-31000;await t.tick();
 assert.equal(t.st.state,t.S.STOPPED);assert.equal(t.st.chunk,0);assert.equal(t.st.ledger.length,0);
 console.log('PASS absent playback boundary stops at original checkpoint without skipping');
 reset();let release;c.mockApi=async(url,body)=>{calls.push({url,body});if(url.endsWith('/speak'))return new Promise(r=>release=r);return {ok:true,json:{ok:true,generation:2}}};vm.runInContext('api=mockApi;',c);
 const target=await t.nextSubmitIndex();const submit=t.submitChunk(target),halt=t.halt('test','PAUSED');assert.equal(t.st.state,t.S.INTERRUPTING);
 release({ok:true,json:{ok:true,completed_chunk_index:0,generation:1,submitted:1,strategy:'B',chars:20}});
 await Promise.all([submit,halt]);assert.equal(t.st.state,t.S.PAUSED);assert.equal(t.st.ledger.length,0);assert.equal(expected.size,0);
 console.log('PASS pause closes submission race before awaiting in-flight request');
})().catch(e=>{console.error(e);process.exitCode=1;});
