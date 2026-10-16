// 回归自测：页面切到后台之后，启动之后才写进 DOM 的英文文案还翻不翻。
//
// 现场是浏览器里抓到的真故障（ISSUE_UI_CN_HIDDEN_TAB）：document.hidden=true 时
// requestAnimationFrame 一次都不回调，于是 MutationObserver 攒下的那批节点永远停在
// 英文上，而覆盖率清单也是空的 —— 因为那句英文根本没进过词典。这里用"永不触发
// 的 rAF"把同一个条件复现出来。
import test from 'node:test';
import assert from 'node:assert/strict';

class TextNode {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = value;
  }
}
class ElementNode {
  constructor(children = []) {
    this.nodeType = 1;
    this.children = children;
    this.attrs = {};
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  setAttribute(name, value) { this.attrs[name] = value; }
  querySelectorAll() { return []; }
}

let observerCallback = null;
let flushRaf = false;      // 模拟可见页面：rAF 到底跑不跑
let blockTimer = false;    // 让定时器也不跑（只为证明这条断言真的会红）

globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 };
globalThis.NodeFilter = { SHOW_TEXT: 4 };
globalThis.MutationObserver = class {
  constructor(fn) { observerCallback = fn; }
  observe() {}
};
globalThis.requestAnimationFrame = (fn) => {
  if (flushRaf) queueMicrotask(fn);
  return 1;                                 // 后台标签页：注册了但永远不来
};
const realTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...rest) => (blockTimer ? 0 : realTimeout(fn, ms, ...rest));

const body = new ElementNode([]);
globalThis.document = {
  documentElement: { lang: '' },
  body,
  createTreeWalker: (root) => {
    const rows = root instanceof ElementNode ? root.children : [root];
    let i = 0;
    return { nextNode: () => (i < rows.length ? rows[i++] : null) };
  },
};

const { startZhCn } = await import('../ui/zh_cn.js');
startZhCn();

const wait = (ms) => new Promise((res) => realTimeout(res, ms));

test('页面在后台时，之后写进 DOM 的英文照样被翻（rAF 不跑也有兜底）', async () => {
  flushRaf = false;                          // 和现场一致：hidden 标签页 rAF 不回调
  const node = new TextNode('Queue timed out');
  body.children.push(node);
  observerCallback([{ type: 'childList', addedNodes: [node], target: body }]);
  await wait(400);
  assert.equal(node.nodeValue, '排队超时了', '后台页面里这句话停在英文 = 故障复现');
});

test('反向对照：把兜底定时器也掐掉，这条断言确实会变红（不是白测）', async () => {
  flushRaf = false;
  blockTimer = true;
  const node = new TextNode('Queue timed out');
  body.children.push(node);
  observerCallback([{ type: 'childList', addedNodes: [node], target: body }]);
  await wait(250);
  blockTimer = false;
  assert.equal(node.nodeValue, 'Queue timed out', '假 DOM 太宽容：什么都不做它也会变中文');
});

test('可见页面走快速通道，同一句话也翻得到', async () => {
  flushRaf = true;
  const node = new TextNode('Realtime transport disconnected');
  body.children.push(node);
  observerCallback([{ type: 'childList', addedNodes: [node], target: body }]);
  await wait(400);
  assert.equal(node.nodeValue, '实时语音连接已断开了');
});
