/* ============================================================================
 * ui_prefs.cjs — 布局类 localStorage 键的"快照 → 清理 → 还原"
 *
 * 为什么必须有它（现场教训）：上一版 screenshots.cjs 只清自己那一个键，结果
 * 上一轮把 avatar_layout.js 的 avatarVideoScale=160 留在了 origin 上，
 * 这一轮 DETAIL_DEFAULT 拍出来就是【放大后】的构图 —— 与 DETAIL_AVATAR_LARGE
 * 几何完全相同，两张本该形成对比的交付图退化成同一张。§5.3 的"首屏=改造前"
 * 必须有"其它 owner 的持久化参数也处于改造前状态"这个前提才成立。
 *
 * 另一层责任：这些键是【用户 origin 上的真实偏好】。测试把它们改了，
 * 收尾就得还原回去，不能拿"反正只是布局参数"当借口留下副作用。
 *
 * 铁律：只按名字动下面这 4 个键，其余一律不碰。
 *   · 绝不用 localStorage.clear() —— 同一 origin 上还有 s2s.* 等业务键（§16）
 *   · 每个键先读原值存进证据，restore 时逐键比对（含"原本不存在"这一态）
 * ==========================================================================*/
'use strict';

const LAYOUT_KEYS = {
  mine: 'ai_girlfriend_ui_layout_v1',                       // 本任务新增
  existingOwner: ['avatarVideoScale', 'avatarVideoOffsetX', 'avatarVideoOffsetY'], // 冻结 owner: avatar_layout.js
};
const ALL = [LAYOUT_KEYS.mine].concat(LAYOUT_KEYS.existingOwner);

/** 读出这 4 个键的当前值（null = 原本不存在，这个信息本身要保留）。 */
async function snapshot(tab) {
  const names = JSON.stringify(ALL);
  return await tab.js(`(() => {
    const names = ${names}; const out = {};
    for (const n of names) out[n] = localStorage.getItem(n);
    return { values: out, allKeys: Object.keys(localStorage).sort() };
  })()`);
}

/** 只删这 4 个名字，返回删掉了哪些、剩余键集合。 */
async function clearLayoutKeys(tab) {
  return await tab.js(`(() => {
    const names = ${JSON.stringify(ALL)}; const removed = {};
    for (const n of names) { removed[n] = localStorage.getItem(n); localStorage.removeItem(n); }
    return { removed, leftKeys: Object.keys(localStorage).sort() };
  })()`);
}

/** 逐键还原：原本有值的写回，原本不存在的删掉。返回逐键对账结果。 */
async function restore(tab, snap) {
  const pairs = ALL.map((n) => [n, snap.values[n]]);
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

/** 业务键对账：把 4 个布局键从两边都剔掉，剩下的必须逐字相同、顺序相同。 */
function businessKeysEqual(before, after) {
  const strip = (a) => (a || []).filter((k) => ALL.indexOf(k) === -1).sort();
  const b = strip(before), a = strip(after);
  return {
    before: b, after: a,
    equal: b.length === a.length && b.every((k, i) => k === a[i]),
    added: a.filter((k) => b.indexOf(k) === -1),
    removed: b.filter((k) => a.indexOf(k) === -1),
  };
}

module.exports = { LAYOUT_KEYS, ALL, snapshot, clearLayoutKeys, restore, businessKeysEqual };
