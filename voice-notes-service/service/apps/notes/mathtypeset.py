"""One LaTeX-to-HTML typesetter for every place a generated note shows math.

The model writes real LaTeX (matrices, accents, \\text{...}, Greek letters). This
module turns it into plain, sanitizer-safe HTML (spans, sup, sub, i, b, br) that the
note template styles with the ``mx-*`` classes. The editor ships a hand port of the same
rules (``apps/notes/services.py``); keep the two in step.

Contract: whatever the input, the output never contains a backslash, a raw ``\\begin`` or
a LaTeX command name glued to its argument. A command that is not known degrades to its
plain name.
"""

from __future__ import annotations

import re
from html import escape

_GREEK_VARIABLES = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "varkappa": "ϰ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "ell": "ℓ", "hbar": "ħ", "imath": "ı", "jmath": "ȷ",
}
_ORDINARY = {
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "infty": "∞", "partial": "∂", "nabla": "∇", "aleph": "ℵ", "emptyset": "∅",
    "varnothing": "∅", "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬",
    "lnot": "¬", "angle": "∠", "measuredangle": "∡", "triangle": "△", "degree": "°",
    "textdegree": "°", "prime": "′", "dagger": "†", "ddagger": "‡", "S": "§",
    "Re": "ℜ", "Im": "ℑ", "wp": "℘", "top": "⊤", "bot": "⊥", "backslash": "∖",
    "textbackslash": "∖", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
    "ddots": "⋱", "therefore": "∴", "because": "∵", "checkmark": "✓",
    "lbrace": "{", "rbrace": "}", "langle": "⟨", "rangle": "⟩", "lceil": "⌈",
    "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "lvert": "|", "rvert": "|",
    "lVert": "‖", "rVert": "‖", "vert": "|", "Vert": "‖", "lbrack": "[", "rbrack": "]",
    "square": "□", "blacksquare": "■", "Box": "□", "diamond": "◇", "clubsuit": "♣",
    "spadesuit": "♠", "heartsuit": "♥", "diamondsuit": "♦", "natural": "♮",
    "flat": "♭", "sharp": "♯", "copyright": "©", "pounds": "£", "euro": "€",
    "textbullet": "•", "textasciitilde": "~", "ss": "ß", "ae": "æ", "oe": "œ",
}
_BINARY = {
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "cdotp": "·",
    "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•", "oplus": "⊕", "ominus": "⊖",
    "otimes": "⊗", "oslash": "⊘", "odot": "⊙", "cup": "∪", "cap": "∩",
    "setminus": "∖", "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨",
    "uplus": "⊎", "sqcup": "⊔", "sqcap": "⊓", "triangleleft": "◁",
    "triangleright": "▷", "amalg": "⨿", "wr": "≀",
}
_RELATIONS = {
    "le": "≤", "leq": "≤", "leqslant": "≤", "ge": "≥", "geq": "≥", "geqslant": "≥",
    "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃",
    "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫", "prec": "≺", "succ": "≻",
    "preceq": "⪯", "succeq": "⪰", "subset": "⊂", "supset": "⊃", "subseteq": "⊆",
    "supseteq": "⊇", "subsetneq": "⊊", "supsetneq": "⊋", "in": "∈", "notin": "∉",
    "ni": "∋", "perp": "⊥", "parallel": "∥", "nparallel": "∦", "mid": "∣",
    "nmid": "∤", "models": "⊨", "vdash": "⊢", "dashv": "⊣", "doteq": "≐",
    "asymp": "≍", "lesssim": "≲", "gtrsim": "≳", "nleq": "≰", "ngeq": "≱",
    "sqsubseteq": "⊑", "sqsupseteq": "⊒", "coloneqq": "≔", "triangleq": "≜",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⟹",
    "impliedby": "⟸", "iff": "⟺", "longrightarrow": "⟶", "longleftarrow": "⟵",
    "longleftrightarrow": "⟷", "Longrightarrow": "⟹", "Longleftarrow": "⟸",
    "Longleftrightarrow": "⟺", "mapsto": "↦", "longmapsto": "⟼", "uparrow": "↑",
    "downarrow": "↓", "updownarrow": "↕", "Uparrow": "⇑", "Downarrow": "⇓",
    "nearrow": "↗", "searrow": "↘", "swarrow": "↙", "nwarrow": "↖",
    "hookrightarrow": "↪", "hookleftarrow": "↩", "rightharpoonup": "⇀",
    "leftharpoonup": "↼", "rightleftharpoons": "⇌", "leadsto": "⇝",
    "colon": ":",
}
_BIG_OPERATORS = {
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭",
    "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁", "bigotimes": "⨂",
    "bigvee": "⋁", "bigwedge": "⋀",
}
_LIMIT_OPERATORS = frozenset(
    {"sum", "prod", "coprod", "bigcup", "bigcap", "bigoplus", "bigotimes", "bigvee", "bigwedge"}
)
_FUNCTIONS = frozenset(
    {
        "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "arccot",
        "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "limsup",
        "liminf", "max", "min", "sup", "inf", "arg", "det", "dim", "gcd", "deg", "hom",
        "ker", "Pr", "rank", "tr", "trace", "diag", "sgn", "var", "cov",
    }
)
_LIMIT_FUNCTIONS = frozenset({"lim", "limsup", "liminf"})

_DELIMITERS = {
    "(": "(", ")": ")", "[": "[", "]": "]", "|": "|", "/": "/", "<": "⟨", ">": "⟩",
    ".": "", "\\{": "{", "\\}": "}", "\\|": "‖", "{": "{", "}": "}",
}

_SPACING = {
    ",": "\u2009", ":": "\u2005", ";": "\u2004", " ": " ", "!": "", ">": "\u2005",
    "quad": "\u2003", "qquad": "\u2003\u2003", "enspace": "\u2002",
    "thinspace": "\u2009", "medspace": "\u2005", "thickspace": "\u2004",
}
_SKIP_WITH_ARGUMENT = frozenset(
    {
        "hspace", "vspace", "phantom", "hphantom", "vphantom", "label", "color",
        "pagecolor", "eqref", "ref", "cline", "rule", "mkern", "mskip", "kern",
    }
)
_SKIP_BARE = frozenset(
    {
        "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "limits",
        "nolimits", "nonumber", "notag", "hline", "vline", "toprule", "midrule",
        "bottomrule", "protect", "relax", "mathstrut", "strut", "not", "centering",
        "noindent", "allowbreak", "nobreak", "bigskip", "medskip", "smallskip",
        "hfill", "vfill", "break", "newline", "crcr", "cr",
    }
)
_UPRIGHT_COMMANDS = frozenset(
    {"mathrm", "textrm", "textnormal", "rm", "operatorname", "mathsf", "textsf", "mathtt", "texttt"}
)
_BOLD_COMMANDS = frozenset({"mathbf", "textbf", "bf", "boldsymbol", "bm", "pmb", "mathbfit"})
_ITALIC_COMMANDS = frozenset({"mathit", "textit", "emph", "mathnormal", "it"})
_PLAIN_WRAPPERS = frozenset(
    {"mathcal", "mathscr", "mathfrak", "mathop", "mathrel", "mathbin", "mathord",
     "mathpunct", "mathinner", "mathclose", "mathopen", "displaystyle", "hbox", "vbox"}
)
_TEXT_COMMANDS = frozenset({"text", "mbox", "textup", "textmd", "intertext", "mathrm_text"})
_BLACKBOARD = {
    "R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "E": "𝔼",
    "H": "ℍ", "F": "𝔽", "1": "𝟙",
}
_ACCENTS = {
    "dot": "dot", "ddot": "ddot", "dddot": "dddot", "hat": "hat", "widehat": "hat",
    "tilde": "tilde", "widetilde": "tilde", "bar": "bar", "overline": "bar",
    "check": "check", "breve": "breve", "acute": "acute", "grave": "grave",
    "vec": "vec", "overrightarrow": "vec", "overleftarrow": "vecl", "mathring": "ring",
    "underline": "under", "underbar": "under",
}
_MATRIX_DELIMITERS = {
    "matrix": "", "smallmatrix": "", "pmatrix": "p", "bmatrix": "b", "Bmatrix": "B",
    "vmatrix": "v", "Vmatrix": "V", "matrix*": "", "pmatrix*": "p", "bmatrix*": "b",
}
_LONG_ARROWS = {"xrightarrow": "→", "xleftarrow": "←", "xRightarrow": "⇒", "xLeftarrow": "⇐",
                "xleftrightarrow": "↔", "xmapsto": "↦"}

_ENVIRONMENT_TAG = re.compile(r"\\(begin|end)\s*\{([^{}]*)\}")
_BROKEN_ENVIRONMENT = re.compile(
    r"\\(begin|end)(?!\s*\{)(smallmatrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|"
    r"cases|aligned|align|gathered|gather|array|split|equation)"
)
_LETTER_RUN = re.compile(r"[A-Za-z]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_COMMAND = re.compile(r"\\([A-Za-z]+\*?|.)", re.S)
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff\u200c]")
_ROW_SPACING = re.compile(r"\s*\[\s*-?[\d.]+\s*(?:pt|em|ex|mm|cm|in|bp)\s*\]")

# JSON turns "\begin", "\frac", "\theta" into a control character plus the rest of the
# word. Put the backslash back when the remainder is a command we know.
_CONTROL_REPAIRS = (
    ("\x08", "b", "egin|eta|ar|oldsymbol|inom|ullet|igcup|igcap|ig|m|mod|ot|oxed"),
    ("\x0c", "f", "rac|orall|box"),
    ("\t", "t", "heta|ext|imes|au|an|o|ilde|extbf|extit|extrm|riangle|op|herefore|ag"),
    ("\r", "r", "ho|ight|ightarrow|angle|ceil|floor|brace|m"),
    ("\n", "n", "abla|u|eq|e|ot|ewline|i|otin|mid|geq|leq|onumber"),
    ("\x07", "a", "lpha|ngle|pprox|st|leph|rg|symp|cute|rcsin|rccos|rctan"),
)
_CONTROL_PATTERNS = tuple(
    (re.compile(re.escape(char) + rf"(?=(?:{suffixes})(?![A-Za-z]))"), "\\" + letter)
    for char, letter, suffixes in _CONTROL_REPAIRS
)


_WHITESPACE_CONTROLS = frozenset("\t\r\n")


_LINEBREAK_GLUED_ENVIRONMENT = re.compile(r"(?<!\\)(\\\\)(begin|end)(?=\s*\{)")


def repair_latex_escapes(source: str, *, whitespace: bool = True) -> str:
    """Undo control characters left behind when JSON consumed a LaTeX backslash.

    ``whitespace=False`` leaves tab/CR/LF alone, for prose where they are real whitespace.
    """
    if not any(char in source for char in "\x08\x0c\t\r\n\x07\\"):
        return source
    text = _BROKEN_ENVIRONMENT.sub(lambda m: "\\" + m.group(1) + "{" + m.group(2) + "}", source)
    text = _LINEBREAK_GLUED_ENVIRONMENT.sub(r"\1\\\2", text)
    for (char, _letter, _suffixes), (pattern, replacement) in zip(_CONTROL_REPAIRS, _CONTROL_PATTERNS):
        if not whitespace and char in _WHITESPACE_CONTROLS:
            continue
        text = pattern.sub(lambda _m, r=replacement: r, text)
    return text


_KNOWN_NAMES: frozenset[str] | None = None


def _known_command_names() -> frozenset[str]:
    global _KNOWN_NAMES
    if _KNOWN_NAMES is None:
        names: set[str] = set()
        for table in (
            _GREEK_VARIABLES, _ORDINARY, _BINARY, _RELATIONS, _BIG_OPERATORS, _ACCENTS,
            _SPACING, _LONG_ARROWS,
        ):
            names.update(key for key in table if len(key) > 1)
        names.update(_FUNCTIONS)
        names.update(_TEXT_COMMANDS | _UPRIGHT_COMMANDS | _BOLD_COMMANDS | _ITALIC_COMMANDS)
        names.update(
            {"frac", "dfrac", "tfrac", "binom", "sqrt", "mathbb", "left", "right", "overset",
             "underset", "boxed", "pmod", "overbrace", "underbrace"}
        )
        _KNOWN_NAMES = frozenset(names)
    return _KNOWN_NAMES


def _text_html(text: str) -> str:
    """Plain (non-math) text; right-to-left script keeps its own direction."""
    if not text:
        return ""
    stripped = text
    if stripped[:1] == " ":
        stripped = "\u00a0" + stripped[1:]
    if stripped[-1:] == " ":
        stripped = stripped[:-1] + "\u00a0"
    stripped = stripped.replace("\\", "").replace("$", "")
    if not stripped.strip():
        return ""
    direction = ' dir="rtl"' if _ARABIC_SCRIPT.search(stripped) else ""
    return f'<span class="mx-text"{direction}>{escape(stripped, quote=False)}</span>'


class _Typesetter:
    def __init__(self, source: str, *, upright: int = 0, bold: int = 0) -> None:
        self.s = source
        self.n = len(source)
        self.i = 0
        self.upright = upright
        self.bold = bold
        self._limit_at: tuple[int, int] | None = None

    def child(self, raw: str) -> str:
        return _Typesetter(raw, upright=self.upright, bold=self.bold).sequence()

    def _skip_whitespace(self) -> None:
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def _raw_group(self) -> str | None:
        self._skip_whitespace()
        if self.i >= self.n or self.s[self.i] != "{":
            return None
        depth = 0
        cursor = self.i
        while cursor < self.n:
            char = self.s[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    raw = self.s[self.i + 1 : cursor]
                    self.i = cursor + 1
                    return raw
            cursor += 1
        raw = self.s[self.i + 1 :]
        self.i = self.n
        return raw

    def _raw_optional(self) -> str | None:
        self._skip_whitespace()
        if self.i >= self.n or self.s[self.i] != "[":
            return None
        close = self.s.find("]", self.i)
        if close == -1:
            return None
        raw = self.s[self.i + 1 : close]
        self.i = close + 1
        return raw

    def _raw_argument(self) -> str:
        grouped = self._raw_group()
        if grouped is not None:
            return grouped
        if self.i >= self.n:
            return ""
        match = _COMMAND.match(self.s, self.i) if self.s[self.i] == "\\" else None
        if match:
            self.i = match.end()
            return match.group(0)
        char = self.s[self.i]
        self.i += 1
        return char

    def argument(self) -> str:
        return self.child(self._raw_argument())

    def sequence(self) -> str:
        out: list[str] = []
        while self.i < self.n:
            self._read_one(out)
        return "".join(out).strip()

    def _read_one(self, out: list[str]) -> None:
        char = self.s[self.i]
        if char.isspace():
            while self.i < self.n and self.s[self.i].isspace():
                self.i += 1
            if out and self.i < self.n and self.s[self.i] not in ")],;" and not out[-1].endswith(("(", "[")):
                out.append(" ")
            return
        if char == "{":
            raw = self._raw_group() or ""
            out.append(self.child(raw))
            return
        if char == "}":
            self.i += 1
            return
        if char in "^_":
            self._script(out)
            return
        if char == "\\":
            self._command(out)
            return
        if char.isalpha() and char.isascii():
            run = _LETTER_RUN.match(self.s, self.i).group(0)  # type: ignore[union-attr]
            self.i += len(run)
            if self.upright:
                out.append(escape(run))
            elif len(run) >= 3:
                out.append(f'<span class="mx-word">{escape(run)}</span>')
            else:
                out.extend(f"<i>{escape(letter)}</i>" for letter in run)
            return
        if char.isdigit() and char.isascii():
            number = _NUMBER.match(self.s, self.i).group(0)  # type: ignore[union-attr]
            self.i += len(number)
            out.append(number)
            return
        if _ARABIC_SCRIPT.match(char):
            end = self.i
            while end < self.n:
                if _ARABIC_SCRIPT.match(self.s[end]):
                    end += 1
                elif self.s[end] == " " and end + 1 < self.n and _ARABIC_SCRIPT.match(self.s[end + 1]):
                    end += 1
                else:
                    break
            out.append(_text_html(self.s[self.i : end]))
            self.i = end
            return
        self.i += 1
        if char == "$":
            return
        if char == "-":
            out.append("−")
        elif char == "*":
            out.append("×")
        elif char == "~":
            out.append("\u00a0")
        elif char == "'":
            out.append("′")
        elif char in "=<>":
            out.append(f'<span class="mx-rel">{escape(char)}</span>\u200b')
        elif char == "+":
            out.append('<span class="mx-bin">+</span>\u200b')
        elif char == "&":
            out.append("&amp;")
        elif "α" <= char <= "ω":
            out.append(f"<i>{char}</i>" if not self.upright else char)
        elif char in "`":
            return
        else:
            out.append(escape(char, quote=False))

    def _script(self, out: list[str]) -> None:
        superscript = subscript = None
        while self.i < self.n:
            self._skip_whitespace()
            if self.i >= self.n or self.s[self.i] not in "^_":
                break
            kind = self.s[self.i]
            self.i += 1
            self._skip_whitespace()
            argument = self.argument()
            if kind == "^":
                superscript = argument
            else:
                subscript = argument
        base = ""
        if out:
            at_limit = self._limit_at == (id(out), len(out) - 1)
            base = out.pop()
        else:
            at_limit = False
        if at_limit:
            top = f'<span class="mx-limit-top">{superscript}</span>' if superscript else ""
            bottom = f'<span class="mx-limit-bottom">{subscript}</span>' if subscript else ""
            out.append(
                f'<span class="mx-limits">{top}<span class="mx-limit-base">{base}</span>{bottom}</span>'
            )
        elif superscript and subscript:
            out.append(base)
            out.append(
                f'<span class="mx-scripts"><sup>{superscript}</sup><sub>{subscript}</sub></span>'
            )
        elif superscript:
            out.extend([base, f"<sup>{superscript}</sup>"])
        elif subscript:
            out.extend([base, f"<sub>{subscript}</sub>"])
        else:
            out.append(base)

    def _delimiter(self) -> str:
        self._skip_whitespace()
        if self.i >= self.n:
            return ""
        if self.s[self.i] == "\\":
            match = _COMMAND.match(self.s, self.i)
            if match:
                self.i = match.end()
                token = match.group(0)
                name = match.group(1)
                if token in _DELIMITERS:
                    return _DELIMITERS[token]
                symbol = _ORDINARY.get(name) or _RELATIONS.get(name)
                return symbol or ""
        char = self.s[self.i]
        self.i += 1
        return _DELIMITERS.get(char, char if not char.isalnum() else "")

    def _command(self, out: list[str]) -> None:
        match = _COMMAND.match(self.s, self.i)
        if not match:
            self.i += 1
            return
        self.i = match.end()
        name = match.group(1)
        if name == "\\":
            rest = _ROW_SPACING.match(self.s, self.i)
            if rest:
                self.i = rest.end()
            out.append("<br>")
            return
        if len(name) == 1 and not name.isalpha():
            self._escaped_character(out, name)
            return
        if name.endswith("*"):
            name = name[:-1]
        self._named_command(out, self._resolve_glued(name))

    def _resolve_glued(self, name: str) -> str:
        """``\\dotx`` -> ``\\dot x``: models sometimes drop the separator after a command."""
        known = _known_command_names()
        if name in known:
            return name
        for cut in range(len(name) - 1, 1, -1):
            prefix, rest = name[:cut], name[cut:]
            if prefix not in known:
                continue
            if rest in known:
                rest = "\\" + rest
            elif len(rest) > 2:
                continue
            self.s = self.s[: self.i - len(name) + cut] + " " + rest + " " + self.s[self.i :]
            self.n = len(self.s)
            self.i = self.i - len(name) + cut
            return prefix
        return name

    def _escaped_character(self, out: list[str], char: str) -> None:
        if char in _SPACING:
            out.append(_SPACING[char])
        elif char == "|":
            out.append("‖")
        elif char == "-" or char == "/" or char == "\n":
            return
        else:
            out.append(escape(char, quote=False))

    def _named_command(self, out: list[str], name: str) -> None:
        if name in _SPACING:
            out.append(_SPACING[name])
        elif name in _GREEK_VARIABLES:
            symbol = _GREEK_VARIABLES[name]
            out.append(symbol if self.upright else f"<i>{symbol}</i>")
        elif name in _ORDINARY:
            out.append(escape(_ORDINARY[name], quote=False))
        elif name == "circ" and (not out or re.search(r"[\d)]\s*$", re.sub(r"<[^>]*>", "", "".join(out[-3:])))):
            while out and not out[-1].strip():
                out.pop()
            out.append("°")
        elif name in _BINARY:
            out.append(f'<span class="mx-bin">{escape(_BINARY[name])}</span>\u200b')
        elif name in _RELATIONS:
            out.append(f'<span class="mx-rel">{escape(_RELATIONS[name])}</span>\u200b')
        elif name in _BIG_OPERATORS:
            out.append(f'<span class="mx-op">{_BIG_OPERATORS[name]}</span>')
            if name in _LIMIT_OPERATORS:
                self._limit_at = (id(out), len(out) - 1)
        elif name in _FUNCTIONS:
            out.append(f'<span class="mx-fn">{name}</span>')
            if name in _LIMIT_FUNCTIONS:
                self._limit_at = (id(out), len(out) - 1)
            elif self.s[self.i : self.i + 1] not in ("(", "[", "|"):
                out.append("\u2009")
        elif name in ("frac", "dfrac", "tfrac", "cfrac"):
            numerator, denominator = self.argument(), self.argument()
            out.append(
                f'<span class="mx-frac"><span class="mx-num">{numerator}</span>'
                f'<span class="mx-den">{denominator}</span></span>'
            )
        elif name in ("binom", "dbinom", "tbinom"):
            top, bottom = self.argument(), self.argument()
            out.append(
                '<span class="mx-delim">(</span><span class="mx-frac mx-frac--bare">'
                f'<span class="mx-num">{top}</span><span class="mx-den">{bottom}</span></span>'
                '<span class="mx-delim">)</span>'
            )
        elif name == "sqrt":
            index = self._raw_optional()
            radicand = self.argument()
            index_html = (
                f'<sup class="mx-root-index">{self.child(index)}</sup>' if index else ""
            )
            out.append(
                f'<span class="mx-root">{index_html}<span class="mx-radical">√</span>'
                f'<span class="mx-radicand">{radicand}</span></span>'
            )
        elif name in _TEXT_COMMANDS:
            grouped = self._raw_group()
            if grouped is None:
                word = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff\u200c]+").match(
                    self.s, self.i
                )
                if word:
                    grouped = word.group(0)
                    self.i = word.end()
                elif self.s[self.i : self.i + 1] == "\\":
                    grouped = ""
                else:
                    grouped = self._raw_argument()
            out.append(_text_html(grouped))
        elif name in _UPRIGHT_COMMANDS:
            self.upright += 1
            inner = self.argument()
            self.upright -= 1
            out.append(f'<span class="mx-rm">{inner}</span>')
        elif name in _BOLD_COMMANDS:
            self.bold += 1
            inner = self.argument()
            self.bold -= 1
            out.append(f"<b>{inner}</b>")
        elif name in _ITALIC_COMMANDS:
            out.append(f"<i>{self.argument()}</i>")
        elif name == "mathbb":
            raw = self._raw_argument()
            mapped = "".join(_BLACKBOARD.get(char, char) for char in raw)
            out.append(escape(mapped, quote=False))
        elif name in _PLAIN_WRAPPERS:
            out.append(self.argument())
        elif name in _ACCENTS:
            out.append(f'<span class="mx-acc mx-acc--{_ACCENTS[name]}">{self.argument()}</span>')
        elif name in ("left", "right", "middle", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr",
                      "Biggl", "Biggr", "bigm", "Bigm"):
            delimiter = self._delimiter()
            if delimiter:
                out.append(f'<span class="mx-delim">{escape(delimiter, quote=False)}</span>')
        elif name in ("big", "Big", "bigg", "Bigg"):
            delimiter = self._delimiter()
            if delimiter:
                out.append(f'<span class="mx-delim">{escape(delimiter, quote=False)}</span>')
        elif name == "begin":
            self._environment(out)
        elif name == "end":
            self._raw_group()
        elif name == "tag":
            out.append(f'<span class="mx-tag">({self.argument()})</span>')
        elif name in ("boxed", "fbox", "framebox"):
            out.append(f'<span class="mx-boxed">{self.argument()}</span>')
        elif name in ("cancel", "xcancel", "bcancel", "sout"):
            out.append(f'<span class="mx-cancel">{self.argument()}</span>')
        elif name in ("textcolor", "colorbox"):
            self._raw_group()
            out.append(self.argument())
        elif name in ("overset", "stackrel"):
            above, base = self.argument(), self.argument()
            out.append(
                f'<span class="mx-limits"><span class="mx-limit-top">{above}</span>'
                f'<span class="mx-limit-base">{base}</span></span>'
            )
        elif name == "underset":
            below, base = self.argument(), self.argument()
            out.append(
                f'<span class="mx-limits"><span class="mx-limit-base">{base}</span>'
                f'<span class="mx-limit-bottom">{below}</span></span>'
            )
        elif name in ("overbrace", "underbrace"):
            kind = "over" if name == "overbrace" else "under"
            out.append(f'<span class="mx-{kind}brace">{self.argument()}</span>')
            self._limit_at = (id(out), len(out) - 1)
        elif name in _LONG_ARROWS:
            below = self._raw_optional()
            above = self.argument()
            below_html = f'<span class="mx-limit-bottom">{self.child(below)}</span>' if below else ""
            out.append(
                f'<span class="mx-limits"><span class="mx-limit-top">{above}</span>'
                f'<span class="mx-limit-base mx-rel">{_LONG_ARROWS[name]}</span>{below_html}</span>'
            )
        elif name in ("bmod", "mod"):
            out.append("\u2005mod\u2005")
        elif name in ("pmod", "pod"):
            out.append(f"\u2005(mod\u2005{self.argument()})")
        elif name == "substack":
            rows = _split_rows(self._raw_group() or "")
            out.append(self._grid(rows, kind="stack", delimiter=""))
        elif name in _SKIP_WITH_ARGUMENT:
            self._raw_optional()
            self._raw_group()
        elif name in _SKIP_BARE:
            return
        else:
            out.append(f'<span class="mx-fn">{escape(name)}</span>\u2009')

    def _environment(self, out: list[str]) -> None:
        name = (self._raw_group() or "").strip()
        body, self.i = _environment_body(self.s, self.i, name)
        base = name.rstrip("*")
        if base in ("array", "tabular", "alignedat", "subarray", "tabular*"):
            self._raw_group()
            body = body.lstrip()
            if body.startswith("{"):
                depth = 0
                for cursor, char in enumerate(body):
                    depth += char == "{"
                    depth -= char == "}"
                    if depth == 0:
                        body = body[cursor + 1 :]
                        break
        rows = _split_rows(body)
        if name in _MATRIX_DELIMITERS or base in _MATRIX_DELIMITERS:
            delimiter = _MATRIX_DELIMITERS.get(name, _MATRIX_DELIMITERS.get(base, ""))
            out.append(self._grid(rows, kind="matrix", delimiter=delimiter))
        elif base in ("cases", "dcases", "rcases"):
            side = "right" if base == "rcases" else "left"
            out.append(self._grid(rows, kind="cases", delimiter="brace-" + side))
        elif base in ("equation", "displaymath", "math", "split", "gathered", "gather"):
            out.append(self._grid(rows, kind="gather", delimiter=""))
        else:
            out.append(self._grid(rows, kind="align", delimiter=""))

    def _grid(self, rows: list[list[str]], *, kind: str, delimiter: str) -> str:
        rendered_rows: list[str] = []
        for row in rows:
            cells = []
            for column, raw in enumerate(row):
                side = ""
                if kind == "align":
                    side = " mx-cell--r" if column % 2 == 0 else " mx-cell--l"
                elif kind == "cases":
                    side = " mx-cell--l"
                cells.append(f'<span class="mx-cell{side}">{self.child(raw)}</span>')
            rendered_rows.append(f'<span class="mx-mrow">{"".join(cells)}</span>')
        grid = f'<span class="mx-grid">{"".join(rendered_rows)}</span>'
        height = min(max(len(rows), 1), 4)
        left = right = ""
        if delimiter.startswith("brace-"):
            glyph = f'<span class="mx-bigbrace mx-bigbrace--{height}">{{</span>'
            closing = f'<span class="mx-bigbrace mx-bigbrace--{height}">}}</span>'
            left, right = (glyph, "") if delimiter.endswith("left") else ("", closing)
        elif delimiter == "B":
            left = f'<span class="mx-bigbrace mx-bigbrace--{height}">{{</span>'
            right = f'<span class="mx-bigbrace mx-bigbrace--{height}">}}</span>'
        elif delimiter:
            left = f'<span class="mx-bracket mx-bracket--{delimiter}-l"></span>'
            right = f'<span class="mx-bracket mx-bracket--{delimiter}-r"></span>'
        return f'<span class="mx-matrix mx-matrix--{kind}">{left}{grid}{right}</span>\u200b'


def _environment_body(source: str, start: int, name: str) -> tuple[str, int]:
    depth = 1
    for tag in _ENVIRONMENT_TAG.finditer(source, start):
        if tag.group(2).strip() != name:
            continue
        depth += 1 if tag.group(1) == "begin" else -1
        if depth == 0:
            return source[start : tag.start()], tag.end()
    return source[start:], len(source)


def _split_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    cells: list[str] = []
    current: list[str] = []
    depth = 0
    nested = 0
    cursor = 0
    while cursor < len(body):
        char = body[cursor]
        tag = _ENVIRONMENT_TAG.match(body, cursor) if char == "\\" else None
        if tag:
            nested += 1 if tag.group(1) == "begin" else -1
            current.append(tag.group(0))
            cursor = tag.end()
            continue
        if char == "\\" and cursor + 1 < len(body):
            following = body[cursor + 1]
            if following == "\\" and depth == 0 and nested <= 0:
                cells.append("".join(current))
                rows.append(cells)
                cells, current = [], []
                cursor += 2
                spacing = _ROW_SPACING.match(body, cursor)
                if spacing:
                    cursor = spacing.end()
                continue
            current.append(char + following)
            cursor += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        elif char == "&" and depth == 0 and nested <= 0:
            cells.append("".join(current))
            current = []
            cursor += 1
            continue
        current.append(char)
        cursor += 1
    cells.append("".join(current))
    rows.append(cells)
    return [row for row in rows if any(cell.strip() for cell in row)]


def _normalize(source: str) -> str:
    text = repair_latex_escapes(str(source or ""))
    text = _BROKEN_ENVIRONMENT.sub(lambda m: "\\" + m.group(1) + "{" + m.group(2) + "}", text)
    return text.replace("\u00a0", " ").strip()


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ue000\ue001]")


def typeset_math(source: str, *, wrap: bool = True) -> str:
    """Return sanitizer-safe HTML for a LaTeX math expression, never leaking raw commands."""
    body = _Typesetter(_CONTROL_CHARACTERS.sub("", _normalize(source))).sequence()
    return f'<span class="mx" dir="ltr">{body}</span>' if wrap else body


_MATH_DELIMITED = re.compile(
    r"\$\$(?P<dd>[^$]+?)\$\$"
    r"|\\\[(?P<br>[^\n]+?)\\\]"
    r"|\\\((?P<pa>[^\n]+?)\\\)"
    r"|(?<![\\$])\$(?P<sd>[^\s$](?:[^$\n]*[^\s$])?)\$"
)
_ENVIRONMENT_OPEN = re.compile(r"\\begin\s*\{([^{}]+)\}")
_SENTINEL = re.compile("\ue000(\\d+)\ue001")


def _stash_environments(segment: str, items: list[tuple[str, bool]]) -> str:
    segment = _BROKEN_ENVIRONMENT.sub(lambda m: "\\" + m.group(1) + "{" + m.group(2) + "}", segment)
    out: list[str] = []
    cursor = 0
    while True:
        opener = _ENVIRONMENT_OPEN.search(segment, cursor)
        if not opener:
            out.append(segment[cursor:])
            break
        _body, end = _environment_body(segment, opener.end(), opener.group(1).strip())
        out.append(segment[cursor : opener.start()])
        items.append((segment[opener.start() : end], True))
        out.append(f"\ue000{len(items) - 1}\ue001")
        cursor = end
    return "".join(out)


def stash_math(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Replace math in prose with placeholders; returns (text, [(latex, is_display)]).

    Covers $..$, $$..$$, \\(..\\), \\[..\\] and bare \\begin..\\end environments, including
    environments whose braces the model dropped (``\\beginbmatrix``).
    """
    text = repair_latex_escapes(text, whitespace=False)
    if "\\" not in text and "$" not in text:
        return text, []
    items: list[tuple[str, bool]] = []
    out: list[str] = []
    cursor = 0
    for match in _MATH_DELIMITED.finditer(text):
        out.append(_stash_environments(text[cursor : match.start()], items))
        latex = next(value for value in match.groupdict().values() if value is not None).strip()
        items.append((latex, match.group("dd") is not None or match.group("br") is not None))
        out.append(f"\ue000{len(items) - 1}\ue001")
        cursor = match.end()
    out.append(_stash_environments(text[cursor:], items))
    return "".join(out), items


_PROSE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_STRAY_DELIMITER = re.compile(r"\\[\[\]()]")
_STRAY_LINEBREAK = re.compile(r"\\\\(?:\[[^\]\s]*\])?")
_STRAY_SPACING = re.compile(r"\\[,;:! ]")


def scrub_stray_latex(html: str) -> str:
    """Drop orphan LaTeX punctuation (unpaired ``\\]``, ``\\\\``, ``\\-``) left in prose.

    Typeset math never contains a backslash, so any that remains is stray markup.
    """
    html = _PROSE_CONTROL_CHARACTERS.sub("", html)
    if "\\" not in html:
        return html
    html = _STRAY_LINEBREAK.sub(" ", html)
    html = _STRAY_DELIMITER.sub("", html)
    html = _STRAY_SPACING.sub(" ", html)
    return html.replace("\\", "")


def restore_math(html: str, items: list[tuple[str, bool]]) -> str:
    if not items:
        return scrub_stray_latex(html)

    def _render(match: re.Match[str]) -> str:
        latex, display = items[int(match.group(1))]
        classes = "inline-math inline-math--latex" + (" inline-math--display" if display else "")
        return f'<span class="{classes}" dir="ltr">{typeset_math(latex)}</span>'

    return scrub_stray_latex(_SENTINEL.sub(_render, html))
