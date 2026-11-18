# Golden ledger

`ledger-v1.jsonl` is the cross-implementation contract for the event log: a small, fixed hash chain
with fixed timestamps that covers **every event type**. Any implementation of the ledger (the Python
kernel here, the hypotaxis plugin, a future port) copies this file and must reproduce every line and
every hash from the events alone.

What `tests/test_golden.py` asserts:

- every line is rebuilt event by event with `Event.create` and is byte-identical, hash included;
- the file is canonical: UTF-8, LF line ends, one JSON object per line, sorted keys, no spaces;
- every type in `EVENT_TYPES` appears at least once;
- the log passes `EventLog.verify()` (the chain and every task transition replay);
- the generator below reproduces the file, and the whole file and its first 21 lines (the original
  14 types, before the harness types were appended) match pinned sha256 values.

The order of events is chosen for coverage and for append-only growth, not to look like a real
session. Payloads avoid floats that print with an exponent; use integers or short decimals
(`0.5`, `1.0`), which every JSON writer prints the same way.

## Regenerate

```sh
python tests/fixtures/golden/make_ledger_v1.py
```

The events live in `make_ledger_v1.py` (`EXISTING`, then `NEW`). Then update the two pinned digests
in `tests/test_golden.py` and copy the file to every implementation that holds one (hypotaxis).

## Changes to this file are breaking

A changed line changes its hash and every hash after it, and means a log written before the change no
longer verifies after it. So:

- **never edit or reorder an existing event.** Adding a new event type means appending events to the
  end (`NEW`); the first 21 lines must stay byte-identical (`EXISTING_PREFIX_SHA256`);
- a change to canonical JSON, to the hash input or to the envelope is a new format version
  (`ledger-v2.jsonl`), not an edit of this file;
- the commit that changes it says so, and the same change lands in every copy on the same day.
