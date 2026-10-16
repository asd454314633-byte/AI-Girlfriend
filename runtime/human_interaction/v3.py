"""V3 opt-in compatibility. Legacy judge and assembly remain authoritative."""
import json,os,threading,time
from pathlib import Path
from . import config,turn_judge
PREFERENCE_PATH=Path(__file__).with_name("dynamic_v3.json")
_LOCK=threading.Lock()
def preference():
    try:
        data=json.loads(PREFERENCE_PATH.read_text(encoding="utf-8"))
        enabled=data.get("enabled") is True
    except (OSError,ValueError,TypeError):enabled=False
    return {"ok":True,"enabled":enabled,"experimental":True,"style":"human_natural",
            "scope":"typed_chat","performance_approved":False}
def set_preference(enabled):
    with _LOCK:
        tmp=PREFERENCE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema":"dynamic-interaction-v3/1","enabled":bool(enabled)},ensure_ascii=False),encoding="utf-8")
        os.replace(tmp,PREFERENCE_PATH)
    return preference()
def build_turn(build,text,history,mode,character_id):
    started=time.perf_counter()
    baseline=build("", "")
    if not baseline.get("ok") or not preference()["enabled"] or mode!="ROLEPLAY":
        return {**baseline,"dynamic":{"used":False,"reason":"OFF_OR_NOT_CHAT"}}
    result={}
    try:
        with config.request_judge_scope() as result:
            candidate=build(text,history)
        judgment=result.get("judgment") or {}
        # Require all six declared fields. Incomplete results safely use baseline.
        valid=judgment.get("ok") and len(judgment.get("fields",{}))==6 and all(judgment["fields"].values())
        valid=valid and candidate.get("ok") and turn_judge.HEADER in candidate.get("内容","")
        reason="USED" if valid else str(judgment.get("reason") or "INVALID_JUDGMENT")[:100]
    except Exception:
        candidate=baseline;judgment={};valid=False;reason="JUDGE_ERROR"
    if not preference()["enabled"]:valid=False;reason="DISABLED_DURING_REQUEST"
    # No raw prompt, persona, memory, judge values or conversation in diagnostics.
    meta={"used":bool(valid),"reason":reason,"character_id":character_id,
          "judge_ms":round(float(judgment.get("wall_s") or 0)*1000,2),
          "prepare_ms":round((time.perf_counter()-started)*1000,2),
          "reasoning_chars":judgment.get("reasoning_chars",0),
          "judge_output_chars":len(judgment.get("raw") or ""),"fields_filled":sum(bool(v) for v in judgment.get("fields",{}).values())}
    return {**(candidate if valid else baseline),"dynamic":meta}
