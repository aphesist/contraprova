"""Module boundaries: the engine must not depend on the experiment (harness) or the analysis.

Keeping these imports one-way makes a later split into an engine package and a study package
a matter of moving files. Imports are read with ``ast``, so lazy imports inside functions count.
"""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "contraprova"
ENGINE = ("events", "log", "projections", "pricing", "stats", "contracts", "interfaces", "usage", "gates",
          "sandbox", "router", "ope", "llm", "forks", "mcp_edge")
FORBIDDEN = ("contraprova.harness", "contraprova.analysis")


def imported_modules(path: Path, root: Path = PKG) -> set[str]:
    """Absolute names of every module imported anywhere in ``path`` (relative imports resolved).

    ``root`` is the directory of the ``contraprova`` package.
    """
    pkg_parts = ["contraprova", *path.relative_to(root).parent.parts]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{a.name}" for a in node.names)  # "from . import harness"
    return names


def test_every_engine_module_exists():
    assert all((PKG / f"{m}.py").is_file() for m in ENGINE)


@pytest.mark.parametrize("module", ENGINE)
def test_engine_does_not_import_harness_or_analysis(module):
    bad = sorted(n for n in imported_modules(PKG / f"{module}.py")
                 if any(n == f or n.startswith(f + ".") for f in FORBIDDEN))
    assert not bad, f"contraprova.{module} imports {bad}"


def test_the_checker_sees_relative_and_lazy_imports(tmp_path):
    (tmp_path / "sub").mkdir()
    probe = tmp_path / "sub" / "probe.py"
    probe.write_text("import json\ndef f():\n    from ..harness import arms\n    from .. import analysis\n")
    names = imported_modules(probe, tmp_path)
    assert {"contraprova.harness", "contraprova.analysis", "json"} <= names
