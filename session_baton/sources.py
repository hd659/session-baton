"""Bounded transcript discovery and parsing for Claude Code, Codex CLI and Pi.

Discovery only walks three whitelisted roots with a depth cap, a file cap and a
wall-clock deadline. It never walks $HOME itself.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOTS = {  # harness -> (relative root, max depth below root)
    "claude": (".claude/projects", 2),
    "codex": (".codex/sessions", 5),
    "pi": (".pi/agent/sessions", 2),
}
INJECTED_PREFIXES = (
    "<environment_context", "<user_instructions", "# AGENTS.md", "<permissions",
    "<command-name", "<local-command", "<turn_aborted", "Caveat:", "<system-reminder",
)
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


@dataclass
class Event:
    kind: str  # user | assistant | tool
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    ts: str = ""


@dataclass
class Transcript:
    harness: str
    path: str
    session_id: str = ""
    cwd: str = ""
    events: list = field(default_factory=list)

    @property
    def started(self) -> str:
        return next((e.ts for e in self.events if e.ts), "")


def _norm(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-").lower()


def _first_json(path: Path) -> dict:
    try:
        with path.open(errors="ignore") as fh:
            return json.loads(fh.readline() or "{}")
    except (OSError, ValueError):
        return {}


def _walk(root: Path, depth: int, deadline: float, cap: int):
    stack, found = [(root, 0)], 0
    while stack and found < cap and time.monotonic() < deadline:
        cur, d = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for ent in entries:
            if ent.is_dir(follow_symlinks=False) and d < depth:
                stack.append((Path(ent.path), d + 1))
            elif ent.name.endswith(".jsonl") and ent.is_file(follow_symlinks=False):
                found += 1
                yield Path(ent.path)


def discover(home: str | None = None, harness: str | None = None, cwd: str | None = None,
             timeout: float = 5.0, cap: int = 4000) -> list[tuple[str, Path, float]]:
    """Return [(harness, path, mtime)] newest first, optionally filtered by cwd."""
    base = Path(home or os.environ.get("BATON_HOME") or Path.home())
    deadline = time.monotonic() + timeout
    want = _norm(os.path.abspath(cwd)) if cwd else None
    out = []
    for name, (rel, depth) in ROOTS.items():
        if harness and harness != name:
            continue
        root = base / rel
        if not root.is_dir():
            continue
        for p in _walk(root, depth, deadline, cap):
            if want:
                if name == "codex":
                    meta = _first_json(p).get("payload") or {}
                    if _norm(str(meta.get("cwd", ""))) != want:
                        continue
                elif _norm(p.parent.name) != want:
                    continue
            try:
                out.append((name, p, p.stat().st_mtime))
            except OSError:
                continue
    return sorted(out, key=lambda x: x[2], reverse=True)


def _clean_user(text: str) -> str:
    text = REMINDER_RE.sub("", text or "").strip()
    return "" if text.startswith(INJECTED_PREFIXES) else text


def _texts(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"):
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def _json_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"input": raw}
    except (TypeError, ValueError):
        return {"input": raw or ""}


def detect(first: dict) -> str:
    if first.get("type") == "session" and "version" in first:
        return "pi"
    if "payload" in first:
        return "codex"
    return "claude"


def _claude(o: dict, t: Transcript):
    t.session_id = t.session_id or o.get("sessionId", "")
    t.cwd = t.cwd or o.get("cwd", "")
    if o.get("type") not in ("user", "assistant") or o.get("isSidechain"):
        return
    msg, ts = o.get("message") or {}, o.get("timestamp", "")
    content = msg.get("content")
    if o["type"] == "user":
        if isinstance(content, list) and any(c.get("type") == "tool_result" for c in content
                                             if isinstance(c, dict)):
            return
        text = _clean_user(_texts(content))
        if text:
            t.events.append(Event("user", text, ts=ts))
        return
    for c in content if isinstance(content, list) else [{"type": "text", "text": content}]:
        if c.get("type") == "text" and (c.get("text") or "").strip():
            t.events.append(Event("assistant", c["text"], ts=ts))
        elif c.get("type") == "tool_use":
            t.events.append(Event("tool", tool=c.get("name", ""), args=c.get("input") or {}, ts=ts))


def _codex(o: dict, t: Transcript):
    p, ts = o.get("payload") or {}, o.get("timestamp", "")
    if o.get("type") == "session_meta":
        t.session_id, t.cwd = p.get("id", ""), p.get("cwd", "")
        return
    if o.get("type") != "response_item":
        return
    kind = p.get("type")
    if kind == "message" and p.get("role") in ("user", "assistant"):
        text = _texts(p.get("content"))
        text = _clean_user(text) if p["role"] == "user" else text.strip()
        if text:
            t.events.append(Event(p["role"], text, ts=ts))
    elif kind in ("function_call", "custom_tool_call"):
        raw = p.get("arguments") if kind == "function_call" else p.get("input")
        t.events.append(Event("tool", tool=p.get("name", ""), args=_json_args(raw), ts=ts))
    elif kind == "local_shell_call":
        cmd = (p.get("action") or {}).get("command") or []
        t.events.append(Event("tool", tool="shell", args={"command": cmd}, ts=ts))


def _pi(o: dict, t: Transcript):
    if o.get("type") == "session":
        t.session_id, t.cwd = o.get("id", ""), o.get("cwd", "")
        return
    msg, ts = o.get("message") or {}, o.get("timestamp", "")
    if o.get("type") != "message" or msg.get("role") not in ("user", "assistant"):
        return
    if msg["role"] == "user":
        text = _clean_user(_texts(msg.get("content")))
        if text:
            t.events.append(Event("user", text, ts=ts))
        return
    for c in msg.get("content") or []:
        if c.get("type") == "text" and (c.get("text") or "").strip():
            t.events.append(Event("assistant", c["text"], ts=ts))
        elif c.get("type") == "toolCall":
            t.events.append(Event("tool", tool=c.get("name", ""), args=_json_args(c.get("arguments")), ts=ts))


PARSERS = {"claude": _claude, "codex": _codex, "pi": _pi}


def load(path: str | Path, harness: str | None = None) -> Transcript:
    path = Path(path)
    harness = harness or detect(_first_json(path))
    t, fn = Transcript(harness, str(path)), PARSERS[harness]
    with path.open(errors="ignore") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                fn(obj, t)
    t.session_id = t.session_id or path.stem
    return t
