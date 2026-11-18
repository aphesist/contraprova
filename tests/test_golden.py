"""The golden ledger: the cross-implementation contract for the event log.

``tests/fixtures/golden/ledger-v1.jsonl`` holds a fixed chain covering every event type. These tests
rebuild it event by event and demand identical bytes and hashes. Changing the file is a breaking
change (see ``tests/fixtures/golden/README.md``); the pinned digests below make that deliberate.
"""

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from contraprova.events import EVENT_TYPES, GENESIS_HASH, Event, verify_chain
from contraprova.log import EventLog, read_events

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
GOLDEN = GOLDEN_DIR / "ledger-v1.jsonl"

# The whole file, and its first 21 lines (the original 14 types, written before the harness types
# were appended). Update both only for an intended, announced change to the golden log.
GOLDEN_SHA256 = "7579b1b366e0e6b5bbf1b8b741ec88d96dd4e8f4639714d2b4dd0e0626f65439"
EXISTING_PREFIX_LINES = 21
EXISTING_PREFIX_SHA256 = "31eeb95e805c264648e01348e3ca4aae42cef3cd0095ad39aff4683b6c5928b7"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lines() -> list[str]:
    return GOLDEN.read_bytes().decode("utf-8").split("\n")[:-1]


def test_every_line_is_rebuilt_byte_for_byte():
    prev = GENESIS_HASH
    for i, line in enumerate(lines()):
        rec = json.loads(line)
        ev = Event.create(rec["seq"], rec["type"], rec["actor"], rec["ts"], rec["payload"], rec["prev_hash"])
        assert rec["seq"] == i
        assert rec["prev_hash"] == prev
        assert ev.to_json() == line, f"line {i} is not canonical"
        assert ev.hash == rec["hash"], f"line {i} hash differs"
        prev = ev.hash


def test_hashes_follow_the_documented_rule_without_the_kernel():
    """hash = sha256(canonical_json({seq, type, actor, ts, payload, prev_hash})), written out by hand."""
    prev = "0" * 64
    for line in lines():
        rec = json.loads(line)
        body = {k: rec[k] for k in ("seq", "type", "actor", "ts", "payload", "prev_hash")}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        assert rec["hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert rec["prev_hash"] == prev
        prev = rec["hash"]


def test_file_is_canonical_bytes():
    data = GOLDEN.read_bytes()
    assert data.endswith(b"\n") and b"\r" not in data and not data.endswith(b"\n\n")
    data.decode("utf-8")
    for line in lines():
        assert line == line.strip() and line.startswith('{"actor":"')
    assert sha256(data) == GOLDEN_SHA256


def test_original_events_keep_their_bytes():
    head = b"".join(line.encode("utf-8") + b"\n" for line in lines()[:EXISTING_PREFIX_LINES])
    assert sha256(head) == EXISTING_PREFIX_SHA256


def test_every_event_type_is_covered():
    assert {json.loads(line)["type"] for line in lines()} == set(EVENT_TYPES)


def test_chain_verifies_and_every_transition_replays(tmp_path):
    verify_chain(read_events(GOLDEN))
    copy = tmp_path / "log.jsonl"
    shutil.copyfile(GOLDEN, copy)
    assert EventLog(copy).verify() == len(lines())


def test_timestamps_are_fixed_utc_milliseconds():
    for line in lines():
        ts = json.loads(line)["ts"]
        assert len(ts) == 24 and ts.endswith("Z") and ts[19] == "."


def test_the_generator_reproduces_the_file():
    spec = importlib.util.spec_from_file_location("make_ledger_v1", GOLDEN_DIR / "make_ledger_v1.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.render(gen.build()) == GOLDEN.read_bytes()


def test_a_replayed_append_reproduces_the_golden_hashes(tmp_path):
    """Writing the same events through the single writer gives the same file."""
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    for line in lines():
        rec = json.loads(line)
        log.append(rec["type"], rec["actor"], rec["payload"], rec["ts"])
    assert path.read_bytes() == GOLDEN.read_bytes()


@pytest.mark.parametrize("index", [0, 5, 13])
def test_editing_a_golden_event_is_detected(index):
    evs = read_events(GOLDEN)
    ev = evs[index]
    forged = Event(ev.seq, ev.type, ev.actor, ev.ts, {**ev.payload, "x": 1}, ev.prev_hash, ev.hash)
    with pytest.raises(Exception):
        verify_chain(evs[:index] + [forged] + evs[index + 1 :])
