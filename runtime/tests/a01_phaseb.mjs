/**
 * A01 Phase B — automated acceptance. Subcommands:
 *   node a01_phaseb.mjs refresh20   — 20 reloads, final active == 1 (ours), no rejection
 *   node a01_phaseb.mjs multitab    — 4+1 pages vs capacity, rejection then recovery
 *   node a01_phaseb.mjs crash       — hard-kill browser mid-session, grace auto-release
 *   node a01_phaseb.mjs soak        — 50 rounds open→connect→close, no leak drift
 *
 * NOTE: an external session (user's own page) may exist; all assertions use deltas.
 */
import {
  launchBrowser, waitAvatar, sendText, saveShot, sessions, log, EV,
} from "./a01_harness.mjs";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const cmd = process.argv[2] || "refresh20";
const verdict = {};

async function connectPage(page, timeoutMs = 30000) {
  // Lazily connected: a text turn is what creates the 8010 session.
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  const t0 = Date.now();
  const before = await sessions();
  await sendText(page, "嗯").catch(() => {});
  while (Date.now() - t0 < timeoutMs) {
    const s = await sessions();
    if (s.active > before.active) return { connected: true, before: before.active, now: s.active };
    await page.waitForTimeout(500);
  }
  return { connected: false, before: before.active, now: (await sessions()).active };
}

async function closeAndCount(page, baseline, timeoutMs = 15000) {
  const t0 = Date.now();
  await page.close().catch(() => {});
  while (Date.now() - t0 < timeoutMs) {
    await new Promise(r => setTimeout(r, 600));
    const s = await sessions();
    if (s.active <= baseline) return { released: true, seconds: +((Date.now() - t0) / 1000).toFixed(1), active: s.active };
  }
  return { released: false, seconds: +((Date.now() - t0) / 1000).toFixed(1), active: (await sessions()).active };
}

if (cmd === "refresh20") {
  const browser = await launchBrowser();
  const page = await browser.newPage();
  try {
    const c = await connectPage(page);
    log("b_refresh_connect", c);
    verdict.initialConnect = c.connected;
    const baseline = c.now;
    const samples = [];
    let maxActive = baseline;
    for (let i = 1; i <= 20; i++) {
      await page.reload({ waitUntil: "domcontentloaded" });
      // re-establish after reload
      const t0 = Date.now();
      let ok = false;
      while (Date.now() - t0 < 25000) {
        const av = await waitAvatar(page, 1).catch(() => ({ ok: false }));
        const s = await sessions();
        if (av.ok && s.active >= baseline) { ok = true; break; }
        await sendText(page, "嗯").catch(() => {});
        await page.waitForTimeout(800);
      }
      const s = await sessions();
      maxActive = Math.max(maxActive, s.active);
      samples.push({ i, ok, active: s.active, rejected: s.totals.rejected_offers });
      log("b_refresh_round", samples[i - 1]);
    }
    const final = await sessions();
    verdict.rounds = 20;
    verdict.refreshOkRounds = samples.filter(x => x.ok).length;
    verdict.maxActive = maxActive;
    verdict.finalActive = final.active;
    verdict.rejectedTotal = final.totals.rejected_offers;
    verdict.PASS = verdict.refreshOkRounds === 20 && verdict.maxActive <= 6 && verdict.finalActive === baseline
      && final.totals.rejected_offers === 0;
    await saveShot(page, "b_refresh_final");
    log("b_refresh_verdict", verdict);
    await page.close().catch(() => {});
  } finally {
    await browser.close().catch(() => {});
  }
}

if (cmd === "multitab") {
  const browser = await launchBrowser();
  try {
    const s0 = await sessions();
    const baseline = s0.active; // includes any external session
    const pages = [];
    // open 4 pages (baseline+4 == max_session when external=1)
    for (let i = 0; i < 4; i++) {
      const p = await browser.newPage();
      const c = await connectPage(p);
      pages.push(p);
      log("b_mt_open", { i, ...c });
    }
    const sFull = await sessions();
    verdict.atCapacity = sFull.active;
    // 6th (our 5th) should be rejected
    const p6 = await browser.newPage();
    await p6.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
    const rejBefore = (await sessions()).totals.rejected_offers;
    await sendText(p6, "嗯").catch(() => {});
    await page6Wait(p6);
    const sRej = await sessions();
    verdict.rejectedDelta = sRej.totals.rejected_offers - rejBefore;
    verdict.capacityPageConnected = sRej.active > sFull.active;
    await saveShot(p6, "b_mt_rejected_page");
    await p6.close().catch(() => {});
    log("b_mt_reject", { ...verdict });

    // close one page → capacity freed → new page connects
    const t0 = Date.now();
    await pages[3].close().catch(() => {});
    let freed = null;
    while (Date.now() - t0 < 15000) {
      await new Promise(r => setTimeout(r, 600));
      freed = await sessions();
      if (freed.active < sRej.active) break;
    }
    const p7 = await browser.newPage();
    const c7 = await connectPage(p7);
    verdict.reconnectAfterRelease = c7.connected;
    log("b_mt_reconnect", c7);
    for (const p of pages) await p.close().catch(() => {});
    await p7.close().catch(() => {});
    const sEnd = await closeAll(s => sessions());
    verdict.PASS = verdict.rejectedDelta >= 1 && !verdict.capacityPageConnected && verdict.reconnectAfterRelease;
    log("b_mt_verdict", { ...verdict, final: sEnd });

    async function page6Wait(p) {
      // give the offer a moment to be attempted and rejected
      for (let i = 0; i < 8; i++) {
        const s = await sessions();
        if (s.totals.rejected_offers > rejBefore) return true;
        await p.waitForTimeout(700);
      }
      return false;
    }
    async function closeAll() { const s = await sessions(); return s.active; }
  } finally {
    await browser.close().catch(() => {});
  }
}

if (cmd === "crash") {
  // dedicated browser killed hard — no pagehide, no release beacon
  const browser = await launchBrowser();
  const page = await browser.newPage();
  try {
    const c = await connectPage(page);
    verdict.connected = c.connected;
    log("b_crash_connect", c);
    await saveShot(page, "b_crash_live");
    const before = await sessions();
    // hard kill the browser process tree (SIGKILL equivalent) — no unload handlers run
    try { process.kill(-browser._process?.pid ?? 0); } catch {}
    try { browser.process().kill("SIGKILL"); } catch {}
    verdict.killed = true;
    log("b_crash_killed", { beforeActive: before.active });
    const t0 = Date.now();
    let after = null;
    while (Date.now() - t0 < 60000) {
      await new Promise(r => setTimeout(r, 800));
      after = await sessions();
      if (after.active < before.active) break;
    }
    verdict.graceReleaseSeconds = +((Date.now() - t0) / 1000).toFixed(1);
    verdict.releasedTo = after.active;
    verdict.PASS = verdict.graceReleaseSeconds <= 45 && after.active < before.active;
    log("b_crash_verdict", verdict);
  } catch (e) {
    verdict.ERROR = String(e).slice(0, 200);
    log("b_crash_error", verdict);
  }
}

if (cmd === "soak") {
  const browser = await launchBrowser();
  try {
    const s0 = await sessions();
    const baseline = s0.active;
    const results = [];
    for (let i = 1; i <= 50; i++) {
      const p = await browser.newPage();
      const c = await connectPage(p, 25000);
      const peak = await sessions();
      const rel = await closeAndCount(p, baseline, 15000);
      results.push({ i, connected: c.connected, activeAtPeak: peak.active, released: rel.released, relSec: rel.seconds });
      log("b_soak_round", results[i - 1]);
    }
    const sEnd = await sessions();
    verdict.rounds = 50;
    verdict.connectedRounds = results.filter(x => x.connected).length;
    verdict.releasedRounds = results.filter(x => x.released).length;
    verdict.maxActiveSeen = Math.max(...results.map(x => x.activeAtPeak));
    verdict.finalActive = sEnd.active;
    verdict.baseline = baseline;
    verdict.drift = sEnd.active - baseline;
    verdict.totalsEnd = sEnd.totals;
    verdict.PASS = verdict.connectedRounds >= 48 && verdict.releasedRounds === 50
      && verdict.maxActiveSeen <= 6 && verdict.drift === 0;
    log("b_soak_verdict", verdict);
  } finally {
    await browser.close().catch(() => {});
  }
}

writeFileSync(join(EV, `b_${cmd}_verdict.json`), JSON.stringify(verdict, null, 2));
console.log(`B_${cmd.toUpperCase()}_VERDICT`, JSON.stringify(verdict));
