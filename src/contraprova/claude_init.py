"""``contraprova init-claude``: install the cascade agents without the plugin system.

Copies ``config/claude-code/agents/*.md`` into ``./.claude/agents`` (or the user's Claude Code
directory with ``--user``: ``$CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``) and the gate script
into ``<dir>/hooks/contraprova-gate.py``. Plugins ship hooks in ``hooks/hooks.json``; a plain agent
directory cannot, so the orchestrator's copy gets the same two hooks in its frontmatter (Claude Code
runs frontmatter hooks when the agent is the main session via ``--agent``). Existing files are left
alone unless ``force``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

GATE_NAME = "contraprova-gate.py"
SOURCE_ENV = "CONTRAPROVA_CLAUDE_SOURCE"


def default_source() -> Path | None:
    """The plugin directory: ``$CONTRAPROVA_CLAUDE_SOURCE``, else ``config/claude-code`` in this checkout."""
    env = os.environ.get(SOURCE_ENV)
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "config" / "claude-code"
    return here if (here / "agents").is_dir() else None


def user_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def gate_frontmatter(gate_arg: str) -> str:
    """YAML for the orchestrator's frontmatter ``hooks`` key: the gate on Agent, before and after."""
    lines = ["hooks:"]
    for event, timeout in (("PreToolUse", 60), ("PostToolUse", 600)):
        lines += [f"  {event}:",
                  '    - matcher: "Agent"',
                  "      hooks:",
                  "        - type: command",
                  '          command: "python3"',
                  f'          args: ["{gate_arg}"]',
                  f"          timeout: {timeout}"]
    return "\n".join(lines) + "\n"


def with_hooks(text: str, gate_arg: str) -> str:
    """Insert the gate hooks before the closing ``---`` of an agent file's frontmatter."""
    if not text.startswith("---\n"):
        raise ValueError("agent file has no frontmatter")
    end = text.index("\n---", 4)
    return text[: end + 1] + gate_frontmatter(gate_arg) + text[end + 1:]


def init_claude(target: Path, source: Path, *, project: bool, force: bool = False,
                gate: bool = True) -> list[tuple[Path, str]]:
    """Install into ``target`` (a ``.claude`` directory). Returns (path, 'written' | 'kept') pairs."""
    agents = sorted((source / "agents").glob("*.md"))
    if not agents:
        raise FileNotFoundError(f"no agent files in {source / 'agents'}")
    gate_src = source / "scripts" / "gate.py"
    gate_dst = target / "hooks" / GATE_NAME
    gate_arg = f"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/{GATE_NAME}" if project else str(gate_dst.resolve())

    plan: list[tuple[Path, str]] = []
    for a in agents:
        text = a.read_text(encoding="utf-8")
        if gate and a.stem == "orchestrator":
            text = with_hooks(text, gate_arg)
        plan.append((target / "agents" / a.name, text))
    if gate:
        plan.append((gate_dst, gate_src.read_text(encoding="utf-8")))

    done = []
    for path, text in plan:
        if path.exists() and not force:
            done.append((path, "kept"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if path == gate_dst:
            shutil.copymode(gate_src, path)
        done.append((path, "written"))
    return done
