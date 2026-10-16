# -*- coding: utf-8 -*-
"""Regression gate for P1-B "state single source of truth".

Standalone runner (there is NO pytest in runtime/core/venv):

    runtime/core/venv/Scripts/python.exe runtime/tests/test_state_single_source.py

Prints ``ok <name>`` / ``FAIL <name>`` per check and ends with
``test_state_single_source: PASS (N)`` or ``test_state_single_source: FAIL (k/N)``.

What it proves, recomputed from disk on every run (state is pointer-ized, not
snapshotted):

  (a) ``tts_switch_state.json`` is a single-writer transaction journal (TSSCP01):
      the ONLY writer is the :7860 controller (voice_models_admin.py:_journal_write,
      atomic replace + read-back verify).  The shipped lifecycle script
      (runtime/windows/Switch-Girlfriend-Voice.ps1, Save-SwitchState) must be
      journal-NEUTRAL: replaying its real body against a seeded state file leaves
      the file byte-identical.  The pre-TSSCP01 backup
      (Switch-Girlfriend-Voice.ps1.bak_tsscp01) is run as a NEGATIVE CONTROL on the
      same input and must WRITE, otherwise check (a) is an empty test. Only temp
      files are touched; the live state file is never written.
  (b) every copy of the forbidden WSL-era legacy scripts (launch_chain.sh,
      restart_seed.sh, stage_main.sh, restart_lt.sh, b2r_torch.sh) found under
      runtime/llm, /home/user (WSL) or D:\\tmp carries the
      BOOT_HARDENING_PREFLIGHT_01 guard ahead of its first destructive statement,
      and the guard on the stray D:\\tmp copy provably aborts with exit 77 when the
      guard slice is executed in isolation inside WSL (never the real body).
  (c) ``active_tts_backend`` has exactly one live writer and one declared
      authoritative source; the second source (runtime/llm copy) and the second
      writer (runtime/llm legacy Switch ps1) stay retired.
  (d) ``service_pids.json`` and the live socket owners agree in the sense the stop
      scripts need: every port with a LISTENING socket has a tracked pid that is
      the owner itself or a live ancestor of it (tree-kill model), and every
      tracked pid without a listener is dead/zero -- i.e. nothing that could
      mislead Stop-AI-Girlfriend.ps1. A listener with tracked==0 fails: it is an
      orphan the stop path cannot reach.

Any probe that cannot compute its answer (wsl.exe missing, powershell blocked,
JSON unparseable) is reported as FAIL, never as a silent pass.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
RUNTIME = PROJECT / "runtime"
BACKEND_NAME = "active_tts_backend.txt"
SWITCH_NAME = "tts_switch_state.json"
GUARD_MARKER = "BOOT_HARDENING_PREFLIGHT_01 GUARD"
DESTRUCTIVE_RE = re.compile(r"^[ \t]*(pkill |kill -|setsid nohup|install -|rm -f|rm -rf)")

FORBIDDEN_SCRIPTS = ["launch_chain.sh", "restart_seed.sh", "stage_main.sh",
                     "restart_lt.sh", "b2r_torch.sh"]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("ok " if ok else "FAIL ") + name + ("" if not detail else "  " + detail))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def guard_static_check(text: str) -> tuple[bool, str]:
    """True when the BOOT_HARDENING guard is present and aborts before danger."""
    lines = text.splitlines()
    marks = [i for i, ln in enumerate(lines, 1) if GUARD_MARKER in ln]
    if not marks:
        return False, "guard marker missing"
    exits = [i for i, ln in enumerate(lines, 1) if re.match(r"^[ \t]*exit 77[ \t]*$", ln)]
    if not exits:
        return False, "no 'exit 77' in guard"
    danger = [i for i, ln in enumerate(lines, 1) if DESTRUCTIVE_RE.match(ln)]
    if danger and exits[0] > danger[0]:
        return False, f"exit77@{exits[0]} AFTER first destructive@{danger[0]}"
    if "GIRLFRIEND_DESTRUCTIVE_OK" not in text:
        return False, "escape-hatch variable missing (guard text was rewritten?)"
    return True, f"exit77@{exits[0]} before_destructive@{danger[0] if danger else 'none'}"


# ---------------------------------------------------------------- (b) legacy guards

def check_repo_guards() -> None:
    seen = []
    for name in FORBIDDEN_SCRIPTS:
        p = RUNTIME / "llm" / name
        if not p.is_file():
            continue  # repo copy does not exist; WSL/tmp copies are still checked
        ok, why = guard_static_check(read_text(p))
        seen.append(f"{name}({why})")
        record(f"legacy-guard-repo:{name}", ok, why)
    if not seen:
        record("legacy-guard-repo:any", False, "no repo copies found at all - scan broken?")


def wsl_cat(path: str) -> str | None:
    try:
        r = subprocess.run(["wsl.exe", "-e", "cat", path],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"__ERR__{exc}"
    if r.returncode != 0:
        return None  # genuinely absent
    out = r.stdout
    for enc in ("utf-8", "utf-16-le"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("utf-8", "replace")


def check_wsl_guards() -> None:
    any_copy = False
    for name in FORBIDDEN_SCRIPTS:
        text = wsl_cat(f"/home/user/{name}")
        if isinstance(text, str) and text.startswith("__ERR__"):
            record(f"legacy-guard-wsl:{name}", False, f"wsl.exe unusable: {text[:120]}")
            continue
        if text is None:
            continue  # no WSL-side copy of this script
        any_copy = True
        ok, why = guard_static_check(text)
        record(f"legacy-guard-wsl:{name}", ok, why)
    if not any_copy:
        record("legacy-guard-wsl:any", False,
               "no WSL copies readable - probe failed, not a pass")


def check_tmp_guard() -> None:
    p = Path(r"D:\tmp\restart_lt.sh")
    if not p.is_file():
        record("legacy-guard-tmp:restart_lt.sh", True, "stray copy absent (nothing to guard)")
        return
    ok, why = guard_static_check(read_text(p))
    record("legacy-guard-tmp:restart_lt.sh", ok, why)
    # Behavioural proof WITHOUT ever running the real body: slice head-through-END-GUARD
    # (guard only, by construction no destructive line), append a marker, run in WSL.
    lines = []
    for ln in read_text(p).replace("\r\n", "\n").split("\n"):
        lines.append(ln)
        if "END GUARD" in ln:
            break
    if not any("END GUARD" in ln for ln in lines):
        record("legacy-guard-tmp:slice-run", False, "no END GUARD marker to slice at")
        return
    lines.append("echo REACHED_CODE_BEYOND_GUARD; exit 0")
    if any(DESTRUCTIVE_RE.match(l) for l in lines):
        record("legacy-guard-tmp:slice-run", False, "slice would contain destructive code")
        return
    payload = "\n".join(lines).encode("utf-8")
    cmd = ("cat > /tmp/p1b_probe_$$.sh && bash /tmp/p1b_probe_$$.sh; rc=$?; "
           "rm -f /tmp/p1b_probe_$$.sh; exit $rc").replace("$$", str(os.getpid()))
    try:
        r = subprocess.run(["wsl.exe", "-e", "bash", "-c", cmd],
                           input=payload, capture_output=True, timeout=90)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        ok2 = (r.returncode == 77 and "NOT a production restart" in out
               and "REACHED_CODE_BEYOND_GUARD" not in out)
        record("legacy-guard-tmp:abort-exit77-live", ok2,
               f"rc={r.returncode} out={out.strip().splitlines()[:1]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("legacy-guard-tmp:abort-exit77-live", False, f"wsl probe unusable: {exc}")


# ------------------------------------- (a) single-writer: the PS side is journal-neutral
# TSSCP01 (TASK_ID = AI_GIRLFRIEND_TTS_SWITCH_CONTROL_PLANE_FINAL_REPAIR_01): the
# switch journal has exactly ONE writer — the :7860 controller
# (voice_models_admin.py:_journal_write).  The lifecycle script's Save-SwitchState
# used to be a second writer; since TSSCP01 it must be journal-NEUTRAL: replaying the
# shipped body against a seeded state file must leave that file byte-identical.  The
# pre-TSSCP01 backup (Switch-Girlfriend-Voice.ps1.bak_tsscp01) is replayed as a
# NEGATIVE CONTROL on the same input and must WRITE, otherwise check (a) is an empty
# test.  Only temp files are touched; the live state file is never written.

_HARNESS_GUARD = r'''
function Read-JsonText([string]$Path) { return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) }
function Write-RuntimeLog([string]$Name,[string]$Line) { }
__ATOMIC__
__BODY__
$outcome = @()
foreach ($step in @('PROBE_ONE', 'PROBE_TWO')) {
    try { Save-SwitchState $false '' '' $step '' 'qwen3' 'qwen3'; $outcome += 'ok' }
    catch { $outcome += 'fail' }
}
$hash = ''
if (Test-Path -LiteralPath $switchFile) {
    $hash = (Get-FileHash -LiteralPath $switchFile -Algorithm SHA256).Hash
}
Write-Output ('one=' + $outcome[0] + ' two=' + $outcome[1] + ' sha=' + $hash)
'''


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_owner_pid_survival() -> None:
    ship = RUNTIME / "windows" / "Switch-Girlfriend-Voice.ps1"
    backup = RUNTIME / "windows" / "Switch-Girlfriend-Voice.ps1.bak_tsscp01"
    common = PROJECT / "scripts" / "Runtime-Common.ps1"
    tmpdir = Path(tempfile.mkdtemp(prefix="tsscp01_journal_neutral_"))
    seed = ('{"busy":true,"phase":"loading","message":"m","result":"","detail":"",'
            '"previous":"qwen3","target":"higgs","switch_id":"seed-switch",'
            '"child_pid":424242,"child_start_time":"700000.0","bff_only_key":"keep-me"}')
    harness = tmpdir / "run_journal_neutral_harness.ps1"

    def run_source(tag: str, src: Path, with_atomic: bool) -> tuple[Path, str]:
        state_path = tmpdir / f"state_{tag}.json"
        state_path.write_text(seed, encoding="ascii")
        before = _sha256(state_path)
        body = None
        # Extract the real Save-SwitchState body with the real parser (never a copy
        # of the rule), the way this gate has always done.
        ps_extract = (
            "$tok=$null;$perr=$null;"
            f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{src}',[ref]$tok,[ref]$perr);"
            "$fn=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
            "-and $n.Name -eq 'Save-SwitchState'},$true)|Select-Object -First 1;"
            "if(-not $fn){throw 'FUNCTION_NOT_FOUND'}; $fn.Extent.Text")
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_extract],
                           capture_output=True, timeout=120)
        body = r.stdout.decode("utf-8-sig", "replace")
        if r.returncode != 0 or "function Save-SwitchState" not in body:
            return state_path, f"EXTRACT_FAILED rc={r.returncode} {r.stderr.decode('utf-8','replace')[:200]}"
        atomic = ""
        if with_atomic:
            ra = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$tok=$null;$perr=$null;"
                 f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{common}',[ref]$tok,[ref]$perr);"
                 "$fn=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
                 "-and $n.Name -eq 'Write-AtomicFile'},$true)|Select-Object -First 1;"
                 "if(-not $fn){throw 'FUNCTION_NOT_FOUND'}; $fn.Extent.Text"],
                capture_output=True, timeout=120)
            atomic = ra.stdout.decode("utf-8-sig", "replace")
            if ra.returncode != 0 or "function Write-AtomicFile" not in atomic:
                return state_path, "ATOMIC_EXTRACT_FAILED"
        # Comment lines are dropped: the shipped body documents old fixed temp names
        # in prose, and matching prose would make a correct file look broken.
        code = ((body + "\n" + atomic) if atomic else body)
        code = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("#"))
        inner = tmpdir / f"inner_{tag}.ps1"
        text = ("$switchFile = '" + str(state_path).replace("'", "''") + "'\n"
                + _HARNESS_GUARD.replace("__ATOMIC__", atomic).replace("__BODY__", code))
        inner.write_bytes(text.encode("utf-8-sig"))
        try:
            r2 = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(inner)],
                capture_output=True, timeout=240)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return state_path, f"HARNESS_ERROR {exc}"
        out = r2.stdout.decode("utf-8", "replace") + r2.stderr.decode("utf-8", "replace")
        after = _sha256(state_path) if state_path.is_file() else "GONE"
        return state_path, f"changed={str(before != after).lower()} rc={r2.returncode} {out.strip()[:220]}"

    new_state, new_out = run_source("NEW", ship, with_atomic=False)
    old_state, old_out = run_source("OLD", backup, with_atomic=True)

    # (1) The shipped writer must be journal-neutral: byte-identical file after the
    #     call, rc=0 (it logs, it does not write), and no stray temp files next to it.
    stray_new = list(tmpdir.glob("state_NEW*.tmp"))
    neutral_ok = ("changed=false" in new_out and "one=ok two=ok" in new_out and not stray_new)
    record("tsscp01-ps-journal-neutral", bool(neutral_ok),
           new_out[:420] if not neutral_ok else
           "shipped Save-SwitchState leaves the journal byte-identical (logs only)")

    # (2) Negative control: the pre-TSSCP01 dual-writer body must still WRITE.
    old_wrote = "changed=true" in old_out
    record("tsscp01-ps-journal-negative-control", bool(old_wrote),
           old_out[:420] if not old_wrote else
           "pre-TSSCP01 backup writes the journal on the same input (test is not empty)")

    # (3) Static: shipped PS never passes the journal to the atomic writer, and the
    #     single Python writer keeps its atomic replace + read-back verify.
    ship_text = read_text(ship)
    admin_text = read_text(RUNTIME / "ui" / "app" / "voice_models_admin.py")
    static_ok = (not re.search(r"Write-AtomicFile\s+\$switchFile", ship_text)
                 and "_journal_write(" in admin_text
                 and "_write_json_atomic(" in admin_text
                 and "os.replace" in admin_text)
    record("tsscp01-single-writer-static", bool(static_ok),
           "" if static_ok else "ship still wires the journal to a writer, or the "
           "controller lost its atomic write/verify path")


# ----------------------------------------------------- (c) active_tts_backend graph

def _iter_live_files():
    """Files in the live control-plane layers only (never audit/tests/evidence/venv).

    A frozen regression snapshot (any directory whose name ends in ``_FREEZE``) is not
    part of the live control plane either: it is a byte-for-byte copy of an older build
    kept as evidence, imported by nothing and run by nothing -- but it does contain the
    old source text, so without this prune the scanner reads a *quarantined* copy of
    ``avatar_admin.py`` as if it were a third writer of ``active_avatar.txt`` and reports
    a single-writer regression that does not exist. The exclusion is by directory NAME,
    and the reason it is safe is that the live files it duplicates are still scanned from
    their own real paths.
    """
    exts = {".ps1", ".py", ".sh", ".cmd", ".bat"}
    prune = {"venv", "__pycache__", "node_modules", "site-packages", "logs",
             "models", "data"}
    for f in PROJECT.glob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            yield f
    for root in (PROJECT / "scripts", RUNTIME / "windows", RUNTIME / "llm",
                 RUNTIME / "ui", RUNTIME / "core"):
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in prune and not d.upper().endswith("_FREEZE")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in exts:
                    yield p


def scan_backend_mentioners() -> tuple[list[tuple[str, bool]], list[str]]:
    """Return [(file, is_writer)] for every live file mentioning the backend file."""
    mention_re = re.compile(re.escape(BACKEND_NAME))
    # Write-AtomicFile is in this list because it is the write primitive now: state
    # writers call it instead of spelling Set-Content/Move-Item per site.
    write_re = re.compile(r"(Set-Content|Add-Content|Out-File|WriteAllText|WriteAllBytes|Write-AtomicFile|Move-Item|write_text\(|open\([^)]*['\"]w)")
    writers, readers = [], []
    for p in _iter_live_files():
        low = p.name.lower()
        if ".bak" in low or ".retired" in low or low.startswith("pre-") or ".old" in low:
            continue
        try:
            text = read_text(p)
        except OSError:
            continue
        if not mention_re.search(text):
            continue
        # bound variables whose assignment names the backend file
        bound = set(re.findall(r"\$(\w+)\s*=[^;\n]*" + re.escape(BACKEND_NAME), text))
        bound |= set(re.findall(r"(\w+)\s*=\s*NATIVE_ROOT\s*/\s*'" + re.escape(BACKEND_NAME) + "'", text))
        is_writer = False
        for line in text.splitlines():
            if not write_re.search(line):
                continue
            for v in bound:
                if re.search(r"\$\{?" + v + r"\b", line):
                    is_writer = True
            if re.search(r"write_selection|open\([^)]*active_tts_backend[^)]*['\"]w", line):
                is_writer = True
        rel = str(p.relative_to(PROJECT)).replace("\\", "/")
        writers.append((rel, is_writer))
        if not is_writer:
            readers.append(rel)
    return writers, readers


def check_single_writer() -> None:
    files, _readers = scan_backend_mentioners()
    writer_set = sorted(f for f, w in files if w)
    expected = ["runtime/windows/Switch-Girlfriend-Voice.ps1"]
    record("backend-single-writer", writer_set == expected,
           f"writers={writer_set} mentions={len(files)}")
    reg = read_text(RUNTIME / "core" / "providers" / "registry.py")
    m = re.search(r'"TTS"\s*:\s*"runtime/windows/active_tts_backend\.txt"', reg)
    record("backend-declared-authority-registry", bool(m),
           "registry.SELECTION_SOURCES[TTS] literal" if m else "SELECTION_SOURCES TTS path not found")


def check_avatar_pointer_source() -> None:
    """``active_avatar.txt`` is a one-line selection pointer (the digital-human
    sibling of ``active_tts_backend.txt``). Exactly two live writers are allowed:
    the BFF module (first-run seeding and the explicit「同步一下」button) and the
    switch script (COMMIT after a verified switch). A one-line file has no foreign
    keys to clobber, so two atomic writers are safe -- but a *third* writer, or one
    of these two disappearing, is a regression this gate must catch."""
    allowed = {
        "runtime/ui/app/avatar_admin.py",
        "runtime/windows/Switch-Girlfriend-Avatar.ps1",
    }
    write_re = re.compile(
        r"(Set-Content|Add-Content|Out-File|WriteAllText|WriteAllBytes|"
        r"Write-AtomicFile|Move-Item|write_text\(|os\.replace|_write_pointer\()")
    found: set[str] = set()
    for p in _iter_live_files():
        low = p.name.lower()
        if ".bak" in low or ".retired" in low or low.startswith("pre-") or ".old" in low:
            continue
        try:
            text = read_text(p)
        except OSError:
            continue
        if "active_avatar.txt" not in text:
            continue
        if not write_re.search(text):
            continue
        found.add(str(p.relative_to(PROJECT)).replace("\\", "/"))
    record("avatar-pointer-writers-bounded", found == allowed, f"writers={sorted(found)}")


def check_legacy_sources_retired() -> None:
    llm_backend = RUNTIME / "llm" / BACKEND_NAME
    llm_retired = (RUNTIME / "llm" / (BACKEND_NAME + ".retired_stale_0925")).is_file()
    record("backend-legacy-copy-retired",
           (not llm_backend.exists()) and llm_retired,
           f"llm copy exists={llm_backend.exists()} retired_sibling_present={llm_retired}")
    llm_switch = RUNTIME / "llm" / "Switch-Girlfriend-Voice.ps1"
    llm_switch_retired = (RUNTIME / "llm" / "Switch-Girlfriend-Voice.ps1.retired_deadcopy_0925").is_file()
    record("backend-legacy-writer-retired",
           (not llm_switch.exists()) and llm_switch_retired,
           f"llm switch exists={llm_switch.exists()} retired_sibling_present={llm_switch_retired}")


# ------------------------------------------------- (d) service_pids vs socket owners

def netstat_listeners() -> dict[int, list[int]]:
    r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8", "replace")
    found: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        m = re.search(r":(\d+)$", parts[1])
        if not m:
            continue
        port = int(m.group(1))
        if port in (7860, 8090, 8765, 8010) and port not in found:
            found[port] = []
        if port in (7860, 8090, 8765, 8010):
            found[port].append(int(parts[4]))
    return found


def process_map() -> dict[int, tuple[int, str]]:
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.ParentProcessId, $_.Name }"],
        capture_output=True, timeout=120)
    out = r.stdout.decode("utf-8", "replace")
    pm: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        bits = line.strip().split("|")
        if len(bits) == 3 and bits[0].isdigit() and bits[1].isdigit():
            pm[int(bits[0])] = (int(bits[1]), bits[2])
    if len(pm) < 20:
        raise RuntimeError(f"process map unusable ({len(pm)} rows)")
    return pm


def check_pid_port_agreement() -> None:
    pidfile = RUNTIME / "windows" / "service_pids.json"
    try:
        state = json.loads(read_text(pidfile))
        assert isinstance(state, dict) and state, "empty/unparseable pid file"
    except Exception as exc:  # a silently-empty dict is exactly the historic bug
        record("service-pids-port-agreement", False, f"{pidfile.name}: {exc}")
        return
    try:
        listeners = netstat_listeners()
        pm = process_map()
    except Exception as exc:
        record("service-pids-port-agreement", False, f"live probe failed: {exc}")
        return

    def ancestors(pid: int) -> set[int]:
        chain, cur = set(), pid
        for _ in range(12):
            node = pm.get(cur)
            if not node:
                break
            chain.add(cur)
            cur = node[0]
        return chain

    problems: list[str] = []
    evidence: list[str] = []
    for port, key in ((7860, "ui"), (8765, "s2s"), (8010, "avatar")):
        tracked = int(state.get(key) or 0)
        owners = listeners.get(port, [])
        if owners:
            if tracked <= 0:
                problems.append(f"{key}: listener pid {owners} but tracked={tracked} (stop cannot reach it)")
                continue
            if tracked not in pm:
                problems.append(f"{key}: tracked={tracked} DEAD while :{port} listens ({owners})")
                continue
            covered = any(tracked in ancestors(o) or tracked == o for o in owners)
            if not covered:
                problems.append(f"{key}: tracked={tracked} not ancestor of owner {owners} on :{port}")
            evidence.append(f":{port} tracked={tracked} owner={owners[0]}")
        else:
            if tracked > 0 and tracked in pm:
                evidence.append(f":{port} no listener; tracked={tracked} still alive (starting?)")
            else:
                evidence.append(f":{port} no listener; tracked={tracked} (0/dead OK)")
    llm_owners = listeners.get(8090, [])
    own = state.get("llmOwnership") or {}
    if own.get("owned"):
        opid = int(own.get("pid") or 0)
        if llm_owners and opid not in llm_owners and not any(opid in ancestors(o) for o in llm_owners):
            problems.append(f"llm: ownership pid {opid} vs :8090 owners {llm_owners}")
        evidence.append(f":8090 owned={opid} actual={llm_owners}")
    else:
        evidence.append(f":8090 unclaimed-by-design actual={llm_owners}")
    record("service-pids-port-agreement", not problems,
           ("; ".join(problems) or "; ".join(evidence))[:400])


# -------------------------------------------------------- switch-state PS writer set

def check_single_switch_state_writer() -> None:
    # TSSCP01: the journal has exactly ONE writer overall — the :7860 controller
    # (voice_models_admin.py:_journal_write).  ZERO PowerShell writers is the new
    # invariant: the lifecycle script only logs its state transitions now, and any
    # PS file writing tts_switch_state.json again is a dual-authority regression.
    writers = []
    for p in list((PROJECT / "scripts").rglob("*.ps1")) + \
             list((RUNTIME / "windows").rglob("*.ps1")) + \
             list((RUNTIME / "llm").rglob("*.ps1")):
        low = p.name.lower()
        if ".bak" in low or ".retired" in low:
            continue
        text = read_text(p)
        if SWITCH_NAME in text and re.search(
                r"(Set-Content|Out-File|Add-Content|WriteAllText|Write-AtomicFile|Move-Item)[^\n]*switchFile",
                text):
            writers.append(str(p.relative_to(PROJECT)).replace("\\", "/"))
    admin_text = read_text(RUNTIME / "ui" / "app" / "voice_models_admin.py")
    controller_writes = "_journal_write(" in admin_text and "_write_json_atomic(" in admin_text
    record("switchstate-single-writer",
           writers == [] and controller_writes,
           f"ps writers={sorted(writers)} controller_writer={controller_writes}")


# ------------------------------- (e) no live state writer uses a fixed temp name

# Why this is a defect and not a style preference: on Windows a name that is *the same
# on every write* is one lingering handle away (an antivirus scan, a sibling that died
# still holding it) from failing every later write with ERROR_ACCESS_DENIED -- the state
# file stops accepting updates while the UI keeps reporting success.  Three writers here
# already converted for that reason (voice_models_admin._write_json_atomic,
# tool_layer/fs_tools, and now Runtime-Common.Write-AtomicFile); this is the ratchet that
# keeps the remaining ones converted instead of re-introducing the pattern next month.

_TMP_LITERAL_RE = re.compile(r"""["'][^"'$]*(?:\$|\{)[^"']*\.tmp["']""")
_TMP_CONCAT_RE = re.compile(r"""\+\s*["'][^"']*\.tmp["']""")
_TMP_UNIQUE_RE = re.compile(
    r"\$PID|GetFileName|NewGuid|Guid|getpid|get_ident|uuid|time_ns|_TEMP_SEQUENCE|\$\$", re.I)


def _iter_state_layer_files():
    """The layers that own production state (no vendored interpreter, models or archives)."""
    exts = {".ps1", ".py", ".sh"}
    prune = {"venv", "python", "__pycache__", "node_modules", "site-packages", "logs",
             "models", "data", "backup", "patches", "pristine", "tests", "avatars"}
    roots = (PROJECT / "scripts", RUNTIME / "windows", RUNTIME / "llm", RUNTIME / "ui",
             RUNTIME / "core", RUNTIME / "character", RUNTIME / "reading")
    for f in PROJECT.glob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            yield f
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in prune]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in exts:
                    yield p


def check_no_fixed_temp_name_writers() -> None:
    offenders, constructions, scanned = [], 0, 0
    for p in _iter_state_layer_files():
        low = p.name.lower()
        if ".bak" in low or ".retired" in low or low.startswith("pre-") or ".old" in low:
            continue
        try:
            lines = read_text(p).replace("\r\n", "\n").split("\n")
        except OSError:
            continue
        scanned += 1
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue        # prose about the old pattern is not the pattern
            if not (_TMP_LITERAL_RE.search(line) or _TMP_CONCAT_RE.search(line)):
                continue
            constructions += 1
            # A statement may wrap, so read the neighbourhood for the unique ingredient.
            if _TMP_UNIQUE_RE.search("\n".join(lines[max(0, no - 3):no + 2])):
                continue
            offenders.append(f"{p.relative_to(PROJECT).as_posix()}:{no}: {line.strip()[:80]}")
    # The sweep must actually be looking at constructions, or "no offenders" is vacuous.
    ok = not offenders and constructions >= 5 and scanned >= 100
    # Self-test of the matcher itself: the pre-fix writer must be recognised as bad.
    tripwire = _TMP_LITERAL_RE.search('    $tmp = "$switchFile.tmp"') is not None
    record("state-writers-unique-temp-name", bool(ok and tripwire),
           "; ".join(offenders[:4]) if offenders or not tripwire else
           f"{constructions} temp-name constructions in {scanned} live files, all unique"
           f" (matcher tripwire {'ok' if tripwire else 'BROKEN'})")


def main() -> int:
    check_owner_pid_survival()
    check_repo_guards()
    check_wsl_guards()
    check_tmp_guard()
    check_single_writer()
    check_avatar_pointer_source()
    check_legacy_sources_retired()
    check_single_switch_state_writer()
    check_pid_port_agreement()
    check_no_fixed_temp_name_writers()
    failed = [n for n, ok, _ in results if not ok]
    total = len(results)
    if failed:
        print(f"test_state_single_source: FAIL ({len(failed)}/{total} failed: {', '.join(failed)})")
        return 1
    print(f"test_state_single_source: PASS ({total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
