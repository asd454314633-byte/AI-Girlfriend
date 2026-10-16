// 定位「注册表下发名仍进漏网清单」的机制：到底哪一步没把它认出来。
// 不复用 zh_cn.js 的正则（那是被测对象），这里按同一份 BASE_BRANDS 手工重建，
// 把每一步中间值打出来。
globalThis.document = { documentElement: {}, body: {} };
const { translateVisible, untranslated, registerBrandTerms } = await import('../ui/zh_cn.js');

const rows = [
  { id: 'qwen3', label_zh: '千问 TTS', display_name: 'Qwen3 TTS' },
  { id: 'higgs', label_zh: 'Higgs Audio v3', display_name: 'Higgs Audio v3' },
  { id: 'fish', label_zh: 'Fish S2 Pro', display_name: 'Fish S2 Pro' },
];
registerBrandTerms(rows.flatMap((row) => [row.id, row.label_zh, row.display_name]));

const BASE_BRANDS = [
  'Hugging ?Face', 'Cerebras', 'Serper', 'GitHub', 'Chrome', 'Edge',
  'AudioContext\\.setSinkId', 'AudioContext', 'setSinkId', 'WebRTC', 'WebSocket',
  'speech-to-speech', 'silero-vad', 'nvidia/parakeet-tdt-1\\.1b',
  'Qwen/Qwen3-TTS-\\S+', 'google/gemma-\\S+', 'huggingface/speech-to-speech',
  'SPEECH_TO_SPEECH_URL',
];
const escapePattern = (word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const dynamic = rows.flatMap((row) => [row.id, row.label_zh, row.display_name]).map((w) => w.trim());
const re = new RegExp('\\b(' + [...BASE_BRANDS, ...dynamic.map(escapePattern)].join('|') + ')\\b', 'gi');

for (const key of ['Higgs Audio v3', 'Fish S2 Pro', 'Qwen3 TTS']) {
  const ascii = key.replace(re, ' ');
  console.log(JSON.stringify(key), '-> strip', JSON.stringify(ascii),
    '| 两段英文?', /[A-Za-z]{3,}\s+[A-Za-z]{3,}/.test(ascii),
    '| 全替换命中', [...key.matchAll(re)].map((m) => m[0]).join('/'));
  translateVisible(key);
}
console.log('ledger:', JSON.stringify(untranslated()));
