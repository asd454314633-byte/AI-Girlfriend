/** Discriminator: known-audible WebRTC turn + all-device loopback capture. */
import { launchBrowser, sendText, waitAvatar, audioRms, waitAudioEnergy, log, EV } from "./a01_harness.mjs";
import { execFileSync } from "node:child_process";
import { writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";

const PY = "<USER_HOME>/.workbuddy/binaries/python/envs/default/Scripts/python.exe";
const REC = "C:/AI-Girlfriend\\runtime\\tests\\a01_loopback_all.py";

const browser = await launchBrowser();
const page = await browser.newPage();
try {
  await page.goto("http://127.0.0.1:7860/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.mouse.click(800, 400);
  await sendText(page, "你好呀");
  await waitAvatar(page, 60000);
  await sendText(page, "说一句长一点的话好吗");

  const out = join(EV, `loopback_disc_${Date.now()}.json`);
  const rec = execFileSync(PY, [REC, "25", out], { timeout: 60000, encoding: "utf8" });
  const loop = JSON.parse(readFileSync(out, "utf8"));
  const rms = await audioRms(page, 2000);
  log("disc_result", { loopPeaks: loop.peaks, inPageRms: rms });
  writeFileSync(join(EV, "disc_verdict.json"), JSON.stringify({ loopPeaks: loop.peaks, inPageRms: rms }, null, 2));
} finally {
  await browser.close().catch(() => {});
}
