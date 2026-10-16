# -*- coding: utf-8 -*-
"""LT 轮（正式回复低思考 A/B）开工前冻结：把「今天那一发正式回复到底长什么样」钉在字节上。

为什么这一轮**必须**先冻结再动手：owner 的 A/B 前提是
「A = 现在正式回复方式 / B = 正式回复关闭隐藏思考」。可盘上有两条互相打脸的证据：

  * `runtime/llm_dual_agent_01/FREEZE_POLICY.md` L37-42 与
    `evidence/provenance_08/e2_disable_thinking_gate_01.txt` 说：s2s 客户端**默认**就发
    `chat_template_kwargs.enable_thinking=false`（那条闸门是 2026-09-19 在
    llama.cpp **b10069 + Qwen3-14B** 上量的）；
  * V2b 那三臂用的 `bench_run.call` 是**裸发**——没有 model、没有 extra_body、stream=False，
    实测带着 1932~2705 字隐藏推理。

两者不是同一发。谁说了算不能靠读注释，也不能照抄网上说的"新版 llama.cpp 改成了
`--reasoning off`"——只能：① 把**本机现役** s2s 包的映射代码逐行、连文件哈希一起落进基线，
并把 8765 的冻结命令行里出现了哪些 `--responses_api_*` 旋钮逐条记清；
② 在本机 **b11065 + Gemma4** 上实测每种发法的 `reasoning_content` 字数（下一步的
`probe_lt_knobs.py`）。这一份只写基线，零模型调用、零网络（8090 只读 /props、/v1/models）。

范围声明：本轮不改生产任何一份文件、不改 8090 启动参数、不新增模型/服务/端口。
`reply_path_watched_now` 这 15 份就是"生产正式回复路径"的字节快照，收尾时必须逐哈希零漂移。
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

OUT = ROOT / "runtime" / "human_interaction_low_thinking_baseline.json"
V2B_BASELINE = ROOT / "runtime" / "human_interaction_v2b_baseline.json"
HI_BASELINE = ROOT / "HUMAN_INTERACTION_BASELINE.json"

# 生产正式回复路径：从装配到发请求，每一份参与字节。venv 里的 s2s 包也算——
# "生产发什么参数"这件事的权威就写在那些文件里，它们若被"顺便"改了一下，
# 本轮 A 臂就不再是生产。
REPLY_WATCHED = (
    "runtime/character/context_builder.py",
    "runtime/character/rules.py",
    "runtime/character/store.py",
    "runtime/character/canon.py",
    "runtime/human_interaction/engine.py",
    "runtime/human_interaction/config.py",
    "runtime/human_interaction/context.py",
    "runtime/human_interaction/modes.py",
    "runtime/human_interaction/guard.py",
    "runtime/human_interaction/humanizer.py",
    "runtime/human_interaction/prompts/human_natural.txt",
    "runtime/human_interaction/human_interaction.json",
    "runtime/ui/app/character_admin.py",
    "runtime/ui/app/main.js",
    "runtime/llm/models.json",
    "runtime/llm/active_model.json",
    "runtime/core/providers/manifests/llm.json",
    "runtime/core/venv/Lib/site-packages/speech_to_speech/s2s_pipeline.py",
    "runtime/core/venv/Lib/site-packages/speech_to_speech/LLM/base_openai_compatible_language_model.py",
    "runtime/core/venv/Lib/site-packages/speech_to_speech/LLM/chat_completions_language_model.py",
    "runtime/core/venv/Lib/site-packages/speech_to_speech/arguments_classes/responses_api_language_model_arguments.py",
    "runtime/core/venv/Lib/site-packages/speech_to_speech/arguments_classes/chat_completions_language_model_arguments.py",
)

# 「生产发什么」这句话的证据锚点：文件 + 要逐字抓下来的片段。
S2S_ANCHORS: dict[str, tuple[str, ...]] = {
    "runtime/core/venv/Lib/site-packages/speech_to_speech/LLM/base_openai_compatible_language_model.py": (
        "self._extra_body = self._build_extra_body",
        "if reasoning_effort:",
        "if disable_thinking:",
        'return {"chat_template_kwargs": {"enable_thinking": False}}',
    ),
    "runtime/core/venv/Lib/site-packages/speech_to_speech/arguments_classes/responses_api_language_model_arguments.py": (
        "responses_api_disable_thinking: bool = field(",
        "responses_api_stream: bool = field(",
    ),
    "runtime/core/venv/Lib/site-packages/speech_to_speech/arguments_classes/chat_completions_language_model_arguments.py": (
        "responses_api_reasoning_effort: Optional[str] = field(",
    ),
    "runtime/core/venv/Lib/site-packages/speech_to_speech/LLM/chat_completions_language_model.py": (
        "extra_body=self._extra_body",
        "stream=self.stream,",
        'create_kwargs["stream_options"]',
    ),
}
PORTS = ("7860", "8010", "8090", "8765")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def grab_lines(path: Path, needles: tuple[str, ...]) -> list[dict[str, Any]]:
    """把锚点片段连**行号**一起抓下来；找不到就打 NOT_FOUND（宁可留缺口也不猜）。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict[str, Any]] = []
    for needle in needles:
        hits = [{"line": idx + 1, "text": one.strip()[:180]}
                for idx, one in enumerate(lines) if needle in one]
        out.append({"needle": needle, "hits": hits or "NOT_FOUND"})
    return out


def listeners() -> dict[str, Any]:
    """两个通道各自判一遍在场与否：psutil 的端口归属 + 直接 connect_ex。

    为什么要两路：这台机器上 psutil 的连接枚举在无权限时会**只看得见自己用户的进程**，
    单通道下"查无"会被误判成"服务死了"。
    """
    import psutil

    by_port: dict[str, list[dict[str, Any]]] = {}
    for conn in psutil.net_connections(kind="tcp"):
        if not conn.laddr or conn.status != "LISTEN":
            continue
        port = str(conn.laddr.port)
        if port not in PORTS:
            continue
        try:
            proc = psutil.Process(conn.pid)
            argv = " ".join(proc.cmdline() or [])
            created = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                    time.localtime(proc.create_time()))
            entry = {"pid": conn.pid, "name": proc.name(), "created": created,
                     "argv_sha256": hashlib.sha256(argv.encode("utf-8")).hexdigest(),
                     "argv_head": argv[:300]}
        except Exception as exc:                                    # noqa: BLE001
            entry = {"pid": conn.pid, "psutil_error": repr(exc)[:120]}
        by_port.setdefault(port, []).append(entry)
    probe = {}
    for port in PORTS:
        sock = socket.socket()
        sock.settimeout(2.0)
        rc = sock.connect_ex(("127.0.0.1", int(port)))
        sock.close()
        probe[port] = "LISTEN" if rc == 0 else "CLOSED_rc_%d" % rc
    return {"psutil_by_port": by_port, "socket_connect_probe": probe,
            "both_channels_agree": {p: ((p in by_port) == (probe[p] == "LISTEN")) for p in PORTS}}


def server_facts() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, path in (("props", "/props"), ("models", "/v1/models")):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8090" + path, timeout=10) as resp:
                out[name] = json.loads(resp.read(400_000).decode("utf-8", "replace"))
        except Exception as exc:                                    # noqa: BLE001
            out[name] = {"error": repr(exc)[:200]}
    props = out.get("props") or {}
    out["summary"] = {k: props.get(k) for k in ("build_info", "model_path", "model_alias", "n_ctx")}
    out["model_ids"] = [one.get("id") for one in (out.get("models") or {}).get("data", [])]
    return out


def find_command_line(node: Any, path: str = "") -> list[tuple[str, str]]:
    """在基线 JSON 里递归找真正的 8765 命令行（键在 `voice_chain_baseline` 下面，
    顶层取是取不到的——取空串会把"哪些旋钮没出现"整段判错，所以这里按内容找而不是按键名猜）。"""
    hits: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            hits += find_command_line(value, path + "/" + str(key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            hits += find_command_line(value, "%s[%d]" % (path, idx))
    elif isinstance(node, str) and "--responses_api_base_url" in node:
        hits.append((path, node))
    return hits


def launch_flags() -> dict[str, Any]:
    """8765 的冻结命令行里，哪些旋钮出现了、哪些没出现（没出现＝吃 s2s 默认值）。"""
    cmd = ""
    pointer = None
    try:
        tree = json.loads(HI_BASELINE.read_text(encoding="utf-8"))
        found = find_command_line(tree)
        pointer, cmd = found[0] if found else (None, "")
    except Exception as exc:                                        # noqa: BLE001
        return {"error": repr(exc)[:200], "source": rel(HI_BASELINE)}
    v2b = {}
    try:
        for one in (json.loads(V2B_BASELINE.read_text(encoding="utf-8"))
                    .get("listeners", {}).get("8765") or []):
            v2b = {k: one.get(k) for k in ("pid", "created", "argv_sha256", "argv_head")}
    except Exception as exc:                                        # noqa: BLE001
        v2b = {"error": repr(exc)[:120]}
    tokens = cmd.split()
    want = ("--responses_api_disable_thinking", "--responses_api_reasoning_effort",
            "--responses_api_stream", "--responses_api_base_url", "--model_name",
            "--llm_backend", "--compact_history", "--chat_size", "--max_tokens", "--reasoning")
    return {
        "source": rel(HI_BASELINE),
        "json_pointer_where_found": pointer,
        "command_line_sha256": hashlib.sha256(cmd.encode("utf-8")).hexdigest(),
        "command_line_chars": len(cmd),
        "present": {one: (one in tokens) for one in want},
        "model_name_value": (tokens[tokens.index("--model_name") + 1]
                             if "--model_name" in tokens else None),
        "flags_absent_therefore_default": [one for one in want if one not in tokens],
        "last_known_8765_at_v2b_freeze": v2b,
    }


def main() -> int:
    import list_v2_files
    from runtime.human_interaction import config

    watched: dict[str, Any] = {}
    for name in REPLY_WATCHED:
        path = ROOT / name
        watched[name] = ({"sha256": sha(path), "bytes": path.stat().st_size}
                         if path.exists() else "MISSING")
    anchors: dict[str, Any] = {}
    for name, needles in S2S_ANCHORS.items():
        path = ROOT / name
        anchors[name] = grab_lines(path, needles) if path.exists() else "MISSING"

    payload = {
        "task": "HUMAN_INTERACTION_LAYER_01/LT_FREEZE",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "why": ("正式回复低思考 A/B：先固定「今天这一发的形状」，"
                "再谈关掉隐藏思考能省多少"),
        "listeners": listeners(),
        "llama_8090": server_facts(),
        "s2s_launch_flags": launch_flags(),
        "s2s_request_mapping_lines": anchors,
        "reply_path_watched_now": watched,
        "layer_source_files": list_v2_files.scan_layer(),
        "layer_effective_config": config.describe_config(),
        "layer_config_file_sha": sha(ROOT / "runtime/human_interaction/human_interaction.json"),
        "v2b_baseline_left_untouched": rel(V2B_BASELINE),
        "arms_planned": {
            "A": "生产今天这一发（model + stream + extra_body 按 s2s 默认映射）",
            "B": "本机 b11065 实测真能关掉 reasoning 的那一发（由 probe_lt_knobs.py 定）",
            "C": "显式带思考（裸发，不发任何关闭标志）——给 owner 那条 ≥50% 判据当分母",
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    probe = payload["listeners"]["socket_connect_probe"]
    print("在场（socket 通道）:", probe)
    print("两通道一致:", payload["listeners"]["both_channels_agree"])
    print("8090 build:", payload["llama_8090"]["summary"].get("build_info"),
          "model:", str(payload["llama_8090"]["summary"].get("model_alias"))[-42:])
    flags = payload["s2s_launch_flags"]
    print("8765 冻结命令行里出现的旋钮:",
          {k: v for k, v in (flags.get("present") or {}).items() if v})
    print("没出现=吃默认:", flags.get("flags_absent_therefore_default"))
    missing = [k for k, v in watched.items() if v == "MISSING"]
    notfound = [f"{f}:{one['needle']}" for f, rows in anchors.items() if rows != "MISSING"
                for one in rows if one["hits"] == "NOT_FOUND"]
    print("回复路径缺件:", missing or "无")
    print("锚点抓不到:", notfound or "无")
    print("LT_BASELINE", OUT)
    return 1 if (missing or notfound) else 0


if __name__ == "__main__":
    raise SystemExit(main())
