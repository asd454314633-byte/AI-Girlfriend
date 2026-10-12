// @ts-check
/**
 * zh-CN 本地化层（AI_GIRLFRIEND_PRODUCT_UI_READING_V2 · 第 1 项）
 *
 * 为什么是一层、而不是一家一家改字符串：这个页面的可见文案散落在 index.html、
 * main.js、ui/chat.js、ui/account.js 四处，其中很多是运行期拼接出来的（倒计时、
 * 队列位次、设备名、报错文本）。逐点改写会把这份冻结基线改成和上游对不上的样子，
 * 也一定漏掉运行期分支。所以这里只做两件事：
 *
 *   1. 启动时翻译一次静态 DOM；
 *   2. 挂一个 rAF 节流的 MutationObserver，把之后任何代码写进 DOM 的文案再过
 *      一遍同一张词典。
 *
 * 命中方式只有两种，都可枚举：`ZH` 做整串精确匹配，`ZH_PATTERNS` 处理带变量的
 * 模板。没命中的英文原样保留，并记进 `untranslated()` —— 验收看的是这张清单，
 * 而不是我自己觉得翻全了。
 *
 * 纯展示层：不碰音频链路、不改状态机、不存业务数据。
 */

/** 整串精确匹配（比较前 trim）。 */
const ZH = {
  // ── 顶栏 / 品牌区 ────────────────────────────────────────────────────────
  "Hugging Face Realtime": "实时语音女友",
  "About this space": "关于",
  "An open, real-time voice chat built on Hugging Face's speech-to-speech stack.":
    "基于 speech-to-speech 实时语音栈搭建的开放语音聊天。",
  "Powered by": "技术支持",
  "Inference Endpoints": "推理端点",
  "Built by": "作者",
  Tools: "工具",
  "Conversation history": "对话记录",
  Settings: "设置",
  Close: "关闭",
  Mute: "静音",
  Unmute: "取消静音",
  End: "结束",
  "Start voice conversation": "开始语音对话",
  "Tap to start": "点一下开始说话",
  Connecting: "正在连接…",
  "Connecting…": "正在连接…",
  "Finding you a spot…": "正在为你找空闲通道…",
  "You're up! 🎉": "轮到你了 🎉",
  "Tap to retry": "点一下重试",
  "Join now": "现在加入",
  "Leave queue": "退出排队",
  "Sign in": "登录",
  camera: "摄像头",

  // ── 状态字幕 / 提示（main.js 运行期写入） ────────────────────────────────
  "Asking for mic…": "正在请求麦克风权限…",
  "Sign in to start": "登录后开始",
  "Sign in again to continue.": "请重新登录以继续。",
  "Your spot expired. Tap to rejoin.": "你的名额已过期，点一下重新排队。",
  "That took a while. Tap to rejoin.": "等了有点久，点一下重新排队。",
  "Sorry, we overhugged! 🤗 Every slot is busy, so we saved you a spot. Hang tight, you're moving up.":
    "抱歉，现在聊天的人有点多 🤗 每个通道都在用，已经帮你留了个位置，马上就到。",

  // ── 设置弹窗 ─────────────────────────────────────────────────────────────
  "Speech-to-speech server URL": "语音服务地址",
  // The hint below is split by two <code> elements, so the DOM never holds the
  // whole sentence — each text node has to be a key of its own.
  "URL of your speech-to-speech server, e.g.": "你的 speech-to-speech 服务地址，例如",
  "(the app adds": "（应用会自动补上",
  ").": "）。",
  "URL of your speech-to-speech server, e.g. http://localhost:8080 (the app adds /v1/realtime).":
    "你的 speech-to-speech 服务地址，例如 http://localhost:8080（应用会自动补上 /v1/realtime）。",
  "Speech-to-speech server URL pinned by this deployment.": "语音服务地址由本部署固定，不可修改。",
  "Set the speech-to-speech server URL to start.": "请先填写语音服务地址，再开始。",
  Transport: "传输方式",
  WebSocket: "WebSocket",
  WebRTC: "WebRTC",
  "How audio travels to the server. Applies on the next conversation.":
    "音频走哪条链路到服务端。下次通话生效。",
  "WebRTC needs a server URL pinned by the deployment (SPEECH_TO_SPEECH_URL).":
    "WebRTC 需要由部署固定服务地址（SPEECH_TO_SPEECH_URL）。",
  Voice: "音色",
  Microphone: "麦克风",
  Speakers: "扬声器",
  Speaker: "扬声器",
  "System default": "系统默认",
  "Applies on the next conversation (or Restart).": "下次通话（或点“重新开始对话”）生效。",
  "Allow microphone access (tap Start once) to see device names. Mic changes apply on Restart.":
    "允许麦克风权限（点一次开始）后才能看到设备名。切换麦克风需点“重新开始对话”。",
  "Where assistant audio plays. Can change live while connected.":
    "对方的声音从哪个设备播出，通话中也能实时切换。",
  "Where assistant audio plays. Chrome/Edge support switching outputs.":
    "对方的声音从哪个设备播出。Chrome/Edge 支持切换输出设备。",
  "Speaker selection needs a browser with AudioContext.setSinkId (Chrome/Edge).":
    "选择扬声器需要支持 AudioContext.setSinkId 的浏览器（Chrome/Edge）。",
  "Noise gate": "噪音门",
  "Noise gate threshold": "噪音门阈值",
  Off: "关闭",
  "Mutes the mic below the handle so room noise isn't sent. Slide fully left to turn it off; the bar shows your live input while in a call.":
    "低于滑块阈值的输入不送出去，避免把房间噪音传给模型。滑到最左即关闭；通话中进度条显示实时音量。",
  Instructions: "人设指令",
  "You are a friendly voice assistant...": "例如：你是一位温柔、会撒娇的女朋友……",
  "Restart conversation with these settings": "用以上设置重新开始对话",
  "Reconnects now with the settings above.": "立即用上面的设置重连。",
  "Starts a conversation with the settings above.": "用上面的设置开始一次对话。",
  "Connect first, then come back here to apply live changes.":
    "先连上，再回到这里就能即时应用改动。",
  Save: "保存",

  // ── 工具弹窗 ─────────────────────────────────────────────────────────────
  "Let the assistant act during the conversation. Changes apply live.":
    "让助手在对话中调用这些能力。改动即时生效。",
  "Web search": "联网搜索",
  "Look things up on Google, via Serper.": "通过 Serper 在 Google 上查资料。",
  "Search API key": "搜索 API Key",
  "••••••••  · provided by the server": "••••••••  · 由服务端提供",
  "Ready. The search key is held server-side and never sent to your browser.":
    "可用。搜索 Key 保存在服务端，不会下发到浏览器。",
  "Paste a Serper key to enable web search": "粘贴一个 Serper Key 以启用联网搜索",
  "Using your key — stored in this browser only.": "使用你自己的 Key — 只存在本浏览器。",
  "No server key configured. Add your own Serper key to enable web search.":
    "服务端未配置 Key。填一个你自己的 Serper Key 即可启用联网搜索。",
  Camera: "摄像头",
  "Let the assistant see through your webcam.": "让助手通过你的摄像头看见画面。",
  "Camera on. The assistant can take a snapshot when it needs to see.":
    "摄像头已开。助手需要看画面时会自己拍一张。",
  "Camera blocked. Allow it from the camera icon in your browser's address bar — it switches on automatically.":
    "摄像头被拦截。请点地址栏里的摄像头图标允许，然后会自动重新打开。",
  "A live preview shows bottom-left while the camera is on.":
    "摄像头开启时，左下角会显示实时预览。",

  // ── 对话记录面板 ─────────────────────────────────────────────────────────
  Conversation: "对话记录",
  "No messages yet": "还没有消息",
  "Tap the orb and start talking": "点一下中间的光球，直接开口说",
  You: "我",
  Assistant: "小夜",
  "Listening…": "正在听…",
  "Sending voice…": "正在发送语音…",
  Interrupted: "（已打断）",
  "Tool call": "工具调用",
  Input: "输入",
  Output: "输出",
  "(no output)": "（无输出）",
  Snapshot: "画面截图",
  "Webcam snapshot sent to the model": "发送给模型的摄像头截图",
  "Audio sent to the model": "发送给模型的语音",
  "Audio sent to the model · beginning unavailable": "发送给模型的语音 · 开头部分已不在缓存里",
  "Replay your audio (the beginning was no longer buffered)": "回听你的语音（开头部分已不在缓存里）",
  "Replay the audio sent to the model": "回听发送给模型的语音",
  "Replay your audio": "回听你的语音",

  // ── 关于弹窗 ─────────────────────────────────────────────────────────────
  About: "关于",
  "Speech-to-speech is Hugging Face's open framework for real-time voice agents. Rather than one end-to-end model, it chains four open models from the Hub (speech detection, transcription, a vision-language model, and synthesis), so any stage can be swapped or run locally. This demo wires that pipeline to hosted inference.":
    "speech-to-speech 是 Hugging Face 开放的实时语音智能体框架。它不用一个端到端模型，而是把 Hub 上的四个开源模型串成一条链（语音检测、转写、视觉语言模型、语音合成），每一环都能单独替换或在本地跑。这个演示把整条链接到了托管推理服务上。",
  "· via": "· 由",
  "You speak": "你说话",
  "The orb replies": "光球回答",
  "The pipeline": "整条链路",
  "detects speech": "检测语音",
  "transcribes it": "转成文字",
  "analyses and composes the reply": "理解并组织回答",
  "speaks it back": "合成语音回答",
  "View the project on GitHub": "在 GitHub 上查看项目",

  // ── 限额弹窗（本部署 limiter 关闭，仍一并翻译） ──────────────────────────
  "That's a wrap for now": "今天就聊到这里吧",
  "Thanks for chatting!": "谢谢你陪我聊天！",
  "Your free minutes refresh tomorrow.": "免费时长明天恢复。",
  "Or come back tomorrow. Your minutes reset daily.": "或者明天再来，时长每天重置。",
  "Upgrade to PRO": "升级到 PRO",
  "Sign in again": "重新登录",
  "Sign in with Hugging Face": "用 Hugging Face 登录",
  "Hugged to the limit 🤗": "今天用满啦 🤗",
  "A spot usually opens up within a few minutes.": "通常几分钟内就有空位。",

  // ── 实时客户端抛出的错（s2s-realtime-client.js，运行期写进状态行） ────────
  // 这些字符串同时是协议层的一部分（`codedError` 的 message 会被 main.js 的
  // catch 拿去显示），所以不在源文件里改，只在这里换显示。
  "Realtime WebSocket handshake failed": "实时语音通道握手失败",
  "Realtime connection failed": "实时语音连接失败",
  "No session endpoint or direct URL configured": "既没有会话入口，也没有可用的语音服务地址",
  "Realtime transport disconnected": "实时语音连接已断开了",
  "Already connected": "已经连着啦",
  "No microphone track available": "没有可用的麦克风音轨",
  "The microphone audio processor did not report its sample rate. Close every tab for this Space, reopen it, and try again.":
    "麦克风的音频处理链没有报出采样率。请把这个应用的其它标签页全部关掉，重新打开后再试一次。",
  "Input audio transcription failed": "你这段语音没能转成文字",
  "Server error": "服务端出错了",
  "Your spot expired": "你的名额已过期",
  "Daily conversation limit reached": "今天的聊天时长用完了",
  "Sign in again to continue": "请重新登录以继续。",
  "The queue is full — try again shortly.": "队列已经满了，稍等一下再试。",
  "Queue timed out": "排队超时了",

  // ── 工具调用回显（main.js 的工具输出，同时也是发给模型的那段话） ──────────
  "Snapshot captured from the webcam and attached as an image.": "已拍下摄像头画面，并作为图片一并送出。",
  "The camera is not available right now.": "现在用不了摄像头。",
  "No query provided.": "没说要查什么。",
  "No results found.": "没有找到结果。",
  "Enter a speech-to-speech server URL in Settings.": "请先在设置里填语音服务地址。",

  // ── 服务端 :7860 的回错（server.py 的 detail，会被显示或拼进工具输出） ────
  "Empty query.": "没填写要查的内容。",
  "Search is not configured.": "还没有配置联网搜索。",
  "Search provider unreachable.": "连不上搜索服务。",
  "Speech service unreachable.": "连不上语音服务。",
  "Run npm ci in the Space repository.": "界面资源还没装好（需要在仓库里执行 npm ci）。",
  "Not found.": "没找到这个内容。",
};

/** 带变量模板：按顺序取第一个命中的。 */
const ZH_PATTERNS = /** @type {Array<[RegExp, string]>} */ ([
  [/^You're #(\d+) in line$/, "你在队列中排第 $1 位"],
  [/^Join now \((\d+)s\)$/, "现在加入（$1 秒）"],
  [/^(Microphone|Speaker) (\d+)$/, "$1 $2"],
  [/^(Microphone|Speaker) \(saved, not found\)$/, "$1（已保存，当前不在列表里）"],
  [/^Camera unavailable(?::\s*(.*))?$/, "摄像头不可用：$1"],
  // avatar-sync.js's debug badge (its own file is frozen — §0), e.g. "avatar: idle".
  [/^avatar: (.+)$/, "数字人：$1"],
  // 工具输出与后端回错是拼出来的（`Tool failed: ${err.message}`、
  // `search error (${detail})`），所以整串永远不等于词典里的任何一条；
  // 这里按外壳匹配，括号里的那段交给下面的递归再翻一次。
  [/^Unknown tool:\s*(.*)$/, "没有这个工具：$1"],
  [/^Tool failed:\s*(.*)$/, "工具没跑通：$1"],
  [/^search error\s*\((.*)\)$/, "搜索出错（$1）"],
  [/^Google search result from\s+(\d{4}-\d{2}-\d{2}):?$/, "来自 Google 的搜索结果（$1）："],
  [/^Answer:\s*([\s\S]*)$/, "回答：$1"],
  [/^Microphone access denied:\s*(.*)$/, "麦克风权限被拒绝了：$1"],
  [/^Microphone access denied$/, "麦克风权限被拒绝了"],
  [/^Session handshake failed\s*\((\d+)\)\.?$/, "会话握手失败（$1）。"],
  [/^Queue poll failed\s*\((\d+)\)\.?$/, "查询排队状态失败（$1）。"],
]);

const AVATAR_STATES = {
  idle: "空闲",
  "connecting…": "连接中…",
  connecting: "连接中",
  new: "等待连接",
  connected: "已连接",
  disconnected: "已断开",
  failed: "连接失败",
  closed: "已关闭",
  speaking: "正在说话",
  interrupt: "被打断",
};

const FALLBACK_LABELS = { Microphone: "麦克风", Speaker: "扬声器" };
const ATTRS = ["title", "aria-label", "placeholder", "alt"];

/**
 * 品牌 / 协议名不翻译，也不该出现在漏网清单里。
 *
 * 这里只留“与厂商无关的通用词”（公司、协议、上游库名）。厂商自己的显示名和 id 由
 * 注册表下发，运行时经 registerBrandTerms() 并进来（D-38）：新增一家模型不需要
 * 再改这个文件。源仓库 URL 仍在本文件里，因为它是 index.html 的固定文档链接，
 * 而不是某个 Provider 的分支条件。
 */
const BASE_BRANDS = [
  "Hugging ?Face", "Cerebras", "Serper", "GitHub", "Chrome", "Edge",
  "AudioContext\\.setSinkId", "AudioContext", "setSinkId", "WebRTC", "WebSocket",
  "speech-to-speech", "silero-vad", "nvidia/parakeet-tdt-1\\.1b",
  "Qwen/Qwen3-TTS-\\S+", "google/gemma-\\S+", "huggingface/speech-to-speech",
  "SPEECH_TO_SPEECH_URL",
];

/**
 * 跨副本共享的状态 —— 这一段是"为什么同一个文件会被加载两遍"的答案。
 *
 * main.js 按 `./ui/zh_cn.js?v=缓存令牌` 引本文件，ui/girlfriend.js 按相对路径
 * `./zh_cn.js` 引它。浏览器以 URL 认模块，两条 URL 就是两个实例：字典、品牌表、
 * 漏网清单各自一套。结果 registerBrandTerms() 叫进"没人跑的那一份"，真正在扫 DOM
 * 的那一份永远看不到注册表下发的显示名，于是把 "Higgs Audio v3" 一路当成漏翻报出来
 * （7860 现场实测；离线双实例自测同样复现）。缓存令牌必须留在 import 站点上，改不动，
 * 所以把会变的三份状态挂到全局：几个副本共用一套，第二份再 startZhCn 也不会装第二个
 * 观察器把界面翻两遍。
 */
const SHARED = (globalThis.__zhCnShared ||= {
  /** @type {string[]} */ brands: [],
  brandsPattern: null,
  /** @type {Set<string>} */ missed: new Set(),
  started: false,
});

/** @param {Array<string|undefined|null>} words */
export function registerBrandTerms(words) {
  const clean = (words || []).filter((word) => typeof word === "string" && word.trim());
  if (!clean.length) return;
  const before = SHARED.brands.length;
  // 数组本身是共享对象里的同一个引用：只往里推，绝不重新赋值，否则又变成每份副本一套。
  for (const word of clean) {
    const term = word.trim();
    if (!SHARED.brands.includes(term)) SHARED.brands.push(term);
  }
  if (SHARED.brands.length !== before) SHARED.brandsPattern = null;
}

/** 转义后再拼：注册表里的显示名可能带点号、括号。 */
const escapePattern = (word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function BRANDS() {
  if (!SHARED.brandsPattern) {
    // 长的排在前面：正则的分支是从左到右取第一个能匹配的，短名字（Provider 的 id
    // "higgs"）会先把长显示名（"Higgs Audio v3"）的头一段吃掉，剩下半截英文看起来
    // 就像"没翻的文案"。按长度降序拼，整条品牌名才是一次性剥掉的。
    const terms = [...BASE_BRANDS, ...SHARED.brands.map(escapePattern)]
      .sort((a, b) => b.length - a.length);
    SHARED.brandsPattern = new RegExp("\\b(" + terms.join("|") + ")\\b", "gi");
  }
  return SHARED.brandsPattern;
}
const CJK = /[\u3400-\u9FFF\u3000-\u303F\uFF00-\uFFEF]/;
/** 联网搜索返回的条目（`- 标题: 摘要 (https://…)`）：第三方正文，不是本产品的措辞。 */
const DATA_LINE = /^[-•]\s.*https?:\/\//;

const handled = new WeakSet();
const missed = SHARED.missed;

/** 设备兜底名 / 数字人状态里混着英文标签，模板回填时顺手换掉。
 *  @param {string} piece */
function cn(piece) {
  return AVATAR_STATES[piece] || FALLBACK_LABELS[/** @type {keyof typeof FALLBACK_LABELS} */ (piece)] || piece;
}

/**
 * @param {string} text
 * @param {number} depth 递归层数：`Tool failed: search error (Search is not
 *   configured.)` 这类"英文套英文"的运行期拼接，外壳换完之后里层还得再换一次。
 *   只走两层，且只有最外层记漏网，避免同一个串被数两遍。
 * @returns {string | null}
 */
function translate(text, depth = 0) {
  // Source markup wraps long sentences, so the DOM text node carries a newline
  // and indentation mid-sentence while the dictionary key is one line.
  const key = text.replace(/\s+/g, " ").trim();
  if (!key) return null;
  if (ZH[key]) return ZH[key];
  for (const [re, tpl] of ZH_PATTERNS) {
    const hit = key.match(re);
    if (!hit) continue;
    let out = tpl;
    hit.slice(1).forEach((g, i) => {
      const raw = (g || "").trim();
      const inner = depth < 2 ? translate(raw, depth + 1) : null;
      out = out.split(`$${i + 1}`).join((inner || cn(raw)).trim());
    });
    return out;
  }
  if (depth > 0) return null;
  // 只报告"像成句英文、且确实还没翻"的漏网文案：品牌名、模型路径、URL、以及
  // 已经含中文的混排句都不算。搜索结果那一条条 `- 标题: 摘要 (链接)` 是第三方
  // 正文不是界面措辞，翻不翻由用户查的东西决定，所以也不进清单。
  if (DATA_LINE.test(key)) return null;
  const ascii = key.replace(BRANDS(), " ");
  if (!CJK.test(key) && /[A-Za-z]{3,}\s+[A-Za-z]{3,}/.test(ascii)) missed.add(key);
  return null;
}

/** 界面上一整块话（工具输出是多行拼出来的）：先整块试，再逐行试。
 *  导出只为让离线自测和浏览器闸门能直接查这张词典，页面本身不叫它。
 *  @param {string} text @returns {string | null} */
export function translateVisible(text) {
  const whole = translate(text);
  if (whole) return whole;
  const rows = String(text).split("\n");
  if (rows.length < 2) return null;
  const out = rows.map((row) => translate(row, 1) ?? row);
  return out.some((row, i) => row !== rows[i]) ? out.join("\n") : null;
}

/** @param {Element} el */
function localizeAttributes(el) {
  for (const name of ATTRS) {
    const raw = el.getAttribute(name);
    if (!raw) continue;
    const hit = translateVisible(raw);
    if (hit && hit !== raw.trim()) el.setAttribute(name, hit);
  }
}

/** @param {Node} root */
function localizeTree(root) {
  const walk = root.nodeType === Node.TEXT_NODE
    ? [/** @type {Text} */ (root)]
    : [];
  if (root.nodeType === Node.ELEMENT_NODE) localizeAttributes(/** @type {Element} */ (root));
  if (root.nodeType === Node.ELEMENT_NODE || root.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const text = /** @type {Text} */ (node);
      if (!handled.has(text)) walk.push(text);
      node = walker.nextNode();
    }
    const els = root.querySelectorAll("*");
    for (const el of els) localizeAttributes(el);
  }
  for (const text of walk) {
    handled.add(text);
    const hit = translateVisible(text.nodeValue || "");
    if (hit) text.nodeValue = hit;
  }
}

/** @returns {string[]} 未被词典命中的英文文案（验收用）。 */
export function untranslated() {
  return [...missed].sort();
}

/** 翻译当前 DOM，并持续翻译后续写入的文案。只装一次：见上面 SHARED 的说明。 */
export function startZhCn() {
  if (SHARED.started) return;
  SHARED.started = true;
  document.documentElement.lang = "zh-CN";
  /** @type {Set<Node>} */
  const dirty = new Set();
  let fallback = 0;

  const flush = () => {
    clearTimeout(fallback);
    fallback = 0;
    if (!dirty.size) return;
    const batch = [...dirty];
    dirty.clear();
    for (const node of batch) localizeTree(node);
  };
  const schedule = (/** @type {Node} */ node) => {
    dirty.add(node);
    if (fallback) return;
    // rAF 只当"看得见的帧"用的快速通道。页面一切到后台，rAF 就彻底不回调了
    // （实测 document.hidden=true 时 3 秒内一次都没跑），于是启动之后写进 DOM 的
    // 那句英文永远停在英文上，而覆盖率清单也看不见它——因为它压根没进过词典。
    // 所以必须再挂一个定时器：切走的标签页也会翻，切回来就是中文。
    requestAnimationFrame(() => flush());
    fallback = setTimeout(() => flush(), 120);
  };

  localizeTree(document.body);
  new MutationObserver((records) => {
    for (const rec of records) {
      if (rec.type === "characterData") {
        // A script rewrote this text node; let it through the dictionary again.
        handled.delete(rec.target);
        schedule(rec.target);
        continue;
      }
      if (rec.type === "attributes") {
        // main.js and ui/account.js assign title/aria-label/placeholder after
        // startup, so attribute mutations need their own pass — a text-node
        // walk would never see them.
        localizeAttributes(/** @type {Element} */ (rec.target));
        continue;
      }
      for (let i = 0; i < rec.addedNodes.length; i += 1) schedule(rec.addedNodes[i]);
    }
  }).observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ATTRS,
  });

  // 覆盖率报告：UI_CN_PASS 的依据是这条清单为空，而不是"看起来都是中文"。
  // 顺手把它挂到 window 上，验收就能在点开任何面板之后随时再读一次，
  // 而不是只有开机 2 秒那一次 console。纯读，不改任何状态。
  if (typeof window !== "undefined") {
    /** @type {{ untranslated: () => string[] }} */ (window).__zhCnCoverage = { untranslated };
  }
  setTimeout(() => {
    const rest = untranslated();
    if (rest.length) console.info(`[zh-CN] ${rest.length} 条文案未命中词典:`, rest);
    else console.info("[zh-CN] dictionary covered every visible string");
  }, 2000);
}
