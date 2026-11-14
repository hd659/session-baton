"""CLI: baton list / pack / show / review."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from . import __version__
from .core import latest_baton, pack, save, to_markdown
from .review import review, find_receiver
from .sources import discover, load

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.tree import Tree
    console = Console()
except ImportError:  # degrade to plain text
    console = None

DEFAULT_DIR = ".baton"


def _out(text: str):
    if console:
        console.print(text, highlight=False)
    else:
        print(text)


def cmd_list(a):
    rows = discover(harness=a.harness, cwd=None if a.all else os.getcwd(), timeout=a.timeout)
    if not rows:
        _out("当前目录没有找到会话记录（加 --all 看全部）。")
    for h, p, m in rows[: a.limit]:
        _out(f"{datetime.fromtimestamp(m):%m-%d %H:%M}  {h:<6}  {p}")
    return 0


def _pick_source(a):
    if a.session:
        return load(a.session)
    rows = discover(harness=a.harness, cwd=a.root, timeout=a.timeout)
    if not rows:
        sys.exit("找不到这个目录对应的会话记录，用 --session 指定文件。")
    return load(rows[0][1], rows[0][0])


def cmd_pack(a):
    t = _pick_source(a)
    b = pack(t, a.root, a.scope)
    out_dir = os.path.join(b["root"], a.out)
    jp, mp = save(b, out_dir)
    if a.stdout:
        print(to_markdown(b))
    else:
        _out(f"已打包：{t.harness} 会话 {t.session_id[:12]}")
        _out(f"  边界 {len(b['boundaries'])} 条，文件范围 {len(b['scope'])} 个，待办 {len(b['todos'])} 项")
        _out(f"  {mp}\n  {jp}")
        shown = os.path.relpath(mp, b["root"])
        _out(f"交给接手端：让它先读 {mp if shown.startswith('..') else shown}")
    return 0


def _load_baton(a) -> dict:
    path = a.baton or latest_baton(os.path.join(os.path.abspath(a.root), a.out))
    if not path:
        sys.exit("没找到交接包，先运行 baton pack。")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_show(a):
    md = to_markdown(_load_baton(a))
    if console and not a.plain:
        console.print(Markdown(md))
    else:
        print(md)
    return 0


def render(r: dict, expand: bool):
    rec = r["receiver"]
    head = f"接手会话：{rec['harness']} {rec['session_id'][:12]}" if rec else "接手会话：未找到（只看 git）"
    lines = [head, f"新提交 {len(r['new_commits'])} 个，工具调用 {r['tool_calls']} 次，"
                   f"最长连续无说明 {r['silent_streak']} 次，跑过测试：{'是' if r['verified'] else '否'}"]
    _out("\n".join(lines))
    tree = Tree("时间线") if console else None
    for i, p in enumerate(r["phases"], 1):
        label = f"{i}. {p['name']}（{p['count']} 步）" + (f"  {p['note']}" if p["note"] else "  [无说明]")
        steps = p["steps"] if expand or p["count"] <= 3 else p["steps"][:2] + [f"... 另 {p['count'] - 2} 步已折叠"]
        if tree is not None:
            node = tree.add(label if p["narrated"] else f"[yellow]{label}[/]")
            for s in steps:
                node.add(s.replace("[", "\\["))
        else:
            print(label)
            for s in steps:
                print("    " + s)
    if tree is not None and r["phases"]:
        console.print(tree)
    problems = []
    if r["out_of_scope"]:
        problems.append("范围外改动：" + "，".join(r["out_of_scope"]))
    elif not r["scope_declared"]:
        problems.append("交接包未声明文件范围，无法判断是否越界")
    for h in r["boundary_hits"]:
        problems.append(f"可能触碰边界「{h['boundary']}」：{'，'.join(h['matched'])}")
    for td in r["todos"]:
        if not td["mentioned"]:
            problems.append(f"待办未见处理：{td['todo']}")
    if r["silent_streak"] > 8:
        problems.append(f"有一段连续 {r['silent_streak']} 次工具调用没说话")
    if not r["verified"] and r["changed"]:
        problems.append("改了文件但没跑测试")
    body = "\n".join(f"- {p}" for p in problems) or "- 没发现问题"
    if r["manual_checks"]:
        body += "\n需要你人工确认的作用域边界：\n" + "\n".join(f"- {m}" for m in r["manual_checks"])
    if console:
        console.print(Panel(body, title="需要你看的", border_style="red" if problems else "green"))
    else:
        print("需要你看的\n" + body)
    return 1 if r["out_of_scope"] or r["boundary_hits"] else 0


def cmd_review(a):
    b = _load_baton(a)
    rec = load(a.receiver) if a.receiver else find_receiver(b, a.timeout)
    r = review(b, rec)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    return render(r, a.expand)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="baton", description="跨端会话接力：打交接包、回审接手方的工作")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=os.getcwd(), help="项目目录（默认当前目录）")
    common.add_argument("--out", default=DEFAULT_DIR, help="交接包目录（相对 root）")
    common.add_argument("--timeout", type=float, default=5.0, help="会话发现超时秒数")
    p = sub.add_parser("list", help="列出会话记录")
    p.add_argument("--harness", choices=["claude", "codex", "pi"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--timeout", type=float, default=5.0)
    p = sub.add_parser("pack", parents=[common], help="从最近会话 + git 打交接包")
    p.add_argument("--session", help="指定会话 jsonl 文件")
    p.add_argument("--harness", choices=["claude", "codex", "pi"])
    p.add_argument("--scope", action="append", default=[], help="额外允许的文件/目录/通配，可多次")
    p.add_argument("--stdout", action="store_true", help="直接打印 markdown 便于粘贴")
    p = sub.add_parser("show", parents=[common], help="查看最新交接包")
    p.add_argument("--baton")
    p.add_argument("--plain", action="store_true")
    p = sub.add_parser("review", parents=[common], help="回审接手方的工作")
    p.add_argument("--baton")
    p.add_argument("--receiver", help="接手方会话 jsonl（默认自动找交接后的新会话）")
    p.add_argument("--expand", action="store_true", help="展开全部命令")
    p.add_argument("--json", action="store_true")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return {"list": cmd_list, "pack": cmd_pack, "show": cmd_show, "review": cmd_review}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
