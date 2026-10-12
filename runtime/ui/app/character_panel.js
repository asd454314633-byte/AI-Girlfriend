/**
 * 人物与「记住的事」面板（任务 §10 / §11 / §16）。
 *
 * 给完全不懂技术的人用：一个下拉换人，五个大按钮办事，全部中文，
 * 需要点确认的地方一律先把「要做什么、会影响几条」说清楚再动手。
 *
 * 三条规矩：
 *   1. 状态只认服务端。这里不存人物资料、不存记忆，localStorage 只留
 *      「面板开在哪一格」这种纯偏好；
 *   2. 一个字都不替用户写。人物台词、要记的事，都是用户给的，界面只搬运；
 *   3. 界面文案不出现内部名词（自动检查在 runtime/character/tests/run_all.py，
 *      浏览器侧在 tests/character_gate.html）。
 *
 * 挂载：`<script type="module" src="character_panel.js"></script>` 就够了，
 * 页面里有 #char-panel 就装进去，没有就自己插一个卡片，不需要构建步骤。
 */

const TEXT = {
  标题: "人物与记忆",
  说明: "选一个人物，或者告诉她该记住什么。",
  选择人物: "选择人物",
  新建: "新建",
  编辑: "编辑",
  记住的事: "记住的事",
  恢复: "恢复",
  删除: "删除",
  书稿: "读过的书",
  关闭: "收起",
  加载中: "正在读…",
  空白: "这里还没有内容。",
  成功: "办好了。",
  失败: "这一步没做成。",
  取消: "再想想",
  确定: "确定",
  继续: "接着做",
  人物名称: "人物名称",
  角色名称: "角色名称（她叫什么、你怎么叫她）",
  和说话的人: "她和你是什么关系",
  怎么说话: "她平时怎么说话",
  称呼方式: "提到她时用哪个字",
  不接的话: "哪些话她不接",
  开场一句: "她开口第一句",
  偏爱: "她偏爱什么（一行一个）",
  不爱: "她不爱什么（一行一个）",
  台词: "人物台词（你写的，系统一个字都不改）",
  台词提示: "写她怎么想、怎么接话。别写「根据我的设定」「我的性格是」这类话，一说就露馅。",
  来源: "资料从哪来",
  手动创建: "我自己填",
  导入书稿: "从一本她读过的书里整理",
  存好: "存好",
  先填名称: "请先填人物名称，至少两个字。",
  存好了: "已经存好了。",
  没存: "还没存上。",
  选书: "选一个 TXT 或 Markdown 文件",
  贴书: "也可以直接把正文贴在这里",
  书名: "这本书叫什么",
  收进来: "收进来",
  分析人物: "分析人物",
  分析提示: "读完之后系统会照着她读过的书，把她的样子一条条理出来，每一条都写清是原书里有的、还是按说法统计推的。",
  进度: "进行到哪一步",
  原著资料: "原著资料",
  人物设定: "人物设定",
  长期记忆: "长期记忆",
  当前关系: "当前关系",
  本次会话: "本次会话",
  新增记忆: "告诉她要记住",
  清空本次会话: "忘掉这一场",
  清空长期记忆: "忘掉长期记着的事",
  恢复初始: "回到刚认识的样子",
  导出人物: "整套存成一个文件",
  导入人物: "把存过的那份搬回来",
  查一查: "查一查她记得什么",
  查: "查",
  内容: "要记的事",
  类别: "这是哪一类",
  类别表: { 偏好: "偏好", 事实: "事实", 关系: "关系", 约定: "约定", 打算: "打算", 一起经历过的事: "一起经历过的事" },
  记下: "记下来",
  改: "改",
  删: "忘",
  改动: "改好",
  还没有: "这一格暂时是空的。",
  几条: "条",
  确认清这一场: "确定要忘记这一场说过的话吗？",
  确认清长期: "确定要忘记她长期记着的事吗？",
  确认删一条: "确定要忘记这一条吗？",
  确认删人物: "确定要删掉这个人物吗？",
  确认回刚认识: "确定要回到刚认识的样子吗？",
  说明清这一场: "只有这一场说的话会忘；她长期记着的事、读过的书，一条都不动。",
  说明清长期: "她长期记着的事会先收起来，还能一键还原；读过的书和人物设定完全不动。",
  说明删人物: "这个人物会先收起来，随时可以在「恢复」里放回来。",
  说明回刚认识: "这一场和长期记着的事都退回去，像刚认识一样；读过的书和人物设定全部保留。",
  影响条数: "会影响",
  影响单位: "条",
  办完了: "已经办好了：",
  没动静: "一条也没动。",
  导出中: "正在把整套存下来…",
  存到文件: "文件已经存好：",
  挑一份: "挑一份存过的备份",
  搬回来: "搬回来",
  换人: "换成她",
  台词露馅: "这几句先别写进台词里：",
  进度空闲: "现在没有正在做的事。",
  字数: "字",
  段: "段",
  本: "本",
  出处: "出处",
  原话: "原书里的那句",
  依据: "依据",
  没有出处: "这条没写出处",
};

const GROUPS = ["原著资料", "人物设定", "长期记忆", "当前关系", "本次会话"];
const PREF_KEY = "zerofrontier.charPanel.prefs";
const POLL_MS = 500;

let state = {
  人物: [],
  当前: "",
  当前名称: "",
  分组: [],
  开着哪一格: "长期记忆",
  台词: "",
  书稿: [],
  忙碌: false,
  会话: "",
};

const store = {
  read() {
    try {
      const raw = globalThis.localStorage?.getItem(PREF_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  },
  write(patch) {
    try {
      const next = Object.assign(store.read(), patch);
      globalThis.localStorage?.setItem(PREF_KEY, JSON.stringify(next));
    } catch {
      /* 存不下就算了：这只是界面偏好，不是数据 */
    }
  },
};

const el = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

async function call(path, body) {
  try {
    // 必须带 content-type：FastAPI 的 JSON 请求体是按头判定的，缺了就 422，
    // 面板只会显示「还没存上」，用户完全看不出为什么。
    const res = body
      ? await fetch(path, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        })
      : await fetch(path);
    const data = await res.json().catch(() => null);
    if (!data || typeof data !== "object") {
      return { ok: false, 说明: TEXT.失败, 状态: res.status };
    }
    return data;
  } catch {
    return { ok: false, 说明: "这台机器上的服务暂时联系不上，稍等一下再试。" };
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
  const box = el("char-note");
  if (!box) return;
  box.textContent = String(message || "");
  box.dataset.kind = kind || "普通";
}

function labelled(label, control, hint) {
  const wrap = make("label", "char-field");
  wrap.append(make("span", "char-field-label", label));
  wrap.append(control);
  if (hint) wrap.append(make("small", "char-field-hint", hint));
  return wrap;
}

function textInput(id, value, placeholder) {
  const box = make("input", "char-input");
  box.id = id;
  box.type = "text";
  box.value = value || "";
  if (placeholder) box.placeholder = placeholder;
  return box;
}

function areaInput(id, value, rows) {
  const box = make("textarea", "char-input char-area");
  box.id = id;
  box.value = value || "";
  box.rows = rows || 3;
  return box;
}

function button(label, cls, onClick) {
  const box = make("button", `char-btn ${cls || ""}`.trim(), label);
  box.type = "button";
  box.addEventListener("click", onClick);
  return box;
}

/** 中文确认框：破坏性动作一律先问，并且把「会影响几条」写在脸上。 */
function ask(title, question, detail, count, onYes) {
  const settingsDialog = el("settings-modal");
  const host = settingsDialog?.open ? settingsDialog : (el("char-host") || document.body);
  const back = make("div", "char-mask");
  const card = make("div", "char-dialog");
  card.append(make("h3", "char-dialog-title", title));
  card.append(make("p", "char-dialog-ask", question));
  if (detail) card.append(make("p", "char-dialog-detail", detail));
  if (count !== undefined && count !== null) {
    card.append(make("p", "char-dialog-count", `${TEXT.影响条数} ${count} ${TEXT.影响单位}。`));
  }
  const row = make("div", "char-row");
  row.append(button(TEXT.取消, "char-btn-quiet", () => back.remove()));
  row.append(button(TEXT.确定, "char-btn-danger", async () => {
    back.remove();
    await onYes();
  }));
  card.append(row);
  back.append(card);
  back.addEventListener("click", (event) => {
    if (event.target === back) back.remove();
  });
  host.append(back);
  const yes = card.querySelector(".char-btn-danger");
  if (yes) yes.focus();
}

/* ------------------------------------------------------------------ 骨架 */

function shell() {
  if (el("char-panel")) return el("char-panel");
  const host = el("char-panel-mount") || el("char-panel");
  const panel = make("section", "char-panel");
  panel.id = "char-panel";
  panel.dataset.mounted = "auto";
  (host || document.body).append(panel);
  return panel;
}

function buildSkeleton(panel) {
  panel.replaceChildren();
  const head = make("div", "char-head");
  head.append(make("h2", "char-title", TEXT.标题));
  head.append(make("p", "char-sub", TEXT.说明));

  const bar = make("div", "char-bar");
  const pick = make("select", "char-input char-select");
  pick.id = "char-pick";
  pick.addEventListener("change", async () => {
    const data = await call("/api/character/select", { character_id: pick.value });
    if (data.ok) {
      state.当前 = pick.value;
      note(String(data.说明 || TEXT.换人));
      await refresh();
    } else {
      note(String(data.说明 || TEXT.没存), "警示");
    }
  });
  bar.append(labelled(TEXT.选择人物, pick));
  const management = make("details", "char-advanced");
  management.append(make("summary", null, "人物资料与备份"));
  const actions = make("div", "char-row");
  actions.append(button(TEXT.新建, "char-btn-main", () => openEditor(null)));
  actions.append(button(TEXT.编辑, "char-btn-quiet", () => openEditor(state.当前)));
  actions.append(button(TEXT.书稿, "char-btn-quiet", () => toggleSection("book")));
  actions.append(button(TEXT.恢复, "char-btn-quiet", restoreCurrent));
  actions.append(button(TEXT.删除, "char-btn-quiet char-btn-danger", deleteCurrent));
  management.append(actions);
  head.append(bar);

  const noteBox = make("div", "char-note");
  noteBox.id = "char-note";
  noteBox.dataset.kind = "普通";
  noteBox.textContent = TEXT.加载中;
  head.append(noteBox);
  panel.append(head);
  panel.append(buildEditor());
  panel.append(buildBook());
  panel.append(buildMemory());
  panel.append(management);
}

function toggleSection(name) {
  const box = el(`char-sec-${name}`);
  if (!box) return;
  box.hidden = !box.hidden;
  const prefs = store.read();
  prefs.open = prefs.open || {};
  prefs.open[name] = !box.hidden;
  store.write({ open: prefs.open });
}

function applyPrefs() {
  const prefs = store.read();
  if (prefs && prefs.格子 && GROUPS.includes(prefs.格子)) state.开着哪一格 = prefs.格子;
  Object.entries((prefs && prefs.open) || {}).forEach(([name, value]) => {
    const box = el(`char-sec-${name}`);
    if (box && typeof value === "boolean") box.hidden = !value;
  });
}

/* ------------------------------------------------------------------ 编辑人物 */

function buildEditor() {
  const sec = make("section", "char-sec");
  sec.id = "char-sec-editor";
  sec.hidden = true;
  const form = make("div", "char-form");
  form.id = "char-editor-form";
  const footer = make("div", "char-row");
  footer.append(
    button(TEXT.存好, "char-btn-main", saveEditor),
    button(TEXT.关闭, "char-btn-quiet", () => { sec.hidden = true; }),
  );
  sec.append(make("h3", "char-sec-title", TEXT.新建), form, footer);
  return sec;
}

function openEditor(characterId) {
  const sec = el("char-sec-editor");
  const form = el("char-editor-form");
  if (!sec || !form) return;
  const row = (state.人物 || []).find((one) => one.编号 === characterId) || {};
  form.replaceChildren();
  form.append(labelled(TEXT.人物名称, textInput("char-display", row.人物名称), TEXT.先填名称));
  form.append(labelled(TEXT.角色名称, textInput("char-nick", row.角色名称)));
  form.append(labelled(TEXT.和说话的人, textInput("char-rel", row.和说话的人)));
  form.append(labelled(TEXT.怎么说话, textInput("char-style", row.怎么说话)));
  form.append(labelled(TEXT.称呼方式, textInput("char-pronoun", row.称呼方式)));
  form.append(labelled(TEXT.不接的话, textInput("char-boundary", row.不接的话)));
  form.append(labelled(TEXT.开场一句, textInput("char-greeting", row.开场一句)));
  form.append(labelled(TEXT.偏爱, areaInput("char-likes", (row.偏爱 || []).join("\n"), 2)));
  form.append(labelled(TEXT.不爱, areaInput("char-dislikes", (row.不爱 || []).join("\n"), 2)));
  form.append(labelled(TEXT.台词, areaInput("char-persona", state.台词, 6), TEXT.台词提示));
  form.dataset.编号 = characterId || "";
  sec.hidden = false;
  const first = el("char-display");
  if (first) first.focus();
}

async function saveEditor() {
  const form = el("char-editor-form");
  if (!form) return;
  const id = form.dataset.编号 || "";
  const value = (name) => String((el(name)?.value || "")).trim();
  const lines = (name) => value(name).split("\n").map((one) => one.trim()).filter(Boolean);
  const display = value("char-display");
  if (display.length < 2) {
    note(TEXT.先填名称, "警示");
    return;
  }
  const body = {
    character_id: id || undefined,
    display_name: display,
    nickname: value("char-nick"),
    relationship: value("char-rel"),
    speaking_style: value("char-style"),
    pronoun: value("char-pronoun"),
    boundary: value("char-boundary"),
    greeting: value("char-greeting"),
    likes: lines("char-likes"),
    dislikes: lines("char-dislikes"),
  };
  const edit = id
    ? await call("/api/character/edit", body)
    : await call("/api/character/create", Object.assign({ select: true }, body));
  if (!edit.ok) {
    note(String(edit.说明 || TEXT.没存), "警示");
    return;
  }
  const persona = String((el("char-persona")?.value || "")).trim();
  let personaBack = "";
  if (persona) {
    const saved = await call("/api/character/persona", {
      text: persona, character_id: edit.编号 || id || undefined,
    });
    personaBack = saved.ok ? "" : `${TEXT.台词露馅}${(saved.提醒 || saved.issues || []).join("；") || saved.说明 || ""}`;
  }
  note(String(edit.说明 || TEXT.存好了) + (personaBack ? ` ${personaBack}` : ""),
       personaBack ? "警示" : "好消息");
  await refresh();
  if (personaBack) openEditor(edit.编号 || id);
}

/* ------------------------------------------------------------------ 书稿 */

function buildBook() {
  const sec = make("section", "char-sec");
  sec.id = "char-sec-book";
  sec.hidden = true;
  sec.append(make("h3", "char-sec-title", TEXT.书稿));

  const pick = make("input", "char-input");
  pick.type = "file";
  pick.accept = ".txt,.md,.markdown,text/plain";
  pick.id = "char-book-file";
  const title = textInput("char-book-title", "", TEXT.书名);
  const body = areaInput("char-book-text", "", 6);
  const subject = textInput("char-book-subject", "", TEXT.角色名称);
  const bar = make("div", "char-row");
  bar.append(
    button(TEXT.收进来, "char-btn-main", () => importBook(false)),
    button(TEXT.分析人物, "char-btn-quiet", () => importBook(true)),
  );
  const meter = make("div", "char-meter");
  meter.id = "char-book-meter";
  const list = make("div", "char-list");
  list.id = "char-book-list";
  sec.append(
    labelled(TEXT.选书, pick),
    labelled(TEXT.书名, title),
    labelled(TEXT.贴书, body),
    labelled(TEXT.分析提示, subject),
    bar, meter, list,
  );
  return sec;
}

async function importBook(analyze) {
  const file = el("char-book-file");
  const text = String(el("char-book-text")?.value || "").trim();
  const picked = file && file.files && file.files[0];
  if (!text && !picked) {
    note("请先选一个 TXT 或 Markdown 文件，或者把正文贴进来。", "警示");
    return;
  }
  const body = {
    title: String(el("char-book-title")?.value || "").trim() || undefined,
    subject: String(el("char-book-subject")?.value || "").trim() || undefined,
    analyze: Boolean(analyze),
    wait: false,
    character_id: state.当前 || undefined,
  };
  if (picked) {
    note(`正在读「${picked.name}」…`);
    body.text = await picked.text();
    body.title = body.title || picked.name.replace(/\.[^.]+$/, "");
  } else {
    body.text = text;
  }
  const start = await call("/api/character/book/import", body);
  if (!start.ok || !start.作业编号) {
    note(String(start.说明 || TEXT.失败), "警示");
    return;
  }
  await watchJob(start.作业编号);
  await refresh();
}

async function watchJob(id) {
  const meter = el("char-book-meter");
  for (let guard = 0; guard < 600; guard += 1) {
    const data = await call(url("/api/character/book/progress", { job_id: id }));
    if (!data.ok) {
      note(String(data.说明 || TEXT.失败), "警示");
      return data;
    }
    const percent = Number(data.完成度 || 0);
    if (meter) {
      meter.replaceChildren();
      const line = make("div", "char-meter-text",
        `${data.进行到哪一步 || ""} · ${percent}%`
        + (Number(data.总数) ? `（${data.已完成}/${data.总数}）` : ""));
      const rail = make("div", "char-meter-rail");
      const fill = make("div", "char-meter-fill");
      fill.style.width = `${Math.max(2, Math.min(100, percent))}%`;
      rail.append(fill);
      meter.append(line, rail);
    }
    if (data.状态 === "完成" || data.状态 === "失败") {
      note(String(data.说明 || (data.状态 === "完成" ? TEXT.办完了 : TEXT.失败)),
           data.状态 === "完成" ? "好消息" : "警示");
      return data;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  note("这一步太久了，先停在这里；可以重新点一次。", "警示");
  return { ok: false };
}

/* ------------------------------------------------------------------ 记住的事 */

function buildMemory() {
  const sec = make("section", "char-sec");
  sec.id = "char-sec-memory";
  sec.hidden = false;
  sec.append(make("h3", "char-sec-title", TEXT.记住的事));

  const tabs = make("div", "char-tabs");
  tabs.id = "char-tabs";
  GROUPS.forEach((name) => {
    const tab = button(name, "char-tab", () => {
      state.开着哪一格 = name;
      store.write({ 格子: name });
      renderGroups();
    });
    tab.dataset.group = name;
    tabs.append(tab);
  });
  const advanced = make("details", "char-advanced");
  advanced.append(make("summary", null, "分类、导入导出与清理"));
  advanced.append(tabs);

  const finder = make("div", "char-row");
  const query = textInput("char-query", "", TEXT.查一查);
  finder.append(
    query,
    button(TEXT.查, "char-btn-quiet", async () => {
      const word = String(query.value || "").trim();
      if (!word) {
        await refresh();
        return;
      }
      const data = await call(url("/api/memory/search",
        { q: word, character_id: state.当前, limit: 12 }));
      renderRows(((data.命中 || []) ).map((one) => one.内容 ? { 内容: one.内容, 编号: one.编号 } : one),
                 `「${word}」${data.说明 || ""}`, "长期记忆");
    }),
      button("查看全部", "char-btn-quiet", () => refresh()),
  );
  sec.append(finder);

  const adder = make("div", "char-row char-row-add");
  const what = textInput("char-add-text", "", TEXT.内容);
  const kind = make("select", "char-input char-select");
  kind.id = "char-add-kind";
  Object.entries(TEXT.类别表).forEach(([key, label]) => {
    const opt = make("option", null, label);
    opt.value = key;
    kind.append(opt);
  });
  adder.append(
    labelled(TEXT.内容, what),
    button(TEXT.记下, "char-btn-main", async () => {
      const text = String(what.value || "").trim();
      if (!text) {
        note("请先写下要她记住的事。", "警示");
        return;
      }
      const label = kind.selectedOptions?.[0]?.textContent || "事实";
      const data = await call("/api/memory/add", {
        text, kind: keyOfKind(label), group: "长期记忆", session_id: state.会话 || undefined,
      });
      note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)),
           data.ok ? "好消息" : "警示");
      if (data.ok) {
        what.value = "";
        await refresh();
      }
    }),
  );
  sec.append(adder);

  const tools = make("div", "char-row char-row-tools");
  tools.append(
    button(TEXT.清空本次会话, "char-btn-quiet", clearSession),
    button(TEXT.清空长期记忆, "char-btn-quiet char-btn-danger", clearLongTerm),
    button(TEXT.恢复初始, "char-btn-quiet", restoreInitial),
    button(TEXT.导出人物, "char-btn-quiet", exportCharacter),
    button(TEXT.导入人物, "char-btn-quiet", openImportDialog),
  );
  advanced.append(labelled(TEXT.类别, kind), tools);
  sec.append(advanced);
  const rows = make("div", "char-list");
  rows.id = "char-rows";
  sec.append(rows);
  return sec;
}

function keyOfKind(label) {
  const table = { 偏好: "preference", 事实: "fact", 关系: "relationship",
    约定: "commitment", 打算: "goal", 一起经历过的事: "shared_event" };
  return table[label] || "fact";
}

function renderGroups() {
  const rows = el("char-rows");
  const tabs = el("char-tabs");
  if (!rows || !tabs) return;
  [...tabs.querySelectorAll(".char-tab")].forEach((tab) => {
    tab.dataset.on = String(tab.dataset.group === state.开着哪一格);
  });
  const group = (state.分组 || []).find((one) => one.名字 === state.开着哪一格);
  if (!group) {
    rows.replaceChildren(make("p", "char-empty", TEXT.还没有));
    return;
  }
  renderRows(group.条目 || [], `${group.名字} · 共 ${group.条数} ${TEXT.几条}`, group.名字);
}

function renderRows(items, headline, groupName) {
  const rows = el("char-rows");
  if (!rows) return;
  rows.replaceChildren();
  rows.append(make("p", "char-list-head", headline || ""));
  if (!items.length) {
    rows.append(make("p", "char-empty", TEXT.还没有));
    return;
  }
  items.forEach((item) => rows.append(renderRow(item, groupName)));
}

function renderRow(item, groupName) {
  const line = make("div", "char-item");
  const body = make("p", "char-item-text", String(item.内容 || ""));
  line.append(body);
  const metaBits = [item.类别, item.依据, item.哪一面, item.出处, item.改动时间 || item.记下时间]
    .filter(Boolean);
  if (metaBits.length) line.append(make("small", "char-item-meta", metaBits.join(" · ")));
  const canEdit = item.可以亲手改 !== false;
  if (item.编号 && canEdit && groupName !== "人物设定") {
    const box = make("div", "char-row");
      box.append(button("修改", "char-btn-quiet char-btn-small", () => editRow(line, item, groupName)));
      const more = make("details", "char-item-more");
      more.append(make("summary", null, "更多"),
        button(TEXT.删, "char-btn-quiet char-btn-small char-btn-danger",
          () => deleteRow(item, groupName)));
      box.append(more);
    line.append(box);
  } else if (!canEdit) {
    line.append(make("small", "char-item-lock", "这条是原书里写定的，改不了；要补就另开一条。"));
  }
  return line;
}

/** 就地改成输入框：改完回填，改不动就原样放回去，不会把整屏刷没。 */
function editRow(line, item, groupName) {
  const box = make("textarea", "char-input char-area");
  box.value = String(item.内容 || "");
  box.rows = 3;
  const yes = button(TEXT.改动, "char-btn-main", async () => {
    const text = String(box.value || "").trim();
    if (!text) {
      line.replaceChildren(renderRow(item, groupName));
      return;
    }
    const data = groupName === "原著资料"
      ? await call("/api/character/canon/update", { fact_id: item.编号, text })
      : await call("/api/memory/update", { memory_id: item.编号, text });
    note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
    if (data.ok) await refresh();
    else line.replaceChildren(renderRow(item, groupName));
  });
  const no = button(TEXT.取消, "char-btn-quiet", () => line.replaceChildren(renderRow(item, groupName)));
  line.replaceChildren(box, makeRow(yes, no));
  box.focus();
}

function makeRow(...nodes) {
  const row = make("div", "char-row");
  nodes.forEach((one) => row.append(one));
  return row;
}

function deleteRow(item, groupName) {
  ask(TEXT.删, TEXT.确认删一条, String(item.内容 || "").slice(0, 60), 1, async () => {
    const data = groupName === "原著资料"
      ? await call("/api/character/canon/delete", { fact_id: item.编号, confirm: "确认" })
      : await call("/api/memory/delete", { memory_id: item.编号, confirm: "确认" });
    note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
    if (data.ok) await refresh();
  });
}

function groupCount(name) {
  const group = (state.分组 || []).find((one) => one.名字 === name);
  return group ? Number(group.条数 || 0) : 0;
}

function clearSession() {
  ask(TEXT.清空本次会话, TEXT.确认清这一场, TEXT.说明清这一场, groupCount("本次会话"),
      async () => {
        const data = await call("/api/memory/clear_session", {
          session_id: state.会话, character_id: state.当前, confirm: "确认",
        });
        note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
        if (data.ok) await refresh();
      });
}

function clearLongTerm() {
  ask(TEXT.清空长期记忆, TEXT.确认清长期, TEXT.说明清长期, groupCount("长期记忆"), async () => {
    const data = await call("/api/memory/clear_long_term", {
      character_id: state.当前, confirm: "确认",
    });
    note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
    if (data.ok) await refresh();
  });
}

function restoreInitial() {
  ask(TEXT.恢复初始, TEXT.确认回刚认识, TEXT.说明回刚认识,
      groupCount("长期记忆") + groupCount("本次会话") + groupCount("当前关系"), async () => {
        const data = await call("/api/memory/restore", {
          character_id: state.当前, mode: "initial", confirm: "确认",
        });
        note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
        if (data.ok) await refresh();
      });
}

async function restoreCurrent() {
  const back = await call("/api/memory/restore", { character_id: state.当前, mode: "archived" });
  const profile = await call("/api/character/restore", { character_id: state.当前 });
  note(`${back.说明 || ""}${profile.ok ? " 人物和资料也都放着。" : ""}`,
       back.ok ? "好消息" : "警示");
  if (back.ok) await refresh();
}

function deleteCurrent() {
  const named = state.当前名称 || state.当前 || "这个人物";
  ask(TEXT.删除, TEXT.确认删人物, TEXT.说明删人物, groupCount("长期记忆"), async () => {
    const data = await call("/api/character/delete", { character_id: state.当前, confirm: "确认" });
    note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
    if (data.ok) await refresh();
  });
}

async function exportCharacter() {
  note(TEXT.导出中);
  const data = await call(url("/api/character/export", { write: true, character_id: state.当前 }));
  if (data.ok) {
    note(`${TEXT.存到文件}${data.文件 || ""} ${data.说明 || ""}`, "好消息");
    await refresh();
    return;
  }
  note(String(data.说明 || TEXT.失败), "警示");
}

async function openImportDialog() {
  const found = await call("/api/character/bundles");
  const back = make("div", "char-mask");
  const card = make("div", "char-dialog");
  card.append(make("h3", "char-dialog-title", TEXT.导入人物));
  const rows = (found.备份文件 || []);
  if (!rows.length) {
    card.append(make("p", "char-dialog-detail", "还没有存过整套备份。先点「整套存成一个文件」。"));
  } else {
    const pick = make("select", "char-input char-select");
    rows.forEach((one) => {
      const opt = make("option", null, `${one.人物} · ${one.导出时间} · ${one.资料条数} 条资料 / ${one.记忆条数} 条记忆`);
      opt.value = one.文件;
      pick.append(opt);
    });
    card.append(labelled(TEXT.挑一份, pick));
    card.append(makeRow(
      button(TEXT.取消, "char-btn-quiet", () => back.remove()),
      button(TEXT.搬回来, "char-btn-main", async () => {
        const data = await call("/api/character/import", { path: pick.value, set_active: true });
        back.remove();
        note(String(data.说明 || (data.ok ? TEXT.成功 : TEXT.失败)), data.ok ? "好消息" : "警示");
        if (data.ok) await refresh();
      }),
    ));
  }
  card.append(makeRow(button(TEXT.关闭, "char-btn-quiet", () => back.remove())));
  back.append(card);
  (el("char-host") || document.body).append(back);
}

/* ------------------------------------------------------------------ 数据刷新 */

async function refresh() {
  const data = await call(url("/api/character/overview", { character_id: state.当前 || undefined,
    session_id: state.会话 || undefined, items: 40 }));
  if (!data.ok) {
    note(String(data.说明 || TEXT.失败), "警示");
    return;
  }
  const list = await call("/api/character/list");
  state.人物 = list.人物列表 || [];
  state.当前 = data.编号 || state.当前;
  state.当前名称 = data.人物名称 || "";
  state.分组 = data.分组 || [];
  state.台词 = data.台词 || "";
  const books = await call(url("/api/character/book/list", { character_id: state.当前 }));
  state.书稿 = books.ok ? (books.书稿 || []) : [];
  const pick = el("char-pick");
  if (pick) {
    pick.replaceChildren(...state.人物.map((one) => {
      const opt = make("option", null, one.人物名称 || one.编号);
      opt.value = one.编号;
      if (one.编号 === state.当前) opt.selected = true;
      return opt;
    }));
  }
  renderGroups();
  renderBooks();
  const noteBox = el("char-note");
  if (noteBox && (!noteBox.textContent || noteBox.textContent === TEXT.加载中)) {
    note(`正在查看${state.当前名称 || state.当前}的记忆 · 长期记忆 ${groupCount("长期记忆")} 条`);
  }
}

function renderBooks() {
  const list = el("char-book-list");
  if (!list) return;
  list.replaceChildren();
  const data = state.书稿 || [];
  if (!data.length) {
    list.append(make("p", "char-empty", "还没有收过她读过的书。"));
    return;
  }
  data.forEach((one) => {
    const line = make("div", "char-item");
    line.append(make("p", "char-item-text", `${one.书名 || "未命名"} · ${one.字数} ${TEXT.字数} · ${one.段数} ${TEXT.段}`));
    list.append(line);
  });
}

function freshSession() {
  const maker = globalThis.crypto;
  if (maker && typeof maker.randomUUID === "function") return maker.randomUUID();
  return `s${Date.now().toString(36)}`;
}

/**
 * 装一次。幂等：重复调用只重画，不会挂出两个面板。
 * @param {{ mount?: string|HTMLElement, sessionId?: string }} options
 */
export function mountCharacterPanel(options) {
  const wanted = options && options.mount;
  let host = null;
  if (typeof wanted === "string") host = el(wanted);
  else if (wanted && wanted.append) host = wanted;
  if (host && host.id !== "char-panel") {
    host.id = "char-panel";
  }
  const panel = host || shell();
  state.会话 = (options && options.sessionId) || state.会话 || freshSession();
  buildSkeleton(panel);
  applyPrefs();
  // 设置里的记忆首页始终可读；旧收起偏好不能让入口变成空页。
  if (el("char-panel-mount")) el("char-sec-memory").hidden = false;
  const prefs = store.read();
  if (!prefs.open) {
    ["memory", "book"].forEach((name) => {
      const box = el(`char-sec-${name}`);
      if (box) box.hidden = name !== "memory";
    });
  }
  void refresh();
  return panel;
}

export { TEXT as 面板文案, GROUPS as 分组名单 };

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mountCharacterPanel());
  } else {
    mountCharacterPanel();
  }
}
