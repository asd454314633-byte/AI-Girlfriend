// Execute the actual Reading module with service/DOM boundaries replaced.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../ui/app/ui/reading.js'),'utf8').replace(/export /g,'');
const context={console,document:{getElementById:()=>null},window:{setTimeout:()=>1,clearTimeout:()=>{},AvatarSync:{interrupt:async()=>{}}}};
vm.createContext(context);
vm.runInContext(source+'\n globalThis.test={st,S,speakCurrent,tick,halt,pause,stopForModeSwitch,pauseForUserTurn,resumeAfterAssistantReply,openDoc,play};',context);
let calls=[];
context.mockApi=async(url,body)=>{
  calls.push({url,body});
  if(url==='/api/reading/speak')return {json:body.chunk_index===0
    ?{ok:true,pending:true,next_chunk_index:1,generation:1,submitted:0}
    :{ok:true,pending:false,completed_chunk_index:body.chunk_index,generation:1,submitted:1,strategy:'B',chars:20}};
  if(url==='/api/reading/status')return {json:{generation:1,pending_queue_items:0,response_open:false,submitted:1}};
  if(url==='/api/reading/pause')return {json:{generation:2}};
  return {ok:true,json:{}};
};
vm.runInContext('api=mockApi; avatarSpeaking=async()=>false;',context);
const {test:t}=context;
function reset(){calls=[];Object.assign(t.st,{docId:'fixture',chapters:[{}],chunks:[{text:'二'},{text:'正文第二段。'},{text:'末段正文。'}],chapter:0,chunk:0,state:t.S.READING,unitNext:null,unitEnd:null,unitRequiresPlayback:false,needsSpeak:true,heard:false,ticking:false,generation:1,serverSubmitted:null,resumePending:false});}
(async()=>{
 reset();await t.speakCurrent();assert.equal(t.st.chunk,0);assert.equal(t.st.unitNext,1);assert.equal(calls.filter(c=>c.url.includes('progress')).length,0);
 await t.speakCurrent();assert.equal(t.st.chunk,0);assert.equal(t.st.unitEnd,1);assert.deepEqual(calls.filter(c=>c.url.includes('speak')).map(c=>c.body.chunk_index),[0,1]);
 await t.tick();assert.equal(t.st.chunk,0);assert.equal(calls.filter(c=>c.url.includes('progress')).length,0);
 console.log('PASS B empty synthesis queue cannot commit before Avatar has actually spoken');
 t.st.heard=true;await t.tick();assert.equal(t.st.chunk,2);assert.equal(calls.find(c=>c.url.includes('progress')).body.chunk_index,2);
 console.log('PASS played source [0,1] commits next checkpoint 2 only after playback');
 for(const operation of [t.pause,t.stopForModeSwitch,t.pauseForUserTurn]){
   reset();await t.speakCurrent();await operation();assert.equal(t.st.chunk,0);assert.equal(t.st.unitNext,null);assert.equal(t.st.unitEnd,null);assert.equal(calls.find(c=>c.url.includes('pause')).body.chunk_index,0);
 }
 console.log('PASS pending Pause / Stop / ROLEPLAY switch / interruption retain source checkpoint 0');
 reset();await t.speakCurrent();await t.speakCurrent();await t.pause();assert.equal(t.st.chunk,0);assert.equal(t.st.unitEnd,null);await t.play();await t.tick();assert.equal(calls.filter(c=>c.url==='/api/reading/speak')[2].body.chunk_index,0);
 console.log('PASS interrupted merged generation resumes first uncommitted source');
 reset();await t.speakCurrent();await t.stopForModeSwitch();await t.resumeAfterAssistantReply({status:'completed'});assert.equal(t.st.state,t.S.STOPPED);
 console.log('PASS stopped reading cannot auto-resume after reply');
})().catch(e=>{console.error(e);process.exitCode=1});
