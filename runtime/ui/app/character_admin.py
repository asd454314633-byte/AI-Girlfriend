# -*- coding: utf-8 -*-
"""人物 / 书稿 / 记住的事 —— 给 :7860 那套界面用的一组本地接口。

这一层只做三件事，逻辑一律不重写：

  1. 把 `runtime/character/` 和 `runtime/memory/` 已有的能力按「人做的动作」摆出来
     （新建人物、导入书稿、查找、记一条、忘掉……），一条接口对一个动作；
  2. 把库里那些内部字段名换成给人看的话 —— 界面上永远读不到那几个技术词；
  3. 出错时给一句中文，绝不把 Python 的异常文字丢到浏览器里。

三条边界：

  * **权威在服务端**：人物是谁、记了哪些事、书稿读到哪一步，全部只在
    ``runtime/character/data`` 与 ``runtime/memory/data`` 里。浏览器 localStorage
    最多存一个「面板开没开」，本模块也不接受前端传状态回来；
  * **写长期记忆只有一条路**：`/api/memory/learn` 走抽取 + 规则 + 查重；
    `/api/memory/add` 只给用户亲手补的那类（§15），它不碰设定层；
  * **不清楚就不动**：清空类动作必须带 ``确认词``，值不对就直接拒，不猜用户意思。

不认得任何模型或语音提供方，不联网，不起进程，不装依赖。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR.parents[1]
PROJECT_ROOT = RUNTIME_DIR.parent
CORE_DIR = RUNTIME_DIR / "core"

# 这个文件由 server.py 以「同目录直接 import」的方式加载，但 engine 在仓库根下面，
# 所以路径要自己补上（和 voice_models_admin.py 同一套做法）。
for _p in (str(PROJECT_ROOT), str(CORE_DIR), str(RUNTIME_DIR), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import APIRouter, Body, Query  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from runtime.character import (  # noqa: E402
    analyzer, bundle, canon, context_builder, importer, rules, store,
)
from runtime.memory import extractor as memory_extractor  # noqa: E402
from runtime.memory import service as memory_service  # noqa: E402
from runtime.memory import summary as memory_summary  # noqa: E402

router = APIRouter()

# ---------------------------------------------------------------------------
# 存放位置（正式跑用项目默认值；自测整套改到临时目录，绝不碰正式数据）
# ---------------------------------------------------------------------------

_PATHS: dict[str, Path | None] = {"character_db": None, "memory_db": None,
                                  "pointer": None, "library": None, "bundle_dir": None}
_original_pointer = store.pointer_path()
_original_library = importer.library_dir()


def configure(*, character_db: Any = None, memory_db: Any = None, pointer: Any = None,
              library: Any = None, bundle_dir: Any = None) -> dict[str, Any]:
    """把五处存放位置指到别的地方去。只在自测里用；不传的那几项保持原样。"""
    for name, value in (("character_db", character_db), ("memory_db", memory_db),
                        ("pointer", pointer), ("library", library),
                        ("bundle_dir", bundle_dir)):
        _PATHS[name] = Path(value) if value else None
    store.set_pointer_path(_PATHS["pointer"] or _original_pointer)
    importer.set_library_dir(_PATHS["library"] or _original_library)
    return locations()


def reset_locations() -> dict[str, Any]:
    """全部回到项目默认位置。"""
    return configure(character_db=None, memory_db=None, pointer=None, library=None,
                     bundle_dir=None)


def locations() -> dict[str, Any]:
    """当前五处存放位置（自检与界面排错都要能看见，值本身不含技术名词）。"""
    return {"人物档案": str(_PATHS["character_db"] or store.DB_PATH),
            "记住的事": str(_PATHS["memory_db"] or memory_service.DB_PATH),
            "当前是哪个人物": str(store.pointer_path()),
            "读回来的原文": str(importer.library_dir()),
            "整套备份文件": str(_PATHS["bundle_dir"] or bundle.BUNDLE_DIR)}


def _cdb() -> Path:
    return _PATHS["character_db"] or store.DB_PATH


def _mdb() -> Path:
    return _PATHS["memory_db"] or memory_service.DB_PATH


def _bundles() -> Path:
    return _PATHS["bundle_dir"] or bundle.BUNDLE_DIR


# ---------------------------------------------------------------------------
# 统一回包：{ok, reason?, 说明}，并且给人看的字段一定过一遍「不许露技术词」
# ---------------------------------------------------------------------------

_MESSAGE_KEYS = ("说明", "理由", "原因", "错误", "状态", "备注", "详情")


def _plain(text: Any, fallback: str = "这一步没做成，请再试一次。") -> str:
    """把一句要给用户看的话洗干净：露了内部名词就换成一句人话，不就地打码。"""
    body = str(text or "").strip()
    if not body:
        return fallback
    if rules.find_banned(body):
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
    """把 engine 返回的各种形状统一成回包：字典照搬，其它包一层。"""
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
    except MemorySafetyError as exc:
        return _fail(exc.reason, str(exc))
    except PermissionError as exc:
        return _fail("NOT_ALLOWED", _plain(exc, "这件事不能这么做：原著里写定的内容"
                                               "不会被聊天改动。"),
                     异常类型=type(exc).__name__)
    except FileNotFoundError as exc:
        return _fail("NOT_FOUND", "没找到那个文件，麻烦再确认一下位置。",
                     异常类型=type(exc).__name__, 内部=str(exc)[:120])
    except ValueError as exc:
        code = str(exc) or "BAD_REQUEST"
        return _fail(code, _plain(code, "这段话我读不懂，请重新填一下。"),
                     异常类型=type(exc).__name__)
    except Exception as exc:                       # noqa: BLE001  兜底：不外泄
        return _fail("SERVER_SIDE_FAILURE",
                     "服务器这边没做成，详情记在日志里了。",
                     异常类型=type(exc).__name__, 内部=f"{type(exc).__name__}: {exc}"[:160])
    if not isinstance(result, dict):
        return _done(result)
    return _tidy(result)


class MemorySafetyError(Exception):
    """不清不楚就不许动手：确认词不对、范围没给全，都算这一类。"""

    def __init__(self, reason: str, note: str) -> None:
        super().__init__(note)
        self.reason = reason


CONFIRM = "确认"


def _need_confirm(value: Any, *, what: str) -> None:
    got = str(value or "").strip()
    if got != CONFIRM:
        raise MemorySafetyError(
            "CONFIRM_REQUIRED",
            f"要{what}，请把确认词填成「{CONFIRM}」再来。界面上点「确定」就会带上。")


def _active_or(value: Any) -> str:
    text = str(value or "").strip()
    return text or store.get_active_character(db_path=_cdb())


def _subject_or_character(ident: str, given: Any) -> str:
    """分析书稿里的"哪一个人"：没填就用**当前这个人**的名字。

    以前这里是把空字符串直接递给分析器的，于是"导入一本书并分析"这条
    最常见的路径等于让分析器去猜书名；猜不出来就 0 条资料，
    而用户看到的只是"分析完了，什么也没收到"。
    这个接口本来就是"给她收资料"，人是谁在这儿已经确定，不该再猜。
    """
    text = str(given or "").strip()
    if text:
        return text
    try:
        profile = store.get_profile(ident, db_path=_cdb())
    except Exception:                                          # noqa: BLE001
        return ""
    return str(getattr(profile, "display_name", "") or "").strip()


def _character_row(profile: Any) -> dict[str, Any]:
    return {"编号": profile.character_id, "人物名称": profile.display_name,
            "角色名称": profile.nickname, "称呼方式": profile.pronoun,
            "怎么说话": profile.speaking_style, "和说话的人": profile.relationship,
            "不接的话": profile.boundary, "开场一句": profile.greeting,
            "偏爱": list(profile.likes or []), "不爱": list(profile.dislikes or []),
            "头像": profile.avatar_id,
            "台词字数": len(store.get_persona(profile.character_id, db_path=_cdb())),
            "改动时间": profile.updated_at}


def _memory_row(record: Any) -> dict[str, Any]:
    return {"编号": record.memory_id, "内容": record.text,
            "类别": KIND_LABELS.get(record.kind, record.kind),
            "类别编号": record.kind,
            "归到哪一组": GROUP_OF_LAYER.get(record.layer, "长期记忆"),
            "分组编号": record.layer,
            "把握": round(float(record.confidence), 2),
            "明说过": bool(record.explicit),
            "哪一场记下的": record.source_session, "第几句": int(record.source_turn),
            "记下时间": record.created_at, "改动时间": record.updated_at,
            "可以亲手改": bool(record.user_editable), "还在用": bool(record.active),
            "标签": list(record.tags or [])}


def _hydrate(hit: Any, db: Any) -> Any:
    """查到的是一条「命中片段」，字段比档案少，所以照编号回读整条。

    少这一步，界面上「哪一场记下的、能不能亲手改」这些栏位就是空的。
    """
    ident = str(getattr(hit, "memory_id", "") or "").strip()
    if not ident:
        return None
    return memory_service.get(ident, db_path=db)



def _canon_row(fact: dict[str, Any]) -> dict[str, Any]:
    return {"编号": fact.get("fact_id", ""), "内容": fact.get("text", ""),
            "哪一面": fact.get("维度", ""), "方面编号": fact.get("dimension", ""),
            "依据": fact.get("依据", ""), "依据编号": fact.get("basis", ""),
            "出处": fact.get("location", ""), "原文引句": fact.get("evidence", ""),
            "哪本书编号": fact.get("document_id", ""),
            "哪一段": fact.get("chunk_id", ""),
            "把握": round(float(fact.get("confidence") or 0.0), 2),
            "可以亲手改": bool(fact.get("user_editable", True)),
            "还在用": bool(fact.get("active", True)),
            "记下时间": fact.get("created_at", ""), "改动时间": fact.get("updated_at", "")}


def _book_row(doc: dict[str, Any]) -> dict[str, Any]:
    return {"编号": doc.get("document_id", ""), "书名": doc.get("title", ""),
            "文件名": doc.get("source_name", ""), "体裁": doc.get("format", ""),
            "字数": int(doc.get("char_count") or 0), "段数": int(doc.get("part_count") or 0),
            "还在用": bool(doc.get("active", True)), "收进来时间": doc.get("created_at", ""),
            "内容指纹": str(doc.get("sha256", ""))[:12]}


def _read_json_at(path: Any) -> dict[str, Any]:
    """按绝对路径读一份备份。utf-8-sig：Windows 上另存的文件常常带开头的隐形标记。"""
    body = Path(str(path or "").strip())
    if not str(path or "").strip():
        raise ValueError("EMPTY_PATH")
    if not body.exists():
        raise FileNotFoundError(str(body))
    data = json.loads(body.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("NOT_A_OBJECT")
    return data


# ---------------------------------------------------------------------------
# 界面分组（§16 那五组，名字全中文，接口里不出现层的英文名）
# ---------------------------------------------------------------------------

KIND_LABELS = {"preference": "偏好", "fact": "事实", "relationship": "关系",
               "commitment": "约定", "goal": "打算", "shared_event": "一起经历过的事",
               "turn": "这一场说过的话"}
GROUP_OF_LAYER = {"PROFILE": "人物设定", "PERSONA": "人物设定", "CANON": "原著资料",
                  "RELATIONSHIP": "当前关系", "LONG_TERM_MEMORY": "长期记忆",
                  "SESSION": "本次会话", "SOURCE": "原著资料"}
UI_GROUPS = ("原著资料", "人物设定", "长期记忆", "当前关系", "本次会话")

# 记忆这件事对外就九个动作（§14 那份清单）。界面和闸门都从这里读，不再各写一份。
MEMORY_VERBS: tuple[dict[str, str], ...] = (
    {"编号": "search", "名字": "查找", "一句话": "按你给的词，去记住的事里找",
     "接口": "/api/memory/search", "做法": "读"},
    {"编号": "add", "名字": "新增", "一句话": "亲手补一条要她记住的事",
     "接口": "/api/memory/add", "做法": "写"},
    {"编号": "update", "名字": "修改", "一句话": "把某一条换成另一句说法",
     "接口": "/api/memory/update", "做法": "写"},
    {"编号": "delete", "名字": "删除", "一句话": "把某一条收起来，还能还原",
     "接口": "/api/memory/delete", "做法": "写"},
    {"编号": "clear_session", "名字": "清空本次会话", "一句话": "只忘这一场说的话",
     "接口": "/api/memory/clear_session", "做法": "写"},
    {"编号": "clear_long_term", "名字": "清空长期记忆", "一句话": "忘掉长期记着的事",
     "接口": "/api/memory/clear_long_term", "做法": "写"},
    {"编号": "restore_character", "名字": "恢复人物", "一句话": "把收起来的放回来，"
                                                              "或退回刚认识",
     "接口": "/api/memory/restore", "做法": "写"},
    {"编号": "export", "名字": "导出", "一句话": "整套读出来，留个底",
     "接口": "/api/memory/export", "做法": "读"},
    {"编号": "import", "名字": "导入", "一句话": "把留过的那一份搬回来",
     "接口": "/api/memory/import", "做法": "写"},
)

# 九件事之外，人物那一侧还有六个动作
CHARACTER_ACTS: tuple[dict[str, str], ...] = (
    {"编号": "list", "名字": "看有哪些人物", "接口": "/api/character/list"},
    {"编号": "create", "名字": "新建人物", "接口": "/api/character/create"},
    {"编号": "select", "名字": "换下一个人物", "接口": "/api/character/select"},
    {"编号": "edit", "名字": "编辑人物", "接口": "/api/character/edit"},
    {"编号": "export", "名字": "导出人物", "接口": "/api/character/export"},
    {"编号": "import", "名字": "导入人物", "接口": "/api/character/import"},
)


def _verb_index() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for verb in MEMORY_VERBS:
        out.append({"动作": verb["编号"], "名字": verb["名字"], "一句话": verb["一句话"],
                    "接口": verb["接口"], "做法": verb["做法"], "属于": "记住的事"})
    for act in CHARACTER_ACTS:
        out.append({"动作": act["编号"], "名字": act["名字"], "接口": act["接口"],
                    "一句话": act["名字"], "做法": "写", "属于": "人物"})
    out.append({"动作": "import_book", "名字": "导入书稿", "一句话": "收进一本 TXT 或 Markdown",
                "接口": "/api/character/book/import", "做法": "写", "属于": "书稿"})
    out.append({"动作": "progress", "名字": "看进行到哪一步", "一句话": "导入和分析都有进度",
                "接口": "/api/character/book/progress", "做法": "读", "属于": "书稿"})
    return out


# ---------------------------------------------------------------------------
# 后台动作的进度（导入书稿、分析人物都可能跑几十秒）
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}
_JOB_ORDER: list[str] = []
_JOB_LOCK = threading.Lock()
_JOB_KEEP = 12

# 每一步占整体多少：读文本 → 切段 → 登记原文 → 分析 → 登记资料
_STAGE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("正在读这份文本", 0.0, 8.0),
    ("正在按章节和段落切开", 8.0, 10.0),
    ("正在把原文登记好", 18.0, 24.0),
    ("正在读这本书", 42.0, 4.0),
    ("正在照着原文找她的样子", 46.0, 18.0),
    ("正在归纳她说话的习惯", 64.0, 12.0),
    ("正在把这些资料登记好", 76.0, 18.0),
    ("正在照抄她的原话", 94.0, 5.0),
)
_STAGE_BY_NAME = {name: (base, span) for name, base, span in _STAGE_BANDS}
_DONE_BAND = (99.0, 1.0)
_FAILED_BAND = (0.0, 0.0)


def _new_job(kind: str, label: str) -> dict[str, Any]:
    job = {"作业编号": uuid.uuid4().hex[:10], "在做什么": label, "类型": kind,
           "进行到哪一步": "排队中", "完成度": 0.0, "已完成": 0, "总数": 0,
           "状态": "进行中", "结果": None, "说明": "", "开始于": memory_service.now_iso(),
           "更新时刻": memory_service.now_iso(), "started": time.monotonic()}
    with _JOB_LOCK:
        _JOBS[job["作业编号"]] = job
        _JOB_ORDER.append(job["作业编号"])
        while len(_JOB_ORDER) > _JOB_KEEP:
            _JOBS.pop(_JOB_ORDER.pop(0), None)
    return job


def _tick(job: dict[str, Any], stage: str, done: int = 0, total: int = 0) -> None:
    base, span = _STAGE_BY_NAME.get(stage, (job.get("_base", 0.0), 0.0))
    ratio = (done / total) if total else 1.0
    with _JOB_LOCK:
        job["_base"] = base
        job["进行到哪一步"] = stage
        job["完成度"] = round(min(99.0, base + span * max(0.0, min(1.0, ratio))), 1)
        job["已完成"] = int(done)
        job["总数"] = int(total)
        job["更新时刻"] = memory_service.now_iso()


def _finish(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    ok = bool(isinstance(result, dict) and result.get("ok"))
    with _JOB_LOCK:
        job["结果"] = result
        job["状态"] = "完成" if ok else "失败"
        job["进行到哪一步"] = "已经做完了" if ok else "这一步没做成"
        job["完成度"] = 100.0
        job["说明"] = _plain((result or {}).get("说明") if isinstance(result, dict) else "",
                             "已经办好了。" if ok else "这一步没做成，原因见下面。")
        job["更新时刻"] = memory_service.now_iso()
    return result


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    with _JOB_LOCK:
        rows = {key: value for key, value in job.items() if not key.startswith("_")}
    rows["耗时秒"] = round(time.monotonic() - float(job.get("started") or 0.0), 2)
    rows.pop("started", None)
    rows["结果"] = None if job.get("状态") == "进行中" else _tidy(rows.get("结果") or {})
    return rows


def _run_job(job: dict[str, Any], body: Callable[[], dict[str, Any]]) -> None:
    """后台线程那一侧的外壳：作业本身已经知道自己是哪一份，不再多传一遍。"""
    def _wrapped() -> dict[str, Any]:
        return _guard(body)
    _finish(job, _wrapped())


def _find_job(job_id: str) -> dict[str, Any] | None:
    with _JOB_LOCK:
        if job_id:
            return _JOBS.get(job_id)
        return _JOBS[_JOB_ORDER[-1]] if _JOB_ORDER else None


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class CharacterRequest(BaseModel):
    character_id: str | None = None
    display_name: str | None = None
    nickname: str | None = None
    pronoun: str | None = None
    speaking_style: str | None = None
    relationship: str | None = None
    boundary: str | None = None
    greeting: str | None = None
    likes: list[str] | None = None
    dislikes: list[str] | None = None
    avatar_id: str | None = None
    persona: str | None = None
    select: bool | None = None
    confirm: str | None = None
    hard: bool | None = None


class PersonaRequest(BaseModel):
    text: str
    character_id: str | None = None


class BookImportRequest(BaseModel):
    path: str | None = None
    text: str | None = None
    title: str | None = None
    character_id: str | None = None
    analyze: bool = False
    subject: str | None = None
    wait: bool = True


class BookAnalyzeRequest(BaseModel):
    document_id: str
    name: str | None = None
    character_id: str | None = None
    quote_limit: int | None = None
    wait: bool = True


class BookRemoveRequest(BaseModel):
    document_id: str
    confirm: str | None = None
    hard: bool = False


class FactRequest(BaseModel):
    fact_id: str | None = None
    text: str | None = None
    dimension: str | None = None
    basis: str | None = None
    weight: int | None = None
    character_id: str | None = None
    evidence: str | None = None
    location: str | None = None
    confirm: str | None = None
    hard: bool = False


class MemoryAddRequest(BaseModel):
    text: str
    kind: str | None = None
    group: str | None = None
    confidence: float | None = None
    session_id: str | None = None
    turn_id: int | None = None
    character_id: str | None = None


class MemoryUpdateRequest(BaseModel):
    memory_id: str
    text: str | None = None
    kind: str | None = None
    confidence: float | None = None
    user_editable: bool | None = None


class MemoryDeleteRequest(BaseModel):
    memory_id: str | None = None
    text: str | None = None
    character_id: str | None = None
    confirm: str | None = None


class ClearSessionRequest(BaseModel):
    session_id: str
    character_id: str | None = None
    confirm: str | None = None


class ClearLongTermRequest(BaseModel):
    character_id: str | None = None
    kinds: list[str] | None = None
    confirm: str | None = None


class RestoreRequest(BaseModel):
    character_id: str | None = None
    mode: str = "archived"
    confirm: str | None = None


class LearnRequest(BaseModel):
    text: str
    session_id: str | None = None
    turn_id: int | None = None
    character_id: str | None = None


class ImportRequest(BaseModel):
    payload: dict[str, Any] | None = None
    path: str | None = None
    character_id: str | None = None
    replace: bool = False
    merge: bool = True
    set_active: bool = True
    include_memories: bool = True
    include_sources: bool = True


# ---------------------------------------------------------------------------
# 0) 自检 / 口径
# ---------------------------------------------------------------------------

@router.get("/api/character/plain-words")
def plain_words():
    """界面文案里禁止出现的那串内部名词。只有闸门会读它，产品界面不渲染。"""
    return {"ok": True, "words": list(rules.BANNED_UI_WORDS), "条数": len(rules.BANNED_UI_WORDS)}


@router.get("/api/character/state")
def character_state():
    """界面开机一把拿到的那份：当前是谁、有哪些人、能帮她做哪几件事、预算从哪来。"""
    def body() -> dict[str, Any]:
        character = store.get_active_character(db_path=_cdb())
        profile = store.get_profile(character, db_path=_cdb())
        try:
            budget = context_builder.recommended_budget()
            money = {"总长度": budget["budget_total"], "来自哪": budget["来源"],
                     "模型窗口": budget.get("上下文窗口", 0)}
        except Exception as exc:                       # noqa: BLE001
            money = {"总长度": 0, "来自哪": "", "读取失败": type(exc).__name__}
        facts = canon.list_facts(character, db_path=_cdb())
        rows = memory_service.list_memories(character, db_path=_mdb(), limit=5000)
        groups = {name: 0 for name in UI_GROUPS}
        groups["原著资料"] = len(facts)
        groups["人物设定"] = (1 if profile is not None else 0)
        for row in rows:
            name = GROUP_OF_LAYER.get(row.layer, "")
            if name in groups:
                groups[name] += 1
        return {"ok": True,
                "当前人物": _character_row(profile) if profile is not None else {},
                "人物名称": (profile.display_name if profile is not None else ""),
                "人物列表": [_character_row(one)
                             for one in store.list_characters(db_path=_cdb())],
                "分组条数": groups,
                "书稿": [_book_row(one) for one in canon.list_documents(character,
                                                                        db_path=_cdb())],
                "能做的事": _verb_index(),
                "长度上限": money,
                "口径来源": rules.rule_sources(),
                "存在哪": locations()}
    return _guard(body)


# ---------------------------------------------------------------------------
# 1) 人物：看 / 新建 / 换 / 编辑 / 台词 / 删除 / 还原 / 导出 / 导入
# ---------------------------------------------------------------------------

@router.get("/api/character/list")
def character_list(include_archived: bool = Query(False)):
    def body() -> dict[str, Any]:
        rows = store.list_characters(db_path=_cdb(), include_archived=include_archived)
        active = store.get_active_character(db_path=_cdb())
        return {"ok": True, "当前人物": active, "条数": len(rows),
                "人物列表": [dict(_character_row(one), 是不是当前=(one.character_id == active))
                             for one in rows]}
    return _guard(body)


@router.post("/api/character/create")
def character_create(req: CharacterRequest):
    def body() -> dict[str, Any]:
        name = (req.display_name or req.nickname or "").strip()
        if not name:
            return _fail("EMPTY_NAME", "请先给人物起一个名称，两个字以上。")
        ident = (req.character_id or "").strip() or _new_character_id(name)
        known = {one.character_id for one in
                 store.list_characters(db_path=_cdb(), include_archived=True)}
        if ident in known:
            return _fail("ALREADY_THERE", f"已经有「{name}」了，换一个名字或直接去编辑。")
        payload = {"character_id": ident, "display_name": name,
                   "nickname": (req.nickname or "").strip(),
                   "pronoun": (req.pronoun or "").strip(),
                   "speaking_style": (req.speaking_style or "").strip(),
                   "relationship": (req.relationship or "").strip(),
                   "boundary": (req.boundary or "").strip(),
                   "greeting": (req.greeting or "").strip(),
                   "likes": [str(one).strip() for one in (req.likes or []) if str(one).strip()],
                   "dislikes": [str(one).strip() for one in (req.dislikes or [])
                                if str(one).strip()],
                   "avatar_id": (req.avatar_id or "").strip()}
        saved = store.save_profile(store.CharacterProfile(**payload),
                                   db_path=_cdb(), persona=(req.persona or "").strip())
        made = store.get_persona(ident, db_path=_cdb())
        note = f"「{name}」已经建好了。"
        if req.select:
            store.set_active_character(ident, db_path=_cdb())
            note += " 现在已经换成她。"
        return _done({"ok": True, "编号": ident, "人物名称": saved.display_name,
                      "台词已写": bool(made), "说明": note})
    return _guard(body)


def _new_character_id(name: str) -> str:
    import hashlib
    return "c" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


@router.post("/api/character/edit")
def character_edit(req: CharacterRequest):
    """只改传上来的那几项；没传的保持原样。人物台词单独走 /persona。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        old = store.get_profile(ident, db_path=_cdb())
        if old is None:
            return _fail("NOT_THERE", "没有这个人物，先建一个再来改。")
        merged = {"character_id": ident,
                  "display_name": _keep(req.display_name, old.display_name),
                  "nickname": _keep(req.nickname, old.nickname),
                  "pronoun": _keep(req.pronoun, old.pronoun),
                  "speaking_style": _keep(req.speaking_style, old.speaking_style),
                  "relationship": _keep(req.relationship, old.relationship),
                  "boundary": _keep(req.boundary, old.boundary),
                  "greeting": _keep(req.greeting, old.greeting),
                  "likes": [str(one).strip() for one in (req.likes if req.likes is not None
                                                         else old.likes or [])
                            if str(one).strip()],
                  "dislikes": [str(one).strip() for one in (req.dislikes if req.dislikes
                                                            is not None else old.dislikes or [])
                               if str(one).strip()],
                  "avatar_id": _keep(req.avatar_id, old.avatar_id)}
        store.save_profile(store.CharacterProfile(**merged), db_path=_cdb())
        if req.select:
            store.set_active_character(ident, db_path=_cdb())
        return _done({"ok": True, "编号": ident, "人物名称": merged["display_name"],
                      "说明": f"「{merged['display_name']}」已经改好了。"})
    return _guard(body)


def _keep(value: Any, old: Any) -> str:
    return str(value).strip() if value is not None else str(old or "")


@router.post("/api/character/select")
def character_select(req: CharacterRequest):
    def body() -> dict[str, Any]:
        ident = (req.character_id or "").strip()
        if not ident:
            return _fail("EMPTY_ID", "要说清楚换成哪一个人物。")
        result = store.set_active_character(ident, db_path=_cdb())
        if not result.get("ok"):
            return _done(result)
        profile = store.get_profile(ident, db_path=_cdb())
        return _done({"ok": True, "编号": ident,
                      "人物名称": profile.display_name if profile else ident,
                      "说明": f"现在跟{profile.display_name if profile else ident}说话。"})
    return _guard(body)


@router.post("/api/character/persona")
def character_persona(req: PersonaRequest):
    """人物台词：用户自己写的，一个字都不代写、不改写，只做露馅自查。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        result = store.set_persona(req.text or "", ident, db_path=_cdb())
        if not result.get("ok"):
            return _done(result, note="这些话先别写进台词里，说出来的时候会露出设定痕迹。")
        return _done({"ok": True, "编号": ident, "字数": result.get("chars", 0),
                      "说明": f"台词已经存好了，一共 {result.get('chars', 0)} 字。"})
    return _guard(body)


@router.get("/api/character/persona")
def character_persona_get(character_id: str | None = Query(None)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        text = store.get_persona(ident, db_path=_cdb())
        return {"ok": True, "编号": ident, "台词": text, "字数": len(text),
                "提醒": store.persona_issues(text) if text else ["台词还是空的，先写一段她怎么说话。"]}
    return _guard(body)


@router.post("/api/character/delete")
def character_delete(req: CharacterRequest):
    """默认只是把这个人收起来（还能还原）；hard=true 才是真抹掉，要两句确认。"""
    def body() -> dict[str, Any]:
        ident = (req.character_id or "").strip()
        if not ident:
            return _fail("EMPTY_ID", "要说清楚删哪一个人物。")
        if ident == store.DEFAULT_CHARACTER_ID:
            return _fail("PROTECTED", "默认那个人物是留着兜底的，不删。")
        _need_confirm(req.confirm, what="删除这个人物")
        facts = len(canon.list_facts(ident, db_path=_cdb()))
        rows = len(memory_service.list_memories(ident, db_path=_mdb()))
        result = store.delete_character(ident, db_path=_cdb(), hard=bool(req.hard))
        if not result.get("ok"):
            return _done(result)
        purged: dict[str, Any] = {}
        if req.hard:
            # 「彻底抹掉」必须连她记得的事一起抹掉。人物档案和记忆本来就在两个库里，
            # 只删前者的话，将来用同一个名字再建一个人，旧记忆会原样复活。
            purged = memory_service.purge(ident, db_path=_mdb()) or {}
        return _done({"ok": True, "编号": ident, "收起条数": facts, "相关记忆条数": rows,
                      "抹掉记忆条数": int(purged.get("条数") or 0) if req.hard else 0,
                      "说明": (f"「{ident}」已经收起来了：{facts} 条资料、{rows} 条记得的事"
                               "一起收起，随时可以还原。"
                               if not req.hard else
                               f"「{ident}」这一份已经彻底抹掉了，"
                               f"连同 {int(purged.get('条数') or 0)} 条记得的事。")})
    return _guard(body)


@router.post("/api/character/restore")
def character_restore(req: CharacterRequest):
    def body() -> dict[str, Any]:
        ident = (req.character_id or "").strip()
        if not ident:
            return _fail("EMPTY_ID", "要说清楚还原哪一个人物。")
        result = store.restore_character(ident, db_path=_cdb())
        back = memory_service.restore_character(ident, db_path=_mdb())
        facts = len(canon.list_facts(ident, db_path=_cdb()))
        return _done({"ok": bool(result.get("ok")), "编号": ident, "资料条数": facts,
                      "记忆还原条数": int(back.get("还原条数") or 0),
                      "说明": (f"「{ident}」已经放回来了：{facts} 条资料在;"
                               f"记忆回来 {back.get('还原条数', 0)} 条。"
                               if result.get("ok") else "没有这个人物的档案。")})
    return _guard(body)


@router.get("/api/character/export")
def character_export(character_id: str | None = Query(None), write: bool = Query(False),
                     include_memories: bool = Query(True), include_sources: bool = Query(True)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        if not write:
            payload = bundle.export_character(ident, include_memories=include_memories,
                                              include_sources=include_sources,
                                              character_db_path=_cdb(), memory_db_path=_mdb())
            payload = dict(payload)
            payload["人物资料"] = payload.pop("profile", payload.get("人物资料", {}))
            return _done(payload)
        result = bundle.write_bundle(ident, character_db_path=_cdb(), memory_db_path=_mdb(),
                                     directory=_bundles(),
                                     include_memories=include_memories,
                                     include_sources=include_sources)
        if not result.get("ok"):
            return _done(result)
        return _done({"ok": True, "文件": result.get("path", ""),
                      "条数": result.get("条数", {}),
                      "说明": _saved_note(result.get("说明", ""), result.get("path"))})
    return _guard(body)


@router.get("/api/character/bundles")
def character_bundles():
    """列出已经写到项目里的那些整套备份，供「导入人物」直接挑。"""
    def body() -> dict[str, Any]:
        folder = _bundles()
        rows = []
        if folder.exists():
            for item in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime,
                               reverse=True)[:40]:
                try:
                    data = json.loads(item.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                if str(data.get("kind") or "") != bundle.BUNDLE_KIND:
                    continue
                rows.append({"文件": str(item), "文件名": item.name,
                             "人物": str(data.get("人物") or ""),
                             "资料条数": int((data.get("条数") or {}).get("设定条数") or 0),
                             "记忆条数": int((data.get("条数") or {}).get("记忆条数") or 0),
                             "导出时间": str(data.get("导出时间") or "")})
        return {"ok": True, "条数": len(rows), "备份文件": rows,
                "说明": f"找到 {len(rows)} 份整套备份。"}
    return _guard(body)


@router.post("/api/character/import")
def character_import(req: ImportRequest):
    def body() -> dict[str, Any]:
        data = req.payload if isinstance(req.payload, dict) else _read_json_at(req.path)
        result = bundle.import_bundle(data, character_id=(req.character_id or None),
                                      replace=bool(req.replace),
                                      set_active=bool(req.set_active),
                                      include_memories=bool(req.include_memories),
                                      include_sources=bool(req.include_sources),
                                      character_db_path=_cdb(), memory_db_path=_mdb())
        return _done(result)
    return _guard(body)


# ---------------------------------------------------------------------------
# 2) 书稿：只吃 TXT / Markdown，带进度
# ---------------------------------------------------------------------------

def _book_import(job: dict[str, Any], req: BookImportRequest,
                 ident: str) -> dict[str, Any]:
    def progress(stage: str, done: int, total: int) -> None:
        _tick(job, stage, done, total)

    text = str(req.text or "").strip()
    if text:
        receipt = importer.import_text(req.text or "", (req.title or "").strip(), ident,
                                       db_path=_cdb(), progress=progress)
    elif str(req.path or "").strip():
        receipt = importer.import_file(req.path or "", ident, db_path=_cdb(),
                                       title=(req.title or None), progress=progress)
    else:
        return _fail("EMPTY_BOOK", "要么挑一个 TXT / Markdown 文件，要么把正文贴进来。")
    if not receipt.get("ok"):
        return _done(receipt)
    out = dict(receipt)
    out["书稿"] = _book_row(next((one for one in canon.list_documents(ident, db_path=_cdb())
                                  if one["document_id"] == receipt.get("document_id")), {}))
    if req.analyze and not receipt.get("skipped"):
        _tick(job, "正在读这本书")
        report = analyzer.analyze_novel(str(receipt["document_id"]),
                                        _subject_or_character(ident, req.subject),
                                        character_id=ident, db_path=_cdb(),
                                        progress=progress)
        out["分析"] = report
        out["说明"] = str(receipt.get("说明") or "") + " " + str(report.get("说明") or "")
    elif req.analyze:
        out["说明"] = str(receipt.get("说明") or "") + " 这份之前收过了，就不重复分析。"
    return out


@router.post("/api/character/book/import")
def book_import(req: BookImportRequest):
    """导入一本 TXT / Markdown。wait=true 一把跑完直接给结果；false 就返回作业编号。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        job = _new_job("book_import", "收一本新书稿")
        work = lambda: _book_import(job, req, ident)  # noqa: E731
        if req.wait:
            return _guard(work)
        threading.Thread(target=_run_job, args=(job, work), daemon=True).start()
        return {"ok": True, "作业编号": job["作业编号"], "进行中": True,
                "说明": "已经开始收了，进度问 /api/character/book/progress。"}
    return _guard(body)


def _book_analyze(job: dict[str, Any], req: BookAnalyzeRequest, ident: str) -> dict[str, Any]:
    def progress(stage: str, done: int, total: int) -> None:
        _tick(job, stage, done, total)

    if not str(req.document_id or "").strip():
        return _fail("EMPTY_ID", "要说清楚分析哪一份书稿。")
    _tick(job, "正在读这本书")
    report = analyzer.analyze_novel(req.document_id,
                                    _subject_or_character(ident, req.name),
                                    character_id=ident, db_path=_cdb(),
                                    quote_limit=max(1, min(int(req.quote_limit or 5), 20)),
                                    progress=progress)
    if report.get("ok"):
        report = dict(report)
        report["条目"] = [_canon_row(one) if "fact_id" in one else one
                          for one in (report.get("条目") or [])[:40]]
    return _done(report)


@router.post("/api/character/book/analyze")
def book_analyze(req: BookAnalyzeRequest, character_id: str | None = Query(None)):
    """「分析人物」那个按钮：从书稿里给她收资料，原著事实与统计推处分得很清。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id or character_id)
        job = _new_job("book_analyze", "照着她读过的书整理资料")
        work = lambda: _book_analyze(job, req, ident)  # noqa: E731
        if req.wait:
            return _guard(work)
        threading.Thread(target=_run_job, args=(job, work), daemon=True).start()
        return {"ok": True, "作业编号": job["作业编号"], "进行中": True,
                "说明": "已经开始整理了，进度问 /api/character/book/progress。"}
    return _guard(body)


@router.get("/api/character/book/progress")
def book_progress(job_id: str = Query("")):
    def body() -> dict[str, Any]:
        job = _find_job(job_id)
        if job is None:
            return _fail("NO_JOB", "现在没有正在跑的任务。")
        rows = _public_job(job)
        rows["ok"] = True
        rows["说明"] = rows.get("说明") or (
            f"{rows['进行到哪一步']}（已完成 {rows['已完成']} / 共 {rows['总数']}）"
            if rows.get("状态") == "进行中" else rows.get("说明") or "")
        if job_id and job_id not in _JOBS:
            return _fail("NO_JOB", "这个作业编号找不到，可能已经清掉了。")
        return _tidy(rows)
    return _guard(body)


@router.get("/api/character/book/list")
def book_list(character_id: str | None = Query(None)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        rows = [_book_row(one) for one in canon.list_documents(ident, db_path=_cdb())]
        return {"ok": True, "编号": ident, "条数": len(rows), "书稿": rows,
                "说明": f"{ident} 手边有 {len(rows)} 份书稿。"}
    return _guard(body)


@router.get("/api/character/book/parts")
def book_parts(document_id: str = Query(...), offset: int = Query(0),
               chars: int = Query(1200)):
    """按字数窗口读原文，给用户核对出处用的，不叫模型。"""
    def body() -> dict[str, Any]:
        window = canon.read_window(document_id, offset, chars, db_path=_cdb())
        if not window.get("ok"):
            return _done(window)
        # 这一条是给界面上「核对出处」用的，所以键名要和别的路由一样说中文；
        # 工具层读原文走的是 canon.read_window，不经过这里，改它不受影响。
        return _done({"ok": True, "编号": str(window.get("document_id") or ""),
                      "书名": str(window.get("title") or ""),
                      "哪一章": str(window.get("chapter") or ""),
                      "正文": str(window.get("text") or ""),
                      "从第几个字": int(window.get("offset") or 0),
                      "下一段从": int(window.get("next_offset") or 0),
                      "读完了": bool(window.get("at_end")),
                      "全书字数": int(window.get("total_chars") or 0),
                      "说明": (f"这一段 {len(str(window.get('text') or ''))} 字，"
                               f"全书共 {int(window.get('total_chars') or 0)} 字。")})
    return _guard(body)


@router.post("/api/character/book/remove")
def book_remove(req: BookRemoveRequest):
    def body() -> dict[str, Any]:
        _need_confirm(req.confirm, what="收走这份书稿")
        if not str(req.document_id or "").strip():
            return _fail("EMPTY_ID", "要说清楚收走哪一份。")
        result = canon.delete_document(req.document_id, hard=bool(req.hard), db_path=_cdb())
        if not result.get("ok"):
            return _done(result)
        return _done({"ok": True, "编号": req.document_id,
                      "说明": "这份书稿已经收起来了；把这个人物的资料一起还原时也会回来。"})
    return _guard(body)


# ---------------------------------------------------------------------------
# 3) 资料（原著写定的那些，聊天改不动）
# ---------------------------------------------------------------------------

@router.get("/api/character/canon/list")
def canon_list(character_id: str | None = Query(None), dimension: str = Query(""),
               basis: str = Query(""), include_archived: bool = Query(False),
               limit: int = Query(200)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        rows = canon.list_facts(ident, include_archived=include_archived,
                                dimension=dimension or None, basis=basis or None,
                                db_path=_cdb())[:max(1, min(limit, 2000))]
        return {"ok": True, "编号": ident, "条数": len(rows), "资料": [_canon_row(one)
                                                                      for one in rows],
                "分组": [{"哪一面": one["label_zh"], "编号": key, "条数": 0}
                         for key, one in canon.DIMENSIONS.items()],
                "可分的面": [{"编号": key, "名字": one["label_zh"]}
                             for key, one in canon.DIMENSIONS.items()]}
    return _guard(body)


@router.get("/api/character/canon/search")
def canon_search(q: str = Query(...), character_id: str | None = Query(None),
                 limit: int = Query(6)):
    """按中文找她读过的书里怎么写的：设定条目和原文段落一起找，都带出处。"""
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        if not str(q or "").strip():
            return _fail("EMPTY_QUERY", "要先说要找什么。")
        rows = canon.canon_passages(q, ident, limit=max(1, min(int(limit or 6), 24)),
                                    db_path=_cdb())
        hits = []
        for one in rows:
            hits.append({"内容": str(getattr(one, "text", "") or ""),
                         "书名": str(getattr(one, "title", "") or ""),
                         "哪一章": str(getattr(one, "chapter", "") or ""),
                         "相关度": round(float(getattr(one, "score", 0.0) or 0.0), 3),
                         "是哪一类": "她的资料" if getattr(one, "source_kind", "") == "canon"
                                     else "书里的原文",
                         "编号": str(getattr(one, "memory_id", "") or "")})
        return {"ok": True, "编号": ident, "查的词": q, "条数": len(hits), "命中": hits,
                "说明": (f"书里对上 {len(hits)} 处。" if hits
                         else f"她在书里的资料没找到「{q}」这件事。")}
    return _guard(body)


@router.post("/api/character/canon/add")
def canon_add(req: FactRequest):
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        result = canon.add_fact(req.text or "", character_id=ident,
                                dimension=(req.dimension or "").strip(),
                                basis=(req.basis or "manual").strip() or "manual",
                                origin="manual", kind="setting",
                                weight=int(req.weight or 100), confidence=0.95,
                                evidence=(req.evidence or "").strip(),
                                location=(req.location or "").strip(),
                                db_path=_cdb())
        return _done(result)
    return _guard(body)


@router.post("/api/character/canon/update")
def canon_update(req: FactRequest):
    def body() -> dict[str, Any]:
        if not str(req.fact_id or "").strip():
            return _fail("EMPTY_ID", "要说清楚改哪一条。")
        result = canon.update_fact(req.fact_id, text=req.text, dimension=req.dimension,
                                   basis=req.basis, weight=req.weight,
                                   evidence=req.evidence, location=req.location,
                                   db_path=_cdb())
        return _done(result)
    return _guard(body)


@router.post("/api/character/canon/delete")
def canon_delete(req: FactRequest):
    def body() -> dict[str, Any]:
        _need_confirm(req.confirm, what="删掉这条资料")
        if not str(req.fact_id or "").strip():
            return _fail("EMPTY_ID", "要说清楚删哪一条。")
        result = canon.delete_fact(req.fact_id, hard=bool(req.hard), db_path=_cdb())
        if not result.get("ok"):
            return _done(result)
        return _done({"ok": True, "编号": req.fact_id, "条数": 1,
                      "说明": "这条资料收起来了，还原人物时会回来。"})
    return _guard(body)


# ---------------------------------------------------------------------------
# 4) 记住的事：九个动作
# ---------------------------------------------------------------------------

def _group_layers(group: str | None) -> tuple[str, ...] | None:
    if not group:
        return None
    wanted = {name: [] for name in UI_GROUPS}
    for layer, name in GROUP_OF_LAYER.items():
        if name == group:
            wanted[name].append(layer)
    rows = tuple(wanted.get(group) or ())
    return rows or None


@router.get("/api/memory/list")
def memory_list(character_id: str | None = Query(None), group: str = Query(""),
                include_archived: bool = Query(False), limit: int = Query(200)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        layers = _group_layers(group or None)
        rows = memory_service.list_memories(ident, include_archived=include_archived,
                                            layers=layers,
                                            limit=max(1, min(limit, 2000)), db_path=_mdb())
        return {"ok": True, "编号": ident, "条数": len(rows), "记忆": [_memory_row(one)
                                                                      for one in rows]}
    return _guard(body)


@router.get("/api/memory/search")
def memory_search(q: str = Query(...), character_id: str | None = Query(None),
                  limit: int = Query(8), group: str = Query(""),
                  session_id: str = Query("")):
    """查找：可以只在某一组里找，也可以连着这一场说的话一起找。"""
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        if not str(q or "").strip():
            return _fail("EMPTY_QUERY", "要先说要找什么。")
        layers = _group_layers(group or None)
        rows = memory_service.search(q, ident, limit=max(1, min(limit, 24)),
                                     include_session=bool(session_id) or group == "本次会话",
                                     session_id=session_id or None, db_path=_mdb())
        pairs = [(one, _hydrate(one, _mdb())) for one in rows]
        picked = [(one, full) for one, full in pairs
                  if full is not None and (not layers or full.layer in layers)]
        hits = []
        for one, full in picked:
            row = _memory_row(full)
            row["相关度"] = round(float(getattr(one, "score", 0.0) or 0.0), 3)
            row["出自哪段话"] = str(getattr(one, "text", "") or "")[:80]
            hits.append(row)
        return {"ok": True, "编号": ident, "查的词": q, "条数": len(hits), "命中": hits,
                "说明": (f"找到 {len(hits)} 条相关的。" if hits
                         else f"「{q}」这件事我这边没有记过。")}
    return _guard(body)


@router.post("/api/memory/add")
def memory_add(req: MemoryAddRequest):
    """用户亲手补一条要记的事。设定层不接受这条路（§20）。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        kind = (req.kind or "").strip() or _kind_of_group(req.group)
        if kind not in memory_service.KINDS:
            return _fail("BAD_KIND", "这种事我这边不记，换个说法或者选一个类别。")
        layer = _layer_of_group(req.group)
        record = memory_service.add(req.text or "", ident, kind=kind, layer=layer,
                                    confidence=float(req.confidence
                                                     if req.confidence is not None else 0.9),
                                    explicit=True, source_session=(req.session_id or ""),
                                    source_turn=int(req.turn_id or 0),
                                    source_kind="USER", db_path=_mdb())
        return _done({"ok": True, "编号": record.memory_id, "内容": record.text,
                      "类别": KIND_LABELS.get(record.kind, record.kind),
                      "归到哪一组": GROUP_OF_LAYER.get(record.layer, "长期记忆"),
                      "说明": f"记下了：{record.text}"})
    return _guard(body)


def _kind_of_group(group: str | None) -> str:
    return {"当前关系": "relationship", "本次会话": "turn"}.get(str(group or ""), "fact")


def _layer_of_group(group: str | None) -> str:
    name = str(group or "")
    if name == "当前关系":
        return "RELATIONSHIP"
    if name == "本次会话":
        return memory_service.SESSION_LAYER
    return "LONG_TERM_MEMORY"


@router.post("/api/memory/update")
def memory_update(req: MemoryUpdateRequest):
    def body() -> dict[str, Any]:
        if not str(req.memory_id or "").strip():
            return _fail("EMPTY_ID", "要说清楚改哪一条。")
        result = memory_service.update(req.memory_id, text=req.text, kind=req.kind,
                                       confidence=req.confidence,
                                       user_editable=req.user_editable, db_path=_mdb())
        if not result.get("ok"):
            return _done(result)
        row = memory_service.get(req.memory_id, db_path=_mdb())
        out = dict(result)
        out["编号"] = req.memory_id
        out["现在写的是"] = row.text if row else ""
        out["说明"] = f"改好了，现在这条是：{row.text}" if row else "改好了。"
        return _done(out)
    return _guard(body)


@router.post("/api/memory/delete")
def memory_delete(req: MemoryDeleteRequest):
    """给编号删一条；给一句话就删最像的那条。默认是收起来，能还原。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        _need_confirm(req.confirm, what="忘掉这条")
        if not str(req.memory_id or "").strip() and not str(req.text or "").strip():
            return _fail("EMPTY_TARGET", "要说清楚是忘掉哪一条，或者忘掉哪句话。")
        result = memory_service.delete(memory_id=(req.memory_id or None),
                                      match_text=(req.text or None),
                                      character_id=ident if not req.memory_id else None,
                                      db_path=_mdb())
        if not result.get("ok"):
            return _done(result)
        out = dict(result)
        out["条数"] = int(result.get("条数") or 0)
        out["说明"] = (f"已经忘记了 {out['条数']} 条。" if out["条数"]
                       else "没有找到对得上的，所以一条也没动。")
        return _done(out)
    return _guard(body)


@router.post("/api/memory/clear_session")
def memory_clear_session(req: ClearSessionRequest):
    """清空本次会话：只收这一场的原话与近况，长期记的一条都不碰。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        session = str(req.session_id or "").strip()
        if not session:
            return _fail("EMPTY_SESSION", "要说清楚是哪一场，避免误清别人的。")
        _need_confirm(req.confirm, what="清空本次会话")
        before = [one for one in memory_service.list_memories(
            ident, layers=(memory_service.SESSION_LAYER,), limit=100000, db_path=_mdb())
            if one.source_session == session]
        result = memory_service.clear_session(ident, session, db_path=_mdb())
        out = dict(result)
        out["这一场原有条数"] = len(before)
        out["长期记忆条数"] = len(memory_service.list_memories(
            ident, layers=("LONG_TERM_MEMORY", "RELATIONSHIP"), limit=100000, db_path=_mdb()))
        out["说明"] = (f"这一场的 {out.get('条数', 0)} 句已经忘了，"
                       f"长期记着的 {out['长期记忆条数']} 条一条没动。")
        return _done(out)
    return _guard(body)


@router.post("/api/memory/clear_long_term")
def memory_clear_long_term(req: ClearLongTermRequest):
    """清空长期记忆：这一场的原话不动，原著资料和人物设定更不动。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        _need_confirm(req.confirm, what="清空长期记忆")
        facts_before = len(canon.list_facts(ident, db_path=_cdb()))
        result = memory_service.clear_long_term(ident, kinds=req.kinds or None,
                                                db_path=_mdb())
        out = dict(result)
        out["条数"] = int(result.get("条数") or 0)
        out["原著资料条数"] = facts_before
        out["现在还用着的条数"] = len(memory_service.list_memories(ident, db_path=_mdb()))
        out["说明"] = (f"长期记着的 {out['条数']} 条已经收起来了，随时能还原；"
                       f"原著资料 {facts_before} 条一条没动。")
        return _done(out)
    return _guard(body)


@router.post("/api/memory/restore")
def memory_restore(req: RestoreRequest):
    """两种恢复：archived = 把收起来的放回来；initial = 退回刚认识（资料保留）。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        mode = str(req.mode or "archived").strip() or "archived"
        if mode not in ("archived", "initial"):
            return _fail("BAD_MODE", "只认得「放回来」和「退回刚认识」这两种。")
        if mode == "initial":
            _need_confirm(req.confirm, what="退回刚认识的样子")
        result = memory_service.restore_character(ident, mode=mode, db_path=_mdb())
        out = dict(result)
        profile = store.restore_character(ident, db_path=_cdb()) if mode == "initial" else None
        out["人物档案"] = bool((profile or {}).get("ok")) if profile else None
        out["原著资料条数"] = len(canon.list_facts(ident, db_path=_cdb()))
        out["说明"] = result.get("说明") or (
            f"回来了 {result.get('还原条数', 0)} 条。" if mode == "archived" else "已经退回刚认识。")
        return _done(out)
    return _guard(body)


@router.get("/api/memory/export")
def memory_export(character_id: str | None = Query(None), write: bool = Query(False),
                  include_archived: bool = Query(True)):
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        payload = memory_service.export(ident, include_archived=include_archived,
                                        db_path=_mdb())
        payload = dict(payload)
        payload["记录"] = [_memory_row(memory_service.MemoryRecord.from_dict(one))
                           for one in payload.get("records") or []]
        if write:
            folder = _bundles()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"memories-{_safe_name(ident)}.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            payload["文件"] = str(target)
            payload["说明"] = _saved_note(payload.get("说明") or "", target)
        return _done(payload)
    return _guard(body)


def _safe_name(value: str) -> str:
    import re as _re
    return _re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", str(value or "default"))[:60] or "default"


def _saved_note(base: Any, path: Any) -> str:
    """\u7ed9\u300c\u5df2\u7ecf\u5b58\u6210\u6587\u4ef6\u300d\u8fd9\u4e00\u6b65\u62fc\u4e00\u53e5\u4e0d\u4f1a\u88ab\u6587\u6848\u95f8\u95e8\u5403\u6389\u7684\u8bf4\u660e\u3002

    engine \u90a3\u8fb9\u539f\u6837\u5199\u7684\u662f \u5df2\u5199\u5230\u300cxxx.json\u300d\uff0c\u800c json \u662f \u00a716 \u7981\u6b62\u51fa\u73b0\u5728\u754c\u9762\u4e0a\u7684\u5185\u90e8\u540d\u8bcd\uff0c
    _plain() \u4e00\u770b\u6574\u53e5\u5c31\u6362\u6210\u300c\u8fd9\u4e00\u6b65\u6ca1\u505a\u6210\u300d\u2014\u2014\u5b58\u6210\u529f\u4e86\u5374\u544a\u8bc9\u7528\u6237\u6ca1\u505a\u6210\u3002
    \u8fd9\u91cc\u53ea\u62a5\u540d\u5b57\uff0c\u4e0d\u62a5\u6269\u5c55\u540d\uff0c\u8def\u5f84\u672c\u8eab\u4ecd\u7136\u5728 \u6587\u4ef6 \u90a3\u4e2a\u5b57\u6bb5\u91cc\u7ed9\u51fa\u53bb\u3002
    """
    stem = Path(str(path or "")).stem or "\u6574\u5957\u5907\u4efd"
    head = str(base or "").split("\u5df2\u5199\u5230")[0].strip()
    return f"{head} \u5df2\u7ecf\u5b58\u6210\u300c{stem}\u300d\u8fd9\u4e00\u4efd\uff0c\u968f\u65f6\u80fd\u5728\u4e0b\u9762\u7684\u5907\u4efd\u5217\u8868\u91cc\u6311\u56de\u6765\u3002"


@router.post("/api/memory/import")
def memory_import(req: ImportRequest):
    def body() -> dict[str, Any]:
        data = req.payload if isinstance(req.payload, dict) else _read_json_at(req.path)
        result = memory_service.import_(data, character_id=(req.character_id or None),
                                        merge=bool(req.merge), db_path=_mdb())
        out = dict(result)
        ident = _active_or(req.character_id)
        out["现在用着的条数"] = len(memory_service.list_memories(ident, db_path=_mdb()))
        if out.get("ok"):
            out["说明"] = (f"搬回来了：新增 {out.get('新增', 0)} 条，"
                           f"并到已有 {out.get('并入已有', 0)} 条，"
                           f"跳过 {out.get('跳过', 0)} 条。")
        return _done(out)
    return _guard(body)


@router.post("/api/memory/learn")
def memory_learn(req: LearnRequest):
    """唯一一条「从聊天里自动记」的路：先抽取，再过规则与查重，最后才落库。"""
    def body() -> dict[str, Any]:
        ident = _active_or(req.character_id)
        result = memory_extractor.learn_from_turn(
            req.text or "", ident, session_id=(req.session_id or ""),
            turn_id=int(req.turn_id or 0), db_path=_mdb(), canon_db_path=_cdb(),
            remember_turn=True)
        out = dict(result) if isinstance(result, dict) else {"结果": result}
        out["写入条数"] = len(out.get("写入") or [])
        out["合并条数"] = len(out.get("合并") or [])
        out["重复跳过"] = len(out.get("跳过（重复）") or [])
        out["被规则挡下"] = len(out.get("拒收（设定）") or []) + len(out.get("拒收") or [])
        return _done(out)
    return _guard(body)


# ---------------------------------------------------------------------------
# 5) 五组一屏（界面「记住的事」面板开机用的那一份）
# ---------------------------------------------------------------------------

@router.get("/api/character/overview")
def character_overview(character_id: str | None = Query(None),
                       session_id: str = Query(""), items: int = Query(6)):
    """§16 那五组一次给全：原著资料 / 人物设定 / 长期记忆 / 当前关系 / 本次会话。"""
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        take = max(1, min(int(items or 6), 50))
        profile = store.get_profile(ident, db_path=_cdb())
        facts = canon.list_facts(ident, db_path=_cdb())
        rows = memory_service.list_memories(ident, limit=5000, db_path=_mdb())
        by_group: dict[str, list[dict[str, Any]]] = {name: [] for name in UI_GROUPS}
        for row in rows:
            name = GROUP_OF_LAYER.get(row.layer, "")
            if name in by_group:
                by_group[name].append(_memory_row(row))
        groups = []
        for name in UI_GROUPS:
            if name == "原著资料":
                shown = [_canon_row(one) for one in facts[:take]]
                total = len(facts)
            elif name == "人物设定":
                shown = [_character_row(profile)] if profile is not None else []
                total = len(shown)
            else:
                shown = by_group[name][:take]
                total = len(by_group[name])
            groups.append({"名字": name, "条数": total, "条目": shown})
        return {"ok": True, "编号": ident,
                "人物名称": profile.display_name if profile else ident,
                "台词": store.get_persona(ident, db_path=_cdb()),
                "分组": groups, "能做的事": _verb_index(), "存在哪": locations()}
    return _guard(body)


def _history_rows(raw: Any) -> list[dict[str, str]]:
    """把「一行一条、谁:话」那种写法收成 turns。

    为什么提成一个函数而不是两处各写一遍：装配预览和本轮取话收的是同一个形状，
    写两遍迟早写成两个形状 —— 那时候浏览器就只能挑一个对得上的，另一条就悄悄坏了。
    """
    turns: list[dict[str, str]] = []
    for line in str(raw or "").splitlines():
        row = line.strip()
        if not row:
            continue
        who, _, text = row.partition(":")
        turns.append({"role": (who or "user").strip(), "text": (text or row).strip()})
    return turns


def _turn_judge_live(mode: str | None = None) -> bool:
    """这一条接口现在该不该按「用户回合」那一档走。

    三把开关（总闸、本轮判断闸、模式）一律现场读真人互动层自己的那一份配置，
    这里不留第二份阈值。**任何一把没开就返回 False**：那时这一条接口连用户这句话
    都不往装配里带，回给你的字节跟这一层从来没存在过时一模一样 ——
    关掉那一档不许有哪怕一个字的行为差，是这一层的设计前提，不是优化。

    ``mode``（MODE_SYSTEM_V1）：先把这一轮属于哪个模式绑到互动层，再问它开不开，
    否则它只能读到文件里那把全局开关，模式隔离就漏了这一处。
    """
    try:
        from runtime import human_interaction

        settings = human_interaction.config
        settings.bind_mode(mode)
        return bool(settings.enabled() and settings.turn_judge_enabled()
                    and settings.mode() == human_interaction.MODE_HUMAN_NATURAL)
    except Exception:
        return False


@router.get("/api/character/preview")
def context_preview(q: str = Query(""), character_id: str | None = Query(None),
                    session_id: str = Query(""), history: str = Query("")):
    """装配预览：给她的那一整块内容长什么样，一句一句列出来。

    这一段也证明了两件事：长度是从当前模型声明里读的，适配器只能照单序列化。
    """
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        turns = _history_rows(history)
        context = context_builder.build(character_id=ident, session_id=session_id,
                                        user_text=q, history=turns,
                                        db_path=_cdb(), memory_db_path=_mdb())
        rows = context_builder.messages(context, temporary_state=None)
        return {"ok": True, "编号": ident, "长度上限": context.budget_total,
                "已用长度": context.budget_used,
                "让出去了什么": list(context.budget_dropped or []),
                "给她看的": [{"谁": str(one.get("role") or ""),
                              "内容": str(one.get("text") or one.get("content") or "")}
                             for one in rows],
                "段数": len(rows),
                "说明": (f"这次给她 {len(rows)} 段，长度用了 {context.budget_used} / "
                         f"{context.budget_total}。")}
    return _guard(body)


@router.get("/api/character/prompt")
def character_prompt(q: str = Query(""), character_id: str | None = Query(None),
                     session_id: str = Query(""), turn: str = Query(""),
                     history: str = Query("")):
    """当前这个人「该怎么说话」的一整块话，拼成一条纯文本给浏览器连线时用。

    为什么单独开这一条，而不是让浏览器自己拿 preview 去拼：

      * preview 回的是「一段一段」，块顺序与砍谁都由装配引擎定；这一条只把其中的
        系统段（也就是「她是谁、她怎么说话」那部分）原样接成一整块，浏览器不认得
        块顺序，也就长不出第二套装配逻辑 —— 装配只此一处；
      * 实时语音那条路能接受的只有「一整块提示词」这一个形状（浏览器把它塞进
        session.update 的 instructions），所以对外给的就是这个形状；
      * 用的还是 preview 那一套原语（同一次 build + messages），预算、让位顺序、
        台词一个字都不代写这些规矩全部照旧，不另开口径。

    不传 q 就是「没有对方这句话可参照」的常态（连线时就是这样）：这时长期记忆里
    只有「当前关系」那一层是常带的，别的要等对方开口后再按词去找。q 只是给人试
    一句「这样说她会想起什么」用的，实时链路不传。

    ``turn``（外加可选的 ``history``）是「这一句就是对方刚说的话」那一档：只有真人
    互动层三把开关都开着时才生效（见 ``_turn_judge_live``），走的是同一个
    ``context_builder.build()`` —— 同一套预算、同一套让位顺序、同一套台词，
    不另开第二条装配路 —— 只是把这句话和最近几句一并带进去，让本层在回给浏览器的
    那一块里多出一行「本轮互动判断」。任何一把开关没开，这一档整个不走，
    回给你的字节跟这一层从来没接进来时逐字相同。判断结果只活在这一次响应里：
    不落库、不进记忆、不进人物状态。

    失败一律是 ok:false + 一句人话（没有活动人物、台词露馅、预算读不到都算），
    浏览器拿到 ok:false 就退回自己原来那句通用话，绝不因为这条接口挂了就断线。
    """
    def body() -> dict[str, Any]:
        ident = _active_or(character_id)
        if not ident:
            return _fail("NO_ACTIVE_CHARACTER",
                         "还没有选定人物，先在人物那一栏选一个或新建一个。")
        profile = store.get_profile(ident, db_path=_cdb())
        if profile is None:
            return _fail("NOT_THERE", "没有这个人物的档案，先去人物那一栏把她建出来。")
        turn_text = (turn or "").strip()
        # 模式只从权威那一处读，不收参数：UI 显示什么、后端按什么装配，必须是同一个值。
        try:
            from runtime.mode import mode_authority
            active_mode = mode_authority.current()
        except Exception:
            active_mode = None
        per_turn = bool(turn_text) and _turn_judge_live(active_mode)
        try:
            if per_turn:
                context = context_builder.build(
                    character_id=ident, session_id=session_id, user_text=turn_text,
                    history=_history_rows(history), query=turn_text, run_turn_judge=True,
                    db_path=_cdb(), memory_db_path=_mdb(), mode=active_mode)
            else:
                context = context_builder.build(character_id=ident, session_id=session_id,
                                                user_text="", history=(),
                                                query=(q or "").strip(),
                                                db_path=_cdb(), memory_db_path=_mdb(),
                                                mode=active_mode)
        except context_builder.PersonaStyleError as exc:
            # 台词不合格：装配引擎会整块拒绝，这里翻译成一句「先改台词」，
            # 而不是让 _guard 兜成一句笼统的服务器失败，用户看不出该动哪里。
            return _fail("PERSONA_STYLE",
                         "台词里有几句一开口就露出设定痕迹，先照人物面板的提醒改掉。",
                         提醒=list(exc.issues)[:6])
        rows = context_builder.messages(context, temporary_state=None)
        # 只取系统段：user / assistant 那几段是「这一句怎么说」，不属于常驻话术。
        parts = [str(one.get("text") or one.get("content") or "").strip()
                 for one in rows if str(one.get("role") or "") == "system"]
        text = "\n\n".join(one for one in parts if one).strip()
        if not text:
            return _fail("EMPTY_CHARACTER",
                         "这个人现在一句可说的话都没有，先去人物那一栏写几句台词。")
        persona = store.get_persona(ident, db_path=_cdb()).strip()
        return {"ok": True, "编号": ident,
                "人物名称": profile.display_name or profile.nickname or ident,
                "台词": persona, "内容": text, "字数": len(text),
                "模式": str(active_mode or ""),
                "已用长度": context.budget_used, "长度上限": context.budget_total,
                "让出去了什么": list(context.budget_dropped or []),
                "说明": (f"这次要说的话一共 {len(text)} 字，"
                         f"长度用了 {context.budget_used} / {context.budget_total}。")}
    return _guard(body)


@router.get("/api/mode")
def mode_state():
    """当前模式 + 三个模式各自能不能点。

    这是模式体系**唯一**的读取口：UI 显示什么、后端按什么装配，都从这里出去，
    所以两边不可能出现 split-brain。localStorage 只用来记住上次选了哪一项，
    它说的从来不算事实。
    """
    def body() -> dict[str, Any]:
        try:
            from runtime.mode import mode_authority
            st = mode_authority.state()
            return {"ok": True, "模式": mode_authority.current(),
                    "模式列表": mode_authority.available(),
                    "第三模式已实现": False,
                    "改动时间": str(st.get("updated_at") or ""),
                    "第几次改动": int(st.get("revision") or 0)}
        except Exception as exc:
            return {"ok": False, "reason": "MODE_LAYER_UNAVAILABLE",
                    "说明": f"模式层没起来：{type(exc).__name__}",
                    "模式": "ROLEPLAY", "模式列表": [], "第三模式已实现": False}
    return body()


class ModeRequest(BaseModel):
    mode: str


@router.post("/api/mode")
def mode_switch(req: ModeRequest):
    """切换模式。切完**当场**返回新装配好的一整块提示词，调用方拿去 updateSession。

    为什么顺手把提示词带回来：以前换人物之后当前 live session 未必立刻重新应用
    instructions，得刷新页面才生效。模式切换绝对不能继承这个问题——所以这里不只会
    写权威状态，还会立刻按新模式重新 build 一次，让「切完就生效」是一次请求里
    完成的事，不靠调用方记得再去问一次。

    第三模式在这里被挡住：它 implemented=false，按下它不进任何半成品。
    """
    def body() -> dict[str, Any]:
        try:
            from runtime.mode import mode_authority
        except Exception as exc:
            return {"ok": False, "reason": "MODE_LAYER_UNAVAILABLE",
                    "说明": f"模式层没起来：{type(exc).__name__}"}
        result = mode_authority.set_mode(req.mode, updated_by="ui")
        if not result.get("ok"):
            return result
        new_mode = str(result.get("mode") or "")
        payload: dict[str, Any] = {
            "ok": True, "模式": new_mode, "上一个模式": result.get("previous"),
            "模式列表": mode_authority.available(),
            "说明": str(result.get("说明") or ""),
        }
        ident = _active_or(None)
        if ident:
            try:
                context = context_builder.build(character_id=ident, session_id="",
                                                user_text="", history=(), query="",
                                                db_path=_cdb(), memory_db_path=_mdb(),
                                                mode=new_mode)
                rows = context_builder.messages(context, temporary_state=None)
                parts = [str(one.get("text") or one.get("content") or "").strip()
                         for one in rows if str(one.get("role") or "") == "system"]
                payload["提示词"] = "\n\n".join(one for one in parts if one).strip()
                payload["字数"] = len(payload["提示词"])
            except Exception as exc:
                payload["提示词"] = ""
                payload["提示词取不到"] = type(exc).__name__
        return payload
    return body()


@router.get("/api/character/budget")
def budget_state():
    def body() -> dict[str, Any]:
        budget = context_builder.recommended_budget()
        caps = context_builder.section_caps(int(budget["budget_total"]))
        return {"ok": True, "总长度": budget["budget_total"], "来自哪": budget["来源"],
                "模型窗口": budget.get("上下文窗口", 0), "每块多少字": caps,
                "口径来源": rules.rule_sources()}
    return _guard(body)


__all__ = ["router", "configure", "reset_locations", "locations", "MEMORY_VERBS",
           "CHARACTER_ACTS", "UI_GROUPS", "KIND_LABELS", "GROUP_OF_LAYER", "CONFIRM"]


# V3 compatibility uses the original builder/judge; no new persona or memory store.
class DynamicPreferenceRequest(BaseModel):
    enabled: bool

class DynamicTurnRequest(BaseModel):
    text: str
    character_id: str = ""
    history: str = ""

@router.get("/api/interaction/dynamic")
def dynamic_preference_get():
    from runtime.human_interaction.v3 import preference
    return preference()

@router.post("/api/interaction/dynamic")
def dynamic_preference_set(req: DynamicPreferenceRequest):
    from runtime.human_interaction.v3 import set_preference
    return set_preference(req.enabled)

@router.post("/api/interaction/turn")
def dynamic_turn(req: DynamicTurnRequest):
    from runtime.human_interaction.v3 import build_turn
    from runtime.mode import mode_authority
    ident = _active_or(req.character_id or None)
    # Capture identity; if user changes character/mode/preferences while waiting,
    # frontend rejects the result and the next turn starts with fresh state.
    return build_turn(lambda text, history: character_prompt(
        q="", character_id=ident, session_id="", turn=text, history=history),
        req.text[:12000], req.history[-24000:], mode_authority.current(), ident)
