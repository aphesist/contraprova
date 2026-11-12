"""The Claude Code cascade: plugin manifests, agent files, the gate hook and ``init-claude``."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from contraprova.cli import main
from contraprova.harness.swepro import TEST_PATTERNS

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "config" / "claude-code"
GATE = PLUGIN / "scripts" / "gate.py"
AGENTS = ("orchestrator", "worker", "escalation-sonnet", "escalation-opus")


def _gate_module():
    spec = importlib.util.spec_from_file_location("cascade_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n")
    head = text[4: text.index("\n---", 4)]
    out = {}
    for line in head.splitlines():
        if line and not line.startswith(" ") and ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# ------------------------------------------------------------------ manifests and agent files

def test_manifests_parse_and_point_at_the_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] == "contraprova-cascade"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    for event in ("PreToolUse", "PostToolUse"):
        (group,) = hooks[event]
        assert group["matcher"] == "Agent"
        assert group["hooks"][0]["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/gate.py"]


def test_agent_files_follow_the_cascade_design():
    fm = {a: frontmatter(PLUGIN / "agents" / f"{a}.md") for a in AGENTS}
    assert sorted(p.stem for p in (PLUGIN / "agents").glob("*.md")) == sorted(AGENTS)
    for a, f in fm.items():
        assert f["name"] == a and f["description"]
        assert "fable" not in f.get("model", "").lower()
    orch = fm["orchestrator"]
    assert orch["model"] == "inherit" and "effort" not in orch  # session model and effort apply
    tools = orch["tools"]
    for a in AGENTS[1:]:
        assert f"Agent(" in tools and a in tools
    assert not any(t in tools for t in ("Edit", "Write", "Bash"))
    assert fm["worker"]["model"] == "claude-haiku-5-5"
    assert fm["escalation-sonnet"]["model"] == "claude-sonnet-5-5"
    assert fm["escalation-opus"]["model"] == "claude-opus-5-5"
    for a in AGENTS[1:]:
        assert fm[a]["effort"] == "medium"
        assert "Agent" not in fm[a]["tools"]  # workers cannot delegate


def test_gate_test_patterns_match_the_harness():
    assert _gate_module().TEST_PATTERNS == TEST_PATTERNS


# ------------------------------------------------------------------ gate logic

def test_parse_brief_and_scope_matching():
    g = _gate_module()
    scope, test = g.parse_brief("scope: src/a.py, `src/pkg/*.py`\ntest: `pytest -q tests/test_a.py`\ngoal: x")
    assert scope == ["src/a.py", "src/pkg/*.py"] and test == "pytest -q tests/test_a.py"
    assert g.parse_brief("goal: no contract") == (None, None)
    assert g.in_scope("src/a.py", scope) and g.in_scope("src/pkg/b.py", scope)
    assert not g.in_scope("src/other.py", scope)
    assert g.in_scope("docs/x/y.md", ["docs/"]) and g.in_scope("lib/z.py", ["./lib/z.py"])
    assert g.is_protected("tests/test_a.py") and g.is_protected("web/app.test.js")
    assert not g.is_protected("src/a.py")
    assert g.agent_base("contraprova-cascade:worker") == "worker"
    assert g.agent_base("escalation-opus") == "escalation-opus"
    assert g.agent_base("Explore") is None and g.agent_base(None) is None


def test_evaluate_verdicts(tmp_path):
    g = _gate_module()
    brief = "scope: src/a.py\ntest: x"
    ok = f"{sys.executable} -c pass"
    bad = f"{sys.executable} -c \"print('boom'); raise SystemExit(3)\""
    assert g.evaluate(brief, ["src/a.py"], ok, tmp_path)[0] == "PASS"
    verdict, findings = g.evaluate(brief, ["src/a.py"], bad, tmp_path)
    assert verdict == "BLOCK" and "exited 3" in findings[-1] and "boom" in findings[-1]
    verdict, findings = g.evaluate(brief, ["src/a.py", "src/b.py"], None, tmp_path)
    assert verdict == "BLOCK" and any("BLOCK scope" in f and "src/b.py" in f for f in findings)
    assert any("CONTRAPROVA_TEST_CMD is not set" in f for f in findings)
    verdict, findings = g.evaluate(brief, ["tests/test_a.py"], None, tmp_path)
    assert verdict == "BLOCK" and any(f.startswith("BLOCK protected") for f in findings)
    assert g.evaluate("goal: none", [], None, tmp_path)[0] == "BLOCK"  # no scope line


# ------------------------------------------------------------------ gate as a hook process

def _hook(payload: dict, env: dict) -> dict | None:
    r = subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=60, check=True)
    return json.loads(r.stdout) if r.stdout.strip() else None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("edit,test_cmd,verdict", [
    ({"src/a.py": "x = 2\n"}, "pass", "PASS"),
    ({"src/a.py": "x = 2\n"}, "raise SystemExit(1)", "BLOCK"),
    ({"src/a.py": "x = 2\n", "src/b.py": "y = 1\n"}, "pass", "BLOCK"),
    ({"tests/test_a.py": "def test_a(): pass\n"}, "pass", "BLOCK"),
])
def test_gate_hook_round_trip(tmp_path, edit, test_cmd, verdict):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    (repo / "notes.txt").write_text("dirty before the worker\n")  # pre-existing change: not the worker's
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "src", "tests"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "base"], check=True)

    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
           "CONTRAPROVA_TEST_CMD": f"{sys.executable} -c \"{test_cmd}\""}
    tool_input = {"subagent_type": "contraprova-cascade:worker", "description": "d",
                  "prompt": "scope: src/a.py\ntest: pytest -q\ngoal: bump x"}
    base = {"session_id": "s", "cwd": str(repo), "tool_name": "Agent", "tool_input": tool_input, "tool_use_id": "t1"}
    assert _hook({**base, "hook_event_name": "PreToolUse"}, env) is None
    for path, text in edit.items():
        (repo / path).write_text(text)
    out = _hook({**base, "hook_event_name": "PostToolUse", "tool_response": {}}, env)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(f"contraprova gate (worker): {verdict}"), ctx
    assert "notes.txt" not in ctx
    assert not list((tmp_path / "data").glob("snap-*"))  # snapshot cleaned up


def test_gate_hook_denies_model_override_and_ignores_other_agents(tmp_path):
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    base = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Agent", "hook_event_name": "PreToolUse",
            "tool_use_id": "t2"}
    out = _hook({**base, "tool_input": {"subagent_type": "worker", "prompt": "p", "model": "fable"}}, env)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _hook({**base, "tool_input": {"subagent_type": "Explore", "prompt": "p", "model": "opus"}}, env) is None
    assert _hook({**base, "tool_name": "Bash", "tool_input": {"command": "ls"}}, env) is None


# ------------------------------------------------------------------ init-claude

def test_init_claude_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init-claude"]) == 0
    agents = tmp_path / ".claude" / "agents"
    assert sorted(p.stem for p in agents.glob("*.md")) == sorted(AGENTS)
    orch = (agents / "orchestrator.md").read_text()
    assert '${CLAUDE_PROJECT_DIR}/.claude/hooks/contraprova-gate.py' in orch
    assert frontmatter(agents / "orchestrator.md")["name"] == "orchestrator"  # frontmatter still parses
    assert (tmp_path / ".claude" / "hooks" / "contraprova-gate.py").read_text() == GATE.read_text()
    assert (agents / "worker.md").read_text() == (PLUGIN / "agents" / "worker.md").read_text()
    assert "claude --agent orchestrator" in capsys.readouterr().out


def test_init_claude_does_not_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mine = tmp_path / ".claude" / "agents" / "worker.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own worker\n")
    assert main(["init-claude"]) == 0
    assert mine.read_text() == "my own worker\n"
    out = capsys.readouterr().out
    assert f"kept\t{mine}" in out and "--force" in out
    assert (mine.parent / "orchestrator.md").exists()  # the others are still installed
    assert main(["init-claude", "--force"]) == 0
    assert mine.read_text() == (PLUGIN / "agents" / "worker.md").read_text()


def test_init_claude_user_dir_from_env(tmp_path, monkeypatch):
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["init-claude", "--user"]) == 0
    assert not (tmp_path / ".claude").exists()
    gate = home / "hooks" / "contraprova-gate.py"
    assert gate.exists()
    assert str(gate.resolve()) in (home / "agents" / "orchestrator.md").read_text()


def test_init_claude_no_gate_and_bad_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init-claude", "--no-gate"]) == 0
    orch = (tmp_path / ".claude" / "agents" / "orchestrator.md").read_text()
    assert orch == (PLUGIN / "agents" / "orchestrator.md").read_text()
    assert not (tmp_path / ".claude" / "hooks").exists()
    assert main(["init-claude", "--source", str(tmp_path / "nowhere")]) == 1
    assert "plugin install contraprova-cascade" in capsys.readouterr().err
