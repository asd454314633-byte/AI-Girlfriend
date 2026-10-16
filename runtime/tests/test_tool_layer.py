"""CORE CONTRACT tests for the Tool Layer (任务 §22/§23/§25) — offline, no network, no GPU.

Run::

    runtime/core/venv/scripts/python.exe runtime/tests/test_tool_layer.py

Three families of checks:

* **registry-driven** — the manifest is the only declaration source; a new tool row
  changes the schema and the executable set with *zero* code edits, and a disabled row
  answers with its declared reason instead of being silently skipped;
* **permissions (attacker's point of view)** — every classic Windows path-escape
  trick is thrown at ``allowed_roots``: ``..`` traversal, absolute paths, drive-letter
  case, forward slashes, UNC, ``\\\\?\\``/``\\\\.\\`` device namespaces, drive-relative
  paths, alternate data streams, trailing dots/spaces, reserved device names,
  wildcards, NUL bytes, a directory **junction** and a **symlink** that point out of
  the root, plus secrets that sit *inside* an allowed root.  Each one must come back
  as a structured verdict, never an exception;
* **end to end** — a fake model asks for a tool in a real wire shape, the layer
  validates/judges/executes/times out, the answer is rendered back for that transport
  and folded into ``ModelContext.tool_lines``, and the real
  ``providers.adapters.fake_llm_adapter`` then receives it.

The sandbox roots live in a temp directory so nothing here touches user data.  The
junction/symlink targets are throwaway temp files: a test never aims a reparse point
at a real system directory, because cleanup must never be able to delete through one.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from queue import Queue
from threading import Event

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT = RUNTIME.parent
for candidate in (PROJECT, RUNTIME / "core"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from providers import registry  # noqa: E402
from providers import tool_layer as tools  # noqa: E402
from providers.contracts import ModelContext, ToolCall  # noqa: E402
from providers.tool_layer import fakes, fs_guard, transport  # noqa: E402
from providers.tool_layer.spec import load_payload  # noqa: E402

LAYER_DIR = RUNTIME / "core" / "providers" / "tool_layer"

# ---------------------------------------------------------------- fixture sandbox
_SANDBOX: str | None = None
# Project-local, not tempfile under C:\Users: the manifest forbids ``C:\Users`` as a
# root, and a fixture that had to fight that rule would prove nothing.
SANDBOX_DIR = RUNTIME / "tests" / "_tool_layer_sandbox"


def sandbox() -> str:
    """read_root (whitelisted, read-only) + scratch_root (whitelisted, writable) +
    outside_root (NOT whitelisted; reparse points aim here, never at C:\\Windows)."""
    global _SANDBOX
    if _SANDBOX is None:
        root = os.path.realpath(str(SANDBOX_DIR))
        for name in ("read_root", "scratch_root", "outside_root"):
            os.makedirs(os.path.join(root, name), exist_ok=True)
        with open(os.path.join(root, "read_root", "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("林晚棠站在天台边上，风把她的衣角吹得笔直。")
        os.makedirs(os.path.join(root, "read_root", "nested", ".ssh"), exist_ok=True)
        with open(os.path.join(root, "read_root", "nested", ".ssh", "id_rsa"), "w",
                  encoding="utf-8") as handle:
            handle.write("SECRET-DO-NOT-HAND-TO-A-MODEL")
        with open(os.path.join(root, "read_root", "api.key"), "w", encoding="utf-8") as handle:
            handle.write("SECRET-KEY")
        with open(os.path.join(root, "read_root", "script.py"), "w", encoding="utf-8") as handle:
            handle.write("print('source code is not model content')")
        with open(os.path.join(root, "outside_root", "outside.txt"), "w", encoding="utf-8") as handle:
            handle.write("OUTSIDE-THE-WHITELIST")
        _SANDBOX = root
    return _SANDBOX


def sandbox_root(name: str, *parts: str) -> str:
    return os.path.join(sandbox(), name, *parts)


def sandbox_read_path(*parts: str) -> str:
    return os.path.join(sandbox_root("read_root"), *parts)


FIXTURE_ROWS: list[dict] = [
    {
        "id": "fixture_file_read",
        "display_name_zh": "夹具：读白名单文件",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fs_tools",
        "entrypoint": "read_file",
        "allowed_roots": [sandbox_root("read_root")],
        "capabilities": {"whole_disk_read": {"supported": False, "policy": "FORBIDDEN"},
                         "writable": {"supported": False}, "timeout_s": 1.0},
        "arguments_schema": {
            "path": {"type": "string", "required": True, "format": "path"},
            "max_bytes": {"type": "integer", "required": False, "default": 4096, "max": 200000},
        },
    },
    {
        "id": "fixture_file_list",
        "display_name_zh": "夹具：列白名单目录",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fs_tools",
        "entrypoint": "list_dir",
        "allowed_roots": [sandbox_root("read_root")],
        "capabilities": {"writable": {"supported": False}, "timeout_s": 1.0},
        "arguments_schema": {
            "path": {"type": "string", "required": False, "default": ".", "format": "path",
                     "expect": "directory"},
            "limit": {"type": "integer", "required": False, "default": 50, "max": 200},
        },
    },
    {
        "id": "fixture_file_write",
        "display_name_zh": "夹具：写白名单文件",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fs_tools",
        "entrypoint": "write_file",
        # declares BOTH roots on purpose: writing into the read-only one must still fail
        "allowed_roots": [sandbox_root("read_root"), sandbox_root("scratch_root")],
        "capabilities": {"writable": {"supported": True}, "timeout_s": 1.0},
        "arguments_schema": {
            "path": {"type": "string", "required": True, "format": "path", "access": "write"},
            "text": {"type": "string", "required": True, "max_length": 4000},
            "overwrite": {"type": "boolean", "required": False, "default": False},
        },
    },
    {
        "id": "fixture_echo",
        "display_name_zh": "夹具：回显",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fakes",
        "entrypoint": "echo",
        "allowed_roots": [],
        "capabilities": {"writable": {"supported": False}, "timeout_s": 1.0},
        "arguments_schema": {"text": {"type": "string", "required": True}},
    },
    {
        "id": "fixture_spy",
        "display_name_zh": "夹具：看注入了什么",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fakes",
        "entrypoint": "spy",
        "allowed_roots": [],
        "capabilities": {"timeout_s": 1.0},
        "arguments_schema": {"note": {"type": "string", "required": False, "default": ""}},
    },
    {
        "id": "fixture_strict",
        "display_name_zh": "夹具：受约束参数",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fakes",
        "entrypoint": "echo",
        "allowed_roots": [],
        "capabilities": {"timeout_s": 1.0},
        "arguments_schema": {
            "mode": {"type": "string", "required": True,
                     "allowed_values": ["calm", "loud"]},
            "limit": {"type": "integer", "required": False, "default": 3, "max": 5},
            "text": {"type": "string", "required": False, "default": "x"},
        },
    },
    {
        "id": "fixture_slow",
        "display_name_zh": "夹具：超时",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fakes",
        "entrypoint": "slow",
        "allowed_roots": [],
        "capabilities": {"timeout_s": 0.2},
        "arguments_schema": {"delay_s": {"type": "number", "required": False, "default": 5}},
    },
    {
        "id": "fixture_boom",
        "display_name_zh": "夹具：抛异常",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fakes",
        "entrypoint": "boom",
        "allowed_roots": [],
        "capabilities": {"timeout_s": 1.0},
        "arguments_schema": {},
    },
    {
        "id": "fixture_row_outside_whitelist",
        "display_name_zh": "夹具：行白名单越权",
        "enabled": True,
        "ui_visible": False,
        "implementation": "providers.tool_layer.fs_tools",
        "entrypoint": "read_file",
        # a row root that the system whitelist does not carry must NOT widen anything
        "allowed_roots": [sandbox_root("outside_root")],
        "capabilities": {"timeout_s": 1.0},
        "arguments_schema": {"path": {"type": "string", "required": True, "format": "path"}},
    },
    {
        "id": "fixture_disabled_row",
        "display_name_zh": "夹具：停用行",
        "enabled": False,
        "ui_visible": False,
        "implementation": None,
        "entrypoint": None,
        "allowed_roots": [],
        "disabled_reason": "夹具：未接线",
        "capabilities": {},
        "arguments_schema": {},
    },
]

PROJECT_ROW = {
    "id": "fixture_project_read",
    "display_name_zh": "夹具：读项目内白名单文件",
    "enabled": True,
    "ui_visible": False,
    "implementation": "providers.tool_layer.fs_tools",
    "entrypoint": "read_file",
    "allowed_roots": [str(PROJECT / "runtime" / "character"),
                      str(PROJECT / "runtime" / "tooling" / "agent_scratch")],
    "capabilities": {"timeout_s": 1.0},
    "arguments_schema": {"path": {"type": "string", "required": True, "format": "path"}},
}


def fixture_payload() -> dict:
    """The *real* manifest plus fixture rows: the only difference is data."""
    payload = copy.deepcopy(load_payload())
    payload.setdefault("providers", []).extend(copy.deepcopy(FIXTURE_ROWS))
    return payload


def sandbox_config(*, allow_writes: bool = True, path: Path | None = None) -> tools.RootsConfig:
    text = path or (Path(sandbox()) / "roots.json")
    payload = {
        "config_version": 1,
        "enabled": True,
        # the sandbox is project-local, so the production rule (no root may leave the
        # project) stays switched on for every one of these checks
        "confine_to_project": True,
        "allow_write_tools": allow_writes,
        "read_only_by_default": True,
        "follow_symlinks": False,
        "max_read_bytes": 200000,
        "roots": [
            {"path": sandbox_root("read_root"), "read": True, "write": False},
            {"path": sandbox_root("scratch_root"), "read": True, "write": True},
        ],
        "forbidden_globs": ["**/.ssh", "**/*.key", "**/id_rsa*", "**/*.py", "**/secrets*"],
        "audit_log": str(Path(sandbox()) / "calls.jsonl"),
    }
    Path(text).write_text(json.dumps(payload), encoding="utf-8")
    return tools.load_config(text, project_root=PROJECT)


_LAYER: tools.ToolLayer | None = None


def layer() -> tools.ToolLayer:
    global _LAYER
    if _LAYER is None:
        _LAYER = tools.ToolLayer(payload=fixture_payload(), config=sandbox_config())
    return _LAYER


def production_layer(*extra_rows: dict) -> tools.ToolLayer:
    """The shipped manifest + the shipped roots.json, with one extra project-local row."""
    payload = copy.deepcopy(load_payload())
    rows = payload.setdefault("providers", [])
    rows.append(copy.deepcopy(PROJECT_ROW))
    rows.extend(copy.deepcopy(item) for item in extra_rows)
    config = tools.load_config(project_root=PROJECT)
    # the shipped whitelist, but the evidence stays in the sandbox: a self-test must
    # not put its own rows into the production audit stream
    config.audit_log = str(Path(sandbox()) / "production_calls.jsonl")
    return tools.ToolLayer(payload=payload, config=config)


# --------------------------------------------------------------------- assertions
def call(tool_id: str, arguments: dict | None = None, transport_name: str = "chat-completions"):
    return layer().execute({"tool_id": tool_id, "arguments": arguments or {}},
                           transport=transport_name)


def expect_denied(outcome, *, code: str = "", contains: str = "") -> None:
    if outcome.ok:
        raise AssertionError(f"expected denial, got ok: {outcome.as_dict()}")
    if code and outcome.reason != code and not outcome.reason.startswith(code):
        raise AssertionError(f"expected reason {code}, got {outcome.reason!r}")
    if contains and contains not in outcome.result.text + outcome.reason:
        raise AssertionError(f"expected {contains!r} in {outcome.result.text!r}")
    if not outcome.reason:
        raise AssertionError("a denial must carry a reason code")


def expect_ok(outcome) -> None:
    if not outcome.ok:
        raise AssertionError(f"expected success, got {outcome.reason!r} / {outcome.result.text!r}")


# ============================================================== registry-driven
def manifest_is_the_only_declaration_source():
    real = registry.providers("TOOL")
    shipped = tools.default_layer()
    assert shipped.ids() == [row["id"] for row in real], shipped.ids()
    assert shipped.ids(enabled_only=True) == [row["id"] for row in real if row.get("enabled")]
    schema_names = [row["function"]["name"] for row in shipped.schemas("chat-completions")["tools"]]
    assert schema_names == shipped.ids(enabled_only=True)
    assert "manifests" not in "".join(schema_names)


def adding_a_tool_row_needs_no_code():
    before = tools.default_layer().ids()
    after = layer().ids()
    assert set(before) < set(after), (before, after)
    assert "fixture_echo" in after and "fixture_file_read" in after
    # ...and the *same* generic code path renders and runs them for every transport
    for name in transport.TRANSPORTS:
        rendered = layer().schemas(name, enabled_only=True)
        flat = json.dumps(rendered, ensure_ascii=False)
        assert "fixture_file_read" in flat, name
    expect_ok(call("fixture_echo", {"text": "你好"}))
    assert call("fixture_echo", {"text": "你好"}).result.text == "你好"


def disabled_row_reports_its_declared_reason():
    outcome = call("web_search", {"query": "今天天气"}, transport_name="responses")
    expect_denied(outcome, code="TOOL_DISABLED")
    assert "本轮不开网" in outcome.result.text, outcome.result.text
    expect_denied(call("fixture_disabled_row", {}), code="TOOL_DISABLED")


def unknown_tool_is_answered_with_the_registered_set():
    outcome = call("no_such_tool", {})
    expect_denied(outcome, code="TOOL_UNKNOWN")
    for tool_id in layer().ids():
        assert tool_id in outcome.result.text, tool_id


def manifest_edit_is_live_without_a_restart():
    first = tools.reload()
    assert first.ids() == tools.reload().ids()
    # the registry caches by mtime: touching the file must be enough to re-read it
    stamp = registry.load_manifest("TOOL")
    assert stamp is registry.load_manifest("TOOL")
    assert registry.load_manifest("TOOL", refresh=True) is not stamp


# ==================================================================== arguments
def missing_required_argument_is_rejected():
    outcome = call("fixture_echo", {})
    expect_denied(outcome, code="TOOL_INVALID_ARGUMENTS")
    assert "MISSING_ARGUMENT:text" in outcome.result.text, outcome.result.text


def argument_types_bounds_and_enums_are_enforced():
    expect_denied(call("fixture_strict", {"mode": "whisper"}), code="TOOL_INVALID_ARGUMENTS")
    expect_denied(call("fixture_strict", {"mode": 7}), code="TOOL_INVALID_ARGUMENTS")
    expect_denied(call("fixture_strict", {"mode": "calm", "limit": "abc"}),
                  code="TOOL_INVALID_ARGUMENTS")
    outcome = call("fixture_strict", {"mode": "calm", "limit": 999})
    expect_ok(outcome)
    assert any(row.startswith("CLAMPED:limit") for row in outcome.audit["notes"]), outcome.audit
    assert outcome.arguments["limit"] == 5
    outcome = call("fixture_strict", {"mode": "loud", "limit": "4"})
    expect_ok(outcome)
    assert outcome.arguments["limit"] == 4
    assert any("COERCED_TO_INTEGER:limit" in row for row in outcome.audit["notes"])
    defaults = call("fixture_strict", {"mode": "calm"})
    expect_ok(defaults)
    assert defaults.arguments == {"mode": "calm", "limit": 3, "text": "x"}


def unknown_arguments_are_dropped_before_the_implementation():
    outcome = call("fixture_echo", {"text": "hi", "whole_disk": True, "root": "C:\\"})
    expect_ok(outcome)
    assert "whole_disk" not in outcome.arguments
    assert any(row == "DROPPED_ARGUMENT:whole_disk" for row in outcome.audit["notes"]), outcome.audit
    assert any(row == "DROPPED_ARGUMENT:root" for row in outcome.audit["notes"])


def arguments_may_arrive_as_a_json_string():
    event = {"id": "c1", "type": "function",
             "function": {"name": "fixture_echo", "arguments": "{\"text\": \"串过来的\"}"}}
    outcome = layer().execute(event, transport="chat-completions")
    expect_ok(outcome)
    assert outcome.result.text == "串过来的"
    assert "ARGUMENTS_WERE_JSON_STRING" in outcome.audit["notes"]


# ==================================================================== transports
def only_the_renderer_knows_the_wire_shape():
    spec = layer().get("fixture_strict")
    chat = transport.tool_schemas([spec], "chat-completions")["tools"][0]
    responses = transport.tool_schemas([spec], "responses")["tools"][0]
    realtime = transport.tool_schemas([spec], "realtime")["tools"][0]
    assert chat["function"]["name"] == "fixture_strict"
    assert responses["name"] == "fixture_strict" and responses["strict"] is False
    assert realtime["name"] == "fixture_strict"
    assert chat["function"]["parameters"]["properties"]["mode"]["enum"] == ["calm", "loud"]
    assert chat["function"]["parameters"]["required"] == ["mode"]
    assert chat["function"]["parameters"]["additionalProperties"] is False
    # the same row, the same schema, minus the protocol wrapper
    assert json.dumps(chat["function"]["parameters"], sort_keys=True) == \
        json.dumps(responses["parameters"], sort_keys=True)
    text = transport.tool_schemas([spec], "text")
    assert text["transport"] == "text" and "fixture_strict" in text["instructions"]
    assert transport.normalize_transport("openai-responses") == "responses"
    assert transport.normalize_transport("proxy") == "text"
    try:
        transport.normalize_transport("no-such-dialect")
    except transport.TransportError as exc:
        assert "Registered:" in str(exc)
    else:
        raise AssertionError("an unknown transport must not be guessed at")


def every_declared_call_shape_parses_and_a_bad_one_does_not_raise():
    shapes = {
        "chat-completions": {"id": "a", "type": "function",
                             "function": {"name": "fixture_echo", "arguments": "{\"text\":\"t\"}"}},
        "responses": {"type": "function_call", "call_id": "b", "name": "fixture_echo",
                      "arguments": {"text": "t"}},
        "realtime": {"type": "response.function_call_arguments.done",
                     "item": {"type": "function_call", "call_id": "c", "name": "fixture_echo",
                              "arguments": "{\"text\":\"t\"}"}},
        "text": {"tool_id": "fixture_echo", "arguments": {"text": "t"}},
    }
    for name, event in shapes.items():
        request = transport.parse_call(event, name)
        assert request.tool_id == "fixture_echo", (name, request.as_dict())
        payload = request.arguments
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload == {"text": "t"}, (name, payload)
        assert transport.parse_call(event, name).call_id in ("a", "b", "c", "")
    for broken in ({"nonsense": True}, "not json", 42, None, []):
        request = transport.parse_call(broken, "chat-completions")
        assert not request.ok and request.error, broken


# ==================================================================== execution
def timeout_is_a_verdict_not_a_hang():
    outcome = call("fixture_slow", {"delay_s": 3})
    expect_denied(outcome, code="TOOL_TIMEOUT")
    assert outcome.audit.get("thread_leaked_until_it_returns") is True
    assert outcome.result.elapsed_ms < 2500, outcome.result.elapsed_ms


def implementation_crash_stays_structured():
    outcome = call("fixture_boom", {})
    expect_denied(outcome, code="TOOL_ERROR:RuntimeError")
    assert "on purpose" in outcome.result.text


def implementations_receive_the_live_guard():
    outcome = call("fixture_spy", {"note": "n"})
    expect_ok(outcome)
    delivered = [name for name in outcome.result.text.split(",") if name]
    assert "guard" in delivered, outcome.result.text
    assert "tool_spec" in delivered, outcome.result.text
    assert "roots_config" in delivered, outcome.result.text
    assert "layer" in delivered, outcome.result.text
    # the injected guard is the *tool-bound* one, so an implementation cannot widen it
    spy_guard = layer().guard(layer().get("fixture_spy"))
    assert spy_guard.roots == []                       # that row declared allowed_roots: []
    assert spy_guard.check("notes.txt").reason == "PATH_NO_EXPLICIT_ROOT"
    read_guard = layer().guard(layer().get("fixture_file_read"))
    assert len(read_guard.roots) == 1
    assert read_guard.check("notes.txt").ok


def audit_line_is_written_per_call():
    before = len(layer().audit)
    outcome = call("fixture_echo", {"text": "记录我"})
    assert len(layer().audit) == before + 1
    row = layer().audit[-1]
    assert row["tool_id"] == "fixture_echo" and row["ok"] and row["code"] == "OK"
    path = Path(sandbox()) / "calls.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["tool_id"] == "fixture_echo"
    assert outcome.transport == "chat-completions"


# ================================================================ allowed_roots
def relative_parent_traversal_is_denied():
    for probe in ("..\\..\\outside_root\\outside.txt", "../../outside_root/outside.txt",
                  "nested/../../outside_root/outside.txt",
                  "..\\..\\..\\..\\..\\..\\Windows\\win.ini",
                  "..\\..\\..\\windows\\system32\\config\\SAM"):
        outcome = call("fixture_file_read", {"path": probe})
        expect_denied(outcome, code="PATH_OUTSIDE_ROOT")
        assert "PARENT_TRAVERSAL_PRESENT" in " ".join(outcome.audit["notes"]), probe


def absolute_paths_outside_the_root_are_denied():
    for probe in (
        sandbox_read_path("notes.txt"),                      # inside: allowed
    ):
        expect_ok(call("fixture_file_read", {"path": probe}))
    for probe in (
        sandbox_root("outside_root", "outside.txt"),
        "C:\\Windows\\win.ini",
        "c:\\windows\\win.ini",                              # drive-letter case
        "C:/Windows/win.ini",                                # forward slashes
        "C:\\WINDOWS\\win.ini",
        "C:\\Windows\\win.ini.",                             # trailing dot
        "C:\\Windows\\SYSTEM~1\\config\\S AM",
        "\\\\?\\C:\\Windows\\win.ini",                       # device namespace
        "\\\\.\\PhysicalDrive0",
        "\\\\fileserv\\share\\report.docx",                  # UNC
        "//fileserv/share/report.docx",
        "C:Windows\\win.ini",                                # drive-relative
        "\\Windows\\win.ini",                                # drive-less absolute
        "C:\\Windows\\win.ini:hidden",                       # alternate data stream
        "notes.txt:hidden::$DATA",
        "C:\\Windows\\System32\\cmd.exe",
        "NUL", "\\\\.\\NUL", "nested\\CON\\x",
        "C:\\Windows\\*.ini", "C:\\Windows\\win.ini?x",
        "C:\\Windows\\\x07win.ini",                           # control character
        "C:\\Windows\\win.ini\x00.txt",
        "C:\\Windows\\" + "x" * 300,
    ):
        outcome = call("fixture_file_read", {"path": probe})
        expect_denied(outcome, code="PATH_")
        assert outcome.result.ok is False


def traversal_out_of_root_and_inside_root_are_told_apart():
    # "read_root/nested/../notes.txt" stays inside the root and must work;
    # "read_root/../../outside_root/x" must not.
    expect_ok(call("fixture_file_read", {"path": "nested/../notes.txt"}))
    assert any("PARENT_TRAVERSAL_PRESENT" in row for row in
               call("fixture_file_read", {"path": "nested/../notes.txt"}).audit["notes"])
    expect_denied(call("fixture_file_read", {"path": "../outside_root/outside.txt"}),
                  code="PATH_OUTSIDE_ROOT")


def secrets_inside_an_allowed_root_are_still_denied():
    for probe, code in (("api.key", "PATH_FORBIDDEN_PATTERN"),
                        ("nested/.ssh/id_rsa", "PATH_FORBIDDEN_PATTERN"),
                        ("script.py", "PATH_FORBIDDEN_PATTERN"),
                        (sandbox_read_path("api.key"), "PATH_FORBIDDEN_PATTERN"),
                        (sandbox_read_path("nested", ".ssh", "id_rsa"), "PATH_FORBIDDEN_PATTERN")):
        expect_denied(call("fixture_file_read", {"path": probe}), code=code)
    # and the sibling that is not a secret still reads fine
    expect_ok(call("fixture_file_read", {"path": "notes.txt"}))


def a_row_cannot_widen_the_system_whitelist():
    outcome = call("fixture_row_outside_whitelist", {"path": "outside.txt"})
    expect_denied(outcome, code="PATH_")
    assert outcome.reason in {"PATH_NO_AVAILABLE_ROOT", "PATH_OUTSIDE_ROOT"}, outcome.reason
    guard = layer().guard(layer().get("fixture_row_outside_whitelist"))
    assert guard.root_dropped and guard.root_dropped[0]["reason"] == "ROOT_NOT_DECLARED_BY_TOOL"


def a_row_may_name_its_root_relative_to_the_project():
    # a row that writes "runtime/tooling/agent_scratch" instead of the absolute form
    # still narrows the whitelist correctly: resolving is not widening
    row = copy.deepcopy(PROJECT_ROW)
    row["id"] = "fixture_relative_root_read"
    row["allowed_roots"] = ["runtime/tooling/agent_scratch"]
    local = production_layer(row)
    guard = local.guard(local.get("fixture_relative_root_read"))
    assert len(guard.roots) == 1, [str(item.path) for item in guard.roots]
    assert guard.roots[0].path.name == "agent_scratch", guard.roots[0].as_dict()
    # narrowing is what a row does: every other system root is dropped, with a reason
    assert {item["reason"] for item in guard.root_dropped} == {"ROOT_NOT_DECLARED_BY_TOOL"}
    expect_ok(local.execute({"tool_id": "fixture_relative_root_read",
                             "arguments": {"path": "README.txt"}}, transport="chat-completions"))
    expect_denied(local.execute({"tool_id": "fixture_relative_root_read",
                                 "arguments": {"path": "../character/search.py"}},
                                transport="chat-completions"), code="PATH_")


def reparse_points_leading_out_of_the_root_are_denied():
    link = sandbox_read_path("junction_escape")
    made_junction = False
    try:
        import _winapi  # noqa: PLC0415
        _winapi.CreateJunction(sandbox_root("outside_root"), link)
        made_junction = True
    except Exception:
        try:
            os.symlink(sandbox_root("outside_root"), link, target_is_directory=True)
            made_junction = True
        except OSError:
            made_junction = False
    if not made_junction:
        print("      (skipped: reparse points cannot be created for this user)")
        return
    try:
        for probe in ("junction_escape/outside.txt", "junction_escape\\..\\junction_escape"):
            expect_denied(call("fixture_file_read", {"path": probe}), code="PATH_")
        outcome = call("fixture_file_read", {"path": "junction_escape/outside.txt"})
        assert outcome.reason in {"PATH_REPARSE_ESCAPE", "PATH_SYMLINK_REFUSED"}, outcome.reason
        # the escape target must still be intact: a verdict, never a deletion
        assert os.path.isfile(sandbox_root("outside_root", "outside.txt"))
    finally:
        try:
            os.rmdir(link)                     # removes the reparse point only
        except OSError:
            os.unlink(link)


def file_symlink_escape_is_denied():
    link = sandbox_read_path("file_escape.txt")
    try:
        os.symlink(sandbox_root("outside_root", "outside.txt"), link)
    except OSError:
        print("      (skipped: symlink creation needs developer mode / privilege)")
        return
    try:
        outcome = call("fixture_file_read", {"path": "file_escape.txt"})
        expect_denied(outcome, code="PATH_")
        assert outcome.reason in {"PATH_REPARSE_ESCAPE", "PATH_SYMLINK_REFUSED"}, outcome.reason
    finally:
        os.unlink(link)


def writes_need_three_explicit_switches():
    writable = layer().get("fixture_file_write")
    assert writable.writable and writable.write_arguments == ("path",)
    # (a) layer switch off
    no_writes = tools.ToolLayer(payload=fixture_payload(),
                                config=sandbox_config(allow_writes=False))
    outcome = no_writes.execute({"tool_id": "fixture_file_write",
                                 "arguments": {"path": "note.txt", "text": "写点东西"}},
                                transport="chat-completions")
    expect_denied(outcome, code="PATH_WRITE_FORBIDDEN_BY_LAYER")
    # (b) root is read-only
    outcome = call("fixture_file_write", {"path": sandbox_read_path("note.txt"), "text": "x"})
    expect_denied(outcome, code="PATH_WRITE_FORBIDDEN_BY_ROOT")
    # (c) a tool that is not declared writable cannot ask for a write anyway
    outcome = call("fixture_file_read", {"path": "notes.txt"})
    expect_ok(outcome)
    guard = layer().guard(layer().get("fixture_file_read"))
    assert guard.check("notes.txt", write=True).reason == "PATH_WRITE_FORBIDDEN_BY_TOOL"
    # all three on -> exactly one directory may receive bytes
    outcome = call("fixture_file_write", {"path": "note.txt", "text": "写点东西"})
    expect_ok(outcome)
    made = sandbox_root("scratch_root", "note.txt")
    assert os.path.isfile(made) and "写点东西" in Path(made).read_text(encoding="utf-8")
    assert "scratch_root" not in outcome.result.text        # model sees a relative name
    # traversal on the write side too
    expect_denied(call("fixture_file_write", {"path": "../outside_root/x.txt", "text": "x"}),
                  code="PATH_OUTSIDE_ROOT")
    expect_denied(call("fixture_file_write", {"path": "C:\\Windows\\x.txt", "text": "x"}),
                  code="PATH_OUTSIDE_ROOT")
    # overwrite is a manifest decision, never a model request
    expect_denied(call("fixture_file_write",
                       {"path": "note.txt", "text": "覆盖", "overwrite": True}),
                  code="PATH_WRITE_OVERWRITE_FORBIDDEN")


def the_model_never_sees_an_absolute_path():
    outcome = call("fixture_file_read", {"path": sandbox_read_path("notes.txt")})
    expect_ok(outcome)
    wire = transport.render_output("chat-completions", call_id="c", tool_id="fixture_file_read",
                                   result=outcome.result)
    assert outcome.arguments["path"].startswith(sandbox_root("read_root"))  # impl gets the real one
    assert sandbox_root("read_root") not in wire["content"], wire
    assert "notes.txt" in wire["content"]
    assert outcome.wire("realtime")["item"]["type"] == "function_call_output"
    assert outcome.wire("responses")["type"] == "function_call_output"


def production_policy_confines_to_the_project():
    local = production_layer()
    scratch_readme = str(PROJECT / "runtime" / "tooling" / "agent_scratch" / "README.txt")
    # an absolute path inside a declared root, and the same file by bare name (the
    # guard shops the whitelist in order), must both read
    expect_ok(local.execute({"tool_id": "fixture_project_read",
                             "arguments": {"path": scratch_readme}},
                            transport="chat-completions"))
    expect_ok(local.execute({"tool_id": "fixture_project_read", "arguments": {"path": "README.txt"}},
                            transport="chat-completions"))
    for probe, code in (
        ("runtime/character/search.py", "PATH_FORBIDDEN_PATTERN"),   # source is not content
        # One ".." from the primary root lands back inside another whitelisted root
        # (runtime/character/library -> runtime/character), so the honest verdict there
        # is PATH_NOT_FOUND, not a containment refusal: assert the denial either way,
        # and use a traversal that leaves *every* declared root for the reason code.
        ("../README_HANDOFF.md", "PATH_"),
        ("../../README_HANDOFF.md", "PATH_OUTSIDE_ROOT"),
        ("C:\\Windows\\win.ini", "PATH_OUTSIDE_ROOT"),
        (str(LAYER_DIR.parent / "manifests" / "tool.json"), "PATH_OUTSIDE_ROOT"),
    ):
        outcome = local.execute({"tool_id": "fixture_project_read", "arguments": {"path": probe}},
                                transport="chat-completions")
        expect_denied(outcome, code=code)
    # A traversal that reaches a file which really exists outside every declared root
    # is the case that matters: "not found" would also be produced by a broken guard,
    # so plant a real file and demand the containment verdict by name.
    planted = PROJECT / "runtime" / "tool_layer_guard_probe.txt"
    planted.write_text("GUARD_PROBE\n", encoding="utf-8")
    try:
        for probe in ("../../tool_layer_guard_probe.txt",
                      "agent_scratch/../../tool_layer_guard_probe.txt"):
            expect_denied(local.execute({"tool_id": "fixture_project_read",
                                         "arguments": {"path": probe}},
                                        transport="chat-completions"),
                          code="PATH_OUTSIDE_ROOT")
        expect_denied(local.execute({"tool_id": "fixture_project_read",
                                     "arguments": {"path": str(planted)}},
                                    transport="chat-completions"),
                      code="PATH_OUTSIDE_ROOT")
    finally:
        planted.unlink(missing_ok=True)
    health = local.health()
    assert health["confine_to_project"] is True
    assert health["allow_write_tools"] is False
    assert health["writable_tools"] == []
    # a whitelist entry outside the project is dropped instead of being honoured
    dropped = tools.load_config(path=_write_outside_project_config(), project_root=PROJECT)
    assert dropped.dropped and dropped.dropped[0]["reason"] == "ROOT_OUTSIDE_PROJECT"
    assert dropped.roots == ()


def _write_outside_project_config() -> Path:
    path = Path(sandbox()) / "confined_roots.json"
    path.write_text(json.dumps({
        "enabled": True,
        "confine_to_project": True,
        "roots": [{"path": "C:\\Windows", "read": True}],
    }), encoding="utf-8")
    return path


# ==================================================================== neutrality
def no_provider_name_lives_in_the_layer():
    tokens = set()
    for kind in registry.KINDS:
        for row in registry.providers(kind):
            tokens.add(str(row.get("id") or ""))
            handler = row.get("handler") or {}
            tokens.add(str(handler.get("cli_flag_value") or ""))
            tokens.update(str(item) for item in handler.get("cli_flag_aliases") or [])
        tokens.add(str(registry.manifest_value(kind, "default_selection") or ""))
    # Two families of token are *not* model coupling and are exempt on principle:
    #  * wire-dialect spellings ("chat-completions" is both a declared cli_flag_value
    #    and a transport name, and keying renderers by dialect is exactly what §0 asks);
    #  * the system's own TOOL ids, which the layer documents and renders.
    exempt = {item.casefold() for item in
              list(transport.TRANSPORTS) + list(transport.TRANSPORT_ALIASES)
              + registry.provider_ids("TOOL")}
    tracked = {item for item in tokens if item and item.casefold() not in exempt}
    assert {"fish", "higgs", "gemma4", "dark_roast", "qwen3", "test_fake"} <= tracked, tracked
    import re
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" +
                         "|".join(re.escape(item) for item in sorted(tracked, key=len, reverse=True)) +
                         r")(?![A-Za-z0-9_])", re.IGNORECASE)
    hits = []
    for path in sorted(LAYER_DIR.glob("*.py")) + [LAYER_DIR / "roots.json"]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.name}:{number}:{line.strip()[:90]}")
    assert not hits, f"provider literals leaked into the tool layer: {hits}"
    # and the guard vocabulary itself is provider-free by construction
    assert fs_guard.EXPECTS == ("file", "directory", "any")


# ================================================================== end to end
def declared_production_row_runs_through_the_same_entry():
    """A row that already ships (no fixture involved) executes through ``execute``."""
    local = production_layer()
    outcome = local.execute({"tool_id": "local_document_search",
                             "arguments": {"query": "小满", "limit": 99}},
                            transport="chat-completions")
    if not outcome.ok and outcome.reason.startswith(("TOOL_IMPORT_FAILED", "TOOL_ENTRYPOINT_MISSING")):
        # the row points at another subsystem's module; say so precisely instead of
        # letting it look like a tool-layer fault
        raise AssertionError(f"{outcome.reason} — 由 {outcome.result.text[:120]} 引起，"
                             "属被指向的子系统，不是工具层")
    expect_ok(outcome)
    assert outcome.arguments["limit"] == 8                      # clamped by the row cap
    assert any(row.startswith("CLAMPED:limit") for row in outcome.audit["notes"]), outcome.audit
    assert outcome.wire("chat-completions")["role"] == "tool"


def end_to_end_model_requests_tool_and_gets_the_answer_back():
    from providers.adapters import fake_llm_adapter, recorder  # noqa: PLC0415
    from speech_to_speech.LLM.chat import Chat, make_system_message, make_user_message  # noqa: E402
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig  # noqa: E402
    from speech_to_speech.pipeline.messages import GenerateResponseRequest  # noqa: E402

    local = production_layer()
    planner = fakes.ScriptedPlanner([
        {"tool_id": "fixture_project_read",
         "arguments": {"path": str(PROJECT / "runtime" / "tooling" / "agent_scratch" / "README.txt")}},
        {"tool_id": "local_document_read", "arguments": {"offset": 0}},   # required arg missing
        {"tool_id": "fixture_project_read", "arguments": {"path": "../../Windows/win.ini"}},
    ])
    events = planner.events("chat-completions")
    context = ModelContext(character_id="default", session_id="sess-1", user_text="在吗")
    outcomes = []
    for event in events:
        request = transport.parse_call(event, "chat-completions")
        assert request.ok, request.as_dict()
        outcome = local.execute(request, transport="chat-completions",
                                turn={"session_id": "sess-1", "turn_id": 1})
        assert outcome.tool_id and outcome.reason is not None
        outcomes.append(outcome)
        # the answer travels back to the model in that transport's own shape
        wire = outcome.wire("chat-completions")
        assert wire["role"] == "tool" and wire["tool_call_id"] == request.call_id
        body = json.loads(wire["content"])
        assert body["tool_id"] == outcome.tool_id
    assert outcomes[0].ok and "allowed_roots" in outcomes[0].result.text
    assert not outcomes[1].ok and outcomes[1].reason == "TOOL_INVALID_ARGUMENTS"
    assert "MISSING_ARGUMENT:document_id" in outcomes[1].result.text
    assert not outcomes[2].ok and outcomes[2].reason == "PATH_OUTSIDE_ROOT"

    context = tools.feed_results(context, outcomes)
    assert len(context.tool_lines) == 3
    assert any("拒绝" in line for line in context.tool_lines), context.tool_lines
    system_text = context.system_text()
    assert "工具 fixture_project_read" in system_text and "PATH_OUTSIDE_ROOT" in system_text

    chat = Chat(10)
    try:
        chat.init_chat(make_system_message(system_text))
        chat.add_item(make_user_message(context.user_text))
        handler = fake_llm_adapter.FakeLLMHandler(
            Event(), Queue(), Queue(), setup_args=(Event(),), setup_kwargs={})
        produced = list(handler.process(GenerateResponseRequest(runtime_config=RuntimeConfig(chat=chat))))
    finally:
        chat.close()
    assert produced and type(produced[-1]).__name__ == "EndOfResponse"
    seen = recorder.latest("llm", _fixture_llm_id())
    assert "fixture_project_read" in seen["system_text"], seen["system_text"][:400]
    assert "PATH_OUTSIDE_ROOT" in seen["system_text"]


def _fixture_llm_id() -> str:
    """The contract fixture's id, read from the manifest, never hardcoded here."""
    rows = [row for row in registry.providers("LLM")
            if str(row.get("role") or "") == "contract_fixture"]
    assert rows, "llm.json declares no contract_fixture row"
    return str(rows[0]["id"])


def proxy_route_mines_tools_out_of_prose():
    local = layer()
    plan = tools.plan_for(_fixture_llm_id(), layer=local)
    assert plan.route == tools.PROXY and plan.transport == "text"
    assert "tool_id" in plan.instructions
    prose = '我先查一下。{"tool_id": "fixture_echo", "arguments": {"text": "从散文里挖出来"}}'
    calls = tools.plan_from_prose(prose)
    assert len(calls) == 1 and isinstance(calls[0], ToolCall)
    outcome = local.execute(calls[0], transport="text")
    expect_ok(outcome)
    assert outcome.result.text == "从散文里挖出来"
    assert outcome.wire("text")["channel"] == "context"


def native_route_is_a_manifest_decision():
    provider = _fixture_llm_id()
    assert registry.manifest_value("LLM", "shared_capabilities", "tools",
                                   "enabled_in_production") is False
    assert tools.route_for(provider) == tools.PROXY
    assert tools.native_calling_declared(provider) is False
    assert tools.plan_for(provider).schemas == []


TESTS = [
    manifest_is_the_only_declaration_source,
    adding_a_tool_row_needs_no_code,
    disabled_row_reports_its_declared_reason,
    unknown_tool_is_answered_with_the_registered_set,
    manifest_edit_is_live_without_a_restart,
    missing_required_argument_is_rejected,
    argument_types_bounds_and_enums_are_enforced,
    unknown_arguments_are_dropped_before_the_implementation,
    arguments_may_arrive_as_a_json_string,
    only_the_renderer_knows_the_wire_shape,
    every_declared_call_shape_parses_and_a_bad_one_does_not_raise,
    timeout_is_a_verdict_not_a_hang,
    implementation_crash_stays_structured,
    implementations_receive_the_live_guard,
    audit_line_is_written_per_call,
    relative_parent_traversal_is_denied,
    absolute_paths_outside_the_root_are_denied,
    traversal_out_of_root_and_inside_root_are_told_apart,
    secrets_inside_an_allowed_root_are_still_denied,
    a_row_cannot_widen_the_system_whitelist,
    a_row_may_name_its_root_relative_to_the_project,
    reparse_points_leading_out_of_the_root_are_denied,
    file_symlink_escape_is_denied,
    writes_need_three_explicit_switches,
    the_model_never_sees_an_absolute_path,
    production_policy_confines_to_the_project,
    no_provider_name_lives_in_the_layer,
    declared_production_row_runs_through_the_same_entry,
    end_to_end_model_requests_tool_and_gets_the_answer_back,
    proxy_route_mines_tools_out_of_prose,
    native_route_is_a_manifest_decision,
]


def _is_reparse(path: str) -> bool:
    try:
        return bool(os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def cleanup() -> None:
    if not _SANDBOX or not os.path.isdir(_SANDBOX):
        return
    # Never let rmtree walk *through* a reparse point: drop the links themselves
    # first, so cleanup can only ever delete the sandbox and nothing it points at.
    for parent, dirs, _files in os.walk(_SANDBOX):
        for name in dirs:
            probe = os.path.join(parent, name)
            try:
                if _is_reparse(probe):
                    os.rmdir(probe)
            except OSError:
                pass
    shutil.rmtree(_SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    passed, failures = 0, []
    skipped = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures.append((test.__name__, f"ASSERTION: {exc}"))
            print(f"  FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a crash is also a failed check
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok  {test.__name__}")
    cleanup()
    print(f"\ntool layer checks: {passed}/{len(TESTS)} passed")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for name, detail in failures:
            print(f"  - {name}: {detail[:300]}")
        raise SystemExit(1)
    print("tool layer: PASS")
