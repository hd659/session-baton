import json
import os
import subprocess
import time

import pytest

from session_baton import cli
from session_baton.core import boundaries, pack, save, tool_class
from session_baton.review import fold, in_scope, review
from session_baton.sources import discover, load

GOAL = "给网关加自用优先级。我说的最高权限是类似并发这种的，但是托管这种还是和其他账号一样的。不要改 billing.py。"


def jl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return path


def claude_rows(cwd):
    return [
        {"type": "user", "sessionId": "c-1", "cwd": cwd, "timestamp": "2026-11-13T10:00:00Z",
         "message": {"role": "user", "content": GOAL}},
        {"type": "user", "sessionId": "c-1", "message": {"role": "user",
         "content": "<system-reminder>noise</system-reminder>"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "先看网关路由。"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": f"{cwd}/gateway.py"}},
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": f"{cwd}/gateway.py"}},
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "ok"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "额度快用完了。\n- [ ] 给 priority 加单测\n下一步：补上独享号满额拦截"}]}},
    ]


def codex_rows(cwd):
    patch = "*** Begin Patch\n*** Update File: gateway.py\n*** Add File: billing.py\n*** End Patch"
    return [
        {"type": "session_meta", "timestamp": "2026-11-13T12:00:00Z", "payload": {"id": "x-2", "cwd": cwd}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "读 .baton 里的交接包继续"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["bash", "-lc", "cat gateway.py"]})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["bash", "-lc", "rg priority ."]})}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["bash", "-lc", "git push --force origin main"]})}},
    ]


def pi_rows(cwd):
    return [
        {"type": "session", "version": 3, "id": "p-3", "timestamp": "2026-11-13T09:00:00Z", "cwd": cwd},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "a", "name": "write", "arguments": {"path": "notes.md"}}]}},
    ]


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "dev")
    (root / "gateway.py").write_text("x = 1\n")
    (root / "billing.py").write_text("y = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def test_parsers_normalise_three_harnesses(tmp_path):
    c = load(jl(tmp_path / "c.jsonl", claude_rows("/w")))
    x = load(jl(tmp_path / "x.jsonl", codex_rows("/w")))
    p = load(jl(tmp_path / "p.jsonl", pi_rows("/w")))
    assert (c.harness, x.harness, p.harness) == ("claude", "codex", "pi")
    assert [e.kind for e in c.events if e.kind == "user"] == ["user"]  # reminder + tool_result dropped
    assert [e.text for e in x.events if e.kind == "user"] == ["读 .baton 里的交接包继续"]
    assert x.cwd == "/w" and p.session_id == "p-3"
    assert sum(e.kind == "tool" for e in x.events) == 4


def test_discovery_is_bounded_and_filters_cwd(tmp_path):
    home = tmp_path / "home"
    jl(home / ".claude/projects/-w-proj/a.jsonl", claude_rows("/w/proj"))
    jl(home / ".claude/projects/-other/b.jsonl", claude_rows("/other"))
    jl(home / ".codex/sessions/2026/11/13/r.jsonl", codex_rows("/w/proj"))
    jl(home / "Documents/deep/c.jsonl", claude_rows("/w/proj"))  # outside whitelist
    found = discover(home=str(home), cwd="/w/proj")
    assert sorted(h for h, _, _ in found) == ["claude", "codex"]
    assert discover(home=str(home), timeout=0) == []


def test_boundary_extraction_keeps_scoped_permission():
    got = boundaries([GOAL])
    assert any("并发" in b and "托管" in b and "一样" in b for b in got)
    assert any("指的是" in b for b in boundaries(["优先指的是排队，但是计费和别人一致。"]))
    assert any("billing.py" in b for b in got)
    assert boundaries(["你能不能帮我做个小工具，直接从会话记录里打包", "和别人的区别在哪里还没想好"]) == []
    assert boundaries(["别动线上配置文件。"]) == ["别动线上配置文件"]


def test_tool_classes():
    assert tool_class("mcp__x__few_edit") == "write"
    assert tool_class("apply_patch") == "write"
    assert tool_class("Bash") == "run"
    assert tool_class("mcp__x__short_grep") == "search"
    assert tool_class("Read") == "read"
    assert tool_class("TodoWrite") == "other"
    assert tool_class("mcp__x__scout_todo") == "other"
    assert tool_class("write_stdin") == "run"
    assert tool_class("exec_command") == "run"


def test_pack_builds_baton(repo, tmp_path):
    t = load(jl(tmp_path / "c.jsonl", claude_rows(str(repo))))
    b = pack(t, str(repo), ["tests/"])
    assert b["goal"].startswith("给网关")
    assert b["scope"] == ["gateway.py", "tests/"]
    assert "给 priority 加单测" in b["todos"]
    assert b["git"]["head"] and b["recent_commands"] == ["pytest -q"]
    jp, mp = save(b, str(repo / ".baton"))
    assert "边界" in open(mp, encoding="utf-8").read()


def test_fold_groups_phases_and_marks_silence(tmp_path):
    x = load(jl(tmp_path / "x.jsonl", codex_rows("/w")))
    phases = fold(x, "/w")
    assert [p["name"] for p in phases] == ["执行命令", "改代码", "提交推送"]
    assert phases[0]["count"] == 2 and not phases[0]["narrated"]


def test_test_detection_needs_real_invocation():
    from session_baton.review import TEST_RE
    assert TEST_RE.search("cd x && python3 -m pytest -q")
    assert TEST_RE.search("pytest tests/")
    assert not TEST_RE.search('python -c "import rich, pytest"')


def test_scope_match():
    assert in_scope("tests/test_a.py", ["tests/"])
    assert in_scope("src/a.py", ["src/*.py"])
    assert not in_scope("billing.py", ["gateway.py"])


def test_review_flags_out_of_scope_and_boundaries(repo, tmp_path):
    b = pack(load(jl(tmp_path / "c.jsonl", claude_rows(str(repo)))), str(repo))
    (repo / "billing.py").write_text("y = 2\n")
    (repo / "gateway.py").write_text("x = 2\n")
    r = review(b, load(jl(tmp_path / "x.jsonl", codex_rows(str(repo)))))
    assert r["out_of_scope"] == ["billing.py"]
    why = " ".join(h["boundary"] for h in r["boundary_hits"])
    assert "billing.py" in why and "强推" in why
    assert r["manual_checks"] and not r["verified"]
    assert all(not td["mentioned"] for td in r["todos"])


def test_cli_end_to_end(repo, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    slug = "-" + str(repo).strip("/").replace("/", "-").replace("_", "-")
    jl(home / f".claude/projects/{slug}/s.jsonl", claude_rows(str(repo)))
    monkeypatch.setenv("BATON_HOME", str(home))
    assert cli.main(["pack", "--root", str(repo)]) == 0
    time.sleep(1.1)
    rec = jl(home / ".codex/sessions/2026/11/14/r.jsonl", codex_rows(str(repo)))
    os.utime(rec)
    (repo / "billing.py").write_text("y = 3\n")
    code = cli.main(["review", "--root", str(repo)])
    out = capsys.readouterr().out
    assert code == 1 and "范围外改动" in out and "billing.py" in out
    assert cli.main(["review", "--root", str(repo), "--json"]) == 0
