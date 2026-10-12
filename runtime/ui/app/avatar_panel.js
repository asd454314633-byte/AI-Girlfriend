/**
 * 数字人面板（AI_GIRLFRIEND_AVATAR_FRONTEND_MANAGER_01 · T04）。
 *
 * 给完全不懂技术的人用：打开 7860 就看见「现在正在用谁」「还有谁可以选」，
 * 选一段视频做一个新的，做好了点一下就能换成她。全程不改任何文件、不开终端。
 *
 * 四条规矩（与 character_panel.js 同一套家风）：
 *   1. 状态只认服务端。面板不保存任何数字人资料，界面上出现的每一个名字、
 *      每一张缩略图都来自后端那一次请求；
 *   2. 界面上一个技术名词都没有。所有会给人看的话集中在下面的 TEXT 里，
 *      服务端回的话还要再过一遍「人话」筛子，掺了拉丁字母的一律不信；
 *   3. 不许让人以为卡住了。制作的每一秒都有可见变化，按钮点了立刻换样；
 *   4. 换人是会打断对话的重动作。先问清楚再动手，没做成也要把话说完整。
 *
 * 挂载：`<script type="module" src="avatar_panel.js"></script>` 就够了，
 * 页面里有 #av-panel 就装进去，没有就自己插一张卡片，不需要构建步骤。
 */

const TEXT = {
  /* 静态 */
  标题: "数字人",
  说明: "挑一个她现在的样子，或者用一段视频做一个新的。",
  正在使用: "正在使用",
  添加数字人: "添加数字人",
  取消: "取消",
  关闭: "收起",
  删除人物: "删除人物",
  删除确认: "要删除这个人物吗？",
  删除说明: "她会从列表移除，生成素材移到保留区，原视频保留。",
  确认删除: "确认删除",
  正在删除: "正在删除，请稍等…",
  删除完成: "已经从人物列表移除，素材已保留。",
  内置保护: "默认保留的人物不能删除。",

  /* 区块名 */
  当前区块: "现在正在用的",
  已有区块: "已经有的人物",

  /* 卡片上的行 */
  来源: "来源视频",
  做成时间: "做成时间",
  帧数: "帧数",
  来源不明: "来源不明",
  时间不明: "时间不明",
  没有第二位: "现在就只有她一位。",
  还没有人: "还没有任何人，先添加一位吧。",

  /* 添加表单 */
  人物名称: "她的名字",
  名称提示: "给她起个好认的名字，至少两个字。",
  名称重复: "已经有一个叫这个名字的了，换一个吧。可以叫：{建议名}",
  选视频: "选一段视频",
  视频限制: "现在只支持 MP4，大小不超过 {上限}，时长在 {最短} 到 {最长} 之间。",
  文件名: "文件名",
  时长: "时长",
  画面尺寸: "画面尺寸",
  帧率: "帧率",
  读不出信息: "这个视频读不出信息，也可以先试试。",
  文件太大: "这个文件太大了（{实际}），最多 {上限}。",
  时间太短: "这段太短了，至少要 {最短}。",
  时间太长: "这段太长了，最多 {最长}。",
  只能传视频: "只能选 MP4 视频。",
  开始制作: "开始制作",
  还没选: "先选一段她的视频。",
  读文件中: "正在读这一段视频…",
  读好了: "{名字} 已经读好了，可以开始做了。",

  /* 进度（5 段） */
  阶段1: "正在准备",
  阶段2: "正在读取视频",
  阶段3: "正在生成画面",
  阶段4: "正在核对素材",
  阶段5: "做好了",
  还在跑: "还在处理，已用 {秒} 秒，别关页面。",
  上传中: "正在上传，{百分比}%",
  取消制作: "取消制作",
  不能取消: "已经开始处理了，等它做完。",
  做好了: "做好了，可以切换到她了。",
  取消好了: "已经停下来了。",
  做得太久: "这一步做得太久了，先停在这里，可以去下面看看有没有多出来。",

  /* 结果 */
  失败: "没做成：{原因}",
  重来: "重来一次",
  失败兜底: "这一步没做成，请先确认那段视频里有一张清楚的正脸。",

  /* 切换 */
  使用这个: "使用这个人物",
  切换确认标题: "要换成她吗？",
  切换确认正文: "换成她会打断现在正在进行的对话。",
  切换确认补充: "她的样子会换成「{名字}」。",
  继续: "继续",
  切换中: "正在切换，请稍等…",
  切换成功: "已经换成她了。",
  切换失败回滚: "没换成，已经换回原来的人物了。",
  切换失败停机: "数字人现在没在运行，点这里重新启动。",
  重新启动: "重新启动",
  切换中断: "这一步没做成，她先不动，稍等一下再试。",
  服务要重启: "换人的时候会把画面重新准备一遍，所以要从头连一次。",

  /* 状态 */
  服务运行中: "数字人服务运行中",
  服务未运行: "数字人服务没在运行，暂时不能切换。",
  不一致: "现在实际在用的是「{A}」，和这里选的「{B}」不一样。",
  同步: "同步一下",
  同步好了: "已经对齐了。",

  /* 兜底 */
  加载中: "正在读…",
  联系不上: "这台机器上的服务暂时联系不上，稍等一下再试。",
};

/* 后端给这套面板的接口都在同一个前缀下。前缀单独拎出来：将来改一处就够，
 * 也保证这一串地址全文只出现一次。请求全部打在 7860 这一侧的代理上。 */
const API = "/api/avatar";
const 接口 = {
  面板: `${API}/panel`,
  列表: `${API}/list`,
  看看: `${API}/inspect`,
  制作: `${API}/create`,
  进度: `${API}/making`,
  取消制作: `${API}/making/cancel`,
  切换: `${API}/switch`,
  切换状态: `${API}/switch/state`,
  画面证据: `${API}/switch/frame-proof`,
  缩略图: `${API}/thumb`,
  同步: `${API}/sync`,
  重启: `${API}/restart`,
  删除检查: `${API}/delete/preview`,
  删除: `${API}/delete`,
};

const 阶段名单 = [TEXT.阶段1, TEXT.阶段2, TEXT.阶段3, TEXT.阶段4, TEXT.阶段5];
const 阶段序号 = new Map(阶段名单.map((名字, 次序) => [名字, 次序 + 1]));

const POLL_MS = 1000;            // 制作进度：1 秒问一次
const 心跳_MS = 500;             // 界面自己走秒：半秒一跳，页面绝不僵住
const 阶段最短停留_MS = 400;     // 防瞬移：每一段至少亮 400 毫秒
const 切换最长等待_MS = 10 * 60 * 1000;
const 制作最长等待_MS = 60 * 60 * 1000;

let state = {
  当前: null,
  列表: [],
  运行中: false,
  服务状态: "",
  一致性: { 一致: true, 实际在用的名称: "", 这里选的名称: "" },
  限制: { 最大字节: 0, 最短秒: 0, 最长秒: 0, 接受类型: [] },
  忙碌: "",              // "" | "制作" | "切换"
  添加区开着: false,
  文件: null,
  文件信息: null,
  句柄: "",
  上传百分比: 0,
  制作编号: "",
  制作: null,            // 最近一次制作进度
  展示阶段: 0,
  进入时刻: 0,
  本地开始: 0,
  切换前编号: "",
  轮次: 0,
  心跳: 0,
};

/* ------------------------------------------------------------------ 小工具 */

const el = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** 把「{某某}」占位换成具体的话。 */
function 套话(template, vars) {
  return String(template || "").replace(/\{([^}]+)\}/g, (原文, key) => (
    vars && vars[key] !== undefined && vars[key] !== null ? String(vars[key]) : 原文
  ));
}

/**
 * 服务端的话要过一遍筛子：只要掺了拉丁字母，就当它是内部代号，不许进界面，
 * 统一换成人话兜底。这样即便后端哪天回了一句英文，也不会漏到用户眼前。
 */
function 人话(text, 兜底) {
  const body = String(text === undefined || text === null ? "" : text).trim();
  if (!body || /[A-Za-z_]/.test(body)) return String(兜底 || "");
  return body;
}

async function call(path, body) {
  try {
    // 必须带 content-type：FastAPI 的 JSON 请求体是按头判定的，缺了就 422，
    // 面板只会显示「这一步没做成」，用户完全看不出为什么。
    const res = body
      ? await fetch(path, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        })
      : await fetch(path);
    const data = await res.json().catch(() => null);
    if (!data || typeof data !== "object") {
      return { ok: false, 说明: TEXT.联系不上, 状态: res.status };
    }
    return data;
  } catch {
    return { ok: false, 说明: TEXT.联系不上 };
  }
}

const url = (path, params) => {
  const tail = Object.entries(params || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");
  return tail ? `${path}?${tail}` : path;
};

function note(message, kind) {
  const box = el("av-note");
  if (!box) return;
  box.textContent = String(message || "");
  box.dataset.kind = kind || "普通";
}

function labelled(label, control, hint) {
  const wrap = make("label", "av-field");
  wrap.append(make("span", "av-field-label", label));
  wrap.append(control);
  if (hint) wrap.append(make("small", "av-field-hint", hint));
  return wrap;
}

function textInput(id, value, placeholder) {
  const box = make("input", "av-input");
  box.id = id;
  box.type = "text";
  box.value = value || "";
  if (placeholder) box.placeholder = placeholder;
  return box;
}

function button(label, cls, onClick) {
  const box = make("button", `av-btn ${cls || ""}`.trim(), label);
  box.type = "button";
  box.addEventListener("click", onClick);
  return box;
}

function 大小话(bytes) {
  const size = Number(bytes || 0);
  if (!size) return "";
  if (size >= 1024 * 1024) return `${Math.round(size / (1024 * 1024))} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
}

function 秒数话(seconds) {
  const num = Number(seconds || 0);
  return String(Number.isFinite(num) ? num : 0);
}

/** 时长显示成 mm:ss（超过一小时就补上小时）。 */
function 时分秒话(seconds) {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function 日期话(value) {
  const body = String(value || "").trim();
  if (!body) return TEXT.时间不明;
  const when = new Date(body);
  if (Number.isNaN(when.getTime())) return TEXT.时间不明;
  const p = (n) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${p(when.getMonth() + 1)}-${p(when.getDate())} `
    + `${p(when.getHours())}:${p(when.getMinutes())}`;
}

/** 中文确认框：把「要换成谁、会打断什么」写在脸上，不让人稀里糊涂点确定。 */
function ask({ 标题, 问题, 补充, 确定字, 危险, onYes }) {
  const host = el("av-panel") || document.body;
  const back = make("div", "av-mask");
  const card = make("div", "av-dialog");
  card.append(make("h3", "av-dialog-title", 标题));
  card.append(make("p", "av-dialog-ask", 问题));
  if (补充) card.append(make("p", "av-dialog-detail", 补充));
  const row = make("div", "av-row av-row-end");
  row.append(button(TEXT.取消, "av-btn-quiet", () => back.remove()));
  row.append(button(确定字 || TEXT.继续, `av-btn-main ${危险 ? "av-btn-danger" : ""}`.trim(),
    async () => {
      back.remove();
      await onYes();
    }));
  card.append(row);
  back.append(card);
  back.addEventListener("click", (event) => {
    if (event.target === back) back.remove();
  });
  host.append(back);
  const yes = card.querySelector(".av-btn-main");
  if (yes) yes.focus();
}

/* ------------------------------------------------------------------ 骨架 */

function shell() {
  if (el("av-panel")) return el("av-panel");
  const host = el("av-panel-mount") || el("av-panel");
  const panel = make("section", "av-panel");
  panel.id = "av-panel";
  panel.dataset.mounted = "auto";
  // UI_FIX_01 · ISSUE 2：面板默认收起。主页上有明确的「数字人」入口，
  // 点击后才展开——面板自己不再直接摆在页面末尾等人撞见。
  panel.hidden = !host;
  (host || document.body).append(panel);
  return panel;
}

function buildSkeleton(panel) {
  panel.replaceChildren();

  const head = make("div", "av-head");
  head.append(make("h2", "av-title", TEXT.标题));
  head.append(make("p", "av-sub", TEXT.说明));
  // UI_FIX_01 · ISSUE 2：面板内给一个「收起」，和主页入口的开合行为一致。
  const headRow = make("div", "av-row av-row-end");
  if (!el("av-panel-mount")) headRow.append(button(TEXT.关闭, "av-btn-quiet", () => { panel.hidden = true; }));
  head.append(headRow);
  const badges = make("div", "av-row av-badges");
  badges.id = "av-badges";
  head.append(badges);
  const gap = make("div", "av-consistency");
  gap.id = "av-consistency";
  gap.hidden = true;
  head.append(gap);
  const box = make("div", "av-note");
  box.id = "av-note";
  box.dataset.kind = "普通";
  box.textContent = TEXT.加载中;
  head.append(box);
  panel.append(head);

  const current = make("section", "av-block");
  current.id = "av-current";
  panel.append(current);

  const list = make("section", "av-block");
  list.append(make("h3", "av-sec-title", TEXT.已有区块));
  const grid = make("div", "av-grid");
  grid.id = "av-list";
  list.append(grid);
  const tools = make("div", "av-row av-tools");
  tools.id = "av-tools";
  list.append(tools);
  panel.append(list);

  const add = make("section", "av-block");
  add.id = "av-add";
  add.hidden = true;
  panel.append(add);

  const progress = make("section", "av-block");
  progress.id = "av-progress";
  progress.hidden = true;
  panel.append(progress);
}

/* ------------------------------------------------------------------ 刷新 */

async function refresh() {
  const data = await call(接口.面板);
  if (!data || !data.ok) {
    note(人话(data && data.说明, TEXT.联系不上), "警示");
    return;
  }
  state.当前 = data.当前 && typeof data.当前 === "object" ? data.当前 : null;
  state.列表 = Array.isArray(data.列表) ? data.列表 : [];
  state.服务状态 = String(data.服务状态 || "");
  state.运行中 = String(data.服务状态 || "") !== "没在运行";
  state.一致性 = data.一致性 && typeof data.一致性 === "object"
    ? data.一致性 : { 一致: true, 实际在用的名称: "", 这里选的名称: "" };
  state.限制 = data.限制 && typeof data.限制 === "object" ? data.限制 : state.限制;
  renderAll();
  const box = el("av-note");
  if (box && (!box.textContent || box.textContent === TEXT.加载中)) {
    const who = state.当前 && state.当前.名称 ? String(state.当前.名称) : "";
    note(who ? `现在是${who}。` : TEXT.还没有人);
  }
}

function renderAll() {
  renderBadges();
  renderConsistency();
  renderCurrent();
  renderList();
  renderTools();
  if (state.添加区开着) renderFileInfo();
  renderProgress();
}

function renderBadges() {
  const box = el("av-badges");
  if (!box) return;
  box.replaceChildren();
  const tag = make("span", "av-badge", state.运行中 ? TEXT.服务运行中 : TEXT.服务未运行);
  tag.dataset.kind = state.运行中 ? "好消息" : "警示";
  box.append(tag);
  if (!state.运行中) box.append(button(TEXT.重新启动, "av-btn-quiet", restartNow));
}

function renderConsistency() {
  const box = el("av-consistency");
  if (!box) return;
  const gap = state.一致性 || {};
  if (gap.一致 !== false) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.hidden = false;
  box.replaceChildren();
  box.append(make("p", "av-consistency-text", 套话(TEXT.不一致, {
    A: String(gap.实际在用的名称 || TEXT.来源不明),
    B: String(gap.这里选的名称 || TEXT.来源不明),
  })));
  box.append(button(TEXT.同步, "av-btn-quiet", syncNow));
}

function 缩略图(item) {
  const node = make("div", "av-thumb");
  const pic = make("img", "av-thumb-img");
  pic.alt = String((item && item.名称) || TEXT.标题);
  pic.loading = "lazy";
  pic.decoding = "async";
  pic.addEventListener("error", () => { node.dataset.miss = "true"; });
  if (item && item.缩略图地址) pic.src = String(item.缩略图地址);
  else node.dataset.miss = "true";
  node.append(pic);
  return node;
}

function 卡片信息(item) {
  return [
    `${TEXT.来源}：${String(item.来源视频名 || TEXT.来源不明)}`,
    `${TEXT.做成时间}：${日期话(item.做成时间)}`,
    `${TEXT.帧数}：${String(item.帧数 === undefined || item.帧数 === null ? TEXT.来源不明 : item.帧数)}`,
  ].join(" · ");
}

function renderCurrent() {
  const box = el("av-current");
  if (!box) return;
  box.replaceChildren();
  box.append(make("h3", "av-sec-title", TEXT.当前区块));
  const one = state.当前;
  if (!one) {
    box.append(make("p", "av-empty", TEXT.还没有人));
    return;
  }
  const card = make("div", "av-card av-card-current");
  card.dataset.编号 = String(one.编号 || "");
  card.append(缩略图(one));
  const body = make("div", "av-card-body");
  const title = make("div", "av-card-title");
  title.append(make("strong", null, String(one.名称 || TEXT.来源不明)));
  title.append(make("span", "av-chip", TEXT.正在使用));
  body.append(title);
  body.append(make("small", "av-card-meta", 卡片信息(one)));
  card.append(body);
  box.append(card);
}

function renderList() {
  const grid = el("av-list");
  if (!grid) return;
  grid.replaceChildren();
  const 当前编号 = state.当前 ? String(state.当前.编号 || "") : "";
  const other = state.列表.filter((one) => String(one.编号 || "") !== 当前编号);
  if (!other.length) {
    grid.append(make("p", "av-empty",
      state.列表.length ? TEXT.没有第二位 : TEXT.还没有人));
    return;
  }
  other.forEach((one) => {
    const card = make("div", "av-card");
    card.dataset.编号 = String(one.编号 || "");
    card.append(缩略图(one));
    const body = make("div", "av-card-body");
    const title = make("div", "av-card-title");
    title.append(make("strong", null, String(one.名称 || TEXT.来源不明)));
    body.append(title);
    body.append(make("small", "av-card-meta", 卡片信息(one)));
    const row = make("div", "av-row");
    const useBtn = button(TEXT.使用这个, "av-btn-quiet", () => askSwitch(one));
    if (!state.运行中 || state.忙碌) useBtn.disabled = true;
    row.append(useBtn);
    const deleteBtn = button(TEXT.删除人物, "av-btn-quiet av-btn-danger", () => askDelete(one));
    deleteBtn.disabled = Boolean(state.忙碌) || one.允许删除 !== true;
    if (one.内置人物) deleteBtn.title = TEXT.内置保护;
    row.append(deleteBtn);
    if (!state.运行中) row.append(make("small", "av-field-hint", TEXT.服务未运行));
    body.append(row);
    card.append(body);
    grid.append(card);
  });
}

function renderTools() {
  const box = el("av-tools");
  if (!box) return;
  box.replaceChildren();
  if (state.忙碌) return;
  box.append(button(TEXT.添加数字人, "av-btn-main", () => {
    if (state.添加区开着) closeAdd();
    else openAdd();
  }));
}

async function askDelete(one) {
  if (state.忙碌) return;
  state.忙碌 = "删除检查";
  renderAll();
  let checked;
  try { checked = await call(接口.删除检查, { 编号: one.编号 }); }
  finally { state.忙碌 = ""; renderAll(); }
  if (!checked?.ok) {
    note(人话(checked?.说明, TEXT.联系不上), "警示");
    return;
  }
  ask({ 标题: TEXT.删除确认, 问题: `删除「${checked.名称}」？`,
    补充: TEXT.删除说明, 确定字: TEXT.确认删除, 危险: true,
    onYes: async () => {
      if (state.忙碌) return;
      state.忙碌 = "删除"; renderAll(); note(TEXT.正在删除);
      try {
        const result = await call(接口.删除, { 编号: one.编号, 确认: true, 校验: checked.校验 });
        note(人话(result?.说明, result?.ok ? TEXT.删除完成 : TEXT.联系不上), result?.ok ? "好消息" : "警示");
      } finally { state.忙碌 = ""; await refresh(); }
    }
  });
}

/* ------------------------------------------------------------------ 添加 */

function openAdd() {
  const box = el("av-add");
  if (!box) return;
  state.添加区开着 = true;
  state.文件 = null;
  state.文件信息 = null;
  state.句柄 = "";
  state.上传百分比 = 0;
  box.replaceChildren();
  box.append(make("h3", "av-sec-title", TEXT.添加数字人));
  const form = make("div", "av-form");
  const name = textInput("av-name", "", TEXT.人物名称);
  const pick = make("input", "av-input");
  pick.type = "file";
  pick.id = "av-file";
  const accept = (state.限制.接受类型 || []).filter(Boolean);
  if (accept.length) pick.accept = accept.join(",");
  pick.addEventListener("change", () => {
    const file = pick.files && pick.files[0];
    void onFilePicked(file || null);
  });
  form.append(labelled(TEXT.人物名称, name, TEXT.名称提示));
  form.append(labelled(TEXT.选视频, pick, 限制话()));
  box.append(form);
  const info = make("div", "av-file-info");
  info.id = "av-file-info";
  box.append(info);
  const row = make("div", "av-row");
  row.append(button(TEXT.开始制作, "av-btn-main", startMaking));
  row.append(button(TEXT.关闭, "av-btn-quiet", closeAdd));
  box.append(row);
  box.hidden = false;
  name.focus();
}

function closeAdd() {
  const box = el("av-add");
  state.添加区开着 = false;
  state.文件 = null;
  state.文件信息 = null;
  state.句柄 = "";
  state.上传百分比 = 0;
  if (box) {
    box.hidden = true;
    box.replaceChildren();
  }
  renderTools();
}

function 限制话() {
  return 套话(TEXT.视频限制, {
    上限: 大小话(state.限制.最大字节) || "200 MB",
    最短: `${秒数话(state.限制.最短秒) || "3"} 秒`,
    最长: `${秒数话(state.限制.最长秒) || "60"} 秒`,
  });
}

async function onFilePicked(file) {
  state.文件 = file || null;
  state.文件信息 = null;
  state.句柄 = "";
  state.上传百分比 = 0;
  renderFileInfo();
  if (!file) return;

  const 上限 = Number(state.限制.最大字节 || 0);
  if (上限 && file.size > 上限) {
    note(套话(TEXT.文件太大, { 实际: 大小话(file.size), 上限: 大小话(上限) }), "警示");
    return;
  }
  const 接受的类型 = (state.限制.接受类型 || [])
    .filter(Boolean).map((one) => String(one).toLowerCase());
  if (接受的类型.length && !接受的类型.some((one) => String(file.name || "").toLowerCase().endsWith(one))) {
    note(TEXT.只能传视频, "警示");
    return;
  }
  note(TEXT.读文件中);
  const back = await 上传看看(file);
  if (!back || !back.ok) {
    note(人话(back && back.说明, TEXT.读不出信息), "警示");
    state.文件信息 = { 文件名: file.name, 空着: true };
    renderFileInfo();
    return;
  }
  state.文件信息 = back;
  state.句柄 = String(back.句柄 || "");
  renderFileInfo();

  const 最短 = Number(state.限制.最短秒 || 0);
  const 最长 = Number(state.限制.最长秒 || 0);
  const 时长 = Number(back.时长 || 0);
  if (最短 && 时长 && 时长 < 最短) {
    note(套话(TEXT.时间太短, { 最短: `${秒数话(最短)} 秒` }), "警示");
    return;
  }
  if (最长 && 时长 && 时长 > 最长) {
    note(套话(TEXT.时间太长, { 最长: `${秒数话(最长)} 秒` }), "警示");
    return;
  }
  if (back.能不能用 === false) {
    note(人话(back.原因, TEXT.读不出信息), "警示");
    return;
  }
  note(套话(TEXT.读好了, { 名字: String(file.name || TEXT.选视频) }), "好消息");
}

/** 文件只传这一次：multipart 交给后端落盘并读出四项信息，回一个句柄。 */
function 上传看看(file) {
  return new Promise((resolve) => {
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", 接口.看看);
      xhr.responseType = "json";
      // 大文件不能让人干等着：上传本身就带读条。
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        state.上传百分比 = Math.round((event.loaded / event.total) * 100);
        renderFileInfo();
      });
      const 收工 = (payload) => {
        state.上传百分比 = 100;
        resolve(payload && typeof payload === "object" ? payload : { ok: false, 说明: TEXT.联系不上 });
      };
      xhr.addEventListener("load", () => 收工(xhr.response));
      xhr.addEventListener("error", () => 收工({ ok: false, 说明: TEXT.联系不上 }));
      xhr.addEventListener("abort", () => 收工({ ok: false, 说明: TEXT.联系不上 }));
      xhr.send(form);
    } catch {
      resolve({ ok: false, 说明: TEXT.联系不上 });
    }
  });
}

function renderFileInfo() {
  const box = el("av-file-info");
  if (!box) return;
  box.replaceChildren();
  const file = state.文件;
  if (!file) return;

  const rows = make("div", "av-info");
  rows.append(make("span", "av-info-key", TEXT.文件名));
  rows.append(make("span", "av-info-value", String(file.name || "")));
  if (state.上传百分比 > 0 && state.上传百分比 < 100) {
    rows.append(make("span", "av-info-progress", 套话(TEXT.上传中, { 百分比: state.上传百分比 })));
    box.append(rows);
    return;
  }

  const info = state.文件信息 || {};
  const 时长 = Number(info.时长 || 0);
  const 尺寸 = String(info.画面尺寸 || "").trim();
  const 帧率数 = info.帧率;
  const 有帧率 = 帧率数 !== undefined && 帧率数 !== null && String(帧率数) !== "";
  if (!时长 && !尺寸 && !有帧率) {
    rows.append(make("span", "av-info-note", TEXT.读不出信息));
  }
  if (时长) {
    rows.append(make("span", "av-info-key", TEXT.时长));
    rows.append(make("span", "av-info-value", 时分秒话(时长)));
  }
  if (尺寸) {
    rows.append(make("span", "av-info-key", TEXT.画面尺寸));
    rows.append(make("span", "av-info-value", 尺寸));
  }
  if (有帧率) {
    rows.append(make("span", "av-info-key", TEXT.帧率));
    rows.append(make("span", "av-info-value", `${String(帧率数)} 帧/秒`));
  }
  box.append(rows);
}

/* ------------------------------------------------------------------ 制作 */

async function startMaking() {
  const nameBox = el("av-name");
  const name = String((nameBox && nameBox.value) || "").trim();
  if (name.length < 2) {
    note(TEXT.名称提示, "警示");
    if (nameBox) nameBox.focus();
    return;
  }
  if (!state.句柄) {
    note(TEXT.还没选, "警示");
    return;
  }
  const back = await call(接口.制作, { 显示名: name, 句柄: state.句柄 });
  if (!back || !back.ok) {
    if (back && back.建议名) note(套话(TEXT.名称重复, { 建议名: String(back.建议名) }), "警示");
    else note(人话(back && back.说明, TEXT.失败兜底), "警示");
    return;
  }
  state.制作编号 = String(back.制作编号 || "");
  state.制作 = null;
  state.展示阶段 = 0;
  state.进入时刻 = 0;
  state.本地开始 = Date.now();
  state.忙碌 = "制作";
  closeAdd();
  note(TEXT.阶段1);
  renderTools();
  renderProgress();
  void watchMaking();
}

function 目标阶段(data) {
  if (String(data.状态 || "") === "没做成") return 0;
  const 名次 = 阶段序号.get(String(data.阶段 || ""));
  if (名次) return 名次;
  const 状态 = String(data.状态 || "");
  if (状态 === "做好了" || 状态 === "完成") return 5;
  const 百分比 = Number(data.百分比 || 0);
  if (状态 === "准备") return 1;
  if (百分比 >= 90) return 4;
  if (百分比 >= 30) return 3;
  return 2;
}

/**
 * 阶段条一步一步往前挪：每一段至少亮 400 毫秒。
 * 后端那边的进度是「有回调才变」，可能从 0 直接跳到 100；没有这道闸，
 * 用户会看到前四段一闪而过、直接落成「做好了」，看着像假的。
 */
function 推进阶段(data) {
  const 目标 = 目标阶段(data);
  if (目标 === 0) return;
  if (!state.展示阶段) {
    state.展示阶段 = Math.min(1, 目标);
    state.进入时刻 = Date.now();
  }
  if (目标 > state.展示阶段 && Date.now() - state.进入时刻 >= 阶段最短停留_MS) {
    state.展示阶段 = Math.min(state.展示阶段 + 1, 目标);
    state.进入时刻 = Date.now();
  }
  if (目标 < state.展示阶段) state.展示阶段 = Math.max(1, 目标);
}

async function watchMaking() {
  const 轮次 = (state.轮次 += 1);
  const 起点 = Date.now();
  startTicker();
  while (Date.now() - 起点 < 制作最长等待_MS) {
    if (轮次 !== state.轮次) return;
    const data = await call(url(接口.进度, { 编号: state.制作编号 }));
    if (轮次 !== state.轮次) return;
    if (!data || !data.ok) {
      note(人话(data && data.说明, TEXT.联系不上), "警示");
      stopMaking();
      return;
    }
    state.制作 = data;
    推进阶段(data);
    renderProgress();
    const 状态 = String(data.状态 || "");
    if (状态 === "做好了" || 状态 === "没做成") {
      await finishMaking(data);
      return;
    }
    await sleep(POLL_MS);
  }
  note(TEXT.做得太久, "警示");
  stopMaking();
}

async function finishMaking(data) {
  stopMaking();
  if (String(data.状态 || "") === "做好了") {
    note(TEXT.做好了, "好消息");
  } else {
    const 原因 = 人话(data.原因, "");
    note(原因 ? 套话(TEXT.失败, { 原因 }) : TEXT.失败兜底, "警示");
  }
  renderProgress();
  await refresh();
}

function stopMaking() {
  stopTicker();
  state.忙碌 = "";
  renderAll();
}

function startTicker() {
  stopTicker();
  state.心跳 = setInterval(() => {
    const line = el("av-progress-line");
    if (line) line.textContent = 进度话();
  }, 心跳_MS);
}

function stopTicker() {
  if (state.心跳) {
    clearInterval(state.心跳);
    state.心跳 = 0;
  }
}

function 已用秒() {
  if (!state.本地开始) return 0;
  return Math.max(0, Math.round((Date.now() - state.本地开始) / 1000));
}

function 进度话() {
  const data = state.制作 || {};
  const 百分比 = Math.max(0, Math.min(100, Math.round(Number(data.百分比 || 0))));
  const 秒 = 已用秒();
  const 做什么 = 人话(data.在做什么, "");
  if (做什么) return `${做什么} · ${百分比}% · 已用 ${秒} 秒`;
  return 套话(TEXT.还在跑, { 秒 });
}

function renderProgress() {
  const box = el("av-progress");
  if (!box) return;
  const data = state.制作;
  if (!data) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.hidden = false;
  box.replaceChildren();
  box.append(make("h3", "av-sec-title", TEXT.添加数字人));

  const 到第几 = Math.max(1, state.展示阶段 || 1);
  const 没做成 = String(data.状态 || "") === "没做成";
  const steps = make("div", "av-steps");
  阶段名单.forEach((名字, 次序) => {
    const 序 = 次序 + 1;
    const step = make("div", "av-step", `${序} ${名字}`);
    step.dataset.state = 没做成 && 序 === 到第几
      ? "失败"
      : (序 < 到第几 ? "过了" : (序 === 到第几 ? "正在" : "没到"));
    steps.append(step);
  });
  box.append(steps);

  const rail = make("div", "av-meter");
  const fill = make("div", "av-meter-fill");
  fill.style.width = `${Math.max(2, Math.min(100, Number(data.百分比 || 0)))}%`;
  rail.append(fill);
  box.append(rail);

  const line = make("div", "av-progress-line", 进度话());
  line.id = "av-progress-line";
  box.append(line);

  const row = make("div", "av-row");
  const 排队中 = String(data.状态 || "") === "准备" && (state.展示阶段 || 1) <= 1;
  const 定局 = String(data.状态 || "") === "做好了" || 没做成;
  if (state.忙碌 === "制作" && 排队中) row.append(button(TEXT.取消制作, "av-btn-quiet", cancelMaking));
  else if (state.忙碌 === "制作") row.append(make("small", "av-field-hint", TEXT.不能取消));
  if (没做成) row.append(button(TEXT.重来, "av-btn-main", openAdd));
  if (定局) row.append(button(TEXT.关闭, "av-btn-quiet", () => {
    state.制作 = null;
    renderProgress();
  }));
  box.append(row);
}

async function cancelMaking() {
  const back = await call(接口.取消制作, { 制作编号: state.制作编号 });
  if (!back || !back.ok) {
    note(人话(back && back.说明, TEXT.不能取消), "警示");
    return;
  }
  state.轮次 += 1;
  stopTicker();
  state.忙碌 = "";
  state.制作 = null;
  note(人话(back.说明, TEXT.取消好了), "好消息");
  await refresh();
}

/* ------------------------------------------------------------------ 切换 */

function askSwitch(one) {
  if (!state.运行中) {
    note(TEXT.服务未运行, "警示");
    return;
  }
  ask({
    标题: TEXT.切换确认标题,
    问题: 套话(TEXT.切换确认补充, { 名字: String(one.名称 || TEXT.来源不明) }),
    补充: `${TEXT.服务要重启}${TEXT.切换确认正文}`,
    确定字: TEXT.继续,
    危险: true,
    onYes: () => runSwitch(String(one.编号 || "")),
  });
}

async function 断开会话() {
  const bridge = globalThis.AvatarSync;
  if (!bridge || typeof bridge.disconnect !== "function") return;
  try {
    bridge.disconnect();
  } catch (err) {
    console.warn("[数字人] 断开这一步没做成：", err);
  }
}

async function 重连会话() {
  const bridge = globalThis.AvatarSync;
  if (!bridge) return;
  try {
    if (typeof bridge.reconnect === "function") await bridge.reconnect();
    else if (typeof bridge.connect === "function") await bridge.connect();
  } catch (err) {
    console.warn("[数字人] 重新连接这一步没做成，刷新页面后会自动再试：", err);
  }
}

async function runSwitch(编号) {
  if (state.忙碌) return;
  state.忙碌 = "切换";
  state.切换前编号 = state.当前 ? String(state.当前.编号 || "") : "";
  note(TEXT.切换中);
  renderAll();
  let 走完了 = false;
  try {
    await 断开会话();
    const back = await call(接口.切换, { 编号 });
    if (!back || !back.ok) {
      note(人话(back && back.说明, TEXT.切换中断), "警示");
    } else {
      走完了 = true;
      const 结局 = await 等切换结束();
      说话(结局);
    }
  } catch (err) {
    console.error("[数字人] 切换没走完：", err);
    note(TEXT.切换中断, "警示");
  } finally {
    state.忙碌 = "";
    await 重连会话();
    if (走完了) await 回报画面证据(编号);
    await refresh();
  }
}

async function 等切换结束() {
  const 起点 = Date.now();
  while (Date.now() - 起点 < 切换最长等待_MS) {
    const data = await call(接口.切换状态);
    if (!data || !data.ok) {
      note(人话(data && data.说明, TEXT.切换中断), "警示");
      return { 走到了: "联系不上", data: null };
    }
    if (data.忙 === false) return { 走到了: "有结果", data };
    const 秒 = Math.round((Date.now() - 起点) / 1000);
    note(`${TEXT.切换中}已经用了 ${秒} 秒。`);
    await sleep(POLL_MS);
  }
  return { 走到了: "太久", data: null };
}

function 说话({ 走到了, data }) {
  if (走到了 !== "有结果" || !data) {
    note(TEXT.切换中断, "警示");
    return;
  }
  const raw = String(data.结果 || "");
  const 中文 = String(data.提示 || "");
  if (/成功|换成了|SWITCH_OK/i.test(raw)) {
    note(TEXT.切换成功, "好消息");
    return;
  }
  if (/停机|起不来|ROLLBACK_FAILED/i.test(raw)) {
    note(TEXT.切换失败停机, "警示");
    return;
  }
  if (/换回|原来|ROLLBACK_OK|ABORT/i.test(raw)) {
    note(人话(中文, TEXT.切换失败回滚), "警示");
    return;
  }
  note(人话(中文, TEXT.切换中断), "警示");
}

/* ------------------------------------------- 画面是不是真的换成她了 */

/** 取一张图（或一帧画面）的 8×8 灰度指纹。 */
function 指纹(source) {
  const canvas = document.createElement("canvas");
  canvas.width = 8;
  canvas.height = 8;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(source, 0, 0, 8, 8);
  const pixels = ctx.getImageData(0, 0, 8, 8).data;
  const out = new Array(64);
  for (let i = 0; i < 64; i += 1) {
    const at = i * 4;
    out[i] = 0.299 * pixels[at] + 0.587 * pixels[at + 1] + 0.114 * pixels[at + 2];
  }
  return out;
}

function 平均差(a, b) {
  let sum = 0;
  for (let i = 0; i < a.length; i += 1) sum += Math.abs(a[i] - b[i]);
  return Math.round((sum / a.length) * 1000) / 1000;
}

function 读图(address) {
  return new Promise((resolve, reject) => {
    const pic = new Image();
    pic.addEventListener("load", () => resolve(pic));
    pic.addEventListener("error", () => reject(new Error("缩略图没读出来")));
    pic.src = address;
  });
}

async function 第一帧(最多毫秒 = 15000) {
  const 截至 = Date.now() + 最多毫秒;
  while (Date.now() < 截至) {
    const node = el("av-video");
    if (node && node.readyState >= 2 && node.videoWidth > 0 && node.videoHeight > 0) return node;
    await sleep(500);
  }
  const node = el("av-video");
  return node && node.readyState >= 2 && node.videoWidth > 0 ? node : null;
}

function 缩略图地址(编号) {
  return `${接口.缩略图}/${encodeURIComponent(String(编号 || ""))}`;
}

/**
 * 画面比对只留证据，不参与判定：就算这里没比成，也不能把一次成功的切换说成失败。
 * 判据用相对的那一个（更像新的还是更像旧的），不用绝对阈值——光线一变阈值就骗人。
 */
async function 回报画面证据(编号) {
  try {
    const video = await 第一帧();
    if (!video) throw new Error("还没等到新的画面");
    const 抓到的帧 = 指纹(video);
    if (!抓到的帧) throw new Error("画面没取下来");

    const 新的图 = await 读图(缩略图地址(编号)).catch(() => null);
    const 新的 = 新的图 ? 指纹(新的图) : null;
    if (!新的) throw new Error("新的比对图没画出来");
    const 距离新 = 平均差(抓到的帧, 新的);

    let 距离旧 = 距离新 + 1;
    if (state.切换前编号 && state.切换前编号 !== 编号) {
      const 旧的图 = await 读图(缩略图地址(state.切换前编号)).catch(() => null);
      const 旧的 = 旧的图 ? 指纹(旧的图) : null;
      if (旧的) 距离旧 = 平均差(抓到的帧, 旧的);
    }
    const 判定 = 距离新 < 距离旧 ? "是" : "否";
    await call(接口.画面证据, { 编号, 距离新, 距离旧, 判定 });
    console.info(判定 === "是"
      ? "[数字人] 画面确实是新选的这一位。"
      : "[数字人] 画面比对没站到新的一边（只记录，不影响结果）。");
  } catch (err) {
    console.info("[数字人] 这一次没比对成画面（只记录，不影响结果）：", err);
  }
}

/* ------------------------------------------------------------------ 同步 / 重启 */

async function syncNow() {
  const 编号 = state.当前 ? String(state.当前.编号 || "") : "";
  if (!编号) return;
  const back = await call(接口.同步, { 编号 });
  if (!back || !back.ok) {
    note(人话(back && back.说明, TEXT.同步好了), "警示");
    return;
  }
  note(人话(back.说明, TEXT.同步好了), "好消息");
  await refresh();
}

async function restartNow() {
  note(TEXT.切换中);
  const back = await call(接口.重启, {});
  if (!back || !back.ok) {
    note(人话(back && back.说明, TEXT.切换中断), "警示");
    return;
  }
  note(人话(back.说明, TEXT.服务运行中), "好消息");
  await refresh();
}

/* ------------------------------------------------------------------ 挂载 */

/**
 * 装一次。幂等：重复调用只重画，不会挂出两个面板。
 * @param {{ mount?: string|HTMLElement }} options
 */
export function mountAvatarPanel(options) {
  const wanted = options && options.mount;
  let host = null;
  if (typeof wanted === "string") host = el(wanted);
  else if (wanted && wanted.append) host = wanted;
  if (host) host.id = "av-panel";
  const panel = host || shell();
  buildSkeleton(panel);
  void refresh();
  bindEntry(panel);
  return panel;
}

/** 主页「数字人」入口的开合（UI_FIX_01 · ISSUE 2）。幂等：重复挂载只绑一次。 */
function bindEntry(panel) {
  const openBtn = el("gf-avatar-open");
  if (!openBtn || openBtn.dataset.avBound) return;
  openBtn.dataset.avBound = "1";
  openBtn.addEventListener("click", () => {
    if (el("av-panel-mount")) {
      if (!el("settings-modal")?.open) el("settings-btn")?.click();
      document.querySelector('#settings-tabs [data-tab="persona"]')?.click();
      panel.hidden = false;
      void refresh();
      return;
    }
    panel.hidden = !panel.hidden;
    if (!panel.hidden) void refresh();
  });
}

export { TEXT as 面板文案, 接口 as 接口清单 };

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mountAvatarPanel());
  } else {
    mountAvatarPanel();
  }
}
