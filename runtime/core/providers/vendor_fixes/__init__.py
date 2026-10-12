"""Vendor fixes that live in this project instead of inside ``site-packages``.

Why this package exists
-----------------------
Two faults (``FAULT-S2S-TERMINATOR`` and ``FAULT-S2S-UNIT-LEAK``) were first
fixed by editing the installed ``speech_to_speech`` wheel in place.  That is not
replayable: ``pip install --force-reinstall speech-to-speech`` restores the
vendor's bytes and the fixes vanish **silently**, and the repo is not a git
checkout, so there is nothing to re-apply from.

Here the same two behaviours are implemented as *wrappers* installed by
:mod:`providers.bootstrap` at import time, so:

* the vendor files stay at the wheel's pristine bytes (checkable against
  ``dist-info/RECORD``);
* a reinstall restores exactly the state this package was written against, so
  the fix re-applies itself with no human step -- that is the "recover after
  reinstall" requirement;
* nothing here copies a vendor code body.  ``turn_terminator`` wraps
  ``handler.process`` and delegates to the vendor's own ``run()``;
  ``unit_drain_release`` wraps ``_release_unit_after_drain`` and lets the
  vendor's own release tail run by setting the ``drained`` event.  Both depend
  only on vendor *contracts*, which is what makes them survive an upstream
  rewrite of the surrounding lines.

Each fix carries three hashes (任务 P0-3):

``upstream_sha256``
    the pristine wheel bytes, taken from ``RECORD``;
``in_place_sha256``
    the bytes produced by the old hand-edit, kept so the tripwire can tell
    "the vendor file has been restored" from "someone re-applied the old patch";
``impl_sha256``
    this project module's own bytes, so an audit can say which implementation was
    loaded into a running process.

The classification rule the tripwires use is deliberately *positive*: a file is
only trusted when its hash equals one of the two known values.  Anything else is
``UNKNOWN_DRIFT`` and the fix refuses to install itself on top of it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

FIX_DIR = Path(__file__).resolve().parent


def sha256_of(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


PRISTINE = "PRISTINE_UPSTREAM"
IN_PLACE = "IN_PLACE_PATCH_PRESENT"
UNKNOWN = "UNKNOWN_DRIFT"
MISSING = "FILE_MISSING"


def classify_vendor(path: Path | str, upstream_sha256: str, in_place_sha256: str) -> str:
    """Which of the three byte-states the installed vendor file is in."""
    p = Path(path)
    if not p.is_file():
        return MISSING
    digest = sha256_of(p)
    if digest == upstream_sha256:
        return PRISTINE
    if digest == in_place_sha256:
        return IN_PLACE
    return UNKNOWN


def rel_of_module(module_name: str) -> str:
    """``speech_to_speech.a.b`` -> ``speech_to_speech/a/b.py``.

    The package directory is part of the answer because that is the key both
    ``dist-info/RECORD`` and ``PATCH_MANIFEST.json`` use.
    """
    prefix = "speech_to_speech."
    if module_name.startswith(prefix):
        return "speech_to_speech/" + module_name[len(prefix):].replace(".", "/") + ".py"
    return module_name.replace(".", "/") + ".py"


@dataclass(frozen=True)
class VendorFix:
    """One project-owned replacement for an in-place vendor edit."""

    fix_id: str
    symptom: str
    target_module: str
    upstream_sha256: str
    in_place_sha256: str
    impl_file: str
    apply: Callable[[Any], str] = field(repr=False, default=lambda _m: "unset")
    requires_pristine: bool = False

    @property
    def impl_sha256(self) -> str:
        return sha256_of(FIX_DIR / self.impl_file)

    @property
    def target_rel(self) -> str:
        return rel_of_module(self.target_module)

    def state(self, site_packages: Path | str) -> str:
        return classify_vendor(
            Path(site_packages) / self.target_rel, self.upstream_sha256, self.in_place_sha256
        )

    def describe(self) -> dict:
        return {
            "fix_id": self.fix_id,
            "symptom": self.symptom,
            "target_module": self.target_module,
            "target_rel": self.target_rel,
            "upstream_sha256": self.upstream_sha256,
            "in_place_sha256": self.in_place_sha256,
            "impl_file": f"runtime/core/providers/vendor_fixes/{self.impl_file}",
            "impl_sha256": self.impl_sha256,
        }


def _load() -> list[VendorFix]:
    from . import turn_terminator, unit_drain_release

    return [turn_terminator.FIX, unit_drain_release.FIX]


def fixes() -> list[VendorFix]:
    return _load()


def by_id(fix_id: str) -> VendorFix:
    for fix in _load():
        if fix.fix_id == fix_id:
            return fix
    raise KeyError(fix_id)
