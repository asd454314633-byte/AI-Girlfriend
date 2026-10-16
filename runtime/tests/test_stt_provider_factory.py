"""Gate for ISSUE_MATRIX ``STT-FACTORY-MISSING``: the recognizer slot is filled from
``manifests/stt.json``, and one manifest row is enough to introduce an STT provider.

Until 2026-09-24 the STT half of the pipeline had a manifest *and no consumer*: the
installed wheel still chose the handler with ``if module_kwargs.stt == "…whisper"``
(``s2s_pipeline.py:769-847``).  ``providers.stt_factory`` + the import hook in
``providers.bootstrap`` close that, so this file checks the six things the row now has
to carry and the properties that follow from it:

======  =========================================================  ==========================================
piece   declared where                                             consumed by
======  =========================================================  ==========================================
id      ``providers[].id`` / ``handler.cli_flag_value``             ``registry.spec_for_flag('STT', …)``
default ``default_selection`` (there is no ``active_stt.txt``)      ``registry.active_id('STT')``, launch
args    ``handler.cli_arguments_class``                             ``stt_factory.build`` looks it up by name
model   ``launch.placeholders.STT_MODEL_DIR`` + ``handler.model_path``  ``registry.model_setup_kwargs``, PS launcher
factory ``handler.module`` / ``handler.class``                      ``stt_factory.load_handler_class``
caps    ``capabilities.*``                                          ``registry.ui_schema``/``validate_selection``
======  =========================================================  ==========================================

Run::

    runtime/core/venv/Scripts/python.exe runtime/tests/test_stt_provider_factory.py

Everything is offline: the handler classes the dispatch checks build are stubs injected
into ``sys.modules``, so no model loads, no socket opens and the live :8765 process is
never contacted.  The one check that touches a real asset only proves the model
directory the manifest declares exists.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import importlib
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT = RUNTIME.parent
for _p in (str(RUNTIME / "core"), str(RUNTIME / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reused rather than re-derived: the provider-name-gate AST walk and the "did this leak
# into the package" byte scan are already correct there, and a second implementation
# that quietly disagreed would be worse than the blind spot it replaces.
import test_site_packages_neutrality as neu  # noqa: E402
from providers import bootstrap, capabilities as cap, registry, stt_factory  # noqa: E402
from speech_to_speech import s2s_pipeline  # noqa: E402

#: A provider id that exists nowhere except inside this test process.
PROBE_ID = "zz_stt_probe"
PROBE_FLAG = "zz-stt-probe"

POWERSHELL = shutil.which("powershell") or "powershell.exe"


def stt_spec(provider_id: str | None = None) -> dict:
    row = registry.get("STT", provider_id or registry.default_selection("STT"))
    assert row is not None, "stt.json declares no default_selection row"
    return row


def stt_tokens() -> set[str]:
    """Everything ``--stt`` may carry for a registered STT provider, ids included."""
    out: set[str] = set()
    for row in registry.providers("STT"):
        handler = row.get("handler") or {}
        out.add(str(row.get("id") or ""))
        out.add(str(handler.get("cli_flag_value") or ""))
        out.update(str(item) for item in handler.get("cli_flag_aliases") or [])
    out.add(str(registry.default_selection("STT") or ""))
    out.discard("")
    return out


def vendor_stt_tokens() -> tuple:
    """The names ``--stt`` accepts, read out of the wheel's own ``Literal`` annotation."""
    field = s2s_pipeline.ModuleArguments.__dataclass_fields__["stt"]
    return bootstrap._literal_of(field.type)  # noqa: SLF001 (the helper is the point)


def args_row(class_name: str, **values):
    """A parsed-arguments stand-in whose *class name* is what the manifest declares.

    ``stt_factory`` finds the row by ``type(row).__name__``, the same key the pipeline's
    ``by_type`` map effectively feeds it in production.  A fresh subclass per name is
    required: renaming ``SimpleNamespace`` itself would mutate a shared builtin type.
    """
    return type(class_name, (types.SimpleNamespace,), {})(**values)


def vendor_dataclasses() -> dict:
    """Every dataclass the wheel's ``arguments_classes`` package exposes, by name.

    Read from ``sys.modules`` instead of importing: ``s2s_pipeline`` (already imported
    above) pulls all of them, so this can no more fail on a heavy optional dependency
    than the pipeline itself can.
    """
    out: dict[str, type] = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("speech_to_speech.arguments_classes."):
            continue
        for attr, obj in vars(module).items():
            if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                out.setdefault(attr, obj)
    return out


class temp_stt_manifest:
    """Mutate a throwaway copy of ``manifests/stt.json`` and point the registry at it.

    Same seam as ``neu.temp_tts_row``: the real manifest belongs to another worker and
    is never opened for writing, yet every read path sees the edited rows exactly as it
    would after a real edit.
    """

    def __init__(self, mutate=None) -> None:
        self.mutate = mutate
        self.dir: Path | None = None
        self._saved_dir = None
        self._saved_cache: dict = {}

    def __enter__(self) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="zero-stt-"))
        shutil.copytree(registry.MANIFEST_DIR, tmp, dirs_exist_ok=True)
        path = tmp / "stt.json"
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if self.mutate is not None:
            self.mutate(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.dir = tmp
        self._saved_dir = registry.MANIFEST_DIR
        self._saved_cache = dict(registry._cache)                    # noqa: SLF001
        registry.MANIFEST_DIR = tmp
        registry._cache.pop("STT", None)                             # noqa: SLF001
        registry._mtime.pop("STT", None)                             # noqa: SLF001
        try:
            registry.load_manifest("STT", refresh=True)
        except BaseException:
            # A rejected manifest is the interesting case (it is what the invariants are
            # for), and leaving MANIFEST_DIR pointed at a deleted temp copy would turn one
            # expected failure into every later check in this process.
            self.__exit__(None, None, None)
            raise
        return payload

    def __exit__(self, *_exc) -> None:
        registry.MANIFEST_DIR = self._saved_dir
        registry._cache.clear()                                      # noqa: SLF001
        registry._cache.update(self._saved_cache)                    # noqa: SLF001
        registry._mtime.pop("STT", None)                             # noqa: SLF001
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)


class stub_handler_module:
    """Install a recording stand-in for the ``speech_to_speech.STT`` module a row names.

    The class is created under the *manifest's* module name, so both the wheel's own
    ``from speech_to_speech.STT.… import …`` and ``stt_factory.load_handler_class``
    resolve the same object -- which is what makes the two dispatchers comparable.
    """

    def __init__(self, spec: dict) -> None:
        handler = spec.get("handler") or {}
        self.module_name = str(handler.get("module") or "")
        self.class_name = str(handler.get("class") or "StubSTTHandler")
        self.calls: list[dict] = []
        self._saved = None
        self._had = False

    def __enter__(self):
        outer = self

        class Stub:
            def __init__(self, stop_event, queue_in=None, queue_out=None,
                         setup_args=(), setup_kwargs=None):
                outer.calls.append({
                    "stop_event": stop_event,
                    "queue_in": queue_in,
                    "queue_out": queue_out,
                    "setup_args": setup_args,
                    "setup_kwargs": dict(setup_kwargs or {}),
                })
                self.stop_event = stop_event
                self.queue_in = queue_in
                self.queue_out = queue_out

        Stub.__name__ = outer.class_name
        module = types.ModuleType(outer.module_name)
        module.__dict__[outer.class_name] = Stub
        outer._had = outer.module_name in sys.modules
        outer._saved = sys.modules.get(outer.module_name)
        sys.modules[outer.module_name] = module
        return self

    def __exit__(self, *_exc):
        if self._had:
            sys.modules[self.module_name] = self._saved
        else:
            sys.modules.pop(self.module_name, None)

    @property
    def handler_class(self) -> type:
        return sys.modules[self.module_name].__dict__[self.class_name]


# ------------------------------------------------------------------ pieces 1 and 2


def active_recognizer_is_the_declared_default() -> None:
    """No selection file exists for STT, so ``default_selection`` must be the authority."""
    spec = stt_spec()
    active, error = registry.active_id("STT")
    assert error is None, f"registry.active_id('STT') reported {error}"
    assert active == registry.default_selection("STT") == spec["id"], (active, spec["id"])
    assert registry.selection_file("STT") is None, (
        "STT picked up a selection state file; its active id then has two authorities "
        "(the file and default_selection) and this check no longer proves anything"
    )
    assert registry.summary()["STT"]["active"] == active
    default_flag = str((spec.get("handler") or {}).get("cli_flag_value") or spec["id"])
    field = s2s_pipeline.ModuleArguments.__dataclass_fields__["stt"]
    assert field.default == default_flag, (
        f"CLI default {field.default!r} disagrees with stt.json default row {default_flag!r}"
    )
    assert default_flag in vendor_stt_tokens(), "the manifest default is not accepted by --stt"

    # Non-vacuity: with a second enabled row listed FIRST, row order must still not win.
    def duplicate(payload: dict) -> None:
        second = copy.deepcopy(payload["providers"][0])
        second["id"] = PROBE_ID
        second["handler"]["cli_flag_value"] = PROBE_FLAG
        payload["providers"].insert(0, second)

    with temp_stt_manifest(duplicate):
        rows = registry.providers("STT", enabled_only=True)
        assert rows[0]["id"] == PROBE_ID, "the control row was not installed first"
        assert registry.active_id("STT") == (spec["id"], None), (
            "registry.active_id('STT') answered by row order: with more than one recognizer "
            "registered the pipeline would start a different one than the panel reports"
        )

    for mutate, expected in (
        (lambda payload: payload.update(default_selection="nope"), "DEFAULT_SELECTION_UNKNOWN"),
        (lambda payload: payload.pop("default_selection", None), "MISSING_DEFAULT_SELECTION"),
    ):
        try:
            with temp_stt_manifest(mutate):
                pass
        except registry.RegistryError as exc:
            assert expected in str(exc), f"{expected} not reported: {exc}"
        else:
            raise AssertionError(f"stt.json loaded without a usable default_selection ({expected})")


# ------------------------------------------------------------------------ piece 3


def every_flag_is_one_the_vendor_cli_accepts() -> None:
    """A row may bind a handler, but it may not invent a ``--stt`` token.

    ``--stt`` is still parsed by the wheel's own ``Literal``, so a manifest row naming a
    value argparse rejects would fail at process start -- the one failure mode this layer
    cannot route around without editing site-packages.
    """
    accepted = set(vendor_stt_tokens())
    assert accepted, "could not read the wheel's --stt Literal; the check would be vacuous"
    for row in registry.providers("STT"):
        flag = str((row.get("handler") or {}).get("cli_flag_value") or row.get("id") or "")
        assert flag in accepted, (
            f"stt.json row {row.get('id')!r} carries --stt {flag!r}, which the wheel's CLI does "
            f"not accept (it accepts: {', '.join(sorted(accepted))})"
        )
    declared = [str((row.get("handler") or {}).get("cli_arguments_class") or "")
                for row in registry.providers("STT")]
    assert all(declared), f"a row the factory must build has no handler.cli_arguments_class: {declared}"
    unknown = set(declared) - set(stt_factory.ARGUMENT_CLASSES)
    assert not unknown, (
        f"stt_factory does not know the argument dataclass names {sorted(unknown)}; add them to "
        "ARGUMENT_CLASSES (it is a name list only -- the module never imports them)"
    )
    for name in declared:
        assert name in vendor_dataclasses(), f"{name} is not a dataclass the wheel exposes"


# ------------------------------------------------------------------------ piece 4


def model_path_is_one_fact_for_both_launchers() -> None:
    """The weights resolve identically for Python and for the PowerShell launcher."""
    spec = stt_spec()
    handler = spec["handler"]
    block = handler.get("model_path") or {}
    flag, placeholder, kwarg = (str(block.get(k) or "") for k in ("cli_flag", "placeholder", "setup_kwarg"))
    assert flag and placeholder and kwarg, f"handler.model_path is incomplete: {block}"

    launch = spec.get("launch") or {}
    cli = list(launch.get("cli_args") or [])
    assert flag in cli, f"{flag} is declared as the model-path flag but never appears in launch.cli_args"
    assert cli[cli.index(flag) + 1] == "{" + placeholder + "}", (
        f"{flag} must be fed by the placeholder the model_path block names ({placeholder}), "
        f"got {cli[cli.index(flag) + 1]!r}"
    )
    declared_dir = str((launch.get("placeholders") or {}).get(placeholder) or "")
    assert declared_dir, f"{placeholder} is not declared in launch.placeholders"
    assert declared_dir in [str(item) for item in launch.get("requires") or []], (
        f"launch.requires does not cover {declared_dir}, so the asset the model path points at "
        "is never checked before the process starts"
    )

    context = registry.launch_context("STT", spec["id"])
    resolved = registry.model_setup_kwargs("STT", spec["id"])
    assert list(resolved) == [kwarg], resolved
    path = Path(str(resolved[kwarg]))
    assert path.is_absolute() and path.is_dir(), (
        f"the declared model path {path} is not an existing directory: launch_context declined "
        "it, so nothing here proves the recognizer can load it"
    )
    plan = registry.launch_args("STT", spec["id"], context)
    assert not plan["missing_placeholders"], plan
    assert plan["args"][plan["args"].index(flag) + 1] == str(path), plan["args"]

    # The same law, run by the other language: the lifecycle script must expand to the
    # identical argv.  Two answers for "where are the weights" is how a pipeline ends up
    # loading a different model than the panel reports.
    workdir = Path(tempfile.mkdtemp(prefix="zero-stt-ps-"))
    script = workdir / "ctx.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{PROJECT / 'scripts' / 'Runtime-Common.ps1'}'\n"
        "$id = Get-ProviderDefault 'STT'\n"
        "$spec = Get-ProviderSpec 'STT' $id\n"
        "$ctx = Get-ProviderLaunchContext $spec\n"
        "$missing = New-Object System.Collections.Generic.List[string]\n"
        "$items = Expand-ProviderLaunchItems @($spec.launch.cli_args) $ctx $missing\n"
        "Write-Output ('ID=' + $id)\n"
        "Write-Output ('MISSING=' + @($missing).Count)\n"
        "Write-Output ('ARGS=' + ($items -join ' '))\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = dict(row.split("=", 1) for row in proc.stdout.splitlines() if "=" in row)
    assert lines.get("ID") == spec["id"], lines
    assert lines.get("MISSING") == "0", f"PowerShell could not resolve the placeholders: {lines}"
    assert lines.get("ARGS") == " ".join(plan["args"]), (
        "Python and PowerShell expanded stt.json differently:\n"
        f"  py: {' '.join(plan['args'])}\n  ps: {lines.get('ARGS')}"
    )

    # And the flag -> setup-kwarg rename the block claims is the wheel's own rule, proven
    # by running the wheel's own rename over a real parsed row.
    args_class = str(handler["cli_arguments_class"])
    row_cls = vendor_dataclasses().get(args_class)
    assert row_cls is not None, f"{args_class} is not one of the wheel's argument dataclasses"
    fields = [f.name for f in dataclasses.fields(row_cls) if f.name.endswith("_model_name")]
    assert fields, f"{args_class} has no *_model_name field for {flag} to fill"
    instance = row_cls(**{name: "sentinel" for name in fields})
    prefix = flag[2:][: -len("_model_name")]
    s2s_pipeline.rename_args(instance, prefix)
    assert kwarg in vars(instance), (
        f"rename_args({args_class}, {prefix!r}) produced {sorted(vars(instance))}: the manifest's "
        f"model_path.setup_kwarg {kwarg!r} is not the key the handler actually receives"
    )


# ------------------------------------------------------------------------ piece 5


def the_slot_is_the_factory_and_branches_on_no_name() -> None:
    """The function that really fills the STT slot must be ours, and must be name-blind."""
    fn = getattr(s2s_pipeline, "get_stt_handler", None)
    assert fn is not None, "speech_to_speech.s2s_pipeline has no get_stt_handler at all"
    hooked = fn is stt_factory.get_stt_handler or bool(getattr(fn, "_stt_registry_factory", False))
    applied = [row for row in bootstrap.applied() if "s2s_pipeline" in row]
    print(f"  bootstrap hooks applied: {applied}")
    assert any("registry-driven: get_stt_handler" in row or
               "providers.stt_factory.get_stt_handler" in row for row in applied), (
        "bootstrap neither recognized nor recovered the registry-driven STT slot"
    )
    assert hooked, (
        f"{fn.__module__}.{fn.__qualname__} is not marked as a registry-driven STT dispatcher"
    )
    tokens = stt_tokens()
    for name in ("get_stt_handler", "resolve", "load_handler_class", "build"):
        gates = neu.gate_lines(ast.parse(inspect.getsource(getattr(stt_factory, name))), tokens)
        for lineno, found in gates:
            print(f"    gate site {name}:{lineno}: {list(found)}")
        assert not gates, (
            f"providers.stt_factory.{name} branches on "
            f"{sorted({n for _, found in gates for n in found})}"
        )
    pipeline_source = inspect.getsource(s2s_pipeline.get_stt_handler)
    assert "elif module_kwargs.stt" not in pipeline_source
    assert not neu.gate_lines(ast.parse(pipeline_source), tokens), (
        "s2s_pipeline.get_stt_handler still branches on a registered provider name"
    )


def pipeline_adapter_matches_the_factory_contract() -> None:
    """The generic core adapter and direct factory make the same constructor call."""
    spec = stt_spec()
    handler = spec["handler"]
    flag = str(handler["cli_flag_value"])
    args_class = str(handler["cli_arguments_class"])
    declared_model = str(registry.model_setup_kwargs("STT", spec["id"])["model_name"])
    tracker = SimpleNamespace(name="turn-tracker")
    stop, qi, qo = Event(), Queue(), Queue()
    rows = {
        "whisper_stt_handler_kwargs": args_row("WhisperSTTHandlerArguments", model_name="ignored"),
        "faster_whisper_stt_handler_kwargs": args_row(
            args_class, model_name=declared_model, device="auto", gen_kwargs={"language": "zh"}),
        # The wheel's own signature has no defaults, so the comparison call has to fill
        # every slot -- which is also the proof that the replacement accepts the same
        # ten positional arguments the pipeline's only call site passes.
        "paraformer_stt_handler_kwargs": args_row("ParaformerSTTHandlerArguments", model_name="ignored"),
        "mlx_audio_whisper_stt_handler_kwargs": args_row(
            "MLXAudioWhisperSTTHandlerArguments", model_name="ignored"),
        "parakeet_tdt_stt_handler_kwargs": args_row("ParakeetTDTSTTHandlerArguments", model_name="ignored"),
    }
    positional = (SimpleNamespace(stt=flag), stop, qi, qo, tracker)

    with stub_handler_module(spec) as stub:
        mine = stt_factory.get_stt_handler(*positional, **rows)
        theirs = s2s_pipeline.get_stt_handler(*positional, **rows)
        assert len(stub.calls) == 2, stub.calls
        assert stub.calls[0] == stub.calls[1], (
            "the factory and the wheel's chain built the handler differently:\n"
            f"  factory: {stub.calls[0]}\n  wheel:   {stub.calls[1]}"
        )
        assert type(mine) is type(theirs)
        assert mine.speculative_turns is theirs.speculative_turns is tracker
        assert mine.provider_id == spec["id"], mine.provider_id
        print(f"  both built {stub.class_name} with an identical call "
              f"({sorted(stub.calls[0]['setup_kwargs'])})")

        # A parsed path always wins over the manifest: the factory reports, it does not steer.
        other = args_row(args_class, model_name="D:\\somewhere\\else")
        mine2 = stt_factory.get_stt_handler(*positional, faster_whisper_stt_handler_kwargs=other)
        assert mine2.queue_out is qo
        assert stub.calls[-1]["setup_kwargs"]["model_name"] == "D:\\somewhere\\else", stub.calls[-1]

        # A row parsed without the field at all is filled from the manifest.
        missing = args_row(args_class, device="auto")
        stt_factory.get_stt_handler(*positional, faster_whisper_stt_handler_kwargs=missing)
        assert stub.calls[-1]["setup_kwargs"]["model_name"] == declared_model, stub.calls[-1]


def an_unregistered_name_fails_closed() -> None:
    """No hidden vendor chain may start a recognizer absent from stt.json."""
    assert PROBE_FLAG not in vendor_stt_tokens(), vendor_stt_tokens()
    try:
        stt_factory.get_stt_handler(
            SimpleNamespace(stt=PROBE_FLAG), Event(), Queue(), Queue(), None,
            None, None, None, None, None)
    except ValueError as exc:
        text = str(exc)
        assert "Unsupported STT backend" in text and "stt.json" in text, text
    else:
        raise AssertionError("an unregistered STT provider reached a hidden fallback")


def malformed_stt_rows_are_rejected_by_manifest_validation() -> None:
    """Factory-required fields are load-time invariants, not late AttributeErrors."""
    cases = []
    for field in ("module", "class", "cli_flag_value", "cli_arguments_class"):
        def remove(payload: dict, field=field) -> None:
            payload["providers"][0]["handler"].pop(field, None)
        cases.append((remove, f"STT_HANDLER_FIELD_MISSING:faster_whisper:{field}"))

    def break_model_binding(payload: dict) -> None:
        payload["providers"][0]["handler"]["model_path"]["placeholder"] = "WRONG_DIR"

    cases.append((break_model_binding, "STT_MODEL_PATH_LAUNCH_MISMATCH"))
    for mutate, expected in cases:
        try:
            with temp_stt_manifest(mutate):
                pass
        except registry.RegistryError as exc:
            assert expected in str(exc), f"{expected} not reported: {exc}"
        else:
            raise AssertionError(f"malformed STT row loaded without {expected}")


# ------------------------------------------------------------------------ piece 6


def capabilities_are_generic_and_agree_with_the_injected_fix() -> None:
    """STT capabilities go through the same code as TTS's, and match what really runs."""
    spec = stt_spec()
    rendered = {row["id"]: row for row in registry.ui_schema("STT")}
    assert spec["id"] in rendered, registry.ui_schema("STT")
    clean, dropped = registry.validate_selection("STT", spec["id"], {"emotion": "calm"})
    assert "emotion" not in clean and "UNKNOWN_CAPABILITY:emotion" in dropped, (clean, dropped)
    for name in spec.get("capabilities") or {}:
        assert cap.is_known(name, "STT"), f"{name} is declared but not in the STT vocabulary"

    module = importlib.import_module(str(spec["handler"]["module"]))
    handler_class = getattr(module, str(spec["handler"]["class"]))
    gated = bool(getattr(handler_class.__dict__.get("process"), "_mic_final_only_fix", False))
    promises_partials = cap.supported(spec.get("capabilities") or {}, "partial_transcripts")
    assert gated == (not promises_partials), (
        f"the wheel's handler for {spec['id']} has the final-only gate injected: {gated}, while "
        f"stt.json declares partial_transcripts supported={promises_partials}. One of the two is "
        "wrong, and the browser renders the manifest's answer"
    )
    assert not cap.supported(spec.get("capabilities") or {}, "runtime_switch"), (
        "STT now claims a runtime switch, which needs a selection state file and a restart path "
        "that do not exist; update registry.SELECTION_SOURCES and this gate together"
    )


# ------------------------------------------------------------------------ all six


class ProbeSTTHandler:
    """The class the synthetic row binds to, in this module (``handler.module``)."""

    def __init__(self, stop_event, queue_in=None, queue_out=None, setup_args=(), setup_kwargs=None):
        self.stop_event = stop_event
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.setup_kwargs = dict(setup_kwargs or {})


def one_row_is_enough_to_add_a_recognizer() -> None:
    """The property the verdict turns on: a new row, zero edits under the wheel."""

    def add(payload: dict) -> None:
        row = copy.deepcopy(payload["providers"][0])
        row["id"] = PROBE_ID
        row["enabled"] = True
        handler = row.setdefault("handler", {})
        handler["cli_flag_value"] = PROBE_FLAG
        handler["cli_arguments_class"] = "ProbeSTTArguments"
        handler["module"] = __name__          # this test module owns the stub class
        handler["class"] = "ProbeSTTHandler"
        handler.pop("model_path", None)
        row["capabilities"] = {"language_zh": {"supported": True}}
        payload["providers"].append(row)

    with temp_stt_manifest(add):
        spec = registry.spec_for_flag("STT", PROBE_FLAG)
        assert spec is not None and spec["id"] == PROBE_ID
        assert PROBE_ID in registry.provider_ids("STT")
        assert PROBE_FLAG in registry.flag_values("STT")
        tracker = SimpleNamespace(name="turn-tracker")
        handler = stt_factory.build(
            PROBE_FLAG, Event(), Queue(), Queue(), tracker,
            {"ProbeSTTArguments": args_row("ProbeSTTArguments", model_name="probe")},
        )
        assert type(handler).__name__ == "ProbeSTTHandler", type(handler).__name__
        assert handler.provider_id == PROBE_ID, handler.provider_id
        assert handler.speculative_turns is tracker
        assert handler.setup_kwargs == {"model_name": "probe"}, handler.setup_kwargs
        offenders = [neu.rel_of(path) for path in neu.package_files()
                     if PROBE_ID in path.read_text(encoding="utf-8", errors="replace")]
        assert not offenders, (
            f"{PROBE_ID} appears in {offenders}: the pipeline only works because those bytes were "
            "edited, which is what §6 of the migration report forbids"
        )
        print(f"  built {PROBE_ID} from a manifest row; the name occurs in 0 of "
              f"{len(neu.package_files())} wheel files")


TESTS = [
    active_recognizer_is_the_declared_default,
    every_flag_is_one_the_vendor_cli_accepts,
    model_path_is_one_fact_for_both_launchers,
    the_slot_is_the_factory_and_branches_on_no_name,
    pipeline_adapter_matches_the_factory_contract,
    an_unregistered_name_fails_closed,
    malformed_stt_rows_are_rejected_by_manifest_validation,
    capabilities_are_generic_and_agree_with_the_injected_fix,
    one_row_is_enough_to_add_a_recognizer,
]

if __name__ == "__main__":
    for case in TESTS:
        case()
        print(f"  ok  {case.__name__}")
    print(f"stt provider factory checks: PASS ({len(TESTS)})")
