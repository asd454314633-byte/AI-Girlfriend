'use strict';
// New negotiation checks only; the three historical contract suites are preserved.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync(__dirname+'/avatar-sync.js','utf8');
const sdp='v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\na=candidate:1 1 udp 2113937151 host.local 50000 typ host\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\na=candidate:1 1 udp 2113937151 host.local 50001 typ host\r\n';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
function fixture(mode){
 const requests=[],pcs=[],timers=new Set();
 const element=()=>({children:[],style:{},appendChild(e){this.children.push(e);},insertBefore(){},play:async()=>{},currentTime:0});
 const status=element(),video=element(),audio=element(),layer=element();
 const document={head:element(),body:element(),createElement:element,getElementById:id=>({'gf-avatar-status':status,'av-video':video,'av-audio':audio,'av-layer':layer}[id]),addEventListener(){}};
 class PC extends EventTarget{
  constructor(){super();pcs.push(this);this.connectionState='new';this.iceConnectionState='new';this.iceGatheringState='new';this.transceivers=[];}
  addTransceiver(kind,options){this.transceivers.push({kind,...options});}
  async createOffer(){assert.deepEqual(this.transceivers.map(x=>x.kind),['video','audio']);assert(this.transceivers.every(x=>x.direction==='recvonly'));return {type:'offer',sdp:'v=0\r\n'};}
  async setLocalDescription(){this.iceGatheringState='gathering';if(mode==='gather-timeout')return;
   setTimeout(()=>{this.localDescription={type:'offer',sdp:mode==='no-candidate'?sdp.replace(/^a=candidate:.*\r\n/gm,''):sdp};this.iceGatheringState='complete';this.dispatchEvent(new Event('icegatheringstatechange'));},5);}
  async setRemoteDescription(answer){this.remoteDescription=answer;this.connectionState='connecting';this.iceConnectionState='checking';
   if(mode==='checking')return;
   setTimeout(()=>{this.connectionState='connected';this.iceConnectionState='completed';this.dispatchEvent(new Event('connectionstatechange'));},5);}
  close(){this.connectionState='closed';this.iceConnectionState='closed';this.dispatchEvent(new Event('connectionstatechange'));}
 }
 const window={RTCPeerConnection:PC,AVATAR_CONFIG:{iceGatheringTimeoutMs:30,iceConnectionTimeoutMs:30,offerTimeoutMs:30,reconnectBackoffConnectMs:100000},addEventListener(){},console:{error(){}}};
 const fetch=async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});
  if(mode==='offer-timeout'&&url.endsWith('/offer'))return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
  return {ok:true,json:async()=>url.endsWith('/offer')?{sessionid:'ice-test',type:mode==='bad-answer'?'offer':'answer',sdp}:{code:0}};};
 const tracked=(fn,ms)=>{const t=setTimeout(fn,ms);timers.add(t);return t;};
 vm.runInNewContext(code,{window,document,fetch,Map,Set,Promise,AbortController,MediaStream:class{},setInterval(){},setTimeout:tracked,clearTimeout,console});
 return {A:window.AvatarSync,pcs,requests,dispose(){for(const t of timers)clearTimeout(t);}};
}
(async()=>{
 const results=[];
 for(const mode of ['success','gather-timeout','no-candidate','checking','bad-answer','offer-timeout']){
  const x=fixture(mode);try{
   if(mode==='success'){assert.equal(await x.A.connect(),'ice-test');assert.equal(x.requests[0].body.sdp,sdp);await x.A.disconnect();}
   else {await assert.rejects(x.A.connect(),mode==='gather-timeout'?/gathering timeout/:mode==='no-candidate'?/no ICE candidates/:mode==='checking'?/connection timeout/:mode==='offer-timeout'?/offer request timeout/:/Invalid remote answer/);await x.A.disconnect();}
   assert(x.pcs.every(p=>p.connectionState==='closed'));
   const offers=x.requests.filter(r=>r.url.endsWith('/offer')),releases=x.requests.filter(r=>r.url.endsWith('/api/session/release'));
   assert.equal(offers.length,['gather-timeout','no-candidate'].includes(mode)?0:1);
   assert.equal(releases.length,mode==='offer-timeout'?0:offers.length);
   if(mode!=='success')assert(x.A.diagnostics().negotiation.error);
   results.push({mode,result:'PASS',offers:offers.length,releases:releases.length});
  }finally{x.dispose();}
 }
 console.log(JSON.stringify({kind:'MOCK_NEGOTIATION_ONLY',results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
