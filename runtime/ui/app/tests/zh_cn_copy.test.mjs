// 离线自测：zh-CN 词典是否真的把界面上会出现的英文措辞都接住了。
//
// 为什么在 node 里测而不是只看页面：§31 不许"代码看起来对 = PASS"，而运行期写进
// DOM 的话（工具输出、抛出来的错）只有在词典这一层查得到；浏览器那一趟再补一次
// 真实 DOM 的漏网清单，两边都对得上才算过。
import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.document = { documentElement: {}, body: {} };
const { translateVisible, untranslated, registerBrandTerms } = await import('../ui/zh_cn.js');

/** 逐条给出「界面会原样显示出来的英文」，断言它翻得出中文。 */
const CHROME = [
  // s2s-realtime-client.js —— 连接与设备
  ['Realtime WebSocket handshake failed', '实时语音通道握手失败'],
  ['Realtime connection failed', '实时语音连接失败'],
  ['No session endpoint or direct URL configured', '既没有会话入口，也没有可用的语音服务地址'],
  ['Realtime transport disconnected', '实时语音连接已断开了'],
  ['Already connected', '已经连着啦'],
  ['No microphone track available', '没有可用的麦克风音轨'],
  ['Input audio transcription failed', '你这段语音没能转成文字'],
  ['Server error', '服务端出错了'],
  ['Your spot expired', '你的名额已过期'],
  ['Daily conversation limit reached', '今天的聊天时长用完了'],
  ['Sign in again to continue', '请重新登录以继续。'],
  ['The queue is full — try again shortly.', '队列已经满了，稍等一下再试。'],
  ['Queue timed out', '排队超时了'],
  // 采样率那一条源文件里是两行字符串拼起来的，词典按拼好后的整句收
  ['The microphone audio processor did not report its sample rate. Close every tab for this Space, reopen it, and try again.',
    '麦克风的音频处理链没有报出采样率。请把这个应用的其它标签页全部关掉，重新打开后再试一次。'],
  // main.js —— 工具执行与权限
  ['The camera is not available right now.', '现在用不了摄像头。'],
  ['Snapshot captured from the webcam and attached as an image.', '已拍下摄像头画面，并作为图片一并送出。'],
  ['No query provided.', '没说要查什么。'],
  ['No results found.', '没有找到结果。'],
  ['Enter a speech-to-speech server URL in Settings.', '请先在设置里填语音服务地址。'],
  // server.py —— :7860 的 detail
  ['Empty query.', '没填写要查的内容。'],
  ['Search is not configured.', '还没有配置联网搜索。'],
  ['Search provider unreachable.', '连不上搜索服务。'],
  ['Speech service unreachable.', '连不上语音服务。'],
  ['Not found.', '没找到这个内容。'],
];

test('词典把每一条界面英文都换成了中文', () => {
  for (const [english, chinese] of CHROME) {
    assert.equal(translateVisible(english), chinese, `没接住：${english}`);
  }
});

test('英文套英文的运行期拼接也要接住（工具输出与搜索回错）', () => {
  assert.equal(translateVisible('Unknown tool: camera_snapshut'), '没有这个工具：camera_snapshut');
  assert.equal(translateVisible('Tool failed: Search is not configured.'), '工具没跑通：还没有配置联网搜索。');
  assert.equal(translateVisible('search error (Search provider unreachable.)'), '搜索出错（连不上搜索服务。）');
  assert.equal(translateVisible('Microphone access denied: Permission denied'), '麦克风权限被拒绝了：Permission denied');
  assert.equal(translateVisible('Microphone access denied'), '麦克风权限被拒绝了');
  assert.equal(translateVisible('Session handshake failed (502).'), '会话握手失败（502）。');
});

test('正向对照：词典没接住的成句英文必须留在漏网清单里（防止这张表自证干净）', () => {
  const probe = 'This sentence is deliberately missing from the dictionary';
  assert.equal(translateVisible(probe), null, '探针不该被翻译');
  assert.ok(untranslated().includes(probe), '漏网清单没记下探针 = 清单是空的在骗人');
  // 反过来：品牌名与已经含中文的混排句不该进清单，否则清单会被噪声填满
  assert.equal(translateVisible('Powered by'), '技术支持');
  assert.equal(translateVisible('Powered by Hugging Face'), null);
  assert.ok(!untranslated().some((row) => row.includes('Hugging Face')), '品牌名不该算漏网');
});

test('注册表下发的显示名进品牌表，新增一家不用改这份词典', () => {
  const before = untranslated().length;
  registerBrandTerms(['Gemma4 12B', 'Kokoro Small']);
  assert.equal(translateVisible('Gemma4 12B online'), null);
  assert.equal(untranslated().length, before, '品牌名被记成漏网文案了');
});

test('多行工具输出逐行翻译，第三方正文原样保留', () => {
  const block = [
    'Google search result from 2026-09-24:',
    'Answer: 明天下雨。',
    '- Typhoon track: officials say more (https://example.org/a)',
  ].join('\n');
  const out = translateVisible(block);
  assert.ok(out, '多行块没能翻译');
  assert.match(out, /^来自 Google 的搜索结果（2026-09-24）：/);
  assert.match(out, /回答：明天下雨。/);
  // 搜索结果条目是第三方正文，不许被改写成中文，也不许进漏网清单
  assert.match(out, /- Typhoon track: officials say more \(https:\/\/example\.org\/a\)$/);
  assert.ok(!untranslated().some((row) => row.startsWith('- Typhoon track')),
    '第三方正文条目不该占用漏网清单');
});
