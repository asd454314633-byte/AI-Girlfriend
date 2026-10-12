// @ts-check
/**
 * Reading uses the existing 8765 TTS -> 8010 Avatar -> WebRTC path.
 * A response-specific output boundary advances the checkpoint; the shared
 * is_speaking flag is only a health observation, never segment attribution.
 * At most two segments have unconsumed input across TTS, browser and Avatar.
 * Consumed input releases synthesis capacity; output-end commits the checkpoint.
 * The playback ledger additionally retains at most one consumed segment (cap 3).
 * Generation/epoch cancellation invalidates pending work before asynchronous I/O.
 */

const TICK_MS = 150;
const SPEAKING_POLL_MS = 300;
const MAX_TICK_FAILURES = 6;
const MAX_WAIT_TICKS = 100;
// Bounded prefetch; response boundaries identify each segment.
const READING_PREFETCH_LIMIT = 2;
const READING_PLAYBACK_LEDGER_LIMIT = 3;

const S = {
  IDLE: "IDLE",
  LOADING: "LOADING",
  READING: "READING",
  INTERRUPTING: "INTERRUPTING",
  INTERRUPTED: "INTERRUPTED",
  ANSWERING: "ANSWERING",
  RESUMING: "RESUMING",
  PAUSED: "PAUSED",
  STOPPED: "STOPPED",
  FINISHED: "FINISHED",
  ERROR: "ERROR",
};

/** 状态 -> 给人看的那句话（不提接口、不提内部名词）。 */
const STATE_LABEL = {
  IDLE: "待开始",
  LOADING: "正在准备",
  READING: "正在朗读",
  INTERRUPTING: "正在停下",
  INTERRUPTED: "你在说话",
  ANSWERING: "回答问题中",
  RESUMING: "正在接着读",
  PAUSED: "已暂停",
  STOPPED: "已停止",
  FINISHED: "已读完",
  ERROR: "出了点问题",
};

const st = {
  /** @type {string} */ docId: "",
  /** @type {Array<{index:number,title:string,paragraph_count:number}>} */ chapters: [],
  /** @type {Array<{index:number,text:string,paragraph:number}>} */ chunks: [],
  chapter: 0,
  chunk: 0,
  /** @type {string} */ state: S.IDLE,
  /** READING_PIPELINE_V2 在飞账本（FIFO，按提交顺序）。每项：
   *  { index: 提交的 chunk_index, unitEnd: 这一段播完时进度应落在哪,
   *    requiresPlayback: B 策略长段必须见到 is_speaking 才算播过,
   *    serverSubmitted: 提交成功时的服务端 submitted 计数,
   *    heard: 观察到过出声, synthDone: 已确认合成完（音频已可到 8010）,
   *    silenceTicks: 连续静音拍数, submittedAt: 提交时间戳(ms) } */
  /** @type {Array<object>} */ ledger: [],
  /** planner 的待定产物：策略 B 合并段落时 /v1/speak 回 pending + next_chunk_index，
   *  下一次提交必须用它；非空时提交车道不许另开新请求。 */
  plannerNext: null,
  /** Read-only lookahead; never a playback queue or a committed checkpoint. */
  chapterCache: null,
  /** 预取验收时间线（acceptance 用）：submit/heard/finish/progress 事件按序记录。 */
  /** @type {Array<object>} */ timeline: [],
  /** 用户手动滚过陪读正文的时间戳：宽限期内渲染函数不自动回中（防抢滚动）。 */
  userScrollUntil: 0,
  /** @type {number} */ timer: 0,
  failures: 0,
  waiting: 0,
  /** 当前提交到 8765 的请求，暂停时要等它落地后再停。 */
  speakingRequest: null,
  resumePending: false,
  resumeEpoch: 0,
  /** 服务端朗读 generation：每次停都 +1，用来挡住"停止之后才到"的那一段。 */
  generation: 0,
  /** tick 重入闸：并发的 tick 会把进度推两遍。 */
  ticking: false,
  /** 已念完的段数（只在真的出过声之后累加），用于进度显示与自检。 */
  /** @type {string[]} */ doneChunks: [],
};

/** 验收时间线：只留最近 60 条，事件名 + 段号 + 时间戳。 */
function noteTimeline(event, chunk, extra) {
  const entry = { event, chunk, at: performance.timeOrigin + performance.now(), ...(extra || {}) };
  st.timeline.push(entry);
  if (st.timeline.length > 60) st.timeline.shift();
  return entry;
}

// Instruction identity only: independent of checkpoint and TTS generation.
let instructionRevision = 0;
let strategySwitching = false;
let selectedStrategy = "B";

function renderStrategy() {
  document.querySelectorAll('[data-reading-strategy]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.readingStrategy === selectedStrategy));
    button.disabled = strategySwitching;
  });
}

async function refreshStrategy() {
  const r = await api('/api/reading/strategy');
  if (!r.ok || !r.json.ok) { say('读书模式读取失败，请稍后重试'); return; }
  selectedStrategy = r.json.strategy;
  renderStrategy();
}

async function selectStrategy(value) {
  if (strategySwitching || value === selectedStrategy) return;
  strategySwitching = true;
  renderStrategy();
  try {
    cancelResume();
    if ([S.READING, S.RESUMING, S.INTERRUPTING].includes(st.state) || st.ledger.length || st.plannerNext !== null || st.speakingRequest) {
      await halt('正在切换读书模式，断点保留', 'PAUSED');
    }
    const r = await api('/api/reading/strategy', {strategy: value});
    if (!r.ok || !r.json.ok) throw new Error(r.json.reason || '保存失败');
    selectedStrategy = r.json.strategy;
    st.generation = Number(r.json.generation);
    noteTimeline('strategy_changed', st.chunk, {strategy: selectedStrategy, generation: st.generation});
    say(`已切换${{A:'原始模式 A',B:'智能模式 B',C:'连续模式 C'}[selectedStrategy]}，继续朗读将从当前断点开始`);
  } catch (err) {
    say(`读书模式切换失败：${String(err)}`);
    await refreshStrategy();
  } finally {
    strategySwitching = false;
    renderStrategy();
  }
}
export function readingInstructionContext() {
  return { documentId: st.docId, documentRevision: instructionRevision };
}

const $ = (/** @type {string} */ id) =>
  /** @type {HTMLElement | null} */ (document.getElementById(id));

/** @param {string} url @param {object} [body] */
async function api(url, body) {
  try {
    const res = await fetch(url, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined);
    const json = await res.json().catch(() => ({}));
    return { ok: res.ok, json };
  } catch (err) {
    return { ok: false, json: { reason: String(err) } };
  }
}

/** @param {string} text */
function say(text) {
  const el = $("reading-status");
  if (el) el.textContent = text || "";
}

/** 状态机的唯一写入口：状态一变，按钮和进度一起重画。 */
function setState(next) {
  st.state = next;
  renderState();
  updateButtons();
}

// ---------------------------------------------------------------- 渲染

function renderDocs(/** @type {Array<object>} */ docs) {
  const select = /** @type {HTMLSelectElement | null} */ ($("reading-doc"));
  if (!select) return;
  select.replaceChildren(
    ...docs.map((d) => {
      const opt = document.createElement("option");
      opt.value = String(d.document_id);
      const prog = /** @type {{chapter_index?:number,status?:string}} */ (d.progress || {});
      const where = prog.status && prog.status !== "UNOPENED"
        ? (prog.status === "DONE" ? "（已读完 100%）" : `（读到第 ${(prog.chapter_index ?? 0) + 1} 节）`)
        : "（未开始）";
      opt.textContent = `${d.title}${where}`;
      if (d.document_id === st.docId) opt.selected = true;
      return opt;
    }),
  );
  if (!docs.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "书库还空着";
    select.replaceChildren(opt);
  }
}

function renderChapters() {
  const select = /** @type {HTMLSelectElement | null} */ ($("reading-chapter"));
  if (!select) return;
  select.replaceChildren(
    ...st.chapters.map((c) => {
      const opt = document.createElement("option");
      opt.value = String(c.index);
      opt.textContent = c.title || `第 ${c.index + 1} 节`;
      if (c.index === st.chapter) opt.selected = true;
      return opt;
    }),
  );
}

function renderText() {
  const box = $("reading-text");
  if (!box) return;
  box.replaceChildren(
    ...st.chunks.map((c) => {
      const p = document.createElement("p");
      p.className = "reading-line" + (c.index === st.chunk ? " now" : "");
      p.textContent = c.text;
      p.addEventListener("click", () => {
        void jumpTo(c.index, st.state === S.READING);
      });
      return p;
    }),
  );
  centerNowLine(true);
}

/**
 * 把 .now 行滚到 #reading-text 容器中部——【只滚这个容器】。
 * SCROLL_FIX_V1：旧实现 scrollIntoView({block:"center"}) 会滚动**所有可滚动的
 * 祖先（含 document）**，这是"每次生成页面自动下拉"的头号根因。改用
 * offsetTop 直接写容器自己的 scrollTop，祖先一概不动。
 * @param {boolean} [respectUserScroll] 用户近期手动滚过时是否让位
 */
function centerNowLine(respectUserScroll) {
  const box = $("reading-text");
  if (!box) return;
  if (respectUserScroll && Date.now() < st.userScrollUntil) return; // 你在看别处，不抢
  const now = /** @type {HTMLElement | null} */ (box.querySelector(".reading-line.now"));
  if (!now) return;
  const target = now.offsetTop - (box.clientHeight - now.offsetHeight) / 2;
  if (Math.abs(box.scrollTop - target) > 4) {
    box.dataset.autoScrolling = "1";   // 让 scroll 监听别把程序写入当作用户滚动
    box.scrollTop = target;
  }
}

/** 用户手动滚动陪读正文后的一段宽限期内，自动回中让位。 */
function bindReadingTextScrollGuard() {
  const box = $("reading-text");
  if (!box || box.dataset.scrollGuardBound === "1") return;
  box.dataset.scrollGuardBound = "1";
  const mark = () => { st.userScrollUntil = Date.now() + 15000; };
  box.addEventListener("wheel", mark, { passive: true });
  box.addEventListener("touchmove", mark, { passive: true });
  box.addEventListener("scroll", () => {
    // 滚动可能来自自动回中本身（程序写入 scrollTop 也触发 scroll 事件）：
    // 只把"不是程序写入"的滚动当作用户行为。程序写入集中在 centerNowLine，
    // 它写完后一拍内不会再写；用 4px 死区 + 写后清标记区分。
    if (box.dataset.autoScrolling === "1") { delete box.dataset.autoScrolling; return; }
    mark();
  }, { passive: true });
}

function renderProgress() {
  const line = $("reading-progress-text");
  if (line) {
    const total = st.chunks.length;
    line.textContent = total
      ? `第 ${st.chunk + 1} / ${total} 段（第 ${st.chapter + 1} 节）`
      : "还没开始";
  }
  const state = $("reading-state-text");
  if (state) state.textContent = STATE_LABEL[st.state] || st.state;
}

function renderState() {
  renderProgress();
}

/**
 * 按钮可用性只由状态派生（§22）：
 *   IDLE/FINISHED/STOPPED/PAUSED → 可以开始；READING → 可以暂停/停止；
 *   ANSWERING 期间不许连点"继续"开出第二条朗读 worker。
 */
function updateButtons() {
  const play = /** @type {HTMLButtonElement | null} */ ($("reading-play"));
  const pause = /** @type {HTMLButtonElement | null} */ ($("reading-pause"));
  const stop = /** @type {HTMLButtonElement | null} */ ($("reading-stop"));
  const busy = st.state === S.READING || st.state === S.INTERRUPTING
    || st.state === S.ANSWERING || st.state === S.RESUMING;
  if (play) {
    play.textContent = (st.state === S.PAUSED || st.state === S.STOPPED
      || st.state === S.INTERRUPTED) ? "继续朗读" : "开始朗读";
    play.disabled = busy || !st.docId;
  }
  if (pause) pause.disabled = st.state !== S.READING;
  if (stop) stop.disabled = !(st.state === S.READING || st.state === S.PAUSED
    || st.state === S.INTERRUPTED || st.state === S.ANSWERING);
  renderProgress();
}

// ---------------------------------------------------------------- 取数

async function refreshLibrary(/** @type {string} */ keepDoc) {
  const r = await api("/api/reading/documents");
  const docs = /** @type {Array<object>} */ (r.json.documents || []);
  renderDocs(docs);
  const want = keepDoc || st.docId || (docs[0] && String(docs[0].document_id)) || "";
  if (want && (!st.docId || want !== st.docId)) await openDoc(want);
}

/** @param {string} document_id */
async function openDoc(document_id) {
  if (!document_id) return;
  instructionRevision += 1;
  if (st.state === S.READING || st.state === S.RESUMING) await pause("已暂停");
  cancelResume();
  st.ledger = [];
  st.plannerNext = null;
  setState(S.LOADING);
  const r = await api(`/api/reading/document?document_id=${encodeURIComponent(document_id)}`);
  if (!r.ok) { setState(S.ERROR); say(`读不到这本书：${r.json?.detail?.reason || "?"}`); return; }
  const doc = /** @type {any} */ (r.json);
  stopTimer();
  st.docId = document_id;
  const docSelect = /** @type {HTMLSelectElement | null} */ ($("reading-doc"));
  if (docSelect) docSelect.value = document_id;
  st.chapters = doc.chapter_titles || [];
  st.chapter = Number(doc.progress?.chapter_index || 0);
  st.chunk = Number(doc.progress?.chunk_index || 0);
  renderChapters();
  await loadChapter();
  setState(S.IDLE);
  say(doc.progress?.status === "DONE"
    ? `${doc.title} · 已读完 100%`
    : `${doc.title} · 断点在第 ${st.chapter + 1} 节第 ${st.chunk + 1} 段`);
}

async function loadChapter() {
  const docId = st.docId, chapter = st.chapter, epoch = st.resumeEpoch;
  const cached = st.chapterCache;
  const r = cached && sameReadingSession(cached) && cached.chapter === chapter
    ? await cached.promise
    : await api(`/api/reading/chapter?document_id=${encodeURIComponent(docId)}&chapter=${chapter}`);
  if (docId !== st.docId || chapter !== st.chapter || epoch !== st.resumeEpoch) return false;
  if (!r?.ok) { say("这一节取不到原文"); return false; }
  st.chunks = /** @type {Array<any>} */ (r.json.chunks || []);
  if (st.chunk >= st.chunks.length) st.chunk = Math.max(st.chunks.length - 1, 0);
  renderText();
  return true;
}

function sameReadingSession(target) {
  return target.docId === st.docId && target.generation === st.generation
    && target.epoch === st.resumeEpoch;
}

function prepareChapter(chapter) {
  const old = st.chapterCache;
  if (old && old.chapter === chapter && sameReadingSession(old)) return old;
  const cached = { docId: st.docId, generation: st.generation, epoch: st.resumeEpoch,
    chapter, chunks: null, ready: false, promise: null };
  st.chapterCache = cached;
  noteTimeline('chapter_prefetch_start', 0, { chapter, generation: cached.generation });
  cached.promise = api(`/api/reading/chapter?document_id=${encodeURIComponent(cached.docId)}&chapter=${chapter}`)
    .then(r => {
      if (st.chapterCache !== cached || !sameReadingSession(cached)) return null;
      cached.ready = true;
      cached.chunks = r.ok ? r.json.chunks || [] : null;
      noteTimeline('chapter_prefetch_complete', 0,
        { chapter, ok: r.ok, generation: cached.generation });
      return r;
    });
  return cached;
}

// ---------------------------------------------------------------- 播放真相

/**
 * 数字人此刻在不在发声。`null` = 问不出来（没连上 / 8010 不应），此时朗读是没有
 * 出口的，不能靠猜推进。
 * @returns {Promise<boolean|null>}
 */
async function avatarSpeaking() {
  const av = /** @type {any} */ (window).AvatarSync;
  const sid = av ? String(av.sessionid || "0") : "0";
  if (sid === "0") return null;
  try {
    const res = await fetch(`${String(av.config.livetalkingBase).replace(/\/+$/, "")}/is_speaking`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionid: sid }),
    });
    const j = /** @type {any} */ (await res.json());
    return j.code === 0 ? Boolean(j.data) : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- 朗读状态机

function stopTimer() {
  if (st.timer) window.clearTimeout(st.timer);
  st.timer = 0;
}

/** 作废一切"待会儿自动接着念"的安排。停止、暂停、切模式都要走这一下。 */
function cancelResume() {
  st.resumePending = false;
  st.resumeEpoch += 1;
  st.chapterCache = null;
}

/** 让服务端把 generation 推进一步：此后到达的旧 speak 一律被拒（审计 §8）。 */
async function bumpServerGeneration(status) {
  const r = await api("/api/reading/pause", {
    document_id: st.docId,
    chapter_index: st.chapter,
    chunk_index: st.chunk,
    status: status || "PAUSED",
  });
  const gen = Number(r.json?.generation);
  if (Number.isFinite(gen) && gen >= 0) st.generation = gen;
  return r;
}

/**
 * 提交一段原文给 TTS（READING_PIPELINE_V2）。
 * 成功（非 pending）时压入在飞账本；pending（策略 B 合并中）返回 planner 的
 * next_chunk_index；BACKPRESSURE / 服务端忙交回调用方下拍再试。
 * @param {number} submitIndex
 * @returns {Promise<'submitted'|'pending'|'retry'|'stale'|'empty'|'error'>}
 */
async function submitChunk(target) {
  if (st.state !== S.READING && st.state !== S.RESUMING) return "retry";
  if (!sameReadingSession(target)) return "stale";
  const { index: submitIndex, chapter, chunks } = target;
  const seg = chunks[submitIndex];
  if (!seg || !String(seg.text).trim()) return "empty";
  noteTimeline("prefetch_enqueue", submitIndex, { chapter, generation: st.generation });
  noteTimeline("submit_start", submitIndex, { chapter, generation: st.generation });
  const readingKey = `${st.generation}:${st.docId}:${chapter}:${submitIndex}:${performance.now()}`;
  const avatar = /** @type {any} */ (window).AvatarSync;
  avatar.expectReading(readingKey);
  const request = api("/api/reading/speak", {
    document_id: st.docId,
    chapter_index: chapter,
    chunk_index: submitIndex,
    generation: st.generation,      // 号小了 = 停止之后才到的陈旧请求，服务端会拒
  });
  st.speakingRequest = request;
  const r = await request;
  if (st.speakingRequest === request) st.speakingRequest = null;
  if (!sameReadingSession(target) || (st.state !== S.READING && st.state !== S.RESUMING)) { avatar.forgetReading(readingKey); return "stale"; }
  const j = /** @type {any} */ (r.json);
  if (!j.ok) {
    avatar.forgetReading(readingKey);
    if (j.reason === "STALE_GENERATION") return "stale"; // 停止之后才回来：丢掉，不出声
    // 背压说明服务端还有音频在合成，下个 tick 再试，不算失败。
    if (j.reason === "BACKPRESSURE") return "retry";
    setState(S.ERROR);
    say(`朗读没起来：${j.reason || "?"}（语音服务 8765 是否在线）`);
    return "error";
  }
  if (j.pending) {
    avatar.forgetReading(readingKey);
    // 策略 B 合并中：这一段还没进 TTS，下一次提交必须用 planner 给的序号。
    st.plannerNext = { ...target, index: Number(j.next_chunk_index) };
    return "pending";
  }
  st.plannerNext = null;
  const gen = Number(j.generation);
  if (Number.isFinite(gen) && gen >= 0) st.generation = gen;
  const unitEnd = Number.isInteger(j.completed_chunk_index) ? j.completed_chunk_index : submitIndex;
  // A B unit may start at an earlier pending source than the request that
  // finally submitted it. Resume must retain that first uncommitted source.
  const firstSource = j.source_chunks?.[0];
  const checkpointIndex = firstSource?.chapter_index === chapter
    && Number.isInteger(firstSource.chunk_index) && firstSource.chunk_index <= submitIndex
    ? firstSource.chunk_index : submitIndex;
  st.ledger.push({
    readingKey,
    chapter,
    chunks,
    index: submitIndex,
    checkpointIndex,
    unitEnd,
    // B pending/merged units cannot use the old lone-chapter "unheard" fallback:
    // an empty synthesis queue says nothing about Avatar upload/playback readiness.
    requiresPlayback: j.strategy === "B" && Number(j.chars) > 3,
    serverSubmitted: Number.isFinite(Number(j.submitted)) ? Number(j.submitted) : null,
    heard: false,
    // 提交 ≠ 合成完：这一段自己的合成何时完成，由 tick 里的 synthIdle 信号
    // （队列空 && response 关闭）来确认。提前写 true 会把"还在合成的段"当成
    // "音频已到 8010"，短段可能在合成期间就被误判播完（自检发现的缺陷）。
    synthDone: false,
    silenceTicks: 0,
    submittedAt: Date.now(),
  });
  // 但提交车道只在服务端合成链路空闲时放行，所以这一次成功 = 此前所有在飞
  // 段的合成都已经完成（音频至少已到达浏览器 → 上传 8010 的路上）。
  for (let i = 0; i < st.ledger.length - 1; i++) st.ledger[i].synthDone = true;
  noteTimeline("submit_ok", submitIndex,
    { chapter, unitEnd, strategy: j.strategy, chars: Number(j.chars), submitted: Number(j.submitted) });
  st.waiting = 0;
  return "submitted";
}

/**
 * Submission cursor is independent of the displayed/uncommitted source cursor.
 * Only metadata is cached here; the existing submit gates bound all audio work.
 * @returns {Promise<object|null>} null = no ready candidate
 */
async function nextSubmitIndex() {
  if (st.plannerNext !== null) return st.plannerNext;
  const tail = st.ledger[st.ledger.length - 1];
  let chapter = tail ? tail.chapter : st.chapter;
  let chunks = tail ? tail.chunks : st.chunks;
  let index = tail ? tail.unitEnd + 1 : st.chunk;
  if (index >= chunks.length) {
    if (chapter + 1 >= st.chapters.length) return null;
    const cached = prepareChapter(chapter + 1);
    if (!cached.ready) return null; // slow GET must not block playback observation
    if (!cached.chunks?.length) { await stop('下一节原文加载失败，已保留断点'); return null; }
    chapter = cached.chapter; chunks = cached.chunks; index = 0;
  }
  if (index < 0 || index >= chunks.length) return null;
  return { docId: st.docId, generation: st.generation, epoch: st.resumeEpoch,
    chapter, chunks, index };
}

/**
 * 流水线主循环（READING_PIPELINE_V3）。
 * 服务端状态控制合成；每个 response 的输出边界控制进度。
 * 最多两段跨越合成、上传、Avatar 和播放；上一段实际结束才释放一格。
 */
async function tick() {
  st.timer = 0;
  if (st.state !== S.READING && st.state !== S.RESUMING) return;
  if (st.ticking) return;                 // 重入闸：一次只跑一个 tick
  st.ticking = true;
  const tickEpoch = st.resumeEpoch;
  try {
    const s = /** @type {any} */ ((await api("/api/reading/status")).json);
    const speaking = await avatarSpeaking();
    const boundaries = await (/** @type {any} */ (window)).AvatarSync.readingPlayback();
    if (s?.pending_expired) {
      await stop('等待下一段超时，已停在原断点');
      return;
    }
    if (tickEpoch !== st.resumeEpoch || (st.state !== S.READING && st.state !== S.RESUMING)) return;
    if (!s || s.ok === false || speaking === null) {
      st.failures += 1;
      if (st.failures >= MAX_TICK_FAILURES) {
        await stop(s && s.ok !== false
          ? "数字人没在听着，先停了"
          : "读不到语音服务，已暂停");
        return;
      }
    } else {
      st.failures = 0;
      const synthIdle = s.pending_queue_items === 0 && s.response_open === false;

      // Synthesis completion is distinct from render/output completion.
      if (synthIdle) {
        for (const entry of st.ledger) entry.synthDone = true;
      }
      // Release synthesis capacity when Avatar has consumed the input, while
      // retaining the entry until its actual output ends. These are different
      // queues: waiting for playout here can starve a short segment's renderer.
      for (const entry of st.ledger) {
        const boundary = boundaries.find(b => b.key === entry.readingKey);
        if (boundary?.inputConsumed && !entry.inputConsumed) {
          entry.inputConsumed = true;
          noteTimeline('input_consumed_observed', entry.index,
            { responseId: boundary.responseId, inputEnd: boundary.inputEnd });
        }
      }

      // ---- 2. 账本弹出（播放真值驱动，进度只在这里推进） ----
      const headBoundary = st.ledger.length
        ? boundaries.find(b => b.key === st.ledger[0].readingKey) : null;
      if (headBoundary?.started && !st.ledger[0].heard) {
        st.ledger[0].heard = true;
        noteTimeline('output_start_observed', st.ledger[0].index,
          { responseId: headBoundary.responseId, output: headBoundary.start,
            playoutDelayMs: headBoundary.playoutDelayMs, clockUncertaintyMs: headBoundary.clockUncertaintyMs });
      }
      if (st.ledger.length) {
        const head = st.ledger[0];
        // Missing output boundaries are a failure, never permission to skip.
        if (!headBoundary?.started && Date.now() - head.submittedAt > 30000) {
          await stop('这一段一直没出声，已停在原断点');
          return;
        }
        const finished = Boolean(headBoundary?.finished);
        if (finished) {
          noteTimeline("output_end_observed", head.index,
            { heard: head.heard, responseId: headBoundary.responseId,
              output: headBoundary.end, playoutDelayMs: headBoundary.playoutDelayMs });
          st.ledger.shift();
          const lastPlayed = Number.isInteger(head.unitEnd) ? head.unitEnd : head.index;
          if (st.ledger.length) {
            // 下一段已经预取在飞：指针跟上它（它此刻多半正在播）。
            st.chapter = st.ledger[0].chapter;
            st.chunks = st.ledger[0].chunks;
            st.chunk = st.ledger[0].checkpointIndex;
          } else if (lastPlayed + 1 < st.chunks.length) {
            st.chunk = lastPlayed + 1;
          } else if (st.chapter + 1 < st.chapters.length) {
            // 本章念完：翻下一章（与旧版 advance 的翻章路径等价）。
            st.chapter += 1;
            st.chunk = 0;
            renderChapters();
            if (!(await loadChapter())) {
              if (tickEpoch === st.resumeEpoch) await stop('下一节原文加载失败，已保留断点');
              return;
            }
            if (tickEpoch !== st.resumeEpoch || (st.state !== S.READING && st.state !== S.RESUMING)) return;
          } else {
            // 念到末尾了：进度留在最后一段（下次从这儿接着），不再算"正在陪读"。
            st.chunk = lastPlayed;
            await writeProgress("DONE");
            setState(S.FINISHED);
            say("到这里就念完了");
            return;
          }
          renderChapters();
          renderText();
          await writeProgress("READING");
          if (tickEpoch !== st.resumeEpoch || (st.state !== S.READING && st.state !== S.RESUMING)) return;
        }
      }

      // ---- 3. 提交车道（与播放重叠的预取） ----
      const inflight = st.ledger.filter(entry => !entry.inputConsumed).length;
      const canSubmit = !st.speakingRequest
        && inflight < READING_PREFETCH_LIMIT
        && st.ledger.length < READING_PLAYBACK_LEDGER_LIMIT
        && synthIdle;                      // 不和小夜自己的回答抢合成链路
      if (canSubmit) {
        const target = await nextSubmitIndex();
        if (tickEpoch !== st.resumeEpoch || (st.state !== S.READING && st.state !== S.RESUMING)) return;
        if (target !== null) {
          await submitChunk(target);
        }
      }

      // ---- 有界等待：完全没有任何在飞段、也一直提交不出去 → 不能无限挂着 ----
      if (!st.ledger.length && !st.speakingRequest && st.plannerNext === null
          && st.state !== S.FINISHED) {
        if (!(await patient("等下一段提交", MAX_WAIT_TICKS))) return;
      } else {
        st.waiting = 0;
      }
    }
  } catch (error) {
    st.failures += 1;
    noteTimeline('playback_observation_error', st.chunk, { error: String(error) });
    if (st.failures >= MAX_TICK_FAILURES) await stop('读不到逐段播放状态，已停在原断点');
  } finally {
    st.ticking = false;
  }
  if (st.state === S.READING || st.state === S.RESUMING) {
    st.timer = window.setTimeout(() => void tick(), TICK_MS);
  }
}

/** 等某个条件成立的过程本身不能无限挂着。 @returns {Promise<boolean>} */
async function patient(/** @type {string} */ what, /** @type {number} */ limit) {
  st.waiting += 1;
  if (st.waiting <= limit) return true;
  await stop(`${what}没等到（超过 ${(limit * TICK_MS) / 1000} 秒），先停了`);
  return false;
}

/** 掐掉声音并把状态落到 STOPPED / PAUSED。 @param {string} why @param {string} [status] */
async function halt(/** @type {string} */ why, status) {
  setState(S.INTERRUPTING);
  cancelResume();
  stopTimer();
  // 预取账本整体作废：未播段不许再发声（进度仍停在"正在出声的那一段"上，
  // 与旧版逐字一致——预取不挪动断点）。
  st.ledger = [];
  st.plannerNext = null;
  // 先切掉 LiveTalking 正在播的整段音频，再让服务端清掉尚未合成的朗读。
  const interrupted = Promise.resolve((/** @type {any} */ (window).AvatarSync)?.interrupt?.());
  // 若 /speak 与暂停同时在飞，等提交完成再清服务端队列。
  if (st.speakingRequest) await st.speakingRequest;
  st.speakingRequest = null;
  if (st.docId) await bumpServerGeneration(status || "PAUSED");
  await interrupted;
  setState(status === "STOPPED" ? S.STOPPED : S.PAUSED);
  say(why || `停在第 ${st.chapter + 1} 节第 ${st.chunk + 1} 段`);
  return false;
}

/** 用户点"暂停"：停在这儿，不自动继续，要用户自己点"继续朗读"。 */
async function pause(/** @type {string} */ why) {
  if (st.state !== S.READING && st.state !== S.RESUMING) return false;
  return halt(why || "已暂停，念到第 " + (st.chapter + 1) + " 节第 " + (st.chunk + 1) + " 段", "PAUSED");
}

/** 用户点"停止"：真停止。自动续读作废，回答播完也不许自己接着念。 */
async function stop(/** @type {string} */ why) {
  cancelResume();
  return halt(why || "不读了，停在第 " + (st.chapter + 1) + " 节第 " + (st.chunk + 1) + " 段", "STOPPED");
}

/** @param {string} status */
async function writeProgress(status) {
  await api("/api/reading/progress", {
    document_id: st.docId,
    chapter_index: st.chapter,
    chunk_index: st.chunk,
    status,
  });
}

async function play(/** @type {{resume?: boolean}|undefined} */ opts) {
  if (strategySwitching) return;
  // resume=true 只允许 resumeAfterAssistantReply() 内部授权的那一次穿过 RESUMING 守卫；
  // 普通调用（按钮、模式切换）在 RESUMING 态仍然一律拒绝，防止快速连点或重复回调
  // 造出第二条朗读 worker（真机 E2E 2026-10-03：resume 自锁根因即在此握手处）。
  if (!(opts && opts.resume) && (st.state === S.READING || st.state === S.RESUMING)) return;
  instructionRevision += 1;
  cancelResume();
  const playEpoch = st.resumeEpoch;
  if (!st.docId) { say("先选一本或导入一段文本"); return; }
  // 声音唯一的出口是数字人；它没连上时朗读是听不见的，别假装在读。
  if (await avatarSpeaking() === null) {
    say("数字人还没连上：先点一下页面任意处，或发一句话把对话连起来");
    return;
  }
  // 先取一次服务端 generation，之后每次 speak 都带上它：
  // 停止之后才到达的那一段会因为号小而直接被服务端拒掉。
  const status = /** @type {any} */ ((await api("/api/reading/status")).json);
  if (playEpoch !== st.resumeEpoch) return;
  const gen = Number(status?.generation);
  if (Number.isFinite(gen) && gen >= 0) st.generation = gen;
  setState(S.READING);
  // 流水线起点：空账本、无 planner 待定；提交车道从进度指针（= 下一个要听的
  // 段，断点语义与旧版一致）开始。
  st.ledger = [];
  st.plannerNext = null;
  st.failures = 0;
  st.waiting = 0;
  await writeProgress("READING");
  if (st.state !== S.READING) return;
  stopTimer();
  st.timer = window.setTimeout(() => void tick(), TICK_MS);
}

/** @param {number} index @param {boolean} keepPlaying */
async function jumpTo(index, keepPlaying) {
  if (!st.docId) return;
  if (st.state === S.READING || st.state === S.RESUMING) await pause("已暂停");
  st.chunk = Math.max(0, Math.min(index, st.chunks.length - 1));
  renderText();
  await writeProgress("PAUSED");
  setState(S.PAUSED);
  if (keepPlaying) await play();
  else say(`跳到第 ${st.chapter + 1} 节第 ${st.chunk + 1} 段`);
}

async function step(/** @type {number} */ delta) {
  if (!st.docId) return;
  const target = st.chunk + delta;
  if (target >= 0) return jumpTo(target, st.state === S.READING);
  if (st.chapter > 0) {
    const keepPlaying = st.state === S.READING;
    if (keepPlaying) await pause("已暂停");
    st.chapter -= 1;
    st.chunk = -1;
    renderChapters();
    await loadChapter();
    return jumpTo(st.chunks.length - 1, keepPlaying);
  }
  say("已经在最开头了");
  return undefined;
}

// ---------------------------------------------------------------- 装配

function bind() {
  document.querySelectorAll('[data-reading-strategy]').forEach((button) => {
    button.addEventListener('click', () => void selectStrategy(button.dataset.readingStrategy));
  });
  // V3_WORKSPACE：面板常驻右侧工作区（.reading-inline），不再有弹层开关。
  // 「gf-reading-open」入口已由顶栏模式切换取代；按钮不存在时这里是空操作。
  const open = $("gf-reading-open");
  open?.addEventListener("click", () => {
    const panel = $("reading-panel");
    if (!panel) return;
    panel.hidden = false;
    panel.classList.add("open");
    void refreshLibrary(st.docId);
  });
  // 关闭 = 切回互动聊天（走 main.js 的 /api/mode 链路：先停朗读、存断点）。
  $("reading-close")?.addEventListener("click", () => {
    window.__gfRequestMode?.("ROLEPLAY");
  });

  /** @type {HTMLSelectElement | null} */
  const docSel = /** @type any */ ($("reading-doc"));
  docSel?.addEventListener("change", () => void openDoc(docSel.value));
  /** @type {HTMLSelectElement | null} */
  const chSel = /** @type any */ ($("reading-chapter"));
  chSel?.addEventListener("change", async () => {
    await pause("已暂停");
    st.chapter = Number(chSel.value);
    st.chunk = 0;
    await loadChapter();
    await writeProgress("PAUSED");
    setState(S.PAUSED);
  });

  $("reading-play")?.addEventListener("click", () => { cancelResume(); void play(); });
  $("reading-pause")?.addEventListener("click", () => void pause(""));
  $("reading-stop")?.addEventListener("click", () => void stop(""));
  $("reading-next")?.addEventListener("click", () => void step(1));
  $("reading-prev")?.addEventListener("click", () => void step(-1));
  $("reading-jump")?.addEventListener("click", () => void openDoc(st.docId));

  $("reading-toggle-paste")?.addEventListener("click", () => {
    const box = $("reading-paste-box");
    if (box) box.hidden = !box.hidden;
  });

  $("reading-import-path")?.addEventListener("click", async () => {
    const input = /** @type {HTMLInputElement | null} */ ($("reading-path"));
    const path = (input?.value || "").trim();
    if (!path) { say("填一个本地的 TXT / MD 路径"); return; }
    say("正在解析…");
    const r = await api("/api/reading/import", { path });
    if (!r.ok) { say(`导入失败：${r.json?.detail?.reason || r.json?.reason || "?"}`); return; }
    input && (input.value = "");
    await refreshLibrary(String(/** @type {any} */ (r.json).document_id || ""));
    say("已导入");
  });

  $("reading-import-paste")?.addEventListener("click", async () => {
    const text = /** @type {HTMLTextAreaElement | null} */ ($("reading-paste-text"));
    const title = /** @type {HTMLInputElement | null} */ ($("reading-paste-title"));
    const body = (text?.value || "").trim();
    if (!body) { say("先粘贴一段文字"); return; }
    const r = await api("/api/reading/import", { title: title?.value || "", text: body });
    if (!r.ok) { say(`存入书库失败：${r.json?.detail?.reason || "?"}`); return; }
    if (text) text.value = "";
    await refreshLibrary(String(/** @type {any} */ (r.json).document_id || ""));
    say("已存进书库");
  });
}

/** 用户开口 / 发文字时由 main.js 调用：只暂停朗读，并把状态推到 INTERRUPTED。 */
export function pauseForUserTurn() {
  const wasPlaying = st.state === S.READING || st.state === S.RESUMING;
  const auto = Boolean((/** @type {HTMLInputElement|null} */ ($("reading-auto-resume")))?.checked);
  const pending = wasPlaying && auto;
  const task = wasPlaying ? pause("你在说话，先停下来") : Promise.resolve();
  cancelResume();
  if (wasPlaying) {
    st.resumePending = pending;
    if (pending) setState(S.INTERRUPTED);
  }
  return task;
}

/**
 * 回答播完才续读；有声回复要等 LiveTalking 真正从说话变为静音。
 * 三道闸门一道都不能少：待续标记、epoch 没被作废、状态不是 STOPPED/FINISHED。
 */
export async function resumeAfterAssistantReply(/** @type {{status:string,audible?:boolean,audioSink?:string}} */ detail) {
  if (!st.resumePending || detail.status !== "completed") return;
  st.resumePending = false;
  if (st.state === S.STOPPED || st.state === S.FINISHED) return;   // 停止就是停止
  if (st.state === S.READING || st.state === S.RESUMING) return;   // 已经在读了
  const epoch = st.resumeEpoch;
  // 回答路由到数字人 sink 时 response-finished 的 audible 恒为 false（真机 E2E 2026-10-03
  // 实测：audioSink:"avatar"、audible:false，而 8010 确实在出声）。所以只要回答走了
  // avatar，就同样按「真出声」处理：等 is_speaking 真正从说话变回静音再续读，
  // 否则会在回答还没播完时叠上朗读（双声）。轮询本身有 60×300ms 的有界上限。
  const audibleReply = Boolean(detail.audible) || String(detail.audioSink || "") === "avatar";
  if (audibleReply) {
    let heardReply = false;
    for (let i = 0; i < 60 && epoch === st.resumeEpoch; i += 1) {
      const speaking = await avatarSpeaking();
      if (speaking === true) heardReply = true;
      if (heardReply && speaking === false) break;
      await new Promise((resolve) => window.setTimeout(resolve, SPEAKING_POLL_MS));
      if (i === 59) return; // 回答没确认播完，不能猜测并叠加朗读。
    }
    if (!heardReply) return;
  }
  if (epoch === st.resumeEpoch && st.docId) {
    setState(S.RESUMING);
    await play({ resume: true });
  }
}

/**
 * 切模式（READING → 别的模式）时由 main.js 调用：
 * 掐掉朗读音频 → 存好断点 → 作废自动续读 → 落到 STOPPED。
 * 返回之后才允许切模式，不许故事还在后台念、角色又开始讲话。
 */
export async function stopForModeSwitch() {
  cancelResume();
  if (st.state !== S.READING && st.state !== S.RESUMING
      && st.state !== S.INTERRUPTED && st.state !== S.PAUSED) {
    return { stopped: false, state: st.state };
  }
  await stop("已经切到别的模式，朗读停在第 "
    + (st.chapter + 1) + " 节第 " + (st.chunk + 1) + " 段");
  return { stopped: true, state: st.state };
}

export function mountReading() {
  bind();
  void refreshStrategy();
  bindReadingTextScrollGuard();
  updateButtons();
  // V3_WORKSPACE：面板常驻工作区，页面加载时就把书库与断点装好，
  // 不等用户点开（原「打开面板才 refreshLibrary」的时机没有了）。
  void refreshLibrary(st.docId);
  // main.js 在切到 READING 模式时调这个钩子刷新书库。
  window.__gfReadingRefresh = () => { void refreshLibrary(st.docId); void refreshStrategy(); };
}

/** 给自检用：只读当前状态，不改任何东西。 */
export function readingDebugState() {
  return { state: st.state, chunk: st.chunk, chapter: st.chapter,
           strategy: selectedStrategy, strategySwitching,
           pipeline: 'READING_PIPELINE_V3',
           ledger: st.ledger.map((e) => ({ index: e.index, heard: e.heard,
             chapter: e.chapter, unitEnd: e.unitEnd, checkpointIndex: e.checkpointIndex,
             inputConsumed: Boolean(e.inputConsumed),
             synthDone: e.synthDone, silenceTicks: e.silenceTicks,
             requiresPlayback: e.requiresPlayback, submittedAt: e.submittedAt })),
           plannerNext: st.plannerNext, prefetchLimit: READING_PREFETCH_LIMIT,
           playbackLedgerLimit: READING_PLAYBACK_LEDGER_LIMIT,
           unconsumedSegments: st.ledger.filter(e => !e.inputConsumed).length,

           timeline: st.timeline.slice(-40),
           deployment: 'reading-pipeline-v3-response-boundaries-20261011',
           generation: st.generation, resumePending: st.resumePending,
           chunks: st.chunks.length };
}
