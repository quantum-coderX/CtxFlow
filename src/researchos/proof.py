"""Formal proof checking through a bounded Lean 4 adapter.

The adapter elaborates Lean source in a fresh temporary directory (or inside
an allowlisted Lake project), with a deadline and a scrubbed environment. It
reports one of ``verified``, ``failed``, ``error`` or ``unavailable`` and
never presents natural-language reasoning or model confidence as a proof.

Source is screened before execution: commands that run arbitrary code at
elaboration time (``#eval``, ``run_cmd``, custom elaborators, ``IO``,
``unsafe``, ``extern``/``implemented_by``, ``initialize``) are rejected. This
is defence in depth on top of process isolation, not a full sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field, ValidationError

from researchos.analytics import _Strict, _validation_message
from researchos.project_store import ProjectStore, safe_key, sha256_bytes, stable_hash

PROOF_ENGINE_VERSION = "proof-lean-1.0"
MAX_SOURCE_CHARS = 200_000
DEFAULT_TIMEOUT_S = 120.0
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

_FORBIDDEN_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"#eval\b", "#eval executes code"),
    (r"#exec\b", "#exec executes code"),
    (r"\brun_cmd\b|\brun_elab\b|\brun_meta\b", "run_cmd/run_elab/run_meta execute code"),
    (r"\belab(_rules)?\b", "custom elaborators execute code"),
    (r"@\[[^\]]*\b(command_elab|term_elab|tactic|init|extern|implemented_by|export|env_linter)\b", "attribute registers executable code"),
    (r"\b(builtin_)?initialize\b", "initialize blocks execute code"),
    (r"\bunsafe\b", "unsafe definitions are not allowed"),
    (r"\bIO\b|\bIO\.|System\.|Process\.", "IO/System access is not allowed"),
    (r"\bimport\s+(Lean|Lake)\b", "importing the Lean/Lake meta-framework is not allowed"),
)
_DECL_RE = r"^[A-Za-z_][A-Za-z0-9_'.]{0,200}$"


class ProofError(ValueError):
    """A proof-check request is invalid."""


class ProofCheckRequest(_Strict):
    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    declaration: Optional[str] = Field(default=None, pattern=_DECL_RE)
    project: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, ge=1.0, le=600.0)


def screen_source(source: str) -> List[str]:
    """Return policy violations (empty when the source may be elaborated)."""
    stripped = re.sub(r"/-.*?-/", " ", source, flags=re.S)
    stripped = re.sub(r"--[^\n]*", " ", stripped)
    return [reason for pattern, reason in _FORBIDDEN_PATTERNS if re.search(pattern, stripped)]


def detect_lean() -> Dict[str, Optional[str]]:
    lean = os.environ.get("RESEARCHOS_LEAN_BIN") or shutil.which("lean")
    lake = os.environ.get("RESEARCHOS_LAKE_BIN") or shutil.which("lake")
    return {"lean": lean, "lake": lake}


def _projects_root() -> Optional[Path]:
    raw = os.environ.get("RESEARCHOS_LEAN_PROJECTS_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _scrubbed_env() -> Dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "ELAN_HOME", "LEAN_PATH", "LANG", "LC_ALL")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _parse_messages(stdout: str) -> List[Dict[str, Any]]:
    messages = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        pos = payload.get("pos") or {}
        end = payload.get("endPos") or {}
        messages.append({
            "severity": payload.get("severity", "information"),
            "line": pos.get("line"),
            "column": pos.get("column"),
            "end_line": end.get("line") if isinstance(end, dict) else None,
            "message": str(payload.get("data", ""))[:4000],
        })
    return messages


def _axioms(messages: List[Dict[str, Any]], declaration: str) -> Optional[List[str]]:
    for message in messages:
        text = message["message"]
        if f"'{declaration}' depends on axioms" in text:
            inner = text.split("[", 1)[1].rsplit("]", 1)[0] if "[" in text else ""
            return sorted(a.strip() for a in inner.split(",") if a.strip())
        if f"'{declaration}' does not depend on any axioms" in text:
            return []
    return None


def _version(lean: str, env: Dict[str, str]) -> Optional[str]:
    try:
        probe = subprocess.run([lean, "--version"], capture_output=True, timeout=20, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = probe.stdout.decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text else None


class ProofChecker:
    """Run and record Lean checks for a project."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def availability(self) -> Dict[str, Any]:
        tools = detect_lean()
        root = _projects_root()
        return {
            "available": bool(tools["lean"]),
            "lean": bool(tools["lean"]),
            "lake": bool(tools["lake"]),
            "projects_dir_configured": root is not None,
        }

    def check(self, request: Dict[str, Any], *, project_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            parsed = ProofCheckRequest.model_validate(request)
        except ValidationError as exc:
            raise ProofError(f"Invalid proof request: {_validation_message(exc)}") from None
        started = time.monotonic()
        source_hash = sha256_bytes(parsed.source.encode("utf-8"))
        base = {
            "engine_version": PROOF_ENGINE_VERSION,
            "declaration": parsed.declaration,
            "project": parsed.project,
            "source_sha256": source_hash,
            "toolchain": None,
            "diagnostics": [],
            "axioms": None,
        }
        violations = screen_source(parsed.source)
        if violations:
            return self._record(project_id, {**base, "status": "rejected", "verified": False,
                                             "reason": "; ".join(violations), "duration_ms": 0.0})
        tools = detect_lean()
        if not tools["lean"]:
            return self._record(project_id, {**base, "status": "unavailable", "verified": False,
                                             "reason": "Lean is not installed (set RESEARCHOS_LEAN_BIN or install elan).",
                                             "duration_ms": 0.0})
        env = _scrubbed_env()
        cwd: Optional[Path] = None
        if parsed.project:
            root = _projects_root()
            if root is None:
                raise ProofError("Lean projects are disabled; set RESEARCHOS_LEAN_PROJECTS_DIR.")
            cwd = (root / parsed.project).resolve()
            has_lakefile = (cwd / "lakefile.lean").exists() or (cwd / "lakefile.toml").exists()
            if root not in cwd.parents or not has_lakefile:
                raise ProofError(f"Lean project {parsed.project!r} is not an allowlisted Lake project.")
            if not tools["lake"]:
                return self._record(project_id, {**base, "status": "unavailable", "verified": False,
                                                 "reason": "lake is not installed.", "duration_ms": 0.0})
        toolchain = _version(tools["lean"], env)
        source = parsed.source.rstrip() + "\n"
        if parsed.declaration:
            source += f"\n#print axioms {parsed.declaration}\n"
        with tempfile.TemporaryDirectory(prefix="researchos-lean-") as temp:
            target = Path(temp) / "Check.lean"
            target.write_text(source, encoding="utf-8")
            if cwd is not None:
                cmd = [tools["lake"], "env", tools["lean"], "--json", str(target)]
            else:
                cmd = [tools["lean"], "--json", str(target)]
            try:
                completed = subprocess.run(
                    cmd, cwd=str(cwd or temp), env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=parsed.timeout_s, check=False,
                )
            except subprocess.TimeoutExpired:
                return self._record(project_id, {**base, "toolchain": toolchain, "status": "error", "verified": False,
                                                 "reason": f"Lean exceeded the {parsed.timeout_s:.0f}s limit.",
                                                 "duration_ms": (time.monotonic() - started) * 1000})
            except OSError as exc:
                return self._record(project_id, {**base, "toolchain": toolchain, "status": "error", "verified": False,
                                                 "reason": f"Could not start Lean: {exc}",
                                                 "duration_ms": (time.monotonic() - started) * 1000})
        stdout = completed.stdout.decode("utf-8", "replace")
        messages = _parse_messages(stdout)
        errors = [m for m in messages if m["severity"] == "error"]
        sorry = [m for m in messages if "declaration uses 'sorry'" in m["message"]]
        axioms = _axioms(messages, parsed.declaration) if parsed.declaration else None
        reasons: List[str] = []
        if completed.returncode not in (0, 1) and not messages:
            status = "error"
            reasons.append(f"Lean exited with code {completed.returncode}: "
                           f"{completed.stderr.decode('utf-8', 'replace')[-1000:]}")
        else:
            if errors:
                reasons.append(f"{len(errors)} elaboration error(s).")
            if sorry:
                reasons.append("Proof uses sorry.")
            if parsed.declaration and axioms is None and not errors:
                reasons.append(f"Declaration {parsed.declaration!r} was not found.")
            nonstandard = sorted(set(axioms or []) - STANDARD_AXIOMS)
            if "sorryAx" in nonstandard and not sorry:
                reasons.append("Declaration depends on sorryAx.")
            nonstandard = [a for a in nonstandard if a != "sorryAx"]
            if nonstandard:
                reasons.append(f"Depends on non-standard axioms: {', '.join(nonstandard)}.")
            status = "failed" if reasons else "verified"
        if status == "verified" and not parsed.declaration:
            reasons.append("No target declaration given; the whole file elaborated without errors or sorry.")
        return self._record(project_id, {
            **base,
            "toolchain": toolchain,
            "status": status,
            "verified": status == "verified",
            "reason": " ".join(reasons),
            "diagnostics": messages[:200],
            "axioms": axioms,
            "duration_ms": (time.monotonic() - started) * 1000,
        })

    def _record(self, project_id: Optional[str], result: Dict[str, Any]) -> Dict[str, Any]:
        result["checked_at"] = time.time()
        result["check_id"] = "pf-" + stable_hash({"source": result["source_sha256"], "decl": result["declaration"],
                                                  "at": result["checked_at"]})
        if project_id:
            self.store.write_json(self.store.kind_dir(project_id, "proofs") / f"{result['check_id']}.json", result)
        return result

    def list(self, project_id: str) -> List[Dict[str, Any]]:
        records = list(self.store.iter_json(self.store.kind_dir(project_id, "proofs")))
        records.sort(key=lambda item: item.get("checked_at", 0), reverse=True)
        return records

    def get(self, project_id: str, check_id: str) -> Dict[str, Any]:
        record = self.store.read_json(self.store.kind_dir(project_id, "proofs") / f"{safe_key(check_id)}.json")
        if not isinstance(record, dict):
            raise ProofError(f"Proof check {check_id!r} not found.")
        return record
