// @ts-check
/**
 * 书库管理弹窗（AI_GIRLFRIEND_READING_LIBRARY_MANAGE_01）。
 *
 * 入口：陪读视图里「书库」旁的「管理书库」按钮。全部操作走 /api/reading/library/*，
 * 按 document_id 精确处理；重复项只高亮提醒，不自动清空；删除一律进回收站，
 * 弹窗内即可恢复；正在朗读的书删除前强制二次确认（服务端同样设卡）。
 */

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

let modal = null;
let rows = [];
let selected = new Set();
let trashOpen = false;

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

function fmtDate(s) {
  return String(s || "").replace("T", " ").slice(0, 16);
}

function fmtStatus(row) {
  if (row.read_status === "DONE") return "已读完";
  if (row.read_status === "READING") return `阅读中 ${row.progress_percent}%`;
  if (row.read_status === "PAUSED") return `暂停 ${row.progress_percent}%`;
  if (row.read_status === "UNOPENED") return "未开始";
  return `${row.read_status} ${row.progress_percent}%`;
}

function rowLi(row, activeId) {
  const li = document.createElement("li");
  li.className = "ra-item" +
    (row.dup_title_group || row.dup_content_group ? " is-dup" : "") +
    (row.document_id === activeId ? " is-active" : "");
  const check = /** @type {HTMLInputElement} */ (document.createElement("input"));
  check.type = "checkbox";
  check.checked = selected.has(row.document_id);
  check.setAttribute("aria-label", `选择 ${row.title}`);
  check.addEventListener("change", () => {
    if (check.checked) selected.add(row.document_id);
    else selected.delete(row.document_id);
    syncSelectionUi();
  });
  const name = document.createElement("span");
  name.className = "ra-name";
  name.textContent = row.title || "(无标题)";
  name.title = `${row.title || ""}\n创建：${fmtDate(row.created_at)} · ${row.char_count} 字 · id ${row.document_id}`;
  const badge = document.createElement("span");
  badge.className = "ra-badge " +
    (row.read_status === "DONE" ? "is-done" : row.read_status === "READING" ? "is-reading" : "");
  badge.textContent = fmtStatus(row);
  const dup = document.createElement("span");
  dup.className = "ra-badge";
  const dups = [];
  if (row.dup_title_group) dups.push("重复标题");
  if (row.dup_content_group) dups.push("重复内容");
  if (dups.length) {
    dup.classList.add("is-dup");
    dup.textContent = dups.join("+");
  } else {
    dup.textContent = "";
    dup.style.display = "none";
  }
  const renameBtn = document.createElement("button");
  renameBtn.type = "button";
  renameBtn.className = "btn ghost";
  renameBtn.textContent = "改名";
  renameBtn.addEventListener("click", () => {
    const next = window.prompt("新的书名", row.title);
    if (!next || !next.trim() || next.trim() === row.title) return;
    void (async () => {
      const r = await api("/api/reading/library/rename",
        { document_id: row.document_id, title: next.trim() });
      status($("ra-status"), r.ok ? "已重命名" : `失败：${reasonOf(r)}`, r.ok ? "ok" : "error");
      if (r.ok) void refresh();
    })();
  });
  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "btn ghost";
  delBtn.textContent = "删除";
  delBtn.addEventListener("click", () => {
    const msg = row.document_id === activeId
      ? `《${row.title}》正在朗读中。删除会停止当前朗读并把这本书移入回收站，确定继续吗？`
      : `把《${row.title}》移入回收站？正文、进度与章节摘要一起移入，可在回收站恢复。`;
    if (!window.confirm(msg)) return;
    void doTrash([row.document_id], row.document_id === activeId);
  });
  li.append(check, name, badge, dup, renameBtn, delBtn);
  return li;
}

function syncSelectionUi() {
  const n = $("ra-selected-count");
  if (n) n.textContent = String(selected.size);
  const del = $("ra-batch-delete");
  if (del) del.disabled = selected.size === 0;
}

async function doTrash(ids, confirmActive) {
  const r = await api("/api/reading/library/trash",
    { document_ids: ids, confirm_active: Boolean(confirmActive) });
  if (!r.ok) { status($("ra-status"), `删除失败：${reasonOf(r)}`, "error"); return; }
  if (r.json.ok === false && r.json.reason === "ACTIVE_READING_CONFIRM") {
    const titles = (r.json.active_titles || []).join("、");
    if (window.confirm(`《${titles}》正在朗读中，删除会停止当前朗读并移入回收站。确定继续？`)) {
      await doTrash(r.json.active_document_ids, true);
      return;
    }
    status($("ra-status"), "已取消：正在朗读的书未被删除");
    return;
  }
  const moved = r.json.moved || 0;
  selected.clear();
  status($("ra-status"), `已移入回收站 ${moved} 本（可恢复）`, "ok");
  void refresh();
}

async function refresh() {
  const q = /** @type {HTMLInputElement} */ ($("ra-search"))?.value || "";
  const sortSel = /** @type {HTMLSelectElement} */ ($("ra-sort"));
  const statusSel = /** @type {HTMLSelectElement} */ ($("ra-status-filter"));
  const r = await api(`/api/reading/library/manage?q=${encodeURIComponent(q)}` +
    `&sort=${encodeURIComponent(sortSel?.value || "created")}` +
    `&status=${encodeURIComponent(statusSel?.value || "all")}`);
  if (!r.ok) { status($("ra-status"), "书库读取失败", "error"); return; }
  rows = r.json.documents || [];
  const activeId = r.json.active_document_id || "";
  const list = $("ra-list");
  if (list) {
    list.replaceChildren(...rows.map((row) => rowLi(row, activeId)));
  }
  const count = $("ra-count");
  if (count) {
    const dupT = r.json.duplicate_title_count || 0;
    const dupC = r.json.duplicate_content_count || 0;
    count.textContent = `共 ${r.json.total} 本` +
      (dupT ? ` · ${dupT} 组重复标题` : "") +
      (dupC ? ` · ${dupC} 组重复内容` : "") +
      (activeId ? " · 正在朗读的书已标出" : "");
  }
  syncSelectionUi();
  if (trashOpen) void refreshTrash();
}

async function refreshTrash() {
  const r = await api("/api/reading/library/trash");
  const box = $("ra-trash-box");
  if (!box || !r.ok) return;
  const ops = r.json.ops || [];
  const list = $("ra-trash-list");
  if (list) {
    list.replaceChildren();
    if (!ops.length) {
      const li = document.createElement("li");
      li.className = "ra-item";
      li.textContent = "回收站是空的";
      list.append(li);
    }
    for (const op of ops) {
      for (const item of op.items || []) {
        const li = document.createElement("li");
        li.className = "ra-item";
        const name = document.createElement("span");
        name.className = "ra-name";
        name.textContent = item.title || item.document_id;
        const meta = document.createElement("span");
        meta.className = "ra-meta";
        meta.textContent = fmtDate(op.created_at) + (item.progress_rows ? ` · ${item.progress_rows} 条断点` : "");
        const restore = document.createElement("button");
        restore.type = "button";
        restore.className = "btn ghost";
        restore.textContent = "恢复";
        restore.addEventListener("click", () => void doRestore([op.op_id]));
        li.append(name, meta, restore);
        list.append(li);
      }
    }
  }
}

async function doRestore(opIds) {
  const r = await api("/api/reading/library/restore", { op_ids: opIds });
  if (!r.ok) { status($("ra-status"), `恢复失败：${reasonOf(r)}`, "error"); return; }
  status($("ra-status"), "已恢复到书库（断点一并恢复）", "ok");
  void refresh();
}

export function mountReadingManager() {
  const btn = $("reading-manage");
  if (!btn) return;
  if (!modal) {
    modal = document.createElement("dialog");
    modal.id = "reading-manager-modal";
    modal.innerHTML = `
      <h3 style="margin:0 0 6px">管理书库</h3>
      <p class="ra-hint">删除只影响所选条目本身（正文、进度、章节摘要一起进回收站）；不碰原始小说文件、其他书与角色记忆。重复项只提示，不会自动清理。</p>
      <div class="ra-toolbar">
        <input id="ra-search" type="search" placeholder="按名称查找" aria-label="按名称查找" />
        <select id="ra-sort" aria-label="排序方式">
          <option value="created">按创建时间</option>
          <option value="read">按最近阅读</option>
          <option value="status">按完成状态</option>
          <option value="title">按名称</option>
          <option value="chars">按字数</option>
        </select>
        <select id="ra-status-filter" aria-label="状态筛选">
          <option value="all">全部状态</option>
          <option value="UNOPENED">未开始</option>
          <option value="READING">阅读中</option>
          <option value="PAUSED">暂停中</option>
          <option value="DONE">已读完</option>
        </select>
        <span id="ra-count" class="ra-count"></span>
      </div>
      <ul id="ra-list" class="ra-list" aria-label="书库列表"></ul>
      <div class="ra-actions">
        <span>已选 <b id="ra-selected-count">0</b> 项</span>
        <button id="ra-select-dup" class="btn ghost" type="button">选中所有重复项</button>
        <button id="ra-batch-delete" class="btn ghost" type="button" disabled>批量删除所选</button>
        <button id="ra-trash-toggle" class="btn ghost" type="button">回收站</button>
        <button id="ra-refresh" class="btn ghost" type="button">刷新</button>
        <button id="ra-close" class="btn ghost" type="button">关闭</button>
      </div>
      <div id="ra-trash-box" class="ra-trash" hidden>
        <p class="ra-hint">回收站：整单恢复，断点与摘要一并还原。彻底清除请由维护人员在服务端操作。</p>
        <ul id="ra-trash-list" class="ra-list ra-trash-list"></ul>
      </div>
      <p id="ra-status" class="ra-status" role="status"></p>
    `;
    document.body.append(modal);
    $("ra-search").addEventListener("input", () => void refresh());
    $("ra-sort").addEventListener("change", () => void refresh());
    $("ra-status-filter").addEventListener("change", () => void refresh());
    $("ra-refresh").addEventListener("click", () => void refresh());
    $("ra-close").addEventListener("click", () => modal.close());
    $("ra-trash-toggle").addEventListener("click", () => {
      trashOpen = !trashOpen;
      $("ra-trash-box").hidden = !trashOpen;
      $("ra-trash-toggle").textContent = trashOpen ? "收起回收站" : "回收站";
      if (trashOpen) void refreshTrash();
    });
    $("ra-select-dup").addEventListener("click", () => {
      rows.forEach((row) => {
        if (row.dup_title_group || row.dup_content_group) selected.add(row.document_id);
      });
      void refresh();
    });
    $("ra-batch-delete").addEventListener("click", () => {
      if (!selected.size) return;
      if (!window.confirm(`把选中的 ${selected.size} 本移入回收站？同名同内容的书只按记录 ID 处理，互不影响。`)) return;
      void doTrash([...selected], false);
    });
  }
  btn.addEventListener("click", () => {
    void refresh();
    modal.showModal();
  });
}
