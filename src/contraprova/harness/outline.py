"""File outlines: what the orchestrator sees of a repository instead of the files themselves.

The cost hypothesis of the study is that a strong planner can direct cheap workers from a
*summary* of the code, so it pays strong-model input prices on a few thousand tokens, not on the
25K-token files the workers edit. If the orchestrator read the full files, arms C and D would pay
Opus prices on the same input as arm A, and "the swarm is cheaper" would fail by construction.
So the outline holds names, signatures and one-line summaries, never statement bodies.

Python is outlined with ``ast`` (the file is parsed, never executed): imports (module names
only), module-level constants (names), classes with bases and decorators, functions and methods
with their full signature line(s), and the first line of each docstring, cut to 100 characters.

JavaScript and TypeScript are outlined line by line with regular expressions after string and
comment contents are blanked: ``function`` declarations, classes (``extends``), class methods,
``const f = (...) =>`` / ``function`` expressions, ``module.exports``/``exports.x =``
assignments, ``Foo.bar = function``, ES ``export`` forms, TypeScript ``interface``/``type``/
``enum`` declarations and object-literal methods at the top two nesting levels. Only the
declaration line is kept (cut at its opening brace), never the body.

Any other file (YAML, RST, JSON...) is listed with its line count only.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Mapping

MAX_SIG_CHARS = 200
MAX_DOC_CHARS = 100


@dataclass(frozen=True)
class OutlineEntry:
    line: int
    depth: int
    text: str


def _doc1(node) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    first = doc.strip().splitlines()[0].strip()
    return first[:MAX_DOC_CHARS] + ("..." if len(first) > MAX_DOC_CHARS else "")


def _cut(s: str, n: int = MAX_SIG_CHARS) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _py_args(a: ast.arguments) -> str:
    def one(arg: ast.arg, default: ast.expr | None) -> str:
        s = arg.arg
        if arg.annotation is not None:
            s += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            d = ast.unparse(default)
            s += f"={d if len(d) <= 30 else '...'}"
        return s

    pos = [*a.posonlyargs, *a.args]
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    parts = [one(x, d) for x, d in zip(pos, defaults)]
    if a.posonlyargs:
        parts.insert(len(a.posonlyargs), "/")
    if a.vararg:
        parts.append("*" + one(a.vararg, None))
    elif a.kwonlyargs:
        parts.append("*")
    parts.extend(one(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults))
    if a.kwarg:
        parts.append("**" + one(a.kwarg, None))
    return ", ".join(parts)


def outline_python(source: str) -> list[OutlineEntry]:
    """Signatures and one-line docstrings of a Python module (raises SyntaxError if unparsable)."""
    tree = ast.parse(source)
    out: list[OutlineEntry] = []
    imports: list[str] = []
    consts: list[str] = []

    def visit(body, depth: int) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deco = "".join(f"@{_cut(ast.unparse(d), 60)} " for d in node.decorator_list)
                ret = f" -> {_cut(ast.unparse(node.returns), 60)}" if node.returns is not None else ""
                kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                doc = _doc1(node)
                text = _cut(f"{deco}{kw} {node.name}({_py_args(node.args)}){ret}")
                out.append(OutlineEntry(node.lineno, depth, text + (f"  # {doc}" if doc else "")))
            elif isinstance(node, ast.ClassDef):
                deco = "".join(f"@{_cut(ast.unparse(d), 60)} " for d in node.decorator_list)
                bases = ", ".join(_cut(ast.unparse(b), 60) for b in [*node.bases, *node.keywords])
                doc = _doc1(node)
                text = _cut(f"{deco}class {node.name}" + (f"({bases})" if bases else ""))
                out.append(OutlineEntry(node.lineno, depth, text + (f"  # {doc}" if doc else "")))
                visit(node.body, depth + 1)
            elif depth == 0 and isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                else:
                    imports.append("." * node.level + (node.module or ""))
            elif depth == 0 and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        consts.append(t.id)
            elif isinstance(node, (ast.If, ast.Try)) and depth == 0:
                # Conditional definitions (``if TYPE_CHECKING:``, ``try: import``) at module level.
                visit(node.body, depth)
                for h in getattr(node, "handlers", []):
                    visit(h.body, depth)
                visit(node.orelse, depth)

    visit(tree.body, 0)
    head: list[OutlineEntry] = []
    if imports:
        uniq = list(dict.fromkeys(imports))
        head.append(OutlineEntry(0, 0, _cut("imports: " + ", ".join(uniq), 400)))
    if consts:
        uniq = list(dict.fromkeys(consts))
        head.append(OutlineEntry(0, 0, _cut("module names: " + ", ".join(uniq), 400)))
    return head + out


# ------------------------------------------------------------------ JavaScript / TypeScript

_JS_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "function", "else", "do", "try",
                          "with", "typeof", "new", "await", "yield", "case", "delete", "void", "throw", "super"})
_JS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\s+[A-Za-z_$][\w$]*.*"),
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*[A-Za-z_$][\w$]*\s*(?:<[^>]*>)?\s*\(.*"),
    re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|enum|namespace)\s+[A-Za-z_$][\w$]*.*"),
    re.compile(r"^\s*(?:export\s+)?type\s+[A-Za-z_$][\w$]*\s*(?:<[^=]*>)?\s*=.*"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*(?::[^=]+)?=\s*(?:async\s+)?"
               r"(?:function\b.*|\([^)]*\)\s*(?::[^=]+)?=>.*|[A-Za-z_$][\w$]*\s*=>.*|React\.memo.*|forwardRef.*)"),
    re.compile(r"^\s*(?:module\.)?exports(?:\.[A-Za-z_$][\w$]*)?\s*=.*"),
    re.compile(r"^\s*(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(?:module\.)?exports\b.*"),
    re.compile(r"^\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*=\s*(?:async\s+)?(?:function\b.*|\([^)]*\)\s*=>.*|"
               r"[A-Za-z_$][\w$]*\s*=>.*)"),
    re.compile(r"^\s*export\s+(?:default\s+)?\{.*"),
)
_JS_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|readonly|async|get|set|override|abstract)\s+)*"
    r"\*?\s*#?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)?\s*(?::\s*[^{]+)?\s*\{?\s*$")
_JS_PROP_FN = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?(?:function\b.*|\([^)]*\)\s*=>.*|[A-Za-z_$][\w$]*\s*=>.*)")


def _blank_js(source: str) -> str:
    """Blank out comments and the contents of string/template literals, keeping line structure,
    so braces and keywords inside them do not confuse the outline."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = source.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and nxt == "*":
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in source[i:j]))
            i = j
        elif c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                if source[j] == "\\":
                    j += 1
                elif source[j] == "\n" and c != "`":
                    break
                j += 1
            seg = source[i + 1:j]
            out.append(c + "".join(ch if ch == "\n" else " " for ch in seg) + (c if j < n and source[j] == c else ""))
            i = j + 1 if j < n and source[j] == c else j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def outline_js(source: str) -> list[OutlineEntry]:
    """Declaration lines of a JavaScript/TypeScript file (bodies are never included)."""
    blank = _blank_js(source).split("\n")
    real = source.split("\n")
    out: list[OutlineEntry] = []
    depth = 0
    class_depths: list[int] = []
    for no, (b, r) in enumerate(zip(blank, real), start=1):
        stripped = b.strip()
        entry = None
        if stripped:
            if any(p.match(b) for p in _JS_PATTERNS):
                entry = r
                if re.match(r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\b", b):
                    class_depths.append(depth)
            elif depth <= 2:
                m = _JS_METHOD.match(b)
                in_class = bool(class_depths) and depth == class_depths[-1] + 1
                if m and m.group(1) not in _JS_KEYWORDS and (in_class or depth <= 1):
                    if in_class or b.rstrip().endswith("{"):
                        entry = r
                elif _JS_PROP_FN.match(b) and depth <= 2:
                    entry = r
        if entry is not None:
            text = entry.split("{")[0].rstrip() if "{" in b else entry.rstrip()
            text = re.sub(r"\s*=>\s*$", " =>", text)
            out.append(OutlineEntry(no, depth, _cut(text)))
        depth += b.count("{") - b.count("}")
        depth = max(depth, 0)
        while class_depths and depth <= class_depths[-1] and "}" in b:
            class_depths.pop()
    return out


# ------------------------------------------------------------------ files

PY_EXT = (".py", ".pyi")
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def outline_file(path: str, source: str) -> list[OutlineEntry]:
    if path.endswith(PY_EXT):
        try:
            return outline_python(source)
        except (SyntaxError, ValueError):
            return [OutlineEntry(0, 0, "(python file did not parse; no outline)")]
    if path.endswith(JS_EXT):
        return outline_js(source)
    return []


def render_outline(files: Mapping[str, str], *, new_files: tuple[str, ...] = (), max_chars: int | None = None) -> str:
    """Outline of every file in ``files`` (path -> source) as plain text, files in path order.

    ``new_files`` are listed as files the task may create. If ``max_chars`` is given, the
    deepest entries are dropped first (then entries are cut from the end of the largest file)
    until the text fits; a note says so. Never includes a statement body."""
    def render(limit_depth: int | None) -> str:
        parts = []
        for path in sorted(files):
            src = files[path]
            nlines = src.count("\n") + (1 if src and not src.endswith("\n") else 0)
            entries = [e for e in outline_file(path, src) if limit_depth is None or e.depth <= limit_depth]
            body = "\n".join(f"{'  ' * e.depth}{'L' + str(e.line) + ': ' if e.line else ''}{e.text}" for e in entries)
            parts.append(f"### {path} ({nlines} lines)\n{body}" if body else f"### {path} ({nlines} lines)")
        for path in new_files:
            parts.append(f"### {path} (new file, does not exist yet)")
        return "\n\n".join(parts)

    text = render(None)
    if max_chars is None or len(text) <= max_chars:
        return text
    for d in (2, 1, 0):
        text = render(d)
        if len(text) <= max_chars:
            return text + f"\n\n[outline limited to nesting depth {d} to fit]"
    return text[: max_chars - 60].rsplit("\n", 1)[0] + "\n\n[outline cut to fit its size limit]"
