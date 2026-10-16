/**
 * A01 Phase C v2 — AUDIO FAIL_OPEN with full evidence chain.
 *  - baseline turn via WebRTC avatar sink (RMS on the stream)
 *  - FAULT: 8010 stopped. Next turn routes to local_browser sink:
 *      evidence A: router lastTurns — sink=local_browser, localBytes>0, ended
 *      evidence B: WASAPI loopback — real speaker output energy (system truth)
 *  - recovery: Ensure 8010 → observe auto-reconnect; if page needs a click
 *    (its own boot contract: "点击页面任意处连接数字人"), a single synthetic
 *    click is a legitimate user action — verify avatar returns, then one more
 *    turn routed to avatar with audible energy.
 */
import {
  launchBrowser, waitAvatar, sendText, waitAudioEnergy, audioRms,
  saveShot, log, EV,
} from "./a01_harness.mjs";
import { execFileSync, spawn } from "node:child_process";
import { writeFileSync, readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const SUP = "C:/AI-Girlfriend\\runtime\\windows\\Avatar-Supervisor.ps1";
const PS = `${process.env.SystemRoot}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`;
const PY = "<USER_HOME>/.workbuddy/binaries/python/envs/default/Scripts/python.exe";
const LOOPBACK = "C:/AI-Girlfriend\\runtime\\tests\\a01_loopback_all.py";

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

async function syncDiag(page) {
  return await page.evaluate(() => {
    try {
      const A = window.AvatarSync;
      if (!A) return { missing: true };
      const d = A.diagnostics();
      return {
        sessionid: d.sessionid, suspended: d.suspended,
        connectionState: d.connectionState, mediaReady: d.mediaReady,
        router: d.router && {
          counters: d.router.counters,
          lastTurns: d.router.lastTurns,
          sinks: d.router.sinks,
        },
      };
    } catch (e) { return { error: String(e).slice(0, 150) }; }
  });
}

const browser = await launchBrowser();
const page = await browser.newPage();
const verdict = {};

try {
  // 1. baseline on the avatar path
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.mouse.click(800, 400);          // boot contract: first interaction connects
  await sendText(page, "你好呀");
  await waitAvatar(page, 60000);
  await sendText(page, "说个短句");
  const a1 = await waitAudioEnergy(page, { timeoutMs: 90000, threshold: 0.008, windowMs: 1500 });
  verdict.baseline = { audioHit: a1.hit, peakRms: a1.peakRms, source: a1.source };
  log("c2_baseline", verdict.baseline);
  await saveShot(page, "c2_baseline");

  // 2. FAULT + loopback recording window (60s covers the whole outage turn)
  const loopOut = join(EV, `c2_loopback_${Date.now()}.json`);
  const rec = spawn(PY, [LOOPBACK, "55", loopOut], { stdio: "ignore" });
  supervisor("Stop");
  verdict._8010_down = !(await avatarAlive(4000));
  log("c2_8010_stopped", { down: verdict._8010_down });
  await new Promise(r => setTimeout(r, 2000));

  // 3. converse while dead; watch the router hand the turn to local sink
  await sendText(page, "人物不在了吗？随便回一句");
  const t0 = Date.now();
  let localTurn = null;
  while (Date.now() - t0 < 100000 && !localTurn) {
    const d = await syncDiag(page);
    const lt = (d.router && d.router.lastTurns) || [];
    localTurn = lt.find(t2 => t2.sink === "local_browser" && (t2.localBytes || 0) > 0 && t2.ended) || null;
    if (!localTurn) await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(12000);           // let it finish playing
  const dFail = await syncDiag(page);
  verdict.failopenSink = localTurn || (dFail.router && dFail.router.lastTurns || []).slice(-1)[0] || null;
  log("c2_failopen_sink", verdict.failopenSink);
  await saveShot(page, "c2_failopen");

  // 4. loopback truth — did the speakers actually move?
  await new Promise(r => { const w = setInterval(() => { if (!existsSync(loopOut)) return; clearInterval(w); r(); }, 800); });
  verdict.loopback = JSON.parse(readFileSync(loopOut, "utf8"));
  log("c2_loopback", verdict.loopback);

  // 5. recovery — observe auto-reconnect; a click is the page's own contract
  supervisor("Ensure");
  verdict._8010_restored = await avatarAlive(60000);
  log("c2_8010_restored", { up: verdict._8010_restored });
  let recovered = false;
  const tR = Date.now();
  let autoReconnected = false;
  while (Date.now() - tR < 90000) {
    const d = await syncDiag(page);
    const vids = await (await import("./a01_harness.mjs")).videoProbe(page);
    const live = vids.find(v => v.srcObject && v.trackLive === "live" && v.videoWidth > 0);
    if (live) { recovered = true; autoReconnected = true; break; }
    if (Date.now() - tR > 45000 && !d.suspended && !page._clicked) {
      page._clicked = true;
      await page.mouse.click(800, 400);      // the page's documented connect gesture
      log("c2_click_kick", {});
    }
    await page.waitForTimeout(2000);
  }
  verdict.recovery = { recovered, autoReconnected };
  log("c2_recovery", verdict.recovery);
  await saveShot(page, "c2_recovered");

  // 6. post-recovery turn back on the avatar path
  if (recovered) {
    await sendText(page, "再回我一句");
    const a3 = await waitAudioEnergy(page, { timeoutMs: 90000, threshold: 0.008, windowMs: 1500 });
    const d3 = await syncDiag(page);
    verdict.afterRecovery = {
      audioHit: a3.hit, source: a3.source,
      lastSink: d3.router && (d3.router.lastTurns || []).slice(-1)[0]?.sink,
      counters: d3.router && d3.router.counters,
    };
    log("c2_after_recovery", verdict.afterRecovery);
  }

  const lb = verdict.loopback || {};
  const maxPeak = Math.max(...Object.values(lb.peaks || { x: 0 }).map(v =>
    typeof v === "number" ? v : 0));
  verdict.loopbackMaxPeak = maxPeak;
  verdict.AUDIO_FAIL_OPEN =
    verdict.baseline.audioHit &&
    Boolean(verdict.failopenSink && (verdict.failopenSink.localBytes || 0) > 0) &&
    maxPeak >= 0.008 &&                                // speakers really moved during outage
    verdict._8010_down && verdict._8010_restored &&
    recovered;
  log("c2_verdict", verdict);
} catch (e) {
  verdict.ERROR = String(e).slice(0, 300);
  try { await saveShot(page, "c2_error"); } catch {}
  log("c2_error", { error: verdict.ERROR });
  try { supervisor("Ensure"); } catch {}
} finally {
  await browser.close().catch(() => {});
}

writeFileSync(join(EV, "c2_verdict.json"), JSON.stringify(verdict, null, 2));
console.log("C2_VERDICT", JSON.stringify(verdict));
