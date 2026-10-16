#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fa_append_master_dock_613_and_state_mirror.py — §6.13 纯追加 + STATE.json 机器可读镜像

TASK_ID = AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01

两个动作，一个脚本：
  A) 往 runtime/llm/AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md 末尾【纯追加】§6.13
     （只放摘要、归属、禁止项、指针与逐字冻结键 ⇒ MASTER_HANDOFF_ROLE = SUMMARY_AND_POINTER，
      详细证据在 runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/FINAL_REPORT.md）
  B) 把同一批核心事实镜像进 …_MASTER_STATE.json 的 "tasks" 对象（纯插入一个条目）

铁律（实现即约束）：
  · 默认【无写】= 干跑并打印所有断言；只有 --write 才落盘。
  · 幂等：两处目标任一已含本任务条目 ⇒ 只校验不写入，打印 *_IDEMPOTENT_NO_WRITE 且 delta = 0。
  · 不改其他 task 的状态：解析后除新键以外的 dict 必须与旧 dict 完全相等。
  · 不重排、不格式化 JSON；沿用文件既有 CRLF + 4/6 空格缩进 + snake_case。
  · MD 用 LF；不编辑、不删除任何历史小节（§6.12 及其以前原样保留）。
  · 不碰生产代码 / UI / 测试资产；不跑任何测试；不发 HTTP。
  · 生产三文件哈希与本轮 POST 台账对拍 ⇒ 回写文档不许改动生产件。

用法：python runtime/tests/fa_append_master_dock_613_and_state_mirror.py [--write]
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MASTER_MD = ROOT / 'runtime' / 'llm' / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md'
STATE = ROOT / 'runtime' / 'llm' / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json'
EVDIR = ROOT / 'runtime' / 'evidence' / 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01'
TASK_ID = 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01'
SECTION_MARK = '## §6.13 '

# 镜像内容 = 本轮裁定的核心事实（不复制报告正文）。
# verdict 是该 JSON 层 tasks.* 的既有读取惯例键，与 status 同值同义（唯一别名键，在此披露）。
FIELDS = [
    ('task_id', TASK_ID),
    ('status', 'CLOSED_PASS'),
    ('verdict', 'CLOSED_PASS'),
    ('left_control_dock', 'PASS'),
    ('central_canvas_cleared', 'PASS'),
    ('existing_controls_reused', 'PASS'),
    ('avatar_transform_unchanged', 'PASS'),
    ('live_portrait_core_model', 'KEEP'),
    ('ui_presentation_only_change', 'PASS'),
    ('requirement_11_all_items', 'PASS'),
    ('runtime_gates_total', 24),
    ('runtime_gates_all_pass', True),
    ('transform_equivalence_gates_total', 11),
    ('moved_not_copied', 'TRUE'),
    ('non_ui_modified_files', []),
    ('layout_generated_backend_requests', 0),
    ('webrtc_reconnect', 0),
    ('avatar_video_node_identity_preserved', 'PASS'),
    ('stream_change_not_layout_caused', 'PASS'),
    ('avatar_layout_transform_owner', 'avatar_layout.js'),
    ('second_transform_owner', 'NONE'),
    ('avatar_scale_stretch_algorithm_modified', 'NONE'),
    ('composition_drift_attributed_to_user', 'TRUE'),
    ('ui_visual_user_acceptance', 'PENDING'),
    ('final_report', 'runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/FINAL_REPORT.md'),
]

# 镜像键 → 主交接 MD §6.13 里的正式键（语义对拍表）
MAP_TO_MD = {
    'status': 'STATUS',
    'left_control_dock': 'LEFT_CONTROL_DOCK',
    'central_canvas_cleared': 'CENTRAL_CANVAS_CLEARED',
    'existing_controls_reused': 'EXISTING_CONTROLS_REUSED',
    'avatar_transform_unchanged': 'AVATAR_TRANSFORM_UNCHANGED',
    'live_portrait_core_model': 'LIVE_PORTRAIT_CORE_MODEL',
    'ui_presentation_only_change': 'UI_PRESENTATION_ONLY_CHANGE',
    'requirement_11_all_items': 'REQUIREMENT_11_ALL_ITEMS',
    'runtime_gates_total': 'RUNTIME_GATES_TOTAL',
    'runtime_gates_all_pass': 'RUNTIME_GATES_ALL_PASS',
    'transform_equivalence_gates_total': 'TRANSFORM_EQUIVALENCE_GATES_TOTAL',
    'moved_not_copied': 'MOVED_NOT_COPIED',
    'non_ui_modified_files': 'NON_UI_MODIFIED_FILES',
    'layout_generated_backend_requests': 'LAYOUT_GENERATED_BACKEND_REQUESTS',
    'webrtc_reconnect': 'WEBRTC_RECONNECT',
    'avatar_video_node_identity_preserved': 'AVATAR_VIDEO_NODE_IDENTITY_PRESERVED',
    'stream_change_not_layout_caused': 'STREAM_CHANGE_NOT_LAYOUT_CAUSED',
    'avatar_layout_transform_owner': 'AVATAR_LAYOUT_TRANSFORM_OWNER',
    'second_transform_owner': 'SECOND_TRANSFORM_OWNER',
    'avatar_scale_stretch_algorithm_modified': 'AVATAR_SCALE_STRETCH_ALGORITHM_MODIFIED',
    'composition_drift_attributed_to_user': 'COMPOSITION_DRIFT_ATTRIBUTED_TO_USER',
    'ui_visual_user_acceptance': 'UI_VISUAL_USER_ACCEPTANCE',
}

MD_SECTION = """
## §6.13 追加（2026-09-26 20:0x）— UI 左侧控制 Dock 合并 / Presentation Layer Only

`TASK_ID = AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01`
`STATUS = CLOSED_PASS`

自动化侧 24 项运行时闸门 + 11 项 transform 等价闸门 + 需求第 11 条 9 项全绿；
主观视觉验收由用户给出，本轮不代答（见下方 `UI_VISUAL_USER_ACCEPTANCE`）。

**事实源（详细证据，本节只是指针）**：
`runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/FINAL_REPORT.md`
（24 项运行时闸门 + 需求第 11 条逐项 + 静态 P1–P8 移动证明 + 哈希台账 + 诚实账 D1–D7）

**为什么要有本节**：Dock 之后生产 7860 前端只有**一个左侧控制 Dock**，中央区已经清空。
新 Agent 若不知道这点，很容易「顺手把功能控件搬回中间」「再造第二个控制面板/第二份布局状态」
或「以为圆球还压在画面上」——那三类正是本任务的禁止项。

**冻结结论（逐字，可直接 grep；判据与实测值在 FINAL_REPORT §0–§2）**：

```text
LEFT_CONTROL_DOCK = PASS
CENTRAL_CANVAS_CLEARED = PASS
EXISTING_CONTROLS_REUSED = PASS
AVATAR_TRANSFORM_UNCHANGED = PASS
LIVE_PORTRAIT_CORE_MODEL = KEEP
UI_PRESENTATION_ONLY_CHANGE = PASS
REQUIREMENT_11_ALL_ITEMS = PASS
RUNTIME_GATES_TOTAL = 24
RUNTIME_GATES_ALL_PASS = true
TRANSFORM_EQUIVALENCE_GATES_TOTAL = 11
MOVED_NOT_COPIED = TRUE
NON_UI_MODIFIED_FILES = NONE
LAYOUT_GENERATED_BACKEND_REQUESTS = 0
WEBRTC_RECONNECT = 0
AVATAR_VIDEO_NODE_IDENTITY_PRESERVED = PASS
STREAM_CHANGE_NOT_LAYOUT_CAUSED = PASS
AVATAR_LAYOUT_TRANSFORM_OWNER = avatar_layout.js
SECOND_TRANSFORM_OWNER = NONE
AVATAR_SCALE_STRETCH_ALGORITHM_MODIFIED = NONE
COMPOSITION_DRIFT_ATTRIBUTED_TO_USER = TRUE
UI_VISUAL_USER_ACCEPTANCE = PENDING
```

**措辞纪律（一条，别再写错）**：本轮**不沿用** `MEDIASTREAM_IDENTITY_PRESERVED` 那种
"流永不变化"的写法。实测是两层事实：人物 video **节点恒等 + `src` 属性始终为空**成立；
而 MediaStream 对象引用在验收会话内变过 1 次，逐窗归因为 `avatar-sync.js`（未改动件）
自身的 `POST /offer` 重协商，布局导致 0 条请求。⇒ 只许引用
`AVATAR_VIDEO_NODE_IDENTITY_PRESERVED` 与 `STREAM_CHANGE_NOT_LAYOUT_CAUSED` 这两条。

**架构归属（在 §6.12 之上追加，不替换）**：

```text
LEFT_DOCK_CONTAINER = aside#gf-dock（唯一；1 aside + 1 .gf-dock-scroll + 5 section + 5 h2 + 5 .gf-dock-body = 17 元素）
LEFT_DOCK_GROUPS = 实时 / 画面 / 对话与声音 / 语音表现 / 对话
LEFT_DOCK_WIDTH_LAW = clamp(340px, 24vw, 430px)（判据是【实测 innerWidth 代入公式】，不许写死 430）
LEFT_DOCK_MAX_HEIGHT_LAW = calc(100vh - 安全边距) + overflow-y:auto
CENTRAL_MAIN_STAGE_CHILDREN = 0
ORB_OWNER = 仍在 Dock「实时」组内，rect 320×320 → 128×128（与人物带间隙 457.98px）
```

**顺带纠正一条跨文档事实漂移（不改写 §6.12 历史）**：§6.12 在册的
`DETAIL_PANE_DIVIDER_RANGE_PCT = 28-70` / `…_STATE = FROZEN` 属 **V1 pane 机制**。
V2（composition 轮）起分隔线位置由 owner 的 `bandLeft` 驱动
（`ui_layout_modes.js:207` `--uivm-divider-x = clamp(g.bandLeft, 0, frame)`；
`PANE = {min:28,max:70,…}` 只在 622 行进 `limits.pane` 供展示，不再驱动布局）。
⇒ 后续不许拿 28–70 当分隔线当前可拖范围去判"漂移"，也不许为此重开 §6.12 或 V2 任一轮。

**实现原则（本任务的永久规则，继承 §6.12）**：

- 只重新布局**现有 DOM**：控件不重建、不复制、不建第二份状态
- 搬移 = 移动：9 个搬移块在现文里各出现**恰好 1 次**（P1）；id 多重集零丢失（P4）
- Dock 新写部分**交互标签 0**（P5：1,469 B 只剩容器）⇒ 新脚手架不承载逻辑
- 布局参数仍只落 localStorage；本轮探针净变化 0（15 → 15 键，diffs []）
- `avatar_layout.js` 仍是人物 transform 唯一 owner；缩放/拉伸算法本轮零改动
- 几何判据必须断言**渲染后**数值（`getBoundingClientRect`），且同一帧内对照

**禁止重新打开（除非出现新的直接回归证据）**：8010 / 8090 / 8765、WebRTC / WebSocket 生命周期、
AudioRouter、TTS / STT / LLM、Session lifecycle、Avatar switch、Persona / Memory、
以及 §6.11 / §6.12 已 CLOSED 的内容。本轮从未撞这条边界：改动文件 =
`index.html` / `ui_layout_modes.js` / `gf_dock.css` 三份（presentation layer），
282 文件全量 sha256 对账 `out_of_scope = 0`。

**本轮性质**：文档事实同步 = **纯追加**（§6.12.1 之后接一节，不动任何历史小节）。
生产三文件字节与哈希与 `POST_CHANGE_MANIFEST.json` 台账一致
（`index.html` 51,431 B / `ui_layout_modes.js` 29,672 B / `gf_dock.css` 13,019 B）。

**登记两条非阻断债（本轮不补）**：
`ARCH_DEBT_DOCK_JS_NO_LINE_LEVEL_BAK = OPEN_NON_BLOCKING` ——
`ui_layout_modes.js` 本轮改动没留 `.bak` ⇒ 该文件的行级"零删除"diff 无法从磁盘重放
（替代通道：PRE sha256 在册 + 43 项控件身份台账 + 20 监听逐选择器全等 + P8 挂载点归属）。
`ARCH_DEBT_CACHE_BUSTER_NOT_ISOMORPHIC_TO_ROUND = OPEN_NON_BLOCKING` ——
`ui_layout_modes.css` 磁盘内容本轮未改但 `?v=` 已是 `dock-01`（在本轮之前一次改动里升的），
串号与"本轮是否改过该文件"不同构，别据此判"本轮动过它"。

**环境事实一条（做任何计时/视口测量前先读）**：
`Emulation.clearDeviceMetricsOverride` 在本机**不可靠可逆** ⇒ 发覆盖的视口扫描必须排在最后，
跨动作几何对照只在同一帧（dpr 指纹）内做。详见
`runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/VIEWPORT_CHANNEL_FINDING.md` 末节。
"""


def md_pairs_from(text, start_marker):
    section = text[text.index(start_marker):]
    pairs = {}
    for line in section.splitlines():
        s = line.strip().strip('`')
        if '=' in s and not s.startswith('#'):
            k, _, v = s.partition('=')
            pairs[k.strip()] = v.strip()
    return pairs


def block_text():
    inner = ',\r\n'.join('      %s: %s' % (json.dumps(k), json.dumps(v, ensure_ascii=False))
                         for k, v in FIELDS)
    return '    %s: {\r\n%s\r\n    }' % (json.dumps(TASK_ID), inner)


def tasks_object_span(text):
    i = text.index('"tasks"')
    j = text.index('{', i)
    depth, in_str, esc, k = 0, False, False, j
    while k < len(text):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return j, k
        k += 1
    raise SystemExit('TASKS_OBJECT_NOT_FOUND')


def main():
    do_write = '--write' in sys.argv
    md_raw = MASTER_MD.read_bytes()
    md_text = md_raw.decode('utf-8')
    st_raw = STATE.read_bytes()
    st_text = st_raw.decode('utf-8')
    st_old = json.loads(st_text)

    md_had = SECTION_MARK in md_text
    st_had = TASK_ID in st_old['tasks']

    # ---- A) MD：纯追加 --------------------------------------------------
    new_md = md_text if md_had else md_text.rstrip('\n') + '\n' + MD_SECTION
    md_action = 'ALREADY_PRESENT_IDEMPOTENT_NO_WRITE' if md_had else 'APPENDED'

    # ---- B) JSON：纯插入一个 tasks 条目 --------------------------------
    if st_had:
        new_st = st_text
        st_action = 'ALREADY_PRESENT_IDEMPOTENT_NO_WRITE'
    else:
        _, obj_end = tasks_object_span(st_text)
        p = st_text.rindex('}', 0, obj_end)
        new_st = st_text[:p + 1] + ',\r\n' + block_text() + st_text[p + 1:]
        st_action = 'INSERTED'

    # ---- 断言（写在脚本里，不靠嘴上说）--------------------------------
    strip = lambda d: {k: v for k, v in d.items() if k != TASK_ID}
    st_new_obj = json.loads(new_st)
    other_equal = strip(st_old['tasks']) == strip(st_new_obj['tasks'])
    top_equal = {k: v for k, v in st_old.items() if k != 'tasks'} == \
                {k: v for k, v in st_new_obj.items() if k != 'tasks'}
    json_parses = isinstance(st_new_obj, dict)

    md_new_pairs = md_pairs_from(new_md, SECTION_MARK)
    ent = st_new_obj['tasks'].get(TASK_ID, {})
    conflicts, exact, loose = [], 0, []
    for jk, mk in MAP_TO_MD.items():
        if mk not in md_new_pairs:
            conflicts.append('%s:MD_KEY_ABSENT' % mk)
            continue
        jv, mv = ent.get(jk), md_new_pairs[mk]
        if str(jv) == mv:
            exact += 1
            continue
        if jv == [] and mv.upper() == 'NONE':
            loose.append('%s:[]⇄NONE' % mk)
            continue
        squash = lambda s: str(s).replace(' ', '').replace('/', '').replace('_', '').upper()
        if squash(jv) == squash(mv):
            loose.append('%s:%r⇄%r(去空格/斜杠/下划线后相等)' % (mk, jv, mv))
            continue
        conflicts.append('%s: json=%r md=%r' % (mk, jv, mv))

    # MD 的纯追加性：旧文本必须是新文本的前缀（= 一行都没删、没改）
    md_prefix_holds = new_md.startswith(md_text.rstrip('\n'))
    md_history_untouched = all(mark in new_md for mark in
                               ('## §6.11 ', '### §6.11.1 ', '### §6.11.2 ',
                                '## §6.12 ', '### §6.12.1 '))
    # JSON 的纯插入性：除插入点外前后缀相等
    if st_had:
        st_structural = new_st == st_text
    else:
        _, obj_end = tasks_object_span(st_text)
        p = st_text.rindex('}', 0, obj_end)
        ins = ',\r\n' + block_text()
        st_structural = new_st[:p + 1] == st_text[:p + 1] and \
            new_st[p + 1 + len(ins):] == st_text[p + 1:]

    # 生产件哈希必须仍等于本轮 POST 台账（回写文档不许动生产）
    man = json.loads((EVDIR / 'POST_CHANGE_MANIFEST.json').read_text(encoding='utf-8'))
    want = {e['file']: (e['sha256'], e['bytes']) for e in man['uiApp']
            if e['file'] in ('index.html', 'ui_layout_modes.js', 'gf_dock.css')}
    prod_changed = []
    for f, (h, n) in want.items():
        b = (ROOT / 'runtime' / 'ui' / 'app' / f).read_bytes()
        if hashlib.sha256(b).hexdigest() != h or len(b) != n:
            prod_changed.append(f)

    mode = 'WRITE' if do_write else 'DRY_RUN (no bytes written)'
    if do_write:
        if not md_had:
            MASTER_MD.write_bytes(new_md.encode('utf-8'))
        if not st_had:
            STATE.write_bytes(new_st.encode('utf-8'))

    md_disk = MASTER_MD.read_bytes()
    st_disk = STATE.read_bytes()
    md_disk_text = md_disk.decode('utf-8')
    st_disk_obj = json.loads(st_disk.decode('utf-8'))
    disk_md_ok = (SECTION_MARK in md_disk_text) if not do_write else (SECTION_MARK in md_disk_text)
    disk_st_ok = TASK_ID in st_disk_obj['tasks']
    # 干跑时磁盘还没条目，判据改用「将要写入的文本」
    md_pair_basis = md_disk_text if (md_had or do_write) else new_md
    st_ent_basis = st_disk_obj['tasks'].get(TASK_ID) if (st_had or do_write) else ent
    pairs_recomputed = []
    for jk, mk in MAP_TO_MD.items():
        mv = md_pairs_from(md_pair_basis, SECTION_MARK).get(mk)
        jv = st_ent_basis.get(jk)
        if jv is None and mk not in ('LIVE_PORTRAIT_CORE_MODEL',):
            continue
        squash = lambda s: str(s).replace(' ', '').replace('/', '').replace('_', '').upper()
        if str(jv) != mv and squash(jv) != squash(mv) and not (jv == [] and str(mv).upper() == 'NONE'):
            pairs_recomputed.append('%s json=%r md=%r' % (jk, jv, mv))

    print('MODE = %s' % mode)
    print('MD_ACTION = %s | JSON_ACTION = %s' % (md_action, st_action))
    print('JSON_PARSE = %s' % ('PASS' if json_parses else 'FAIL'))
    print('OTHER_TASKS_UNTOUCHED = %s | TOP_LEVEL_UNTOUCHED = %s' % (other_equal, top_equal))
    print('JSON_PURE_INSERT_NO_REFORMAT = %s' % st_structural)
    print('MD_PURE_APPEND_OLD_TEXT_IS_PREFIX = %s | MD_HISTORY_SECTIONS_611_612_PRESENT = %s'
          % (md_prefix_holds, md_history_untouched))
    print('MD_KEY_BLOCK_PAIRS_CHECKED = %d | EXACT = %d | SYNONYM = %d %s'
          % (len(MAP_TO_MD), exact, len(loose), loose))
    print('DOCK_MIRROR_VALUE_CONFLICTS_WITH_MASTER_MD = %d %s' % (len(conflicts), conflicts))
    print('DISK_READBACK_MD_HAS_SECTION = %s | DISK_JSON_HAS_TASK = %s | DISK_PAIR_CONFLICTS = %d %s'
          % (disk_md_ok, disk_st_ok, len(pairs_recomputed), pairs_recomputed))
    print('PRODUCTION_FILE_HASH_CHANGES = %d %s' % (len(prod_changed), prod_changed))
    print('MASTER_MD bytes = %d (was %d, delta = %d) | lines = %d | sha256 = %s'
          % (len(md_disk), len(md_raw), len(md_disk) - len(md_raw),
             md_disk_text.count('\n'), hashlib.sha256(md_disk).hexdigest()))
    print('MASTER_STATE bytes = %d (was %d, delta = %d) | lines = %d | sha256 = %s'
          % (len(st_disk), len(st_raw), len(st_disk) - len(st_raw),
             st_disk.decode('utf-8').count('\r\n') + 1, hashlib.sha256(st_disk).hexdigest()))
    ok = (json_parses and other_equal and top_equal and st_structural and md_prefix_holds
          and md_history_untouched and not conflicts and not prod_changed
          and (disk_md_ok if (md_had or do_write) else True)
          and (disk_st_ok if (st_had or do_write) else True)
          and not pairs_recomputed)
    print('VERDICT = %s' % ('DOCK_MASTER_WRITEBACK_PASS' if ok else 'DOCK_MASTER_WRITEBACK_FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
