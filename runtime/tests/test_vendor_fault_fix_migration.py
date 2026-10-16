"""任务 P0-1: the two FAULT fixes must now come from the project, not the wheel.

``baseHandler.py`` and ``websocket_router.py`` used to carry hand-edits inside
``site-packages`` -- which is exactly what made them un-reinstallable and
unauditable.  Their behaviour lives in :mod:`providers.vendor_fixes` now, so the
claim this test enforces has two halves, and *both* have to hold at once:

1. the two files are byte-identical to the wheel (their ``RECORD`` hash), so
   ``pip install --force-reinstall speech-to-speech`` changes nothing about them;
2. the project override is live in *this* process -- the vendor class/function
   has been wrapped, and wrapping is what supplies the fault behaviour.

Half 1 alone would be a silently broken service (that is the P0 symptom this all
started from); half 2 alone would mean the fix is still an in-place edit.
"""
from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
for _p in (str(RUNTIME / "core"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from providers import vendor_fixes  # noqa: E402

SITE_PACKAGES = RUNTIME / "core" / "venv" / "Lib" / "site-packages"
RECORD = SITE_PACKAGES / next(
    p.name for p in SITE_PACKAGES.glob("speech_to_speech-*.dist-info")
) / "RECORD"


def record_sha256(rel: str) -> str:
    """The wheel's own baseline for one file, as hex."""
    for line in RECORD.read_text(encoding="utf-8").splitlines():
        if line.startswith(rel + ","):
            token = line.split(",")[1]
            assert token.startswith("sha256="), f"{rel}: RECORD is not sha256: {token}"
            return base64.urlsafe_b64decode(token[7:] + "=" * (-len(token[7:]) % 4)).hex()
    raise AssertionError(f"{rel} is not listed in {RECORD}")


def _file(rel: str) -> Path:
    return SITE_PACKAGES / rel


def _rel(fix) -> str:
    name = fix.target_module
    assert name.startswith("speech_to_speech."), name
    return "speech_to_speech/" + name[len("speech_to_speech."):].replace(".", "/") + ".py"


def both_fixes_are_declared() -> None:
    ids = [fix.fix_id for fix in vendor_fixes.fixes()]
    assert ids == ["FAULT-S2S-TERMINATOR", "FAULT-S2S-UNIT-LEAK"], ids
    for fix in vendor_fixes.fixes():
        assert fix.upstream_sha256 and len(fix.upstream_sha256) == 64, fix.fix_id
        assert fix.in_place_sha256 and len(fix.in_place_sha256) == 64, fix.fix_id
        assert fix.upstream_sha256 != fix.in_place_sha256, fix.fix_id
        assert fix.apply is not None and callable(fix.apply), fix.fix_id
        assert vendor_fixes.by_id(fix.fix_id) is fix


def vendor_files_are_byte_pristine() -> None:
    """Half 1: no project bytes left inside the wheel for these two files."""
    for fix in vendor_fixes.fixes():
        rel = _rel(fix)
        path = _file(rel)
        state = vendor_fixes.classify_vendor(path, fix.upstream_sha256, fix.in_place_sha256)
        print(f"  {rel:56s} {state}")
        assert state == vendor_fixes.PRISTINE, (
            f"{rel} is {state}, not {vendor_fixes.PRISTINE}: the fault fix is still (or "
            f"again) an in-place edit -> run runtime/audit/system_v2/tools/"
            f"migrate_vendor_fault_fixes_out.py"
        )
        assert vendor_fixes.sha256_of(path) == record_sha256(rel) == fix.upstream_sha256, (
            f"{rel}: the declared upstream hash does not match dist-info/RECORD"
        )


def overrides_are_installed_in_this_process() -> None:
    """Half 2: the wrapping happened, so the pristine files are not inert."""
    base = importlib.import_module("speech_to_speech.baseHandler")
    router = importlib.import_module("speech_to_speech.api.openai_realtime.websocket_router")

    assert getattr(base.BaseHandler, "emits_turn_terminator", None) is False, (
        "BaseHandler has no turn-terminator role attribute: providers.bootstrap did not "
        "install the terminator override, so a TTS adapter that fails to emit "
        "AUDIO_RESPONSE_DONE leaves the realtime response open forever"
    )
    from providers.vendor_fixes.turn_terminator import _MARKER as TERM_MARKER
    from providers.vendor_fixes.turn_terminator import FIX_ID as TERM_ID

    assert getattr(base.BaseHandler.run, TERM_MARKER, None) == TERM_ID, (
        "BaseHandler.run is the vendor's own again: the terminator guarantee is dead"
    )

    from providers.vendor_fixes.unit_drain_release import FIX_ID as LEAK_ID
    from providers.vendor_fixes.unit_drain_release import SESSION_END_FORCE_RELEASE_S
    from providers.vendor_fixes.unit_drain_release import _MARKER as LEAK_MARKER

    window = getattr(router, "SESSION_END_FORCE_RELEASE_S", None)
    assert window == SESSION_END_FORCE_RELEASE_S, (
        f"websocket_router has no SESSION_END_FORCE_RELEASE_S (got {window!r}): the "
        "drain wait is unbounded again, which is the dead-pool half of the P0 symptom"
    )
    assert getattr(router._release_unit_after_drain, LEAK_MARKER, None) == LEAK_ID, (
        "_release_unit_after_drain is the vendor's own again: one wedged handler thread "
        "would hold the only pipeline unit forever"
    )


def hooks_are_registered_for_every_entry_point() -> None:
    """A reinstall keeps the pristine bytes, so recovery is purely a question of
    whether the import hook still fires.  Both entry points must name both targets.
    """
    from providers import bootstrap

    for fix in vendor_fixes.fixes():
        hook = bootstrap.TARGETS.get(fix.target_module)
        assert hook is not None, (
            f"{fix.target_module} is not an import-hook target: the override would never "
            "install itself after a reinstall"
        )
        # The hook is the fix's own entry point, reached through the fix registry, so a
        # row cannot be added here without the hashes and the implementation travelling
        # with it.
        assert hook.__name__ == "patch_" + fix.fix_id.lower().replace("-", "_"), hook
    assert set(bootstrap.VENDOR_FIX_TARGETS.values()) == {f.fix_id for f in vendor_fixes.fixes()}
    pth = SITE_PACKAGES / "zz_providers_bootstrap.pth"
    assert pth.is_file(), "the .pth that installs the hooks is gone"
    body = pth.read_text(encoding="utf-8")
    assert "providers.bootstrap" in body and "install" in body, body


def reapplying_a_hook_is_idempotent() -> None:
    """The .pth fires once per interpreter, but ``patch_now`` is a public test seam
    and ``__main__`` double-imports are real -- a second application must not wrap
    twice, or the terminator guarantee would fire per layer."""
    from providers import bootstrap

    base = importlib.import_module("speech_to_speech.baseHandler")
    router = importlib.import_module("speech_to_speech.api.openai_realtime.websocket_router")
    run_before = base.BaseHandler.run
    release_before = router._release_unit_after_drain

    first = bootstrap.patch_now("speech_to_speech.baseHandler")
    second = bootstrap.patch_now("speech_to_speech.api.openai_realtime.websocket_router")
    assert base.BaseHandler.run is run_before, "patch_now re-wrapped BaseHandler.run"
    assert router._release_unit_after_drain is release_before, "patch_now re-wrapped the release"
    assert "already applied" in first and "already applied" in second, (first, second)


TESTS = [
    both_fixes_are_declared,
    vendor_files_are_byte_pristine,
    overrides_are_installed_in_this_process,
    hooks_are_registered_for_every_entry_point,
    reapplying_a_hook_is_idempotent,
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
        print(f"vendor fault-fix migration checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"vendor fault-fix migration checks: PASS ({len(TESTS)})")
