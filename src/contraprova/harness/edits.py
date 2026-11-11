"""SEARCH/REPLACE edit blocks: the edit format for repository-scale tasks (SWE-bench Pro).

Whole-file replies (``arms.FILE_FORMAT``) do not scale to files of 50K characters: the model
must copy every unchanged line, output costs grow with file size, and a truncated reply loses the
whole file. A SEARCH/REPLACE block names one exact piece of a file and its replacement.

Format (canonical form, the one prompts ask for)::

    <<<<<<< SEARCH path/relative/to/repo.py
    lines copied exactly from the current file
    =======
    the lines that replace them
    >>>>>>> REPLACE

Rules, exactly as :func:`parse_edits` and :func:`apply_edits` implement them:

1. A block starts at a line that is ``<<<<<<< SEARCH`` (seven ``<``) optionally followed by the
   path. If the marker line has no path, the path is taken from the nearest non-blank line above
   it, skipping one Markdown fence line (Aider's layout: path, fence, block). A block with no
   path is an error.
2. The block ends at the first line that is exactly ``>>>>>>> REPLACE``; the SEARCH part ends at
   the first line in between that is exactly ``=======`` (seven ``=``). If the body holds several
   such lines (the edited file itself contains one, as reStructuredText headings do), every split
   is tried, longest SEARCH first, and the first whose SEARCH text matches the file is used. Marker lines may
   carry trailing whitespace. A block cut off before its closing marker is an error and is
   dropped (a truncated reply never half-applies a block).
3. Text outside blocks is ignored, so prose and Markdown fences around blocks are harmless.
   Line endings in the reply are normalised to ``\\n``.
4. An empty SEARCH part creates a new file whose content is the REPLACE part. It is an error
   if the file already exists.
5. Otherwise the SEARCH text must occur **exactly once** in the file as it stands after the
   earlier blocks of the same reply (blocks apply in order, so several edits to one file are
   fine). Matching is exact. Only if the exact text is absent, one fallback is tried: the same
   lines compared with trailing whitespace ignored, again required to be unique. Nothing
   fuzzier is attempted. Zero or several matches are errors naming the path and block number.
6. If the file uses CRLF line endings, SEARCH and REPLACE are converted to CRLF before
   matching, so the file keeps its own line endings.
7. Paths must be relative, without ``..``; a path outside the allowed set (the lane contract)
   or matching a protected (test) pattern is rejected before anything is applied.
8. Block bodies always end in a newline. If a file has no final newline and the SEARCH text
   (without its last newline) is the unique end of the file, it matches there, and the
   REPLACE text's last newline is dropped too, so the file keeps having no final newline.

A failing block is reported and skipped; the other blocks still apply. Errors are returned, never
raised, so a worker's malformed reply becomes evidence in the log, not a crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Callable, Iterable, Mapping, Sequence

SEARCH_MARK = "<<<<<<< SEARCH"
DIVIDER = "======="
REPLACE_MARK = ">>>>>>> REPLACE"

_OPEN = re.compile(r"^<<<<<<< SEARCH(?:[ \t]+(?P<path>\S.*?))?[ \t]*$")
_DIV = re.compile(r"^=======[ \t]*$")
_CLOSE = re.compile(r"^>>>>>>> REPLACE[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")

EDIT_FORMAT = f"""Return your changes as SEARCH/REPLACE blocks, each in exactly this form:

{SEARCH_MARK} path/relative/to/repo
lines copied exactly from the current file (enough to be unique in that file)
{DIVIDER}
the lines that replace them
{REPLACE_MARK}

Rules: the SEARCH lines must match the current file exactly, including indentation, and must occur
exactly once in it. Use several small blocks rather than one large one; blocks apply in order. To
create a new file, leave the SEARCH part empty. To delete code, leave the REPLACE part empty. Only
the blocks are read; text outside them is ignored. Never edit test files."""


@dataclass(frozen=True)
class EditBlock:
    path: str
    search: str  # "" creates a file
    replace: str
    index: int  # 1-based position in the reply
    # Other (search, replace) splits when the body holds more than one "=======" line.
    alternatives: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ParsedEdits:
    blocks: tuple[EditBlock, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppliedEdits:
    files: dict[str, str]  # path -> new content, only files that changed or were created
    applied: tuple[int, ...]  # indices of blocks applied
    errors: tuple[str, ...] = ()
    created: tuple[str, ...] = ()


def clean_path(raw: str) -> str | None:
    """A safe repo-relative POSIX path, or None (absolute, ``..``, drive letters, empty)."""
    p = raw.strip().strip("`'\"*").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p) or any(c.isspace() for c in p):
        return None
    return p


def _path_above(lines: Sequence[str], i: int) -> str | None:
    j = i - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j >= 0 and _FENCE.match(lines[j]):
        j -= 1
        while j >= 0 and not lines[j].strip():
            j -= 1
    if j < 0:
        return None
    cand = lines[j].strip()
    if _FENCE.match(cand) or _CLOSE.match(cand) or _DIV.match(cand):
        return None
    cand = re.sub(r"^(?:#+\s*|File:\s*|file:\s*)", "", cand)
    return clean_path(cand)


def _body(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def parse_edits(text: str) -> ParsedEdits:
    """Parse every SEARCH/REPLACE block in a reply (see the module docstring for the rules)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[EditBlock] = []
    errors: list[str] = []
    i = 0
    n = 0
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m:
            if _DIV.match(lines[i]) or _CLOSE.match(lines[i]):
                pass  # stray markers outside a block are ignored like any other prose
            i += 1
            continue
        n += 1
        raw = m.group("path")
        path = clean_path(raw) if raw else _path_above(lines, i)
        # Everything up to the closing marker (or the next opening marker) is the block body.
        j = i + 1
        body: list[str] = []
        while j < len(lines) and not _CLOSE.match(lines[j]) and not _OPEN.match(lines[j]):
            body.append(lines[j])
            j += 1
        dividers = [k for k, ln in enumerate(body) if _DIV.match(ln)]
        if not dividers:
            errors.append(f"block {n}: no '{DIVIDER}' line after SEARCH (block dropped)")
            i = j if j < len(lines) and _OPEN.match(lines[j]) else j + 1
            continue
        if j >= len(lines) or not _CLOSE.match(lines[j]):
            errors.append(f"block {n}: no '{REPLACE_MARK}' line (block truncated or malformed; dropped)")
            i = j if j < len(lines) else j
            continue
        if path is None:
            errors.append(f"block {n}: missing or unsafe path {raw.strip() if raw else '(none)'!r}")
        else:
            # A divider line splits SEARCH from REPLACE. If the body has several (a file that
            # itself contains a "=======" line), the other splits are kept as alternatives and
            # the applier picks among them (rule 2).
            splits = tuple((_body(body[:k]), _body(body[k + 1:])) for k in dividers)
            blocks.append(EditBlock(path, splits[0][0], splits[0][1], n, splits[1:]))
        i = j + 1
    return ParsedEdits(tuple(blocks), tuple(errors))


def _find_unique(haystack: str, needle: str) -> tuple[int, int] | str:
    """(start, end) of the unique match of ``needle``; else an error string."""
    count = haystack.count(needle)
    if count == 1:
        s = haystack.index(needle)
        return s, s + len(needle)
    if count > 1:
        return f"matches {count} places; add surrounding lines to make it unique"
    # Fallback: the same lines with trailing whitespace ignored, still unique.
    nl = "\r\n" if needle.endswith("\r\n") else "\n"
    want = [ln.rstrip() for ln in needle.split(nl)[:-1]] if needle.endswith(nl) else [ln.rstrip() for ln in needle.split(nl)]
    have = haystack.split(nl)
    if not want:
        return "is empty"
    hits = [s for s in range(len(have) - len(want) + 1) if [x.rstrip() for x in have[s:s + len(want)]] == want]
    if len(hits) == 1:
        s = hits[0]
        start = sum(len(x) + len(nl) for x in have[:s])
        end = start + sum(len(x) + len(nl) for x in have[s:s + len(want)])
        return start, min(end, len(haystack))
    if len(hits) > 1:
        return f"matches {len(hits)} places (ignoring trailing whitespace); add surrounding lines"
    return "not found in the file (SEARCH must match the current file exactly)"


def _locate(text: str, search: str, replace: str) -> tuple[tuple[int, int] | str, str]:
    """Where ``search`` matches in ``text`` (rules 5, 6 and 8) and the replacement to use there."""
    if "\r\n" in text:
        search = search.replace("\r\n", "\n").replace("\n", "\r\n")
        replace = replace.replace("\r\n", "\n").replace("\n", "\r\n")
    stem = search[:-1].rstrip("\r") if search.endswith("\n") else search
    if search not in text and not text.endswith("\n") and stem and text.endswith(stem) and text.count(stem) == 1:
        # The file has no final newline; block bodies always end in one (rule 8).
        return (len(text) - len(stem), len(text)), (replace[:-1].rstrip("\r") if replace.endswith("\n") else replace)
    return _find_unique(text, search), replace


ScopeCheck = Callable[[str], "str | None"]


def scope_checker(allowed: Iterable[str], protected: Iterable[str] = ()) -> ScopeCheck:
    """A check returning None for a writable path, else the reason it is not."""
    allowed_t, protected_t = tuple(allowed), tuple(protected)

    def check(path: str) -> str | None:
        if any(fnmatchcase(path, p) for p in protected_t):
            return "protected (test file)"
        if not any(fnmatchcase(path, p) for p in allowed_t):
            return "outside the files this task may change"
        return None

    return check


def apply_edits(blocks: Sequence[EditBlock], originals: Mapping[str, str], *,
                scope: ScopeCheck | None = None, exists: Callable[[str], bool] | None = None) -> AppliedEdits:
    """Apply ``blocks`` in order to ``originals`` (path -> current content).

    ``exists(path)`` says whether a file not in ``originals`` exists in the repository (default:
    no), so a create-block cannot overwrite an unseen file. Returns only the files that changed.
    """
    current: dict[str, str] = {}
    created: list[str] = []
    applied: list[int] = []
    errors: list[str] = []
    for b in blocks:
        where = f"block {b.index} ({b.path})"
        if scope is not None:
            why = scope(b.path)
            if why:
                errors.append(f"{where}: rejected, {why}")
                continue
        text = current.get(b.path, originals.get(b.path))
        if b.search == "":
            if text is not None or (exists is not None and exists(b.path)):
                if text is not None and text == "":
                    current[b.path] = b.replace
                    applied.append(b.index)
                    continue
                errors.append(f"{where}: empty SEARCH creates a file, but the file already exists")
                continue
            current[b.path] = b.replace
            created.append(b.path)
            applied.append(b.index)
            continue
        if text is None:
            errors.append(f"{where}: file not found (only the files shown can be edited)")
            continue
        first_error = ""
        # Longest SEARCH first: when the file itself has a "=======" line, the split that keeps it
        # in SEARCH and still matches the file is the one that was meant.
        for search, replace in sorted(((b.search, b.replace), *b.alternatives), key=lambda x: -len(x[0])):
            found, replace = _locate(text, search, replace)
            if not isinstance(found, str):
                break
            first_error = first_error or found
        if isinstance(found, str):
            errors.append(f"{where}: SEARCH text {first_error}")
            continue
        s, e = found
        current[b.path] = text[:s] + replace + text[e:]
        applied.append(b.index)
    changed = {p: c for p, c in current.items() if p in created or c != originals.get(p)}
    return AppliedEdits(changed, tuple(applied), tuple(errors), tuple(p for p in created if p in changed))


def format_blocks(blocks: Iterable[EditBlock]) -> str:
    """Render blocks in the canonical form."""
    out = []
    def nl(x: str) -> str:
        return x if not x or x.endswith("\n") else x + "\n"

    for b in blocks:
        out.append(f"{SEARCH_MARK} {b.path}\n{nl(b.search)}{DIVIDER}\n{nl(b.replace)}{REPLACE_MARK}")
    return "\n\n".join(out) + ("\n" if out else "")


# ------------------------------------------------------------------ unified diff -> blocks


@dataclass(frozen=True)
class Hunk:
    old_start: int  # 1-based line in the original file (0 for an empty original)
    old: tuple[str, ...]  # old-side lines, each ending in "\n" unless it is a last line without one
    new: tuple[str, ...]


@dataclass(frozen=True)
class FileDiff:
    path: str  # new path
    old_path: str | None  # None for a created file
    hunks: tuple[Hunk, ...]
    deleted: bool = False
    renamed: bool = False


_DIFF_HEAD = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(patch: str) -> list[FileDiff]:
    """Parse a unified (``diff --git``) patch into per-file hunks (text patches only).

    Hunk bodies are read by the line counts in their ``@@`` headers, so blank lines and
    ``\\ No newline at end of file`` markers are handled exactly."""
    out: list[FileDiff] = []
    lines = patch.split("\n")
    i = 0
    while i < len(lines):
        m = _DIFF_HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        old_path: str | None = m.group(1)
        path = m.group(2)
        deleted = renamed = False
        i += 1
        while i < len(lines) and not lines[i].startswith("@@") and not _DIFF_HEAD.match(lines[i]):
            ln = lines[i]
            if ln.startswith("new file mode") or ln == "--- /dev/null":
                old_path = None
            elif ln.startswith("deleted file mode") or ln == "+++ /dev/null":
                deleted = True
            elif ln.startswith("rename from"):
                renamed = True
            elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
                raise ValueError(f"{path}: binary patches are not supported")
            i += 1
        hunks: list[Hunk] = []
        while i < len(lines):
            h = _HUNK.match(lines[i])
            if not h:
                break
            n_old = int(h.group(2)) if h.group(2) is not None else 1
            n_new = int(h.group(4)) if h.group(4) is not None else 1
            i += 1
            old: list[str] = []
            new: list[str] = []
            last = ""
            while i < len(lines) and (len(old) < n_old or len(new) < n_new or lines[i].startswith("\\")):
                ln = lines[i]
                if ln.startswith("\\"):  # "\ No newline at end of file": the previous line has no "\n"
                    if last in ("-", " ") and old:
                        old[-1] = old[-1][:-1]
                    if last in ("+", " ") and new:
                        new[-1] = new[-1][:-1]
                elif ln.startswith("+"):
                    new.append(ln[1:] + "\n")
                    last = "+"
                elif ln.startswith("-"):
                    old.append(ln[1:] + "\n")
                    last = "-"
                else:  # context; a bare blank line stands for an empty context line
                    body = ln[1:] if ln.startswith(" ") else ln
                    old.append(body + "\n")
                    new.append(body + "\n")
                    last = " "
                i += 1
            hunks.append(Hunk(int(h.group(1)), tuple(old), tuple(new)))
        out.append(FileDiff(path, old_path, tuple(hunks), deleted, renamed))
    return out


def diff_to_blocks(patch: str, originals: Mapping[str, str]) -> list[EditBlock]:
    """Convert a unified diff into SEARCH/REPLACE blocks that :func:`apply_edits` accepts.

    Each hunk becomes one block whose SEARCH is the hunk's old side, located by the hunk header.
    If that text is not unique in the file as it stands after the earlier hunks, lines from the
    file are added above and below until it is. Used to push the gold patch through the same edit
    path as model output. Raises ValueError for deletions, renames and binary patches.
    """
    blocks: list[EditBlock] = []
    idx = 0
    for fd in parse_unified_diff(patch):
        if fd.deleted or fd.renamed:
            raise ValueError(f"{fd.path}: deletions and renames are not expressible as SEARCH/REPLACE")
        if fd.old_path is None:
            idx += 1
            blocks.append(EditBlock(fd.path, "", "".join(ln for h in fd.hunks for ln in h.new), idx))
            continue
        text = originals[fd.path]
        delta = 0  # line shift caused by the hunks already applied to ``text``
        for h in fd.hunks:
            old_s, new_s = "".join(h.old), "".join(h.new)
            file_lines = text.splitlines(keepends=True)
            s = (max(h.old_start - 1, 0) if h.old else h.old_start) + delta
            e = s + len(h.old)
            if "".join(file_lines[s:e]) != old_s:
                raise ValueError(f"{fd.path}: hunk at line {h.old_start} does not match the original file")
            pre, post = "", ""
            while not (pre + old_s + post) or text.count(pre + old_s + post) != 1:
                grew = False
                if s > 0:
                    s -= 1
                    pre = file_lines[s] + pre
                    grew = True
                if e < len(file_lines):
                    post = post + file_lines[e]
                    e += 1
                    grew = True
                if not grew:
                    raise ValueError(f"{fd.path}: cannot make the hunk at line {h.old_start} unique")
            idx += 1
            search, replace = pre + old_s + post, pre + new_s + post
            blocks.append(EditBlock(fd.path, search, replace, idx))
            text = text.replace(search, replace, 1)
            delta += len(h.new) - len(h.old)
    return blocks


def unified_patch(originals: Mapping[str, str], files: Mapping[str, str]) -> str:
    """A ``git apply``-compatible patch of ``files`` against ``originals`` (for logs and review),
    with ``\\ No newline at end of file`` markers where needed."""
    import difflib

    parts: list[str] = []
    for path in sorted(files):
        a = originals.get(path)
        b = files[path]
        if a == b:
            continue
        a_lines = (a or "").splitlines(keepends=True)
        b_lines = b.splitlines(keepends=True)
        head = [f"diff --git a/{path} b/{path}\n"]
        if a is None:
            head.append("new file mode 100644\n")
        fromfile = "/dev/null" if a is None else f"a/{path}"
        body = list(difflib.unified_diff(a_lines, b_lines, fromfile=fromfile, tofile=f"b/{path}"))
        fixed = []
        for ln in body:
            if ln.endswith("\n"):
                fixed.append(ln)
            else:
                fixed.append(ln + "\n\\ No newline at end of file\n")
        parts.extend(head + fixed)
    return "".join(parts)
