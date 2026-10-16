# -*- coding: utf-8 -*-
"""角色扮演模式验收（MODE_SYSTEM_V1 §27）：真实 Gemma4，六个问题。

不测「像不像人」，只测三件可以证伪的事：
  1. THIRD_PERSON_NARRATION —— 她有没有用第三人称描述自己（「她微微垂下眼帘」）；
  2. ACTION_NARRATION      —— 有没有星号/括号里的动作、神情、心理；
  3. DIRECT_FIRST_PERSON   —— 有没有直接开口说话（不是空话、不是客服腔、不是「作为AI」）。

提示词不在这里拼：一律取生产那一条 ``/api/character/prompt``，保证测的就是线上那一块。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

UI_BASE = "http://127.0.0.1:7860"
LLM_BASE = "http://127.0.0.1:8090/v1"
CHARACTER_ID = "c279dcae6d8"

QUESTIONS = [
    "你是我的妈妈吗？",
    "你刚才在干嘛？",
    "你想我了吗？",
    "今天心情怎么样？",
    "你为什么生我的气？",
    "给我简单解释一下 LoRA。",
]

# 第三人称自述：她/他 + 人物自称，或明显的叙事句式
THIRD_PERSON_MARKERS = ["她微微", "她轻轻", "她伸手", "她看着你", "她低头", "她笑了笑",
                        "他微微", "妈妈轻轻", "妈妈微微", "她垂下", "她抬起",
                        "她顿了顿", "她说着", "她一边", "她转身"]
ACTION_MARKERS = ["*", "（", "("]
NON_PERSONA = ["作为一个AI", "作为AI", "我是人工智能", "我是一个语言模型",
               "作为一个人工智能", "我无法", "请问还有什么可以帮您", "如果您还有"]


def _post(url: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    prompt = _get(f"{UI_BASE}/api/character/prompt?character_id={CHARACTER_ID}")
    if not prompt.get("ok"):
        print(f"ABORT 取不到生产提示词：{prompt}")
        return 2
    system_text = str(prompt["内容"])
    mode = str(prompt.get("模式") or "")
    print(f"生产提示词 {len(system_text)} 字，模式={mode}")
    for kw in ("Narrate", "Run the world", "Do not pause the fiction", "3rd person"):
        if kw in system_text:
            print(f"ABORT 提示词里仍有 Ember 叙事要求：{kw}")
            return 3

    results = []
    for q in QUESTIONS:
        out = _post(f"{LLM_BASE}/chat/completions", {
            "model": "local",
            "messages": [{"role": "system", "content": system_text},
                         {"role": "user", "content": q}],
            # 不给上限会截断：Gemma4 会先出一段 reasoning_content 再开口，
            # max_tokens 给小了就只能拿到空 content（实测 300 都不够）。
            "temperature": 1.0, "top_p": 0.95, "max_tokens": 900, "stream": False,
        })
        text = str(out["choices"][0]["message"]["content"] or "").strip()
        third = [m for m in THIRD_PERSON_MARKERS if m in text]
        action = [m for m in ACTION_MARKERS if m in text]
        bad = [m for m in NON_PERSONA if m in text]
        results.append({"q": q, "text": text, "third": third, "action": action,
                        "non_persona": bad})
        print("=" * 72)
        print(f"问：{q}")
        print(f"答：{text}")
        print(f"  第三人称={third or '无'}  动作/旁白符号={action or '无'}  出戏={bad or '无'}")

    n_third = sum(1 for r in results if r["third"])
    n_action = sum(1 for r in results if r["action"])
    n_bad = sum(1 for r in results if r["non_persona"])
    n_direct = sum(1 for r in results if r["text"] and not r["third"]
                   and not r["action"] and not r["non_persona"])
    total = len(results)
    print("=" * 72)
    print(f"ROLEPLAY_NARRATION_COUNT = {n_third}/{total}")
    print(f"ACTION_NARRATION_COUNT   = {n_action}/{total}")
    print(f"NON_PERSONA_COUNT        = {n_bad}/{total}")
    print(f"DIRECT_FIRST_PERSON      = {n_direct}/{total}")
    Path(__file__).with_name("out_roleplay_acceptance.json").write_text(
        json.dumps({"mode": mode, "results": results,
                    "third_person": n_third, "action": n_action,
                    "non_persona": n_bad, "direct": n_direct, "total": total},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    ok = n_third == 0 and n_action == 0 and n_bad == 0 and n_direct == total
    print(f"ROLEPLAY_MODE = {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
