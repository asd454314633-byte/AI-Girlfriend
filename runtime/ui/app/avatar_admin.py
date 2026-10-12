# -*- coding: utf-8 -*-
"""数字人（人物形象）的本地管理面 —— 给 :7860 那套界面用的一组接口。

这一层只做四件事，逻辑一律不重写：

  1. 把「现在有哪些人物、正在用哪一个」这个事实收在一张注册表里
     （``runtime/windows/avatars.json``），注册表首次由扫描素材目录生成；
  2. 把「用一段视频做出一个新人物」转交给官方那台服务自己的制作接口，
     **不自己造生成器**；
  3. 换人是重启级动作，由 PowerShell 切换脚本执行（八步状态机 + 唯一回滚），
     这里只负责调度、超时兜底与状态回显；
  4. 出错时给一句中文，绝不把 Python 的异常文字丢到浏览器里。

三条边界：

  * **不认得任何具体人物**：人物标识只以数据形态出现（注册表条目、指针值、
    命令行里的占位符值），代码里没有一个 ``if 某个人物`` 的分支；
  * **不自己管进程**：不起进程、不杀进程、不写 ``service_pids.json``；
    换人一律交给 :8010 的唯一生命周期入口 ``runtime/windows/Avatar-Supervisor.ps1``
    的 ``-Action Switch``（本模块实际调的是同目录下的
    ``runtime/windows/Switch-Girlfriend-Avatar.ps1``，它已经不再自带启停逻辑；
    ``scripts/Switch-Girlfriend-Avatar.ps1`` 自 2026-09-25 起也只是转发到 Supervisor
    的薄壳，两份实现都不再自己起停进程）；
  * 删除仅由独立资产层执行可恢复移除；内置与使用中的人物受保护。
    不删用户原视频、不动官方暂存目录，启动/切换仍由原入口负责。

不装任何新依赖：HTTP 走 ``httpx``，进程存活走同目录的 ``pid_liveness``（只读句柄判活，
**不用** ``os.kill`` —— 在 Windows 上它对跨 console 的目标等同 ``TerminateProcess(h, 0)``，
理由与实测见 ``_pid_alive``），没有 psutil。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, File, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR.parents[1]
PROJECT_ROOT = RUNTIME_DIR.parent
CORE_DIR = RUNTIME_DIR / "core"

# 与 character_admin.py:32-51 同一套路径引导：本文件由 server.py 以「同目录直接
# import」的方式加载，但 engine 在仓库根下面，所以路径要自己补上。
for _p in (str(PROJECT_ROOT), str(CORE_DIR), str(RUNTIME_DIR), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from providers import registry  # noqa: E402  (上面的路径引导必须先跑)
import pid_liveness  # noqa: E402  (同目录：判活只用只读句柄，见该模块开头那段实测)

# 文案净化复用既有的那张词表；读不到时就地用一份同内容的兜底，绝不因为少一个
# 模块而让整个界面起不来。
try:
    from runtime.character import rules as _character_rules

    _RULES_BANNED: tuple[str, ...] = tuple(getattr(_character_rules, "BANNED_UI_WORDS", ()))
except Exception:  # noqa: BLE001  兜底：不因可选依赖而塌
    _RULES_BANNED = (
        "JSON", "json", "RAG", "chunk", "Chunk", "CHUNK", "embedding", "Embedding",
        "BM25", "bm25", "数据库", "prompt", "Prompt", "PROMPT", "token", "Token",
        "TOKEN", "sqlite", "SQL", "索引", "向量", "语料", "分词",
    )

router = APIRouter()

# ---------------------------------------------------------------------------
# 存放位置（全部由项目根推导，只有这一处常量，翻转成本为零）
# ---------------------------------------------------------------------------

KIND = "AVATAR"
AVATAR_DATA_DIR = (RUNTIME_DIR / "avatar" / "app" / "data" / "avatars").resolve()
STATE_DIR = RUNTIME_DIR / "windows"
AVATARS_JSON = STATE_DIR / "avatars.json"
# 选择类状态单独落一份指针文件，与 active_tts_backend.txt 逐字同构（一行 ASCII、
# 不带 BOM）。唯一写者是 PowerShell 切换脚本 —— 本模块对它只读。这样登记表
# （条目增删、名称改动）就只剩 Python 一个写者，与 test_state_single_source.py
# 立下的「选择类状态单一写者」纪律一致。
POINTER_FILE = STATE_DIR / "active_avatar.txt"
SWITCH_STATE = STATE_DIR / "avatar_switch_state.json"
UPLOAD_DIR = STATE_DIR / "avatar_uploads"
SWITCH_SCRIPT = STATE_DIR / "Switch-Girlfriend-Avatar.ps1"
LAUNCH_LIB = PROJECT_ROOT / "scripts" / "Avatar-Launch-Common.ps1"
RUNTIME_COMMON = PROJECT_ROOT / "scripts" / "Runtime-Common.ps1"
# :8010 的唯一生命周期入口（启动 / 停止 / 读状态）。本模块只调它，不再自己
# dot-source 启动体：以前 restart 走的是「就地拼一条 dot-source 命令」，那份
# 知识（谁在守端口、启动器 re-exec 成了谁）在四处各写了一遍，互相不认。
AVATAR_SUPERVISOR = STATE_DIR / "Avatar-Supervisor.ps1"
# 入口落的那份状态文件。本模块对它**只读**（写者只有 Avatar-Supervisor.ps1），
# 与指针文件同一套纪律：一份事实一个写者。字段名与
# runtime/avatar/app/server/routes.py 的 _process_ownership() 读的是同一套。
AVATAR_RUNTIME_STATE = STATE_DIR / "avatar_runtime_state.json"
POWERSHELL = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "pwsh.exe")
# 生命周期入口固定用 5.1，和计划任务包装器（GirlfriendAvatar8010_Manual 的
# Action）同一个引擎：owner 身份判定、状态文件写法都只在这一种下实测过，
# 两种引擎混用等于把「谁 owns 8010」再问一遍。
SYSTEM_POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
LOG_DIR = PROJECT_ROOT / "logs" / "runtime"

PID_FILE = STATE_DIR / "service_pids.json"
PID_KEY = "avatar"
THUMB_REL = "full_imgs/00000000.png"

MAX_BYTES = 200 * 1024 * 1024
MIN_SECONDS = 3.0
# 主限制是时长不是文件大小：实测产出约 0.589 MB/帧（324 MB / 550 帧），帧数守恒
# 且与输入码率基本无关。20 s = 500 帧 ≈ 295 MB，低于本机 22 s / 324 MB 的既有
# 稳定基线。MAX_BYTES 只是粗筛 —— 1.4 MB 的输入能产出约 150 MB（1:107）。
MAX_SECONDS = 20.0
# Q4 的 mkv / webm 结论出来之前，只接受 mp4。这是全模块唯一的可变量。
ACCEPT_EXT: tuple[str, ...] = (".mp4",)

PROBE_TIMEOUT_S = 1.0
HTTP_TIMEOUT_S = 10.0
UPLOAD_TIMEOUT_S = 300.0
FFPROBE_TIMEOUT_S = 30
SWITCH_TIMEOUT_S = 900

STATE_READY = "ready"
STATE_MAKING = "making"
STATE_FAILED = "failed"


# ---------------------------------------------------------------------------
# 统一回包：{ok, reason?, 说明}，给人看的字段一定过一遍「不许露技术词」
# ---------------------------------------------------------------------------

_MESSAGE_KEYS = ("说明", "理由", "原因", "错误", "状态", "备注", "详情", "提示", "明细")

# PRD §9.1 全表 + rules.BANNED_UI_WORDS。只用来洗回包，不用来拒绝请求。
_BANNED: tuple[str, ...] = (
    "avatar_id", "avatar", "wav2lip", "musetalk", "PID", "进程", "端口", "manifest",
    "provider", "registry", "task_id", "coords.pkl", "full_imgs", "face_imgs", "JSON",
    "8010", "sessionid", "WebRTC", "img_size", "pads", "nosmooth",
) + _RULES_BANNED


def banned_words() -> list[str]:
    """给闸门页读的那张表。界面文案里出现次数必须为 0。"""
    return list(_BANNED)


def _find_banned(*texts: Any) -> list[str]:
    hits: list[str] = []
    for text in texts:
        body = str(text or "")
        for word in _BANNED:
            if word and word in body and word not in hits:
                hits.append(word)
    return hits


def _plain(text: Any, fallback: str = "这一步没做成，请再试一次。") -> str:
    """把一句要给用户看的话洗干净：露了内部名词就换成一句人话，不就地打码。"""
    body = str(text or "").strip()
    if not body:
        return fallback
    if _find_banned(body):
        return fallback
    return body


def _tidy(payload: dict[str, Any]) -> dict[str, Any]:
    for key in _MESSAGE_KEYS:
        if key in payload:
            payload[key] = _plain(payload.get(key))
    return payload


def _fail(reason: str, note: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "reason": str(reason or "FAILED"), "说明": _plain(note)}
    out.update(extra)
    return _tidy(out)


def _done(payload: Any, *, note: str = "") -> dict[str, Any]:
    """把各处返回的不同形状统一成回包：字典照搬，其它包一层。"""
    if isinstance(payload, dict):
        out = dict(payload)
    elif payload is None:
        out = {}
    else:
        out = {"结果": payload}
    out.setdefault("ok", True)
    if note:
        out["说明"] = note
    if out.get("ok") is not True and "reason" not in out:
        out["reason"] = "REFUSED"
    return _tidy(out)


def _guard(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """任何异常都变成一句中文 + 一个原因码；浏览器永远看不到 Python 的报错原文。"""
    try:
        result = action()
    except PermissionError as exc:
        return _fail("NOT_ALLOWED", "这件事现在不能这么做。", 异常类型=type(exc).__name__)
    except FileNotFoundError as exc:
        return _fail("NOT_FOUND", "没找到那个文件，麻烦再确认一下位置。",
                     异常类型=type(exc).__name__)
    except ValueError as exc:
        code = str(exc) or "BAD_REQUEST"
        return _fail(code, _plain(code, "这段话我读不懂，请重新填一下。"),
                     异常类型=type(exc).__name__)
    except Exception as exc:                       # noqa: BLE001  兜底：不外泄
        return _fail("SERVER_SIDE_FAILURE", "服务器这边没做成，详情记在日志里了。",
                     异常类型=type(exc).__name__, 内部=f"{type(exc).__name__}: {exc}"[:160])
    if not isinstance(result, dict):
        return _done(result)
    # UI_FIX_01：与 _done 同一信封约定——正常回包必须带 ok:true，否则前端
    # avatar_panel.js 的 refresh() 一律按「联系不上」处理（面板永远开天窗）。
    result.setdefault("ok", True)
    return _tidy(result)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return _now_iso()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """照 voice_models_admin.py:84：唯一临时名 + os.replace。

    Windows 上固定 ``*.tmp`` 只要被一个残留句柄占住，之后每一次写都会永久失败，
    而界面还一直显示成功 —— 所以临时名必须每次都不同。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _log(name: str, message: str) -> None:
    """与 Runtime-Common.ps1:585 的 Write-RuntimeLog 逐字一致的时间前缀。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{stamp}] {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / name, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _human_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def _human_duration(seconds: float) -> str:
    total = max(0, int(float(seconds or 0)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _safe_token(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", text):
        return ""
    return text


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name or ""))[:120] or "video"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _placeholder_png_bytes() -> bytes:
    """1×1 灰色 PNG：缩略图缺失时的占位，不报错、不空白。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)     # 灰度、8 位
    raw = b"\x00" + bytes([0x9A])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


_PLACEHOLDER_PNG = _placeholder_png_bytes()


# ---------------------------------------------------------------------------
# manifest 驱动的事实：端口 / flag 名 / 型号 全部从清单读，代码里零字面量
# ---------------------------------------------------------------------------

def _avatar_provider_id() -> str:
    active = registry.active_id(KIND)[0] or ""
    if active and registry.get(KIND, active):
        return active
    rows = registry.providers(KIND, enabled_only=True)
    return str(rows[0].get("id") or "") if rows else ""


def _avatar_spec() -> dict[str, Any]:
    return registry.get(KIND, _avatar_provider_id()) or {}


def _avatar_base_url() -> str:
    plan = registry.health_plan(KIND, _avatar_provider_id())
    return str(plan.get("base_url") or "").rstrip("/")


def _avatar_port() -> int:
    parsed = urlparse(_avatar_base_url())
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _avatar_host() -> str:
    return urlparse(_avatar_base_url()).hostname or "127.0.0.1"


def _cli_args() -> list[str]:
    launch = _avatar_spec().get("launch") or {}
    return [str(item) for item in (launch.get("cli_args") or [])]


def _avatar_id_flag() -> str:
    """清单里值为 ``{AVATAR_ID}`` 的那一项的前一项，就是选人用的那个开关名。"""
    args = _cli_args()
    for index, item in enumerate(args):
        if item == "{AVATAR_ID}" and index > 0:
            return args[index - 1]
    return ""


def _avatar_port_flag() -> str:
    """清单里值等于自身监听端口的那一项的前一项，就是端口开关名。"""
    args = _cli_args()
    port = str(_avatar_port())
    for index, item in enumerate(args):
        if item == port and index > 0:
            return args[index - 1]
    return ""


def _avatar_model() -> str:
    args = _cli_args()
    for index, item in enumerate(args):
        if item == "--model" and index + 1 < len(args):
            return args[index + 1]
    return ""


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_registry_cache: dict[str, Any] | None = None
_registry_lock = threading.Lock()


def _handle_of(avatar_id: str) -> str:
    """对外不透明句柄：``v`` + sha256(内部标识)[:10]。内部标识绝不进 URL。"""
    return "v" + hashlib.sha256(str(avatar_id).encode("utf-8")).hexdigest()[:10]


def _probe_dir(avatar_dir: Path) -> dict[str, Any]:
    """三件套（画面帧 / 人脸帧 / 对齐数据）的实测事实。读不到就记 0，不抛。"""
    out: dict[str, Any] = {"frame_count": 0, "coords_bytes": 0, "coords_sha256": "",
                           "三件套齐全": False}
    try:
        full = avatar_dir / "full_imgs"
        if full.is_dir():
            out["frame_count"] = sum(1 for item in full.iterdir() if item.is_file())
    except OSError:
        out["frame_count"] = 0
    faces_ok = False
    try:
        face = avatar_dir / "face_imgs"
        faces_ok = face.is_dir() and any(face.iterdir())
    except OSError:
        faces_ok = False
    try:
        coords = avatar_dir / "coords.pkl"
        if coords.is_file():
            data = coords.read_bytes()
            out["coords_bytes"] = len(data)
            out["coords_sha256"] = hashlib.sha256(data).hexdigest()
    except OSError:
        pass
    out["三件套齐全"] = bool(out["frame_count"] > 0 and faces_ok and out["coords_bytes"] > 0)
    return out


def scan_and_seed_registry() -> dict[str, Any]:
    """首次扫描素材目录生成注册表。已存在的文件一律不动。"""
    entries: list[dict[str, Any]] = []
    if AVATAR_DATA_DIR.is_dir():
        dirs = [item for item in AVATAR_DATA_DIR.iterdir() if item.is_dir()]
        dirs.sort(key=lambda item: item.stat().st_mtime)
        model_type = _avatar_model()
        for index, directory in enumerate(dirs, start=1):
            probe = _probe_dir(directory)
            # 首个（最早改动的）叫「默认人物」，其余「备用人物 2/3…」。中性名，
            # 用户之后可以在面板里改；不写任何具体人物的名字进代码。
            display = "默认人物" if index == 1 else f"备用人物 {index}"
            entries.append({
                "id": directory.name,
                "编号": _handle_of(directory.name),
                "display_name": display,
                "model_type": model_type,
                "dir": str(directory),
                "thumb": THUMB_REL,
                "frame_count": int(probe["frame_count"]),
                "coords_bytes": int(probe["coords_bytes"]),
                "coords_sha256": str(probe["coords_sha256"]),
                "source_video": None,
                "created_at": _iso_from_timestamp(directory.stat().st_ctime),
                "state": STATE_READY if probe["三件套齐全"] else STATE_FAILED,
                "builtin": True,
            })
    # 指针首次取值：以服务实际在用的那个为准，取不到才回落到第一条。
    active = _runtime_avatar_id() or ""
    known = [str(entry["id"]) for entry in entries]
    if active not in known:
        active = known[0] if known else ""
    payload = {"version": 1, "updated_at": _now_iso(), "avatars": entries}
    _write_json_atomic(AVATARS_JSON, payload)
    if active and not _read_pointer():
        _write_pointer(active)
    return payload


def _read_registry() -> dict[str, Any]:
    global _registry_cache
    with _registry_lock:
        if _registry_cache is not None:
            return _registry_cache
        data = _read_json(AVATARS_JSON)
        if isinstance(data.get("avatars"), list) and data.get("avatars"):
            _registry_cache = data
            return data
        _registry_cache = scan_and_seed_registry()
        return _registry_cache


def _write_registry(payload: dict[str, Any]) -> dict[str, Any]:
    global _registry_cache
    with _registry_lock:
        # A slow panel refresh/completed-making callback may hold an older
        # snapshot. Preserve committed deletions instead of resurrecting it.
        persisted = _read_json(AVATARS_JSON)
        deleted = {str(item.get("id")): item for item in persisted.get("deleted_avatars", [])}
        deleted.update({str(item.get("id")): item for item in payload.get("deleted_avatars", [])})
        if deleted:
            payload["deleted_avatars"] = list(deleted.values())
            payload["avatars"] = [item for item in payload.get("avatars", []) if str(item.get("id")) not in deleted]
        payload["updated_at"] = _now_iso()
        _write_json_atomic(AVATARS_JSON, payload)
        _registry_cache = payload
        return payload


def _read_pointer() -> str:
    """「这里选的」落在独立指针文件里（一行 ASCII、不带 BOM），本模块只读。

    读不到 / 读坏了都返回空串 —— 由调用方决定回落（与 active_tts_backend.txt
    缺失时 TTS 走 default_selection 同一套语义），绝不在 Python 里破例写一次。
    """
    try:
        return POINTER_FILE.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


def _write_pointer(avatar_id: str) -> None:
    """唯一调用方是 PowerShell 切换脚本。这里只为「种子初始化」兜底一次。"""
    POINTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POINTER_FILE.with_name(
        f"{POINTER_FILE.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(f"{avatar_id}\n", encoding="ascii")
        os.replace(tmp, POINTER_FILE)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _entries() -> list[dict[str, Any]]:
    data = _read_registry()
    return [item for item in (data.get("avatars") or []) if isinstance(item, dict)]


def _entry_by_id(avatar_id: str) -> dict[str, Any] | None:
    for entry in _entries():
        if str(entry.get("id")) == str(avatar_id):
            return entry
    return None


def _by_handle(编号: str) -> dict[str, Any] | None:
    token = str(编号 or "").strip()
    if not token:
        return None
    for entry in _entries():
        if str(entry.get("编号")) == token:
            return entry
    return None


def _new_avatar_id(display_name: str, source_sha256: str, created_at: str) -> str:
    """内部标识永不碰撞；目录已存在就 -2 / -3 递增，绝不复用既有目录名。"""
    seed = f"{display_name}|{source_sha256}|{created_at}"
    base = "av_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    candidate, suffix = base, 2
    while (AVATAR_DATA_DIR / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _suggest_name(base: str) -> str:
    """重名时给一个可用建议名：原名 → 原名 2 → 原名 3。"""
    taken = {str(entry.get("display_name")) for entry in _entries()}
    if base not in taken:
        return base
    suffix = 2
    while f"{base} {suffix}" in taken:
        suffix += 1
    return f"{base} {suffix}"


def assert_not_builtin(编号: str) -> None:
    """内置护栏：本次改造之前就存在的条目不许动。本轮没有删除入口，为将来预留。"""
    entry = _by_handle(编号)
    if entry and bool(entry.get("builtin")):
        raise ValueError("BUILTIN_PROTECTED")


def _refresh_entry(entry: dict[str, Any]) -> bool:
    """把素材事实刷回内存条目；变了才落盘，返回是否变化。"""
    directory = Path(str(entry.get("dir") or ""))
    if not directory.is_dir():
        return False
    probe = _probe_dir(directory)
    changed = (int(entry.get("frame_count") or 0) != int(probe["frame_count"]) or
               str(entry.get("coords_sha256") or "") != str(probe["coords_sha256"]))
    if changed:
        entry["frame_count"] = int(probe["frame_count"])
        entry["coords_bytes"] = int(probe["coords_bytes"])
        entry["coords_sha256"] = str(probe["coords_sha256"])
    return changed


def _public(entry: dict[str, Any], active_id: str) -> dict[str, Any]:
    """给浏览器的一份卡片。内部标识与磁盘路径一律不出门。"""
    handle = str(entry.get("编号") or "")
    source = entry.get("source_video")
    source_name = str(source.get("name") or "") if isinstance(source, dict) else ""
    return {
        "编号": handle,
        "名称": str(entry.get("display_name") or "没名字"),
        "缩略图地址": f"/api/avatar/thumb/{handle}?v={int(entry.get('frame_count') or 0)}",
        "来源视频名": source_name or "来源不明",
        "做成时间": str(entry.get("created_at") or "")[:16].replace("T", " "),
        "帧数": int(entry.get("frame_count") or 0),
        "正在使用": str(entry.get("id")) == str(active_id),
        "能用": str(entry.get("state")) == STATE_READY,
        "内置人物": bool(entry.get("builtin", True)),
        "允许删除": entry.get("builtin") is False and str(entry.get("id")) != str(active_id)
                     and str(entry.get("state")) != STATE_MAKING,
    }


# ---------------------------------------------------------------------------
# 服务实际状态（谁在真正服务，只有进程命令行能回答）
# ---------------------------------------------------------------------------

def _service_pids() -> dict[str, Any]:
    return _read_json(PID_FILE)


def _tracked_pid() -> int:
    try:
        return int(_service_pids().get(PID_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    """问一句「还在不在」，而这一问**绝不能**把对方问死。

    这里以前写的是 ``os.kill(pid, 0)`` —— 教科书上的判活写法，但在 Windows + CPython 上
    它是一把枪：``CTRL_C_EVENT`` 的数值就是 0，于是 sig=0 先进 console 分支调
    ``GenerateConsoleCtrlEvent(0, pid)``；本面板（:7860）与数字人服务（:8010 的
    listener/launcher）**从来不在同一个 console 里**，这一调用必然失败，CPython 接着掉进
    ``OpenProcess(PROCESS_TERMINATE)`` + ``TerminateProcess(handle, 0)``，目标当场以退出码
    ``0x00000000`` 消失：不跑 atexit、faulthandler 留不下栈、没有 WER 事件、也不产生任何
    新进程，而这边只剩一条被 ``except Exception`` 吞掉的异常 —— 于是每一次刷新数字人面板
    都是一发看不见的终止。实测矩阵在
    ``runtime/debug/pid_probe/lab3_cross_console_kill0.py``（R1/R2/R3/R3b 跨 console 全部
    打成 0x00000000，R4 同 console 才无害，R5 ``SIGTERM`` 给 0x0000000F 作正向对照）。

    现在只申请 ``SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION`` 这一个只读句柄，看
    ``GetExitCodeProcess`` 是否还是 ``STILL_ACTIVE``。语义保持不变：**问不到就当不在**
    （宁可把还在的人说成不在，也绝不把不在的人说成在），差别只在于"问不到"从此不再等于
    "被我杀了"。判活的实现只有 ``pid_liveness`` 这一份，别在这里再写一份。
    """
    return pid_liveness.pid_alive(pid)


def _runtime_avatar_id() -> str | None:
    """从登记的命令行里把「现在真正用的是哪一个」抠出来。"""
    owners = _service_pids().get("owners") or {}
    owner = owners.get(PID_KEY)
    command = ""
    if isinstance(owner, dict):
        command = str(owner.get("commandLine") or "")
    flag = _avatar_id_flag()
    if command and flag:
        match = re.search(re.escape(flag) + r'(?:\s+|=)([^\s"]+)', command)
        if match:
            return match.group(1)
    # 命令行抠不出来（owners 条目过期、启动器已经 re-exec 走了）时，退一步认
    # supervisor 记的那一份 —— 但它必须**确实还在服务**才作数：服务停了的时候
    # 状态文件里还留着上一个 id，拿它去回答「现在用的是谁」就是把「刚才用过」
    # 说成「正在用」，那正是「同步一下」最不该做的事。
    state = _runtime_state()
    if state and _port_open():
        recorded = str(state.get("avatar_id") or "").strip()
        if recorded and str(state.get("state") or "").upper() in ("RUNNING", "STARTING"):
            return recorded
    return None


def _runtime_state() -> dict[str, Any]:
    """Avatar-Supervisor.ps1 落的那份状态文件，只读。

    读不到 / 解析不了就是空表 —— 那是「第一次运行」或「有人手删了」，不是错误：
    调用方一律退回老探针，界面不能因为一个 JSON 读失败就把数字人说死。
    """
    return _read_json(AVATAR_RUNTIME_STATE)


def _state_int(state: dict[str, Any], key: str) -> int:
    try:
        return int(state.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _port_open() -> bool:
    """一次纯端口探针：不问 pid，只看 8010 这一刻接不接受连接。"""
    try:
        with socket.create_connection((_avatar_host(), _avatar_port()), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False
    except Exception:                              # noqa: BLE001
        return False


def _service_alive() -> bool:
    """端口通不通这件事仍然自己打一次 TCP —— 状态文件是「谁 owns 8010」的权威，
    不是「现在这一刻还通不通」的权威。变的只有**该拿哪个 pid 去判活**：老代码取
    ``service_pids.json['avatar']``，那是 venv 启动器，而真正守端口的是它 re-exec
    出来的那个子进程；启动器一退出这个 pid 就永远过期，界面于是把还在服务的人
    显示成「没在运行」。现在优先用状态文件的 listener_pid，文件不在才退回老键。
    """
    state = _runtime_state()
    pid = _state_int(state, "listener_pid") or _state_int(state, "launcher_pid")
    if pid <= 0:
        pid = _tracked_pid()
    if pid > 0 and not _pid_alive(pid):
        return False
    return _port_open()


def _name_of(avatar_id: str | None) -> str:
    if not avatar_id:
        return ""
    entry = _entry_by_id(avatar_id)
    return str(entry.get("display_name") or "") if entry else ""


def _consistency() -> dict[str, Any]:
    """「这里选的」与「实际在用的」是否一致。不一致由用户显式点「同步一下」解决。"""
    runtime = _runtime_avatar_id()
    active = _read_pointer()
    return {
        "一致": bool(not runtime or runtime == active),
        "实际在用的名称": _name_of(runtime),
        "这里选的名称": _name_of(active),
    }


# ---------------------------------------------------------------------------
# 视频元数据
# ---------------------------------------------------------------------------

def _ffprobe(path: Path) -> dict[str, Any] | None:
    """读不出就返回 None —— 读不出不等于不能用，判定权交给制作流程。"""
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
             "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FFPROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        return None
    streams = payload.get("streams") or []
    video = next((item for item in streams
                  if isinstance(item, dict) and str(item.get("codec_type")) == "video"), None)
    if not isinstance(video, dict):
        return None
    duration = None
    for source in (payload.get("format") or {}, video):
        try:
            if source.get("duration"):
                duration = float(source["duration"])
                break
        except (TypeError, ValueError):
            continue
    fps = 0.0
    raw_fps = str(video.get("r_frame_rate") or "")
    if "/" in raw_fps:
        try:
            numerator, denominator = raw_fps.split("/", 1)
            if float(denominator):
                fps = float(numerator) / float(denominator)
        except (TypeError, ValueError):
            fps = 0.0
    return {
        "duration_s": float(duration or 0.0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
    }


# ---------------------------------------------------------------------------
# 制作：转发给官方服务（必须 multipart，绝不用相对路径字段）
# ---------------------------------------------------------------------------

_making_lock = threading.Lock()
_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()
_making_slot: dict[str, Any] | None = None


def _claim_slot(payload: dict[str, Any]) -> bool:
    global _making_slot
    with _tasks_lock:
        if _making_slot is not None:
            return False
        _making_slot = payload
        return True


def _release_slot() -> None:
    global _making_slot
    with _tasks_lock:
        _making_slot = None


def _post_create_task(video_path: Path, avatar_id: str) -> str:
    """返回官方服务给的制作任务号。文件走 multipart ``video_file``。"""
    base = _avatar_base_url()
    if not base:
        raise RuntimeError("BASE_URL_UNDECLARED")
    with open(video_path, "rb") as handle:
        files = {"video_file": (video_path.name, handle, "video/mp4")}
        data = {"model": _avatar_model(), "avatar_id": avatar_id}
        response = httpx.post(base + "/api/avatar/task", data=data, files=files,
                              timeout=UPLOAD_TIMEOUT_S)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or int(payload.get("code", -1)) != 0:
        raise RuntimeError(str(payload.get("msg") or response.status_code))
    task_id = str((payload.get("data") or {}).get("task_id") or "")
    if not task_id:
        raise RuntimeError("TASK_ID_MISSING")
    return task_id


def _get_task(task_id: str) -> dict[str, Any] | None:
    base = _avatar_base_url()
    if not base or not task_id:
        return None
    try:
        response = httpx.get(f"{base}/api/avatar/task/{task_id}", timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if response.status_code != 200 or int(payload.get("code", -1)) != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _delete_task(task_id: str) -> tuple[bool, str]:
    base = _avatar_base_url()
    if not base or not task_id:
        return False, "NO_TASK"
    try:
        response = httpx.delete(f"{base}/api/avatar/task/{task_id}", timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code == 200 and int(payload.get("code", -1)) == 0:
        return True, str(payload.get("msg") or "")
    return False, str(payload.get("msg") or response.status_code)


# ---------------------------------------------------------------------------
# 切换调度（Python 只调度，不碰进程；执行体是 PowerShell 脚本）
# ---------------------------------------------------------------------------

_switch_lock = threading.RLock()  # RLock：start_switch 持锁调用 _switch_is_held（其内部再取锁），
                                  # 非重入 Lock 在第一次换人时就会自死锁（A01 终验实测）。
_switch_state: dict[str, Any] = {"busy": False, "phase": "", "message": "", "result": "",
                                 "detail": "", "previous": "", "target": ""}
_runner: threading.Thread | None = None


def _read_switch_state() -> dict[str, Any]:
    return _read_json(SWITCH_STATE)


def _write_switch_state(**fields: Any) -> dict[str, Any]:
    merged = _read_switch_state()
    merged.update(fields)
    _write_json_atomic(SWITCH_STATE, merged)
    return merged


def _runner_alive() -> bool:
    runner = _runner
    return bool(runner is not None and runner.is_alive())


def _switch_is_held(persisted: dict[str, Any]) -> bool:
    """busy 是活人还是尸体：只有「本进程的线程还活着」或「另一个活进程记了 pid」才算。"""
    with _switch_lock:
        if _switch_state.get("busy") is True and _runner_alive():
            return True
    if persisted.get("busy") is not True:
        return False
    try:
        owner = int(persisted.get("owner_pid") or 0)
    except (TypeError, ValueError):
        owner = 0
    if owner and owner != os.getpid():
        return _pid_alive(owner)
    return _runner_alive()


def _recover_stale_switch(persisted: dict[str, Any]) -> None:
    """采纳一具尸体留下的终态，然后解锁。调用时已持有 _switch_lock。"""
    _switch_state.update(
        busy=False,
        target=str(persisted.get("target") or ""),
        previous=str(persisted.get("previous") or ""),
        result=str(persisted.get("result") or "ROLLBACK_FAILED"),
        detail=str(persisted.get("detail") or "STALE_SWITCH_STATE_RECOVERED"),
        phase="",
        message="",
    )


def _parse_switch_output(output: str) -> str:
    if "AVATAR_SWITCH_OK" in output:
        return "SWITCH_OK"
    if "AVATAR_SWITCH_ABORT" in output:
        return "ABORT"
    if "AVATAR_SWITCH_FAILED ROLLBACK_OK" in output:
        return "ROLLBACK_OK"
    if "AVATAR_SWITCH_FAILED ROLLBACK_FAILED" in output:
        return "ROLLBACK_FAILED"
    return "ROLLBACK_FAILED"


def _run_switch(target_id: str, old_id: str) -> None:
    global _registry_cache
    try:
        completed = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(SWITCH_SCRIPT), "-Target", target_id],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SWITCH_TIMEOUT_S,
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        result = _parse_switch_output(output)
    except Exception as exc:                       # noqa: BLE001  兜底：不外泄
        result, output = "ROLLBACK_FAILED", f"{type(exc).__name__}: {exc}"[:300]
    with _switch_lock:
        _switch_state.update(busy=False, result=result, detail=output[-1000:],
                             phase="", message="")
    _write_switch_state(busy=False, result=result, detail=output[-1000:], phase="", message="")
    with _registry_lock:
        _registry_cache = None


def start_switch(编号: str) -> dict[str, Any]:
    entry = _by_handle(编号)
    if entry is None:
        return _fail("AVATAR_NOT_FOUND", "没有找到这个人物。")
    if str(entry.get("state")) != STATE_READY:
        return _fail("AVATAR_NOT_READY", "这个人物还没做好，暂时不能换过去。")
    target_id = str(entry.get("id") or "")
    persisted = _read_switch_state()
    global _runner
    runner = threading.Thread(target=_run_switch, args=(target_id, ""),
                              daemon=True, name="avatar-switch")
    with _switch_lock:
        if _switch_is_held(persisted):
            return _fail("SWITCH_ALREADY_RUNNING", "正在换人，请稍等一下。")
        if _switch_state.get("busy") is True or persisted.get("busy") is True:
            _recover_stale_switch(persisted)
        previous = _read_pointer()
        _switch_state.update(busy=True, target=target_id, previous=previous, result="",
                             detail="", phase="precheck", message="正在检查能不能换…")
        payload = dict(_switch_state)
        _runner = runner
    payload["owner_pid"] = os.getpid()
    try:
        _write_switch_state(**payload)
    except OSError as exc:
        with _switch_lock:
            _runner = None
            _switch_state.update(busy=False, result="ROLLBACK_FAILED", phase="", message="",
                                 detail=f"STATE_WRITE_FAILED: {type(exc).__name__}"[:300])
        return _fail("STATE_WRITE_FAILED", "状态记不下来，这一步先不做了。")
    runner.start()
    return _done({"目标": str(entry.get("编号") or ""), "说明": "开始换人了，请稍等。"})


def _supervisor_answer(output: str) -> dict[str, Any]:
    """把 supervisor `-Json` 那一行抠出来。

    它内部的写日志函数是往 stdout 打的，所以合同是**最后一个**以 '{' 开头的行，
    不是第一行；解析不出来就交回空表，让调用方按退出码判断，绝不猜成成功。
    """
    for line in reversed([ln for ln in (output or "").splitlines() if ln.strip().startswith("{")]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _run_avatar_supervisor(action: str, avatar_id: str = "",
                           timeout: int = SWITCH_TIMEOUT_S) -> tuple[bool, str, str, str]:
    """向 :8010 的唯一生命周期入口要一次动作，返回 (成功?, verdict, state, 尾巴)。

    Ensure = 「把它跑起来，已经在跑就 ALREADY_RUNNING，一个进程都不杀」，正是界面
    「重新启动」想要的；本模块不再自己 dot-source 启动体拼命令行。
    """
    shell = SYSTEM_POWERSHELL if Path(SYSTEM_POWERSHELL).is_file() else POWERSHELL
    argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(AVATAR_SUPERVISOR), "-Action", action, "-Json"]
    if avatar_id:
        # -AvatarId 插在 -Json 之前：三个参数都属于被 -File 加载的那个脚本，
        # 顺序无所谓，但拼错一个就整条命令没人接。
        argv.insert(argv.index("-Json"), "-AvatarId", str(avatar_id))
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:                       # noqa: BLE001  兜底：不外泄
        return False, "SUBPROCESS_FAILED", "", f"{type(exc).__name__}: {exc}"[:300]
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    answer = _supervisor_answer(output)
    status = answer.get("status") if isinstance(answer.get("status"), dict) else {}
    verdict = str(answer.get("verdict") or answer.get("state") or "")
    state = str(status.get("state") or "")
    ok = completed.returncode == 0 and verdict != "FAILED"
    if not ok and not verdict:
        verdict = f"EXIT_{completed.returncode}"
    return ok, verdict, state, (f"{output[-700:]} exit={completed.returncode}")


def _start_native(avatar_id: str) -> tuple[bool, str]:
    ok, verdict, state, tail = _run_avatar_supervisor("Ensure", avatar_id)
    detail = f"verdict={verdict or '-'} state={state or '-'} {tail}"
    _log("avatar-switch.log", f"AVATAR_ENSURE id={avatar_id} ok={ok} {detail[:300]}")
    if not ok:
        return False, detail
    if state and state != "RUNNING":
        return False, f"服务没起到 RUNNING：{detail}"
    return True, detail


def sync_to_runtime() -> dict[str, Any]:
    """把指针改成服务实际在用的那个。不重启、不改进程侧，只改这一份记录。"""
    runtime = _runtime_avatar_id()
    if not runtime:
        return _fail("SERVICE_DOWN", "数字人服务没在运行，看不出现在用的是谁。")
    entry = _entry_by_id(runtime)
    if entry is None:
        return _fail("AVATAR_NOT_FOUND", "现在用的这个人物不在列表里。")
    if _read_pointer() == runtime:
        return _done({"说明": "两边已经一致了。"})
    _write_pointer(runtime)
    return _done({"说明": f"已经同步到「{entry.get('display_name')}」。"})


def restart_service() -> dict[str, Any]:
    """回滚也失败之后的「重新启动」：只起当前指针指向的那一个，不自动、不重试。"""
    active = _read_pointer()
    if not active:
        return _fail("AVATAR_NOT_FOUND", "还没选中一个人物，不知道该起哪一个。")
    entry = _entry_by_id(active)
    if entry is None:
        return _fail("AVATAR_NOT_FOUND", "选中的这个人物不在列表里。")
    ok, output = _start_native(active)
    if not ok:
        _log("avatar-switch.log", f"AVATAR_RESTART_FAILED id={active} detail={output[:200]}")
        return _fail("RESTART_FAILED", "没能把数字人服务起起来，请再试一次。")
    _log("avatar-switch.log", f"AVATAR_RESTART_OK id={active}")
    return _done({"说明": f"已经把「{entry.get('display_name')}」重新起起来了。"})


# ---------------------------------------------------------------------------
# R1 / R2 面板与列表
# ---------------------------------------------------------------------------

@router.get("/api/avatar/panel")
def panel() -> dict[str, Any]:
    """界面开机一把拿全：当前是谁、有哪些人、服务在不在、两边一致不一致。"""

    def body() -> dict[str, Any]:
        data = _read_registry()
        active_id = _read_pointer()
        dirty = False
        for entry in (data.get("avatars") or []):
            if isinstance(entry, dict) and _refresh_entry(entry):
                dirty = True
        if dirty:
            data = _write_registry(data)
        items = [_public(entry, active_id) for entry in _entries()
                 if str(entry.get("state")) != STATE_FAILED]
        current = next((item for item in items if item["正在使用"]), None)
        if current is None and items:
            current = items[0]
        return {
            "当前": current,
            "列表": items,
            "服务状态": "运行中" if _service_alive() else "没在运行",
            "一致性": _consistency(),
            "限制": {"最大字节": MAX_BYTES, "最短秒": MIN_SECONDS, "最长秒": MAX_SECONDS,
                     "接受类型": list(ACCEPT_EXT)},
        }

    return _guard(body)


@router.get("/api/avatar/list")
def avatar_list() -> dict[str, Any]:
    """只剩能用的那些；没做成的条目不进列表。"""

    def body() -> dict[str, Any]:
        data = _read_registry()
        active_id = _read_pointer()
        items = [_public(entry, active_id) for entry in _entries()
                 if str(entry.get("state")) != STATE_FAILED]
        return {"条数": len(items), "列表": items}

    return _guard(body)


# ---------------------------------------------------------------------------
# R3 上传前回显 / R4 开始制作 / R5 进度 / R6 取消
# ---------------------------------------------------------------------------

@router.post("/api/avatar/inspect")
def inspect(file: UploadFile = File(...)) -> dict[str, Any]:
    """选完文件立刻落一次盘并读出四项信息；文件只传这一次。"""

    def body() -> dict[str, Any]:
        name = str(file.filename or "")
        if Path(name).suffix.lower() not in ACCEPT_EXT:
            return _fail("BAD_TYPE", "只能选 MP4 视频。")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        handle = uuid.uuid4().hex[:12]
        target = UPLOAD_DIR / f"{handle}_{_safe_filename(name)}"
        size = 0
        with open(target, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    return _fail("TOO_BIG",
                                 f"这个文件太大了（{_human_bytes(size)}），"
                                 f"最多 {_human_bytes(MAX_BYTES)}。")
                out.write(chunk)
        meta = _ffprobe(target)
        if meta is None:
            return _done({"文件名": name, "时长": "", "画面尺寸": "", "帧率": "",
                          "大小": _human_bytes(size), "句柄": handle, "能不能用": True,
                          "原因": "这个视频读不出信息，也可以先试试。"})
        duration = float(meta.get("duration_s") or 0.0)
        if duration and duration < MIN_SECONDS:
            return _fail("TOO_SHORT", f"这段太短了，至少要 {MIN_SECONDS:g} 秒。")
        if duration and duration > MAX_SECONDS:
            return _fail("TOO_LONG", f"这段太长了，最多 {MAX_SECONDS:g} 秒。")
        fps = float(meta.get("fps") or 0.0)
        return _done({
            "文件名": name,
            "时长": _human_duration(duration),
            "画面尺寸": f"{int(meta.get('width') or 0)}×{int(meta.get('height') or 0)}",
            "帧率": f"{fps:g} 帧/秒" if fps else "",
            "大小": _human_bytes(size),
            "句柄": handle,
            "能不能用": True,
            "原因": "",
        })

    return _guard(body)


class CreateRequest(BaseModel):
    显示名: str = ""
    句柄: str = ""


@router.post("/api/avatar/create")
def create(req: CreateRequest | None = Body(default=None)) -> dict[str, Any]:
    """只收名字与句柄：文件早在上一步落过盘了，这里转发一次即可。"""

    def body() -> dict[str, Any]:
        payload = req or CreateRequest()
        name = str(payload.显示名 or "").strip()
        if not name:
            return _fail("EMPTY_NAME", "请给她起个名字。")
        if len(name) < 2:
            return _fail("NAME_TOO_SHORT", "名字至少两个字，换一个吧。")
        handle = _safe_token(payload.句柄)
        if not handle:
            return _fail("BAD_HANDLE", "这个视频的临时编号不对，请重新选一次。")
        if not UPLOAD_DIR.is_dir():
            return _fail("HANDLE_NOT_FOUND", "没找到刚才那个视频，请重新选一次。")
        matches = sorted(UPLOAD_DIR.glob(f"{handle}_*"))
        if not matches:
            return _fail("HANDLE_NOT_FOUND", "没找到刚才那个视频，请重新选一次。")
        video = matches[0]
        for entry in _entries():
            if str(entry.get("display_name")) == name:
                return _fail("NAME_TAKEN",
                             f"已经有一个叫这个名字的了，换一个吧。可以叫：{_suggest_name(name)}")
        with _making_lock:
            avatar_id = _new_avatar_id(name, _sha256_file(video), _now_iso())
            try:
                task_id = _post_create_task(video, avatar_id)
            except Exception as exc:                    # noqa: BLE001
                _log("avatar-create.log",
                     f"CREATE_AVATAR = FAIL reason=UPSTREAM_UNREACHABLE detail={type(exc).__name__}")
                return _fail("UPSTREAM_UNREACHABLE",
                             "数字人服务现在连不上，请确认它正在运行。")
            制作编号 = uuid.uuid4().hex[:12]
            meta = _ffprobe(video) or {}
            record = {
                "task_id": task_id,
                "avatar_id": avatar_id,
                "display_name": name,
                "created_at": _now_iso(),
                "video_name": _safe_filename(str(file_name_of(video))),
                "video_sha256": _sha256_file(video),
                "duration_s": float(meta.get("duration_s") or 0.0),
                "width": int(meta.get("width") or 0),
                "height": int(meta.get("height") or 0),
                "fps": float(meta.get("fps") or 0.0),
            }
            if not _claim_slot(record):
                return _fail("MAKING_BUSY", "还有一个人物正在做，等它做完再开下一个。")
            with _tasks_lock:
                _tasks[制作编号] = record
        return _done({"制作编号": 制作编号, "说明": "开始做了，界面上会一直告诉你在做什么。"})

    return _guard(body)


def file_name_of(video: Path) -> str:
    """``<句柄>_<原名>`` → 原名。句柄定长 12，切掉前 13 个字符。"""
    stem = video.name
    return stem[13:] if len(stem) > 13 else stem


@router.get("/api/avatar/making")
def making(编号: str = Query("")) -> dict[str, Any]:
    """每 1 秒被问一次：把官方服务那三元组翻成人看的五段。"""

    def body() -> dict[str, Any]:
        token = _safe_token(编号)
        with _tasks_lock:
            record = dict(_tasks.get(token) or {}) if token else {}
        if not record:
            return _fail("NO_TASK", "没有找到这次制作。")
        task = _get_task(str(record.get("task_id") or ""))
        if task is None:
            return _fail("NO_TASK", "这次制作查不到了，请重来一次。")
        status = str(task.get("status") or "")
        try:
            progress = float(task.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        try:
            duration = float(task.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if status == "completed":
            return _settle_completed(record)
        if status == "failed":
            reason = str(task.get("error_msg") or "")
            _log("avatar-create.log",
                 f"CREATE_AVATAR = FAIL reason=UPSTREAM_FAILED id={record.get('avatar_id')}")
            _release_slot()
            return _done({"阶段": 0, "百分比": int(progress), "已用秒": round(duration, 1),
                          "在做什么": "没做成", "状态": "没做成",
                          "原因": "没做成：请先确认那段视频里有一张清楚的正脸。" if not reason
                                  else "没做成：请先确认那段视频里有一张清楚的正脸。"})
        if status == "running":
            if progress < 30:
                return _done({"阶段": 2, "百分比": int(progress), "已用秒": round(duration, 1),
                              "在做什么": "正在读取视频", "状态": "进行中", "原因": ""})
            if progress < 90:
                return _done({"阶段": 3, "百分比": int(progress), "已用秒": round(duration, 1),
                              "在做什么": "正在生成画面", "状态": "进行中", "原因": ""})
            return _done({"阶段": 4, "百分比": int(progress), "已用秒": round(duration, 1),
                          "在做什么": "正在核对素材", "状态": "进行中", "原因": ""})
        return _done({"阶段": 1, "百分比": int(progress), "已用秒": round(duration, 1),
                      "在做什么": "正在准备", "状态": "准备", "原因": ""})

    return _guard(body)


def _settle_completed(record: dict[str, Any]) -> dict[str, Any]:
    """做完了：三件套齐全才写进注册表，不齐全按没做成处理。"""
    avatar_id = str(record.get("avatar_id") or "")
    probe = _probe_dir(AVATAR_DATA_DIR / avatar_id)
    if not probe["三件套齐全"]:
        _log("avatar-create.log", f"CREATE_AVATAR = FAIL reason=ASSETS_INCOMPLETE id={avatar_id}")
        _release_slot()
        return _done({"阶段": 0, "百分比": 100, "已用秒": 0, "在做什么": "没做成",
                      "状态": "没做成",
                      "原因": "没做成：素材没生成全，请重来一次。"})
    data = _read_registry()
    entry = _entry_by_id(avatar_id)
    if entry is None:
        entry = {
            "id": avatar_id,
            "编号": _handle_of(avatar_id),
            "display_name": str(record.get("display_name") or "新人物"),
            "model_type": _avatar_model(),
            "dir": str(AVATAR_DATA_DIR / avatar_id),
            "thumb": THUMB_REL,
            "frame_count": int(probe["frame_count"]),
            "coords_bytes": int(probe["coords_bytes"]),
            "coords_sha256": str(probe["coords_sha256"]),
            "source_video": {
                "name": str(record.get("video_name") or ""),
                "sha256": str(record.get("video_sha256") or ""),
                "duration_s": float(record.get("duration_s") or 0.0),
                "width": int(record.get("width") or 0),
                "height": int(record.get("height") or 0),
                "fps": float(record.get("fps") or 0.0),
            },
            "created_at": str(record.get("created_at") or _now_iso()),
            "state": STATE_READY,
            "builtin": False,
        }
        data.setdefault("avatars", []).append(entry)
    else:
        entry["frame_count"] = int(probe["frame_count"])
        entry["coords_bytes"] = int(probe["coords_bytes"])
        entry["coords_sha256"] = str(probe["coords_sha256"])
        entry["state"] = STATE_READY
    _write_registry(data)
    _log("avatar-create.log",
         f"CREATE_AVATAR = PASS id={avatar_id} frames={int(probe['frame_count'])} "
         f"sha256={str(probe['coords_sha256'])[:12]}")
    _release_slot()
    return _done({"阶段": 5, "百分比": 100, "已用秒": 0, "在做什么": "做好了",
                  "状态": "做好了", "原因": "", "编号": _handle_of(avatar_id)})


class CancelRequest(BaseModel):
    制作编号: str = ""


@router.post("/api/avatar/making/cancel")
def cancel(req: CancelRequest | None = Body(default=None)) -> dict[str, Any]:
    """只有还在排队的那一段能真的取消；已经开始处理就明说不能取消。"""

    def body() -> dict[str, Any]:
        payload = req or CancelRequest()
        token = _safe_token(payload.制作编号)
        with _tasks_lock:
            record = dict(_tasks.get(token) or {}) if token else {}
        if not record:
            return _fail("NO_TASK", "没有找到这次制作。")
        ok, _message = _delete_task(str(record.get("task_id") or ""))
        if not ok:
            return _fail("CANCEL_TOO_LATE", "已经开始处理了，等它做完。")
        _release_slot()
        with _tasks_lock:
            _tasks.pop(token, None)
        return _done({"说明": "已经取消了这个制作。"})

    return _guard(body)


# ---------------------------------------------------------------------------
# R7 / R8 / R9 / R10 / R11 切换
# ---------------------------------------------------------------------------

class SwitchRequest(BaseModel):
    编号: str = ""


@router.post("/api/avatar/switch")
def switch(req: SwitchRequest | None = Body(default=None)) -> dict[str, Any]:
    """换人：后台线程跑切换脚本，界面轮询 /api/avatar/switch/state。"""

    def body() -> dict[str, Any]:
        payload = req or SwitchRequest()
        return start_switch(str(payload.编号 or ""))

    return _guard(body)


@router.get("/api/avatar/switch/state")
def switch_state() -> dict[str, Any]:
    """照 tts_switch_state.json 的键语义，只是键翻成中文给界面用。"""

    def body() -> dict[str, Any]:
        persisted = _read_switch_state()
        with _switch_lock:
            live = dict(_switch_state)
        merged = dict(persisted)
        merged.update({key: value for key, value in live.items() if value})
        previous_name = _name_of(str(merged.get("previous") or ""))
        target_name = _name_of(str(merged.get("target") or ""))
        return {
            "忙": bool(_switch_is_held(merged)),
            "阶段": str(merged.get("phase") or ""),
            "提示": _plain(merged.get("message") or "", ""),
            "结果": str(merged.get("result") or ""),
            "明细": _plain(merged.get("detail") or "", ""),
            "原来用的": previous_name,
            "目标": target_name,
        }

    return _guard(body)


class FrameProofRequest(BaseModel):
    编号: str = ""
    距离新: float = 0.0
    距离旧: float = 0.0
    判定: bool = False


@router.post("/api/avatar/switch/frame-proof")
def frame_proof(req: FrameProofRequest | None = Body(default=None)) -> dict[str, Any]:
    """画面比对只留证据，不进闸门：绝不让一次 flaky 的像素比对把成功的切换判成失败。"""

    def body() -> dict[str, Any]:
        payload = req or FrameProofRequest()
        handle = str(payload.编号 or "").strip()[:32]
        try:
            new_distance = float(payload.距离新)
        except (TypeError, ValueError):
            new_distance = 0.0
        try:
            old_distance = float(payload.距离旧)
        except (TypeError, ValueError):
            old_distance = 0.0
        verdict = "是" if bool(payload.判定) else "否"
        _log("avatar-switch.log",
             f"AVATAR_SWITCH_FRAME_PROOF new={handle} d_new={new_distance:.4f} "
             f"d_old={old_distance:.4f} 判定={verdict}")
        return {"ok": True}

    return _guard(body)


class SyncRequest(BaseModel):
    编号: str = ""


@router.post("/api/avatar/sync")
def sync(req: SyncRequest | None = Body(default=None)) -> dict[str, Any]:
    """用户显式点「同步一下」才动指针；绝不静默改写任何一侧。"""

    def body() -> dict[str, Any]:
        return sync_to_runtime()

    return _guard(body)


@router.post("/api/avatar/restart")
def restart() -> dict[str, Any]:
    """回滚也失败之后的手动入口。只起当前指针指向的那一个，不自动、不重试。"""

    def body() -> dict[str, Any]:
        return restart_service()

    return _guard(body)


# ---------------------------------------------------------------------------
# R12 缩略图 / R13 禁用词表
# ---------------------------------------------------------------------------

def _thumb_path(handle: str) -> Path | None:
    """带目录穿越防护：解析后的真实路径必须还在素材目录里。

    路径参数名必须是 ASCII —— 本机 starlette 1.6.0 的 compile_path 只认
    ``[a-zA-Z_]`` 开头的参数名，中文名会被当成字面文本转义，路由永远匹配不上。
    """
    entry = _by_handle(handle)
    if entry is None:
        return None
    base = Path(str(entry.get("dir") or ""))
    if not str(base):
        return None
    try:
        target = (base / str(entry.get("thumb") or THUMB_REL)).resolve()
        root = AVATAR_DATA_DIR.resolve()
        if not str(target).lower().startswith(str(root).lower()):
            return None
    except (OSError, ValueError):
        return None
    return target if target.is_file() else None


@router.get("/api/avatar/thumb/{handle}")
def thumb(handle: str) -> Any:
    """7860 的静态目录够不着素材盘，所以由这条路由把帧字节喂出去。"""
    try:
        path = _thumb_path(handle)
    except Exception:                              # noqa: BLE001
        path = None
    if path is None:
        return Response(content=_PLACEHOLDER_PNG, media_type="image/png",
                        headers={"Cache-Control": "no-store"})
    return FileResponse(str(path), media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/avatar/plain-words")
def plain_words() -> dict[str, Any]:
    """界面文案里禁止出现的那串内部名词。只有闸门会读它，产品界面不渲染。"""
    words = banned_words()
    return {"ok": True, "words": words, "条数": len(words)}


# Asset deletion has no start/stop/switch implementation or process commands.
import avatar_assets as _avatar_assets
_avatar_assets.register(sys.modules[__name__])
