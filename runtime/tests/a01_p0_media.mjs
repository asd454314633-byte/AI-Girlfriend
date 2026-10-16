/**
 * A01 P0 v2 — correct order: text chat starts the session; avatar video follows.
 * Evidence: LIVE_CONVERSATION, USER_VISIBLE_AVATAR (real frames + currentTime growth),
 * REAL_AUDIO_PLAYING (RMS from WebRTC stream), session release on page close.
 */
import {
  launchBrowser, waitAvatar, videoGrowth, sendText, waitAudioEnergy,
  chatEvidence, saveShot, sessions, log, EV,
} from "./a01_harness.mjs";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const browser = await launchBrowser();
const page = await browser.newPage();
const verdict = {};

try {
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  log("p0_open", {});

  const sent = await sendText(page, "你好，请用一句话介绍你自己");
  log("p0_text_sent", { accepted: sent });

  const av = await waitAvatar(page, 60000);
  verdict.AVATAR_VIDEO_LIVE = av.ok;
  const v0 = (av.videos || []).find(v => v.srcObject) || {};
  verdict.VIDEO_DIMENSIONS = `${v0.videoWidth}x${v0.videoHeight}`;
  verdict.AVATAR_WAIT_MS = av.waitedMs;
  log("p0_avatar_wait", { ok: av.ok, waitedMs: av.waitedMs, dims: verdict.VIDEO_DIMENSIONS });
  await saveShot(page, "p0_avatar");

  const growth = await videoGrowth(page, 2500);
  const ga = (growth.a).find(v => v.srcObject) || {};
  const gb = (growth.b).find(v => v.srcObject) || {};
  verdict.VIDEO_CURRENTTIME_GROWING = ga.currentTime >= 0 && gb.currentTime > ga.currentTime;
  verdict.CT_A = ga.currentTime; verdict.CT_B = gb.currentTime;
  verdict.AUDIO_TRACK_ON_STREAM = (ga.audioTracks || 0) > 0;
  log("p0_video_growth", { a: ga, b: gb });
  await saveShot(page, "p0_growth");

  // Second turn: measure audio energy while the reply is being spoken.
  await sendText(page, "再随便说一句短一点的话");
  const audio = await waitAudioEnergy(page, { timeoutMs: 90000, threshold: 0.008, windowMs: 1500 });
  verdict.AUDIO_ENERGY_HIT = audio.hit;
  verdict.AUDIO_PEAK_RMS = audio.peakRms;
  verdict.AUDIO_AVG_RMS = audio.avgRms;
  verdict.AUDIO_TRACK_STATE = audio.audioTrack;
  log("p0_audio_energy", audio);
  await saveShot(page, "p0_reply_audio");

  const chat = await chatEvidence(page);
  verdict.CHAT_BUBBLES = chat.bubbles;
  log("p0_chat_evidence", chat);

  // Close page → server should release our session within ~10s (grace).
  const before = await sessions();
  await page.close();
  const t0 = Date.now();
  let after = null;
  while (Date.now() - t0 < 20000) {
    await new Promise(r => setTimeout(r, 700));
    after = await sessions();
    if (after.active <= before.active - 1) break;
  }
  verdict.SESSIONS_BEFORE_CLOSE = before.active;
  verdict.SESSIONS_AFTER_CLOSE = after.active;
  verdict.RELEASE_SECONDS = +((Date.now() - t0) / 1000).toFixed(1);
  verdict.RELEASE_OK = after.active <= before.active - 1 && after.active <= 1;
  log("p0_release", verdict);
} catch (e) {
  verdict.ERROR = String(e).slice(0, 300);
  try { await saveShot(page, "p0_error"); } catch {}
  log("p0_error", { error: verdict.ERROR });
} finally {
  await browser.close().catch(() => {});
}

writeFileSync(join(EV, "p0_verdict.json"), JSON.stringify(verdict, null, 2));
console.log("P0_VERDICT", JSON.stringify(verdict));
