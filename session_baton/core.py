"""Baton packing: extract goal, directives, scoped boundaries, file scope, todos, git anchor."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from . import __version__
from .sources import Transcript

WRITE_HINTS = {"edit", "write", "patch", "multiedit", "notebookedit", "create", "str_replace"}
RUN_HINTS = {"bash", "shell", "exec", "run", "task", "terminal", "command"}
READ_HINTS = {"read", "view", "cat", "open"}
SEARCH_HINTS = {"grep", "find", "ls", "glob", "search", "list"}
OVERRIDES = {"todowrite": "other", "todo": "other", "update_plan": "other",
             "write_stdin": "run", "send_input": "run", "exec_command": "run"}
NAME_SPLIT_RE = re.compile(r"[_\-\s.]+|(?<=[a-z])(?=[A-Z])")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
SCOPE_WORDS = r"指的是|是指|只是|只指|仅限|只限|我说的|我的意思是|是类似|类似"
SCOPED_RE = re.compile(
    rf"[^。！？\n]*(?:{SCOPE_WORDS})[^。！？\n]*(?:但是|但|其他|其余)[^。！？\n]*(?:一样|一致|相同|不变|照旧)[^。！？\n]*")
BOUNDARY_RES = [
    SCOPED_RE,
    re.compile(r"[^。！？\n]*(?:不要|(?<![特区分类识级性告离辨鉴派])别(?![人的处])|不准|禁止|严禁|"
               r"(?<![能可])不能(?!不)|不许|千万别)[^。！？\n]{2,80}"),
    re.compile(r"[^.!?\n]*\b(?:don't|do not|never|must not|only|except)\b[^.!?\n]{3,120}", re.I),
]
QUESTION_RE = re.compile(r"能不能|可不可以|要不要|是不是|会不会|吗$")
TODO_RES = [
    re.compile(r"^\s*[-*]\s*\[ \]\s*(.+)$", re.M),
    re.compile(r"(?:TODO|待办|下一步|接下来|还没|还差|剩下)[:：]?\s*([^\n。]{4,100})"),
]


def tool_class(name: str) -> str:
    last = name.rsplit("__", 1)[-1]
    for key, cls in OVERRIDES.items():
        if last.lower() == key or last.lower().endswith("_" + key):
            return cls
    toks = {t.lower() for t in NAME_SPLIT_RE.split(last) if t} | {last.lower()}
    for cls, hints in (("write", WRITE_HINTS), ("run", RUN_HINTS),
                       ("search", SEARCH_HINTS), ("read", READ_HINTS)):
        if toks & hints:
            return cls
    return "other"


def command_of(args: dict) -> str:
    cmd = args.get("command") or args.get("cmd") or args.get("script") or ""
    return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)


def files_of(args: dict) -> list[str]:
    out = []
    for key in ("file_path", "path", "notebook_path", "filePath", "target_file"):
        if isinstance(args.get(key), str):
            out.append(args[key])
    patch = args.get("input") or args.get("patch") or ""
    if isinstance(patch, str):
        out += [m.strip() for m in PATCH_FILE_RE.findall(patch)]
    return out


def rel(path: str, root: str) -> str:
    if not path:
        return path
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(root or ".", path))
    if root and (full == root or full.startswith(root.rstrip("/") + "/")):
        return os.path.relpath(full, root)
    return full


def _dedupe(items, limit=None):
    seen, out = set(), []
    for it in items:
        k = it.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[-limit:] if limit else out


def boundaries(texts: list[str]) -> list[str]:
    found = []
    for text in texts:
        for rx in BOUNDARY_RES:
            found += [m.group(0).strip(" ，,;；") for m in rx.finditer(text)]
    found = [b for b in found if 4 <= len(b) <= 160 and not QUESTION_RE.search(b)]
    return _dedupe(found, limit=20)


def open_todos(t: Transcript) -> list[str]:
    tail = [e.text for e in t.events if e.kind == "assistant"][-6:]
    found = []
    for text in tail:
        for rx in TODO_RES:
            found += rx.findall(text)
    return _dedupe(found, limit=12)


def git(root: str, *args: str, timeout: float = 10.0) -> str:
    try:
        res = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, timeout=timeout)
        return res.stdout.rstrip() if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_anchor(root: str) -> dict:
    head = git(root, "rev-parse", "HEAD")
    dirty = [ln[3:] for ln in git(root, "status", "--porcelain").splitlines() if ln]
    return {"head": head, "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": dirty, "log": git(root, "log", "--oneline", "-5").splitlines()}


def pack(t: Transcript, root: str | None = None, extra_scope: list[str] | None = None) -> dict:
    root = os.path.abspath(root or t.cwd or ".")
    users = [e.text for e in t.events if e.kind == "user"]
    touched, reads, cmds = [], [], []
    for e in t.events:
        if e.kind != "tool":
            continue
        cls = tool_class(e.tool)
        if cls == "write":
            touched += [rel(f, root) for f in files_of(e.args)]
        elif cls in ("read", "search"):
            reads += [rel(f, root) for f in files_of(e.args)]
        elif cls == "run":
            cmds.append(command_of(e.args)[:160])
    touched = _dedupe(touched)
    scope = _dedupe(touched + list(extra_scope or []))
    last_reply = next((e.text for e in reversed(t.events) if e.kind == "assistant"), "")
    return {
        "baton_version": __version__,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"harness": t.harness, "session_id": t.session_id, "path": t.path,
                   "started": t.started},
        "root": root,
        "goal": users[0][:1200] if users else "",
        "directives": [u[:500] for u in users[1:]][-6:],
        "boundaries": boundaries(users),
        "scope": scope,
        "touched": touched,
        "consulted": _dedupe(reads, limit=15),
        "recent_commands": _dedupe(cmds, limit=8),
        "todos": open_todos(t),
        "last_reply": last_reply[-1500:],
        "git": git_anchor(root),
    }


def _bullets(items, empty="（无）"):
    return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"


def to_markdown(b: dict) -> str:
    g = b["git"]
    anchor = f"`{g['head'][:10]}` on `{g['branch']}`" if g.get("head") else "（非 git 仓库）"
    parts = [
        f"# 交接包 / Session Baton\n\n来源：{b['source']['harness']} 会话 `{b['source']['session_id']}`"
        f"，工作目录 `{b['root']}`，git 锚点 {anchor}。",
        "## 你在接力什么\n\n" + (b["goal"] or "（未找到首条用户消息）"),
        "## 用户后续指令（按时间）\n\n" + _bullets(b["directives"]),
        "## 边界（必须遵守，不要扩大解释）\n\n" + _bullets(b["boundaries"], "未抽取到明确边界"),
        "## 允许改动的文件范围\n\n" + _bullets(b["scope"], "未声明，改动前先确认"),
        "## 未完成事项\n\n" + _bullets(b["todos"], "原会话未列出"),
        "## 原会话最后一段回复\n\n" + (b["last_reply"] or "（无）"),
        "## 最近执行的命令\n\n" + _bullets([f"`{c}`" for c in b["recent_commands"]]),
        "## 接手须知\n\n- 先读完边界再动手；超出范围的文件改动会在回审时被标红。\n"
        "- 阶段之间写一句人话说明进度，不要连续刷命令。\n"
        f"- 工作区未提交改动：{len(g.get('dirty', []))} 个文件。",
    ]
    return "\n\n".join(parts) + "\n"


def save(b: dict, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = os.path.join(out_dir, f"baton-{stamp}.json")
    mp = os.path.join(out_dir, f"baton-{stamp}.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(b, fh, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(b))
    return jp, mp


def latest_baton(out_dir: str) -> str | None:
    if not os.path.isdir(out_dir):
        return None
    files = sorted(f for f in os.listdir(out_dir) if f.startswith("baton-") and f.endswith(".json"))
    return os.path.join(out_dir, files[-1]) if files else None
