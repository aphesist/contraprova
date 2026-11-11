import math

import pytest

from contraprova.contracts import LaneContract, check_scope, overlapping_scopes
from contraprova.projections import pareto_frontier
from contraprova.stats import discordant, holm, mcnemar_exact, paired_bootstrap_ci, wilson_interval


def test_wilson_known_value():
    lo, hi = wilson_interval(45, 90)
    assert math.isclose(lo, 0.3990, abs_tol=5e-4)
    assert math.isclose(hi, 0.6010, abs_tol=5e-4)
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0


def test_wilson_rejects_bad_input():
    with pytest.raises(ValueError):
        wilson_interval(5, 0)
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_mcnemar_exact():
    assert mcnemar_exact(0, 0) == 1.0
    assert math.isclose(mcnemar_exact(0, 6), 2 / 64)
    assert math.isclose(mcnemar_exact(3, 3), 1.0)
    assert mcnemar_exact(2, 12) < 0.05


def test_discordant():
    a = [True, True, False, False]
    b = [True, False, True, True]
    assert discordant(a, b) == (1, 2)


def test_bootstrap_is_seeded_and_brackets_the_mean():
    diffs = [1, 0, 0, 1, -1, 1, 0, 1, 0, 0] * 9
    m1 = paired_bootstrap_ci(diffs, iters=2000, seed=7)
    m2 = paired_bootstrap_ci(diffs, iters=2000, seed=7)
    assert m1 == m2
    mean, lo, hi = m1
    assert lo <= mean <= hi


def test_holm():
    adj = holm([0.01, 0.04, 0.03, 0.20])
    assert adj == pytest.approx([0.04, 0.09, 0.09, 0.20])


def test_scope_check():
    c = LaneContract("lane-1", "t1", allowed=("src/parser/*",))
    v = check_scope(c, ["src/parser/lexer.py", "./src/parser/ast.py", "src/main.py", "tests/test_parser.py", "../etc/x"])
    assert [(x.path, x.reason) for x in v] == [
        ("src/main.py", "out_of_scope"),
        ("tests/test_parser.py", "protected"),
        ("../etc/x", "out_of_scope"),
    ]


def test_dotfiles_are_not_mangled():
    c = LaneContract("lane-1", "t1", allowed=(".github/workflows/*",))
    assert check_scope(c, [".github/workflows/ci.yml"]) == []


def test_overlapping_scopes():
    a = LaneContract("a", "t1", allowed=("src/x.py",))
    b = LaneContract("b", "t1", allowed=("src/y.py", "src/x.py"))
    c = LaneContract("c", "t1", allowed=("src/z.py",))
    assert overlapping_scopes([a, b, c]) == [("a", "b")]


def test_pareto():
    pts = [("A", 1.0, 0.5), ("B", 2.0, 0.5), ("C", 3.0, 0.9), ("D", 0.5, 0.2), ("E", 3.0, 0.8)]
    assert [p[0] for p in pareto_frontier(pts)] == ["D", "A", "C"]
