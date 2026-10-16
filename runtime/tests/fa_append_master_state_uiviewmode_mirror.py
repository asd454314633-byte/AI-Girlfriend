#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fa_append_master_state_uiviewmode_mirror.py — 独立「状态镜像同步」动作

TASK_ID = AI_GIRLFRIEND_MASTER_STATE_UI_VIEWMODE_MIRROR_01

把已经 CLOSED/PASS 的 UI_DUAL_VIEWMODE 最小事实镜像进
runtime/llm/AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json 的 "tasks" 对象。

铁律（本脚本的实现就是按这些约束写的）：
  · 只在 tasks 对象里插入这一个条目；文件其余字节逐字保留（前缀/后缀相等即证）。
  · 不重排、不格式化、不改键名大小写风格（沿用文件既有的 CRLF + 4/6 空格缩进 + snake_case）。
  · 不改其他 task 的状态：解析后除新键以外的 dict 必须与旧 dict 完全相等。
  · 幂等：条目已存在时只校验、不再写入（重复运行不产生第二次插入）。
  · 不碰生产代码 / UI / 测试资产；不跑任何测试。

用法：python runtime/tests/fa_append_master_state_uiviewmode_mirror.py [--check-only]
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'runtime' / 'llm' / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json'
MASTER_MD = ROOT / 'runtime' / 'llm' / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md'
TASK_ID = 'AI_GIRLFRIEND_UI_DUAL_VIEWMODE_PRESENTATION_ONLY_01'

# 镜像内容 = 用户裁定的核心事实（不复制报告正文）。
# verdict 是该文件跨任务的既有读取惯例键，与 status 同值同义（唯一新增的别名键，已在主交接披露）。
FIELDS = [
    ('task_id', TASK_ID),
    ('status', 'CLOSED_PASS'),
    ('verdict', 'CLOSED_PASS'),
    ('ui_only_scope', 'PASS'),
    ('detail_mode', 'PASS'),
    ('live_portrait_mode', 'PASS'),
    ('live_stage_9_16', 'PASS'),
    ('single_avatar_video_node', 'PASS'),
    ('mediastream_identity_preserved', 'PASS'),
    ('view_switch_no_reconnect', 'PASS'),
    ('layout_generated_backend_requests', 0),
    ('non_ui_modified_files', []),
    ('avatar_layout_transform_owner', 'avatar_layout.js'),
    ('viewmode_layer_role', 'MODE_SWITCH_AND_PRESET_ONLY'),
    ('second_transform_owner', 'NONE'),
    ('detail_duplicate_scale_xy_controls', 'NONE'),
    ('detail_pane_divider_range_pct', '28-70'),
    ('detail_pane_divider_range_state', 'FROZEN'),
    ('final_report', 'runtime/evidence/UI_DUAL_VIEWMODE_01/FINAL_REPORT.md'),
]

# 镜像键 → 主交接 MD 里的正式键（语义对拍表；snake_case 是本 JSON 层的既有风格）
MAP_TO_MD = {
    'status': 'STATUS',
    'ui_only_scope': 'UI_ONLY_SCOPE',
    'detail_mode': 'DETAIL_MODE',
    'live_portrait_mode': 'LIVE_PORTRAIT_MODE',
    'live_stage_9_16': 'LIVE_STAGE_9_16',
    'single_avatar_video_node': 'SINGLE_AVATAR_VIDEO_NODE',
    'mediastream_identity_preserved': 'MEDIASTREAM_IDENTITY_PRESERVED',
    'view_switch_no_reconnect': 'VIEW_SWITCH_NO_RECONNECT',
    'layout_generated_backend_requests': 'LAYOUT_GENERATED_BACKEND_REQUESTS',
    'non_ui_modified_files': 'NON_UI_MODIFIED_FILES',
    'avatar_layout_transform_owner': 'AVATAR_LAYOUT_TRANSFORM_OWNER',
    'viewmode_layer_role': 'VIEWMODE_LAYER_ROLE',
    'second_transform_owner': 'SECOND_TRANSFORM_OWNER',
    'detail_duplicate_scale_xy_controls': 'DETAIL_DUPLICATE_SCALE_XY_CONTROLS',
    'detail_pane_divider_range_pct': 'DETAIL_PANE_DIVIDER_RANGE_PCT',
    'detail_pane_divider_range_state': 'DETAIL_PANE_DIVIDER_RANGE_STATE',
}
# 归一化：CLOSED / PASS 与 CLOSED_PASS 同义；空数组 [] 与 NONE 同义
NORM = {('status', 'CLOSED_PASS'): 'CLOSEDPASS'}


def block_text():
    inner = ',\r\n'.join('      %s: %s' % (json.dumps(k), json.dumps(v, ensure_ascii=False))
                         for k, v in FIELDS)
    return '    %s: {\r\n%s\r\n    }' % (json.dumps(TASK_ID), inner)


def tasks_object_span(text):
    """返回 ("tasks" 对象值的) 起始 { 与结束 } 的下标；字符串感知的括号扫描。"""
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


def prev_entry_close(text, obj_end):
    """tasks 对象内最后一个条目的收尾 }（在它后面插逗号 + 新条目）。"""
    return text.rindex('}', 0, obj_end)


def main():
    check_only = '--check-only' in sys.argv
    raw = STATE.read_bytes()
    text = raw.decode('utf-8')
    old_obj = json.loads(text)
    had = TASK_ID in old_obj['tasks']

    if had:
        new_text = text
        action = 'ALREADY_PRESENT_IDEMPOTENT_NO_WRITE'
    else:
        _, obj_end = tasks_object_span(text)
        p = prev_entry_close(text, obj_end)
        ins = ',\r\n' + block_text()
        new_text = text[:p + 1] + ins + text[p + 1:]
        action = 'INSERTED'

    # 语义不变式：除本条目以外，解析结果必须与旧文件完全相等（= 没动过别的 task）
    #   两侧都要先剔除 TASK_ID：否则"条目已在盘上"的幂等复跑会自己判成不等（假 FAIL）。
    new_obj = json.loads(new_text)
    strip = lambda d: {k: v for k, v in d.items() if k != TASK_ID}
    other_equal = strip(old_obj['tasks']) == strip(new_obj['tasks'])
    top_equal = {k: v for k, v in old_obj.items() if k != 'tasks'} == \
                {k: v for k, v in new_obj.items() if k != 'tasks'}
    assert (TASK_ID in new_obj['tasks']) or had, 'ENTRY_MISSING'

    if not check_only and not had:
        STATE.write_bytes(new_text.encode('utf-8'))

    disk = STATE.read_bytes()
    dtext = disk.decode('utf-8')
    dobj = json.loads(dtext)
    present_new = TASK_ID in json.loads(new_text)['tasks']
    present_disk = TASK_ID in dobj['tasks']

    # ① 前缀/后缀相等 = 纯插入（未格式化、未重排）
    #   check-only 时磁盘尚未写入 ⇒ 用「将要写入的文本」对拍，并标明是预演
    basis = dtext if not check_only else new_text
    mode_note = '' if not check_only else ' (PREVIEW, not yet written)'
    if had:
        structural = basis == text
    else:
        p = prev_entry_close(text, tasks_object_span(text)[1])
        structural = basis[:p + 1] == text[:p + 1] and \
            basis[p + 1 + len(',\r\n' + block_text()):] == text[p + 1:]

    # ② 与主交接 MD 的语义对拍
    md = MASTER_MD.read_text(encoding='utf-8')
    section = md[md.index('## §6.12'):]
    md_pairs = {}
    for line in section.splitlines():
        s = line.strip().strip('`')
        if '=' in s and not s.startswith('#'):
            k, _, v = s.partition('=')
            md_pairs[k.strip()] = v.strip()
    conflicts, checked, exact, loose = [], 0, 0, []
    ent = json.loads(basis)['tasks'][TASK_ID]
    for jk, mk in MAP_TO_MD.items():
        if mk not in md_pairs:
            conflicts.append('%s:MD_KEY_ABSENT' % mk)
            continue
        jv, mv = ent.get(jk), md_pairs[mk]
        checked += 1
        # 第 1 档：逐字相等
        if str(jv) == mv:
            exact += 1
            continue
        # 第 2 档：空数组 ⇄ NONE（JSON 层用空数组表达"无"）
        if jv == [] and mv.upper() == 'NONE':
            loose.append('%s:[]⇄NONE' % mk)
            continue
        # 第 3 档：去分隔符归一化（CLOSED / PASS ⇄ CLOSED_PASS 这类同义写法）
        squash = lambda s: str(s).replace(' ', '').replace('/', '').replace('_', '').upper()
        if squash(jv) == squash(mv):
            loose.append('%s:%r⇄%r(去空格/斜杠/下划线后相等)' % (mk, jv, mv))
            continue
        conflicts.append('%s: json=%r md=%r' % (mk, jv, mv))

    # ③ 生产文件哈希变化（与 PRE/POST 台账对拍）
    man = json.loads((ROOT / 'runtime' / 'evidence' / 'UI_DUAL_VIEWMODE_01' /
                      'POST_CHANGE_MANIFEST.json').read_text(encoding='utf-8'))
    rec = {e['file']: (e['sha256'], e['bytes']) for e in man['uiApp']
           if e['file'] in ('index.html', 'ui_layout_modes.css', 'ui_layout_modes.js')}
    prod_changed = []
    for f, (h, n) in rec.items():
        b = (ROOT / 'runtime' / 'ui' / 'app' / f).read_bytes()
        if hashlib.sha256(b).hexdigest() != h or len(b) != n:
            prod_changed.append(f)

    print('ACTION = %s%s' % (action, mode_note))
    print('JSON_PARSE = %s' % ('PASS' if isinstance(dobj, dict) else 'FAIL'))
    print('OTHER_TASKS_UNTOUCHED = %s | TOP_LEVEL_UNTOUCHED = %s' % (other_equal, top_equal))
    print('PURE_INSERT_NO_REFORMAT = %s' % structural)
    print('MIRROR_ENTRY_IN_TARGET = %s | ON_DISK = %s | fields = %d'
          % (present_new, present_disk, len(ent)))
    print('MD_PAIRS_IN_SECTION = %d | SEMANTIC_PAIRS_CHECKED = %d | EXACT = %d'
          % (len(md_pairs), checked, exact))
    print('SYNONYM_PAIRS (same after documented normalization) = %d %s' % (len(loose), loose))
    print('UI_VIEWMODE_MIRROR_VALUE_CONFLICTS_WITH_MASTER_MD = %d %s'
          % (len(conflicts), conflicts if conflicts else ''))
    print('PRODUCTION_FILE_HASH_CHANGES = %d %s' % (len(prod_changed), prod_changed))
    print('STATE bytes = %d | lines = %d | sha256 = %s'
          % (len(disk), dtext.count('\r\n') + 1, hashlib.sha256(disk).hexdigest()))
    print('BASELINE bytes = %d | delta = %d' % (len(raw), len(disk) - len(raw)))
    ok = (isinstance(dobj, dict) and other_equal and top_equal and structural
          and present_new and (present_disk or check_only) and not conflicts
          and not prod_changed)
    print('VERDICT = %s%s' % ('PASS' if ok else 'FAIL',
                              '' if not check_only else ' [dry-run: 未写入]'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
