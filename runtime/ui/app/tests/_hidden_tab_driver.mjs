// 共用驱动：在"rAF 永不回调"的假 DOM 里，看一句话翻没翻。
// 用法：node _hidden_tab_driver.mjs old|new   （old = 去掉兜底定时器的副本）
class TextNode {
  constructor(value) { this.nodeType = 3; this.nodeValue = value; }
}
class ElementNode {
  constructor() { this.nodeType = 1; this.children = []; this.attrs = {}; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  setAttribute(name, value) { this.attrs[name] = value; }
  querySelectorAll() { return []; }
}

let observerCallback = null;
globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 };
globalThis.NodeFilter = { SHOW_TEXT: 4 };
globalThis.MutationObserver = class {
  constructor(fn) { observerCallback = fn; }
  observe() {}
};
globalThis.requestAnimationFrame = () => 1;      // 后台标签页：注册了但永远不来

const body = new ElementNode();
globalThis.document = {
  documentElement: { lang: "" },
  body,
  createTreeWalker(root) {
    const rows = root.nodeType === 1 ? root.children : [root];
    let index = 0;
    return { nextNode: () => (index < rows.length ? rows[index++] : null) };
  },
};

const which = process.argv[2] === "old" ? "old" : "new";
const mod = which === "old" ? "./_zh_cn_no_fallback.mjs" : "../ui/zh_cn.js";
const { startZhCn } = await import(mod);
startZhCn();

const node = new TextNode("Queue timed out");
body.children.push(node);
observerCallback([{ type: "childList", addedNodes: [node], target: body }]);
await new Promise((res) => setTimeout(res, 700));
console.log(which.toUpperCase() + "_RESULT=" + JSON.stringify(node.nodeValue));
