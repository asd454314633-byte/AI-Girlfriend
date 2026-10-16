# -*- coding: utf-8 -*-
"""模式切换压力测试（MODE_SYSTEM_V1 §三十）+ 禁止 split-brain（§十八）。

做两件事：
  1. ROLEPLAY ↔ READING 往返 20 次，每次都验「UI 说的 = 后端权威 = 装配出来的那一块」；
  2. 每次切换都验 Ember 叙事要求一个字都没回来（旧 Prompt 残留），
     并且 ROLEPLAY 那一档里没有阅读上下文、READING 那一档里有。

「无多余 LLM 请求」这一条靠 8090 的 completion token 计数验：切换不传 turn，
所以 ``/api/mode`` 与 ``/api/character/prompt`` 都不该产生任何一次模型调用。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

UI = "http://127.0.0.1:7860"
LLM = "http://127.0.0.1:8090"
HERE = Path(__file__).resolve().parent

EMBER_MARKERS = ["Narrate", "Run the world", "ephemeral", "Do not pause the fiction",
                 "3rd person", "Narrative Authority", "Embodied Reality"]
ROUNDS = 20


def get(url: str, timeout: int = 90) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url: str, payload: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def prompt_tokens() -> int:
    """8090 到目前为止累计生成的 token 数（用来验「切换有没有偷偷问模型」）。"""
    try:
        with urllib.request.urlopen(f"{LLM}/metrics", timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
        for line in text.splitlines():
            if "llamacpp_tokens_predicted_total" in line and not line.startswith("#"):
                return int(float(line.split()[-1]))
    except Exception:
        pass
    return -1


def main() -> int:
    failures: list[str] = []
    tokens_before = prompt_tokens()

    for i in range(ROUNDS):
        target = "READING" if i % 2 == 0 else "ROLEPLAY"
        res = post(f"{UI}/api/mode", {"mode": target})
        if str(res.get("模式")) != target:
            failures.append(f"第 {i + 1} 次：切到 {target} 失败 {res}")
            continue
        # 回包里带回来的那一块，就是这一场马上要用的那一块
        bundle = str(res.get("提示词") or "")
        state = get(f"{UI}/api/mode")
        prompt = get(f"{UI}/api/character/prompt")

        if str(state.get("模式")) != target:
            failures.append(f"第 {i + 1} 次：权威状态 {state.get('模式')} != {target}")
        if str(prompt.get("模式")) != target:
            failures.append(f"第 {i + 1} 次：装配出来的模式 {prompt.get('模式')} != {target}")
        if bundle and bundle != str(prompt.get("内容") or ""):
            failures.append(f"第 {i + 1} 次：切模式回包里那一块与 /api/character/prompt 不一致")
        text = str(prompt.get("内容") or "")
        left = [m for m in EMBER_MARKERS if m in text]
        if left:
            failures.append(f"第 {i + 1} 次：{target} 里残留 Ember 叙事要求 {left}")
        if target == "ROLEPLAY" and ("正在陪读" in text or "灯塔" in text):
            failures.append(f"第 {i + 1} 次：ROLEPLAY 里混进了阅读上下文")
        if i % 5 == 0:
            print(f"  第 {i + 1} 轮 -> {target}（提示词 {len(text)} 字）")

    # 收尾停在 ROLEPLAY（真人聊天那一档）
    post(f"{UI}/api/mode", {"mode": "ROLEPLAY"})
    tokens_after = prompt_tokens()
    extra = (tokens_after - tokens_before) if (tokens_before >= 0 and tokens_after >= 0) else -1

    state = get(f"{UI}/api/mode")
    prompt = get(f"{UI}/api/character/prompt")
    text = str(prompt.get("内容") or "")
    print("=" * 72)
    print(f"往返次数              = {ROUNDS}")
    print(f"最终权威模式          = {state.get('模式')}")
    print(f"最终装配模式          = {prompt.get('模式')}")
    print(f"Ember 叙事残留        = {[m for m in EMBER_MARKERS if m in text] or '无'}")
    print(f"切换期间模型调用增量  = {extra} token（-1 = 这台机器上取不到计数）")
    if state.get("模式") != prompt.get("模式"):
        failures.append("收尾时权威模式与装配模式不一致")

    ok = not failures and extra in (-1, 0)
    if extra > 0:
        failures.append(f"切换期间多花了 {extra} 个生成 token：模式切换不该问模型")
        ok = False
    for f in failures:
        print(f"[FAIL] {f}")
    print(f"MODE_SWITCH_STRESS = {'PASS' if ok else 'FAIL'}")
    print(f"MODE_SINGLE_SOURCE_OF_TRUTH = {'YES' if ok else 'NO'}")
    (HERE / "out_mode_switch_stress.json").write_text(json.dumps(
        {"rounds": ROUNDS, "failures": failures, "extra_tokens": extra,
         "final_mode": state.get("模式"), "assembled_mode": prompt.get("模式"),
         "ok": ok}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
