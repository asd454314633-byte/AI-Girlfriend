/**
 * A-6 Avatar Runtime Switch — real persona switch with evidence.
 *  默认人物(v02cd025713) → 备用人物(vd1a66f0b29) → verify → reload(刷新保持)
 *  → 切回默认 → verify → reload. Screenshots at every stage.
 */
import {
  launchBrowser, waitAvatar, videoProbe, saveShot, log, EV,
} from "./a01_harness.mjs";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const browser = await launchBrowser();
const page = await browser.newPage();
const verdict = {};

async function usingFlag(handle) {
  const r = await fetch("http://127.0.0.1:7860/api/avatar/list", { signal: AbortSignal.timeout(10000) });
  const d = await r.json();
  const entry = (d.列表 || []).find(a => a.编号 === handle);
  return Boolean(entry && entry.正在使用);
}

async function waitUsing(handle, timeoutMs = 240000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try { if (await usingFlag(handle)) return true; } catch {}
    await page.waitForTimeout(2500);
  }
  return false;
}

async function switchState() {
  const r = await fetch("http://127.0.0.1:7860/api/avatar/switch/state", { signal: AbortSignal.timeout(8000) });
  return await r.json();
}

async function doSwitch(handle) {
  const r = await fetch("http://127.0.0.1:7860/api/avatar/switch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ 编号: handle }),
  });
  return await r.json();
}

async function waitSwitchDone(timeoutMs = 300000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const s = await switchState();
    const busy = s["忙"];
    const result = s["结果"] || "";
    if (!busy && result) return { state: s, seconds: +((Date.now() - t0) / 1000).toFixed(0) };
    await page.waitForTimeout(3000);
  }
  return { state: await switchState(), seconds: -1, timeout: true };
}

try {
  // 0. baseline on 默认人物
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.mouse.click(800, 400);
  await sendTextSafe(page, "你好");
  const av0 = await waitAvatar(page, 60000);
  verdict.baseline = { avatarLive: av0.ok, defaultInUse: await usingFlag("v02cd025713") };
  await saveShot(page, "av0_default");
  log("av_baseline", verdict.baseline);

  // 1. switch to 备用人物
  const r1 = await doSwitch("vd1a66f0b29");
  log("av_switch_post", r1);
  const d1 = await waitSwitchDone();
  verdict.switch1 = { result: d1.state["结果"], seconds: d1.seconds, detail: (d1.state["明细"] || "").slice(0, 160) };
  log("av_switch1_done", verdict.switch1);
  const inUse1 = await waitUsing("vd1a66f0b29", 180000);
  const av1 = await waitAvatar(page, 90000);
  verdict.switch1.inUse = inUse1;
  verdict.switch1.avatarLive = av1.ok;
  await saveShot(page, "av1_backup");
  log("av_switch1_verified", { inUse: inUse1, live: av1.ok });

  // 2. 刷新保持
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.mouse.click(800, 400);
  const av2 = await waitAvatar(page, 90000);
  const keepFlag = await usingFlag("vd1a66f0b29");
  verdict.refreshKeep = { avatarLive: av2.ok, keepFlag, keep: keepFlag };
  await saveShot(page, "av2_refresh_keep");
  log("av_refresh_keep", verdict.refreshKeep);

  // 3. switch back to 默认人物 (回滚)
  const r3 = await doSwitch("v02cd025713");
  log("av_switchback_post", r3);
  const d3 = await waitSwitchDone();
  verdict.switchBack = { result: d3.state["结果"], seconds: d3.seconds };
  const inUse3 = await waitUsing("v02cd025713", 180000);
  const av3 = await waitAvatar(page, 90000);
  verdict.switchBack.inUse = inUse3;
  verdict.switchBack.avatarLive = av3.ok;
  await saveShot(page, "av3_back_default");
  log("av_switchback_verified", { inUse: inUse3, live: av3.ok });

  verdict.AVATAR_RUNTIME_SWITCH =
    verdict.switch1.result === "SWITCH_OK" && verdict.switch1.avatarLive &&
    verdict.refreshKeep.keep && verdict.refreshKeep.avatarLive &&
    verdict.switchBack.result === "SWITCH_OK" && verdict.switchBack.avatarLive;
  log("av_verdict", verdict);
} catch (e) {
  verdict.ERROR = String(e).slice(0, 300);
  try { await saveShot(page, "av_error"); } catch {}
  log("av_error", { error: verdict.ERROR });
} finally {
  await browser.close().catch(() => {});
}

function sendTextSafe(p, text) {
  return import("./a01_harness.mjs").then(h => h.sendText(p, text)).catch(() => false);
}

writeFileSync(join(EV, "av_verdict.json"), JSON.stringify(verdict, null, 2));
console.log("AV_VERDICT", JSON.stringify(verdict));
