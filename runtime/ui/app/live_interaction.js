/* Additive live input adapter. Existing WebSocket/session/AudioRouter own output. */
(function () {
  'use strict';
  const base = 'http://127.0.0.1:8781';
  const clientId = crypto.randomUUID();
  const NativeSocket = window.WebSocket;
  let socket = null, active = null, privateBusy = false, privateUntil = 0;
  let sessionId = '';
  let responseId = '', text = '', bytes = 0, generationDone = false;
  let enabled = false, interval = 600, pollBusy = false, closed = false;
  const observed = new Set(), phases = new Set();
  const liveResponses = new Set();
  let dispatchTimeout = 90000, routerAcceptedAt = 0, soundSeen = false;
  let localQueuedMs = 0, localPlayed = 0, playedBaseline = 0;
  const NativeWorklet = window.AudioWorkletNode;
  if (NativeWorklet) window.AudioWorkletNode = new Proxy(NativeWorklet, {construct(Target,args) {
    const node = new Target(...args);
    if (args[1] === 'audio-playback') {
      node.port.addEventListener('message', event => {
        if (event.data?.kind === 'stats') {localQueuedMs=event.data.queuedMs||0; localPlayed=event.data.played||0;}
      });
      node.port.start();
    }
    return node;
  }});
  let pendingPhases = Promise.resolve();
  const api = async (path, data) => {
    const res = await fetch(base + path, {method: data ? 'POST' : 'GET',
      headers: data ? {'Content-Type':'application/json'} : {}, body: data ? JSON.stringify(data) : undefined,
      signal: AbortSignal.timeout(5000)});
    if (!res.ok) throw new Error('LIVE_HTTP_' + res.status);
    return res.json();
  };
  function phase(name, data = {}) {
    if (!active || (phases.has(name) && name !== 'llm_done')) return;
    phases.add(name);
    const payload = {client_id: clientId, ticket: active.ticket, phase: name, data};
    pendingPhases = pendingPhases.then(async () => {
      for (let attempt=0; attempt<3; attempt++) {
        try { await api('/phase', payload); return; }
        catch (error) {
          if (attempt===2) console.warn('[live] receipt failed', name, error.message);
          else await new Promise(resolve=>setTimeout(resolve, 250*(attempt+1)));
        }
      }
    });
  }
  function finish(name, data = {}) {
    if (!active) return;
    phase(name, {reply:text, audio_bytes:bytes, ...data});
    active = null; responseId = ''; generationDone = false;
  }
  function wire(ws) {
    socket = ws;
    const originalSend = ws.send.bind(ws);
    ws.send = function (raw) {
      try {
        const event = JSON.parse(raw);
        if (event.type === 'conversation.item.create' || event.type === 'input_audio_buffer.commit') {
          privateUntil = Date.now() + 3000;
          if (active && responseId) {
            originalSend(JSON.stringify({type:'response.cancel'}));
            finish('cancelled', {reason:'owner_input'});
          }
        }
      } catch (_) { /* binary or non-protocol messages */ }
      return originalSend(raw);
    };
    ws.addEventListener('message', event => {
      let data; try { data = JSON.parse(event.data); } catch (_) { return; }
      if (data.type === 'session.created') sessionId = data.session?.id || '';
      if (data.type === 'input_audio_buffer.speech_started') {
        privateUntil = Date.now() + 3000;
        if (active) finish('cancelled', {reason:'owner_speech'});
      }
      if (data.type === 'response.created') {
        const ticket = data.response?.metadata?.live_ticket;
        if (active && ticket === active.ticket) {
          responseId = data.response.id; liveResponses.add(responseId);
          if(liveResponses.size>256)liveResponses.delete(liveResponses.values().next().value);
        }
        else privateBusy = true;
      }
      const mine = active && responseId && (data.response_id === responseId || data.response?.id === responseId);
      if (mine) {
        if (['response.output_audio_transcript.delta','response.output_text.delta','response.text.delta'].includes(data.type)) {
          phase('llm_first_token'); text += data.delta || '';
        }
        if (data.type === 'response.output_audio_transcript.done') {
          phase('llm_first_text_block'); text += data.transcript || ''; phase('llm_done', {reply:text});
        }
        if (data.type === 'response.output_audio.delta') {
          phase('tts_start'); phase('audio_generated');
          bytes += Math.floor((data.delta || '').length * 3 / 4);
        }
        if (data.type === 'response.done') {
          generationDone = true;
          if (data.response.status !== 'completed') finish('failed', {reason:data.response.status});
        }
      }
      if (data.type === 'response.done' && !mine) privateBusy = false;
      if (data.type === 'error' && active) finish('failed', {reason:data.error?.type || 'realtime_error'});
    });
    ws.addEventListener('close', () => {
      if (socket === ws) { finish('failed', {reason:'socket_closed'}); socket = null; privateBusy = false; }
    });
  }
  // Wrap the already-installed Avatar socket wrapper; no second realtime connection.
  window.WebSocket = new Proxy(NativeSocket, {construct(Target, args) {
    const ws = new Target(...args);
    if (String(args[0]).includes('/v1/realtime')) wire(ws);
    return ws;
  }});
  function audioBusy() {
    const d = window.s2sAudioRouter?.diagnostics();
    const unfinished = (d?.openTurns || []).some(t => !t.cancelled && (!t.ended || ['TTS_READY','AUDIO_GENERATED','AUDIO_PLAYING'].includes(t.state)));
    const av = window.AvatarSync?.diagnostics?.();
    return unfinished || Boolean(av?.pendingUploads?.length) || localQueuedMs > 0;
  }
  function observeAudio() {
    const router = window.s2sAudioRouter;
    if (!router || observed.has(router)) return;
    observed.add(router);
    router.on('state', event => {
      if (active && event.responseId === responseId && event.state === 'AUDIO_PLAYING')
        {routerAcceptedAt=Date.now(); phase('router_accepted', {sink:event.sink, played_bytes:event.playedBytes});}
    });
  }
  async function poll() {
    if (pollBusy || closed) return;
    pollBusy = true;
    try {
      observeAudio();
      const state = await api('/status');
      enabled = state.LIVE_INTERACTION_ENABLED === 'ON';
      interval = state.frontend_poll_ms || 600;
      dispatchTimeout = (state.dispatch_timeout || 90) * 1000;
      render(state);
      if (active && !enabled) {
        // OFF cancels only this adapter's current reply, never a private reply.
        if (socket?.readyState === 1 && responseId) socket.send(JSON.stringify({type:'response.cancel'}));
        finish('cancelled', {reason:'live_disabled'});
      }
      if (active && Date.now() - active.startedAt > dispatchTimeout) {
        if (socket?.readyState === 1 && responseId) socket.send(JSON.stringify({type:'response.cancel'}));
        finish('failed', {reason:'dispatch_timeout'});
      }
      if (active && generationDone) {
        const audible = window.s2sAudioRouter?.turnAudibility(responseId);
        let speaking = false;
        if (audible?.sink === 'avatar' && window.AvatarSync?.sessionid) {
          const url = window.AvatarSync.config.livetalkingBase.replace(/\/+$/,'') + '/is_speaking';
          const res = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sessionid:window.AvatarSync.sessionid}),signal:AbortSignal.timeout(3000)});
          const body=await res.json();
          if(body.code!==0)throw new Error('AVATAR_PLAYBACK_STATUS_UNKNOWN');
          speaking = Boolean(body.data);
        } else speaking = localPlayed > playedBaseline && localQueuedMs > 0;
        if(speaking && !soundSeen){soundSeen=true;phase('audio_start',{sink:audible?.sink});}
        // PCM duration is measured from bytes at the existing 24 kHz mono s16
        // output, preventing a synthesis receipt from releasing playback early.
        const tailOver = routerAcceptedAt && Date.now() >= routerAcceptedAt + bytes / 48;
        if (!audioBusy() && !speaking && tailOver) finish(audible?.audible ? 'completed' : 'failed', {audible:Boolean(audible?.audible), sink:audible?.sink, sound_observed:soundSeen});
      }
      // MODE_SYSTEM_V1 §33：朗读模式下不让普通弹幕把故事打碎。本轮不设计「主播朗读
      // + 弹幕抢答」的策略，只做这一条隔离 —— 模式是 READING 时干脆不去 claim。
      // 当前模式由 main.js 从 /api/mode 读到后挂在 window 上，这里只读不写。
      const readingNow = String(window.__aiGirlfriendMode || '') === 'READING';
      if (enabled && !readingNow && !active && !privateBusy && Date.now() >= privateUntil && !audioBusy() && socket?.readyState === 1) {
        await pendingPhases;
        const result = await api('/claim', {client_id:clientId, ready:true, private_busy:false,s2s_session_id:sessionId});
        if (result.job) {
          active = {...result.job, startedAt:Date.now()}; text = ''; bytes = 0; phases.clear();
          routerAcceptedAt=0; soundSeen=false; playedBaseline=localPlayed;
          responseId = ''; generationDone = false;
          // Recheck after await: the owner may have spoken while claiming.
          if (privateBusy || Date.now() < privateUntil || audioBusy() || socket?.readyState !== 1) {
            finish('cancelled', {reason:'owner_won_dispatch_race'});
          } else {
            socket.send(JSON.stringify(active.request)); phase('llm_start');
          }
        }
      }
    } catch (error) {
      const el = document.getElementById('live-status');
      if (el) el.textContent = '直播服务未启动';
      const toggle = document.getElementById('live-toggle');
      if (toggle) toggle.disabled = true;
    } finally { pollBusy = false; if (!closed) setTimeout(poll, interval); }
  }
  let panel;
  function render(state) {
    if (!panel) {
      panel = document.createElement('aside'); panel.id = 'live-interaction-panel';
      panel.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:5000;background:#161a23;color:white;padding:12px;border-radius:12px;font:13px sans-serif;max-width:280px;box-shadow:0 2px 12px #0008';
      panel.innerHTML = '<strong>直播互动</strong> <button id="live-toggle">开启</button><div id="live-status"></div><div id="live-comment"></div><div id="live-reply"></div>';
      panel.hidden = true;
      document.body.appendChild(panel);
      panel.querySelector('#live-toggle').onclick = async () => {
        try { await api('/control', {enabled:!enabled}); } catch (_) {}
      };
    }
    panel.querySelector('#live-toggle').disabled = state.available === false;
    panel.querySelector('#live-toggle').textContent = enabled ? '关闭' : '开启';
    panel.querySelector('#live-status').textContent = `${enabled?'开启':'关闭'} · ${state.collector_connected?'评论已连接':'评论未连接'} · 队列 ${state.queue_depth}`;
    const comment = state.recent_comments?.at(-1);
    panel.querySelector('#live-comment').textContent = comment ? `${comment.username}: ${comment.content}` : '';
    panel.querySelector('#live-reply').textContent = state.recent_replies?.at(-1)?.reply || '';
  }
  window.liveInteraction = {isLiveResponse:id=>liveResponses.has(id),status:()=>({enabled,active:active?.ticket,responseId,bytes,text,privateBusy,localQueuedMs}), control:value=>api('/control',{enabled:value})};
  window.addEventListener('pagehide', () => {closed=true; finish('failed',{reason:'page_closed'});});
  function start() {
    render({available:false, collector_connected:false, queue_depth:0});
    panel.querySelector('#live-status').textContent = '正在检查直播服务…';
    document.getElementById('live-interaction-open')?.addEventListener('click', event => {
      panel.hidden = !panel.hidden;
      event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
    });
    poll();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true}); else start();
})();
