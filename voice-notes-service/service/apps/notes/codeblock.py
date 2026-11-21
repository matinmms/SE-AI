from __future__ import annotations

from html import escape
import re
from typing import Any

MAX_CODE_CHARS = 6000
MAX_LINES_PER_BLOCK = 24

LANGUAGE_LABELS = {
    "python": "Python",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "sql": "SQL",
    "bash": "Bash",
    "matlab": "MATLAB",
    "verilog": "Verilog",
    "vhdl": "VHDL",
    "assembly": "Assembly",
    "html": "HTML",
    "css": "CSS",
    "json": "JSON",
    "r": "R",
    "go": "Go",
    "rust": "Rust",
    "php": "PHP",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "pseudocode": "Pseudocode",
    "text": "Code",
}

_ALIASES = {
    "py": "python", "python3": "python", "c++": "cpp", "cc": "cpp", "cxx": "cpp", "c#": "csharp",
    "cs": "csharp", "js": "javascript", "node": "javascript", "ts": "typescript", "sh": "bash",
    "shell": "bash", "zsh": "bash", "m": "matlab", "octave": "matlab", "v": "verilog",
    "systemverilog": "verilog", "sv": "verilog", "asm": "assembly", "x86": "assembly", "mips": "assembly",
    "arm": "assembly", "psql": "sql", "mysql": "sql", "postgresql": "sql", "sqlite": "sql",
    "golang": "go", "rs": "rust", "pseudo": "pseudocode", "algorithm": "pseudocode", "plain": "text",
    "txt": "text", "": "text",
}

_C_KEYWORDS = (
    "if else for while do switch case break continue return goto default struct union enum typedef "
    "static const volatile extern sizeof void int char short long float double unsigned signed "
    "bool true false NULL nullptr class public private protected namespace using template typename "
    "new delete this virtual override final auto inline try catch throw"
)
_KEYWORDS: dict[str, frozenset[str]] = {
    "python": frozenset(
        "def class if elif else for while in not and or is return import from as with try except finally "
        "raise lambda yield pass break continue None True False global nonlocal assert del async await self".split()
    ),
    "c": frozenset(_C_KEYWORDS.split()),
    "cpp": frozenset(_C_KEYWORDS.split()),
    "csharp": frozenset(
        (_C_KEYWORDS + " string var foreach in async await get set readonly override interface").split()
    ),
    "java": frozenset(
        (_C_KEYWORDS + " extends implements interface package import final abstract synchronized boolean "
         "String instanceof throws").split()
    ),
    "javascript": frozenset(
        "function const let var if else for while do switch case break continue return class extends new "
        "this import export from default async await try catch finally throw typeof instanceof in of null "
        "undefined true false yield".split()
    ),
    "typescript": frozenset(
        "function const let var if else for while do switch case break continue return class extends new "
        "this import export from default async await try catch finally throw typeof instanceof in of null "
        "undefined true false interface type enum implements readonly public private protected string "
        "number boolean any void".split()
    ),
    "sql": frozenset(
        "select from where group by having order insert into values update set delete create table alter "
        "drop add index primary key foreign references join inner left right outer full on as and or not "
        "null is in like between distinct limit offset union all exists case when then else end count sum "
        "avg min max constraint default unique view database int varchar text date".split()
    ),
    "bash": frozenset(
        "if then else elif fi for while do done case esac in function return exit echo export local cd "
        "ls grep sed awk cat mkdir rm cp mv sudo".split()
    ),
    "matlab": frozenset(
        "function end if elseif else for while switch case otherwise break continue return try catch "
        "global persistent true false".split()
    ),
    "verilog": frozenset(
        "module endmodule input output inout wire reg assign always begin end if else case endcase for "
        "posedge negedge parameter localparam initial integer logic generate endgenerate default".split()
    ),
    "vhdl": frozenset(
        "library use entity is port architecture of begin end process if then else elsif case when "
        "signal variable in out inout std_logic std_logic_vector integer downto to others".split()
    ),
    "assembly": frozenset(),
    "go": frozenset(
        "func package import var const type struct interface map chan go defer return if else for range "
        "switch case default break continue select nil true false".split()
    ),
    "rust": frozenset(
        "fn let mut const struct enum impl trait pub use mod match if else for while loop return self "
        "Self true false as in ref move async await".split()
    ),
    "php": frozenset(
        "function class public private protected static if else elseif foreach for while return new echo "
        "namespace use try catch throw null true false".split()
    ),
    "kotlin": frozenset(
        "fun val var class object interface if else for while when return null true false in is as "
        "override open data sealed".split()
    ),
    "swift": frozenset(
        "func let var class struct enum protocol if else for while switch case return nil true false in "
        "guard import self init".split()
    ),
    "r": frozenset("function if else for while repeat return TRUE FALSE NULL NA in library".split()),
    "pseudocode": frozenset(
        "if then else elseif endif for while do endwhile endfor repeat until return function procedure "
        "end begin input output print set to and or not true false".split()
    ),
}

_LINE_COMMENT = {
    "python": "#", "bash": "#", "r": "#", "matlab": "%", "vhdl": "--", "sql": "--", "assembly": ";",
    "pseudocode": "//", "text": "",
}
_BLOCK_COMMENT = {"c", "cpp", "csharp", "java", "javascript", "typescript", "go", "rust", "php", "kotlin",
                  "swift", "verilog", "css", "sql"}
_SLASH_COMMENT = {"c", "cpp", "csharp", "java", "javascript", "typescript", "go", "rust", "php", "kotlin",
                  "swift", "verilog", "pseudocode"}

_TOKEN = re.compile(
    r"(?P<string>\"(?:\\.|[^\"\\\n])*\"?|'(?:\\.|[^'\\\n])*'?|`[^`\n]*`?)"
    r"|(?P<number>\b0[xX][0-9a-fA-F]+\b|\b0[bB][01]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<other>.)",
    re.DOTALL,
)


def normalize_language(value: Any) -> str:
    raw = re.sub(r"\s+", "", str(value or "").strip().casefold())
    raw = _ALIASES.get(raw, raw)
    return raw if raw in LANGUAGE_LABELS else "text"


def language_label(language: str) -> str:
    return LANGUAGE_LABELS.get(language, "Code")


def clean_code(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = re.sub(r"^```[^\n]*\n|\n?```\s*$", "", text.strip("\n"))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    indent = min((len(line) - len(line.lstrip(" ")) for line in lines if line.strip()), default=0)
    return "\n".join(line[indent:] for line in lines)[:MAX_CODE_CHARS]


def _span(kind: str, text: str) -> str:
    return f'<span class="tok-{kind}">{escape(text)}</span>' if text else ""


def _highlight_line(line: str, language: str, in_block_comment: bool) -> tuple[str, bool]:
    keywords = _KEYWORDS.get(language, frozenset())
    case_insensitive = language in {"sql", "vhdl", "pseudocode", "assembly"}
    line_comment = _LINE_COMMENT.get(language, "")
    out: list[str] = []
    position = 0

    if in_block_comment:
        end = line.find("*/")
        if end == -1:
            return _span("c", line), True
        out.append(_span("c", line[: end + 2]))
        position = end + 2
        in_block_comment = False

    while position < len(line):
        rest = line[position:]
        if language in _SLASH_COMMENT and rest.startswith("//"):
            out.append(_span("c", rest))
            break
        if line_comment and line_comment != "//" and rest.startswith(line_comment):
            out.append(_span("c", rest))
            break
        if language in _BLOCK_COMMENT and rest.startswith("/*"):
            end = rest.find("*/", 2)
            if end == -1:
                out.append(_span("c", rest))
                in_block_comment = True
                break
            out.append(_span("c", rest[: end + 2]))
            position += end + 2
            continue
        if language in {"c", "cpp"} and position == len(line) - len(line.lstrip()) and rest.startswith("#"):
            out.append(_span("p", rest))
            break
        match = _TOKEN.match(line, position)
        if not match:  # defensive; the "other" branch matches any character
            out.append(escape(rest[0]))
            position += 1
            continue
        text = match.group(0)
        kind = match.lastgroup
        position = match.end()
        if kind == "string":
            out.append(_span("s", text))
        elif kind == "number":
            out.append(_span("n", text))
        elif kind == "word":
            lookup = text.lower() if case_insensitive else text
            tail = line[position:].lstrip()
            if lookup in keywords or (case_insensitive and lookup in keywords):
                out.append(_span("k", text))
            elif tail.startswith("("):
                out.append(_span("f", text))
            elif text[:1].isupper() and language in {"python", "java", "csharp", "cpp", "typescript",
                                                      "kotlin", "swift", "rust", "go"}:
                out.append(_span("t", text))
            else:
                out.append(escape(text))
        else:
            out.append(escape(text))
    return "".join(out), in_block_comment


def highlight_code_lines(code: str, language: str) -> list[str]:
    in_block = False
    rendered: list[str] = []
    for line in code.split("\n"):
        html, in_block = _highlight_line(line, language, in_block)
        rendered.append(html)
    return rendered


def render_code_block_html(block: dict[str, Any]) -> str:
    code = clean_code(block.get("code") or block.get("content") or "")
    language = normalize_language(block.get("language"))
    caption = str(block.get("caption") or block.get("title") or "").strip()
    start = block.get("start_line")
    start_line = int(start) if isinstance(start, int) and start > 1 else 1
    lines = highlight_code_lines(code, language)
    body = "".join(
        f'<span class="code-block__line">{line or "&#8203;"}</span>' for line in lines
    )
    caption_html = (
        f'<span class="code-block__caption">{escape(caption)}</span>' if caption else ""
    )
    counter = f' style="counter-reset: cb-line {start_line - 1}"' if start_line > 1 else ""
    return (
        f'<div class="code-block" data-language="{escape(language)}" dir="ltr">\n'
        '    <div class="code-block__header">\n'
        '        <span class="code-block__dots" aria-hidden="true"><i></i><i></i><i></i></span>\n'
        f'        <span class="code-block__language">{escape(language_label(language))}</span>\n'
        f"        {caption_html}\n"
        "    </div>\n"
        f'    <pre class="code-block__pre"{counter}><code class="code-block__code">{body}</code></pre>\n'
        "</div>"
    )


def split_code_for_pages(code: str, max_lines: int = MAX_LINES_PER_BLOCK) -> list[tuple[str, int]]:
    """Split long code into page-sized chunks, returning (code, first_line_number)."""
    lines = clean_code(code).split("\n")
    if not lines or lines == [""]:
        return []
    if len(lines) <= max_lines:
        return [("\n".join(lines), 1)]
    chunks: list[tuple[str, int]] = []
    index = 0
    while index < len(lines):
        end = min(index + max_lines, len(lines))
        if end < len(lines):
            # Prefer breaking at a blank line near the end of the chunk.
            for probe in range(end, index + max_lines // 2, -1):
                if not lines[probe - 1].strip():
                    end = probe
                    break
        chunk = "\n".join(lines[index:end]).strip("\n")
        if chunk.strip():
            chunks.append((chunk, index + 1))
        index = end
    return chunks
