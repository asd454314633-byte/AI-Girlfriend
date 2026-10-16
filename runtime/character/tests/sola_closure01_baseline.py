# -*- coding: utf-8 -*-
"""baseline — 任务 §3「冻结现场」：只读采集，产出 baseline.json（§22 清单里的 baseline.json）。

采什么（逐条对任务 §3）：
  * 时间 / Windows PID / 进程创建时间 / 7860·8090·8765·8010 端口监听
  * GPU utilization、VRAM used/free、RAM available/used、swap
  * 当前 active character、human_interaction 配置、Sola 配置、voice_minimal 配置、turn_judge 配置
  * 关键文件 SHA256：context_builder.py、contracts.py、Sola 原版冻结件、Voice Minimal 冻结件、
    Sola 配置文件、human_interaction.json

本脚本不写任何生产状态；只往 tests/out/sola_unattended_closure_01/ 落证据。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sola_closure01_lib as L  # noqa: E402

HASH_FILES = [
    "runtime/character/context_builder.py",
    "runtime/core/providers/contracts.py",
    "runtime/character/sola_v2_ember/source/Sola V2 Ember.json",
    "runtime/character/sola_v2_ember/sola_v2_ember_voice_minimal.txt",
    "runtime/character/sola_v2_ember/sola_v2_ember.json",
    "runtime/character/sola_v2_ember/voice_minimal_edits.json",
    "runtime/character/sola_v2_ember/voice_minimal.py",
    "runtime/character/sola_v2_ember/adapter.py",
    "runtime/character/sola_v2_ember/__init__.py",
    "runtime/human_interaction/human_interaction.json",
    "runtime/character/data/active_character.json",
    "runtime/llm/models.json",
    "runtime/llm/active_model.json",
    "runtime/core/providers/manifests/llm.json",
    "runtime/windows/active_tts_backend.txt",
    "runtime/windows/avatar_runtime_state.json",
    "runtime/windows/service_pids.json",
]


def main() -> int:
    listeners = L.listeners()
    hi = L.read_json_file("runtime/human_interaction/human_interaction.json")
    sola_cfg = L.read_json_file("runtime/character/sola_v2_ember/sola_v2_ember.json")
    pointer = L.read_json_file("runtime/character/data/active_character.json")
    active_model = L.read_json_file("runtime/llm/active_model.json")
    models = L.read_json_file("runtime/llm/models.json")

    out = {
        "task_id": L.TASK_ID,
        "stage": "3_freeze_scene",
        "collected_at_local": L.now_local(),
        "collected_at_utc": L.now_utc_z(),
        "read_only": True,
        "services": {
            str(port): {
                "listening": bool(rows),
                "listeners": rows,
                "tcp_connect_127_0_0_1": L.port_open(port),
            } for port, rows in listeners.items()
        },
        "gpu_pre": L.gpu_snapshot(),
        "ram_pre": L.ram_snapshot(),
        "active_character_pointer": pointer,
        "llm": {
            "active_model_file": active_model,
            "llama_server": (models or {}).get("llama_server") if isinstance(models, dict) else None,
            "launch_params_frozen": (models or {}).get("launch_params_frozen") if isinstance(models, dict) else None,
            "model_paths": {k: v.get("path") for k, v in (models or {}).get("models", {}).items()}
            if isinstance(models, dict) else None,
        },
        "sola_config": {
            "sola_v2_ember_enabled": (sola_cfg or {}).get("sola_v2_ember_enabled") if isinstance(sola_cfg, dict) else None,
            "voice_minimal_enabled": (sola_cfg or {}).get("voice_minimal_enabled") if isinstance(sola_cfg, dict) else None,
            "user_label": (sola_cfg or {}).get("user_label") if isinstance(sola_cfg, dict) else None,
            "char_label_fallback": (sola_cfg or {}).get("char_label_fallback") if isinstance(sola_cfg, dict) else None,
            "ember_source": (sola_cfg or {}).get("ember_source") if isinstance(sola_cfg, dict) else None,
            "voice_minimal_spec": (sola_cfg or {}).get("voice_minimal_spec") if isinstance(sola_cfg, dict) else None,
        },
        "human_interaction_config": {
            "feature_enabled": (hi or {}).get("feature_enabled") if isinstance(hi, dict) else None,
            "turn_judge_enabled": (hi or {}).get("turn_judge_enabled") if isinstance(hi, dict) else None,
            "interaction_mode": (hi or {}).get("interaction_mode") if isinstance(hi, dict) else None,
            "revise_enabled": (hi or {}).get("revise_enabled") if isinstance(hi, dict) else None,
        },
        "file_sha256": [L.file_fact(rel) for rel in HASH_FILES],
    }
    path = L.write_json("baseline.json", out)
    print(f"WROTE {path}")
    for port, rows in L.listeners().items():
        print(f":{port} " + ("; ".join(
            f"pid={r['pid']} created={r.get('created', '-')}" for r in rows) or "no listener"))
    print("sola =", out["sola_config"]["sola_v2_ember_enabled"],
          "voice_minimal =", out["sola_config"]["voice_minimal_enabled"])
    print("hi feature =", out["human_interaction_config"]["feature_enabled"],
          "turn_judge =", out["human_interaction_config"]["turn_judge_enabled"])
    print("active_character =", (pointer or {}).get("character_id") if isinstance(pointer, dict) else pointer)
    print("ram =", out["ram_pre"])
    print("gpu =", out["gpu_pre"].get("gpus") or out["gpu_pre"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
