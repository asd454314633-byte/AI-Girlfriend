// @ts-check
/**
 * 任务 §5 的验收闸门：capability-driven 前端。
 *
 * 这里不测「千问能不能画出来」，而是证明*任何人*新注册一家模型都不需要改这个页面：
 *   1. 真实 /api/voice-models 载荷 -> 按各家声明渲染；
 *   2. 验收夹具 test_fake 只声明 情绪 + 音高 -> 页面就只有这两格，绝不出现「语速」；
 *   3. 现场编造一家页面从没见过的新厂商（含 runtime_control）-> 照样渲染；
 *   4. 本地存储只当 UI 偏好：别名归一、跨模型残留键丢弃。
 *
 * 跑法（项目根目录）：
 *   node runtime/tests/test_capability_ui.mjs
 * 退出码 0=PASS，1=FAIL。pytest 没装，这里和同目录其他脚本一样自带 runner。
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const PY = path.join(ROOT, "runtime", "core", "venv", "scripts", "python.exe");

const failures = [];
/** @param {boolean} ok @param {string} what */
function check(ok, what) {
  if (!ok) failures.push(what);
  console.log(`${ok ? "  ok  " : " FAIL "} ${what}`);
}

/* ------------------------------------------------------------------ tiny DOM */

class El {
  /** @param {string} tag */
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.listeners = new Map();
    this._text = "";
    this._value = "";
    this.selected = false;
    this.style = {};
    this.classList = { toggle() {}, add() {}, remove() {} };
  }

  get className() { return this.attrs.class || ""; }
  set className(v) { this.attrs.class = String(v); }

  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
  }
  set textContent(v) { this.children = []; this._text = String(v ?? ""); }

  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  /** @param {string} type */
  fire(type) {
    for (const fn of this.listeners.get(type) || []) fn({ preventDefault() {} });
  }

  /** @param {El[]} nodes */
  replaceChildren(...nodes) { this.children = [...nodes]; }

  get options() { return this.children.filter((c) => c.tagName === "OPTION"); }
  get selectedOptions() { return this.options.filter((o) => o.selected); }
  get selectedIndex() { return this.options.findIndex((o) => o.selected); }

  get value() {
    if (this.tagName === "SELECT") {
      const hit = this.options.find((o) => o.selected) || this.options[0];
      return hit ? hit._value : "";
    }
    return this._value;
  }
  set value(v) {
    this._value = String(v ?? "");
    if (this.tagName !== "SELECT") return;
    for (const option of this.options) option.selected = option._value === this._value;
  }
}

/** @param {string[]} ids */
function installDom(ids) {
  const byId = new Map(ids.map((id) => [id, new El(id.startsWith("gf-") && id.includes("model") ? "select" : "div")]));
  /** @type {Map<string, string>} */
  const store = new Map();
  globalThis.document = /** @type {any} */ ({
    createElement: (tag) => new El(tag),
    getElementById: (id) => byId.get(id) || null,
  });
  globalThis.localStorage = /** @type {any} */ ({
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  });
  globalThis.window = /** @type {any} */ ({
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  });
  return { byId, store };
}

const IDS = [
  "gf-model", "gf-model-status", "gf-voice-model", "gf-voice-status",
  "gf-performance-fields", "gf-performance-model", "gf-performance-note",
  "gf-performance-reset", "gf-composer", "gf-composer-input", "gf-composer-send",
];

/** Titles of the widgets actually rendered into the performance panel. */
function renderedTitles(fields) {
  return (fields?.children || []).map((label) => (label.children.find((c) => c.tagName === "SPAN") || {}).textContent || "");
}

/* ------------------------------------------------------------------- payload */

/** Real registry output, straight from the same code path the browser polls. */
function serverPayload() {
  const app = path.join(ROOT, "runtime", "ui", "app");
  const code = [
    "import json, sys",
    `sys.path.insert(0, r"${app}")`,
    "import voice_models_admin as v",
    "print(json.dumps(v.state(), ensure_ascii=False))",
  ].join("\n");
  return JSON.parse(execFileSync(PY, ["-c", code], { encoding: "utf-8", cwd: ROOT, maxBuffer: 32 << 20 }));
}

/** @param {any} payload @param {string} active @param {any} [extraRow] */
async function mount(payload, active, extraRow) {
  const rows = extraRow ? [...payload.capabilities, extraRow] : payload.capabilities;
  const body = { ...payload, active, capabilities: rows, providers: rows };
  const dom = installDom(IDS);
  const seen = [];
  globalThis.fetch = /** @type {any} */ (async (url) => {
    const kind = String(url).includes("voice-models") ? body : { models: [], active: "", health: {} };
    return { ok: true, status: 200, json: async () => kind };
  });
  // Fresh module instance per scenario: the page keeps its provider list in module state.
  const mod = await import(pathToFileURL(path.join(ROOT, "runtime", "ui", "app", "ui", "girlfriend.js")).href + `?s=${Math.random()}`);
  mod.mountGirlfriend({
    sendText: async () => true,
    ensureLive: async () => true,
    setManualVoiceMode: (selection) => seen.push(selection),
    setKnownVoiceBackends: (ids) => seen.push({ backends: ids }),
  });
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r));
  return { mod, dom, seen, fields: dom.byId.get("gf-performance-fields"), selector: dom.byId.get("gf-voice-model") };
}

/* ---------------------------------------------------------------------- runs */

console.log("capability-driven UI gate");
const payload = serverPayload();
check(payload.active && Array.isArray(payload.capabilities) && payload.capabilities.length > 0,
  `/api/voice-models returns active + ${payload.capabilities?.length ?? 0} capability rows`);

const fake = payload.capabilities.find((row) => row.id === "test_fake");
check(Boolean(fake), "registry hands the page the TEST_FAKE row (no frontend knowledge needed)");
check((fake?.controls || []).length === 2, "fixture declares exactly two controls");

/* 1 — the fixture renders emotion + pitch and nothing else (§5's gate). */
{
  const { fields, seen } = await mount(payload, "test_fake");
  const titles = renderedTitles(fields);
  check(titles.length === 2, `test_fake renders 2 widgets, got ${titles.length}`);
  check(titles.includes("情绪") && titles.includes("音高"), `test_fake labels are 情绪 + 音高 (${titles.join(" / ")})`);
  check(!titles.some((t) => t.includes("语速")), "no 语速 widget leaks in (speed is undeclared)");
  const wire = (fields?.children || []).filter((l) => (l.children.find((c) => c.tagName === "SELECT") || {}).dataset?.voiceField);
  check(wire.length === 2, "both widgets are per-turn delivery controls, not file-scoped ones");
  const be = seen.find((row) => row.backends);
  check(Array.isArray(be?.backends) && be.backends.includes("test_fake") === false,
    "setKnownVoiceBackends carries enabled rows only (fixture stays off the wire)");
  const enabled = payload.capabilities.filter((row) => row.enabled);
  check(be?.backends?.length === enabled.length && enabled.every((row) => be.backends.includes(row.id)),
    `backends list equals the registry's enabled rows (${(be?.backends || []).join(",")})`);
}

/* 2 — each real provider renders its own declared set, from the same code path. */
for (const id of ["qwen3", "higgs", "fish"]) {
  const row = payload.capabilities.find((item) => item.id === id);
  if (!row) { check(false, `${id} row present`); continue; }
  const { fields } = await mount(payload, id);
  const declared = [...(row.controls || []).map((c) => c.label_zh), ...(row.runtime_controls || []).map((c) => c.label_zh)];
  const titles = renderedTitles(fields);
  check(titles.length === declared.length && declared.every((t) => titles.includes(t)),
    `${id} renders exactly its declared widgets (${titles.join(" / ") || "none"})`);
}

/* 3 — a provider the page has never heard of still renders. This is §0's falsification. */
{
  const newcomer = {
    id: "zzz_unseen", label_zh: "全新语音", enabled: true, ui_visible: true, note_zh: "页面里没有这个名字的任何分支",
    health_scheme: null, version: null,
    controls: [{ field: "speed", label_zh: "语速", type: "enum", default: "normal", runtime_change_requires_reload: false,
      values: [{ id: "normal", label_zh: "正常" }, { id: "fast", label_zh: "快" }] }],
    runtime_controls: [{ field: "delivery_preset", label_zh: "默认表演预设", type: "enum", default: "auto",
      current: "auto", runtime_change_requires_reload: false, values: [{ id: "auto", label_zh: "自动" }] }],
  };
  const { fields, mod, dom } = await mount(payload, "zzz_unseen", newcomer);
  const titles = renderedTitles(fields);
  check(titles.join("|") === "语速|默认表演预设", `unknown provider renders its own widgets (${titles.join(" / ")})`);
  const note = dom.byId.get("gf-performance-note");
  check(note?.textContent === newcomer.note_zh, "panel note is manifest text, not a hardcoded string");
  const select = (fields.children[0].children.find((c) => c.tagName === "SELECT"));
  check(select?.options.map((o) => o._value).join(",") === "normal,fast", "enum values come from the declaration");
  check(Object.keys(mod.manualSelection().manual_voice_mode).length === 0, "an untouched new provider sends no stale fields");
}

/* 4 — localStorage is a UI preference: aliases normalise, foreign keys are dropped. */
{
  const dom = installDom(IDS);
  globalThis.fetch = /** @type {any} */ (async (url) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("voice-models") ? payload : { models: [], active: "", health: {} }),
  }));
  const mod = await import(pathToFileURL(path.join(ROOT, "runtime", "ui", "app", "ui", "girlfriend.js")).href + `?s=${Math.random()}`);
  mod.mountGirlfriend({ sendText: async () => true, ensureLive: async () => true, setManualVoiceMode() {}, setKnownVoiceBackends() {} });
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r));
  const aliases = payload.state.field_aliases || {};
  const legacy = Object.fromEntries(Object.entries(aliases).map(([from, to]) => [from, to === "sound_effect" ? "老效果" : "老自定义"]));
  dom.store.set(`${payload.active}_voice_preferences`, JSON.stringify({ ...legacy, emotion: "excited", pitch: "hallucinated" }));
  const selection = mod.manualSelection();
  const sent = selection.manual_voice_mode || {};
  check(selection.tts_backend === payload.active, "selection names the active provider as the registry reports it");
  check(sent.emotion === "excited", "declared capability survives");
  check(!("pitch" in sent), `undeclared capability dropped for ${payload.active}`);
  check(Object.keys(legacy).every((k) => !(k in sent)), "legacy alias keys never reach the wire");
}

/* 5 — the page itself still names no vendor (§0's wording, checked on source). */
{
  const src = readFileSync(path.join(ROOT, "runtime", "ui", "app", "ui", "girlfriend.js"), "utf-8");
  const tokens = [...new Set(payload.capabilities.map((row) => String(row.id)))];
  const named = tokens.filter((token) => new RegExp(`(['"\`])${token}\\1`).test(src));
  check(named.length === 0, `girlfriend.js contains no provider-name branch (found: ${named.join(",") || "none"})`);
  check(!/VOICE_LABELS|VOICE_FIELDS/.test(src), "the copied label/field tables are gone");
}

console.log(failures.length ? `\nFAIL (${failures.length}): ${failures.join(" | ")}` : "\nPASS");
process.exitCode = failures.length ? 1 : 0;
