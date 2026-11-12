"""Offline tests for Contract Lanes (arm La): plan, board, lanes, merges, blame, ladder, shadow merge,
detectors, selection, pre-flight, analysis and CLI. Synthetic multi-file tasks, fake clients, no
Docker, no network."""

import json
from dataclasses import replace

import pytest

from contraprova.analysis import LANES_PRIMARY, analyze, lane_metrics, lanes_report_markdown
from contraprova.events import EVENT_TYPES
from contraprova.harness.arms import BudgetGuard
from contraprova.harness.edits import diff_to_blocks, format_blocks, unified_patch
from contraprova.harness.lanes import (
    DEFAULT_LADDER,
    LANE_MODELS,
    LANE_SYSTEM,
    T0_INTERFACES,
    Interface,
    Lane,
    LaneConfig,
    LanePlan,
    blame,
    by_file_plan,
    gold_lane_check,
    interface_check,
    interface_requests,
    lane_preflight,
    lane_prompt,
    merge_governed,
    merge_shadow,
    parse_plan,
    plan_reply,
    seed_interfaces,
    single_lane_plan,
    task_interfaces,
)
from contraprova.harness.lanes_detect import (
    duplicated_work,
    interface_output_clashes,
    lane_round_clashes,
    overlapping_scopes,
    regressions,
    unresolved_uses,
)
from contraprova.harness.polyglot import Partition
from contraprova.harness.run import CascadeRouter
from contraprova.harness.swepro import (
    LaneSplits,
    ProExercise,
    TaskFiles,
    lane_prompt_estimate,
    lane_tier,
    lanes_selection,
    make_lane_splits,
    split_tests,
    task_from_row,
)
from contraprova.harness.swepro_arms import (
    PRO_LEAD_SYSTEM,
    ProFakeClient,
    ProGates,
    ProHarness,
    ProScorer,
    ProSubmission,
    Verifier,
    gold_files,
    gold_oracle,
    pro_arm_config,
    run_tasks,
)
from contraprova.interfaces import FABLE, HAIKU, OPUS, SONNET, GateResult
from contraprova.log import EventLog
from contraprova.projections import outcomes, project_tasks

# ------------------------------------------------------------------ a synthetic multi-file task

A_SRC = '''"""Parsing."""


def parse(text):
    """Split text into words."""
    return text.split(" ")  # body_marker_alpha
'''
B_SRC = '''"""Rendering."""


def render(items, sep=","):
    """Join items."""
    return sep.join(items)  # body_marker_beta
'''
C_SRC = '''"""The command."""
from lib.a import parse
from lib.b import render


def run(text):
    return render(parse(text))  # body_marker_gamma
'''
NEW = {
    "lib/a.py": A_SRC.replace('text.split(" ")', "text.split()"),
    "lib/b.py": B_SRC.replace("sep.join(items)", "sep.join(sorted(items))"),
    "lib/c.py": C_SRC.replace("return render(parse(text))", "return render(parse(text), sep=';')"),
    "lib/d.py": '"""Summaries."""\n\n\ndef summarize(items):\n    return len(items)\n',
}
ORIG = {"lib/a.py": A_SRC, "lib/b.py": B_SRC, "lib/c.py": C_SRC}
IFACE = ("- Path: `lib/d.py`\n- Name: `summarize`\n- Type: function\n- Input: items: list\n- Output: int\n"
         "- Description: Count items.\n")
F2P = ["tests/test_a.py::test_parse", "tests/test_b.py::test_render", "tests/test_c.py::test_run",
       "tests/test_d.py::test_summarize"]


def make_row(iid, orig=ORIG, new=NEW, interface=IFACE, f2p=F2P):
    return {
        "instance_id": iid, "repo": "ansible/ansible", "repo_language": "py", "base_commit": "b" * 40,
        "problem_statement": "Make parsing, rendering and the command consistent; add summarize.",
        "requirements": "- parse splits on whitespace.", "interface": interface,
        "patch": unified_patch(orig, new), "test_patch": "diff --git a/tests/t.py b/tests/t.py\n",
        "fail_to_pass": json.dumps(f2p), "pass_to_pass": json.dumps(["tests/test_x.py::test_old"]),
        "selected_test_files_to_run": "[]", "docker_image": f"img:{iid}", "hard": False,
    }


IDS = [f"instance_ansible__ansible-{'%040x' % n}-vnan" for n in range(3)]


def make_ex(iid=IDS[0], **kw):
    t = task_from_row(make_row(iid, **kw))
    orig = kw.get("orig", ORIG)
    mod, created = t.gold_paths()
    return ProExercise(t, TaskFiles({p: orig[p] for p in mod}, created, "raw"), split_tests(t))


@pytest.fixture()
def exercises():
    return {i: make_ex(i) for i in IDS}


def harness(tmp_path, exercises, *, policy=None, solve=lambda i: True, budget=None, run_fn=None, client=None,
            lanes_k=3, cfg=None, name="log.jsonl"):
    v = Verifier(run_fn or gold_oracle(exercises))
    client = client or ProFakeClient(exercises, solve, policy=policy, lanes_k=lanes_k)
    h = ProHarness(EventLog(tmp_path / name), client, ProScorer(v), ProGates(v), CascadeRouter(HAIKU, OPUS),
                   BudgetGuard(budget), pro_arm_config(fork_branches=(("control-1", "control", HAIKU),
                                                                      ("treatment", "treatment", OPUS)),
                                                       worker_effort_by_model=((OPUS, "low"),), derive_b=True),
                   "p" * 64)
    if cfg is not None:
        h.lane_config = cfg
    return h, v, client


def events_of(h, type_):
    return [e.payload for e in h.log.events() if e.type == type_]


ESCALATE = lambda t, m, lane: "none" if m == HAIKU and (lane is None or lane[0] == f"L{lane[1]}") else "gold"


# ------------------------------------------------------------------ config: the ladder, never Fable


def test_ladder_is_haiku_repair_sonnet_opus_and_never_fable():
    cfg = LaneConfig()
    assert [m for _, m, _ in cfg.ladder] == [HAIKU, HAIKU, SONNET, OPUS]
    assert [s for s, _, _ in cfg.ladder] == ["haiku", "haiku-repair", "sonnet", "opus"]
    assert [e for _, _, e in cfg.ladder] == ["medium", "medium", "medium", "low"]
    assert cfg.planner_model == OPUS and cfg.planner_effort == "low" and cfg.lane_max_tokens == 32_000
    assert FABLE not in LANE_MODELS and all(m in LANE_MODELS for _, m, _ in DEFAULT_LADDER)


@pytest.mark.parametrize("kw", [{"planner_model": FABLE}, {"ladder": (("haiku", HAIKU, "medium"), ("fable", FABLE, "low"))},
                                {"ladder": ()}, {"max_lanes": 5}])
def test_lane_config_refuses_fable_and_bad_settings(kw):
    with pytest.raises(ValueError):
        LaneConfig(**kw)


def test_no_request_ever_goes_to_fable_even_at_the_top_of_the_ladder(tmp_path, exercises):
    h, _, client = harness(tmp_path, exercises, policy=lambda t, m, lane: "none" if lane and lane[0] == "L1" else "gold")
    assert not h.run_task("La", exercises[IDS[0]], exercises[IDS[0]].test_split)
    models = {c["model"] for c in client.calls}
    assert FABLE not in models and models <= LANE_MODELS
    steps = [p["step"] for p in events_of(h, "route.decided")
             if p.get("decision") == "lane_escalation" and p["task_id"].endswith(":L1")]
    assert steps == ["haiku-repair", "sonnet", "opus"]


# ------------------------------------------------------------------ plan parsing and validation


def _plan_json(lanes, interfaces=()):
    return "```json\n" + json.dumps({"lanes": lanes, "interfaces": list(interfaces), "rationale": "r"}) + "\n```"


def test_valid_plan_parses_and_partitions_every_file():
    ex = make_ex()
    text = _plan_json([{"lane_id": "L1", "files": ["lib/a.py", "lib/d.py"], "tests": [ex.test_split.visible[0], "nope"]},
                       {"lane_id": "L2", "files": ["lib/b.py", "lib/c.py"], "provides": ["render(items, sep)"]}],
                      [{"symbol": "render", "signature": "render(items, sep=',')", "path": "lib/b.py", "owner": "L2"}])
    got = parse_plan(text, ex, ex.test_split)
    assert got.plan is not None and not got.errors
    assert got.plan.k == 2 and got.plan.lane("L1").new_files == ("lib/d.py",)
    assert got.plan.lane("L1").tests == (ex.test_split.visible[0],)
    assert any("dropped" in w for w in got.warnings)
    assert got.plan.frozen() == {"render": "render(items, sep=',')"}


@pytest.mark.parametrize("lanes,needle", [
    ([{"lane_id": "L1", "files": ["lib/a.py", "lib/b.py", "lib/c.py"]}], "lib/d.py is assigned to no lane"),
    ([{"lane_id": "L1", "files": ["lib/a.py", "lib/b.py", "lib/d.py"]}, {"lane_id": "L2", "files": ["lib/b.py", "lib/c.py"]}],
     "assigned to lanes L1, L2"),
    ([{"lane_id": "L1", "files": ["lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py", "tests/test_a.py"]}], "test file"),
    ([{"lane_id": "L1", "files": ["lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py", "lib/zzz.py"]}], "not one of the files"),
    ([{"lane_id": f"L{i}", "files": [f]} for i, f in enumerate(["lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py"])]
     + [{"lane_id": "L9", "files": []}], "at most 4"),
    ([{"lane_id": "bad id!", "files": ["lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py"]}], "lane_id"),
])
def test_invalid_plans_are_rejected(lanes, needle):
    ex = make_ex()
    got = parse_plan(_plan_json(lanes), ex, ex.test_split)
    assert got.plan is None and any(needle in e for e in got.errors), got.errors


def test_unparsable_and_oversized_plans_are_rejected():
    ex = make_ex()
    assert parse_plan("no json here", ex, ex.test_split).plan is None
    assert parse_plan("```json\n{\"lanes\": [}\n```", ex, ex.test_split).errors
    text = _plan_json([{"lane_id": "L1", "files": ["lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py"]}])
    got = parse_plan(text, ex, ex.test_split, lane_tokens=lambda p, ln: 95_000)
    assert got.plan is None and "above 90000" in got.errors[0]


def test_task_interface_field_seeds_the_board():
    ex = make_ex()
    ti = task_interfaces(ex.task.interface, ex.solution_files)
    assert [(i.symbol, i.path, i.kind, i.source) for i in ti] == [("summarize", "lib/d.py", "function", "task_interface")]
    two = task_interfaces("- Path: `x.py`\n- Name: `x`\n- Type: file\n\n- Path: `x.py`\n- Name: `qt.mix`\n"
                          "- Type: function\n- Input: a: int\n- Output: str\n")
    assert [i.symbol for i in two] == ["qt.mix"] and two[0].signature == "qt.mix(a: int) -> str"
    plan = by_file_plan(ex, 2)
    seeded = seed_interfaces((), ti, plan.lanes)
    assert seeded[0].owner == plan.owner_of("lib/d.py")
    assert task_interfaces("No new interfaces are introduced.") == []


# ------------------------------------------------------------------ prompts: outlines only for the planner


def test_planner_prompt_has_no_file_body_and_lists_every_file(tmp_path, exercises):
    h, _, client = harness(tmp_path, exercises)
    h.run_task("La", exercises[IDS[0]], exercises[IDS[0]].test_split)
    planner = [c for c in client.calls if c["role"] == "orchestrator"]
    assert len(planner) == 1 and planner[0]["model"] == OPUS and planner[0]["effort"] == "low"
    assert planner[0]["system"] == PRO_LEAD_SYSTEM and planner[0]["max_tokens"] == 8000
    p = planner[0]["prompt"]
    assert "body_marker" not in p and "## Request: lane plan" in p
    for f in ("lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py"):
        assert f in p
    assert "def parse(text)" in p  # the outline's signature line


def test_lane_prompt_has_own_files_in_full_and_only_outlines_of_others():
    ex = make_ex()
    plan = LanePlan((Lane("L1", ("lib/a.py",), ("lib/d.py",)), Lane("L2", ("lib/b.py", "lib/c.py"))),
                    (Interface("render", "render(items, sep=',')", "lib/b.py", "L2"),))
    p = lane_prompt(ex, ex.test_split, plan, plan.lanes[0])
    assert "body_marker_alpha" in p and "body_marker_beta" not in p and "body_marker_gamma" not in p
    assert "def render(items, sep=',')" in p  # outline of another lane's file
    assert "Lane: L1 of 2 (files: lib/a.py, lib/d.py)" in p and "render(items, sep=',')  [file lib/b.py]  [owner L2]" in p
    assert "INTERFACE REQUEST" in p and "INTERFACE REQUEST" in p
    for t in ex.test_split.gate_private:
        assert t not in p


# ------------------------------------------------------------------ the arm end to end (fakes)


def test_gold_lanes_solve_and_log_valid_events(tmp_path, exercises):
    h, v, client = harness(tmp_path, exercises)
    ex = exercises[IDS[0]]
    assert h.run_task("La", ex, ex.test_split, "lanes-probe")
    h.log.verify()
    tasks = project_tasks(h.log.events())
    assert tasks[f"La:{ex.id}"].status == "done"
    lanes = [t for t in tasks if t.startswith(f"La:{ex.id}:")]
    assert len(lanes) == 3 and all(tasks[t].status == "done" for t in lanes)
    o = outcomes(h.log.events())
    assert o[("La", ex.id)] and o[("La-shadow", ex.id)] and o[("La-first", ex.id)] and o[("La-sonnet", ex.id)]
    planned = events_of(h, "lane.planned")[0]
    assert planned["k"] == 3 and planned["fallback"] is None and planned["plan_sha256"]
    frozen = events_of(h, "interface.frozen")[0]
    assert "summarize" in frozen["interfaces"] and frozen["board"]
    modes = [m["mode"] for m in events_of(h, "merge.decided")]
    assert modes == ["governed", "shadow"]
    usage = events_of(h, "usage.recorded")
    assert all(u["arm"] == "La" for u in usage)
    assert {u["role"] for u in usage} == {"orchestrator", "worker"}
    assert all(u.get("lane_id") for u in usage if u["role"] == "worker")
    subs = [s for s in events_of(h, "task.submitted") if s["task_id"] != f"La:{ex.id}"]
    assert all(s["tier"] == "haiku" and s["round"] == 0 and s["plan_id"] == planned["plan_id"] for s in subs)
    gd = [g for g in events_of(h, "gate.decided") if g["task_id"] == f"La:{ex.id}"][0]
    assert T0_INTERFACES in gd["gate"]
    assert set(EVENT_TYPES) >= {"lane.planned", "interface.frozen", "interface.requested", "merge.decided",
                                "coordination.detected"}


def test_blamed_lane_repairs_then_escalates_alone_and_cost_includes_everything(tmp_path, exercises):
    h, v, client = harness(tmp_path, exercises, policy=ESCALATE)
    ex = exercises[IDS[0]]
    assert h.run_task("La", ex, ex.test_split)
    merges = [m for m in events_of(h, "merge.decided") if m["mode"] == "governed"]
    assert [m["verdict"] for m in merges] == ["block", "block", "pass"]
    assert merges[0]["blamed"] == ["L3"] and merges[1]["blamed"] == ["L3"]
    assert merges[-1]["tiers"] == {"L1": "haiku", "L2": "haiku", "L3": "sonnet"}
    workers = [c for c in client.calls if c["role"] == "worker"]
    lane_of = [c["tags"]["lane_id"] for c in workers]
    assert lane_of == ["L1", "L2", "L3", "L3", "L3"]  # only the blamed lane runs again
    assert [c["model"] for c in workers][-1] == SONNET and workers[-1]["effort"] == "medium"
    routes = [r for r in events_of(h, "route.decided") if r.get("decision") == "lane_escalation"]
    assert [r["action"] for r in routes] == [HAIKU, SONNET] and routes[1]["propensities"] == {SONNET: 1.0}
    o = outcomes(h.log.events())
    assert o[("La", ex.id)] and not o[("La-first", ex.id)] and o[("La-sonnet", ex.id)] and not o[("La-shadow", ex.id)]
    usage = [u for u in events_of(h, "usage.recorded") if u["arm"] == "La"]
    assert len(usage) == len(client.calls)  # planner + 3 lanes + repair round + Sonnet: all La's cost
    a = analyze(h.log.events(), iters=50)
    assert a.arms["La"].requests == len(client.calls) and a.arms["La-shadow"].total_cost == 0
    # retry prompts carry only gate feedback: never a gate-private test name
    for c in workers[3:]:
        assert "rejected when all lanes were merged" in c["prompt"]
        for t in ex.test_split.gate_private:
            assert t not in c["prompt"]
    # gate replays score each blocked merge
    assert sum(1 for e in h.log.events() if e.type == "outcome.scored" and e.payload.get("gate_seq") is not None) == 2


def test_lane_blamed_again_at_opus_ends_unsolved_within_the_attempt_limit(tmp_path, exercises):
    h, v, client = harness(tmp_path, exercises, policy=lambda t, m, lane: "none" if lane and lane[0] == "L2" else "gold")
    ex = exercises[IDS[0]]
    assert not h.run_task("La", ex, ex.test_split)
    merges = [m for m in events_of(h, "merge.decided") if m["mode"] == "governed"]
    assert len(merges) == 4 and merges[-1]["tiers"]["L2"] == "opus"
    tasks = project_tasks(h.log.events())
    assert tasks[f"La:{ex.id}:L2"].attempts == 4 and tasks[f"La:{ex.id}:L2"].status == "failed"
    assert tasks[f"La:{ex.id}"].status == "failed"
    assert not outcomes(h.log.events())[("La", ex.id)]


def test_max_rounds_bounds_the_ladder(tmp_path, exercises):
    h, v, client = harness(tmp_path, exercises, policy=lambda t, m, lane: "none" if lane and lane[0] == "L2" else "gold",
                           cfg=LaneConfig(max_rounds=1))
    ex = exercises[IDS[0]]
    assert not h.run_task("La", ex, ex.test_split)
    assert len([m for m in events_of(h, "merge.decided") if m["mode"] == "governed"]) == 2


def test_out_of_scope_block_rejected_by_governed_merge_applied_by_shadow(tmp_path, exercises):
    pol = lambda t, m, lane: "conflict" if lane and lane[0] == "L1" and m == HAIKU else "gold"
    h, v, client = harness(tmp_path, exercises, policy=pol)
    ex = exercises[IDS[0]]
    assert h.run_task("La", ex, ex.test_split)
    o = outcomes(h.log.events())
    assert o[("La", ex.id)] and not o[("La-shadow", ex.id)]
    clashes = events_of(h, "coordination.detected")
    sv = [(c["mode"], c["evidence"].get("action")) for c in clashes if c["kind"] == "scope_violation"]
    assert ("governed", "rejected") in sv and ("shadow", "applied") in sv
    l1 = [s for s in events_of(h, "task.submitted") if s.get("lane_id") == "L1"][0]
    assert l1["rejected_blocks"] and l1["original_failure"] == "apply"


def test_interface_request_is_logged_rejected_and_counted(tmp_path, exercises):
    class Requesting(ProFakeClient):
        def complete(self, **kw):
            c = super().complete(**kw)
            if "Lane: L2" in kw["messages"][-1].content:
                return replace(c, text=c.text + "\nINTERFACE REQUEST: render: render(items, sep, reverse)\n")
            return c

    h, _, _ = harness(tmp_path, exercises, client=Requesting(exercises))
    ex = exercises[IDS[0]]
    h.run_task("La", ex, ex.test_split)
    req = events_of(h, "interface.requested")
    assert req == [{"task_id": f"La:{ex.id}:L2", "plan_id": req[0]["plan_id"], "lane_id": "L2", "status": "rejected",
                    "reason": "interfaces are frozen for this task", "symbol": "render",
                    "requested_signature": "render(items, sep, reverse)"}]
    assert any(c["kind"] == "interface_change_request" for c in events_of(h, "coordination.detected"))
    assert interface_requests("x\nINTERFACE REQUEST: a.b: b(x)\n") == [{"symbol": "a.b", "requested_signature": "b(x)"}]


class ScriptedPlanner(ProFakeClient):
    def __init__(self, exercises, plans):
        super().__init__(exercises)
        self.plans = list(plans)

    def complete(self, **kw):
        if kw.get("tags", {}).get("role") == "orchestrator":
            c = super().complete(**kw)
            return replace(c, text=self.plans.pop(0))
        return super().complete(**kw)


def test_invalid_plan_gets_one_repair_turn(tmp_path, exercises):
    ex = exercises[IDS[0]]
    good = plan_reply(by_file_plan(ex, 2))
    h, _, client = harness(tmp_path, exercises, client=ScriptedPlanner(exercises, ["not a plan", good]))
    assert h.run_task("La", ex, ex.test_split)
    p = events_of(h, "lane.planned")[0]
    assert p["repair_used"] and p["k"] == 2 and p["fallback"] is None and p["parse_errors"]
    rep = [u for u in events_of(h, "usage.recorded") if u["role"] == "orchestrator"]
    assert len(rep) == 2 and rep[1].get("repair") is True


def test_still_invalid_plan_falls_back_to_one_lane(tmp_path, exercises):
    ex = exercises[IDS[0]]
    h, _, client = harness(tmp_path, exercises, client=ScriptedPlanner(exercises, ["nope", "{\"lanes\": []}"]))
    assert h.run_task("La", ex, ex.test_split)
    p = events_of(h, "lane.planned")[0]
    assert p["k"] == 1 and p["fallback"] == "single_lane"
    assert lane_metrics(h.log.events(), iters=20).fallbacks == 1


def test_budget_stop_is_clean_for_la(tmp_path, exercises):
    h, _, _ = harness(tmp_path, exercises, budget=0.0001)
    s = run_tasks(h, IDS, ["La"], lambda i: exercises[i])
    assert s.stopped and s.attempted["La"] == 1
    h.log.verify()


# ------------------------------------------------------------------ interfaces, merges, blame, detectors


def test_interface_check_passes_on_gold_and_blocks_missing_or_changed_symbols():
    ex = make_ex()
    plan = LanePlan((Lane("L1", ("lib/a.py",), ("lib/d.py",)), Lane("L2", ("lib/b.py", "lib/c.py"), exports=("render",))),
                    (Interface("render", "render(items, sep=',')", "lib/b.py", "L2"),
                     Interface("summarize", "summarize(items)", "lib/d.py", None, "task_interface", "function")))
    gold = gold_files(ex)[0]
    assert interface_check(plan, ex.context.files, gold).verdict == "pass"
    no_d = {p: c for p, c in gold.items() if p != "lib/d.py"}
    r = interface_check(plan, ex.context.files, no_d)
    assert r.verdict == "block" and r.evidence["missing"] == ["summarize"] and r.evidence["owners"] == ["L1"]
    renamed = {**gold, "lib/b.py": gold["lib/b.py"].replace("def render(items, sep=\",\")", "def render(things)")}
    r = interface_check(plan, ex.context.files, renamed)
    assert r.verdict == "block" and r.evidence["mismatched"] == ["render"] and r.evidence["owners"] == ["L2"]
    assert blame(plan, [GateResult("T0.edits", "pass"), r]) == (("L2",), 1, "interfaces")


def test_governed_merge_unions_and_reports_overlap():
    plan = LanePlan((Lane("L1", ("a.py",)), Lane("L2", ("b.py",))))
    subs = {"L1": ProSubmission("x", {"a.py": "1", "b.py": "x"}, edit_errors=("block 2 (b.py): rejected, outside",
                                                                              "block 3 (a.py): SEARCH text not found")),
            "L2": ProSubmission("y", {"b.py": "2"})}
    m = merge_governed(plan, subs)
    assert m.files == {"a.py": "1", "b.py": "x"} and m.conflicts == (("b.py", ("L1", "L2")),)
    assert any("SEARCH text not found" in e for e in m.errors) and not any("rejected" in e for e in m.errors)


def test_shadow_merge_later_create_wins_out_of_scope_applies_and_reverts_are_seen():
    base = {"a.py": "x = 1\ny = 2\n"}
    plan = LanePlan((Lane("L1", ("a.py",)), Lane("L2", (), ("n.py",))))
    r1 = format_blocks(diff_to_blocks(unified_patch(base, {"a.py": "x = 1\nadded_by_one = 3\ny = 2\n"}), base))
    r1 += "\n<<<<<<< SEARCH n.py\n=======\nfirst = 1\n>>>>>>> REPLACE\n"
    r2 = ("<<<<<<< SEARCH n.py\n=======\nsecond = 2\n>>>>>>> REPLACE\n\n"
          "<<<<<<< SEARCH a.py\nx = 1\nadded_by_one = 3\n=======\nx = 1\n>>>>>>> REPLACE\n")
    m = merge_shadow(plan, {"L1": r1, "L2": r2}, base)
    assert m.files["n.py"] == "second = 2\n" and m.files.get("a.py", base["a.py"]) == "x = 1\ny = 2\n"
    assert ("n.py", ("L1", "L2")) in m.conflicts
    assert m.out_of_scope == ({"lane": "L2", "path": "a.py", "block": 2},)  # L1's n.py lost to L2's later create
    assert m.reverted and m.reverted[0]["by"] == "L2" and m.reverted[0]["of"] == ["L1"]


def test_blame_rules():
    plan = LanePlan((Lane("L1", ("lib/a.py",), tests=("tests/test_a.py::t1",)), Lane("L2", ("lib/b.py",)),
                     Lane("L3", ("lib/c.py",))))
    vis = lambda failed: [GateResult("T0.edits", "pass"), GateResult("T0.visible_tests", "block", "x",
                                                                      {"tests": {"failed_ids": failed}})]
    assert blame(plan, vis(["tests/test_a.py::t1"]), failed_tests=["tests/test_a.py::t1"]) == (("L1",), 1, "tests:assigned")
    assert blame(plan, vis(["tests/test_b.py::t2"]), failed_tests=["tests/test_b.py::t2"])[:2] == (("L2",), 2)
    assert blame(plan, vis(["t"]), failed_tests=["tests/zz.py::t"], output='File "/app/lib/c.py", line 3')[:2] == (("L3",), 2)
    assert blame(plan, vis(["t"]), failed_tests=["tests/zz.py::t"]) == (("L1", "L2", "L3"), 3, "tests:unattributed")
    edits = [GateResult("T0.edits", "block", "", {"errors": ["lane L2: block 1 (lib/b.py): SEARCH text not found"]})]
    assert blame(plan, edits) == (("L2",), 1, "edits")
    assert blame(plan, [GateResult("T0.edits", "block", "", {"errors": []})])[1] == 3
    assert blame(plan, [GateResult("T2.review", "block")])[1] == 3


def test_detectors():
    ex = make_ex()
    assert overlapping_scopes([("L1", ("a", "b")), ("L2", ("b",))])[0].lanes == ("L1", "L2")
    lanes = (Lane("L1", ("a.py",), imports=("helper(x)",)), Lane("L2", ("b.py",), exports=("other()",)))
    assert unresolved_uses(lanes, {}, set())[0].evidence["unresolved"] == ["helper"]
    assert not unresolved_uses(lanes, {"helper": "helper(x)"}, set())
    dup = duplicated_work({"L1": {"x.py": "def shared():\n    pass\n__metaclass__ = type\n"},
                           "L2": {"y.py": "def shared():\n    pass\n__metaclass__ = type\n"}}, {})
    assert len(dup) == 1 and dup[0].evidence["symbols"] == ["shared"]
    out = interface_output_clashes("E   AttributeError: module has no attribute 'render'\n", {"render": "render()"},
                                   {"render": "L2"}, ["L1", "L2"])
    assert out[0].lanes == ("L2",) and out[0].kind == "interface_mismatch"
    assert regressions({"t1", "t2"}, {"t2"}, ("L3",))[0].evidence["tests"] == ["t2"]
    sub = ProSubmission("x", {}, blocks=1, info={"rejected_blocks": [{"block": 1, "path": "b.py", "reason": "r"}]})
    kinds = [c.kind for c in lane_round_clashes("L1", sub, [{"symbol": "s", "requested_signature": "s()"}])]
    assert kinds == ["scope_violation", "interface_change_request", "wasted_lane"]
    del ex


def test_gold_lane_check_passes_and_catches_the_conflicting_lane():
    ex = make_ex()
    oracle = gold_oracle({ex.id: ex})
    row = gold_lane_check(ex, by_file_plan(ex, 3), lambda files: oracle(ex.task.image, None, files).reward)
    assert row["ok"] and row["governed_reward"] == 1 and row["shadow_reward"] == 1
    assert row["conflict"]["rejected_by_governed"] == 1 and row["conflict"]["shadow_reward"] == 0
    assert row["interfaces_verdict"] == "pass" and "summarize" in row["interfaces_checked"]


def test_single_lane_and_by_file_plans():
    ex = make_ex()
    one = single_lane_plan(ex, ex.test_split)
    assert one.k == 1 and one.lanes[0].files == ex.solution_files and one.fallback == "single_lane"
    three = by_file_plan(ex, 3)
    assert [ln.files for ln in three.lanes] == [("lib/a.py", "lib/b.py"), ("lib/c.py", "lib/d.py")] or three.k == 3
    assert sorted(f for ln in three.lanes for f in ln.files) == list(ex.solution_files)
    assert parse_plan(plan_reply(three), ex, ex.test_split).plan.k == three.k


# ------------------------------------------------------------------ selection and pre-flight


def _sel_rows():
    big = {f"lib/m{i}.py": "x = 1\n" * 10 for i in range(3)}
    big_new = {p: c + "y = 2\n" * 60 for p, c in big.items()}  # 3 files, 180 changed lines
    mid_new = {p: c + "y = 2\n" * 40 for p, c in big.items()}  # 120 lines
    rows = {}
    for n in range(30):
        iid = f"instance_ansible__ansible-{'%040x' % (1000 + n)}-vnan"
        new = big_new if n % 3 else mid_new
        if n % 10 == 9:
            new = {"lib/m0.py": big_new["lib/m0.py"]}  # one file: not large
        rows[iid] = make_row(iid, orig=big, new={**big, **new}, interface="", f2p=["tests/test_m.py::t"])
    return rows


def test_lanes_selection_tiers_order_and_probe_sources():
    rows = _sel_rows()
    tasks = [task_from_row(r) for r in rows.values()]
    ids = sorted(rows)
    part = Partition(1, {}, {"probe": tuple(ids[:4]), "probe2": tuple(ids[4:8]), "main": tuple(reversed(ids[8:])),
                             "smoke": ()}, "d" * 64, {})
    main90 = ids[8:14]
    scored = set(ids[8:12])
    sel = lanes_selection(tasks, part, main90=main90, main90_scored=scored, source_bytes=lambda t: 3000,
                          n_tier2=2, probe_per_source=2)
    fresh = [i for i in reversed(ids[8:]) if i not in main90]
    t1 = [i for i in fresh if lane_tier(task_from_row(rows[i]), 1) == 1]
    t2 = [i for i in fresh if lane_tier(task_from_row(rows[i]), 1) == 2]
    assert sel["tiers"][1] == t1 and sel["tiers"][2] == t2
    assert sel["lanes-main"] == t1 + t2[:2]
    assert set(sel["probe_sources"]["main90"]) <= scored and set(sel["probe_sources"]["probe12"]) <= set(ids[:8])
    assert len(sel["lanes-probe"]) == 4
    huge = lanes_selection(tasks, part, main90=main90, main90_scored=scored, source_bytes=lambda t: 300_000,
                           n_tier2=2, probe_per_source=0)
    assert huge["tiers"][1] == [] and huge["tiers"][3]
    t = task_from_row(rows[ids[0]])
    assert lane_prompt_estimate(t, 3000) == pytest.approx(((3000 + len(t.problem_statement) + len(t.requirements))
                                                           / 3 + 1212) * 1.075, abs=1)


def test_lane_splits_persist_with_sha256(tmp_path):
    rows = _sel_rows()
    tasks = [task_from_row(r) for r in rows.values()]
    ids = sorted(rows)
    part = Partition(1, {}, {"probe": tuple(ids[:4]), "probe2": tuple(ids[4:8]), "main": tuple(ids[8:]), "smoke": ()},
                     "d" * 64, {})
    ls = make_lane_splits(tasks, part, main90=ids[8:14], main90_scored=ids[8:12], source_bytes=lambda t: 3000,
                          n_tier2=2, probe_per_source=2)
    p = tmp_path / "s.json"
    ls.save(p)
    back = LaneSplits.load(p)
    assert back.sha256 == ls.sha256 and back.splits == ls.splits and back.base_partition_sha256 == part.sha256
    d = json.loads(p.read_text())
    assert d["split_sha256"]["lanes-main"] == ls.split_sha256("lanes-main")
    d["splits"]["lanes-main"] = d["splits"]["lanes-main"][::-1]
    p.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        LaneSplits.load(p)


def test_lane_preflight_counts_planner_and_lane_prompts(tmp_path, exercises):
    h, _, client = harness(tmp_path, exercises)
    seen = []

    def count(model, system, messages):
        seen.append((model, system))
        return 1000

    row = lane_preflight(h, exercises[IDS[0]], exercises[IDS[0]].test_split, count, k=3)
    assert row.planner_input == 1000 and row.lane_inputs == (1000,) * row.k
    assert seen[0][0] == OPUS and all(m == HAIKU and s == LANE_SYSTEM for m, s in seen[1:])
    assert 0 < row.expected < row.worst and set(row.by_part) >= {"planner", "lanes", "sonnet", "opus"}
    assert not client.calls  # counting is free: no completion request


# ------------------------------------------------------------------ analysis


def test_lanes_report_primary_family_of_one_and_lane_section(tmp_path, exercises):
    h, _, _ = harness(tmp_path, exercises, policy=ESCALATE)
    for i in IDS:
        h.run_task("D", exercises[i], exercises[i].test_split)
        h.run_task("La", exercises[i], exercises[i].test_split)
    a = analyze(h.log.events(), primary=LANES_PRIMARY, iters=50)
    prim = [c for c in a.contrasts if c.primary]
    assert [(c.a, c.b) for c in prim] == [("La", "La-shadow")] and prim[0].p_holm == prim[0].p_exact
    m = lane_metrics(h.log.events(), iters=50)
    assert m.tasks == 3 and m.k == {3: 3} and m.rounds == {3: 3} and m.final_tiers["sonnet"] == 3
    assert 0 < m.planner_share < 1 and m.cost_per_solved_ratio.n == 3
    assert m.first_reply_failures["La lanes"]["requests"] == 9 and m.first_reply_failures["D"]["requests"] == 3
    text = lanes_report_markdown(h.log.events(), iters=50)
    assert "Holm across m=1" in text and "## Contract Lanes" in text and "La vs D" in text
    assert "wasted_lane" in text and "Cost per solved task, La / D" in text


# ------------------------------------------------------------------ CLI


@pytest.fixture()
def data_dir(tmp_path):
    from contraprova.harness.swepro import PROBE2_IDS, PROBE_IDS

    d = tmp_path / "data"
    d.mkdir()
    ids = list(PROBE_IDS) + list(PROBE2_IDS) + [f"instance_ansible__ansible-{'%040x' % (500 + n)}-vnan" for n in range(8)]
    big = {**NEW, "lib/a.py": NEW["lib/a.py"] + "".join(f"EXTRA_{n} = {n}\n" for n in range(160))}
    rows = [make_row(i, new=big) for i in ids]
    (d / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for r in rows:
        root = d / "context" / r["instance_id"]
        for p, text in ORIG.items():
            (root / "files" / p).parent.mkdir(parents=True, exist_ok=True)
            (root / "files" / p).write_text(text)
        (root / "meta.json").write_text(json.dumps({"source": "raw"}))
    main90 = tmp_path / "m90.txt"
    main90.write_text("".join(i + "\n" for i in ids[24:30]))
    log = tmp_path / "main.jsonl"
    log.write_text("".join(json.dumps({"type": "outcome.scored", "payload": {"arm": a, "task_id": i, "solved": True}})
                           + "\n" for i in ids[24:30] for a in ("A", "D")))
    return d, main90, log


def test_cli_select_dry_run_preflight_and_report(tmp_path, data_dir, capsys):
    from contraprova.cli import main as cp_main
    from contraprova.harness.swepro_run import main

    d, m90, mlog = data_dir
    assert main(["--action", "select-lanes", "--data", str(d), "--main90", str(m90), "--main-log", str(mlog)]) == 0
    sel = json.loads(capsys.readouterr().out)
    assert sel["sizes"]["lanes-probe"] == 12 and sel["split_sha256"]["lanes-probe"]
    log = tmp_path / "dry.jsonl"
    args = ["--lanes", "--split", "lanes-probe", "--data", str(d), "--log", str(log), "--dry-run"]
    assert main([*args, "--budget-usd", "25", "--dry-solve", "escalate", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["arms"] == ["La", "D"] and out["solved"] == {"La": 2, "D": 2} and out["preflight"]["n"] == 2
    assert main(["--action", "preflight", *args, "--budget-usd", "25", "--limit", "1"]) == 0
    pre = json.loads(capsys.readouterr().out)["preflight"]
    assert pre["expected_by_arm"]["La"] > 0 and pre["la_by_part"]["planner"] > 0
    assert main([*args, "--budget-usd", "0.0001", "--limit", "1", "--log", str(tmp_path / "x.jsonl")]) == 3
    capsys.readouterr()
    assert cp_main(["report", "--lanes", str(log)]) == 0
    assert "La vs La-shadow" in capsys.readouterr().out
