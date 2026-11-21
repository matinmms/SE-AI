from __future__ import annotations

from html import escape
import re
from typing import Any

from apps.notes.codeblock import clean_code, render_code_block_html
from apps.notes.mathtypeset import repair_latex_escapes, restore_math, stash_math, typeset_math
from apps.notes.validators import validate_body_fragment


SUPPORTED_BLOCK_TYPES: tuple[str, ...] = (
    "paragraph",
    "chapter-header",
    "section-header",
    "subsection-header",
    "formula-box",
    "code-block",
    "example-box",
    "unrelated-discussion-box",
    "note-box",
    "summary-box",
    "exam-point",
    "table-container",
    "figure-reference",
    "key-highlight",
    "page-header",
    "page-footer",
    "list",
    "table-of-contents",
)


SUMMARY_TYPE_ALIASES = {
    "summary",
    "summary-box",
    "summarybox",
    "conclusion",
    "takeaways",
    "key-takeaways",
    "جمع-بندی",
    "جمع‌بندی",
    "جمع بندی",
    "خلاصه",
}
SUMMARY_TITLE_PREFIXES = (
    "summary",
    "finalsummary",
    "lessonsummary",
    "coursesummary",
    "conclusion",
    "keytakeaways",
    "takeaways",
    "جمعبندی",
    "خلاصه",
    "نتیجهگیری",
    "نکاتپایانی",
    "نکتهپایانی",
    "مرورنهایی",
)

UNRELATED_DISCUSSION_TYPE_ALIASES = {
    "off-topic",
    "offtopic",
    "unrelated-discussion",
    "unrelated-discussion-box",
    "side-discussion",
    "non-course-discussion",
    "گفتگویخارجازموضوع",
    "گفتوگویخارجازموضوع",
    "خارجازموضوعدرس",
}


def _is_unrelated_discussion_label(label: str) -> bool:
    key = _semantic_key(label)
    return "خارجازموضوع" in key or "offtopic" in key or "unrelateddiscussion" in key


def _semantic_key(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", normalized)


def canonical_semantic_block_type(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    normalized = re.sub(r"[\s_]+", "-", raw)
    semantic_key = _semantic_key(raw)
    if (
        raw in SUMMARY_TYPE_ALIASES
        or normalized in SUMMARY_TYPE_ALIASES
        or any(semantic_key.startswith(prefix) for prefix in SUMMARY_TITLE_PREFIXES)
    ):
        return "summary-box"
    if raw in UNRELATED_DISCUSSION_TYPE_ALIASES or normalized in UNRELATED_DISCUSSION_TYPE_ALIASES:
        return "unrelated-discussion-box"
    if normalized in {"code", "codeblock", "code-snippet", "source-code", "snippet"}:
        return "code-block"
    return normalized


def is_summary_semantic_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = canonical_semantic_block_type(
        block.get("type") or block.get("block_type") or block.get("component")
    )
    if block_type == "summary-box":
        return True
    if block_type in {
        "chapter-header",
        "section-header",
        "subsection-header",
        "page-header",
        "page-footer",
        "formula-box",
        "code-block",
        "figure-reference",
        "table-container",
        "example-box",
        "exam-point",
    }:
        return False
    title_key = _semantic_key(block.get("title") or block.get("label") or "")
    return bool(title_key and any(title_key.startswith(prefix) for prefix in SUMMARY_TITLE_PREFIXES))


def _attr(value: Any) -> str:
    return escape(str(value), quote=True)


# Generated notes occasionally slip into LaTeX-style math delimiters
# ($...$, $$...$$, \(...\), \[...\]) even though nothing in this app
# renders LaTeX, leaving literal delimiters visible around otherwise-plain
# text like "$T(n)$". Strip the delimiters everywhere text is rendered and
# keep the inner content, since that's the only place every block type's
# text passes through on the way to HTML.
LATEX_DELIMITER_PATTERN = re.compile(
    r"\$\$([^$]+?)\$\$"
    r"|\$([^\s$](?:[^$\n]*[^\s$])?)\$"
    r"|\\\[([^\n]+?)\\\]"
    r"|\\\(([^\n]+?)\\\)"
)


def _strip_latex_delimiters(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return next(group for group in match.groups() if group is not None).strip()

    return LATEX_DELIMITER_PATTERN.sub(_replace, text)


def _text(value: Any) -> str:
    stashed, items = stash_math(str(value))
    return restore_math(escape(stashed), items)


_FORMULA_MARKUP_PATTERN = re.compile(r"\\[A-Za-z]|\^|_\{|_\d|\x08|\x0c|\x07")
_ARABIC_SCRIPT_PATTERN = re.compile(r"[\u0600-\u06FF]")
# A formula box that holds only prose or a word equation ("RAAS رنین: ...") written in
# Persian/Arabic: it is text, not math, and must not be set in the left-to-right mono face.
_PLAIN_TEXT_FORMULA_BOX_PATTERN = re.compile(r'<div class="formula-box"( role="math")?>(\s*)([^<>]*?)(\s*)</div>')


def mark_text_formula_boxes(html: str) -> str:
    """Give older notes the same text styling for prose-only formula boxes as new ones."""

    def mark(match: re.Match[str]) -> str:
        text = match.group(3)
        if not _ARABIC_SCRIPT_PATTERN.search(text):
            return match.group(0)
        return f'<div class="formula-box formula-box--text"{match.group(1) or ""}>{match.group(2)}{text}{match.group(4)}</div>'

    return _PLAIN_TEXT_FORMULA_BOX_PATTERN.sub(mark, html)


def _typeset_formula(source: str) -> str:
    return typeset_math(source)


_TEXT_COMMAND = re.compile(r"\\(?:text|textrm|mathrm|mbox)\s*\{")
# A Persian phrase; a unit in parentheses, «جرم آگارز (گرم)», stays with its words.
_BARE_ARABIC_RUN = re.compile(r"[\u0600-\u06FF](?:[\u0600-\u06FF\u200c\s،؛؟:()]*[\u0600-\u06FF)])?")
_SPACING_COMMANDS = re.compile(r"\\(?:q?quad|,|;|:|!|\s)")
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9_\[\]()+\-*/^<>=.|]*")


_FRAC_COMMAND = re.compile(r"\\[dt]?frac\s*\{")


def _brace_group(raw: str, open_index: int) -> tuple[str, int]:
    """The text inside the {…} that opens at `open_index` (balanced), and the index after its closing brace."""
    depth, index = 1, open_index + 1
    while index < len(raw) and depth:
        depth += {"{": 1, "}": -1}.get(raw[index], 0)
        index += 1
    return raw[open_index + 1 : index - 1 if depth == 0 else index], index


def _has_words(chunk: str) -> bool:
    return bool(_TEXT_COMMAND.search(chunk) or _BARE_ARABIC_RUN.search(chunk))


def _split_mixed_formula(raw: str) -> list[tuple[str, Any]]:
    """Split a formula that mixes Persian words and math into ("math" | "text" | "frac", content) parts.

    The parts are laid out in a right-to-left row, each math part on its own left-to-right island,
    so Persian reads naturally and every expression keeps its own order. A fraction with words in
    its numerator or denominator stays one fraction: ("frac", (numerator, denominator)).
    """
    parts: list[tuple[str, Any]] = []

    def add_math(chunk: str) -> None:
        chunk = _SPACING_COMMANDS.sub(" ", chunk).strip()
        # A lone operator between words ("درصد = …") is math too; a bare hyphen between words is not.
        if chunk and (re.search(r"[A-Za-z0-9\\]", chunk) or re.search(r"[=<>≈≤≥≠+×%]", chunk)):
            parts.append(("math", chunk))

    def add_bare(chunk: str) -> None:
        cursor = 0
        for match in _BARE_ARABIC_RUN.finditer(chunk):
            add_math(chunk[cursor : match.start()])
            parts.append(("text", match.group(0).strip()))
            cursor = match.end()
        add_math(chunk[cursor:])

    index = 0
    buffer_start = 0
    while index < len(raw):
        text_command = _TEXT_COMMAND.match(raw, index)
        frac_command = _FRAC_COMMAND.match(raw, index)
        if text_command:
            add_bare(raw[buffer_start:index])
            inner, after = _brace_group(raw, text_command.end() - 1)
            if inner.strip():
                parts.append(("text", inner.strip()))
            index = buffer_start = after
        elif frac_command:
            numerator, after_num = _brace_group(raw, frac_command.end() - 1)
            brace = raw.find("{", after_num) if after_num < len(raw) and raw[after_num:].lstrip().startswith("{") else -1
            if brace == -1:
                index += 1
                continue
            denominator, after = _brace_group(raw, brace)
            if _has_words(numerator) or _has_words(denominator):
                add_bare(raw[buffer_start:index])
                parts.append(("frac", (numerator, denominator)))
                index = buffer_start = after
            else:
                index = after  # a fraction of pure math stays inside the surrounding math chunk
        else:
            index += 1
    add_bare(raw[buffer_start:])
    return parts


def _mixed_text_html(text: str) -> str:
    """A Persian phrase; Latin symbols inside it (DP[j]+1, A_i) become small left-to-right islands."""
    output: list[str] = []
    cursor = 0
    for match in _LATIN_RUN.finditer(text):
        output.append(escape(text[cursor : match.start()]))
        run = match.group(0)
        latex = _ascii_math_to_latex(run)
        output.append(f'<span class="formula-inline" dir="ltr">{_typeset_formula(latex)}</span>')
        cursor = match.end()
    output.append(escape(text[cursor:]))
    return "".join(output)


def _render_mixed_parts(parts: list[tuple[str, Any]]) -> str:
    pieces: list[str] = []
    for kind, content in parts:
        if kind == "math":
            latex = _ascii_math_to_latex(content)
            pieces.append(f'<span class="formula-box__part formula-box__part--math" dir="ltr">{_typeset_formula(latex)}</span>')
        elif kind == "frac":
            numerator, denominator = content
            pieces.append(
                '<span class="formula-box__part formula-box__part--frac" dir="ltr"><span class="mx-frac">'
                f'<span class="mx-num"><span class="formula-box__mixed" dir="rtl">{_render_mixed_parts(_split_mixed_formula(numerator))}</span></span>'
                f'<span class="mx-den"><span class="formula-box__mixed" dir="rtl">{_render_mixed_parts(_split_mixed_formula(denominator))}</span></span>'
                "</span></span>"
            )
        else:
            pieces.append(f'<span class="formula-box__part formula-box__part--text">{_mixed_text_html(content)}</span>')
    return "".join(pieces)


def render_mixed_formula_parts(raw: str) -> str:
    """HTML for a formula row that mixes Persian text and math; empty when there is no math."""
    if "\\begin" in raw:
        return ""  # matrices and other environments keep the single typeset block
    parts = _split_mixed_formula(raw)
    # A lone Latin word beside Persian ("RAAS رنین: ...") is a word equation, not math.
    if not any(
        kind == "frac" or (kind == "math" and _MATH_TRIGGER_PATTERN.search(_ascii_math_to_latex(content) + "=" * ("[" in content)))
        for kind, content in parts
    ):
        return ""
    return _render_mixed_parts(parts)


_MATH_FUNCTIONS = ("sqrt", "sin", "cos", "tan", "log", "ln", "exp", "max", "min", "abs")
_MATH_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_^(){}\[\]+\-*/=<>.|\\,%]+$")
_MATH_TRIGGER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]_[A-Za-z0-9{]"
    r"|\^"
    r"|\bsqrt\("
    r"|\\[A-Za-z]+"
    r"|<=|>=|!="
    r"|="
)
_TRAILING_PUNCTUATION = ".,;:!?؛،؟"
_SINGLE_TOKEN_MATH_PATTERN = re.compile(r"sqrt\(|\\[A-Za-z]|_[A-Za-z0-9]{2,}")


def _balanced_group(source: str, start: int) -> int:
    depth = 0
    for cursor in range(start, len(source)):
        if source[cursor] == "(":
            depth += 1
        elif source[cursor] == ")":
            depth -= 1
            if depth == 0:
                return cursor
    return -1


def _ascii_math_to_latex(source: str) -> str:
    """Turn plain-ASCII math like sqrt((a-b)^2) or x_A1 into the LaTeX-lite the typesetter reads."""
    result: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("sqrt(", index) and (index == 0 or source[index - 1] != "\\"):
            end = _balanced_group(source, index + 4)
            if end != -1:
                result.append("\\sqrt{" + _ascii_math_to_latex(source[index + 5 : end]) + "}")
                index = end + 1
                continue
        char = source[index]
        if char in "^_" and index + 1 < len(source) and source[index + 1] != "{":
            if source[index + 1] == "(":
                end = _balanced_group(source, index + 1)
                if end != -1:
                    result.append(char + "{" + _ascii_math_to_latex(source[index + 2 : end]) + "}")
                    index = end + 1
                    continue
            # A digit run stops at the next letter ("c_1r_1^n" is c_1 * r_1^n); a letter run may carry digits ("x_A1").
            match = re.match(r"\d+|[A-Za-z]+\d*", source[index + 1 :])
            if match:
                result.append(char + "{" + match.group(0) + "}")
                index += 1 + len(match.group(0))
                continue
        result.append(char)
        index += 1
    text = "".join(result)
    text = re.sub(r"\s\*\s", " \\\\cdot ", text).replace("<=", "\\leq ").replace(">=", "\\geq ").replace("!=", "\\neq ")
    return text


def _is_math_token(token: str) -> bool:
    if not _MATH_TOKEN_PATTERN.match(token):
        return False
    letters_only = re.sub(r"[^A-Za-z]", "", token)
    if token == letters_only and len(token) > 2:
        return False
    return True


# "W1i= ..." / "d1 = ..." : a one-letter symbol with an index, written without an underscore and
# followed by a relation. Left as plain text it is reordered inside right-to-left prose.
_INDEXED_SYMBOL_RELATION = re.compile(r"(?<![A-Za-z0-9_{])([A-Za-z])(\d+[A-Za-z]?)\s*(<=|>=|!=|=|<|>|≤|≥)\s*")


# «X = b, a, c, b, b, a»: an assignment of a comma list is one expression; the plain-ASCII
# typesetter would stop at the first comma and leave the rest of the list to the bidi algorithm.
_ASSIGNED_LIST_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{0,2}\s*=\s*[A-Za-z0-9]{1,3}(?:\s*,\s*[A-Za-z0-9]{1,3}){1,}(?![A-Za-z0-9_])"
)


def _inline_math_html(text: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in _ASSIGNED_LIST_PATTERN.finditer(str(text or "")):
        output.append(_inline_math_html_core(text[cursor : match.start()]))
        latex = _ascii_math_to_latex(match.group(0))
        output.append(f'<span class="formula-inline" dir="ltr">{_typeset_formula(latex)}</span>')
        cursor = match.end()
    output.append(_inline_math_html_core(str(text or "")[cursor:]))
    return "".join(output)


def _inline_math_html_core(text: str) -> str:
    """Escape text, typesetting plain-ASCII equations (x_A1, sqrt(..), a^2, y = x) as inline math."""
    text = _INDEXED_SYMBOL_RELATION.sub(lambda m: f"{m.group(1)}_{{{m.group(2)}}} {m.group(3)} ", text)
    if not text or not re.search(r"[A-Za-z]", text) or not _MATH_TRIGGER_PATTERN.search(text):
        return escape(text)
    pieces = re.split(r"(\s+)", text)
    output: list[str] = []
    group: list[str] = []

    def flush() -> None:
        if not group:
            return
        raw = "".join(group)
        trailing = ""
        while raw and raw[-1] in _TRAILING_PUNCTUATION:
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        if (
            raw
            and _MATH_TRIGGER_PATTERN.search(raw)
            and raw.count("(") == raw.count(")")
            and (" " in raw or _SINGLE_TOKEN_MATH_PATTERN.search(raw))
        ):
            latex = _ascii_math_to_latex(raw)
            output.append(
                f'<span class="formula-inline" dir="ltr">{_typeset_formula(latex)}</span>'
                f"{escape(trailing)}"
            )
        else:
            output.append(escape(raw + trailing))
        group.clear()

    pending_space = ""
    for piece in pieces:
        if not piece:
            continue
        if piece.isspace():
            pending_space = piece
            continue
        token = piece.rstrip(_TRAILING_PUNCTUATION)
        is_math = _is_math_token(token) if token else False
        if is_math and (_MATH_TRIGGER_PATTERN.search(token) or group or re.fullmatch(r"[A-Za-z0-9.+\-*/()]+", token)):
            if group:
                group.append(pending_space)
            elif pending_space:
                output.append(escape(pending_space))
            group.append(piece)
            pending_space = ""
            if piece != token:
                flush()
            continue
        flush()
        if pending_space:
            output.append(escape(pending_space))
            pending_space = ""
        output.append(escape(piece))
    flush()
    if pending_space:
        output.append(escape(pending_space))
    return "".join(output)


def _html(value: Any) -> str:
    return validate_body_fragment(_strip_latex_delimiters(str(value)))


INLINE_MARKUP_PATTERN = re.compile(
    r"\*\*(.+?)\*\*"
    r"|\[\[(key|label|slide|added):(.*?)\]\]"
    r"|(?<!\S)((?:نکته|مثال)\s*[:：])"
    r"|`([^`\n]+)`"
)


# Generated note text spells out math/CS notation in plain ASCII -- things
# like "T(n/2)", "2^{i-1}", "t_i", "n!" -- which is hard to read as a wall
# of underscores and carets inside Persian prose. These patterns recognize
# that notation (run AFTER html.escape(), since none of the characters they
# match are altered by escaping) and turn it into real <sup>/<sub> and a
# visually distinct, LTR-safe span so it doesn't get bidi-reordered inside
# right-to-left paragraph text.
_MATH_FUNC_NAME_ALTERNATION = (
    r"(?:T|f|g|h|F|O|o|N|S|P|Q|R|D|E|log|ln|exp|sin|cos|tan|lim)"
)
_MATH_FUNC_CALL_PATTERN = re.compile(
    rf"\b({_MATH_FUNC_NAME_ALTERNATION})\(([^()\n]{{1,30}})\)"
)
_MATH_FACTORIAL_PATTERN = re.compile(
    r"(\([0-9A-Za-z/*+\-^_{} ]{1,12}\)|[A-Za-z0-9]+)!(?!\w)"
)
_MATH_SUP_BRACED_PATTERN = re.compile(r"([A-Za-z0-9])\^\{([^{}]{1,12})\}")
_MATH_SUP_BARE_PATTERN = re.compile(r"([A-Za-z0-9])\^(-?[A-Za-z0-9])(?!\w)")
_MATH_SUB_BRACED_PATTERN = re.compile(r"([A-Za-z])_\{([^{}]{1,12})\}")
_MATH_SUB_BARE_PATTERN = re.compile(r"([A-Za-z])_(-?[A-Za-z0-9])(?!\w)")


def _format_sup_sub_plain(text: str) -> str:
    text = _MATH_SUP_BRACED_PATTERN.sub(r"\1<sup>\2</sup>", text)
    text = _MATH_SUP_BARE_PATTERN.sub(r"\1<sup>\2</sup>", text)
    text = _MATH_SUB_BRACED_PATTERN.sub(r"\1<sub>\2</sub>", text)
    text = _MATH_SUB_BARE_PATTERN.sub(r"\1<sub>\2</sub>", text)
    return text


def _format_inline_math(text: str) -> str:
    # Function calls and factorials are handled first and their own inner
    # content is formatted for sup/sub too, so e.g. "T(2^i)" becomes one
    # highlighted span with a real superscript inside it, rather than a
    # function-call span followed by a separately-wrapped superscript.
    def _func_repl(match: re.Match[str]) -> str:
        name, args = match.group(1), match.group(2)
        return f'<span class="inline-math" dir="ltr">{name}({_format_sup_sub_plain(args)})</span>'

    def _factorial_repl(match: re.Match[str]) -> str:
        return f'<span class="inline-math" dir="ltr">{_format_sup_sub_plain(match.group(0))}</span>'

    def _sup_repl(match: re.Match[str]) -> str:
        base, exponent = match.group(1), match.group(2)
        return f'<span class="inline-math" dir="ltr">{base}<sup>{exponent}</sup></span>'

    def _sub_repl(match: re.Match[str]) -> str:
        base, subscript = match.group(1), match.group(2)
        return f'<span class="inline-math" dir="ltr">{base}<sub>{subscript}</sub></span>'

    text = _MATH_FUNC_CALL_PATTERN.sub(_func_repl, text)
    text = _MATH_FACTORIAL_PATTERN.sub(_factorial_repl, text)
    text = _MATH_SUP_BRACED_PATTERN.sub(_sup_repl, text)
    text = _MATH_SUP_BARE_PATTERN.sub(_sup_repl, text)
    text = _MATH_SUB_BRACED_PATTERN.sub(_sub_repl, text)
    text = _MATH_SUB_BARE_PATTERN.sub(_sub_repl, text)
    return text


_LATEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι",
    "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "int": "∫", "iint": "∬", "oint": "∮", "sum": "∑", "prod": "∏",
    "infty": "∞", "partial": "∂", "nabla": "∇", "cdot": "·", "times": "×",
    "div": "÷", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤", "ge": "≥",
    "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡",
    "sim": "∼", "propto": "∝", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "in": "∈", "notin": "∉",
    "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "ldots": "…", "cdots": "⋯", "dots": "…",
    "circ": "∘", "ll": "≪", "gg": "≫", "angle": "∠", "degree": "°",
}
_LATEX_FUNCTION_NAMES = frozenset(
    {"log", "ln", "exp", "sin", "cos", "tan", "cot", "sec", "csc", "lim",
     "max", "min", "sup", "inf", "arg", "det", "gcd", "mod", "sinh", "cosh",
     "tanh", "arcsin", "arccos", "arctan"}
)
_LATEX_SPACE_COMMANDS = {",": " ", ";": " ", ":": " ", " ": " ", "!": ""}
_LATEX_DROPPED_COMMANDS = frozenset({"left", "right", "big", "Big", "displaystyle", "limits"})
_LATEX_TEXT_COMMANDS = frozenset({"text", "mathrm", "textbf", "mathbf", "operatorname", "mathit"})
_LATEX_BIG_OPERATORS = frozenset({"∫", "∬", "∮", "∑", "∏"})

_MATH_RUN_ATOM = r"(?:\\[A-Za-z]+|\\[,;:!]|[A-Za-z0-9{}^_()|'!]|(?<=\d)\.(?=\d))"
_MATH_RUN_PATTERN = re.compile(
    rf"{_MATH_RUN_ATOM}+(?:(?:\s?[=+\-*/<>]\s?|\s(?=[A-Za-z0-9(]*[\\^_]))(?:{_MATH_RUN_ATOM})+)*"
)
_LATEX_COMMAND_IN_RUN = re.compile(r"\\[A-Za-z]{2,}")


def _latex_to_html(src: str) -> str:
    return typeset_math(src)


# A Latin run inside Persian prose: "X = b, a, c", "Longest Increasing Subsequence", "DP[i]". Left
# to the bidi algorithm, the commas, equals signs and brackets around it reorder against the
# right-to-left text ("b, a" reads "a, b"), so each run becomes its own left-to-right island.
_LATIN_TOKEN = r"[A-Za-z][A-Za-z0-9_'’]*(?:\[[^\]\s<>&]{1,14}\])*"
_LATIN_RUN_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])"
    rf"(?:\(\s*(?P<paren>{_LATIN_TOKEN}(?:(?:\s*[,=<>+*/|:\-]\s*|\s+)(?:{_LATIN_TOKEN}|\d+(?:\.\d+)?))*)\s*\)"
    rf"|(?P<run>{_LATIN_TOKEN}(?:(?:\s*[,=<>+*/|:\-]\s*|\s+)(?:{_LATIN_TOKEN}|\d+(?:\.\d+)?))*))"
)
_HTML_TAG_SPLIT = re.compile(r"(<[^>]+>)")


def _isolate_latin_runs(html: str) -> str:
    """Wrap Latin runs in plain text nodes of already-escaped HTML in a left-to-right isolate.

    Text already inside a left-to-right span (typeset math) is left alone.
    """
    if not re.search(r"[A-Za-z]", html) or not _ARABIC_SCRIPT_PATTERN.search(html):
        return html
    output: list[str] = []
    stack: list[bool] = []  # per open <span>: does it set dir="ltr"?
    for piece in _HTML_TAG_SPLIT.split(html):
        if not piece:
            continue
        if piece.startswith("<"):
            if re.match(r"<span\b", piece):
                stack.append('dir="ltr"' in piece)
            elif piece.startswith("</span"):
                if stack:
                    stack.pop()
            output.append(piece)
            continue
        if any(stack):
            output.append(piece)
            continue

        def wrap(match: re.Match[str], text: str = piece) -> str:
            if match.group("paren") is not None:
                return f'<span class="latin-run" dir="ltr">({match.group("paren").strip()})</span>'
            run = match.group("run")
            # A lone word between Persian words reads fine; one glued to punctuation ("... bab.")
            # or one with an index or a list needs its own island.
            simple = re.fullmatch(r"[A-Za-z0-9_'’]+", run) is not None
            if simple and not re.match(r"[.,;:!?،؛؟)]", text[match.end() : match.end() + 1]):
                return run
            return f'<span class="latin-run" dir="ltr">{run}</span>'

        output.append(_LATIN_RUN_PATTERN.sub(wrap, piece))
    return "".join(output)


def _format_latex_runs(text: str) -> str:
    """Escape ``text``, rendering undelimited LaTeX-style runs as clean math.

    Runs without a backslash command are left to the older plain-ASCII
    formatter; only runs the model clearly wrote as LaTeX are converted.
    """
    parts: list[str] = []
    last = 0
    for match in _MATH_RUN_PATTERN.finditer(text):
        run = match.group(0)
        start = match.start()
        lead = re.match(r"(?:[A-Za-z]{2,}\s+)+", run)
        if lead:
            run, start = run[lead.end() :], start + lead.end()
        run = re.sub(r"(?:\s+[A-Za-z]{2,})+$", "", run)
        if not _LATEX_COMMAND_IN_RUN.search(run):
            continue
        parts.append(_isolate_latin_runs(_format_inline_math(_inline_math_html(text[last:start]))))
        parts.append(
            f'<span class="inline-math inline-math--latex" dir="ltr">{_latex_to_html(run)}</span>'
        )
        last = start + len(run)
    parts.append(_isolate_latin_runs(_format_inline_math(_inline_math_html(text[last:]))))
    return "".join(parts)


def _escape_with_math(value: Any) -> str:
    return _format_latex_runs(str(value or ""))


SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[\.\!\?؟])\s+|\n{2,}")


RENDER_LABELS = {
    "fa": {
        "page": "صفحه",
        "session": "جلسه",
        "slide": "اسلاید",
        "solution": "▼ راه‌حل:",
        "note_title": "نکته",
        "example_title": "مثال",
        "unrelated_discussion_title": "گفت\u200cوگوی خارج از موضوع درس",
        "summary_title": "جمع‌بندی",
        "figure": "شکل",
        "table": "جدول",
        "fallback_block": "این بخش به‌صورت خودکار از یک بلوک ناسازگار تبدیل شد.",
        "topics_covered_title": "موضوعات این جزوه",
        "other_topics_title": "سایر موارد مطرح‌شده",
        "exam_important_title": "مهم برای امتحان",
        "exam_excluded_title": "خارج از امتحان",
        "ai_added_tag": "افزودهٔ تسانت",
        "key_idea_title": "یادت بماند",
        "section_intro_label": "در این بخش",
    },
    "international": {
        "page": "Page",
        "session": "Session",
        "slide": "Slide",
        "solution": "▼ Solution:",
        "note_title": "Note",
        "example_title": "Example",
        "unrelated_discussion_title": "Off-topic class discussion",
        "summary_title": "Summary",
        "figure": "Figure",
        "table": "Table",
        "fallback_block": "This section was automatically converted from an incompatible block.",
        "topics_covered_title": "Topics Covered",
        "other_topics_title": "Other Topics Discussed",
        "exam_important_title": "Important for the exam",
        "exam_excluded_title": "Not on the exam",
        "ai_added_tag": "Added by TesaNote",
        "key_idea_title": "Remember",
        "section_intro_label": "In this section",
    },
}


def _labels_for_language(language_code: str) -> dict[str, str]:
    if str(language_code or "fa").lower() == "fa":
        return RENDER_LABELS["fa"]
    return RENDER_LABELS["international"]


def semantic_labels_for_language(language_code: str) -> dict[str, str]:
    return dict(_labels_for_language(language_code))


def _rich_text(block: dict[str, Any], field: str, *, labels: dict[str, str] | None = None) -> str:
    html_field = f"{field}_html"
    if block.get(html_field):
        return _html(block.get(html_field, ""))
    return _inline_rich_text(block.get(field, ""), labels=labels)


def _join_classes(*values: str | None) -> str:
    return " ".join(part for part in values if part)


_INLINE_BULLET_PATTERN = re.compile(r"(?:(?<=^)|(?<=[:：.؛;؟?!)\]]))\s*\*\s+(?=\S)")
_INLINE_NUMBER_PATTERN = re.compile(r"(?:(?<=^)|(?<=[:：.؛;؟?!)\]]))\s*(\d{1,2})[.)]\s+(?=\S)")


def _split_inline_list(text: str) -> tuple[str, str, list[str]]:
    """Split flattened markdown-ish enumerations into (lead, kind, items).

    Generated text is whitespace-collapsed before it reaches the renderer, so
    "* item" bullets and "1. item" numbering end up inline in one paragraph.
    Numbered markers only count as a list when they run 1, 2, 3, ... in order
    so ordinary numbers in a sentence are never mistaken for list markers.
    """
    bullets = list(_INLINE_BULLET_PATTERN.finditer(text))
    if bullets:
        lead = text[: bullets[0].start()].strip()
        ends = [m.start() for m in bullets[1:]] + [len(text)]
        items = [text[m.end() : end].strip() for m, end in zip(bullets, ends)]
        return lead, "bullet", [item for item in items if item]
    markers: list[re.Match[str]] = []
    expected = 1
    for match in _INLINE_NUMBER_PATTERN.finditer(text):
        if int(match.group(1)) == expected:
            markers.append(match)
            expected += 1
    if len(markers) >= 2:
        lead = text[: markers[0].start()].strip()
        ends = [m.start() for m in markers[1:]] + [len(text)]
        items = [text[m.end() : end].strip() for m, end in zip(markers, ends)]
        return lead, "number", items
    return text, "", []


def _inline_code(token: str) -> str:
    escaped = escape(token)
    escaped = re.sub(r"([A-Za-z0-9\)])_([A-Za-z0-9]+)", r"\1<sub>\2</sub>", escaped)
    escaped = re.sub(r"([A-Za-z0-9\)])\^([A-Za-z0-9]+)", r"\1<sup>\2</sup>", escaped)
    return f'<span class="inline-math" dir="ltr">{escaped}</span>'


def _inline_rich_text(value: Any, *, labels: dict[str, str] | None = None) -> str:
    labels = labels or RENDER_LABELS["fa"]
    text, math_items = stash_math(str(value or ""))
    if not text.strip():
        return ""
    lead, kind, items = _split_inline_list(text)
    if kind and items:
        parts = [_inline_segment(lead, labels)] if lead else []
        for index, item in enumerate(items, start=1):
            mark = "•" if kind == "bullet" else f"{index}."
            parts.append(
                f'<span class="inline-item inline-item--{kind}">'
                f'<span class="inline-item__mark">{mark}</span>'
                f'<span class="inline-item__text">{_inline_segment(item, labels)}</span></span>'
            )
        return restore_math("".join(parts), math_items)
    return restore_math(_inline_segment(text, labels), math_items)


def _inline_segment(text: str, labels: dict[str, str]) -> str:
    rendered: list[str] = []
    last_index = 0
    for match in INLINE_MARKUP_PATTERN.finditer(text):
        rendered.append(_escape_with_math(text[last_index:match.start()]))
        if match.group(1) is not None:
            rendered.append(f"<strong>{_escape_with_math(match.group(1))}</strong>")
        elif match.group(2) == "key":
            key_text = _escape_with_math(match.group(3) or "")
            rendered.append(f'<span class="inline-emphasis inline-emphasis--key">{key_text}</span>')
        elif match.group(2) == "label":
            label_text = _escape_with_math(match.group(3) or "")
            rendered.append(
                f'<span class="inline-emphasis inline-emphasis--label">{label_text}</span>'
            )
        elif match.group(2) == "added":
            rendered.append(
                f'<span class="ai-corrected">{_escape_with_math(match.group(3) or "")}</span> '
                f'<span class="ai-tag">{escape(labels["ai_added_tag"])}</span>'
            )
        elif match.group(2) == "slide":
            rendered.append(
                f'<span class="ref-note ref-note--slide">{escape(labels["slide"])} {escape(match.group(3) or "")}</span>'
            )
        elif match.group(4) is not None:
            rendered.append(f'<span class="inline-emphasis inline-emphasis--label">{escape(match.group(4))}</span>')
        elif match.group(5) is not None:
            rendered.append(_inline_code(match.group(5)))
        last_index = match.end()
    rendered.append(_escape_with_math(text[last_index:]))
    return "".join(rendered)


def _normalized_text_item(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "content", "title", "label", "value"):
            candidate = str(item.get(key, "") or "").strip()
            if candidate:
                return candidate
        joined = " ".join(str(value).strip() for value in item.values() if str(value).strip())
        return joined.strip()
    return str(item or "").strip()


class SemanticNoteRenderer:
    """Renders compact semantic note blocks into the existing HTML design system."""

    page_capacity_score = 1640
    target_page_fill_ratio = 0.84
    minimum_page_fill_ratio = 0.68
    paragraph_split_target_chars = 520
    compact_paragraph_split_target_chars = 430
    list_split_target_chars = 620
    list_split_max_items = 4
    table_split_max_rows = 8

    def __init__(self, *, language_code: str = "fa") -> None:
        self.language_code = str(language_code or "fa").lower()
        self.labels = _labels_for_language(self.language_code)

    @classmethod
    def supported_block_types(cls) -> tuple[str, ...]:
        return SUPPORTED_BLOCK_TYPES

    def render_document(self, document: dict[str, Any]) -> str:
        pages = document.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("Semantic note document must contain a non-empty 'pages' array.")

        repaginated_pages = self._repaginate_pages(pages)
        rendered_pages = [self.render_page(page, index=index) for index, page in enumerate(repaginated_pages, start=1)]
        # The "whole class" text is drawn as a plain reading page (see .template-wrapper--transcript).
        wrapper = "template-wrapper template-wrapper--transcript" if document.get("document_kind") == "transcript" else "template-wrapper"
        return f'<div class="{wrapper}">\n' + "\n".join(rendered_pages) + "\n</div>"

    def render_page(self, page: dict[str, Any], *, index: int) -> str:
        header = self.render_page_header(page.get("header", {}))
        footer = self.render_page_footer(page.get("footer", {"page_number": self._page_number(index)}))
        blocks = page.get("blocks", [])
        if not isinstance(blocks, list):
            raise ValueError("Page 'blocks' must be an array.")
        content = "\n".join(self._wrap_block_html(block, self.render_block(block)) for block in blocks)
        return (
            '<div class="a4-page">\n'
            f"{header}\n"
            '    <main class="page-content">\n'
            f"{self._indent(content, 8)}\n"
            "    </main>\n"
            f"{footer}\n"
            "</div>"
        )

    def _wrap_block_html(self, block: dict[str, Any], html: str) -> str:
        block_type = _attr(str(block.get("type", "") or "unknown"))
        anchor_id = str(block.get("anchor_id", "") or "").strip()
        id_attr = f' id="{_attr(anchor_id)}"' if anchor_id else ""
        return (
            f'<div class="note-block" data-block-type="{block_type}"{id_attr}>\n'
            f"{self._indent(html, 4)}\n</div>"
        )

    def render_block(self, block: dict[str, Any]) -> str:
        if not isinstance(block, dict):
            raise ValueError("Each semantic block must be an object.")
        block_type = str(block.get("type", "")).strip()
        if not block_type:
            raise ValueError("Each semantic block must include a type.")
        renderer = getattr(self, f"_render_{block_type.replace('-', '_')}", None)
        if renderer is None:
            return self._render_unknown_block(block)
        return renderer(block)

    def _render_unknown_block(self, block: dict[str, Any]) -> str:
        items = block.get("items")
        if isinstance(items, list) and items:
            return self._render_list(
                {
                    "type": "list",
                    "title": block.get("title", ""),
                    "items": items,
                    "ordered": bool(block.get("ordered")),
                }
            )

        content_parts = [
            str(block.get("title", "") or "").strip(),
            str(block.get("content", "") or "").strip(),
            str(block.get("text", "") or "").strip(),
            str(block.get("caption", "") or "").strip(),
            str(block.get("formula", "") or "").strip(),
        ]
        content = " ".join(part for part in content_parts if part).strip()
        if not content:
            content = self.labels["fallback_block"]
        return self._render_paragraph(
            {
                "type": "paragraph",
                "variants": ["compact"],
                "content": content,
            }
        )

    def render_page_header(self, data: dict[str, Any]) -> str:
        course_name = _text(data.get("course_name", ""))
        course_detail = _text(data.get("course_detail", ""))
        lecture_badge = data.get("lecture_badge")
        session_number = _text(data.get("session_number", "")).strip()
        if session_number and self.language_code == "fa":
            session_number = session_number.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        session_html = (
            f'            <span class="page-header__session">{self.labels["session"]} {session_number}</span>\n'
            if session_number
            else ""
        )
        detail_html = f'\n            <span class="page-header__course-detail">{course_detail}</span>' if course_detail else ""
        badge_html = (
            f'\n            <span class="page-header__lecture-badge">{_text(lecture_badge)}</span>'
            if lecture_badge
            else ""
        )
        return (
            "    <header class=\"page-header\">\n"
            "        <div class=\"page-header__brand\">\n"
            "            <img class=\"page-header__brand-logo\" src=\"{{THEME_LOGO_SRC}}\" alt=\"Tesanote\" />\n"
            "        </div>\n"
            "        <div class=\"page-header__meta\">\n"
            f"{session_html}"
            f"            <span class=\"page-header__course-name\">{course_name}</span>\n"
            f"{detail_html}{badge_html}\n"
            "        </div>\n"
            "    </header>"
        )

    def render_page_footer(self, data: dict[str, Any]) -> str:
        page_number = _text(data.get("page_number", ""))
        page_label, page_index = self._split_page_number(page_number)
        watermark = str(data.get("watermark", "") or "").strip()
        watermark_html = (
            f'\n        <span class="page-footer__brand-watermark">{_text(watermark)}</span>'
            if watermark
            else ""
        )
        if page_label and page_index:
            page_number_html = (
                f'<span class="page-footer__page-number" data-page-label="{_attr(page_label)}" '
                f'data-page-index="{_attr(page_index)}" aria-label="{_attr(page_number)}">'
                f'<span class="page-footer__page-label">{page_label}</span> '
                f'<span class="page-footer__page-index" dir="ltr">{page_index}</span>'
                "</span>"
            )
        else:
            page_number_html = (
                f'<span class="page-footer__page-number" aria-label="{_attr(page_number)}">{page_number}</span>'
            )
        return (
            "    <footer class=\"page-footer\">\n"
            f"        {page_number_html}{watermark_html}\n"
            "        <span class=\"page-footer__made-with\">"
            "<span class=\"page-footer__made-with-text\">Made with</span>"
            "<span class=\"page-footer__made-with-brand\">TesaNote</span>"
            "</span>\n"
            "    </footer>"
        )

    def _render_chapter_header(self, block: dict[str, Any]) -> str:
        subtitle = block.get("subtitle")
        subtitle_html = (
            f'\n    <p class="chapter-header__subtitle">{self._rich_text(block, "subtitle")}</p>'
            if subtitle
            else ""
        )
        slide_name = str(block.get("slide_name", "") or "").strip()
        slide_html = (
            '\n    <p class="chapter-header__source">'
            f'<span class="chapter-header__source-label">{escape(self.labels["slide"])}</span>'
            f'<span class="chapter-header__source-name" dir="auto">{escape(slide_name)}</span></p>'
            if slide_name
            else ""
        )
        return (
            '<div class="chapter-header">\n'
            f'    <span class="chapter-header__number">{_text(block.get("number", ""))}</span>\n'
            f'    <h1 class="chapter-header__title">{self._rich_text(block, "title")}</h1>{subtitle_html}{slide_html}\n'
            '    <div class="chapter-header__divider"></div>\n'
            "</div>"
        )

    def _render_section_header(self, block: dict[str, Any]) -> str:
        show_dot = block.get("accent_dot", True)
        dot_html = '\n    <span class="section-header__accent-dot"></span>' if show_dot else ""
        return (
            '<div class="section-header">\n'
            f'    <span class="section-header__number">{_text(block.get("number", ""))}</span>\n'
            f'    <h2 class="section-header__title">{self._rich_text(block, "title")}</h2>{dot_html}\n'
            "</div>"
        )

    def _render_subsection_header(self, block: dict[str, Any]) -> str:
        number = str(block.get("number", "") or "").strip()
        number_html = f'<span class="subsection-header__number">{_text(number)}</span> ' if number else ""
        return f'<h3 class="subsection-header">{number_html}{self._rich_text(block, "title")}</h3>'

    def _render_paragraph(self, block: dict[str, Any]) -> str:
        variants = block.get("variants", [])
        classes = ["paragraph"]
        if isinstance(variants, list):
            classes.extend(
                f"paragraph--{_attr(variant)}"
                for variant in variants
                if variant in {"lead", "compact", "ai-added", "section-intro", "transcript"}
            )
        extra_classes = block.get("class_name")
        if isinstance(extra_classes, str) and extra_classes.strip():
            classes.append(extra_classes.strip())
        content_html = self._rich_text(block, "content")
        if isinstance(variants, list) and "ai-added" in variants:
            content_html = f'<span class="ai-tag">{_text(self.labels["ai_added_tag"])}</span> {content_html}'
        time_label = str(block.get("time_label", "") or "").strip()
        if time_label:
            content_html = f'<span class="paragraph__time">{_text(time_label)}</span>{content_html}'
        return f'<p class="{_attr(" ".join(classes))}">{content_html}</p>'

    def _render_formula_box(self, block: dict[str, Any]) -> str:
        numbered = bool(block.get("label"))
        display_mode = str(block.get("display_mode", "") or "").strip()
        classes = _join_classes(
            "formula-box",
            "formula-box--numbered" if numbered else None,
            "formula-box--query" if display_mode == "query" else None,
        )
        label_html = (
            f'\n    <span class="formula-box__label">{_text(block.get("label", ""))}</span>'
            if numbered
            else ""
        )
        raw_formula = repair_latex_escapes(_strip_latex_delimiters(str(block.get("formula", "") or ""))).strip()
        source_attr = ""
        mixed_html = (
            render_mixed_formula_parts(raw_formula)
            if display_mode != "query" and _ARABIC_SCRIPT_PATTERN.search(raw_formula)
            else ""
        )
        if display_mode == "query":
            formula_html = f'    <pre class="formula-box__query-text">{escape(raw_formula)}</pre>'
        elif mixed_html:
            classes = _join_classes(classes, "formula-box--mixed")
            source_attr = f' data-formula-source="{_attr(raw_formula)}"'
            formula_html = f'    <span class="formula-box__mixed" dir="rtl">{mixed_html}</span>'
        elif _FORMULA_MARKUP_PATTERN.search(_ascii_math_to_latex(raw_formula)):
            raw_formula = _ascii_math_to_latex(raw_formula)
            source_attr = f' data-formula-source="{_attr(raw_formula)}"'
            formula_html = (
                f'    <span class="formula-box__math" dir="ltr" aria-label="{_attr(raw_formula)}">'
                f"{_typeset_formula(raw_formula)}</span>"
            )
        else:
            formula_html = f"    {escape(raw_formula)}"
            if _ARABIC_SCRIPT_PATTERN.search(raw_formula):
                classes = _join_classes(classes, "formula-box--text")
        return (
            f'<div class="{_attr(classes)}" role="math"{source_attr}>\n'
            f"{formula_html}{label_html}\n"
            "</div>"
        )

    def _render_code_block(self, block: dict[str, Any]) -> str:
        return render_code_block_html(block)

    def _render_example_box(self, block: dict[str, Any]) -> str:
        solution_html = ""
        if re.search(r"\w", str(block.get("solution") or "")):  # a solution box needs real content, not just a label
            solution_html = (
                f'\n        <span class="example-box__solution-label">{_text(self.labels["solution"])}</span>\n'
                f'        <p class="paragraph paragraph--compact u-mt-sm">{self._rich_text(block, "solution")}</p>'
            )
        ai_added = block.get("origin") == "ai"
        ai_tag_html = (
            f' <span class="ai-tag">{_text(self.labels["ai_added_tag"])}</span>' if ai_added else ""
        )
        return (
            f'<div class="{"example-box example-box--ai" if ai_added else "example-box"}">\n'
            '    <div class="example-box__header">\n'
            '        <span class="example-box__icon"><span class="example-box__icon-inner"></span></span>\n'
            f'        <span class="example-box__title">{self._rich_text(block, "title")}{ai_tag_html}</span>\n'
            "    </div>\n"
            '    <div class="example-box__content">\n'
            f'        <p class="paragraph paragraph--compact">{self._rich_text(block, "content")}</p>{solution_html}\n'
            "    </div>\n"
            "</div>"
        )

    def _render_unrelated_discussion_box(self, block: dict[str, Any]) -> str:
        title = block.get("title") or self.labels["unrelated_discussion_title"]
        return (
            '<aside class="unrelated-discussion-box">\n'
            '    <div class="unrelated-discussion-box__header">\n'
            '        <span class="unrelated-discussion-box__icon" aria-hidden="true"></span>\n'
            f'        <span class="unrelated-discussion-box__title">{self._inline_rich_text(title)}</span>\n'
            "    </div>\n"
            '    <div class="unrelated-discussion-box__content">\n'
            f'        <p class="paragraph paragraph--compact">{self._rich_text(block, "content")}</p>\n'
            "    </div>\n"
            "</aside>"
        )

    def _render_note_box(self, block: dict[str, Any]) -> str:
        return (
            '<div class="note-box">\n'
            '    <div class="note-box__header">\n'
            '        <span class="note-box__icon"><span class="note-box__icon-mark">!</span></span>\n'
            f'        <span class="note-box__title">{self._rich_text(block, "title")}</span>\n'
            "    </div>\n"
            '    <div class="note-box__content">\n'
            f'        <p class="paragraph paragraph--compact">{self._rich_text(block, "content")}</p>\n'
            "    </div>\n"
            "</div>"
        )

    def _render_summary_box(self, block: dict[str, Any]) -> str:
        items = block.get("items", [])
        if not isinstance(items, list):
            raise ValueError("summary-box items must be an array.")
        list_html = "\n".join(
            f'            <li class="summary-box__list-item">{self._inline_rich_text(item)}</li>' for item in items
        )
        footer_html = (
            f'\n        <p class="paragraph paragraph--compact u-mt-sm">{self._rich_text(block, "content")}</p>'
            if block.get("content")
            else ""
        )
        return (
            '<div class="summary-box">\n'
            '    <div class="summary-box__header">\n'
            f'        <span class="summary-box__icon">{_text(block.get("icon", "📋"))}</span>\n'
            f'        <span class="summary-box__title">{self._rich_text(block, "title")}</span>\n'
            "    </div>\n"
            '    <div class="summary-box__content">\n'
            '        <ul class="summary-box__list">\n'
            f"{list_html}\n"
            f"        </ul>{footer_html}\n"
            "    </div>\n"
            "</div>"
        )

    def _render_exam_point(self, block: dict[str, Any]) -> str:
        badge = block.get("badge")
        badge_html = f'\n        <span class="exam-point__badge">{_text(badge)}</span>' if badge else ""
        status = str(block.get("status", "") or "")
        variant = f" exam-point--{status}" if status in {"important", "excluded"} else ""
        return (
            f'<div class="exam-point{variant}">\n'
            '    <div class="exam-point__header">\n'
            f'        <span class="exam-point__icon">{_text(block.get("icon", "🎯"))}</span>\n'
            f'        <span class="exam-point__title">{self._rich_text(block, "title")}</span>{badge_html}\n'
            "    </div>\n"
            f'    <div class="exam-point__content">{self._rich_text(block, "content")}</div>\n'
            "</div>"
        )

    def _render_table_container(self, block: dict[str, Any]) -> str:
        headers = block.get("headers", [])
        rows = block.get("rows", [])
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise ValueError("table-container headers and rows must be arrays.")
        header_html = "".join(f"<th>{_text(cell)}</th>" for cell in headers)
        row_html = "\n".join(
            "                <tr>"
            + "".join(f"<td>{_text(cell)}</td>" for cell in row)
            + "</tr>"
            for row in rows
        )
        return (
            '<div class="table-container">\n'
            f'    <div class="table-container__caption">{self._rich_text(block, "caption")}</div>\n'
            "    <table>\n"
            f"        <thead><tr>{header_html}</tr></thead>\n"
            "        <tbody>\n"
            f"{row_html}\n"
            "        </tbody>\n"
            "    </table>\n"
            "</div>"
        )

    def _render_list(self, block: dict[str, Any]) -> str:
        items = block.get("items", [])
        if not isinstance(items, list):
            raise ValueError("list items must be an array.")
        cleaned_items = [_normalized_text_item(item) for item in items]
        cleaned_items = [item for item in cleaned_items if item]
        if not cleaned_items:
            return ""

        ordered = bool(block.get("ordered"))
        list_tag = "ol" if ordered else "ul"
        title = str(block.get("title", "") or "").strip()
        title_html = f'\n    <div class="list-block__title">{self._inline_rich_text(title)}</div>' if title else ""
        item_html = "\n".join(
            f'        <li class="list-block__item">{self._inline_rich_text(item)}</li>' for item in cleaned_items
        )
        return (
            '<div class="list-block">\n'
            f"{title_html}\n"
            f'    <{list_tag} class="list-block__items{" list-block__items--ordered" if ordered else ""}">\n'
            f"{item_html}\n"
            f"    </{list_tag}>\n"
            "</div>"
        )

    def _render_table_of_contents(self, block: dict[str, Any]) -> str:
        raw_items = block.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("table-of-contents items must be an array.")
        cleaned_items: list[tuple[str, str]] = []
        for raw_item in raw_items:
            anchor = ""
            if isinstance(raw_item, dict):
                anchor = _attr(str(raw_item.get("anchor", "") or "").strip())
            label = _normalized_text_item(raw_item)
            if label and not _is_unrelated_discussion_label(label):
                cleaned_items.append((label, anchor))
        if not cleaned_items:
            return ""

        title = str(block.get("title", "") or self.labels.get("topics_covered_title", "")).strip()
        title_html = (
            f'\n    <div class="table-of-contents__title">{self._inline_rich_text(title)}</div>'
            if title
            else ""
        )
        item_rows = []
        for index, (label, anchor) in enumerate(cleaned_items, start=1):
            label_html = self._inline_rich_text(label)
            if anchor:
                label_html = (
                    f'<a class="table-of-contents__link" href="#{anchor}" '
                    f'data-topic-anchor="{anchor}">{label_html}</a>'
                )
            item_rows.append(
                "        <li class=\"table-of-contents__item\">"
                f'<span class="table-of-contents__index">{index}</span>'
                f'<span class="table-of-contents__label">{label_html}</span>'
                "</li>"
            )
        item_html = "\n".join(item_rows)
        return (
            '<div class="table-of-contents">\n'
            f"{title_html}\n"
            '    <ol class="table-of-contents__items">\n'
            f"{item_html}\n"
            "    </ol>\n"
            "</div>"
        )

    def _render_figure_reference(self, block: dict[str, Any]) -> str:
        if not block.get("image_src"):
            caption = self._rich_text(block, "caption")
            number = _text(block.get("number", "") or str(block.get("label", "") or ""))
            if not caption and not number:
                return ""
            return (
                '<div class="figure-reference figure-reference--compact">\n'
                '    <div class="figure-reference__caption">\n'
                f'        <span class="figure-reference__number">{number}</span> {caption}\n'
                "    </div>\n"
                "</div>"
            )
        media_html = (
            f'<img src="{_attr(block.get("image_src"))}" alt="{_attr(block.get("image_alt", ""))}" />'
            if block.get("image_src")
            else ""
        )
        return (
            '<div class="figure-reference">\n'
            '    <div class="figure-reference__image-placeholder">\n'
            f"        {media_html}\n"
            "    </div>\n"
            '    <div class="figure-reference__caption">\n'
            f'        <span class="figure-reference__number">{_text(block.get("number", ""))}</span> {self._rich_text(block, "caption")}\n'
            "    </div>\n"
            "</div>"
        )

    def _render_key_highlight(self, block: dict[str, Any]) -> str:
        title = str(block.get("title", "") or "").strip()
        if not title:
            return f'<div class="key-highlight">{self._rich_text(block, "content")}</div>'
        return (
            '<div class="key-highlight key-highlight--takeaway">'
            f'<span class="key-highlight__label">{_text(title)}</span>'
            f'{self._rich_text(block, "content")}</div>'
        )

    def _render_page_header(self, block: dict[str, Any]) -> str:
        return self.render_page_header(block)

    def _render_page_footer(self, block: dict[str, Any]) -> str:
        return self.render_page_footer(block)

    def _render_figure_reference_inline(self, block: dict[str, Any]) -> str:
        return self._render_figure_reference(block)

    @staticmethod
    def _indent(value: str, count: int) -> str:
        prefix = " " * count
        return "\n".join(f"{prefix}{line}" if line else line for line in value.splitlines())

    def _rich_text(self, block: dict[str, Any], field: str) -> str:
        return _rich_text(block, field, labels=self.labels)

    def _inline_rich_text(self, value: Any) -> str:
        return _inline_rich_text(value, labels=self.labels)

    def _page_number(self, index: int) -> str:
        return f"{self.labels['page']} {index}"

    def _repaginate_pages(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        header_template: dict[str, Any] = {}
        footer_template: dict[str, Any] = {}
        blocks: list[dict[str, Any]] = []

        for page in pages:
            if not isinstance(page, dict):
                continue
            header = page.get("header")
            footer = page.get("footer")
            if isinstance(header, dict) and not header_template:
                header_template = dict(header)
            if isinstance(footer, dict) and not footer_template:
                footer_template = dict(footer)
            page_blocks = page.get("blocks", [])
            if not isinstance(page_blocks, list):
                continue
            for block in page_blocks:
                normalized = self._prepare_block_for_pagination(block)
                if normalized:
                    blocks.append(normalized)

        if not blocks:
            return [
                {
                    "header": header_template,
                    "blocks": [],
                    "footer": {"page_number": self._page_number(1), "watermark": ""},
                }
            ]

        blocks = self._consolidate_summary_boxes(blocks)
        split_blocks: list[dict[str, Any]] = []
        for block in blocks:
            split_blocks.extend(self._split_block_for_pagination(block))

        block_pages = self._pack_blocks(split_blocks)
        block_pages = self._rebalance_pages(block_pages)

        paginated: list[dict[str, Any]] = []
        for index, block_page in enumerate(block_pages, start=1):
            paginated.append(
                {
                    "header": dict(header_template),
                    "blocks": block_page,
                    "footer": {
                        "page_number": self._page_number(index),
                        "watermark": "",
                    },
                }
            )
        return paginated

    def _prepare_block_for_pagination(self, block: Any) -> dict[str, Any] | None:
        if not isinstance(block, dict):
            return None
        normalized = dict(block)
        block_type = canonical_semantic_block_type(
            normalized.get("type") or normalized.get("block_type") or normalized.get("component")
        )
        if is_summary_semantic_block(normalized):
            block_type = "summary-box"
        if not block_type:
            return None
        normalized["type"] = block_type
        if block_type == "figure-reference":
            has_image = bool(normalized.get("image_src"))
            has_caption = bool(str(normalized.get("caption", "") or normalized.get("content", "") or "").strip())
            has_number = bool(str(normalized.get("number", "") or normalized.get("label", "") or "").strip())
            if not has_image and not has_caption and not has_number:
                return None
        if block_type == "summary-box":
            items = normalized.get("items")
            if isinstance(items, str):
                normalized["items"] = [items.strip()] if items.strip() else []
                items = normalized["items"]
            if isinstance(items, list):
                normalized["items"] = [_normalized_text_item(item) for item in items]
                normalized["items"] = [item for item in normalized["items"] if item]
                if not normalized["items"] and not str(normalized.get("content", "") or "").strip():
                    return None
        if block_type == "list":
            items = normalized.get("items")
            if isinstance(items, list):
                normalized["items"] = [_normalized_text_item(item) for item in items]
                normalized["items"] = [item for item in normalized["items"] if item]
            if not isinstance(normalized.get("items"), list) or not normalized["items"]:
                return None
        if block_type in {
            "paragraph",
            "note-box",
            "example-box",
            "unrelated-discussion-box",
            "key-highlight",
            "exam-point",
        }:
            if not str(normalized.get("content", "") or "").strip():
                return None
        if block_type in {"chapter-header", "section-header", "subsection-header"}:
            if not str(normalized.get("title", "") or "").strip():
                return None
        if block_type == "code-block":
            if not clean_code(normalized.get("code") or normalized.get("content") or ""):
                return None
        return normalized

    def _split_block_for_pagination(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        block_type = str(block.get("type", "") or "")
        if block_type == "paragraph":
            return self._split_paragraph_block(block)
        if block_type == "list":
            return self._split_list_block(block)
        if block_type == "summary-box":
            return self._split_summary_box(block)
        if block_type == "table-container":
            return self._split_table_block(block)
        return [block]

    def _split_paragraph_block(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        if block.get("content_html"):
            return [block]
        content = str(block.get("content", "") or "").strip()
        if not content:
            return []
        variants = block.get("variants", [])
        target_chars = (
            self.compact_paragraph_split_target_chars
            if isinstance(variants, list) and "compact" in variants
            else self.paragraph_split_target_chars
        )
        sentences = [part.strip() for part in SENTENCE_SPLIT_PATTERN.split(content) if part.strip()]
        if len(content) <= target_chars or len(sentences) <= 1:
            return [block]

        parts: list[str] = []
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip() if current else sentence
            if current and len(candidate) > target_chars:
                parts.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            parts.append(current)

        split_blocks: list[dict[str, Any]] = []
        for part in parts:
            split_block = dict(block)
            split_block["content"] = part
            split_blocks.append(split_block)
        return split_blocks

    def _split_list_block(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        items = block.get("items", [])
        if not isinstance(items, list) or not items:
            return []
        if len(items) <= self.list_split_max_items and sum(len(item) for item in items) <= self.list_split_target_chars:
            return [block]

        chunks: list[list[str]] = []
        current_chunk: list[str] = []
        current_chars = 0
        for item in items:
            item_text = _normalized_text_item(item)
            if not item_text:
                continue
            candidate_chars = current_chars + len(item_text)
            if current_chunk and (
                len(current_chunk) >= self.list_split_max_items or candidate_chars > self.list_split_target_chars
            ):
                chunks.append(current_chunk)
                current_chunk = [item_text]
                current_chars = len(item_text)
            else:
                current_chunk.append(item_text)
                current_chars = candidate_chars
        if current_chunk:
            chunks.append(current_chunk)

        split_blocks: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            split_block = dict(block)
            split_block["items"] = chunk
            if index > 0:
                split_block["title"] = ""
            split_blocks.append(split_block)
        return split_blocks

    def _split_summary_box(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        return [block]

    def _split_table_block(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        rows = block.get("rows", [])
        if not isinstance(rows, list) or len(rows) <= self.table_split_max_rows:
            return [block]

        split_blocks: list[dict[str, Any]] = []
        for index in range(0, len(rows), self.table_split_max_rows):
            split_block = dict(block)
            split_block["rows"] = rows[index : index + self.table_split_max_rows]
            if index > 0:
                caption = str(block.get("caption", "") or "").strip()
                continuation = "continued" if self.language_code != "fa" else "ادامه"
                split_block["caption"] = f"{caption} ({continuation})" if caption else continuation
            split_blocks.append(split_block)
        return split_blocks

    def _consolidate_summary_boxes(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summary_blocks = [block for block in blocks if str(block.get("type", "") or "") == "summary-box"]
        if not summary_blocks:
            return blocks

        merged_summary = dict(summary_blocks[0])
        merged_items: list[str] = []
        seen_items: set[str] = set()
        content_parts: list[str] = []
        seen_content: set[str] = set()

        for block in summary_blocks:
            raw_items = block.get("items", [])
            for item in raw_items if isinstance(raw_items, list) else [raw_items]:
                normalized_item = _normalized_text_item(item)
                if normalized_item and normalized_item not in seen_items:
                    seen_items.add(normalized_item)
                    merged_items.append(normalized_item)

            content = str(block.get("content", "") or "").strip()
            if content and not raw_items:
                if content not in seen_items:
                    seen_items.add(content)
                    merged_items.append(content)
                content = ""
            if content and content not in seen_content:
                seen_content.add(content)
                content_parts.append(content)

            if not str(merged_summary.get("title", "") or "").strip() and str(block.get("title", "") or "").strip():
                merged_summary["title"] = block["title"]
            if not str(merged_summary.get("icon", "") or "").strip() and str(block.get("icon", "") or "").strip():
                merged_summary["icon"] = block["icon"]

        merged_summary["items"] = merged_items
        merged_summary["content"] = "\n".join(content_parts)

        non_summary_blocks = [block for block in blocks if str(block.get("type", "") or "") != "summary-box"]
        non_summary_blocks.append(merged_summary)
        return non_summary_blocks

    def _pack_blocks(self, blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        pages: list[list[dict[str, Any]]] = []
        current_page: list[dict[str, Any]] = []
        current_score = 0
        index = 0
        while index < len(blocks):
            block = blocks[index]
            block_score = self._estimate_block_score(block)
            next_block = blocks[index + 1] if index + 1 < len(blocks) else None
            keep_with_next = self._is_keep_with_next_block(block) and next_block is not None
            if keep_with_next:
                group = [block, next_block]
                group_score = sum(self._estimate_block_score(item) for item in group if item is not None)
                if current_page and current_score + group_score > self.page_capacity_score:
                    pages.append(current_page)
                    current_page = []
                    current_score = 0
                current_page.extend(group)
                current_score += group_score
                index += 2
                continue

            if current_page and current_score + block_score > self.page_capacity_score:
                pages.append(current_page)
                current_page = [block]
                current_score = block_score
            else:
                current_page.append(block)
                current_score += block_score
            index += 1

        if current_page:
            pages.append(current_page)
        return pages

    def _rebalance_pages(self, pages: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
        if len(pages) < 2:
            return pages

        target_fill = int(self.page_capacity_score * self.target_page_fill_ratio)
        minimum_fill = int(self.page_capacity_score * self.minimum_page_fill_ratio)

        changed = True
        while changed:
            changed = False
            for page_index in range(len(pages) - 1):
                current_page = pages[page_index]
                next_page = pages[page_index + 1]
                if not current_page or not next_page:
                    continue

                current_score = self._page_score(current_page)
                next_score = self._page_score(next_page)
                if current_score >= target_fill and next_score >= minimum_fill:
                    continue

                group = self._next_page_transfer_group(next_page)
                if not group:
                    continue
                group_score = sum(self._estimate_block_score(block) for block in group)
                if current_score + group_score > self.page_capacity_score:
                    continue
                remaining_next = next_page[len(group) :]
                if remaining_next and self._page_score(remaining_next) < minimum_fill and len(pages) - (page_index + 1) == 1:
                    continue

                current_page.extend(group)
                del next_page[: len(group)]
                changed = True

            pages = [page for page in pages if page]

        repaired_pages: list[list[dict[str, Any]]] = []
        page_index = 0
        while page_index < len(pages):
            page = pages[page_index]
            if (
                len(page) == 1
                and self._is_keep_with_next_block(page[0])
                and page_index + 1 < len(pages)
                and pages[page_index + 1]
            ):
                pages[page_index + 1].insert(0, page[0])
            else:
                repaired_pages.append(page)
            page_index += 1
        return repaired_pages

    @staticmethod
    def _is_keep_with_next_block(block: dict[str, Any]) -> bool:
        return str(block.get("type", "") or "") in {"chapter-header", "section-header", "subsection-header"}

    def _next_page_transfer_group(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not blocks:
            return []
        first = blocks[0]
        if self._is_keep_with_next_block(first) and len(blocks) > 1:
            return blocks[:2]
        return [first]

    def _page_score(self, blocks: list[dict[str, Any]]) -> int:
        return sum(self._estimate_block_score(block) for block in blocks)

    def _estimate_block_score(self, block: dict[str, Any]) -> int:
        block_type = str(block.get("type", "") or "")
        if block_type == "chapter-header":
            return 95 + len(str(block.get("title", "") or "")) // 8
        if block_type == "section-header":
            return 68 + len(str(block.get("title", "") or "")) // 10
        if block_type == "subsection-header":
            return 52 + len(str(block.get("title", "") or "")) // 12
        if block_type == "paragraph":
            content = str(block.get("content", "") or "")
            return 68 + (len(content) // 55) * 24
        if block_type == "note-box":
            content = str(block.get("content", "") or "")
            return 150 + (len(content) // 58) * 24
        if block_type == "example-box":
            content = str(block.get("content", "") or "") + str(block.get("solution", "") or "")
            return 160 + (len(content) // 56) * 26
        if block_type == "unrelated-discussion-box":
            content = str(block.get("content", "") or "")
            return 145 + (len(content) // 58) * 22
        if block_type == "summary-box":
            items = block.get("items", [])
            item_count = len(items) if isinstance(items, list) else 0
            content = str(block.get("content", "") or "")
            return 140 + (item_count * 30) + (len(content) // 70) * 16
        if block_type == "list":
            items = block.get("items", [])
            item_count = len(items) if isinstance(items, list) else 0
            item_chars = sum(len(str(item)) for item in items) if isinstance(items, list) else 0
            return 118 + (item_count * 28) + (item_chars // 70) * 12
        if block_type == "exam-point":
            content = str(block.get("content", "") or "")
            return 165 + (len(content) // 55) * 30
        if block_type == "formula-box":
            if str(block.get("display_mode", "") or "") == "query":
                return 145 + len(str(block.get("formula", "") or "")) // 5
            return 120 + len(str(block.get("formula", "") or "")) // 4
        if block_type == "code-block":
            code = clean_code(block.get("code") or block.get("content") or "")
            return 76 + 22 * (code.count("\n") + 1) + (24 if block.get("caption") else 0)
        if block_type == "table-container":
            rows = block.get("rows", [])
            row_count = len(rows) if isinstance(rows, list) else 0
            return 220 + row_count * 42
        if block_type == "figure-reference":
            return 300
        if block_type == "key-highlight":
            content = str(block.get("content", "") or "")
            return 100 + (len(content) // 55) * 18
        return 120

    def _split_page_number(self, page_number: str) -> tuple[str, str]:
        normalized = str(page_number or "").strip()
        if not normalized:
            return "", ""
        match = re.match(r"^(?P<label>.*\D)\s*(?P<index>\d+)\s*$", normalized)
        if not match:
            return "", ""
        label = match.group("label").strip()
        index = match.group("index").strip()
        return label, index
