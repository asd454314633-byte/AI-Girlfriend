// @ts-check
/**
 * 女友版页面控件（AI_GIRLFRIEND_PRODUCT_UI_READING_V2 · 第 2/3/5/6 项）
 *
 * 只做四件事，全部走既有服务，不起新进程：
 *   • 模型下拉：GET /api/models 读 models.json + active_model.json，
 *     POST /api/models/switch 触发唯一的切换器 Switch-Girlfriend-LLM.ps1；
 *     这里只轮询它的进度，不自己拉 llama-server，也不另管一套模型状态。
 *   • 每次回复统一输出音频，跟随当前已激活的 TTS 后端。
 *   • 文字输入框：把打字的内容经同一根 realtime WS 发出去，和语音共用一份
 *     会话与记忆。没连上时，点发送会先以"文字模式"连一次（不占麦克风）。
 *   • 陪读入口：见 ui/reading.js（下一个切片）。
 *
 * 语音模型与会话仍由现有生命周期接口管理。
 */

import { registerBrandTerms } from "./zh_cn.js";

const POLL_MS = 2000;

/** @type {{ sendText:(text:string)=>Promise<boolean>, ensureLive:()=>Promise<boolean>, setManualVoiceMode:(selection:object)=>void, setKnownVoiceBackends?:(ids:string[])=>void }} */
let hooks = {
  sendText: async () => false,
  ensureLive: async () => false,
  setManualVoiceMode: () => {},
  setKnownVoiceBackends: () => {},
};

let live = false;
/** @type {number} */ let pollTimer = 0;
let lastActive = "";

/** @param {string} id @returns {HTMLElement} */
const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

/** @param {string} text */
function setStatus(text) {
  const el = $("gf-model-status");
  if (el) el.textContent = text || "";
}

/** @param {string} url @param {object} [body] */
async function api(url, body) {
  try {
    const res = await fetch(url, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined);
    const json = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, json };
  } catch (err) {
    return { ok: false, status: 0, json: { error: String(err) } };
  }
}

/** 下拉里只放 `ui_visible !== false` 的模型：14B 是紧急回滚位，不进普通 UI。 */
function renderModels(payload) {
  const select = /** @type {HTMLSelectElement | null} */ ($("gf-model"));
  if (!select) return;
  const visible = (payload.models || []).filter((m) => m.uiVisible !== false);
  lastActive = payload.active || "";
  select.replaceChildren(
    ...visible.map((m) => {
      const opt = document.createElement("option");
      opt.value = m.key;
      opt.textContent = m.hint ? `${m.label}（${m.hint}）` : m.label;
      if (m.key === lastActive) opt.selected = true;
      return opt;
    }),
  );
  select.disabled = Boolean(payload.switch?.busy);
  if (!live) {
    if (payload.selectionError) setStatus("模型选择状态异常");
    else if (payload.health?.reachable && payload.selectionApplied === false) setStatus("当前选择与 8090 实际加载模型不一致");
    else if (!payload.health?.reachable) setStatus("8090 暂未就绪；当前选择已保留");
    else setStatus("");
  }
}

async function refreshModels() {
  const r = await api("/api/models");
  if (!r.ok) { setStatus("模型状态读不到"); return; }
  renderModels(r.json);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = 0;
}

function startPolling(targetLabel, targetKey) {
  stopPolling();
  pollTimer = window.setInterval(async () => {
    const r = await api("/api/models");
    if (!r.ok) return;
    const sw = r.json.switch || {};
    renderModels(r.json);
    if (sw.busy) { setStatus(`正在切换到 ${targetLabel}…`); return; }
    stopPolling();
    if (sw.result === "SWITCH_OK" && r.json.active === targetKey && r.json.selectionApplied === true) setStatus(`已切换到 ${targetLabel}`);
    else if (sw.result === "SWITCH_OK") setStatus("切换器已返回，但 8090 实际模型与保存选择不一致");
    else if (sw.result === "ROLLBACK_OK") setStatus(`${targetLabel} 没起来，已自动回滚`);
    else if (sw.result) setStatus(`切换失败：${sw.result}`);
    setTimeout(() => setStatus(""), 8000);
  }, POLL_MS);
  // 兜底：切换器最长 900 s 会自己收口，这里只是别把轮询漏成长期定时器。
  setTimeout(stopPolling, 960000);
}

async function onModelChange() {
  const select = /** @type {HTMLSelectElement | null} */ ($("gf-model"));
  if (!select) return;
  const target = select.value;
  const label = select.options[select.selectedIndex]?.textContent || target;
  if (target === lastActive) return;
  setStatus(`正在安排切换到 ${label}…`);
  const r = await api("/api/models/switch", { target });
  const body = r.json || {};
  if (!r.ok || !body.ok) {
    select.value = lastActive;
    const reason = body.reason || `HTTP ${r.status}`;
    setStatus(reason === "BUSY" ? "已有切换在进行中" : `切换未开始：${reason}`);
    return;
  }
  select.disabled = true;
  startPolling(label, target);
}

let voicePollTimer = 0;
// 首帧不能假设任何厂商：注册表读到谁，界面就显示谁（D-12）。
let voiceActive = '';
/** GET /api/voice-models 下发的 capability 清单，即 registry.ui_schema('TTS')。 */
let voiceProviders = [];
/** 最近一次语音状态；field_aliases / default 这些注册表事实都从这里取。 */
let voiceState = {};
let performanceSignature = '';
let voiceRefreshPending = null;

/** @param {string} id */
function voiceSpec(id) {
  return voiceProviders.find((row) => row.id === id) || null;
}

/** 显示名只有注册表一份来源；这里不再复制任何厂商名（D-13）。 */
function voiceLabel(id) {
  return (voiceSpec(id) || {}).label_zh || '语音模型';
}

const preferenceKey = (id) => `${id}_voice_preferences`;

/**
 * 本地存储只是 UI 偏好，且必须归一回注册表的规范键（任务 §8）。
 * 旧版本写过 effect / custom，别名表由服务端下发；不在该模型声明里的键一律丢弃，
 * 这样“给 A 模型设过的值”不可能被发给 B 模型。
 */
function canonicalPreferences(id, stored) {
  const aliases = ((voiceState.state || {}).field_aliases) || {};
  const spec = voiceSpec(id) || /** @type {{controls?: any[]}} */ ({});
  const allowed = new Set((spec.controls || []).map((control) => control.field));
  /** @type {Record<string, string>} */
  const merged = {};
  Object.entries(stored || {}).forEach(([key, value]) => {
    const target = aliases[key] && !(aliases[key] in (stored || {})) ? aliases[key] : key;
    if (!allowed.has(target) || typeof value !== 'string' || !value.trim()) return;
    const control = (spec.controls || []).find((row) => row.field === target);
    if (control?.type === 'enum' && !(control.values || []).some((row) => row.id === value.trim())) return;
    merged[target] = value.trim();
  });
  return merged;
}

function readPreferences(id) {
  try {
    const stored = JSON.parse(localStorage.getItem(preferenceKey(id)) || '{}');
    return stored && typeof stored === 'object' && !Array.isArray(stored) ? canonicalPreferences(id, stored) : {};
  } catch { return {}; }
}

function manualSelection() {
  return { tts_backend: voiceActive, manual_voice_mode: readPreferences(voiceActive) };
}

function syncManualVoice() { hooks.setManualVoiceMode(manualSelection()); }

/**
 * 一个控件 = 注册表里的一条 capability 声明。这里没有任何 ``if (backend === ...)``：
 * 验收夹具 test_fake 只声明 emotion + pitch，页面就只画情绪 + 音高（任务 §5）。
 */
function performanceField(control, values) {
  const field = document.createElement('label');
  field.className = 'gf-performance-field';
  const title = document.createElement('span');
  const label = control.label_zh || control.field;
  title.textContent = control.runtime_change_requires_reload ? `${label}（重新加载后生效）` : label;
  const isEnum = control.type === 'enum';
  const controlEl = /** @type {HTMLSelectElement | HTMLInputElement} */ (
    document.createElement(isEnum ? 'select' : 'input'));
  controlEl.dataset.voiceField = control.field;
  controlEl.setAttribute('aria-label', label);
  if (isEnum) {
    (control.values || []).forEach((item) => {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.label_zh || item.id;
      controlEl.append(option);
    });
  } else {
    controlEl.type = 'text';
    controlEl.maxLength = control.max_length || 160;
    controlEl.placeholder = control.placeholder_zh || '';
  }
  controlEl.value = values[control.field] || control.default || '';
  controlEl.addEventListener(isEnum ? 'change' : 'input', () => {
    const next = readPreferences(voiceActive);
    const text = String(controlEl.value || '').trim();
    if (text && text !== String(control.default ?? '')) next[control.field] = text;
    else delete next[control.field];
    try { localStorage.setItem(preferenceKey(voiceActive), JSON.stringify(next)); } catch {}
    syncManualVoice();
  });
  field.append(title, controlEl);
  return field;
}

/** 文件/进程级开关（如默认表演预设）：写在服务端状态里，不是本地偏好。 */
function runtimeControlField(control) {
  const field = document.createElement('label');
  field.className = 'gf-performance-field';
  const title = document.createElement('span');
  const label = control.label_zh || control.field;
  title.textContent = label;
  const select = /** @type {HTMLSelectElement} */ (document.createElement('select'));
  select.dataset.runtimeControl = control.field;
  select.setAttribute('aria-label', label);
  (control.values || []).forEach((item) => {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = item.label_zh || item.id;
    select.append(option);
  });
  select.value = control.current || control.default || '';
  select.addEventListener('change', async () => {
    const picked = select.selectedOptions[0];
    const body = { provider: voiceActive, control: control.field, value: select.value };
    let res = await api('/api/voice-models/runtime-control', body);
    if (res.status === 404 || res.status === 405) {
      res = await api('/api/voice-models/emotion', { emotion: select.value });
    }
    const ok = res.ok && Boolean((res.json || {}).ok);
    if (!ok) {
      select.value = control.current || control.default || '';
      voiceStatus(`设置未生效：${(res.json || {}).reason || `HTTP ${res.status}`}`);
      return;
    }
    control.current = select.value;
    voiceStatus(`${label}已设为 ${(picked && picked.textContent) || select.value}`);
    syncManualVoice();
  });
  field.append(title, select);
  return field;
}

function renderPerformance() {
  const fields = $('gf-performance-fields');
  if (!fields) return;
  const spec = voiceSpec(voiceActive);
  const name = $('gf-performance-model');
  if (name) name.textContent = `当前模型：${voiceLabel(voiceActive)}`;
  if ($('inline-voice-current')) $('inline-voice-current').textContent = voiceLabel(voiceActive);
  const note = $('gf-performance-note');
  if (note) note.textContent = (spec && spec.note_zh) || '';
  const values = readPreferences(voiceActive);
  fields.replaceChildren();
  if (!spec) {
    const waiting = document.createElement('p');
    waiting.className = 'gf-performance-note';
    waiting.textContent = '正在读取该语音模型的能力清单…';
    fields.append(waiting);
    return;
  }
  (spec.controls || []).forEach((control) => fields.append(performanceField(control, values)));
  (spec.runtime_controls || []).forEach((control) => fields.append(runtimeControlField(control)));
}

function voiceStatus(message) {
  const el = $('gf-voice-status');
  if (el) el.textContent = message;
}

/** 下拉由注册表清单生成：新增一家只需要改 manifests/tts.json（D-17）。 */
function renderVoiceOptions() {
  const selector = /** @type {HTMLSelectElement | null} */ ($('gf-voice-model'));
  if (!selector) return;
  const signature = voiceProviders.map((row) => `${row.id}:${row.label_zh}:${row.enabled}:${row.ui_visible}`).join(',');
  if (selector.dataset.schema !== signature) {
    selector.dataset.schema = signature;
    selector.replaceChildren(
      ...voiceProviders
        .filter((row) => row.enabled && row.ui_visible !== false)
        .map((row) => {
          const option = document.createElement('option');
          option.value = row.id;
          option.textContent = row.label_zh || row.id;
          return option;
        }),
    );
  }
  if (hooks.setKnownVoiceBackends) {
    hooks.setKnownVoiceBackends(voiceProviders.filter((row) => row.enabled).map((row) => row.id));
  }
}

function renderVoice(state) {
  const selector = /** @type {HTMLSelectElement | null} */ ($('gf-voice-model'));
  voiceState = state || {};
  voiceProviders = state.capabilities || state.providers || [];
  renderVoiceOptions();
  voiceActive = state.active || '';
  if (selector) {
    selector.value = voiceActive;
    selector.disabled = Boolean(state.switch?.busy);
  }
  // The selected provider can stay constant while its schema arrives or changes.
  const signature = JSON.stringify({ active: voiceActive, spec: voiceSpec(voiceActive),
    aliases: (voiceState.state || {}).field_aliases, ready: Boolean(state.ready) });
  if (signature !== performanceSignature || !$('gf-performance-fields')?.children.length) {
    performanceSignature = signature;
    renderPerformance();
    syncManualVoice();
  }
  registerBrandTerms(voiceProviders.flatMap((row) => [row.id, row.label_zh, row.display_name]));
}

/** ready 只在 mount 和用户主动切换时被读过一次：8765 受控重启、外部切换、
 *  或者页面先于服务打开，都会把"未就绪"一直挂在屏幕上。这里补一条有界的回读。 */
function readyLabel(state) {
  return `${voiceLabel(state.active)} 已就绪`;
}

let voiceReadyPoll = 0;
let voiceReadyTries = 0;

function stopReadyPolling() {
  if (voiceReadyPoll) window.clearInterval(voiceReadyPoll);
  voiceReadyPoll = 0;
  voiceReadyTries = 0;
}

function startReadyPolling() {
  if (voiceReadyPoll) return;
  voiceReadyPoll = window.setInterval(async () => {
    const r = await api('/api/voice-models');
    if (!r.ok) return;
    renderVoice(r.json);
    if (r.json.ready) {
      stopReadyPolling();
      voiceStatus(readyLabel(r.json));
      return;
    }
    if (++voiceReadyTries >= 60) {
      stopReadyPolling();
      voiceStatus('语音服务未就绪');
    }
  }, 3000);
}

async function refreshVoice() {
  if (voiceRefreshPending) return voiceRefreshPending;
  voiceRefreshPending = refreshVoiceState().finally(() => { voiceRefreshPending = null; });
  return voiceRefreshPending;
}

async function refreshVoiceState() {
  const res = await api('/api/voice-models');
  if (!res.ok) { voiceStatus('语音模型状态读取失败'); return; }
  renderVoice(res.json);
  if (res.json.activeError) {
    stopReadyPolling();
    voiceStatus('语音模型状态异常，请重新选择语音模型；不会自动回退到千问');
  } else if (res.json.ready) {
    stopReadyPolling();
    voiceStatus(readyLabel(res.json));
  } else {
    voiceStatus('模型已注册，当前尚未加载；回到聊天后自动恢复。');
    startReadyPolling();
  }
}

async function onVoiceChange() {
  const selector = /** @type {HTMLSelectElement | null} */ ($('gf-voice-model'));
  if (!selector) return;
  const target = selector.value;
  if (target === voiceActive) return;
  selector.disabled = true;
  voiceStatus(`正在关闭 ${voiceLabel(voiceActive)}…`);
  const res = await api('/api/voice-models/switch', { target });
  if (!res.ok || !res.json.ok) {
    selector.disabled = false;
    selector.value = voiceActive;
    voiceStatus(`切换未开始：${res.json.detail?.reason || res.json.reason || `HTTP ${res.status}`}`);
    return;
  }
  selector.disabled = true;
  if (voicePollTimer) clearInterval(voicePollTimer);
  const verifyUntil = Date.now() + 45000;
  voicePollTimer = window.setInterval(async () => {
    const current = await api('/api/voice-models');
    if (!current.ok) {
      if (Date.now() < verifyUntil) return;
      clearInterval(voicePollTimer);
      voicePollTimer = 0;
      selector.disabled = false;
      voiceStatus('切换结果暂时无法确认，请查看语音服务状态');
      return;
    }
    renderVoice(current.json);
    const sw = current.json.switch || {};
    const targetReady = current.json.active === target && current.json.ready;
    if (targetReady) {
      clearInterval(voicePollTimer);
      voicePollTimer = 0;
      selector.disabled = false;
      voiceStatus(sw.result === 'SWITCH_OK'
        ? `${voiceLabel(target)} 已就绪`
        : `${voiceLabel(target)} 已就绪（运行状态已核实）`);
      return;
    }
    if (sw.busy) {
      selector.disabled = true;
      voiceStatus(current.json.switch?.message || `正在加载 ${voiceLabel(target)}…`);
      return;
    }
    if (sw.result === 'SWITCH_OK' && Date.now() < verifyUntil) {
      // The lifecycle receipt can become visible just before the UI readiness
      // probe catches up. Keep the selector locked and verify the live backend.
      selector.disabled = true;
      voiceStatus(`切换器已完成，正在确认 ${voiceLabel(target)} 就绪…`);
      return;
    }
    clearInterval(voicePollTimer);
    voicePollTimer = 0;
    selector.disabled = false;
    if (sw.result === 'ROLLBACK_OK') {
      voiceStatus(`${voiceLabel(target)} 启动失败，已恢复 ${voiceLabel(current.json.active)}`);
    } else {
      const activeLabel = current.json.active ? voiceLabel(current.json.active) : '未知模式';
      voiceStatus(sw.result === 'SWITCH_OK'
        ? `切换器报告完成，但 ${voiceLabel(target)} 未通过就绪检查（当前：${activeLabel}）`
        : `切换失败，当前：${activeLabel}`);
    }
  }, POLL_MS);
}

async function onSend(event) {
  event?.preventDefault();
  const input = /** @type {HTMLTextAreaElement | null} */ ($("gf-composer-input"));
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  if (!live) {
    setStatus("");
    live = await hooks.ensureLive();
    if (!live) return;
  }
  if (!(await hooks.sendText(text))) return;
  input.value = "";
  input.style.height = "auto";
}

export function mountGirlfriend(h) {
  hooks = { ...hooks, ...h };
  const select = /** @type {HTMLSelectElement | null} */ ($("gf-model"));
  if (select) select.addEventListener("change", onModelChange);
  const voiceSelect = $('gf-voice-model');
  if (voiceSelect) voiceSelect.addEventListener('change', onVoiceChange);
  const reset = $('gf-performance-reset');
  if (reset) reset.addEventListener('click', () => {
    try { localStorage.removeItem(preferenceKey(voiceActive)); } catch {}
    renderPerformance();
    syncManualVoice();
  });
  const form = $("gf-composer");
  if (form) form.addEventListener("submit", onSend);
  const input = /** @type {HTMLTextAreaElement | null} */ ($("gf-composer-input"));
  if (input) {
    input.addEventListener("keydown", (e) => {
      // 回车发送、Shift+回车换行——和常见聊天框一致。
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void onSend(e);
      }
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
    });
  }
  // Keep the registry current even after the bounded readiness retry has expired.
  window.addEventListener('focus', () => { void refreshVoice(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshVoice(); });
  $('settings-tabs')?.addEventListener('click', () => { void refreshVoice(); });
  $('settings-btn')?.addEventListener('click', () => { void refreshVoice(); });
  window.setInterval(() => { if (!document.hidden && !voicePollTimer) void refreshVoice(); }, 15000);
  void refreshModels();
  renderPerformance();
  void refreshVoice();
}

export { manualSelection };

/** 由 main.js 在连接建立 / 断开时调用：文字发送要有活着的 WS。 */
export function setLive(value) {
  live = Boolean(value);
  if (live) void refreshVoice();
  const input = /** @type {HTMLTextAreaElement | null} */ ($("gf-composer-input"));
  const send = $("gf-composer-send");
  if (input) input.classList.toggle("idle", !live);
  if (send) send.textContent = "发送";
}
