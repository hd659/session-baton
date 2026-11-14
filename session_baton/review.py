"""Relay review: fold the receiver's tool calls into phases and check them against the baton."""
from __future__ import annotations

import fnmatch
import os
import re
from datetime import datetime

from .core import SCOPED_RE, command_of, files_of, git, rel, tool_class
from .sources import Transcript, discover, load

RISKY = [
    (re.compile(r"git\s+push\s+.*(--force|-f\b)"), "强推覆盖远端历史"),
    (re.compile(r"git\s+reset\s+--hard"), "硬重置丢弃改动"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"), "递归强删"),
    (re.compile(r"git\s+(checkout|restore)\s+(--\s+)?\.(\s|$)"), "整体丢弃工作区改动"),
    (re.compile(r"(grep|rg|find)\s+[^|]*\s(~|\$HOME|/Users/[^/\s]+)/?(\s|$)"), "全盘扫描主目录"),
    (re.compile(r"curl[^|]*\|\s*(ba|z)?sh"), "管道执行远程脚本"),
]
TEST_RE = re.compile(r"(?:^|[;&|(]\s*|\s-m\s+|/)(pytest|unittest|npm (run )?test|go test|cargo test|"
                     r"npx (jest|vitest)|jest|vitest|make test)\b")
SHIP_RE = re.compile(r"\bgit\s+(commit|push|tag)\b")
PHASE_NAMES = {"search": "摸底", "read": "摸底", "write": "改代码", "test": "跑测试",
               "ship": "提交推送", "run": "执行命令", "other": "其他操作"}
TOKEN_RE = re.compile(r"[A-Za-z_][\w.\-/]{2,}|[\u4e00-\u9fff]{2,}")
STOP = {"不要", "不能", "禁止", "严禁", "其他", "一样", "但是", "only", "never", "except", "don't"}


def step_class(tool: str, args: dict) -> str:
    cls = tool_class(tool)
    if cls == "run":
        cmd = command_of(args)
        if TEST_RE.search(cmd):
            return "test"
        if SHIP_RE.search(cmd):
            return "ship"
    return cls


def describe(tool: str, args: dict, root: str) -> str:
    cmd = command_of(args)
    if cmd:
        return cmd.replace("\n", " ")[:120]
    files = [rel(f, root) for f in files_of(args)]
    return f"{tool} {', '.join(files)}".strip()[:120]


def fold(t: Transcript, root: str) -> list[dict]:
    """Group tool calls into phases.

    A new phase starts whenever the agent narrates (assistant text) or the kind of
    work changes (explore -> edit -> test -> ship). Consecutive same-kind calls merge.
    A phase with no preceding narration is marked silent.
    """
    phases, cur, note = [], None, ""
    for e in t.events:
        if e.kind == "assistant":
            text = e.text.strip()
            if text:
                note, cur = text.splitlines()[0][:140], None
            continue
        if e.kind != "tool":
            continue
        name = PHASE_NAMES.get(step_class(e.tool, e.args), "其他操作")
        if name == "其他操作" and cur is not None:
            continue  # bookkeeping (todo/plan) rides along with the current phase
        if cur is None or cur["name"] != name:
            cur = {"name": name, "note": note, "narrated": bool(note), "steps": []}
            phases.append(cur)
            note = ""
        cur["steps"].append(describe(e.tool, e.args, root))
    for p in phases:
        p["count"] = len(p["steps"])
    return phases


def longest_silent(t: Transcript) -> int:
    best = run = 0
    for e in t.events:
        if e.kind == "tool":
            run += 1
            best = max(best, run)
        elif e.kind == "assistant":
            run = 0
    return best


def in_scope(path: str, scope: list[str]) -> bool:
    for s in scope:
        s = s.rstrip("/")
        if path == s or path.startswith(s + "/") or fnmatch.fnmatch(path, s):
            return True
    return False


def changed_files(root: str, anchor: str) -> list[str]:
    files = set()
    if anchor:
        files |= set(git(root, "diff", "--name-only", anchor).splitlines())
        files |= set(git(root, "diff", "--name-only", f"{anchor}..HEAD").splitlines())
    for ln in git(root, "status", "--porcelain", "--untracked-files=all").splitlines():
        if len(ln) > 3:
            files.add(ln[3:].split(" -> ")[-1])
    return sorted(f for f in files if f)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in TOKEN_RE.findall(text)} - STOP


def boundary_hits(boundaries: list[str], cmds: list[str], files: list[str]) -> list[dict]:
    hits, hay = [], "\n".join(cmds + files).lower()
    for b in boundaries:
        if not re.search(r"不要|别|禁止|严禁|不能|不许|don't|do not|never|must not", b, re.I):
            continue
        toks = [w for w in _tokens(b) if re.match(r"[a-z_]", w) and len(w) >= 3]
        matched = [w for w in toks if w in hay]
        if matched:
            hits.append({"boundary": b, "matched": matched})
    for c in cmds:
        for rx, why in RISKY:
            if rx.search(c):
                hits.append({"boundary": why, "matched": [c[:100]]})
    return hits


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def todo_status(todos: list[str], t: Transcript) -> list[dict]:
    said = _bigrams(" ".join(e.text for e in t.events if e.kind == "assistant"))
    out = []
    for td in todos:
        grams = _bigrams(td)
        ratio = len(grams & said) / len(grams) if grams else 0
        out.append({"todo": td, "mentioned": ratio >= 0.5})
    return out


def find_receiver(baton: dict, timeout: float = 5.0) -> Transcript | None:
    created = datetime.fromisoformat(baton["created"]).timestamp()
    src = baton["source"]["path"]
    for _h, p, mtime in discover(cwd=baton["root"], timeout=timeout):
        if str(p) != src and mtime >= created:
            return load(p)
    return None


def review(baton: dict, receiver: Transcript | None) -> dict:
    root, scope = baton["root"], baton.get("scope", [])
    anchor = baton.get("git", {}).get("head", "")
    events = receiver.events if receiver else []
    cmds = [command_of(e.args) for e in events if e.kind == "tool" and command_of(e.args)]
    written = [rel(f, root) for e in events if e.kind == "tool" and tool_class(e.tool) == "write"
               for f in files_of(e.args)]
    changed = sorted(set(changed_files(root, anchor)) | set(written))
    outside = [f for f in changed if scope and not in_scope(f, scope)]
    phases = fold(receiver, root) if receiver else []
    return {
        "receiver": {"harness": receiver.harness, "session_id": receiver.session_id,
                     "path": receiver.path} if receiver else None,
        "anchor": anchor,
        "new_commits": git(root, "log", "--oneline", f"{anchor}..HEAD").splitlines() if anchor else [],
        "phases": phases,
        "tool_calls": sum(p["count"] for p in phases),
        "silent_streak": longest_silent(receiver) if receiver else 0,
        "changed": changed,
        "out_of_scope": outside,
        "scope_declared": bool(scope),
        "boundary_hits": boundary_hits(baton.get("boundaries", []), cmds, changed),
        "manual_checks": [b for b in baton.get("boundaries", []) if SCOPED_RE.fullmatch(b)],
        "todos": todo_status(baton.get("todos", []), receiver) if receiver else [],
        "verified": any(TEST_RE.search(c) for c in cmds),
    }
