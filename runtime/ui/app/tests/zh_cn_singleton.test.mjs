// 离线自测：词典被两条不同 URL 各加载一次时，状态还是不是同一份。
//
// 为什么单独测这张：index.html/main.js 用 `?v=缓存令牌` 引 zh_cn.js，而
// ui/girlfriend.js 用相对路径 `./zh_cn.js` 引它 —— 浏览器按 URL 认模块，于是
// 同一个文件被实例化两遍。注册表下发的显示名写进"没人跑的那一份"，真正在扫 DOM
// 的那一份永远看不到，漏网清单就把品牌名一路当成漏翻报出来（现场 7860 实测）。
// 这里用两个不同的 import 说明符造出同样的双实例，把这条锁住。
import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.document = {
  documentElement: {},
  body: { nodeType: 1, getAttribute: () => null, querySelectorAll: () => [] },
  createTreeWalker: () => ({ nextNode: () => null }),
};
globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 };
globalThis.NodeFilter = { SHOW_TEXT: 4 };
let observers = 0;
globalThis.MutationObserver = class {
  constructor() { observers += 1; }
  observe() {}
};
// 故意永不触发的 rAF：本地化只能靠兜底定时器，这也顺带压住后台标签页那条路。
globalThis.requestAnimationFrame = () => 0;

test('注册表下发的名字必须进"真正在跑的那一份"，哪怕模块被加载了两次', async () => {
  const mainCopy = await import('../ui/zh_cn.js?v=capability-ui-04');
  const componentCopy = await import('../ui/zh_cn.js');
  assert.notEqual(mainCopy, componentCopy, '探针失效：只实例化了一份，测不到重复加载');

  // ui/girlfriend.js 里 registerBrandTerms() 叫到的是 componentCopy。
  componentCopy.registerBrandTerms(['higgs', 'Higgs Audio v3', 'Qwen3 TTS']);
  assert.equal(mainCopy.translateVisible('Higgs Audio v3'), null, '品牌名被翻成别的话了');
  assert.deepEqual(mainCopy.untranslated(), [],
    '品牌名进了漏网清单 = 两份副本各握一套品牌表，清单永远洗不干净');
});

test('第二份副本再 startZhCn，不得装第二个观察器把 DOM 翻两遍', async () => {
  const first = await import('../ui/zh_cn.js?start=a');
  const second = await import('../ui/zh_cn.js?start=b');
  const before = observers;
  first.startZhCn();
  second.startZhCn();
  assert.equal(observers - before, 1, `装了 ${observers - before} 个观察器：两份副本都在改 DOM`);
});

test('短 id 与长显示名同框时，整条品牌名一次剥掉，不许剩半截英文', async () => {
  const copy = await import('../ui/zh_cn.js?v=longest-brand');
  // 注册表里 id 总排在 label_zh / display_name 前面，正则分支又是从左到右取第一个
  // 能匹配的：不排序就会先吃掉 "Nova"，剩下 " Speech Studio Pro" 像一句没翻的话。
  copy.registerBrandTerms(['nova', 'Nova Speech Studio Pro']);
  const before = copy.untranslated().length;
  assert.equal(copy.translateVisible('Nova Speech Studio Pro'), null, '品牌名被翻成别的话了');
  assert.equal(copy.untranslated().length, before,
    `漏网清单被记了一条：${JSON.stringify(copy.untranslated().slice(before))}`);
});
