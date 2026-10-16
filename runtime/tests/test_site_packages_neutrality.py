"""Gate for 任务 §0/§6: nothing under ``speech_to_speech/**`` may need editing to add
a provider, and the code that actually fills the TTS slot may not branch on a
provider name.

This is the site-packages half of the neutrality property.  The project-wide
ratchet (``runtime/tests/test_provider_neutrality.py`` + ``provider_literal_baseline.json``)
freezes per-line hits in ~14 named files; it is *not* touched or re-frozen here.
This file adds the four things that ratchet cannot see:

1. **Whole-package coverage.**  Every file inside the installed package is scanned
   (94 today, all extensions), so a provider name in a file nobody was watching is
   a failure rather than a blind spot.
2. **Pristine-vs-project distinction.**  A file whose bytes still equal the wheel's
   ``RECORD`` hash contains upstream's own names, which the project does not own
   and cannot be blamed for.  The assertion is therefore on the *project-edited*
   files: every non-pristine, non-self-naming file under the package must contain
   ZERO provider-name literals.  Today that holds for
   ``baseHandler.py`` and ``api/openai_realtime/websocket_router.py`` (the two
   FAULT fixes) and for the four §6 shims.
3. **Shape, not just presence.**  A provider name used as a branch condition is a
   gate: it decides which providers may exist.  Names in a default, a help string
   or a provider's own adapter are not gates.  What is checked is the *effective*
   function (``s2s_pipeline.get_tts_handler`` after ``providers.bootstrap`` has
   replaced it), because the wheel's own if/elif chain is still in the pristine
   file bytes and is inert at runtime.
4. **A dynamic proof.**  A brand-new provider row is added to a throwaway copy of
   ``manifests/tts.json``, the pipeline builds it through the real production
   path, and the new name is shown to occur in zero bytes of the package --
   "adding a fake provider needs no edit under speech_to_speech/**".

Run::

    runtime/core/venv/scripts/python.exe runtime/tests/test_site_packages_neutrality.py

Everything is in-process and offline: the synthetic provider is bound to
``providers.adapters.fake_tts_adapter``, which loads no model, opens no socket and
touches no GPU; the live server on :8765 is never contacted.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import shutil
import sys
import tempfile
import typing
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT = RUNTIME.parent
for _p in (str(RUNTIME / "core"), str(RUNTIME / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_provider_neutrality as ratchet  # noqa: E402  (single source of the token set)
from providers import registry  # noqa: E402
from speech_to_speech import s2s_pipeline  # noqa: E402

SITE_PACKAGES = RUNTIME / "core" / "venv" / "Lib" / "site-packages"
PACKAGE = SITE_PACKAGES / "speech_to_speech"
MANIFEST = RUNTIME / "audit" / "system_v2" / "PATCH_MANIFEST.json"

# A provider id that exists nowhere except inside this test process.
PROBE_TOKEN = "zz_neutral_probe"
PROBE_TEMPLATE_ID = "test_fake"          # the existing model-free fixture adapter

# Files this worker patched or shimmed; §3 of the report claims every one of them
# is registered in PATCH_MANIFEST.json with a reason, a fresh hash, a byte-exact
# mirror and a backup -- this test enforces the claim.
MY_PATCHED: list[str] = []          # nothing is patched in place any more
#: The two files that *used to* be here.  Their behaviour is now a
#: ``providers.vendor_fixes`` override installed at import time, so they must be
#: byte-identical to the wheel *and* the override must be live in this process --
#: see ``vendor_fault_fixes_are_pristine_and_overridden``.
FAULT_FIX_TARGETS = [
    "speech_to_speech/baseHandler.py",
    "speech_to_speech/api/openai_realtime/websocket_router.py",
]
MY_SHIMS = [
    "speech_to_speech/TTS/manual_voice_mode.py",
    "girlfriend_reading.py",
]
# 任务 §6 end-state: the higgs/fish adapters are named by the manifest straight from
# ``s2s_business``, so the import shim inside the wheel is dead weight -- and a shim the
# manifest still named would be a file ``pip install --force-reinstall`` could delete and
# take the voice with it.  They are retired, byte-preserved under patches/removed_shells/.
MY_RETIRED_SHIMS = [
    "speech_to_speech/TTS/higgs_tts_handler.py",
    "speech_to_speech/TTS/fish_tts_handler.py",
]
REQUIRED_PTH = ["zero_replica_providers.pth"]


# --------------------------------------------------------------------------- helpers

def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def rel_of(path: Path) -> str:
    return str(path.relative_to(PACKAGE)).replace("\\", "/")


def provider_tokens() -> list[str]:
    """Every provider name from every manifest -- never a hand-written list."""
    return ratchet.provider_tokens()


def tts_tokens() -> set[str]:
    """Everything ``--tts`` may carry for a registered TTS provider, ids included."""
    out: set[str] = set()
    for row in registry.providers("TTS"):
        handler = row.get("handler") or {}
        out.add(str(row.get("id") or ""))
        out.add(str(handler.get("cli_flag_value") or ""))
        out.update(str(item) for item in handler.get("cli_flag_aliases") or [])
    out.add(str(registry.default_selection("TTS") or ""))
    out.discard("")
    return out


def self_naming_rels() -> set[str]:
    """Files that *are* the provider: its handler module, or an argument module named
    after it.  Naming yourself is not coupling -- the same rule that keeps the
    project-wide ratchet from scanning ``providers/adapters/``."""
    out: set[str] = set()
    for kind in registry.KINDS:
        for row in registry.providers(kind):
            module = str((row.get("handler") or {}).get("module") or "")
            if module.startswith("speech_to_speech."):
                out.add(module[len("speech_to_speech."):].replace(".", "/") + ".py")
    names = {token.lower() for token in provider_tokens()}
    for path in package_files():
        rel = rel_of(path)
        if rel.endswith(".py") and any(name in path.name.lower() for name in names):
            out.add(rel)
    return out


def non_pristine_rels() -> set[str]:
    """The files whose bytes differ from the wheel, i.e. the ones this project owns."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {e["path"] for e in manifest["modified"] + manifest["project_added"]}


def lexical_hits_by_file() -> dict[str, int]:
    """Lines carrying a provider-name literal, counted the way the ratchet counts
    them (same token set, same comment skipping)."""
    regex = ratchet.pattern()
    counts: dict[str, int] = {}
    for path in package_files():
        count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(ratchet.SKIP_PREFIXES):
                continue
            if regex.findall(stripped):
                count += 1
        if count:
            counts[rel_of(path)] = count
    return counts


def _matched_constants(node: ast.AST, tokens: set[str]) -> list[tuple[int, str]]:
    return [(child.lineno, child.value)
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in tokens]


def gate_lines(tree: ast.AST, tokens: set[str]) -> list[tuple[int, tuple[str, ...]]]:
    """(lineno, names) wherever the code *branches* on a provider name."""
    hits: dict[int, set[str]] = {}

    def record(subtree: ast.AST | None) -> None:
        if subtree is None:
            return
        for lineno, name in _matched_constants(subtree, tokens):
            hits.setdefault(lineno, set()).add(name)

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.Assert)):
            record(node.test)
        elif isinstance(node, ast.IfExp):
            record(node.test)
        elif isinstance(node, ast.comprehension):
            for clause in node.ifs:
                record(clause)
        elif isinstance(node, ast.Compare):
            record(node)
    return sorted((lineno, tuple(sorted(names))) for lineno, names in hits.items())


def effective_tts_assembly():
    """The function that really fills the pipeline's TTS slot, after import hooks.

    ``zz_providers_bootstrap.pth`` runs ``providers.bootstrap.install()``, which
    replaces ``s2s_pipeline.get_tts_handler`` with the manifest-driven factory.  The
    wheel's own ``if module_kwargs.tts == "chatTTS"`` chain is still in the pristine
    file bytes but is inert; a test that read only the file would either miss the
    real dispatch or cry wolf about the dead one.
    """
    from providers import bootstrap, tts_factory

    fn = getattr(s2s_pipeline, "get_tts_handler", None)
    if fn is None:
        raise AssertionError("speech_to_speech.s2s_pipeline has no get_tts_handler at all")
    hooked = fn is tts_factory.get_tts_handler
    return fn, f"{fn.__module__}.{fn.__qualname__}", hooked, bootstrap.applied()


class temp_tts_row:
    """Append a provider row to a throwaway copy of ``manifests/tts.json`` and point
    the registry at that copy.  The real manifest belongs to another worker and is
    never opened for writing, yet every read path -- ``_manifest_path``,
    ``load_manifest``, ``spec_for_flag``, ``tts_factory`` -- sees the new provider
    exactly as it would after a real edit."""

    def __init__(self) -> None:
        self.dir: Path | None = None
        self.row: dict = {}
        self._saved_dir = None
        self._saved_cache: dict = {}

    def __enter__(self) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="zero-neutral-"))
        shutil.copytree(registry.MANIFEST_DIR, tmp, dirs_exist_ok=True)
        path = tmp / "tts.json"
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        template = next((row for row in payload["providers"] if row.get("id") == PROBE_TEMPLATE_ID), None)
        if template is None:
            raise AssertionError(f"manifest has no {PROBE_TEMPLATE_ID} row to copy")
        row = copy.deepcopy(template)
        row["id"] = PROBE_TOKEN
        row["enabled"] = True
        handler = row.setdefault("handler", {})
        handler["cli_flag_value"] = PROBE_TOKEN
        handler.pop("cli_flag_aliases", None)
        row["label"] = f"{PROBE_TOKEN} neutrality probe"
        payload["providers"].append(row)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self.dir, self.row = tmp, row
        self._saved_dir = registry.MANIFEST_DIR
        self._saved_cache = dict(registry._cache)          # noqa: SLF001 (test seam)
        registry.MANIFEST_DIR = tmp
        registry._cache.pop("TTS", None)                   # noqa: SLF001
        registry._mtime.pop("TTS", None)                   # noqa: SLF001
        registry.load_manifest("TTS", refresh=True)
        return row

    def __exit__(self, *_exc) -> None:
        registry.MANIFEST_DIR = self._saved_dir
        registry._cache.clear()                            # noqa: SLF001
        registry._cache.update(self._saved_cache)          # noqa: SLF001
        registry._mtime.pop("TTS", None)                   # noqa: SLF001
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)


def literal_choices(cls: type, field_name: str) -> tuple[str, ...]:
    """The ``argparse`` choices a dataclass field currently produces -- i.e. what the
    real launcher may pass, after ``providers.bootstrap`` has rewritten the field.
    An empty tuple means the field is not a ``Literal`` at all (free-form string)."""
    field = next(f for f in dataclasses.fields(cls) if f.name == field_name)
    annotation = field.type if not isinstance(field.type, str) else cls.__annotations__.get(field_name)
    literals = [arg for arg in typing.get_args(annotation) if typing.get_origin(arg) is typing.Literal]
    if not literals:
        return ()
    return tuple(str(item) for item in typing.get_args(literals[0]))


# ------------------------------------------------------------------------------ tests

def project_edited_files_name_no_provider() -> None:
    """Zero provider names in any file the project itself edited -- unless it *is*
    that provider's own module.  Pristine wheel files are reported but not blamed:
    their vendor names are upstream's, and ``providers/bootstrap.py`` overrides them
    at import time."""
    measured = lexical_hits_by_file()
    self_files = self_naming_rels()
    edited = non_pristine_rels()
    baseline = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))

    print(f"  provider tokens tracked     : {len(provider_tokens())}")
    print(f"  files under the package     : {len(package_files())}")
    print(f"  non-pristine (project) files: {len(edited)}")
    print(f"  self-naming (exempt) files  : {len(self_files)}")

    offenders: list[str] = []
    pristine_total = project_total = 0
    for rel, hits in sorted(measured.items()):
        if rel in self_files:
            continue
        if rel in edited:
            project_total += hits
            offenders.append(f"{rel}: {hits} provider literal(s) in a project-edited file")
            print(f"    PROJECT-EDITED {rel:44s} hits={hits}")
        else:
            pristine_total += hits
            print(f"    upstream/pristine {rel:44s} hits={hits}")
    print(f"  totals: project-edited literals={project_total}  "
          f"upstream literals inside the package={pristine_total}")

    # before / after, the number the brief asks for.  "before" is the frozen
    # project-wide ledger; "after" is this scan.
    for hit_file in sorted({str(h["file"]) for h in baseline["hits"] if "speech_to_speech/" in str(h["file"])}):
        rel = hit_file.split("speech_to_speech/", 1)[1]
        allowed = sum(1 for h in baseline["hits"] if str(h["file"]).replace("\\", "/").endswith(rel))
        print(f"  ledger vs now for {rel:28s}: before={allowed} after={measured.get(rel, 0)} "
              f"(owner re-freezes provider_literal_baseline.json, never this test)")
    assert not offenders, "; ".join(offenders)


def tts_cli_accepts_every_registered_flag() -> None:
    """``--tts`` must accept exactly what the manifest registers -- no core edit.

    Upstream ships ``tts: Optional[Literal[...]]`` and ``HfArgumentParser`` turns a
    ``Literal`` into argparse ``choices``, so a fixed list there *is* a gate.  The
    project now widens it at import time from the registry
    (``providers/bootstrap._patch_module_arguments``); this checks the effective
    field, then proves it end-to-end through the real parser.
    """
    from speech_to_speech.arguments_classes.module_arguments import ModuleArguments

    registered = tts_tokens()
    choices = set(literal_choices(ModuleArguments, "tts"))
    if choices:
        assert choices == registered, (
            f"--tts choices are not the manifest's flags: file/hook says {sorted(choices)} but "
            f"the manifest registers {sorted(registered)}. Adding a provider would need an edit."
        )
        print(f"  effective --tts choices ({len(choices)}): {sorted(choices)}")
    else:
        print("  effective --tts type is a free-form str (registry validates)")

    argv = list(sys.argv)

    def parse_with(flag: str) -> str:
        """Run the real parser; argparse calls sys.exit(2) on an invalid choice, which
        must surface as a test failure and not kill the runner."""
        sys.argv = ["s2s_pipeline.py", "--mode", "realtime", "--tts", flag]
        try:
            parsed = s2s_pipeline.parse_arguments()
        except SystemExit as exc:
            raise AssertionError(
                f"argparse rejected --tts {flag!r} (exit {exc.code}). The choices come from "
                "providers.bootstrap._patch_module_arguments, which reads the manifest at import "
                "time: a provider registered after this process started is invisible until the "
                "hook is re-applied (see patch_now below)."
            ) from None
        return str(parsed.module_kwargs.tts)

    try:
        for flag in sorted(registered):
            assert parse_with(flag) == flag, f"{flag} did not survive parsing"
        with temp_tts_row():
            # Choices are computed at import, so re-apply the hook to show that a
            # *manifest-only* provider is accepted without touching any package file.
            from providers import bootstrap

            outcome = bootstrap.patch_now("speech_to_speech.arguments_classes.module_arguments")
            print(f"  re-applied the argument hook: {outcome}")
            assert parse_with(PROBE_TOKEN) == PROBE_TOKEN, (
                f"a provider that exists only in the manifest was rejected by the CLI: the "
                "argument layer still carries a hard-coded allow-list"
            )
        bootstrap.patch_now("speech_to_speech.arguments_classes.module_arguments")
        assert parse_with("higgs") == "higgs"
        # An unknown value is rejected by argparse (the choices mirror the manifest) and,
        # if it reaches the factory, answered by the registry with a list of what exists.
        from providers import tts_factory

        try:
            tts_factory.resolve("no_such_vendor")
        except ValueError as exc:
            assert "Registered" in str(exc), str(exc)
        else:
            raise AssertionError("resolve() accepted an unregistered TTS backend")
    finally:
        sys.argv = argv


def effective_tts_dispatch_never_branches_on_a_name() -> None:
    """The code that runs must not decide between providers by string."""
    fn, origin, hooked, applied = effective_tts_assembly()
    print(f"  effective TTS assembly : {origin}")
    print(f"  bootstrap hooks applied: {[a for a in applied if 's2s_pipeline' in a] or applied}")
    assert hooked, (
        f"{origin} is the wheel's own function again. The pristine file still contains the "
        "if/elif chain over provider names, so if the bootstrap hook is gone nothing routes "
        "through the registry and higgs/fish (bound only in the manifest) cannot start."
    )
    source = inspect.getsource(fn)
    tree = ast.parse(source)
    tokens = tts_tokens()
    gates = gate_lines(tree, tokens)
    for lineno, names in gates:
        print(f"    gate site line {lineno}: {list(names)}")
    assert not gates, (
        f"{origin} branches on {sorted({n for _, names in gates for n in names})}: a new "
        "provider would need an edit here. Resolve it via providers.registry instead."
    )
    # The same rule for the factory's own helpers, which the slot function delegates to.
    from providers import tts_factory

    for name in ("resolve", "load_handler_class", "build"):
        assert not gate_lines(ast.parse(inspect.getsource(getattr(tts_factory, name))), tokens), \
            f"providers.tts_factory.{name} branches on a provider name"


def brand_new_provider_needs_no_package_edit() -> None:
    """The property itself: one manifest row, zero edits under speech_to_speech/**."""
    with temp_tts_row():
        assert PROBE_TOKEN in registry.flag_values("TTS"), "the new flag is not visible to the registry"
        spec = registry.spec_for_flag("TTS", PROBE_TOKEN)
        assert spec is not None and spec["id"] == PROBE_TOKEN

        fn, origin, _hooked, _applied = effective_tts_assembly()
        handler = fn(SimpleNamespace(tts=PROBE_TOKEN), Event(), Queue(), Queue(), Event())
        assert type(handler).__name__ == "FakeTTSHandler", type(handler).__name__
        assert handler.provider_id == PROBE_TOKEN, (
            "the adapter got someone else's identity: provider_id must come from the manifest "
            f"row through the factory, got {handler.provider_id!r}"
        )
        # FAULT-001's guarantee is topological, so a provider invented after the fix
        # inherits it without knowing the concept exists.
        assert getattr(handler, "emits_turn_terminator", False) is True, (
            f"{origin} filled the TTS slot without marking it as the turn-terminator owner; "
            "a provider added later would silently reintroduce the stuck-response fault"
        )
        assert handler.__class__.__module__.startswith("providers.adapters."), handler.__class__.__module__

    offenders = [rel_of(path) for path in package_files()
                 if PROBE_TOKEN in path.read_text(encoding="utf-8", errors="replace")]
    assert not offenders, (
        f"the new provider name {PROBE_TOKEN} appears in {offenders}: the pipeline only works "
        "because those bytes were edited, which is exactly what §6 forbids"
    )
    print(f"  built {PROBE_TOKEN} through {origin}")
    print(f"  files mentioning {PROBE_TOKEN}: 0 of {len(package_files())}")


def my_work_is_registered_in_the_patch_manifest() -> None:
    """§3's deliverable, checked against disk: reasons, hashes, mirrors, backups."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("schema_version") == 2, (
        "PATCH_MANIFEST.json is not the recomputed v2 -- run "
        "runtime/audit/system_v2/tools/build_patch_manifest_v2.py"
    )
    entries = {e["path"]: e for e in manifest["modified"] + manifest["project_added"]}
    assert manifest["counts"]["undocumented"] == 0, manifest["problems"]["undocumented_patches"]
    for rel in MY_PATCHED + MY_SHIMS + REQUIRED_PTH:
        entry = entries.get(rel)
        assert entry, f"{rel} is missing from PATCH_MANIFEST.json"
        on_disk = SITE_PACKAGES / rel
        assert on_disk.is_file(), f"{rel} vanished from site-packages"
        disk_sha = sha256_of(on_disk)
        assert entry["sha256_new"] == disk_sha, (
            f"PATCH_MANIFEST is stale for {rel}: manifest says {entry['sha256_new'][:12]}, disk "
            f"says {disk_sha[:12]} -> re-run runtime/audit/system_v2/tools/build_patch_manifest_v2.py"
        )
        assert entry["reason"] and "UNDOCUMENTED" not in entry["reason"], entry
        assert entry["sha256_original_record"].startswith("sha256=") or entry["kind"] == "project_added", \
            f"{rel}: no pristine baseline hash recorded"
        mirror = Path(entry["patch_copy"]["abs"])
        assert sha256_of(mirror) == disk_sha, f"{rel}: the frozen copy under patches/ is not byte-exact"
        assert entry["backup_files"] or rel.endswith(".pth"), (
            f"{rel} has no .bak/.patch file for its original bytes"
        )
        print(f"    registered {('MOD' if entry['kind'] == 'modified' else 'ADD')} "
              f"{rel:52s} backups={len(entry['backup_files'])} endings={entry['line_endings']['style']}")
    for rel in MY_SHIMS:
        migration = entries[rel].get("migration")
        assert migration and migration["present"], f"{rel}: §6 migration target is missing"
        target = PROJECT / migration["project_path"]
        assert sha256_of(target) == migration["sha256"], f"{rel}: migration target changed after migration"
        print(f"    survives   {rel:52s} -> {migration['project_path']}")
    retired_shims()


def retired_shims() -> None:
    """A shim nothing imports must not linger in the wheel: it is unaccounted-for code
    that ``pip`` will happily delete and that the next reader will mistake for the real
    adapter.  All three halves are asserted — the file is gone from the wheel, no
    manifest row names it, and its bytes are still on disk under ``patches/``.
    """
    frozen_dir = RUNTIME / "audit" / "system_v2" / "patches" / "removed_shims"
    for rel in MY_RETIRED_SHIMS:
        on_disk = SITE_PACKAGES / rel
        assert not on_disk.exists(), (
            f"{rel} is back inside site-packages although nothing should import it; the "
            "project implementation lives under runtime/ and the manifest names that"
        )
        module_path = rel[:-3].replace("/", ".")
        named = [f"{kind}/{row.get('id')}" for kind in registry.KINDS for row in registry.providers(kind)
                 if module_path in str((row.get("handler") or {}).get("module") or "")]
        assert not named, f"manifest rows still bind to the retired shim: {named}"
        frozen = frozen_dir / rel.replace("/", "__")
        assert frozen.is_file(), f"{rel} was removed without keeping its bytes at {frozen}"
        print(f"    retired    {rel:52s} (bytes preserved in audit/system_v2/patches/removed_shims)")


TESTS = [
    project_edited_files_name_no_provider,
    tts_cli_accepts_every_registered_flag,
    effective_tts_dispatch_never_branches_on_a_name,
    brand_new_provider_needs_no_package_edit,
    my_work_is_registered_in_the_patch_manifest,
]

if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    if failures:
        print(f"site-packages neutrality checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"site-packages neutrality checks: PASS ({len(TESTS)})")
