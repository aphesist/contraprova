"""SEARCH/REPLACE edit format: parser, applier, scope rejection and unified-diff conversion."""

import pytest

from contraprova.harness.edits import (
    EditBlock,
    apply_edits,
    diff_to_blocks,
    format_blocks,
    parse_edits,
    parse_unified_diff,
    scope_checker,
    unified_patch,
)

SRC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def block(path, search, replace):
    return f"<<<<<<< SEARCH {path}\n{search}=======\n{replace}>>>>>>> REPLACE\n"


def test_canonical_block_parses_and_applies():
    text = "Here is the fix.\n\n" + block("m.py", "    return a + b\n", "    return int(a) + int(b)\n") + "\nDone."
    p = parse_edits(text)
    assert p.errors == () and len(p.blocks) == 1
    b = p.blocks[0]
    assert (b.path, b.search, b.replace, b.index) == ("m.py", "    return a + b\n", "    return int(a) + int(b)\n", 1)
    r = apply_edits(p.blocks, {"m.py": SRC})
    assert r.errors == () and r.applied == (1,)
    assert r.files["m.py"] == SRC.replace("return a + b", "return int(a) + int(b)")


def test_multiple_edits_to_one_file_apply_in_order():
    text = block("m.py", "def add(a, b):\n", "def add(a, b, c=0):\n") + \
        block("m.py", "    return a + b\n", "    return a + b + c\n") + \
        block("m.py", "def add(a, b, c=0):\n    return a + b + c\n", "def add(a, b, c=0):\n    return sum((a, b, c))\n")
    r = apply_edits(parse_edits(text).blocks, {"m.py": SRC})
    assert r.errors == () and r.applied == (1, 2, 3)
    assert "return sum((a, b, c))" in r.files["m.py"] and "return a - b" in r.files["m.py"]


def test_aider_style_path_above_block_with_fences():
    text = "m.py\n```python\n<<<<<<< SEARCH\n    return a - b\n=======\n    return b - a\n>>>>>>> REPLACE\n```\n"
    p = parse_edits(text)
    assert p.errors == () and p.blocks[0].path == "m.py"
    assert apply_edits(p.blocks, {"m.py": SRC}).files["m.py"].endswith("return b - a\n")


@pytest.mark.parametrize("text,needle", [
    ("<<<<<<< SEARCH m.py\n    return a + b\n>>>>>>> REPLACE\n", "no '======='"),
    ("<<<<<<< SEARCH m.py\n    return a + b\n=======\n    return 0\n", "truncated"),
    ("<<<<<<< SEARCH m.py\nx\n=======\ny\n<<<<<<< SEARCH m.py\nx\n=======\ny\n>>>>>>> REPLACE\n", "truncated"),
    ("<<<<<<< SEARCH ../etc/passwd\nx\n=======\ny\n>>>>>>> REPLACE\n", "unsafe path"),
    ("<<<<<<< SEARCH /abs/path.py\nx\n=======\ny\n>>>>>>> REPLACE\n", "unsafe path"),
    ("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n", "missing or unsafe path"),
])
def test_malformed_blocks_are_reported_not_applied(text, needle):
    p = parse_edits(text)
    assert any(needle in e for e in p.errors), p.errors
    assert all(b.path == "m.py" for b in p.blocks)  # a later well-formed block still parses


def test_truncated_reply_keeps_complete_blocks_only():
    text = block("m.py", "def add(a, b):\n", "def add(x, y):\n") + "<<<<<<< SEARCH m.py\n    return a - b\n=====\n"
    p = parse_edits(text)
    assert len(p.blocks) == 1 and len(p.errors) == 1


def test_not_found_and_ambiguous_search():
    src = "x = 1\nx = 1\ny = 2\n"
    text = block("m.py", "z = 3\n", "z = 4\n") + block("m.py", "x = 1\n", "x = 9\n") + block("m.py", "y = 2\n", "y = 3\n")
    r = apply_edits(parse_edits(text).blocks, {"m.py": src})
    assert r.applied == (3,)
    assert any("block 1" in e and "not found" in e for e in r.errors)
    assert any("block 2" in e and "matches 2 places" in e for e in r.errors)
    assert r.files["m.py"] == "x = 1\nx = 1\ny = 3\n"


def test_trailing_whitespace_fallback_is_unique_only():
    src = "a = 1   \nb = 2\n"
    r = apply_edits(parse_edits(block("m.py", "a = 1\nb = 2\n", "a = 10\nb = 20\n")).blocks, {"m.py": src})
    assert r.errors == () and r.files["m.py"] == "a = 10\nb = 20\n"


def test_crlf_files_keep_crlf_and_crlf_replies_are_normalised():
    src = "one\r\ntwo\r\nthree\r\n"
    reply = block("w.js", "two\n", "TWO\n").replace("\n", "\r\n")
    r = apply_edits(parse_edits(reply).blocks, {"w.js": src})
    assert r.errors == () and r.files["w.js"] == "one\r\nTWO\r\nthree\r\n"


def test_create_file_and_refuse_overwrite():
    text = block("new.py", "", "X = 1\n") + block("m.py", "", "oops\n")
    r = apply_edits(parse_edits(text).blocks, {"m.py": SRC})
    assert r.files == {"new.py": "X = 1\n"} and r.created == ("new.py",)
    assert any("already exists" in e for e in r.errors)
    r2 = apply_edits(parse_edits(block("hidden.py", "", "x\n")).blocks, {}, exists=lambda p: p == "hidden.py")
    assert r2.files == {} and "already exists" in r2.errors[0]


def test_unknown_file_is_an_error():
    r = apply_edits(parse_edits(block("other.py", "a\n", "b\n")).blocks, {"m.py": SRC})
    assert r.files == {} and "file not found" in r.errors[0]


def test_scope_rejects_out_of_lane_and_test_files_before_applying():
    scope = scope_checker(["m.py", "new.py"], ["tests/*", "*/tests/*", "test_*.py", "*.test.*"])
    text = block("tests/test_m.py", "", "def test_x(): pass\n") + block("other.py", "a\n", "b\n") + \
        block("src/a.test.ts", "", "x\n") + block("m.py", "    return a - b\n", "    return 0\n")
    r = apply_edits(parse_edits(text).blocks, {"m.py": SRC, "other.py": "a\n"}, scope=scope)
    assert list(r.files) == ["m.py"]
    assert sum("rejected" in e for e in r.errors) == 3
    assert any("protected" in e for e in r.errors) and any("outside" in e for e in r.errors)


def test_empty_replace_deletes_code():
    r = apply_edits(parse_edits(block("m.py", "\n\ndef sub(a, b):\n    return a - b\n", "")).blocks, {"m.py": SRC})
    assert r.files["m.py"] == "def add(a, b):\n    return a + b\n"


def test_format_blocks_round_trips():
    bs = [EditBlock("a.py", "x\n", "y\n", 1), EditBlock("b.py", "", "new\n", 2)]
    assert [(b.path, b.search, b.replace) for b in parse_edits(format_blocks(bs)).blocks] == \
        [(b.path, b.search, b.replace) for b in bs]


# ------------------------------------------------------------------ unified diff -> blocks

ORIG = {
    "pkg/m.py": "".join(f"line {i}\n" for i in range(1, 30)) + "dup\nx\ndup\nx\n",
    "pkg/noeol.js": "a\nb\nc",
}
NEW = {
    "pkg/m.py": ORIG["pkg/m.py"].replace("line 3\n", "LINE 3\n").replace("line 25\n", "line 25\nadded\n")
    .replace("dup\nx\ndup\nx\n", "dup\nx\ndup\ny\n"),
    "pkg/noeol.js": "a\nB\nc",
    "pkg/created.py": "CREATED = True\n",
}


def test_unified_patch_and_diff_to_blocks_round_trip():
    patch = unified_patch(ORIG, NEW)
    assert "\\ No newline at end of file" in patch and "new file mode" in patch
    fds = {fd.path: fd for fd in parse_unified_diff(patch)}
    assert fds["pkg/created.py"].old_path is None and len(fds["pkg/m.py"].hunks) >= 2
    blocks = diff_to_blocks(patch, ORIG)
    r = apply_edits(parse_edits(format_blocks(blocks)).blocks, ORIG)
    assert r.errors == ()
    assert r.files == NEW


def test_diff_to_blocks_extends_context_until_unique():
    orig = {"f.py": "a\nx\nb\nc\nx\nd\n"}
    patch = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -5 +5 @@\n-x\n+X\n"
    blocks = diff_to_blocks(patch, orig)
    assert blocks[0].search.count("x") == 1 and blocks[0].search != "x\n"
    assert apply_edits(blocks, orig).files["f.py"] == "a\nx\nb\nc\nX\nd\n"


def test_diff_to_blocks_rejects_deletions_and_renames():
    with pytest.raises(ValueError):
        diff_to_blocks("diff --git a/f.py b/f.py\ndeleted file mode 100644\n--- a/f.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
                       {"f.py": "x\n"})
