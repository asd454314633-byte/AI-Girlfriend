# -*- coding: utf-8 -*-
"""本层唯一会碰网络的地方：向**已经开着的那个** 8090 发一次很短的判断请求。

为什么单独一个模块，而不是把 HTTP 写进 turn_judge.py：

  * 判断的「脑子」（简报 + 材料 + 六个字段）和判断的「管子」（地址 + 超时 + 回执形状）
    分开，单测就能只测脑子、不碰网络；
  * 全仓库只有这一处发请求，出问题顺着 import 就能找到；
  * 装配路径（context_builder → engine）永远不 import 到这里 —— 这一层不许把
    一次模型调用藏在「拼提示词」里面，那条路是延迟黑洞。

三条硬规矩：

  1. **不新增服务、不新增模型、不改 8090 参数**：地址从 ``runtime/llm/models.json``
     的 ``listen.port`` 读。请求体带 ``messages``/``stream``，再带两样**不是采样**的东西：
     把这一次的思考关掉、给输出封顶。采样口径仍然由服务端 argv 冻结
     （--temp 1.0 --top-p 0.95），本层一个字都不碰。为什么要带这两样 —— 实测：
     同一个 prompt 裸发（= 主请求那一份形状）要 8.47 秒、生 838 个 token，其中 2423
     字是「思考」，而生产解析只看正文，思考全被丢掉；同一份请求带上 ``enable_thinking``
     关思考后 0.9 秒、68 个 token，六个字段照样填满。判断这一步本来就是给主模型递
     一张小纸条，让它想 2400 字没有任何收益，反而把这轮的延迟顶到不可接受。
     这个开关不是新发明：speech_to_speech 侧的 ``--responses_api_disable_thinking``
     走的就是同两条 extra_body（``chat_template_kwargs.enable_thinking=false`` 与
     ``reasoning_effort``），只不过那是全局的；这里**只影响这一次判断**，
     主回复那一发仍然照旧带着思考跑。
  2. **任何失败都不许抛**：超时、连不上、JSON 坏了、字段缺了，一律回一个
     ``{"ok": False, "error": ...}``，让调用侧退回「没有判断」= V1 那份行为；
  3. **读回来的东西一律封顶**：单次响应最多读 ``MAX_RESPONSE_BYTES``，正文最多留
     ``MAX_CONTENT_CHARS``。解析别人吐的字节流不留无上限的口子。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
REGISTRY_PATH = PROJECT_ROOT / "runtime" / "llm" / "models.json"

MAX_RESPONSE_BYTES = 400_000
MAX_CONTENT_CHARS = 4_000
DEFAULT_TIMEOUT_S = 5.0
MAX_TOKENS_DEFAULT = 200

# 只关「这一次判断要不要先想一大段」，不是采样参数，也不是模型分支：一份固定形状，
# 两条都带上是因为服务端认哪条不由本层决定，认哪条的效果都是不再产生思考正文。
REASONING_OFF_KEYS = ("chat_template_kwargs", "reasoning_effort")


def reasoning_off() -> dict[str, Any]:
    return {"chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none"}


def request_body(system_text: str, user_text: str, *,
                 max_tokens: int = MAX_TOKENS_DEFAULT,
                 disable_thinking: bool = True) -> dict[str, Any]:
    """这一次判断要发的形状。单独拿出来是为了能被纯测：不带采样参数，
    只带「别先想一大段」和「输出封顶」，其余交给服务端 argv 冻结的口径。"""
    body: dict[str, Any] = {
        "messages": [{"role": "system", "content": str(system_text or "")},
                     {"role": "user", "content": str(user_text or "")}],
        "stream": False,
    }
    if disable_thinking:
        body.update(reasoning_off())
    if max_tokens and int(max_tokens) > 0:
        body["max_tokens"] = int(max_tokens)
    return body


def endpoint(timeout: float = 3.0) -> dict[str, Any]:
    """从注册表算出打哪儿。读不到注册表就明确失败，不猜端口。"""
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        listen = registry.get("listen") or {}
        port = int(listen.get("port") or 0)
        if port <= 0:
            return {"ok": False, "error": "NO_PORT_IN_REGISTRY"}
        return {"ok": True, "url": f"http://127.0.0.1:{port}/v1/chat/completions",
                "port": port}
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": f"REGISTRY_UNREADABLE:{type(exc).__name__}"}


def complete_short(system_text: str, user_text: str, *,
                   timeout_s: float = DEFAULT_TIMEOUT_S,
                   max_tokens: int = MAX_TOKENS_DEFAULT,
                   disable_thinking: bool = True) -> dict[str, Any]:
    """一发一收的非流式短请求。永远返回 dict，绝不抛。"""
    where = endpoint()
    if not where.get("ok"):
        return {"ok": False, "error": str(where.get("error")), "wall_s": 0.0}
    body: dict[str, Any] = request_body(system_text, user_text, max_tokens=max_tokens,
                                        disable_thinking=disable_thinking)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    sent = sorted(str(one) for one in body)
    request = urllib.request.Request(where["url"], data=payload,
                                     headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_s))) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
        data = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "wall_s": round(time.time() - started, 2), "url": where["url"], "sent": sent}
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": f"UNEXPECTED:{type(exc).__name__}:{str(exc)[:180]}",
                "wall_s": round(time.time() - started, 2), "url": where["url"], "sent": sent}

    wall = round(time.time() - started, 2)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"ok": False, "error": "NO_CHOICES", "wall_s": wall, "sent": sent,
                "keys": sorted(str(one) for one in (data or {}))[:8]}
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = str(message.get("content") or "")[:MAX_CONTENT_CHARS]
    return {"ok": True, "content": content.strip(), "sent": sent,
            "truncated": len(str(message.get("content") or "")) > MAX_CONTENT_CHARS,
            "reasoning_chars": len(str(message.get("reasoning_content") or "")),
            "finish": str(choice.get("finish_reason") or ""),
            "usage": data.get("usage") or {}, "timings": data.get("timings") or {},
            "model": str(data.get("model") or ""), "wall_s": wall, "url": where["url"]}


__all__ = ["endpoint", "complete_short", "request_body", "reasoning_off", "REGISTRY_PATH",
           "DEFAULT_TIMEOUT_S", "MAX_TOKENS_DEFAULT"]
