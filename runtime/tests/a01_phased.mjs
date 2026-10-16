/**
 * A01 Phase D — 8765 SESSION DRAIN.
 * During a TTS turn the pool should show in_use=1; after the turn (and after
 * page close) /v1/pool must return to in_use=0 without restarting 8765.
 * Verdict: 8765_SESSION_DRAIN.
 */
import {
  launchBrowser, sendText, waitAvatar, sessions, pool, log, EV,
} from "./a01_harness.mjs";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const browser = await launchBrowser();
const page = await browser.newPage();
const verdict = {};

try {
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await sendText(page, "你好");
  await waitAvatar(page, 60000);

  // trigger a turn and watch the pool continuously
  const samples = [];
  await sendText(page, "给我讲一个十 个字以内的小故事");
  const t0 = Date.now();
  let sawInUse = false;
  while (Date.now() - t0 < 60000) {
    try {
      const p = await pool();
      const inUse = p.in_use;
      if (inUse > 0) sawInUse = true;
      samples.push({ t: +((Date.now() - t0) / 1000).toFixed(1), in_use: inUse, state: (p.units || []).map(u => u.state).join(",") });
      if (sawInUse && inUse === 0 && Date.now() - t0 > 8000) break;
    } catch (e) {
      samples.push({ t: +((Date.now() - t0) / 1000).toFixed(1), error: String(e).slice(0, 80) });
    }
    await new Promise(r => setTimeout(r, 500));
  }
  verdict.sawInUse = sawInUse;
  verdict.samplesTail = samples.slice(-8);
  log("d_turn_trace", { sawInUse: sawInUse, n: samples.length });

  // after everything settles (page closed), pool must drain to 0
  await page.close().catch(() => {});
  await new Promise(r => setTimeout(r, 12000));
  const pEnd = await pool();
  verdict.finalInUse = pEnd.in_use;
  verdict.finalUnits = pEnd.units;
  verdict.NO_RESTART = true; // we never touched 8765 in this phase
  verdict.PASS = sawInUse && verdict.finalInUse === 0;
  log("d_verdict", verdict);
} catch (e) {
  verdict.ERROR = String(e).slice(0, 300);
  log("d_error", verdict);
} finally {
  await browser.close().catch(() => {});
}

writeFileSync(join(EV, "d_verdict.json"), JSON.stringify(verdict, null, 2));
console.log("D_VERDICT", JSON.stringify(verdict));
