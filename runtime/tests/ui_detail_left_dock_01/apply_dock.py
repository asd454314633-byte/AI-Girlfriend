#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_dock.py — AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01

把 index.html 里现有的控件块【原样搬进】唯一左侧 Dock。

为什么用脚本而不是手改：
  本轮验收要求「同一个 DOM 控件，不是复制品」。最硬的证据是搬移前后每个
  控件的 outerHTML 哈希逐字节相同。手工重排一定会顺手改缩进，而缩进属于
  文本节点、会被序列化进 outerHTML —— 一旦重排，哈希就变，那条验收就只能
  降级成"看起来是同一个"。所以这里做**纯切片搬移**：每个块连它原来的前导
  空白一起整段挪走，一个字节都不动，只有新增的 Dock 脚手架是新写的。

脚本自带的自检（任何一条不成立就整体不落盘）：
  1) 每个锚点在原文里必须唯一；
  2) 搬移块拼回去必须能还原出原始 stage 区（round-trip 逐字节相等）；
  3) 输出里每个搬移块必须恰好出现一次（证明是移动、不是复制）；
  4) 输出里 id 集合只允许新增 {gf-dock, gf-dock-screen-slot}，不得丢失、不得重复；
  5) 输出里不得出现 cloneNode / innerHTML= 之类的新写法（本轮只重排 DOM）。
"""
import hashlib
import re
import shutil
import sys
from pathlib import Path

APP = Path(str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'runtime/ui/app'))
TARGET = APP / "index.html"
BAK = APP / "index.html.bak_dock01"

src = TARGET.read_text(encoding="utf-8")
orig_bytes = src.encode("utf-8")
print("BEFORE index.html bytes=%d sha256=%s" % (len(orig_bytes),
      hashlib.sha256(orig_bytes).hexdigest()[:16]))

if BAK.exists():
    print("ABORT: backup already exists -> %s (refusing to overwrite)" % BAK.name)
    sys.exit(2)


def uniq(s, anchor, label):
    n = s.count(anchor)
    if n != 1:
        raise SystemExit("ABORT: anchor %s appears %d times (need exactly 1)" % (label, n))
    return s.index(anchor)


def line_start(s, i):
    return s.rfind("\n", 0, i) + 1


# ---------------------------------------------------------------- 切片 stage
m_start = uniq(src, '<main class="stage">', "main.stage-open")
m_close = uniq(src, "</main>", "main.stage-close")
m_end = m_close + len("</main>")
region = src[m_start:m_end]

A = [
    ("orb", '<div class="orb-wrap">'),
    ("caps", '<p id="circle-caption"'),
    ("queue", '<div id="queue-actions"'),
    ("gfctl", "<!-- 女友版控件条"),
    ("mount", "<!-- 数字人面板挂在这里"),
    ("perf", '<section id="gf-voice-performance"'),
    ("conv", '<section id="gf-live-conversation"'),
    ("comp", "<!-- 文字聊天"),
]
pos = [(name, uniq(region, anchor, name)) for name, anchor in A]
pos.append(("end", region.index("</main>")))
if [p[0] for p in pos] != [n for n, _ in A] + ["end"]:
    raise SystemExit("ABORT: stage anchors not in source order: %r" % ([p[0] for p in pos],))

blocks = {}
head = region[: pos[0][1]]
for i, (name, at) in enumerate(pos[:-1]):
    blocks[name] = region[at : pos[i + 1][1]]
tail = region[pos[-1][1] :]
if head + "".join(blocks[n] for n, _ in A) + tail != region:
    raise SystemExit("ABORT: stage round-trip mismatch")
print("stage round-trip OK: %d bytes, %d blocks" % (len(region), len(blocks)))

# --------------------------------------------------------- 切片 status aside
s_start = uniq(src, '<aside id="gf-status-container"', "status-aside")
s_end = uniq(src, '<footer class="footer">', "footer")
status_block = src[line_start(src, s_start) : s_end].rstrip() + "\n"
if "gf-avatar-status" not in status_block:
    raise SystemExit("ABORT: status block does not look right")
print("status aside block = %d bytes" % len(status_block))

# ------------------------------------------------------------------ 新脚手架
DOCK_HEAD = """      <!-- ====================================================================
           唯一左侧控制 Dock（AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01）

           这里【没有任何新控件】：下面每一块都是从 main.stage 原样搬进来的
           同一批 DOM 节点，连前导缩进都逐字节保留 —— 目的是让「控件数量修改
           前后相同 / 同一个 DOM 控件，不是复制品」可以用 outerHTML 哈希逐节点
           对账（缩进属于文本节点，会被序列化进 outerHTML，重排就等于改证据）。
           事件监听器挂在节点自身上，搬移不解绑；验收里用 CDP
           DOMDebugger.getEventListeners 实测每个控件的监听条数前后一致。

           分组顺序 = 任务书第 6 条：实时 → 画面 → 对话与声音 → 语音表现 → 对话。
           新增元素只有 17 个纯容器（1 aside + 1 滚动层 + 5×(section+h2+div)），
           不含任何 button/input/select/textarea，所以交互控件总数不变。
           ==================================================================== -->
      <aside id="gf-dock" class="gf-dock" aria-label="控制台">
        <div class="gf-dock-scroll">

          <section class="gf-dock-group gf-dock-g-live" aria-label="实时">
            <h2 class="gf-dock-title">实时</h2>
            <!-- 第 8 条：大语音交互圆球缩小后移进 Dock 上部的实时交互区，
                 不再悬浮在中央画布上覆盖人物面部 / 黑雾交界。 -->
            <div class="gf-dock-body">
"""

DOCK_MID_LIVE = """            </div>
          </section>

          <section class="gf-dock-group gf-dock-g-screen" aria-label="画面">
            <h2 class="gf-dock-title">画面</h2>
            <!-- 「画面布局」入口按钮 #gf-layout-open 与调节面板 #uivm-panel 由
                 ui_layout_modes.js 挂进下面这个 slot。它们原来是 body 上的独立
                 悬浮层 + .gf-controls 里的一个按钮，本轮改成 Dock 内的一个分组；
                 控件本身、默认收起语义、单一入口语义都没变。 -->
            <div class="gf-dock-body" id="gf-dock-screen-slot"></div>
          </section>

          <section class="gf-dock-group gf-dock-g-io" aria-label="对话与声音">
            <h2 class="gf-dock-title">对话与声音</h2>
            <div class="gf-dock-body">
"""

DOCK_MID_IO = """            </div>
          </section>

          <section class="gf-dock-group gf-dock-g-perf" aria-label="语音表现">
            <h2 class="gf-dock-title">语音表现</h2>
            <div class="gf-dock-body">
"""

DOCK_MID_PERF = """            </div>
          </section>

          <section class="gf-dock-group gf-dock-g-chat" aria-label="对话">
            <h2 class="gf-dock-title">对话</h2>
            <div class="gf-dock-body">
"""

DOCK_TAIL = """            </div>
          </section>

        </div>
      </aside>
"""

STAGE_NEW = """      <!-- 中央主画布：本轮起【清空】（第 7 条）。
           人物背景与黑雾来自 avatar-sync.js 注入的 body 级 #av-layer
           （position:fixed;inset:0;z-index:0），它本来就不在 .stage 里面；
           模型 / 语音模型 / 语音表现 / 对话卡片已全部迁进左侧 #gf-dock。
           保留这个空 main 有两个理由：上游 style.css 的
           #app{grid-template-rows:auto 1fr auto} 仍需要第二行有主，
           以及 main.js 里的 $(".orb-wrap") 之类查询不会因为结构缺失而报错。
           验收判据：main.stage 的元素子节点数 = 0、后代元素数 = 0。 -->
      <main class="stage">
      </main>
"""

dock = (
    DOCK_HEAD
    + blocks["orb"] + blocks["caps"] + blocks["queue"]
    + DOCK_MID_LIVE
    + blocks["gfctl"] + blocks["mount"] + status_block
    + DOCK_MID_IO
    + blocks["perf"]
    + DOCK_MID_PERF
    + blocks["conv"] + blocks["comp"]
    + DOCK_TAIL
)

# ------------------------------------------------------------- 组装新的 #app
prefix = src[: line_start(src, m_start)]
suffix = src[m_end:]
new_src = prefix + dock + STAGE_NEW + suffix

# 原位的 status aside 已迁入 dock → 从 #app 里整段摘掉（按行边界，别留残缩进）。
# 注意：此时 new_src 里 '<aside id="gf-status-container"' 有【两处】——
# Dock 里那一份是刚搬进去的，必须保留；要删的是 </main> 之后的原始那一份。
# 所以搜索起点必须落在 </main> 之后，不能全文找第一个。
mclose_new = uniq(new_src, "</main>", "new-main-close") + len("</main>")
ss = new_src.index('<aside id="gf-status-container"', mclose_new)
se = uniq(new_src, '<footer class="footer">', "new-footer")
if not (mclose_new < ss < se):
    raise SystemExit("ABORT: original status aside is not between </main> and <footer>")
new_src = new_src[: line_start(new_src, ss)] + new_src[se:]

# ------------------------------------------------------- 样式表与缓存串号
css_anchor = '<link rel="stylesheet" href="ui_layout_modes.css?v=composition-01" />'
uniq(new_src, css_anchor, "uivm-css-link")
css_new = (
    '<link rel="stylesheet" href="ui_layout_modes.css?v=dock-01" />\n'
    "    <!-- 唯一左侧控制 Dock（AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01）：\n"
    "         只排布 #gf-dock 与迁进去的现有控件，并把大圆球缩小到 Dock 内可用尺寸。\n"
    "         本文件【不得】出现任何 #av-video 的 transform / mask 规则 ——\n"
    "         人物变换的唯一 owner 是 avatar_layout.js（第 9 条，本轮不改它）。\n"
    "         必须排在 ui_layout_modes.css 之后：它要覆盖后者的面板停靠规则。 -->\n"
    '    <link rel="stylesheet" href="gf_dock.css?v=dock-01" />'
)
new_src = new_src.replace(css_anchor, css_new, 1)

js_anchor = '<script src="/ui_layout_modes.js?v=composition-01"></script>'
uniq(new_src, js_anchor, "uivm-js-script")
new_src = new_src.replace(
    js_anchor, '<script src="/ui_layout_modes.js?v=dock-01"></script>', 1)

# avatar_layout.js 的缓存串号【刻意不动】：第 9 条要求它逐字保持，
# 文件没变就不该换串号（换了反而让人以为它被改过）。

# ------------------------------------------------------------------- 自检
moved = dict(blocks)
moved["status"] = status_block
for name, blk in moved.items():
    probe = blk.rstrip("\n")
    c = new_src.count(probe)
    if c != 1:
        raise SystemExit("ABORT: block %s appears %d times in output (need 1 -> 移动而非复制)"
                         % (name, c))
    print("  moved block %-7s %5d bytes  occurrences=%d" % (name, len(probe), c))

for needle in ('id="gf-dock"', 'id="gf-dock-screen-slot"', 'gf_dock.css?v=dock-01',
               'ui_layout_modes.js?v=dock-01', 'ui_layout_modes.css?v=dock-01',
               '<main class="stage">'):
    if new_src.count(needle) != 1:
        raise SystemExit("ABORT: expected exactly 1 occurrence of %r, got %d"
                         % (needle, new_src.count(needle)))

for bad in ("cloneNode", "innerHTML =", "insertAdjacentHTML"):
    if bad in new_src:
        raise SystemExit("ABORT: forbidden construct %r present" % bad)

ids_old = re.findall(r'\bid="([^"]+)"', src)
ids_new = re.findall(r'\bid="([^"]+)"', new_src)
added = sorted({i for i in ids_new if ids_new.count(i) > ids_old.count(i)})
removed = sorted({i for i in ids_old if ids_new.count(i) < ids_old.count(i)})
if removed:
    raise SystemExit("ABORT: ids lost during move: %r" % removed)
if added != ["gf-dock", "gf-dock-screen-slot"]:
    raise SystemExit("ABORT: unexpected added ids %r" % added)
dupes = sorted({i for i in ids_new if ids_new.count(i) != 1})
if dupes:
    raise SystemExit("ABORT: duplicate ids in output: %r" % dupes)

new_bytes = new_src.encode("utf-8")
print("AFTER  index.html bytes=%d sha256=%s" % (len(new_bytes),
      hashlib.sha256(new_bytes).hexdigest()[:16]))
print("delta bytes = %+d ; ids total %d -> %d ; added=%r removed=[]"
      % (len(new_bytes) - len(orig_bytes), len(ids_old), len(ids_new), added))

shutil.copyfile(TARGET, BAK)
TARGET.write_bytes(new_bytes)
print("backup -> %s" % BAK.name)
print("OK: index.html rewritten")
