"""Prove the whole-environment reinstall detector actually detects.

``scan_site_packages_env.py`` is the guard that answers 任务 P0-2 for *every*
installed distribution, not only ``speech_to_speech``: after a
``pip install --force-reinstall <pkg>`` the project's in-place patch is gone, and
the sweep must say so out loud instead of returning a reassuring "clean".

A detection claim that has never been exercised is worth nothing, so this test
builds a throwaway site-packages tree in a temp directory, registers the patched
file the way the real scan does, and then walks it through the three byte-states
that matter:

    patched bytes present      -> REGISTERED,                    exit 0
    wheel bytes restored       -> REINSTALL_RESTORED_THE_WHEEL,   exit 1 + how to recover
    a *new* unregistered edit  -> REGISTERED_BYTES_CHANGED,       exit 1

plus the probe self-check that stops "my sweep found nothing" from being read as
"the environment is clean".

Nothing here touches the real venvs or the real SITE_PACKAGES_ENV_SCAN.json.

    python runtime/tests/test_env_reinstall_detection.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "runtime" / "audit" / "system_v2" / "tools" / "scan_site_packages_env.py"
PYTHON = ROOT / "runtime" / "core" / "venv" / "Scripts" / "python.exe"
LABEL = "FIXTURE/venv"

PRISTINE = b'# wheel bytes\ndef helper():\n    return 1\n'
PATCHED = b'# wheel bytes\ndef helper():\n    return 1  # project patch: keeps the old behaviour\n'
UNKNOWN_EDIT = b'# wheel bytes\ndef helper():\n    return 2  # somebody else\n'

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" :: {detail}" if detail else ""))


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record_field(raw: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def run(tree: Path, reg: Path, backups: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable if Path(sys.executable).is_file() else str(PYTHON), str(TOOL),
           "--no-default-trees", "--tree", f"{LABEL}={tree}",
           "--extra-registrations", str(reg),
           "--backup-dir", str(backups), "--out", str(out), *extra]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(ROOT))


def build(tree: Path, pkg_bytes: bytes) -> None:
    """A one-file distribution whose RECORD claims `pkg_bytes` as its wheel content."""
    (tree / "fakepkg").mkdir(parents=True, exist_ok=True)
    info = tree / "fakepkg-1.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "RECORD").write_bytes(
        b"fakepkg/__init__.py,,\n"
        + ("fakepkg/mod.py,%s,%d\n" % (record_field(PRISTINE), len(PRISTINE))).encode()
        + b"fakepkg-1.0.0.dist-info/METADATA,,\n")
    (tree / "fakepkg" / "__init__.py").write_bytes(b"")
    (tree / "fakepkg" / "mod.py").write_bytes(pkg_bytes)


def main() -> int:
    if not TOOL.is_file():
        print(f"missing tool: {TOOL}")
        return 2
    key = f"{LABEL}|fakepkg/mod.py"
    with tempfile.TemporaryDirectory(prefix="env-sweep-") as tmp:
        base = Path(tmp)
        tree = base / "site-packages"
        reg = base / "registrations.json"
        backups = base / "backups"
        out = base / "scan.json"
        reg.write_text(json.dumps({key: {
            "reason": "test fixture: emulates a project patch inside a vendor package",
            "sha256_patched": digest(PATCHED),
        }}, ensure_ascii=False), encoding="utf-8")

        # --- state 1: the patch is in place, and its verified backup exists -------------
        build(tree, PATCHED)
        (backups / LABEL / "fakepkg").mkdir(parents=True, exist_ok=True)
        (backups / LABEL / "fakepkg" / "mod.py").write_bytes(PATCHED)
        proc = run(tree, reg, backups, out, "--write")
        check("patched bytes on a registered file are accepted",
              proc.returncode == 0 and "REGISTERED " in proc.stdout,
              f"rc={proc.returncode}")
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("the sweep really hashed the fixture",
              payload["counts"]["rows_hashed"] >= 2,
              f"rows_hashed={payload['counts']['rows_hashed']}")
        check("the fixture tree is reported with its own label",
              list(payload["probe_self_check"]["per_tree"]) == [LABEL],
              str(list(payload["probe_self_check"]["per_tree"])))
        check("defaults really are off when --no-default-trees is passed",
              payload["counts"]["venvs"] == 1 and payload["counts"]["rows_hashed"] < 50,
              f"venvs={payload['counts']['venvs']} rows={payload['counts']['rows_hashed']}")

        # --- state 2: pip --force-reinstall put the wheel's own bytes back --------------
        (tree / "fakepkg" / "mod.py").write_bytes(PRISTINE)
        proc = run(tree, reg, backups, out, "--check")
        recovered = "Copy-Item" in proc.stdout and str(backups / LABEL / "fakepkg" / "mod.py") in proc.stdout
        check("a reinstall is DETECTED (not read as clean)",
              proc.returncode == 1 and "REINSTALL_RESTORED_THE_WHEEL" in proc.stdout,
              f"rc={proc.returncode}")
        check("the detector prints the exact recovery command",
              recovered, "Copy-Item ... <backup> ... -> <file>")

        # --- state 3: a second hand-edit nobody registered ------------------------------
        (tree / "fakepkg" / "mod.py").write_bytes(UNKNOWN_EDIT)
        proc = run(tree, reg, backups, out, "--check")
        check("an unregistered second edit is a different verdict than a reinstall",
              proc.returncode == 1 and "REGISTERED_BYTES_CHANGED" in proc.stdout
              and "REINSTALL_RESTORED_THE_WHEEL" not in proc.stdout,
              f"rc={proc.returncode}")

        # --- the patch has no verified backup -> not good enough -------------------------
        (tree / "fakepkg" / "mod.py").write_bytes(PATCHED)
        (backups / LABEL / "fakepkg" / "mod.py").unlink()
        proc = run(tree, reg, backups, out, "--check")
        check("a registered patch without a byte-exact copy fails the gate",
              proc.returncode == 1 and "MISSING_BACKUP" in proc.stdout,
              f"rc={proc.returncode}")

        # --- probe self-check: an empty sweep must never look clean ---------------------
        empty = base / "empty-site-packages"
        empty.mkdir()
        proc = run(empty, reg, backups, out, "--check")
        check("a sweep that hashed nothing refuses to report clean",
              proc.returncode == 1 and "PROBE_SELF_CHECK" in proc.stdout,
              f"rc={proc.returncode} out={proc.stdout.strip()[:80]}")

        # --- --write really creates the backup it later verifies ------------------------
        (tree / "fakepkg" / "mod.py").write_bytes(PATCHED)
        (backups / LABEL / "fakepkg" / "mod.py").write_bytes(UNKNOWN_EDIT)   # wrong bytes
        proc = run(tree, reg, backups, out, "--write")
        written = (backups / LABEL / "fakepkg" / "mod.py").read_bytes()
        proc2 = run(tree, reg, backups, out, "--check")
        check("--write replaces a stale backup and the gate then passes",
              written == PATCHED and proc2.returncode == 0,
              f"write rc={proc.returncode} check rc={proc2.returncode}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nenv reinstall-detection checks: {'PASS' if not failed else 'FAIL'} "
          f"({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    for name in failed:
        print("  FAILED:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
