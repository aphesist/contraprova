"""Run each smoke+pilot exercise's reference solution through the full suite and the split gates/scorer."""
import json, sys
from contraprova.harness.polyglot import load_exercises, make_partition, count_tests, TestSplit
from contraprova.harness.runners import (exercise_unavailable, reference_files, run_full_suite, discovered_tests,
                                         polyglot_gates, polyglot_scorer)
from contraprova.harness.arms import TaskContext
from contraprova.interfaces import Submission
ex = load_exercises(sys.argv[1]); part = make_partition(ex); by = {e.id: e for e in ex}
rows = []
for split in ("smoke", "pilot"):
    for i in part.splits[split]:
        e = by[i]; row = {"split": split, "id": i, "heuristic": count_tests(e)}
        why = exercise_unavailable(e)
        if why:
            row["unavailable"] = why; rows.append(row); print(json.dumps(row), flush=True); continue
        ref = reference_files(e)
        rc, out = run_full_suite(e, ref)
        row.update(ref_rc=rc, discovered=discovered_tests(e.language, out))
        if rc != 0: row["tail"] = out[-600:]
        ts = TestSplit.from_dict(i, part.test_splits[i])
        ctx = TaskContext("D", f"D:{i}", e, ts, "x", 1, split=split)
        sub = Submission(ctx.task_id, ref, 1)
        g = polyglot_gates(sub, ctx)
        row["gates"] = [(r.gate, r.verdict, r.reason[:120]) for r in g]
        row["score_solved"] = polyglot_scorer(sub, ctx).solved
        stub_rc, _ = run_full_suite(e, e.stubs())
        row["stub_rc"] = stub_rc
        rows.append(row); print(json.dumps(row), flush=True)
json.dump(rows, open(sys.argv[2], "w"), indent=1)
