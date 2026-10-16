// Real reading module, deterministic API and per-response playback boundaries.
// Run with READING_SOURCE to validate an isolated candidate before promotion.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(process.env.READING_SOURCE||path.join(__dirname,'../ui/app/ui/reading.js'),'utf8').replace(/export /g,'');
let passed=0;
function fixture({strategy='B',chapters=[['正常原文。','结束。'],['好。','下一章正常原文。']],busy=false,slow=false,pendingFront=false}={}) {
  let calls=[],boundaries=[],reads=[],submitDeferred=null;
  const chunks=chapters.map(a=>a.map((text,index)=>({text,index})));
  const avatar={interrupt:async()=>{},expectReading:()=>{},forgetReading:()=>{},readingPlayback:async()=>boundaries};
  const c={console,performance,Date,document:{getElementById:()=>null,querySelectorAll:()=>[]},window:{AvatarSync:avatar,setTimeout:()=>1,clearTimeout:()=>{}},fetch:async()=>{throw Error('unexpected network')}};
  vm.createContext(c);vm.runInContext(source+'\nglobalThis.t={st,S,tick,halt,pause,stop,stopForModeSwitch,nextSubmitIndex,submitChunk,prepareChapter,cancelResume,selectStrategy,openDoc,loadChapter,jumpTo};',c);
  const t=c.t;Object.assign(t.st,{docId:'fixture',chapters:chapters.map((_,index)=>({index})),chapter:0,chunk:0,chunks:chunks[0],state:t.S.READING,generation:4});
  c.mockApi=async(url,body)=>{
    calls.push({url,body});
    if(url.endsWith('/status'))return {ok:true,json:{pending_queue_items:busy?1:0,response_open:busy,generation:t.st.generation}};
    if(url.includes('/chapter?')){
      const chapter=Number(new URL(url,'http://fixture').searchParams.get('chapter'));
      if(slow)return new Promise(resolve=>reads.push(()=>resolve({ok:!!chunks[chapter],json:{chunks:chunks[chapter]}})));
      return {ok:!!chunks[chapter],json:{chunks:chunks[chapter]}};
    }
    if(url.endsWith('/speak')){
      if(pendingFront&&body.chapter_index===1&&body.chunk_index===0)return {ok:true,json:{ok:true,pending:true,next_chunk_index:1,generation:4}};
      const end=strategy==='B'?chunks[body.chapter_index].length-1:body.chunk_index;
      const sources=(strategy==='B'?chunks[body.chapter_index].slice(0,end+1):[chunks[body.chapter_index][body.chunk_index]])
        .map(x=>({chapter_index:body.chapter_index,chunk_index:x.index}));
      const result={ok:true,json:{ok:true,generation:4,completed_chunk_index:end,strategy,chars:20,submitted:1,source_chunks:sources}};
      return submitDeferred?new Promise(resolve=>submitDeferred(()=>resolve(result))):result;
    }
    if(url.endsWith('/pause'))return {ok:true,json:{ok:true,generation:5}};
    if(url.endsWith('/strategy'))return {ok:true,json:{ok:true,strategy:body.strategy,generation:5}};
    if(url.includes('/document?'))return {ok:true,json:{chapter_titles:chapters.map((_,index)=>({index})),progress:{}}};
    return {ok:true,json:{ok:true}};
  };
  vm.runInContext('api=mockApi;avatarSpeaking=async()=>true;',c);
  const entry=(chapter,index,end=index)=>({readingKey:`k${chapter}-${index}`,chapter,chunks:chunks[chapter],index,checkpointIndex:index,unitEnd:end,heard:false,inputConsumed:false,submittedAt:Date.now()});
  return {t,c,calls,boundaries,reads,chunks,entry,setBusy:v=>busy=v,deferSubmit:f=>submitDeferred=f};
}
async function test(name,run){await run();passed++;console.log('PASS '+name);}
async function ready(f){await f.t.nextSubmitIndex();await Promise.resolve();return f.t.nextSubmitIndex();}
(async()=>{
  for(const strategy of ['A','B','C'])await test(strategy+' cross-chapter submit does not display or commit the next chapter',async()=>{
    const f=fixture({strategy});f.t.st.ledger=[f.entry(0,0,1)];
    const target=await ready(f);assert.equal(target.chapter,1);assert.equal(target.index,0);
    await f.t.submitChunk(target);assert.equal(f.t.st.chapter,0);assert.equal(f.t.st.chunk,0);
    assert.equal(f.t.st.ledger[1].chapter,1);assert.equal(f.calls.filter(x=>x.url.endsWith('/progress')).length,0);
    assert.equal(f.calls.find(x=>x.url.endsWith('/speak')).body.chapter_index,1);
  });
  await test('B short previous tail is covered once; next short first source stays in its own chapter',async()=>{
    const f=fixture();const target=await f.t.nextSubmitIndex();await f.t.submitChunk(target);
    assert.equal(f.t.st.ledger[0].unitEnd,1);const next=await ready(f);await f.t.submitChunk(next);
    assert.deepEqual(f.calls.filter(x=>x.url.endsWith('/speak')).map(x=>[x.body.chapter_index,x.body.chunk_index]),[[0,0],[1,0]]);
  });
  await test('B pending next-chapter prefix retains chapter and source cursor until its unit submits',async()=>{
    const f=fixture({pendingFront:true});f.t.st.ledger=[f.entry(0,0,1)];const next=await ready(f);await f.t.submitChunk(next);
    assert.equal(f.t.st.plannerNext.chapter,1);assert.equal(f.t.st.plannerNext.index,1);assert.equal(f.t.st.chapter,0);
    await f.t.submitChunk(await f.t.nextSubmitIndex());assert.equal(f.t.st.ledger[1].chapter,1);assert.equal(f.t.st.ledger[1].index,1);
    assert.equal(f.t.st.ledger[1].unitEnd,1);assert.equal(f.t.st.plannerNext,null);
    f.setBusy(true);f.boundaries.push({key:'k0-0',started:true,finished:true});await f.t.tick();
    assert.equal(f.t.st.chapter,1);assert.equal(f.t.st.chunk,0);await f.t.pause();
    const paused=f.calls.find(x=>x.url.endsWith('/pause'));assert.equal(paused.body.chapter_index,1);assert.equal(paused.body.chunk_index,0);
  });
  await test('manual paragraph jump cancels prefetched successor and preserves selected chapter',async()=>{
    const f=fixture();f.t.st.ledger=[f.entry(0,0,1)];await ready(f);await f.t.jumpTo(1,false);
    assert.equal(f.t.st.chapter,0);assert.equal(f.t.st.chunk,1);assert.equal(f.t.st.chapterCache,null);assert.equal(f.t.st.ledger.length,0);
  });
  await test('full inflight lane waits; consumed input releases capacity without checkpoint changes',async()=>{
    const f=fixture();f.t.st.ledger=[f.entry(0,0),f.entry(0,1)];await f.t.tick();
    assert.equal(f.calls.some(x=>x.url.includes('/chapter?')||x.url.endsWith('/speak')),false);
    f.boundaries.push({key:'k0-0',started:true,inputConsumed:true,finished:false});
    await f.t.tick();await f.t.tick();assert.equal(f.t.st.ledger.length,3);assert.equal(f.t.st.chapter,0);
    await f.t.tick();assert.equal(f.calls.filter(x=>x.url.endsWith('/speak')).length,1);
  });
  await test('busy synthesis prevents cross-chapter submission',async()=>{
    const f=fixture({busy:true});f.t.st.ledger=[f.entry(0,0,1)];await f.t.tick();assert.equal(f.calls.some(x=>x.url.endsWith('/speak')),false);
  });
  await test('slow metadata GET does not block ticks or mutate current display',async()=>{
    const f=fixture({slow:true});f.t.st.ledger=[f.entry(0,0,1)];await f.t.tick();await f.t.tick();
    assert.equal(f.reads.length,1);assert.equal(f.t.st.chapter,0);assert.equal(f.t.st.ticking,false);
    f.reads[0]();await Promise.resolve();await f.t.tick();assert.equal(f.calls.filter(x=>x.url.endsWith('/speak')).length,1);
  });
  await test('actual output_end promotes cached chapter and writes the unread checkpoint exactly once',async()=>{
    const f=fixture({busy:true});f.t.st.ledger=[f.entry(0,0,1)];const next=await ready(f);await f.t.submitChunk(next);
    f.boundaries.push({key:'k0-0',responseId:'r0',started:true,finished:false});await f.t.tick();assert.equal(f.t.st.chapter,0);
    f.boundaries[0].finished=true;await f.t.tick();await f.t.tick();
    assert.equal(f.t.st.chapter,1);assert.equal(f.t.st.chunk,0);assert.equal(f.t.st.chunks[0].text,'好。');
    const writes=f.calls.filter(x=>x.url.endsWith('/progress'));assert.equal(writes.length,1);assert.equal(writes[0].body.chapter_index,1);
  });
  for(const operation of ['pause','stop','stopForModeSwitch'])await test(operation+' invalidates late chapter GET; source checkpoint preserved',async()=>{
    const f=fixture({slow:true});f.t.st.ledger=[f.entry(0,0,1)];await f.t.tick();await f.t[operation]();
    f.reads[0]();await Promise.resolve();await f.t.tick();assert.equal(f.t.st.chapterCache,null);assert.equal(f.t.st.ledger.length,0);
    assert.equal(f.calls.some(x=>x.url.endsWith('/speak')),false);const p=f.calls.find(x=>x.url.endsWith('/pause'));assert.equal(p.body.chapter_index,0);assert.equal(p.body.chunk_index,0);
  });
  await test('late speak completion after halt cannot reattach an old cross-chapter entry',async()=>{
    const f=fixture();f.t.st.ledger=[f.entry(0,0,1)];const next=await ready(f);let finish;
    f.deferSubmit(resolve=>finish=resolve);const speak=f.t.submitChunk(next);const stop=f.t.stop();finish();await Promise.all([speak,stop]);
    assert.equal(f.t.st.ledger.length,0);assert.equal(f.t.st.state,f.t.S.STOPPED);assert.equal(f.t.st.generation,5);
  });
  await test('strategy change discards old chapter metadata',async()=>{
    const f=fixture({slow:true});f.t.st.ledger=[f.entry(0,0,1)];await f.t.tick();await f.t.selectStrategy('A');f.reads[0]();await Promise.resolve();
    assert.equal(f.t.st.chapterCache,null);assert.equal(f.t.st.ledger.length,0);assert.equal(f.t.st.chapter,0);
  });
  await test('book identity and epoch prevent a late metadata response from entering another book',async()=>{
    const f=fixture({slow:true});f.t.st.ledger=[f.entry(0,0,1)];await f.t.tick();f.t.cancelResume();f.t.st.docId='another';f.reads[0]();await Promise.resolve();assert.equal(f.t.st.chapterCache,null);
  });
  await test('missing next chapter fails safely; last chapter never fetches a nonexistent successor',async()=>{
    const f=fixture({chapters:[['正文。']]});f.t.st.ledger=[f.entry(0,0)];assert.equal(await f.t.nextSubmitIndex(),null);
    f.boundaries.push({key:'k0-0',started:true,finished:true});await f.t.tick();assert.equal(f.t.st.state,f.t.S.FINISHED);
    assert.equal(f.calls.filter(x=>x.url.endsWith('/progress')&&x.body.status==='DONE').length,1);await f.t.tick();
    assert.equal(f.calls.filter(x=>x.url.endsWith('/progress')&&x.body.status==='DONE').length,1);
    const g=fixture();g.t.st.ledger=[g.entry(0,0,1)];g.chunks.pop();await ready(g);assert.equal(g.t.st.state,g.t.S.STOPPED);
  });
  console.log(JSON.stringify({passed,failed:0}));
})().catch(e=>{console.error(e);process.exitCode=1});
