# -*- coding: utf-8 -*-
"""装配一份能直接交给模型的上下文（ModelContext）。

块顺序是固定的，写死在这里，谁都不许调：

  1. 身份（档案里那几句）
  2. 人物台词（用户自己写的，本模块一个字都不生成）
  3. 设定摘要 + 从小说里检索到的原文
  4. 关系
  5. 检索到的长期记忆
  6. 这一场能做什么（工具能力）
  7. 作者官方 Sola V2 Ember 规则块（独立开关；关着时这块整个不出现）
  8. 动态临时状态（放在最后，紧跟在用户这句话前面）

三条硬规矩：
  * 模型适配器只能序列化，不许自己拼人物台词：本模块给出 :func:`messages`，
    适配器把它逐条转成请求体就行；
  * 人物台词读起来必须是人在说话，不能出现「根据我的人物设定…」「作为某某角色…」
    「我的性格是…」这类一开口就露设定痕迹的说法，也不许要求每轮复述总结、说教一堆
    「不许做什么」、拿模板句凑字数；
  * 预算从清单里读（recommended_context_budget），这里不写死任何数字。

给用户看的文字里不出现那几个技术词：一律说人话。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from . import canon, role_state_bridge, rules, store
from runtime.memory import retrieval, service as memory_service

try:
    from . import contracts as _contracts  # type: ignore
except ImportError:  # pragma: no cover
    from runtime.core.providers import contracts as _contracts  # type: ignore

ModelContext = _contracts.ModelContext
TurnRef = _contracts.TurnRef

_CJK_RE = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]")


class ContextBudgetUnavailable(RuntimeError):
    """读不到预算就不装配：宁可不出话，也不要拍一个数字。"""


class PersonaStyleError(RuntimeError):
    """人物台词不合格：把问题一条条说清楚，让人去改台词，而不是偷偷改写。"""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = list(issues)
        super().__init__("；".join(self.issues))


def _estimates() -> tuple[float, float]:
    """长度估算器的口径也来自 context_policy.json，不在代码里写死。"""
    row = rules.policy()["token_estimate"]
    return (float(row.get("cjk_per_token") or 1.0), float(row.get("ascii_per_token") or 0.28))


def estimate_tokens(text: str | None) -> int:
    per_cjk, per_ascii = _estimates()
    body = text or ""
    cjk = len(_CJK_RE.findall(body))
    other = len(body) - cjk
    return int(round(cjk * per_cjk + other * per_ascii)) + 1


def chars_per_token() -> float:
    """中文按字算、英文按四个字母算，混在一起时一块能塞多少「字」。"""
    per_cjk, per_ascii = _estimates()
    return 1.0 / max(per_cjk, per_ascii, 0.01)


def section_caps(budget_total: int) -> dict[str, int]:
    """每一块最多多少字 = 预算总数 × 声明的比例（§18：代码里不出现具体数字）。"""
    per = chars_per_token()
    return {name: rules.chars_for(budget_total, name, per_token=per)
            for name in rules.policy()["section_shares"]}


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------

def _registry():
    from runtime.core.providers import registry  # 延迟导入：只有要预算时才用得到
    return registry


def recommended_budget(*, provider_id: str | None = None,
                       refresh: bool = False) -> dict[str, Any]:
    """从 llm 清单里读该用多少预算。

    顺序：指定模型的能力项 -> 当前选中的模型的能力项 -> 清单里的公共默认值。
    三条都读不到就报错，不猜。
    """
    registry = _registry()
    manifest = registry.load_manifest("LLM", refresh=refresh)
    rows = list(manifest.get("providers") or [])
    picked = None
    if provider_id:
        picked = registry.get("LLM", provider_id)
    if picked is None:
        active, _error = registry.active_id("LLM")
        picked = registry.get("LLM", active)
    if picked is None and len(rows) == 1:
        picked = rows[0]
    budget = ((picked or {}).get("capabilities") or {}).get("recommended_context_budget")
    origin = "选中的模型" if budget else ""
    if not budget:
        budget = (manifest.get("shared_capabilities") or {}).get("recommended_context_budget")
        origin = "清单公共默认"
    if not budget:
        raise ContextBudgetUnavailable("RECOMMENDED_CONTEXT_BUDGET_UNDECLARED")
    try:
        value = int(budget)
    except (TypeError, ValueError) as exc:
        raise ContextBudgetUnavailable("RECOMMENDED_CONTEXT_BUDGET_NOT_INT") from exc
    if value <= 0:
        raise ContextBudgetUnavailable("RECOMMENDED_CONTEXT_BUDGET_NON_POSITIVE")
    return {"budget_total": value, "来源": origin,
            "上下文窗口": int(((picked or {}).get("capabilities") or {})
                                  .get("context_window") or 0)}


def enabled_tools(*, refresh: bool = False) -> list[dict[str, Any]]:
    """这一场能做什么：只从工具清单里读，不写死。"""
    try:
        registry = _registry()
        rows = registry.providers("TOOL", enabled_only=True, refresh=refresh) \
            if "refresh" in registry.providers.__code__.co_varnames[:3] \
            else registry.providers("TOOL", enabled_only=True)
    except Exception:
        return []
    out = []
    for row in rows:
        name = str(row.get("display_name_zh") or row.get("display_name") or "").strip()
        if not name:
            continue
        caps = row.get("capabilities") or {}
        out.append({"name": name,
                    "max_results": caps.get("max_results"),
                    "max_chars": caps.get("max_chars")})
    return out[:int(rules.policy()["tool_note_cap"])]


# ---------------------------------------------------------------------------
# 各个块
# ---------------------------------------------------------------------------

def profile_block(profile: Any) -> str:
    """档案里的几句身份话。只搬事实，不加评论、不加指令。"""
    if profile is None:
        return ""
    lines: list[str] = []
    name = (profile.display_name or profile.nickname or profile.character_id or "").strip()
    if name:
        nick = (profile.nickname or "").strip()
        lines.append(f"你叫{name}" + (f"，他也叫你{nick}。" if nick and nick != name else "。"))
    if profile.relationship:
        lines.append(f"你和说话的人：{profile.relationship.strip().rstrip('。')}。")
    if profile.pronoun:
        lines.append(f"别人提到你的时候用「{profile.pronoun.strip()}」。")
    if profile.speaking_style:
        lines.append(f"你怎么说话：{profile.speaking_style.strip().rstrip('。')}。")
    slots = rules.cap_int("profile_list_items", 4)
    for item in (profile.likes or [])[:slots]:
        lines.append(f"你偏爱：{str(item).rstrip('。')}。")
    for item in (profile.dislikes or [])[:slots]:
        lines.append(f"你不爱：{str(item).rstrip('。')}。")
    if profile.boundary:
        lines.append(f"有些话你不接：{profile.boundary.strip().rstrip('。')}。")
    return "\n".join(lines)


def persona_block(character_id: str | None, *, strict: bool = True,
                  db_path: Any = None) -> str:
    """人物台词：原样取用，一个字都不改写、不补写。"""
    text = store.get_persona(character_id, db_path=db_path).strip()
    if strict and text:
        issues = store.persona_issues(text)
        if issues:
            raise PersonaStyleError(issues)
    return text


def policy_block(character_id: str | None = None, *, db_path: Any = None,
                 extra_lines: Sequence[str] = ()) -> str:
    """系统规则那一块（§17 第 1 项）：内容全在 style_rules.json 里，代码只负责渲染。

    这一块的文案本身也要过一遍「不许出现内部名词」的口径，写漏了就当场报出来，
    免得规则文件里混进「prompt / 数据库」这种词，最后被原样念给用户听。
    """
    profile = store.get_profile(character_id, db_path=db_path)
    name = ""
    if profile is not None:
        name = (profile.display_name or profile.nickname or "").strip()
    text = rules.render_policy(name, extra_lines=extra_lines)
    leaked = rules.find_banned(text)
    if leaked:
        raise PersonaStyleError([f"系统规则文案里出现了不该露出来的词：{'、'.join(leaked)}"])
    return text


def summary_block(character_id: str, session_id: str, *, db_path: Any = None) -> str:
    """这一场的近况摘要（§17 第 6 项）：只读记忆模块算好的那份，不在这里现编。"""
    if not session_id:
        return ""
    from runtime.memory import summary as memory_summary  # 延迟导入：避免环
    return str(memory_summary.current(character_id, session_id, db_path=db_path) or "")


def fit_chars(text: str, max_chars: int) -> str:
    """把一块内容压进它那一格：整句砍，不从句子中间拦腰切。"""
    body = str(text or "")
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    floor = float(rules.cap("fit_cut_min_ratio", 0.5))
    cut = body[:max_chars]
    for mark in ("\n", "。", "！", "？", "；", "，"):
        index = cut.rfind(mark)
        if index >= max_chars * floor:
            return cut[:index + 1].rstrip()
    return cut.rstrip() + "…"


def lines_within(lines: Sequence[str], max_chars: int) -> list[str]:
    """一条条往上加，加到超过这一格就停：长的一条宁可整条不要，也不切碎。"""
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(str(line)) + 1
        if out and used + cost > max_chars:
            break
        out.append(str(line))
        used += cost
    return out


def canon_block(character_id: str | None, *, max_chars: int,
                db_path: Any = None) -> tuple[str, int]:
    """设定摘要 + 从小说里检索到的原文，同属一个块，顺序不可换。"""
    summary = canon.summarize(character_id, max_chars=max_chars, db_path=db_path)
    return summary, estimate_tokens(summary)


def canon_passage_block(passages: Sequence[Any], *, limit: int) -> list[str]:
    lines: list[str] = []
    for passage in list(passages)[:limit]:
        text = _clean_quote(passage.text)
        if not text:
            continue
        where = " / ".join(part for part in (passage.title, passage.chapter) if part)
        lines.append(f"原著里写过（{where}）：{text}" if where else f"原著里写过：{text}")
    return lines


def memory_block(passages: Sequence[Any], *, limit: int) -> list[str]:
    lines: list[str] = []
    for passage in list(passages)[:limit]:
        text = (passage.text or "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(f"你之前说过：{text}")
    return lines


def relationship_block(character_id: str | None, *, db_path: Any = None,
                       limit: int = 0) -> str:
    rows = memory_service.list_memories(character_id, layers=("RELATIONSHIP",),
                                        limit=limit or rules.cap_int("relationship_items", 3),
                                        db_path=db_path)
    lines = [f"{str(row.text).rstrip('。')}。" for row in rows]
    return "\n".join(lines)


def tool_block(tools: Sequence[dict[str, Any]] | None = None) -> str:
    """能力说明：说清「能查什么、要不要查」，不提任何提供方名字。"""
    rows = list(tools if tools is not None else enabled_tools())
    if not rows:
        return ""
    names = "、".join(str(row.get("name") or "") for row in rows if row.get("name"))
    lines = [f"手边能用的：{names}。"]
    lines.append("不确定她说过什么、或者原著里怎么写的时候先去查，查到了再顺着说；"
                 "没查到就照当下的话接，别编。")
    return "\n".join(lines)


def _clean_quote(text: str | None, *, max_chars: int = 0) -> str:
    body = re.sub(r"\s*\n\s*", " ", str(text or "")).strip()
    body = re.sub(r"^#{1,6}\s*", "", body)
    body = re.sub(r"^\S{0,24}?(第[一二三四五六七八九十百千零〇两0-9]{1,9}[章回节卷篇折][^\n]{0,24})\s*",
                  "", body)
    limit = int(max_chars or rules.cap_int("quote_max_chars", 190))
    floor = float(rules.cap("quote_cut_min_ratio", 0.55))
    if limit > 0 and len(body) > limit:
        cut = body[:limit]
        for mark in ("。", "！", "？", "；", "，"):
            index = cut.rfind(mark)
            if index >= limit * floor:
                cut = cut[:index + 1]
                break
        body = cut.rstrip() + "…"
    return body.strip()


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def _attach_interaction(base: str, *, run_turn_judge: bool = False,
                        mode: str | None = None, **inputs: Any) -> str:
    """真人互动层在现有生产代码里的**唯一**接入口。

    为什么写成一个自带保险的小函数：

      * 模块装不起来（目录被删、语法坏了）就当作没这一层，原样退回 ——
        「关掉之后旧路径不许有新依赖」这条验收要靠结构成立，不是靠测试碰巧过；
      * 这一层内部任何一步抛错也绝不能把装配带崩，所以异常一律吞在这里，
        退回传进来的那块话；
      * 除了这一处，本仓库其他地方不许再调用它（设计说明 §6）。

    ``mode``（MODE_SYSTEM_V1）：真人互动层现在也归模式管。ROLEPLAY 要它 —— 它自带
    那句通道事实（「她这一轮的话是要被人念出来的：别写括号里的动作、神情、旁白」）
    正是角色扮演需要的输出约束；READING 不要它 —— 朗读那一路根本不过 LLM，
    回答问题那一档也不需要再叠一层「这一轮怎么接」。模式层读不出来时按「不要」处理。

    ``run_turn_judge`` 是「本轮互动判断」那一发唯一的开关，默认 ``False``：
    只有被指定为用户回合的那一次装配会传 ``True``，预览、测试台这些一律不走。
    True 也不等于一定发 —— 还要看本层自己那三把开关（总闸、判断器闸、模式），
    判断口径全部从 ``human_interaction.config`` 现场读，这里不留第二份阈值。
    判断结果只作为 ``turn_judgment`` 交给本层现成的 ``engine.attach``：
    这一层不判断、不写库，也不为判断器另开一条装配路（同一份 ``inputs`` 直接喂进去，
    入参形状与 ``turn_judge.judge()`` 一一对应）。任何一步不顺利就当没这一发，
    退回关掉那一档的字节。
    """
    try:
        from runtime import human_interaction
        human_interaction.config.bind_mode(mode)
    except Exception:
        return base
    judgment: Any = None
    if run_turn_judge:
        try:
            settings = human_interaction.config
            if (settings.enabled() and settings.turn_judge_enabled()
                    and settings.mode() == human_interaction.MODE_HUMAN_NATURAL
                    and str(inputs.get("user_text") or "").strip()):
                from runtime.human_interaction import turn_judge
                judgment = turn_judge.judge(**inputs)
                settings.record_judgment(judgment)
        except Exception:
            judgment = None
    try:
        if judgment is None:
            return human_interaction.engine.attach(base, **inputs)
        return human_interaction.engine.attach(base, turn_judgment=judgment, **inputs)
    except Exception:
        return base


def _sola_block(character: str | None, character_db: Any, *, mode: str | None = None) -> str:
    """作者官方 Sola V2 Ember 规则块在本仓库里的**唯一**接入口。

    和 ``_attach_interaction`` 同一套自带保险的写法：

      * 这一层没装起来（目录被删、冻结件坏、宏解析抛错）就当没有这一块，原样退回空串；
      * 开关关着时它也返回空串 —— 空串进不了 ``system_blocks()`` 的拼接，
        所以「关掉这一层」在字节层面等于「这一层从来没存在过」（验收项 OFF_PATH_IDENTICAL）；
      * 这一块的文本一个字都不在这里生成：全部来自
        ``runtime/character/sola_v2_ember/source/`` 那份只读冻结的官方原始件，
        由该包按 Ember 的模块启停顺序渲染出来。

    除了这一处，本仓库其他地方不许再调用它。

    **MODE_SYSTEM_V1 加的一把闸**：Ember 能不能进这一条 system，现在由**当前模式**
    说了算（``runtime/mode/mode_config.json`` 里那个模式自己的 ``sola_v2_ember``），
    不再由一个全局开关说了算。这是本轮要根治的那个 Bug 的落点：上一次是一个任务
    把 Ember 全局打开，于是整个 AI 女友所有场景都变成第三人称小说。

    两把闸是**与**的关系：模式说不要，或 Ember 自己那把开关（含
    ``SOLA_V2_EMBER_ENABLED`` 环境变量）说不要，都返回空串。文件一个都没删，
    历史资产照旧躺在 ``runtime/character/sola_v2_ember/``。
    """
    try:
        from runtime.mode import mode_authority
        if not mode_authority.sola_v2_ember_enabled(mode):
            return ""
    except Exception:
        return ""          # 模式层读不出来 = 不敢确定该不该给，宁可不给
    try:
        try:
            from . import sola_v2_ember  # type: ignore
        except ImportError:  # pragma: no cover
            from runtime.character import sola_v2_ember  # type: ignore
        if not sola_v2_ember.enabled():
            return ""
        return sola_v2_ember.system_text(character, db_path=character_db)
    except Exception:
        return ""


def _mode_block(mode: str | None = None) -> str:
    """当前模式自己的那段输出契约文案（ROLEPLAY 的第一人称直接对白 / READING 的朗读纪律）。

    文案一个字都不在这里写，全部来自 ``runtime/mode/prompts/``；读不到就返回空串。
    空串不参与拼接，所以「这一档没有额外契约」时字节与没有模式体系时相同。
    """
    try:
        from runtime.mode import mode_authority
        return mode_authority.mode_prompt_text(mode)
    except Exception:
        return ""


def _reading_block(mode: str | None = None) -> str:
    """READING 模式下才给的「正在朗读的附近原文」。

    它是**文档上下文**，不是用户记忆：块本身就带着那句「不得当成用户的经历写进长期
    记忆」的边界（在 ``reading.documents.reading_state_block`` 里），并且只取当前段
    加前后各几段，绝不把整本几十万字塞进来。
    ROLEPLAY 一律不给 —— 那是两条不同的路，不能互相污染。
    """
    try:
        from runtime.mode import mode_authority
        if not mode_authority.include_reading_context(mode):
            return ""
    except Exception:
        return ""
    try:
        from runtime.reading import documents as reading_documents
        return str(reading_documents.reading_state_block() or "").strip()
    except Exception:
        return ""


def build(*, character_id: str | None = None, session_id: str = "",
          user_text: str = "", turn: Any = None, history: Sequence[dict[str, Any]] = (),
          query: str | None = None, budget_total: int | None = None,
          max_passages: int | None = None, max_memories: int | None = None,
          strict_persona: bool = True,
          include_memories: bool = True, include_canon_passages: bool = True,
          include_tools: bool = True, include_policy: bool = True,
          run_turn_judge: bool = False,
          temporary_state: Any = None, recent_terms: Sequence[str] | None = None,
          db_path: Any = None, memory_db_path: Any = None,
          character_db_path: Any = None, mode: str | None = None) -> ModelContext:
    """装配一份上下文。db_path 是测试用的临时位置，正式跑不用传。

    temporary_state 可以传一个会话对象（只读地从里面取临时状态），也可以直接把
    状态字典传进来。

    ``run_turn_judge`` 默认关着：那是「本轮互动判断」那一发模型调用的唯一入口，
    只有被指定为用户回合的那一次装配才传 ``True``（口径见 ``_attach_interaction``）。
    两处刻意不留参数：
      * 每块能占多少字 —— 只问 section_caps(budget)，budget 来自模型能力声明，
        比例来自 context_policy.json（§18「不许写死数字」）；
      * 先丢谁 —— 同样来自 context_policy.json 的 drop_order。

    ``mode`` 是模式体系的输入（MODE_SYSTEM_V1）。不传 = 问权威（
    ``runtime/mode/mode_authority.current()``），所以任何调用方拿到的都是同一个
    当前模式，长不出第二份「现在是什么模式」。模式决定三件事：作者 Ember 那一块
    给不给、真人互动层那一块给不给、阅读上下文那一块给不给。
    """
    settings = rules.policy()
    try:
        from runtime.mode import mode_authority
        active_mode = str(mode or mode_authority.current() or "").strip().upper() or None
    except Exception:
        active_mode = str(mode or "").strip().upper() or None
    character_db = character_db_path or db_path
    memory_db = memory_db_path or db_path
    character = character_id or store.get_active_character(db_path=character_db)
    budget = int(budget_total) if budget_total else recommended_budget()["budget_total"]
    caps = section_caps(budget)
    limit_passages = int(max_passages if max_passages is not None
                         else settings["passage_limit"])
    limit_memories = int(max_memories if max_memories is not None
                         else settings["memory_limit"])

    profile = store.get_profile(character, db_path=character_db)
    text_policy = (fit_chars(policy_block(character, db_path=character_db),
                             caps.get("policy", 0)) if include_policy else "")
    text_profile = fit_chars(profile_block(profile), caps.get("profile", 0))
    # 台词是用户自己写的：超了自己那一格也不偷偷删，只把这句记进「让出去了什么」
    text_persona = persona_block(character, strict=strict_persona, db_path=character_db)
    text_canon = fit_chars(canon.summarize(character, max_chars=caps.get("canon_summary", 0),
                                           db_path=character_db),
                           caps.get("canon_summary", 0))
    text_relationship = fit_chars(relationship_block(character, db_path=memory_db),
                                  caps.get("relationship", 0))
    text_summary = fit_chars(summary_block(character, session_id, db_path=memory_db),
                             caps.get("session_summary", 0))

    key = (query if query is not None else user_text) or ""
    terms = list(recent_terms) if recent_terms is not None else retrieval.context_terms(history)
    overfetch = rules.cap_int("retrieval_overfetch", 2)
    canon_found: list[Any] = []
    memory_found: list[Any] = []
    if key:
        if include_canon_passages:
            canon_found = canon.canon_passages(key, character, limit=limit_passages + overfetch,
                                               recent_terms=terms, db_path=character_db)
        if include_memories:
            memory_found = memory_service.search(key, character,
                                                 limit=limit_memories + overfetch,
                                                 recent_terms=terms, db_path=memory_db)
    lines_canon = lines_within(canon_passage_block(canon_found, limit=limit_passages),
                               caps.get("canon_passages", 0))
    lines_memory = lines_within(memory_block(memory_found, limit=limit_memories),
                                caps.get("memories", 0))
    note_tools = fit_chars(tool_block(), caps.get("tools", 0)) if include_tools else ""
    text_temporary = _temporary_text(temporary_state)
    if text_temporary:
        text_temporary = "这一场临时约定的事：" + text_temporary

    # 真人互动层：只在「系统规则」那一块尾巴上加一小段「这一轮怎么接」。
    # 总闸关着时这里返回的字符串和传进去的一模一样，块顺序与让位逻辑一律没动。
    # run_turn_judge 一路照原样递下去：这一层要不要多问一次模型，由那里现场读
    # 三把开关决定，这里不提前替它判。
    text_policy = _attach_interaction(
        text_policy, run_turn_judge=run_turn_judge, mode=active_mode,
        character_id=character, session_id=session_id,
        persona_text="\n\n".join(one for one in (text_persona, text_profile) if one),
        temporary_text=text_temporary, scene_text=text_summary,
        relationship_text=text_relationship, memory_lines=lines_memory,
        history=history, user_text=user_text)

    # 模式自己的输出契约（ROLEPLAY 的第一人称直接对白 / READING 的朗读纪律）。
    # 它和 Ember 那一块互斥地解决同一件事：Ember 要求第三人称叙事，模式契约要求
    # 人物本人开口。两者只能有一个在场，由模式配置决定。
    text_mode = _mode_block(active_mode)

    # 作者官方 Sola V2 Ember 规则块：独立的一块，永远排在现有那些块之后。
    # 它不进 drop_order —— 一块要么整块在、要么整块不在，不参与「让位」；
    # 正因如此它的长度必须算进 fixed_cost，否则挤掉了什么就看不见了，等于偷偷超预算。
    text_sola = _sola_block(character, character_db, mode=active_mode)

    # 正在朗读的附近原文：只有 READING 模式有，是文档上下文，不是用户记忆。
    text_reading = _reading_block(active_mode)

    # 固定顺序：系统规则 -> 身份 -> 台词 -> 设定摘要 -> 检索到的设定 -> 关系 ->
    # 记忆 -> 这一场近况 -> 工具 -> 模式契约 -> 阅读上下文 -> Sola（开关关着就没这块）
    # -> 临时状态（块顺序写死在这里，适配器不许调）
    required = [text_profile, text_persona, text_canon, text_relationship, text_summary,
                text_temporary, text_mode, text_reading, text_sola, user_text]
    fixed_cost = sum(estimate_tokens(value) for value in required if value)
    pools: dict[str, list[str]] = {"canon_passages": lines_canon,
                                   "memory_lines": lines_memory,
                                   "tool_note": [note_tools] if note_tools else []}
    recent = [dict(item) for item in history]

    def pool_cost() -> int:
        rows = sum((list(value) for value in pools.values()), [])
        return (sum(estimate_tokens(row) for row in rows)
                + sum(estimate_tokens(str(item.get("text") or item.get("content") or ""))
                      for item in recent))

    def history_cost() -> int:
        return sum(estimate_tokens(str(item.get("text") or item.get("content") or ""))
                   for item in recent)

    dropped: list[str] = []

    # 最近对话先按自己那一格收（超了从最老的开始让），再进全局兜底
    budget_history_chars = caps.get("history", 0)
    while len(recent) > 1 and history_cost() > budget_history_chars:
        del recent[0]
        dropped.append("history")

    def over() -> bool:
        return fixed_cost + pool_cost() > budget

    def cut(name: str) -> bool:
        """砍掉这一堆里最不靠前的一条，砍光了返回 False。"""
        if name == "history":
            if len(recent) <= 1:
                return False
            keep = max(len(recent) - int(settings["history_kept_on_trim"]), 1)
            del recent[:len(recent) - keep]
            return True
        rows = pools.get(name)
        if not rows:
            return False
        del rows[len(rows) - 1]
        return True

    for name in tuple(settings["drop_order"]):
        if name not in pools and name != "history":
            continue
        guard = 0
        while over() and guard < rules.cap_int("trim_guard_steps", 64):
            if not cut(name):
                break
            dropped.append(name)
            guard += 1
        if not over():
            break

    if over():
        dropped.append("OVER_BUDGET")
    if text_persona and estimate_tokens(text_persona) > max(1, int(budget
                                                                   * float(settings[
                       "section_shares"].get("persona") or 0.0))):
        dropped.append("PERSONA_OVER_SHARE")

    context = ModelContext(
        character_id=character, session_id=session_id,
        turn=turn if isinstance(turn, TurnRef) else TurnRef(session_id=session_id),
        policy_text=text_policy,
        profile_text=text_profile, persona_text=text_persona,
        canon_text=text_canon, relationship_text=text_relationship,
        summary_text=text_summary,
        memory_lines=pools["memory_lines"], passage_lines=pools["canon_passages"],
        tool_lines=pools["tool_note"], history=recent, user_text=user_text,
        mode_text=text_mode, reading_text=text_reading, sola_text=text_sola,
        budget_total=budget,
    )
    if text_temporary:
        # 契约里最后一个系统块是工具；临时状态必须紧贴用户这句话，所以挂在 history 末尾
        context.history = list(context.history) + [
            {"role": "system", "kind": TEMPORARY_MARKER, "text": text_temporary}]
    context.budget_dropped = [_dropped_label(name) for name in dict.fromkeys(dropped)]
    context.budget_used = _message_cost(context)
    context.provider_id = ""      # 上下文对象里不带任何提供方名字
    return context



TEMPORARY_MARKER = "temporary_state"
_DROPPED_ZH = {
    "history": "靠前的对话先让出去了",
    "canon_passages": "原著里检索到的段落先让出去了",
    "memory_lines": "一些记忆先让出去了",
    "tool_note": "工具能力说明先让出去了",
    "PERSONA_OVER_SHARE": "人物台词比它该占的那一格长：先挤掉别的内容，"
                          "没有偷偷删你的台词，想腾出更多地方就把它改短一点",
    "OVER_BUDGET": "必要内容本身已经超出预算：需要压缩人物台词或设定摘要",
}


def _dropped_label(name: str) -> str:
    return _DROPPED_ZH.get(name, name)


def temporary_state_of(context: ModelContext) -> str:
    """把临时状态块从上下文里读回来（给界面显示「她现在按什么分寸说话」用）。"""
    for item in context.history:
        if str(item.get("kind") or "") == TEMPORARY_MARKER:
            return str(item.get("text") or "")
    return ""


def _message_cost(context: ModelContext) -> int:
    overhead = rules.cap_int("history_row_overhead", 2)
    total = estimate_tokens(context.system_text()) + estimate_tokens(context.user_text)
    for item in context.history:
        if str(item.get("kind") or "") == TEMPORARY_MARKER:
            total += estimate_tokens(str(item.get("text") or ""))
            continue
        total += estimate_tokens(str(item.get("text") or item.get("content") or "")) + overhead
    return total


def messages(context: ModelContext, *, temporary_state: Any = None) -> list[dict[str, Any]]:
    """唯一的序列化出口：适配器只能把这份列表逐条转成请求体，不许自己拼。

    顺序就是要求的块顺序，临时状态永远排在用户这句话前面。
    """
    out: list[dict[str, Any]] = []
    system_text = context.system_text()
    if system_text:
        out.append({"role": "system", "text": system_text})
    has_temporary = bool(temporary_state_of(context))
    for item in context.history:
        row = dict(item)
        if str(row.get("kind") or "") == TEMPORARY_MARKER:
            row.pop("kind", None)
        out.append(row)
    if temporary_state and not has_temporary:
        tail = _temporary_text(temporary_state)
        if tail:
            out.append({"role": "system", "text": "这一场临时约定的事：" + tail})
    if context.user_text:
        out.append({"role": "user", "text": context.user_text})
    return out


def _temporary_text(temporary_state: Any) -> str:
    if not temporary_state:
        return ""
    if isinstance(temporary_state, str):
        return temporary_state
    return role_state_bridge.describe(temporary_state)
