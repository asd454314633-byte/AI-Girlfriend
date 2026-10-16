/**
 * A01 Phase C — AUDIO FAIL_OPEN.
 * Real fault: 8010 (avatar listener) STOPPED mid-conversation.
 * Expect: TTS reply still audible via router fail-open → local <audio> playback,
 * router counters show fail_open/to_local; then Ensure restores 8010 and the
 * page reconnects → avatar video + avatar-sink routing return.
 * Verdict: AUDIO_FAIL_OPEN.
 */
import {
  launchBrowser, waitAvatar, sendText, waitAudioEnergy, audioRms,
  saveShot, log, EV,
} from "./a01_harness.mjs";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const SUP = "C:/AI-Girlfriend\\runtime\\windows\\Avatar-Supervisor.ps1";
const PS = `${process.env.SystemRoot}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`;

function supervisor(action) {
  execFileSync(PS, ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
    "Bypass", "-File", SUP, "-Action", action, "-Json"], { timeout: 240000, stdio: "pipe" });
}

async function avatarAlive(timeoutMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch("http://127.0.0.1:8010/api/media_ready", { signal: AbortSignal.timeout(3000) });
      if (r.ok) return true;
    } catch {}
    await new Promise(r => setTimeout(r, 800));
  }
  return false;
}

async function diag(page) {
  return await page.evaluate(() => {
    try { return window.__audioRouterDiagnostics ? window.__audioRouterDiagnostics() : { missing: true }; }
    catch (e) { return { error: String(e).slice(0, 120) }; }
  });
}

const browser = await launchBrowser();
const page = await browser.newPage();
const verdict = {};

try {
  // 1. baseline: avatar path carries the audio
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sendText(page, "你好呀");
  const av1 = await waitAvatar(page, 60000);
  log("c_baseline_avatar", { ok: av1.ok });
  await sendText(page, "说个短句");
  const audio1 = await waitAudioEnergy(page, { timeoutMs: 90000, threshold: 0.008, windowMs: 1500 });
  const d1 = await diag(page);
  verdict.baseline = { audioHit: audio1.hit, peakRms: audio1.peakRms, source: audio1.source, diag: d1 && d1.counters };
  log("c_baseline", verdict.baseline);
  await saveShot(page, "c1_baseline");

  // 2. FAULT: stop 8010
  supervisor("Stop");
  verdict._8010_down = !(await avatarAlive(4000));
  log("c_8010_stopped", { down: verdict._8010_down });
  await new Promise(r => setTimeout(r, 2000));

  // 3. converse while avatar is dead — TTS must still be heard locally
  await sendText(page, "人物还在吗？随便回我一句");
  const audio2 = await waitAudioEnergy(page, { timeoutMs: 120000, threshold: 0.006, windowMs: 1500 });
  const d2 = await diag(page);
  verdict.failopen = {
    audioHit: audio2.hit, peakRms: audio2.peakRms, source: audio2.source,
    el: audio2.el, diag: d2 && { counters: d2.counters, events: (d2.events || []).slice(-6) },
  };
  log("c_failopen", verdict.failopen);
  await saveShot(page, "c2_failopen_reply");

  // 4. restore 8010 — page auto-reconnects, avatar returns
  supervisor("Ensure");
  const up = await avatarAlive(60000);
  verdict._8010_restored = up;
  log("c_8010_restored", { up });
  await sendText(page, "再回我一句短的");
  const av3 = await waitAvatar(page, 90000);
  const audio3 = await waitAudioEnergy(page, { timeoutMs: 90000, threshold: 0.008, windowMs: 1500 });
  const d3 = await diag(page);
  verdict.recovery = { avatarOk: av3.ok, audioHit: audio3.hit, source: audio3.source, diag: d3 && d3.counters };
  log("c_recovery", verdict.recovery);
  await saveShot(page, "c3_recovered");

  const counters = d3 && d3.counters || {};
  verdict.AUDIO_FAIL_OPEN =
    verdict.baseline.audioHit &&                     // sound before fault
    verdict.failopen.audioHit &&                     // sound DURING fault
    verdict._8010_down && verdict._8010_restored &&  // fault was real + recovered
    av3.ok && audio3.hit;                            // avatar path back after recovery
  log("c_verdict", verdict);
} catch (e) {
  verdict.ERROR = String(e).slice(0, 300);
  try { await saveShot(page, "c_error"); } catch {}
  log("c_error", { error: verdict.ERROR });
  try { supervisor("Ensure"); } catch {}
} finally {
  await browser.close().catch(() => {});
}

writeFileSync(join(EV, "c_verdict.json"), JSON.stringify(verdict, null, 2));
console.log("C_VERDICT", JSON.stringify(verdict));
