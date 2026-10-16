/* 离线自测：把 dock_acceptance.cjs 里真正会上场的那段扫描器代码【原样抽出来】eval，
 * 然后按 4 条阳性对照 + 1 条作用域断言检查。不连 CDP、不起浏览器、不动服务。
 * 目的：证明"0 命中"来自注释被正确抹掉，而不是扫描器本身失灵。
 * 运行：node runtime/tests/ui_detail_left_dock_01/static_scan_selftest.cjs
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'dock_acceptance.cjs');
const txt = fs.readFileSync(SRC, 'utf8');
const from = txt.indexOf('const NET_CALLSITE_RE');
const to = txt.indexOf('const LAYOUT_STACK_RE');
if (from < 0 || to < 0 || to < from) { console.log('HARNESS_FAIL cannot slice scanner source'); process.exit(2); }
const code = txt.slice(from, to);
// eslint-disable-next-line no-new-func
const factory = new Function('fs', 'path', code + '\nreturn {NET_CALLSITE_RE, maskComments, scanCallSites, gfDockRegion, codeHitsIn};');
const S = factory(fs, path);

const REPO = path.resolve(__dirname, '..', '..', '..');
const fails = [];
const chk = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? 'PASS ' : 'FAIL ') + name + ' got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
  if (!ok) fails.push(name);
};

/* 1) 阳性对照：真实脏文件必须数得出来 */
const dirty = fs.readFileSync(path.join(REPO, 'runtime/ui/app/avatar-sync.js'), 'utf8');
const dirtyCode = S.codeHitsIn(dirty, 'js');
console.log((dirtyCode > 0 ? 'PASS ' : 'FAIL ') + 'positive_control.dirty_file code_hits=' + dirtyCode);
if (!(dirtyCode > 0)) fails.push('positive_control.dirty_file');

/* 2) 真代码里的 fetch 必须被数到 */
chk('positive_control.plain_call', S.codeHitsIn("var a = fetch('/api/x');", 'js'), 1);

/* 3) 行注释吞掉后半行，但前半行的真代码仍要数到 */
chk('positive_control.line_comment_tail',
  S.codeHitsIn("var a = new WebSocket(u); // new EventSource 只是文档", 'js'), 1);

/* 4) 多行块注释里的命中必须归零；同一行去掉注释外壳必须为 1 */
const block = '<!-- 第一行\n     才能包装到 RTCPeerConnection / WebSocket 的构造\n     第三行 -->';
chk('negative.html_comment_masked', S.codeHitsIn(block, 'html'), 0);
chk('positive.same_text_without_comment',
  S.codeHitsIn('     才能包装到 RTCPeerConnection / WebSocket 的构造     ', 'html'), 1);

/* 5) JS 的 /* *\/ 块注释（上一轮 612 行假 FAIL 的同类） */
chk('negative.js_block_comment_masked',
  S.codeHitsIn('/*\n * 不 fetch、不发 WebSocket、不碰 RTCPeerConnection\n */', 'js'), 0);

/* 6) 字符串里的 // 不能被误判成行注释（否则会把真代码整段抹掉 ⇒ 假 PASS） */
chk('positive.url_string_does_not_swallow_line',
  S.codeHitsIn("var u = 'https://example.com/x'; var a = fetch(u);", 'js'), 1);

/* 7) index.html 的 Dock 作用域：必须找得到、必须配平、区内 0 命中、区外命中只在注释里 */
const html = fs.readFileSync(path.join(REPO, 'runtime/ui/app/index.html'), 'utf8');
const reg = S.gfDockRegion(html);
const lines = (s) => html.slice(0, s).split(/\r?\n/).length;
console.log('dock_region = ' + JSON.stringify(reg && { from_line: lines(reg.start), to_line: lines(reg.end), chars: reg.end - reg.start }));
if (!reg) fails.push('dock_region_not_found');
else {
  const inRegion = S.scanCallSites(path.join(REPO, 'runtime/ui/app/index.html'), 'html', reg);
  chk('region.code_hits', inRegion.code_hits, []);
  const whole = S.scanCallSites(path.join(REPO, 'runtime/ui/app/index.html'), 'html');
  console.log('whole_file raw=' + whole.raw_total + ' code=' + JSON.stringify(whole.code_hits) +
              ' comment=' + JSON.stringify(whole.comment_hits));
  chk('whole_file.code_hits', whole.code_hits, []);
  /* 每个 raw 命中都必须在 Dock 区段之外，且在注释里（comment_hits 已证） */
  if (whole.raw_total !== whole.comment_hits.length) fails.push('whole_file_raw_not_all_comment');
}

/* 8) 另外两份本轮写过的文件 */
for (const [rel, lang] of [['runtime/ui/app/gf_dock.css', 'css'], ['runtime/ui/app/ui_layout_modes.js', 'js']]) {
  const r = S.scanCallSites(path.join(REPO, rel), lang);
  chk(rel + '.code_hits', r.code_hits, []);
  console.log('  ' + rel + ' regions=' + r.comment_regions + ' masked_chars=' + r.masked_chars +
              ' raw=' + r.raw_total + ' lines=' + r.scoped_lines);
}

/* 9) 扫描器不吞换行：抹完注释后行数必须与原文一致（否则报出来的行号会漂移） */
for (const [rel, lang] of [['runtime/ui/app/ui_layout_modes.js', 'js'], ['runtime/ui/app/index.html', 'html'], ['runtime/ui/app/gf_dock.css', 'css']]) {
  const t = fs.readFileSync(path.join(REPO, rel), 'utf8');
  const m = S.maskComments(t, lang).masked;
  chk(rel + '.line_count_preserved', m.split('\n').length === t.split('\n').length, true);
}

console.log(fails.length ? 'SELFTEST_FAIL ' + JSON.stringify(fails) : 'SELFTEST_PASS all ' + 15 + ' checks');
process.exit(fails.length ? 1 : 0);
