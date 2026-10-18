// @ts-check
/**
 * 音色管理面板（AI_GIRLFRIEND_VOICE_LIBRARY_01）。
 *
 * 只说服务端已证实的事：能力矩阵来自 GET /api/voices 的 backend_support，
 * 后端不支持就显示限制原因，绝不渲染一个点了没反应的假按钮。
 * 上传 → 本地校验/转码 → 参考文字（可改）→ 保存 → 试听 → 切换，全部一条链。
 */

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

let mounted = false;
let voices = [];
let support = {};
let activeHiggs = "xiaoyan";
let trashSupported = false;

/** @param {HTMLElement} el @param {string} text @param {string} [kind] */
function status(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("is-error", kind === "error");
  el.classList.toggle("is-ok", kind === "ok");
}

async function api(url, body) {
  try {
    const res = await fetch(url, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined);
    const json = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, json };
  } catch (err) {
    return { ok: false, status: 0, json: { detail: { reason: String(err) } } };
  }
}

function reasonOf(r) {
  const d = r.json && (r.json.detail || r.json);
  return (d && (d.reason || d.detail)) || `HTTP ${r.status}`;
}

function currentVoiceId() {
  const sel = /** @type {HTMLSelectElement|null} */ ($("vl-active-select"));
  return sel ? sel.value : "";
}

/** 每个音色一张卡：名称 / 来源 / 参考文字 / 试听 / 编辑 / 删除 / 激活。 */
function voiceCard(v) {
  const li = document.createElement("li");
  li.className = "vl-item" + (v.voice_id === activeHiggs ? " is-active" : "");
  const head = document.createElement("div");
  head.className = "vl-item-head";
  const name = document.createElement("span");
  name.className = "vl-name";
  name.textContent = v.name + (v.kind === "system" ? " · 系统音色" : "");
  head.append(name);
  if (v.voice_id === activeHiggs) {
    const badge = document.createElement("span");
    badge.className = "ra-badge";
    badge.textContent = "Higgs 当前使用";
    head.append(badge);
  }
  if (v.duration_s) {
    const meta = document.createElement("span");
    meta.className = "ra-meta";
    meta.textContent = `${v.duration_s}s`;
    head.append(meta);
  }
  li.append(head);

  if (v.note) {
    const note = document.createElement("p");
    note.className = "vl-item-sub";
    note.textContent = v.note;
    li.append(note);
  }
  if (Array.isArray(v.warnings) && v.warnings.length) {
    const warn = document.createElement("p");
    warn.className = "vl-item-sub";
    warn.style.color = "#ffd489";
    warn.textContent = "⚠ " + v.warnings.join("；");
    li.append(warn);
  }

  // 参考文字（可编辑；系统音色只读展示）
  const refRow = document.createElement("div");
  refRow.className = "vl-ref-row";
  const refBox = /** @type {HTMLTextAreaElement} */ (document.createElement("textarea"));
  refBox.value = v.ref_text || "";
  refBox.placeholder = "参考音频说的话（克隆时给模型对齐用）";
  refBox.setAttribute("aria-label", "参考文字");
  if (v.kind === "system") refBox.readOnly = true;
  const saveRef = document.createElement("button");
  saveRef.type = "button";
  saveRef.className = "btn ghost";
  saveRef.textContent = "保存参考文字";
  saveRef.disabled = v.kind === "system";
  saveRef.addEventListener("click", async () => {
    const r = await api(`/api/voices/${v.voice_id}/ref_text`, { ref_text: refBox.value });
    status($("vl-status"), r.ok ? "参考文字已保存" : `保存失败：${reasonOf(r)}`,
           r.ok ? "ok" : "error");
    if (r.ok) v.ref_text = refBox.value.trim();
  });
  refRow.append(refBox, saveRef);
  li.append(refRow);

  const actions = document.createElement("div");
  actions.className = "vl-item-actions";

  const hearRef = document.createElement("button");
  hearRef.type = "button";
  hearRef.className = "btn ghost";
  hearRef.textContent = "听参考音频";
  hearRef.addEventListener("click", () => {
    const a = new Audio(`/api/voices/${v.voice_id}/audio`);
    a.play().catch(() => status($("vl-status"), "参考音频播放失败", "error"));
  });
  actions.append(hearRef);

  const preview = document.createElement("button");
  preview.type = "button";
  preview.className = "btn ghost";
  preview.textContent = "试听合成";
  preview.disabled = !(support.higgs && support.higgs.worker_ready);
  preview.addEventListener("click", async () => {
    preview.disabled = true;
    try {
      const res = await fetch(`/api/voices/${v.voice_id}/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "" }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        status($("vl-status"), (j.detail && j.detail.reason) || `HTTP ${res.status}`, "error");
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = audio.onerror = () => URL.revokeObjectURL(url);
      await audio.play().catch(error => { URL.revokeObjectURL(url); throw error; });
      status($("vl-status"), "试听已生成", "ok");
    } catch (error) {
      status($("vl-status"), `试听失败：${error.message}`, "error");
    } finally {
      preview.disabled = !(support.higgs && support.higgs.worker_ready);
    }
  });
  actions.append(preview);

  const activate = document.createElement("button");
  activate.type = "button";
  activate.className = "btn ghost";
  activate.textContent = v.voice_id === activeHiggs ? "恢复为当前音色" : "设为 Higgs 音色";
  activate.disabled = !(support.higgs && support.higgs.switch);
  activate.addEventListener("click", async () => {
    const r = await api("/api/voices/activate", { voice_id: v.voice_id, backend: "higgs" });
    if (r.ok && r.json.ok) {
      activeHiggs = r.json.active_voice || v.voice_id;
      status($("vl-status"), v.voice_id === "xiaoyan"
        ? "已恢复原版小烟音色" : `已切换 Higgs 音色为「${v.name}」，下一句回复即生效`, "ok");
      render();
    } else {
      status($("vl-status"), `切换未生效：${reasonOf(r)}`, "error");
    }
  });
  actions.append(activate);

  if (v.kind === "user") {
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "btn ghost";
    rename.textContent = "重命名";
    rename.addEventListener("click", () => {
      const next = window.prompt("新的音色名称", v.name);
      if (!next || !next.trim()) return;
      void (async () => {
        const r = await api(`/api/voices/${v.voice_id}/rename`, { name: next.trim() });
        status($("vl-status"), r.ok ? "已重命名" : `失败：${reasonOf(r)}`, r.ok ? "ok" : "error");
        if (r.ok) void refresh();
      })();
    });
    actions.append(rename);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn ghost";
    del.textContent = "删除";
    del.addEventListener("click", () => {
      if (!window.confirm(`删除音色「${v.name}」？它会移入音色回收站${trashSupported ? '，可在下方恢复' : '，可由维护人员找回'}。`)) return;
      void (async () => {
        const res = await fetch(`/api/voices/${v.voice_id}`, { method: "DELETE" });
        const j = await res.json().catch(() => ({}));
        if (res.ok && j.ok) {
          status($("vl-status"), "已删除（移入回收站）", "ok");
          void refresh();
        } else {
          status($("vl-status"), `删除失败：${(j.detail && j.detail.reason) || `HTTP ${res.status}`}`, "error");
        }
      })();
    });
    actions.append(del);
  }
  li.append(actions);
  return li;
}

function render() {
  const list = $("vl-list");
  if (!list) return;
  list.replaceChildren(...voices.map(voiceCard));
  const limits = $("vl-limits");
  if (limits) {
    limits.replaceChildren();
    for (const [id, row] of Object.entries(support)) {
      if (row.switch) continue;
      const p = document.createElement("p");
      p.className = "vl-limit is-blocked";
      p.textContent = `${id.toUpperCase()}：${row.note_zh || "不支持在线音色切换"}`;
      limits.append(p);
    }
  }
  const activeSel = /** @type {HTMLSelectElement|null} */ ($("vl-active-select"));
  if (activeSel) activeSel.value = activeHiggs;
  const current = $("inline-current-voice");
  if (current) current.textContent = (voices.find((v) => v.voice_id === activeHiggs) || {}).name || activeHiggs;
}

async function refresh() {
  const r = await api("/api/voices");
  if (!r.ok) { status($("vl-status"), "音色列表读取失败", "error"); return; }
  voices = r.json.voices || [];
  support = r.json.backend_support || {};
  activeHiggs = r.json.active_higgs || "xiaoyan";
  trashSupported = r.json.trash_supported === true;
  render();
  const panel = $("vl-trash-panel");
  if (panel) panel.hidden = !trashSupported;
  if (trashSupported) await refreshTrash();
}

async function refreshTrash() {
  const r = await api('/api/voices/trash');
  const list = $("vl-trash-list");
  if (!list || !r.ok) return;
  list.replaceChildren();
  for (const voice of r.json.voices || []) {
    const row = document.createElement('li');
    row.textContent = voice.name + ' ';
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'btn ghost'; button.textContent = '恢复';
    button.addEventListener('click', async () => {
      const restored = await api(`/api/voices/trash/${voice.trash_id}/restore`, {});
      status($("vl-status"), restored.ok ? '音色已恢复' : `恢复失败：${reasonOf(restored)}`, restored.ok ? 'ok' : 'error');
      if (restored.ok) void refresh();
    });
    row.append(button); list.append(row);
  }
}

export function mountVoiceLibrary() {
  const mount = $("vl-panel-mount");
  if (!mount || mounted) return;
  mounted = true;
  const panel = document.createElement("div");
  panel.className = "vl-panel";
  panel.innerHTML = `
    <h3>音色管理</h3>
    <p class="vl-hint">上传一段你授权使用的声音（WAV / MP3 等，3–120 秒），系统在本机提取参考文字并建立克隆音色；录音不上传到任何外部服务。原版小烟音色始终保留、随时可恢复。</p>
    <div class="vl-upload">
      <input id="vl-file" type="file" accept=".wav,.mp3,.flac,.m4a,.ogg,.opus,.webm" aria-label="选择参考音频" />
      <input id="vl-name" type="text" placeholder="音色名称（可选）" aria-label="音色名称" />
      <input id="vl-ref-text" type="text" placeholder="参考文字（留空则自动识别）" aria-label="参考文字" />
      <button id="vl-upload-btn" class="btn primary" type="button">保存音色</button>
    </div>
    <ul id="vl-list" class="vl-list" aria-label="音色列表"></ul>
    <div id="vl-limits"></div>
    <section id="vl-trash-panel" hidden><h4>音色回收站</h4><ul id="vl-trash-list"></ul></section>
    <p id="vl-status" class="vl-status" role="status"></p>
  `;
  mount.replaceChildren(panel);
  let transcriptionSerial = 0;
  $("vl-file").addEventListener("change", async () => {
    const serial = ++transcriptionSerial;
    const file = $("vl-file").files?.[0];
    if (!file) { $("vl-upload-btn").disabled = false; return; }
    const ref = $("vl-ref-text");
    const initial = ref.value;
    const form = new FormData();
    form.append("file", file);
    $("vl-upload-btn").disabled = true;
    status($("vl-status"), "正在本机 Whisper 识别参考文字…可继续编辑");
    try {
      const response = await fetch("/api/voices/transcribe", { method: "POST", body: form });
      const result = await response.json();
      if (serial !== transcriptionSerial) return;
      if (!response.ok || !result.ok) throw new Error(result.detail?.reason || result.detail || "识别失败");
      if (ref.value === initial) ref.value = result.ref_text;
      status($("vl-status"), "参考音频与识别文字已关联；可修改文字后保存。", "ok");
    } catch (error) {
      if (serial === transcriptionSerial) status($("vl-status"), error.message + "；可手工填写后保存。", "error");
    } finally {
      if (serial === transcriptionSerial) $("vl-upload-btn").disabled = false;
    }
  });
  $("vl-upload-btn").addEventListener("click", async () => {
    const input = /** @type {HTMLInputElement} */ ($("vl-file"));
    const nameInput = /** @type {HTMLInputElement} */ ($("vl-name"));
    const refInput = /** @type {HTMLInputElement} */ ($("vl-ref-text"));
    const btn = $("vl-upload-btn");
    if (!input.files || !input.files[0]) {
      status($("vl-status"), "请先选择一段音频文件", "error");
      return;
    }
    const form = new FormData();
    form.append("file", input.files[0]);
    form.append("name", nameInput.value);
    form.append("ref_text", refInput.value);
    btn.disabled = true;
    status($("vl-status"), "正在校验并保存参考音频…");
    try {
      const res = await fetch("/api/voices/upload", { method: "POST", body: form });
      const j = await res.json().catch(() => ({}));
      if (res.ok && j.ok) {
        const v = j.voice || {};
        status($("vl-status"),
          `音色「${v.name}」已创建（${v.duration_s}s）` +
          (v.asr_transcribed ? "，参考文字已自动识别，可修改" : ""),
          "ok");
        input.value = ""; nameInput.value = ""; refInput.value = "";
        void refresh();
      } else {
        status($("vl-status"), `创建失败：${(j.detail && j.detail.reason) || `HTTP ${res.status}`}`, "error");
      }
    } catch (error) {
      status($("vl-status"), `创建失败：${error.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  });
  void refresh();
}
