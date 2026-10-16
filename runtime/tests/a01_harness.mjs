/**
 * A01_FINAL_ACCEPTANCE harness — shared browser automation + evidence utils.
 * Evidence dir: runtime/audit/system_v2/logs/a01_final_acceptance/
 */
import { chromium } from "playwright-core";
import { mkdirSync, appendFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const PROJ = "C:/AI-Girlfriend";
export const EV = join(PROJ, "runtime/audit/system_v2/logs/a01_final_acceptance");
mkdirSync(EV, { recursive: true });

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
export const PAGE_URL = "http://127.0.0.1:7860/";
export const S2S_ADMIN = "http://127.0.0.1:8010/api/admin/sessions";
export const POOL_URL = "http://127.0.0.1:8765/v1/pool";

export function log(name, data) {
  const rec = { ts: new Date().toISOString(), name, ...data };
  appendFileSync(join(EV, "a01_events.jsonl"), JSON.stringify(rec) + "\n");
  console.log(JSON.stringify(rec));
}

export function saveShot(page, tag) {
  const p = join(EV, `${tag}_${Date.now()}.png`);
  return page.screenshot({ path: p, fullPage: false }).then(() => p);
}

export async function sessions() {
  const r = await fetch(S2S_ADMIN, { signal: AbortSignal.timeout(8000) });
  const j = await r.json();
  const d = j.data || j;
  return {
    active: d.active_sessions,
    max: d.max_session,
    totals: d.totals,
    list: (d.sessions || []).map(s => ({ id: s.session_id, pc: s.pc_state, age: s.age })),
  };
}

export async function pool() {
  const r = await fetch(POOL_URL, { signal: AbortSignal.timeout(8000) });
  return await r.json();
}

export async function launchBrowser(opts = {}) {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: opts.headless ?? false,
    args: [
      "--autoplay-policy=no-user-gesture-required",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-session-crashed-bubble",
      "--window-size=1600,900",
    ],
    viewport: { width: 1600, height: 900 },
  });
  return browser;
}

/** Probe every <video> element; return the avatar stream one if any. */
export async function videoProbe(page) {
  return await page.evaluate(() => {
    const vids = [...document.querySelectorAll("video")];
    return vids.map(v => {
      const st = v.srcObject;
      const vt = st && st.getVideoTracks ? st.getVideoTracks()[0] : null;
      const at = st && st.getAudioTracks ? st.getAudioTracks() : [];
      return {
        id: v.id || v.className || "anon",
        srcObject: Boolean(st),
        trackLive: vt ? vt.readyState : "none",
        videoWidth: v.videoWidth || 0,
        videoHeight: v.videoHeight || 0,
        currentTime: Number.isFinite(v.currentTime) ? +v.currentTime.toFixed(3) : -1,
        paused: v.paused,
        muted: v.muted,
        audioTracks: at.length,
        audioEnabled: at[0] ? at[0].enabled : null,
        streamId: st ? st.id : null,
      };
    });
  });
}

/** Two video probes separated by ms — used to prove currentTime growth. */
export async function videoGrowth(page, ms = 2500) {
  const a = await videoProbe(page);
  await page.waitForTimeout(ms);
  const b = await videoProbe(page);
  return { a, b, dt: ms };
}

/** Sample audio RMS from the avatar WebRTC stream (or local audio element fallback).
 * Stream path: analyser taps the MediaStream (non-invasive, playback unaffected).
 * Local path: captureStream() on the <audio> element — never createMediaElementSource,
 * which would permanently hijack the element's output into our context and mute it. */
export async function audioRms(page, ms = 2500) {
  return await page.evaluate(async (ms) => {
    const v = [...document.querySelectorAll("video")].find(x => {
      if (!x.srcObject) return false;
      const at = (x.srcObject.getAudioTracks?.() || [])[0];
      return at && at.readyState === "live";
    });
    let srcDesc;
    let ctx = new AudioContext();
    try {
      await ctx.resume();
      let src;
      if (v) {
        src = ctx.createMediaStreamSource(v.srcObject);
        srcDesc = "webrtc_stream";
      } else {
        const a = document.querySelector("audio");
        if (!a) { await ctx.close(); return { error: "no_live_stream_no_audio_el" }; }
        const cs = (a.captureStream || a.mozCaptureStream) ? a.captureStream() : null;
        if (!cs || cs.getAudioTracks().length === 0) {
          // no tap-able output — report element state only
          await ctx.close();
          return {
            error: "no_capture_stream",
            el: { tag: "audio", paused: a.paused, currentTime: +a.currentTime.toFixed(2),
                  duration: +Number(a.duration).toFixed(2), ended: a.ended },
          };
        }
        src = ctx.createMediaStreamSource(cs);
        srcDesc = "audio_element_capture";
      }
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an); // analyser only — does not affect playback
      const buf = new Float32Array(an.fftSize);
      let peak = 0, sum = 0, n = 0;
      const t0 = performance.now();
      while (performance.now() - t0 < ms) {
        an.getFloatTimeDomainData(buf);
        let s2 = 0;
        for (let i = 0; i < buf.length; i++) s2 += buf[i] * buf[i];
        const rms = Math.sqrt(s2 / buf.length);
        if (rms > peak) peak = rms;
        sum += rms; n++;
        await new Promise(r => setTimeout(r, 40));
      }
      const at = v ? (v.srcObject.getAudioTracks()[0]) : null;
      return {
        peakRms: +peak.toFixed(5),
        avgRms: +(sum / Math.max(n, 1)).toFixed(5),
        samples: n,
        source: srcDesc,
        audioTrack: at ? { readyState: at.readyState, enabled: at.enabled, muted: at.muted } : null,
        sampleRate: ctx.sampleRate,
      };
    } finally {
      try { await ctx.close(); } catch {}
    }
  }, ms);
}

/** Wait until a live avatar video appears (srcObject + live track + nonzero frame). */
export async function waitAvatar(page, timeoutMs = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const vids = await videoProbe(page);
    const ok = vids.find(v => v.srcObject && v.trackLive === "live" && v.videoWidth > 0);
    if (ok) return { ok: true, waitedMs: Date.now() - t0, videos: vids };
    await page.waitForTimeout(600);
  }
  return { ok: false, waitedMs: Date.now() - t0, videos: await videoProbe(page) };
}

/** Send a text line through the girlfriend composer. Returns false if rejected. */
export async function sendText(page, text) {
  const input = page.locator("#gf-composer-input");
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.fill(text);
  await input.press("Enter");
  return await page.evaluate(() => {
    const i = document.querySelector("#gf-composer-input");
    return i ? i.value === "" : null; // cleared === accepted
  });
}

/** Poll audio RMS until peak exceeds threshold (TTS reply arriving/playing). */
export async function waitAudioEnergy(page, { timeoutMs = 90000, threshold = 0.008, windowMs = 1500 } = {}) {
  const t0 = Date.now();
  let best = { peakRms: 0 };
  while (Date.now() - t0 < timeoutMs) {
    const r = await audioRms(page, windowMs).catch(e => ({ error: String(e).slice(0, 120) }));
    if (r && r.peakRms > (best.peakRms || 0)) best = r;
    if (r && r.peakRms >= threshold) return { hit: true, waitedMs: Date.now() - t0, ...r };
    await page.waitForTimeout(300);
  }
  return { hit: false, waitedMs: Date.now() - t0, best };
}

/** Collect visible chat bubbles / caption text as conversation evidence. */
export async function chatEvidence(page) {
  return await page.evaluate(() => {
    const grab = (sel) => [...document.querySelectorAll(sel)].map(e => (e.textContent || "").trim()).filter(Boolean);
    const bubbles = grab("[class*='bubble'],[class*='msg'],[class*='chat'] [class*='item']");
    const caption = document.querySelector("[class*='caption'],[id*='caption']");
    return { bubbles: bubbles.slice(-6), caption: caption ? caption.textContent.trim() : null };
  });
}
