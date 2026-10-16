/* ============================================================================
 * ui_prefs_v2.cjs — V2（UI_DETAIL_COMPOSITION_REPAIR_01）的布局键白名单
 *
 * 为什么不复用 V1 的 ui_prefs.cjs：V2 的 owner 多了一个持久化键
 * （avatarVideoStretchX，§8 的"人物宽度"），面板收起态也换了新键名
 * （ai_girlfriend_uivm_panel_v2）。名单漏一个，测试就会把【自己写过的值】
 * 留在用户 origin 上 —— 那正是 V1 里 screenshot 批次互相污染的同一种错。
 * V1 的文件属上一轮已验收证据链，本轮不顺手改（范围外只登记不代改）。
 *
 * 铁律不变：只按名字动，绝不用 localStorage.clear()（同 origin 上有 s2s.* 业务键）。
 * ==========================================================================*/
'use strict';

const LAYOUT_KEYS = {
  mine: ['ai_girlfriend_ui_layout_v1', 'ai_girlfriend_uivm_panel_v2'],
  owner: ['avatarVideoScale', 'avatarVideoStretchX', 'avatarVideoOffsetX', 'avatarVideoOffsetY'],
  legacy: ['uiViewModePanelCollapsed', 'avatarPanelRatio'],   // 只测不写：验证"被忽略"
};
const ALL = LAYOUT_KEYS.mine.concat(LAYOUT_KEYS.owner);

async function snapshot(tab) {
  return await tab.js(`(() => {
    const names = ${JSON.stringify(ALL.concat(LAYOUT_KEYS.legacy))}; const out = {};
    for (const n of names) out[n] = localStorage.getItem(n);
    return { values: out, allKeys: Object.keys(localStorage).sort() };
  })()`);
}

/** 只删白名单里的键（含 V1 遗留名，为了测"迁移后旧名不再影响布局"）。 */
async function clearLayoutKeys(tab) {
  return await tab.js(`(() => {
    const names = ${JSON.stringify(ALL.concat(LAYOUT_KEYS.legacy))}; const removed = {};
    for (const n of names) { removed[n] = localStorage.getItem(n); localStorage.removeItem(n); }
    return { removed, leftKeys: Object.keys(localStorage).sort() };
  })()`);
}

async function restore(tab, snap) {
  const names = ALL.concat(LAYOUT_KEYS.legacy);
  const pairs = names.map((n) => [n, snap.values[n]]);
  return await tab.js(`(() => {
    const pairs = ${JSON.stringify(pairs)}; const res = {};
    for (const [n, v] of pairs) {
      if (v === null || v === undefined) { localStorage.removeItem(n); res[n] = 'REMOVED_AS_BEFORE'; }
      else { localStorage.setItem(n, v); res[n] = 'REWROTE'; }
    }
    const now = {}; for (const [n] of pairs) now[n] = localStorage.getItem(n);
    return { actions: res, now,
             faithful: pairs.every(([n, v]) => (now[n] ?? null) === (v ?? null)) };
  })()`);
}

/** 业务键对账：把布局键（含遗留名）从两边都剔掉，剩下的必须逐字相同。 */
function businessKeysEqual(before, after) {
  const strip = (a) => (a || []).filter((k) => ALL.indexOf(k) === -1
    && LAYOUT_KEYS.legacy.indexOf(k) === -1).sort();
  const b = strip(before), a = strip(after);
  return {
    before: b, after: a,
    equal: b.length === a.length && b.every((k, i) => k === a[i]),
    added: a.filter((k) => b.indexOf(k) === -1),
    removed: b.filter((k) => a.indexOf(k) === -1),
  };
}

module.exports = { LAYOUT_KEYS, ALL, snapshot, clearLayoutKeys, restore, businessKeysEqual };
