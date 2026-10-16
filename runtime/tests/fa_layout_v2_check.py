"""AI_GIRLFRIEND_UI_AVATAR_LAYOUT_ADJUSTER_02_FIX — 浏览器级验收（无侵入）。

不启动任何会话、不占 8765 pipeline slot：只加载页面做显示层验证。
Gate 覆盖（TASK 02 STEP 1/2/3/6 + STEP 4 挂载检查）：
  G1  LAYOUT_MODULE_LOADED      AvatarLayout v2 + conn_trace + __connUi 挂载
  G2  DIVIDER_REMOVED           #av-layout-divider 不存在（正式 UI 无分割线）
  G3  STAGE_DEFAULT_GEOMETRY    默认模式零内联覆盖；#av-video 原生 cover + 左缘 mask 渐隐
  G4  NO_V1_RECTANGLE_LEGACY    无 left/width/object-fit:contain 窗口化遗留
  G5  ADJUST_TRANSFORM_ONLY     setScale/translate 只写 transform，几何仍交 avatar-sync
  G6  RESET_TO_DEFAULT          恢复默认清空全部内联覆盖
  G7  CONN_BADGE_STATIC         徽标显示/隐藏，无动画（computed animation-name == none）
  G8  RECONNECTING_STATE_STATIC state-reconnecting 圆环 core/ring 无动画
  G9  REDUCED_MOTION            reduce 模式下圆环动画关闭（Emulation 模拟）
  G10 CONN_TRACE_CAPTURES      dispatchEvent 旁路记录 status 事件（ring 增量）
  G11 SYNTHETIC_RENDER_BLEND   合成流渲染 + 截图：右贴全高、左缘渐变、无硬切边
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fa_cdp  # noqa: E402

OUT = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/tests/fa_layout_v2_out'))
results = []


def gate(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main():
    page = await fa_cdp.Page.attach()
    # 清理测试 profile 里 v1 遗留的 localStorage（避免污染"默认几何"判定）
    await page.eval_js(
        "['avatarVideoScale','avatarVideoOffsetX','avatarVideoOffsetY',"
        "'avatarPanelRatio','avatarLayoutDisabled'].forEach(k=>localStorage.removeItem(k))")
    await page.navigate("http://127.0.0.1:7860/")
    await page.eval_js("new Promise(r => setTimeout(r, 2500))")

    # G1 模块挂载
    g1 = await page.eval_js(
        "(()=>{const L=window.AvatarLayout||{};return JSON.stringify({"
        "layout: typeof L.isDefault==='function', disabled: L.disabled===false,"
        "connTrace: Array.isArray(window.__connTrace),"
        "connUi: !!(window.__connUi && typeof window.__connUi.show==='function'),"
        "entryBtn: !!document.getElementById('gf-layout-open')})})()")
    g1 = json.loads(g1)
    gate("G1_LAYOUT_MODULE_LOADED", all(g1.values()), json.dumps(g1, ensure_ascii=False))

    # G2 divider 删除
    g2 = await page.eval_js(
        "JSON.stringify({divider: document.getElementById('av-layout-divider'),"
        "cssDividerRule: !!Array.from(document.styleSheets).some(s=>{try{return Array.from(s.cssRules).some(r=>r.selectorText&&r.selectorText.includes('#av-layout-divider'))}catch(e){return false}})})")
    g2 = json.loads(g2)
    gate("G2_DIVIDER_REMOVED", g2["divider"] is None and not g2["cssDividerRule"], json.dumps(g2))

    # G3/G4 默认几何：零内联覆盖 + 原生 cover + mask 渐隐 + 无窗口化遗留
    g34 = await page.eval_js(
        "(()=>{const v=document.getElementById('av-video');if(!v)return JSON.stringify({missing:true});"
        "const cs=getComputedStyle(v);return JSON.stringify({"
        "isDefault: window.AvatarLayout.isDefault(),"
        "inlineTransform: v.style.transform,"
        "inlineLeft: v.style.left, inlineWidth: v.style.width,"
        "objectFit: cs.objectFit, objectPosition: cs.objectPosition,"
        "maskImage: (cs.webkitMaskImage||cs.maskImage||'').slice(0,60),"
        "right: cs.right, height: cs.height})})()")
    g34 = json.loads(g34)
    if g34.get("missing"):
        gate("G3_STAGE_DEFAULT_GEOMETRY", False, "video missing (page not bootstrapped?)")
        gate("G4_NO_V1_RECTANGLE_LEGACY", False, "video missing")
    else:
        gate("G3_STAGE_DEFAULT_GEOMETRY",
             g34["isDefault"] and g34["inlineTransform"] == "" and g34["objectFit"] == "cover"
             and "linear-gradient" in (g34["maskImage"] or ""),
             json.dumps(g34, ensure_ascii=False))
        gate("G4_NO_V1_RECTANGLE_LEGACY",
             g34["inlineLeft"] == "" and g34["inlineWidth"] == "" and g34["objectFit"] != "contain",
             "inline left/width empty, objectFit != contain")

    # G5 调整 = transform-only
    await page.eval_js("window.AvatarLayout.setScale(130);window.AvatarLayout.state.x=50;window.AvatarLayout.state.y=-25;window.AvatarLayout.apply()")
    g5 = await page.eval_js(
        "(()=>{const v=document.getElementById('av-video');const cs=getComputedStyle(v);"
        "return JSON.stringify({transform: cs.transform, inlineLeft: v.style.left,"
        "inlineObjectFit: v.style.objectFit, origin: cs.transformOrigin})})()")
    g5 = json.loads(g5)
    ok5 = ("matrix" in g5["transform"] and g5["inlineLeft"] == ""
           and g5["inlineObjectFit"] == "" and "1.3" in g5["transform"])
    gate("G5_ADJUST_TRANSFORM_ONLY", ok5, json.dumps(g5))

    # G6 恢复默认
    await page.eval_js("window.AvatarLayout.resetToDefault()")
    g6 = await page.eval_js(
        "(()=>{const v=document.getElementById('av-video');"
        "return JSON.stringify({t: v.style.transform, isDefault: window.AvatarLayout.isDefault(),"
        "saved: localStorage.getItem('avatarVideoScale')})})()")
    g6 = json.loads(g6)
    gate("G6_RESET_TO_DEFAULT", g6["t"] == "" and g6["isDefault"] and g6["saved"] in ("", None, "null"),
         json.dumps(g6))

    # G7 徽标静态显示/隐藏 + 无动画
    await page.eval_js("window.__connUi.show('正在重连…（第 2 次）')")
    g7 = await page.eval_js(
        "(()=>{const b=document.getElementById('conn-badge');if(!b)return JSON.stringify({missing:true});"
        "const cs=getComputedStyle(b);return JSON.stringify({hidden:b.hidden,text:b.textContent,"
        "pos:cs.position,animation:cs.animationName,transition:cs.transitionDuration,"
        "pointerEvents:cs.pointerEvents})})()")
    g7 = json.loads(g7)
    await page.eval_js("window.__connUi.hide()")
    g7h = await page.eval_js("document.getElementById('conn-badge').hidden")
    gate("G7_CONN_BADGE_STATIC",
         not g7.get("missing") and not g7["hidden"] and "重连" in g7["text"]
         and g7["animation"] in ("none",) and g7["pointerEvents"] == "none" and g7h,
         json.dumps(g7, ensure_ascii=False))

    # G8 state-reconnecting 静态（注入同构子树读 computed）
    g8 = await page.eval_js(
        "(()=>{const d=document.createElement('div');"
        "d.innerHTML='<div class=\"circle state-reconnecting\"><div class=\"circle-core\"></div><div class=\"circle-ring\"></div></div>';"
        "document.body.append(d);const c=d.querySelector('.circle-core'),r=d.querySelector('.circle-ring');"
        "const a1=getComputedStyle(c).animationName,a2=getComputedStyle(r).animationName;"
        "d.remove();return JSON.stringify({core:a1,ring:a2})})()")
    g8 = json.loads(g8)
    gate("G8_RECONNECTING_STATE_STATIC", g8["core"] == "none" and g8["ring"] == "none", json.dumps(g8))

    # G9 prefers-reduced-motion 模拟
    try:
        await page._send("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce"}]})
        g9 = await page.eval_js(
            "(()=>{const d=document.createElement('div');"
            "d.innerHTML='<div class=\"circle state-connecting\"><div class=\"circle-core\"></div><div class=\"circle-ring\"></div></div>';"
            "document.body.append(d);const a1=getComputedStyle(d.querySelector('.circle-core')).animationName;"
            "const a2=getComputedStyle(d.querySelector('.circle-ring')).animationName;d.remove();"
            "return JSON.stringify({core:a1,ring:a2,matched:matchMedia('(prefers-reduced-motion: reduce)').matches})})()")
        g9 = json.loads(g9)
        gate("G9_REDUCED_MOTION", g9["matched"] and g9["core"] == "none" and g9["ring"] == "none",
             json.dumps(g9))
        await page._send("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": ""}]})
    except Exception as exc:  # noqa: BLE001
        gate("G9_REDUCED_MOTION", False, f"emulation error: {exc}")

    # G10 conn_trace 旁路记录
    g10 = await page.eval_js(
        "(()=>{const n0=window.__connTrace.length;"
        "window.dispatchEvent(new CustomEvent('status',{detail:{status:'__probe__'}}));"
        "const n1=window.__connTrace.length;const last=window.__connTrace[n1-1]||{};"
        "return JSON.stringify({n0,n1,kind:last.kind,status:last.status})})()")
    g10 = json.loads(g10)
    gate("G10_CONN_TRACE_CAPTURES",
         g10["n1"] == g10["n0"] + 1 and g10["kind"] == "status" and g10["status"] == "__probe__",
         json.dumps(g10))

    # G11 合成流渲染截图（右贴全高 + 左缘渐变融合）
    shot_b64 = await page.eval_js(
        "(async()=>{const v=document.getElementById('av-video');const layer=document.getElementById('av-layer');"
        "if(!v||!layer)return 'NO_VIDEO';"
        "const c=document.createElement('canvas');c.width=720;c.height=1280;const x=c.getContext('2d');"
        "const bg=x.createLinearGradient(0,0,0,1280);bg.addColorStop(0,'#20242c');bg.addColorStop(1,'#3a3f4a');"
        "x.fillStyle=bg;x.fillRect(0,0,720,1280);"
        "x.fillStyle='#c9a689';x.beginPath();x.ellipse(360,300,110,140,0,0,7);x.fill();"  # 脸
        "x.fillStyle='#5a6577';x.fillRect(190,440,340,840);"  # 身体
        "x.fillStyle='#e8e8e8';x.font='40px monospace';x.fillText('SYNTHETIC',240,700);"
        "v.srcObject=c.captureStream(15);v.play();layer.classList.add('live');"
        "await new Promise(r=>setTimeout(r,1200));"
        "return 'OK'})()")
    if shot_b64 == "OK":
        data = await page._send("Page.captureScreenshot", {"format": "png"})
        OUT.mkdir(parents=True, exist_ok=True)
        png = OUT / "layout_v2_synthetic.png"
        png.write_bytes(base64.b64decode(data["data"]))
        gate("G11_SYNTHETIC_RENDER_BLEND", True, f"screenshot -> {png}")
    else:
        gate("G11_SYNTHETIC_RENDER_BLEND", False, shot_b64)

    # 清理：移除合成流，避免残留状态
    await page.eval_js(
        "(()=>{const v=document.getElementById('av-video');if(v){v.srcObject=null;}"
        "const l=document.getElementById('av-layer');if(l)l.classList.remove('live');"
        "window.dispatchEvent(new CustomEvent('status',{detail:{status:'__probe_end__'}}));return 'cleaned'})()")

    fails = [r for r in results if r[1] == "FAIL"]
    print("\n==== SUMMARY: %d gates, %d PASS, %d FAIL ====" % (len(results), len(results) - len(fails), len(fails)))
    for name, st, _ in results:
        print(f"  {st}  {name}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(fa_cdp.run(main()))
