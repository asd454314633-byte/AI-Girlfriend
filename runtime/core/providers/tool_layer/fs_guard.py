"""``allowed_roots`` enforcement — the one place a path is judged (任务 §23).

Design rules, all of them mechanical rather than advisory:

* **deny by default.**  A candidate is usable only if *both* its lexically normalized
  form and its fully resolved form (:func:`os.path.realpath`, which on Windows also
  expands 8.3 short names and normalizes drive-letter case) land inside a whitelisted
  root.  Checking both is what separates a ``..`` traversal from a reparse escape.
* **every rejection is a code, never an exception.**  The caller gets a structured
  :class:`Decision` so the verdict can be handed back to a model, logged, and
  asserted on; an exception inside a real-time voice turn is exactly what 任务 §0
  forbids.
* **read is the ceiling.**  A write needs three independent yeses: the tool row
  declares ``writable``, the roots config sets ``allow_write_tools``, and the target
  root itself carries ``write``.
* **the whitelist is configuration** (:file:`roots.json`), so changing file
  permissions never means changing code — and nothing in this module knows which
  tool, model or transport asked.

Attack surfaces covered (each has a case in ``runtime/tests/test_tool_layer.py``):
``..`` traversal, absolute paths, drive-letter case, ``/`` vs ``\\``, UNC
(``\\\\server\\share``), device namespaces (``\\\\?\\``, ``\\\\.\\``), drive-relative
paths (``C:Windows``), drive-less absolutes (``\\Windows\\win.ini``), alternate data
streams (``file.txt:secret``), trailing dots/spaces (``notes.txt.``), NTFS reserved
names (``NUL``, ``CON``), wildcards, embedded NULs and control characters, and
reparse points — symlinks *and* directory junctions — pointing out of the root.
"""
from __future__ import annotations

import fnmatch
import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .roots import Root, RootsConfig, fold, project_relative
from .spec import ToolSpec

DOS_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

#: Win32 reserved characters plus every control code.
_INVALID_CHARS = set('<>|"*?') | {chr(code) for code in range(0, 32)}
#: MAX_PATH.  Longer is refused rather than silently truncated to something else.
_MAX_LENGTH = 260

_SEPARATORS = re.compile(r"[\\/]+")

EXPECT_FILE = "file"
EXPECT_DIR = "directory"
EXPECT_ANY = "any"
EXPECTS = (EXPECT_FILE, EXPECT_DIR, EXPECT_ANY)


@dataclass(slots=True)
class Decision:
    """A verdict on one path argument.  Only ``ok`` may be trusted downstream."""

    ok: bool = False
    reason: str = ""
    raw: str = ""
    normalized: str = ""
    resolved: str = ""
    relative: str = ""
    root: str = ""
    write: bool = False
    expect: str = EXPECT_FILE
    exists: bool = False
    is_dir: bool = False
    size: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_audit(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "raw": self.raw,
            "resolved": self.resolved,
            "root": self.root,
            "write": self.write,
            "size": self.size,
            "notes": list(self.notes),
        }

    def as_model_hint(self) -> str:
        """Model-facing text: a root-relative name at most, never a private path."""
        if self.ok:
            return self.relative or self.resolved
        return f"拒绝访问（{self.reason}）"


class PathGuard:
    """Judge paths against one tool's *effective* whitelist.

    The guard is bound to a :class:`~.spec.ToolSpec` so a row's ``allowed_roots``
    narrows (never widens) the system whitelist, and it is handed to the tool
    implementation as a live capability, which lets an implementation re-check
    instead of trusting the caller.
    """

    def __init__(
        self,
        config: RootsConfig,
        *,
        spec: ToolSpec | None = None,
        require_explicit_root: bool = True,
        extra_forbidden: Iterable[str] = (),
    ) -> None:
        self.config = config
        self.spec = spec
        self.require_explicit_root = bool(require_explicit_root)
        self.forbidden = [str(item) for item in config.forbidden_globs if str(item).strip()]
        self.forbidden += [str(item) for item in extra_forbidden if str(item).strip()]
        if spec is not None:
            self.forbidden += list(row_forbidden(spec))
        self.root_dropped: list[dict[str, Any]] = []
        self.roots_declared = self._select_roots(config, spec)

    # ------------------------------------------------------------------ selection
    def _select_roots(self, config: RootsConfig, spec: ToolSpec | None) -> list[Root]:
        if spec is None or spec.allowed_roots is None:
            return list(config.roots)
        declared = [_fold_declared(item, config.project_root) for item in spec.allowed_roots]
        kept: list[Root] = []
        for root in config.roots:
            folded = fold(root.path)
            if any(_is_inside(folded, item) for item in declared):
                kept.append(root)
            else:
                self.root_dropped.append({"root": str(root.path), "reason": "ROOT_NOT_DECLARED_BY_TOOL"})
        return kept

    @property
    def roots(self) -> list[Root]:
        return list(self.roots_declared)

    @property
    def missing_roots(self) -> list[str]:
        return [str(row.path) for row in self.roots_declared if not row.exists]

    def health(self) -> dict[str, Any]:
        return {
            "tool_id": self.spec.id if self.spec else "",
            "roots": [row.as_dict() for row in self.roots_declared],
            "missing_roots": self.missing_roots,
            "dropped_roots": list(self.root_dropped),
            "forbidden_globs": list(self.forbidden),
            "confine_to_project": self.config.confine_to_project,
            "allow_write_tools": self.config.allow_write_tools,
        }

    # ------------------------------------------------------------------- judging
    def check(self, value: Any, *, write: bool = False, expect: str = EXPECT_FILE) -> Decision:
        """Full verdict for one candidate.  Never raises, whatever it is handed."""
        decision = Decision(raw="" if value is None else str(value),
                            write=bool(write), expect=expect if expect in EXPECTS else EXPECT_FILE)
        try:
            self._judge(value, decision)
        except Exception as exc:  # pragma: no cover - defensive: a rejection stays data
            decision.ok = False
            decision.reason = f"PATH_GUARD_INTERNAL:{type(exc).__name__}"
            decision.notes.append("GUARD_RAISED")
        return decision

    def _judge(self, value: Any, decision: Decision) -> None:
        if not self.config.enabled:
            decision.reason = "TOOL_LAYER_DISABLED"
            return
        if self.require_explicit_root and self.spec is not None and self.spec.allowed_roots == ():
            decision.reason = "PATH_NO_EXPLICIT_ROOT"
            return
        roots = [row for row in self.roots_declared if row.exists]
        if not roots:
            decision.reason = "PATH_NO_AVAILABLE_ROOT"
            decision.notes.append(f"configured={len(self.roots_declared)}")
            return
        if decision.write:
            if not self.config.allow_write_tools:
                decision.reason = "PATH_WRITE_FORBIDDEN_BY_LAYER"
                return
            if self.spec is not None and not self.spec.writable:
                decision.reason = "PATH_WRITE_FORBIDDEN_BY_TOOL"
                return
            if not [row for row in roots if row.write]:
                decision.reason = "PATH_WRITE_FORBIDDEN_BY_ROOT"
                return

        text = self._sanitize(value, decision)
        if text is None:
            return
        absolute = self._absolutize(text, roots, decision, write=decision.write)
        if absolute is None:
            return

        lexical = os.path.normpath(absolute)
        decision.normalized = lexical
        if self._forbidden_hit(lexical):
            decision.reason = "PATH_FORBIDDEN_PATTERN"
            return
        host = _matching_root(lexical, roots, write=False)
        if host is None:
            # The lexical form alone already left the whitelist: ".." traversal, an
            # absolute path, another drive, or a case/separator trick.  This is the
            # *declared* boundary and the tighter one, so it is also the verdict an
            # operator reads: "this tool may not reach there".
            decision.reason = "PATH_OUTSIDE_ROOT"
            return
        if decision.write and not host.write:
            # Inside a root that explicitly is not writable: a different mistake, and
            # worth a different verdict for whoever reads the audit log.
            decision.reason = "PATH_WRITE_FORBIDDEN_BY_ROOT"
            return
        if self.config.confine_to_project and not _is_inside(lexical, self.config.project_root):
            # A whitelisted root that reaches outside the project, i.e. the whitelist
            # itself is over-broad.  :func:`~.roots.load_config` refuses to build such
            # a config; this second gate means a hand-made one cannot skip the rule.
            decision.reason = "PATH_OUTSIDE_PROJECT"
            return
        decision.root = str(host.path)

        try:
            resolved = os.path.realpath(lexical, strict=False)
        except OSError:
            decision.reason = "PATH_UNRESOLVABLE"
            return
        decision.resolved = os.path.normpath(resolved)
        if not _is_inside(decision.resolved, host.path):
            # Lexically fine, physically not: a symlink, a junction, a mount point or
            # an 8.3 short name carried the read somewhere else.
            decision.reason = "PATH_REPARSE_ESCAPE"
            decision.notes.append("RESOLVES_ELSEWHERE")
            return
        if self.config.confine_to_project and not _is_inside(decision.resolved, self.config.project_root):
            decision.reason = "PATH_OUTSIDE_PROJECT"
            return
        if os.path.islink(decision.resolved) and not self.config.follow_symlinks:
            decision.reason = "PATH_SYMLINK_REFUSED"
            return
        self._finish(decision)

    # ------------------------------------------------------------------ sanitize
    def _sanitize(self, value: Any, decision: Decision) -> str | None:
        if not isinstance(value, str):
            decision.reason = "PATH_NOT_TEXT"
            return None
        text = value.strip()
        if value != text:
            decision.notes.append("SURROUNDING_SPACE_STRIPPED")
        if not text:
            decision.reason = "PATH_EMPTY"
            return None
        if "\x00" in text:
            decision.reason = "PATH_NULL_BYTE"
            return None
        if any(ord(char) < 0x20 for char in text):
            decision.reason = "PATH_CONTROL_CHAR"
            return None
        if len(text) > _MAX_LENGTH:
            decision.reason = "PATH_TOO_LONG"
            return None
        # Namespace prefixes are checked before the character blacklist: both refuse
        # the candidate, but ``PATH_DEVICE_NAMESPACE`` / ``PATH_UNC_FORBIDDEN`` tells
        # the operator *which* trick was tried, and "the ? in \\?\ is illegal" would
        # read like a typo in the filename.
        if text.startswith(("\\\\.\\", "//./", "\\\\?\\", "//?/")):
            decision.reason = "PATH_DEVICE_NAMESPACE"
            return None
        if text.startswith(("\\\\", "//")):
            decision.reason = "PATH_UNC_FORBIDDEN"
            return None
        if any(char in text for char in _INVALID_CHARS):
            decision.reason = "PATH_INVALID_CHAR"
            return None
        if len(text) > 1 and text[1] == ":":
            if text[0] not in string.ascii_letters:
                decision.reason = "PATH_BAD_DRIVE"
                return None
            if not _SEPARATORS.match(text[2:3]):
                # drive-relative ("C:Windows") resolves against an arbitrary cwd on
                # that drive, which is never what a model meant.
                decision.reason = "PATH_DRIVE_RELATIVE"
                return None
            text = f"{text[0].upper()}:{text[2:]}"
            decision.notes.append("DRIVE_CASE_NORMALIZED")
        elif text.startswith(("\\", "/")):
            decision.reason = "PATH_DRIVE_LESS_ABSOLUTE"
            return None
        if ":" in text[2:]:
            decision.reason = "PATH_DATA_STREAM"
            return None
        stripped = text.rstrip(". ")
        if stripped != text:
            # NTFS ignores trailing dots and spaces: "notes.txt." *is* "notes.txt".
            decision.notes.append("TRAILING_DOT_OR_SPACE_STRIPPED")
            text = stripped
        if not text:
            decision.reason = "PATH_EMPTY"
            return None
        segments = [row for row in _SEPARATORS.split(text) if row]
        if not segments:
            decision.reason = "PATH_EMPTY"
            return None
        if any(_device_name(row) for row in segments):
            decision.reason = "PATH_DEVICE_NAME"
            return None
        if any(row == ".." for row in segments):
            decision.notes.append("PARENT_TRAVERSAL_PRESENT")
        return text

    def _absolutize(self, text: str, roots: Sequence[Root], decision: Decision, *,
                    write: bool = False) -> str | None:
        if Path(text).is_absolute():
            return os.path.normpath(text)
        pieces = [row for row in _SEPARATORS.split(text) if row]
        if not pieces:
            decision.reason = "PATH_EMPTY"
            return None
        # A relative name is looked for inside each whitelisted root, in whitelist
        # order — and a *write* prefers the writable ones, so "note.txt" cannot
        # silently aim at a read-only root and then fail on a permission rule.
        candidates = list(roots)
        if write:
            candidates = [row for row in roots if row.write] + [row for row in roots if not row.write]
        for root in candidates:
            candidate = os.path.normpath(os.path.join(str(root.path), *pieces))
            if os.path.exists(candidate):
                decision.notes.append(f"RESOLVED_RELATIVE_TO={root.declared}")
                return candidate
        # Nothing exists yet (typically a write into a whitelisted directory): the
        # first candidate root is the documented base, and containment still decides.
        decision.notes.append(f"RELATIVE_TO_PRIMARY_ROOT={candidates[0].declared}")
        return os.path.normpath(os.path.join(str(candidates[0].path), *pieces))

    # --------------------------------------------------------------------- stat
    def _finish(self, decision: Decision) -> None:
        target = decision.resolved
        exists = os.path.exists(target)
        decision.exists = bool(exists)
        decision.is_dir = bool(exists and os.path.isdir(target))
        decision.size = None if not exists or decision.is_dir else _size_of(target)
        decision.relative = _relative_to(target, decision.root)
        if decision.write:
            parent = os.path.dirname(target)
            if decision.is_dir:
                decision.reason = "PATH_WRITE_TARGET_IS_DIR"
                return
            if exists and not self._overwrite_allowed():
                decision.reason = "PATH_WRITE_OVERWRITE_FORBIDDEN"
                return
            if not os.path.isdir(parent):
                decision.reason = "PATH_WRITE_PARENT_MISSING"
                return
            decision.ok = True
            return
        if not exists:
            decision.reason = "PATH_NOT_FOUND"
            return
        if decision.expect == EXPECT_FILE and decision.is_dir:
            decision.reason = "PATH_NOT_A_FILE"
            return
        if decision.expect == EXPECT_DIR and not decision.is_dir:
            decision.reason = "PATH_NOT_A_DIRECTORY"
            return
        decision.ok = True

    def _overwrite_allowed(self) -> bool:
        if self.spec is None:
            return False
        entry = self.spec.note_for("overwrite")
        return bool(entry.get("default", False))

    # ------------------------------------------------------------------ patterns
    def _forbidden_hit(self, candidate: str) -> bool:
        """Match against the whole path, its basename and every ancestor prefix."""
        folded = fold(candidate)
        parts = [row for row in _SEPARATORS.split(folded) if row]
        probes = [folded, parts[-1] if parts else ""]
        accumulated = ""
        for row in parts:
            accumulated = row if not accumulated else os.path.normpath(os.path.join(accumulated, row))
            probes.append(fold(accumulated))
        for pattern in self.forbidden:
            text = fold(pattern)
            loose = fold(pattern.replace("**", "").strip("\\/"))
            for probe in probes:
                if not probe:
                    continue
                if _glob_match(text, probe) or _glob_match(loose, probe):
                    return True
                if "**" in text and loose and _is_inside(probe, loose):
                    return True
        return False


# --------------------------------------------------------------------- module glue
def row_forbidden(spec: ToolSpec) -> tuple[str, ...]:
    """A tool row may add its own deny patterns via ``forbidden_globs``."""
    row = spec.raw.get("forbidden_globs") or []
    return tuple(str(item) for item in row) if isinstance(row, list) else ()


def guard_for(config: RootsConfig, spec: ToolSpec | None, *,
              require_explicit_root: bool = True,
              extra_forbidden: Iterable[str] = ()) -> PathGuard:
    return PathGuard(config, spec=spec, require_explicit_root=require_explicit_root,
                     extra_forbidden=extra_forbidden)


def _fold_declared(value: str, project_root: Path) -> str:
    """Fold a tool row's own root declaration so it can meet a system root.

    ``roots.json`` keeps system roots resolved to their real absolute form, so a row
    that names the same directory relatively (``runtime/character``) or with a
    different drive-letter case has to be resolved the same way before it can narrow
    anything.  Resolving is *not* widening: a relative name is joined to the project
    root, and the row still keeps only the system roots that sit inside what it named.
    """
    candidate = project_relative(str(value), project_root)
    try:
        candidate = Path(os.path.realpath(str(candidate), strict=False))
    except OSError:  # pragma: no cover - realpath only fails on pathological input
        pass
    return fold(candidate)


def _device_name(segment: str) -> str | None:
    head = segment.split(".")[0].upper()
    return head if head in DOS_DEVICE_NAMES else None


def _size_of(path: str) -> int | None:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return None


def _glob_match(pattern: str, candidate: str) -> bool:
    """``fnmatch`` plus ``**`` meaning "any depth"."""
    if "**" in pattern:
        body = re.escape(pattern).replace(re.escape("**"), "\x00").replace(re.escape("*"), "[^\\\\/]*")
        return re.fullmatch(body.replace("\x00", ".*"), candidate) is not None
    return fnmatch.fnmatchcase(candidate, pattern)


def _is_inside(candidate: Path | str, parent: Path | str) -> bool:
    left, right = fold(candidate), fold(parent)
    if left == right:
        return True
    stem = right.rstrip("\\/")
    return left.startswith(stem + "\\") or left.startswith(stem + "/")


def _matching_root(candidate: Path | str, roots: Sequence[Root], *, write: bool) -> Root | None:
    for root in roots:
        if write and not root.write:
            continue
        if _is_inside(candidate, root.path):
            return root
    return None


def _relative_to(candidate: str, parent: str) -> str:
    left, right = fold(candidate), fold(parent)
    if left.startswith(right) and len(left) > len(right):
        return left[len(right):].lstrip("\\/")
    return left
