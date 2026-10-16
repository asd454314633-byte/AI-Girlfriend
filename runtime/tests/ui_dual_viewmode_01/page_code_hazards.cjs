/* ============================================================================
 * page_code_hazards.cjs — 静态红线：页面侧代码写进 Node 模板字符串的安全性
 *
 * 根因（本任务真实踩过，见 J2 假 FAIL）：探针代码以 `...` 模板字符串形式存放在
 * .cjs 里，经 Runtime.evaluate 送到页面执行。模板求值会【吃掉反斜杠】——
 *   源码 /matrix\\(([^)]+)\\)/  →  页面收到 /matrix(([^)]+))/
 * 括号因此从分组变成字面量，捕获组错位，parseFloat 拿到 "(" → NaN → JSON 序列化成
 * null → 闸门以"看起来是对的"的姿势假 FAIL。`${` 与裸反引号则是另外两种提前
 * 改写/截断这段代码的方式。
 *
 * 本脚本把每个 .cjs 里的模板字面量逐个抽出来（字符级扫描，认 \`\ 与 ${} 嵌套），
 * 只要它的内容像页面侧代码（含 document./getComputedStyle/querySelector），就检查
 * 是否含反斜杠 / ${ / 三目嵌套里的反引号，命中就报行号。
 *
 *   node page_code_hazards.cjs      → 退出码 0=干净，1=有命中需人工判
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');

const DIR = __dirname;

/** 字符级抽出模板字面量：返回 [{start, end, body, startLine}] */
function templates(src) {
  const out = [];
  for (let i = 0; i < src.length; i++) {
    if (src[i] !== '`') continue;
    // 排除 `x` 前面是标识符/右括号/右括号的情况（模板标签调用在这里不存在，保守跳过注释行）
    let j = i + 1, depth = 0;
    let body = '';
    let closed = false;
    while (j < src.length) {
      const c = src[j];
      if (c === '\\') { body += c + (src[j + 1] || ''); j += 2; continue; }
      if (depth === 0 && c === '`') { closed = true; break; }
      if (depth === 0 && c === '$' && src[j + 1] === '{') { depth++; body += '${'; j += 2; continue; }
      if (depth > 0) {
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { body += '}'; j++; continue; } }
      }
      body += c; j++;
    }
    if (closed) out.push({ start: i, body });
    i = j;
  }
  return out;
}

const lineOf = (src, idx) => src.slice(0, idx).split('\n').length;

let hits = 0, unmarkedN = 0;
for (const f of fs.readdirSync(DIR).sort()) {
  if (!f.endsWith('.cjs') || f === 'page_code_hazards.cjs') continue;
  const src = fs.readFileSync(path.join(DIR, f), 'utf8');
  for (const t of templates(src)) {
    const looksPageSide = /\bdocument\.|getComputedStyle|querySelector|window\./.test(t.body);
    if (!looksPageSide) continue;
    const base = lineOf(src, t.start);
    const findings = [];
    const srcLines = src.split('\n');
    const marked = (n) => /HAZARD-OK/.test(srcLines[n - 1] || '') || /HAZARD-OK/.test(srcLines[n - 2] || '');
    t.body.split('\n').forEach((ln, k) => {
      if (ln.indexOf('\\') >= 0) findings.push(['BACKSLASH', base + k, marked(base + k), ln.trim().slice(0, 100)]);
      if (ln.indexOf('${') >= 0) findings.push(['TEMPLATE-SUBST', base + k, true, ln.trim().slice(0, 100)]);
    });
    /* 判据只压在 BACKSLASH 上：${} 插值是这套探针的正常用法（Node 侧填参数）。
     * 反斜杠则必须当场写明"为什么这个转义是故意双写的"（HAZARD-OK），否则视为
     * 未经复核的新增转义 —— 不做自动豁免，也不做"报了但永远不响"的火警。 */
    const unmarked = findings.filter((x) => x[0] === 'BACKSLASH' && !x[2]);
    if (findings.length) {
      hits += findings.length;
      unmarkedN += unmarked.length;
      console.log(`${f}:${base} 页面侧模板含 ${findings.length} 处转义/插值（未标注 ${unmarked.length}）`);
      findings.slice(0, 14).forEach(([k, ln, ok, txt]) =>
        console.log(`    ${k}${ok ? ' [HAZARD-OK]' : ' [UNMARKED]'} @${ln}  ${txt}`));
    }
  }
}
console.log(`\nPAGE_SIDE_ESCAPES=${hits} UNMARKED=${unmarkedN}`
  + (unmarkedN ? ' —— 未标注的反斜杠逐条读一遍：页面收到的是吃掉反斜杠后的形状' : ' —— 每处反斜杠都已就地标注理由'));
process.exitCode = unmarkedN ? 1 : 0;
