"""JavaScript port of ``mathtypeset`` for the in-browser note editor.

Symbol tables are generated from the Python module so the two cannot drift; the parsing
rules are a hand port of ``_Typesetter``. Output uses the same ``mx-*`` markup, so a
formula edited in the editor looks like one rendered on the server.
"""

from __future__ import annotations

import json

from apps.notes import mathtypeset as m

_JS_BODY = r"""
const MX = __TABLES__;
const BS = '\\';
const ARABIC = /[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿‌]/;
const ARABIC_RUN = /^[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿‌]+/;
const ENV_TAG = /^\\(begin|end)\s*\{([^{}]*)\}/;
const COMMAND = /^\\([A-Za-z]+\*?|[\s\S])/;
const ROW_SPACING = /^\s*\[\s*-?[\d.]+\s*(?:pt|em|ex|mm|cm|in|bp)\s*\]/;
const mxEscape = (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const mxSet = (list) => new Set(list);
const MX_FUNCTIONS = mxSet(MX.functions);
const MX_LIMIT_FUNCTIONS = mxSet(MX.limitFunctions);
const MX_LIMIT_OPERATORS = mxSet(MX.limitOperators);
const MX_UPRIGHT = mxSet(MX.upright);
const MX_BOLD = mxSet(MX.bold);
const MX_ITALIC = mxSet(MX.italic);
const MX_PLAIN = mxSet(MX.plain);
const MX_TEXT = mxSet(MX.text);
const MX_SKIP_ARG = mxSet(MX.skipArgument);
const MX_SKIP_BARE = mxSet(MX.skipBare);
const MX_KNOWN = mxSet(MX.known);
const MX_SIZING = mxSet(['left', 'right', 'middle', 'bigl', 'bigr', 'Bigl', 'Bigr', 'biggl', 'biggr',
  'Biggl', 'Biggr', 'bigm', 'Bigm', 'big', 'Big', 'bigg', 'Bigg']);

function mxRepair(source) {
  let text = String(source || '');
  text = text.replace(
    /\\(begin|end)(?!\s*\{)(smallmatrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|cases|aligned|align|gathered|gather|array|split|equation)/g,
    (_all, word, env) => `${BS}${word}{${env}}`
  );
  text = text.replace(/(^|[^\\])(\\\\)(begin|end)(?=\s*\{)/g, (_all, lead, breaks, word) => `${lead}${breaks}${BS}${word}`);
  MX.controlRepairs.forEach(([char, letter, suffixes]) => {
    text = text.split(char).join('\x00');
    text = text.replace(new RegExp(`\x00(?=(?:${suffixes})(?![A-Za-z]))`, 'g'), BS + letter);
    text = text.split('\x00').join(char);
  });
  text = text.replace(
    /\\(begin|end)(?!\s*\{)(smallmatrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|cases|aligned|align|gathered|gather|array|split|equation)/g,
    (_all, word, env) => `${BS}${word}{${env}}`
  );
  return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '').replace(/ /g, ' ').trim();
}

function mxTextHtml(raw) {
  let text = String(raw || '').split(BS).join('').split('$').join('');
  if (!text.trim()) return '';
  if (text[0] === ' ') text = ` ${text.slice(1)}`;
  if (text.endsWith(' ')) text = `${text.slice(0, -1)} `;
  const dir = ARABIC.test(text) ? ' dir="rtl"' : '';
  return `<span class="mx-text"${dir}>${mxEscape(text)}</span>`;
}

function mxEnvironmentBody(source, start, name) {
  const pattern = /\\(begin|end)\s*\{([^{}]*)\}/g;
  pattern.lastIndex = start;
  let depth = 1;
  let tag;
  while ((tag = pattern.exec(source))) {
    if (tag[2].trim() !== name) continue;
    depth += tag[1] === 'begin' ? 1 : -1;
    if (depth === 0) return [source.slice(start, tag.index), tag.index + tag[0].length];
  }
  return [source.slice(start), source.length];
}

function mxSplitRows(body) {
  const rows = [];
  let cells = [];
  let current = '';
  let depth = 0;
  let nested = 0;
  let cursor = 0;
  while (cursor < body.length) {
    const char = body[cursor];
    const tag = char === BS ? ENV_TAG.exec(body.slice(cursor)) : null;
    if (tag) {
      nested += tag[1] === 'begin' ? 1 : -1;
      current += tag[0];
      cursor += tag[0].length;
      continue;
    }
    if (char === BS && cursor + 1 < body.length) {
      const following = body[cursor + 1];
      if (following === BS && depth === 0 && nested <= 0) {
        cells.push(current);
        rows.push(cells);
        cells = [];
        current = '';
        cursor += 2;
        const spacing = ROW_SPACING.exec(body.slice(cursor));
        if (spacing) cursor += spacing[0].length;
        continue;
      }
      current += char + following;
      cursor += 2;
      continue;
    }
    if (char === '{') depth += 1;
    else if (char === '}') depth = Math.max(depth - 1, 0);
    else if (char === '&' && depth === 0 && nested <= 0) {
      cells.push(current);
      current = '';
      cursor += 1;
      continue;
    }
    current += char;
    cursor += 1;
  }
  cells.push(current);
  rows.push(cells);
  return rows.filter((row) => row.some((cell) => cell.trim()));
}

class MxTypesetter {
  constructor(source, upright = 0, bold = 0) {
    this.s = source;
    this.n = source.length;
    this.i = 0;
    this.upright = upright;
    this.bold = bold;
    this.limitAt = null;
  }

  child(raw) { return new MxTypesetter(raw, this.upright, this.bold).sequence(); }

  skipSpace() { while (this.i < this.n && /\s/.test(this.s[this.i])) this.i += 1; }

  rawGroup() {
    this.skipSpace();
    if (this.i >= this.n || this.s[this.i] !== '{') return null;
    let depth = 0;
    let cursor = this.i;
    while (cursor < this.n) {
      const char = this.s[cursor];
      if (char === BS) { cursor += 2; continue; }
      if (char === '{') depth += 1;
      else if (char === '}') {
        depth -= 1;
        if (depth === 0) {
          const raw = this.s.slice(this.i + 1, cursor);
          this.i = cursor + 1;
          return raw;
        }
      }
      cursor += 1;
    }
    const raw = this.s.slice(this.i + 1);
    this.i = this.n;
    return raw;
  }

  rawOptional() {
    this.skipSpace();
    if (this.i >= this.n || this.s[this.i] !== '[') return null;
    const close = this.s.indexOf(']', this.i);
    if (close === -1) return null;
    const raw = this.s.slice(this.i + 1, close);
    this.i = close + 1;
    return raw;
  }

  rawArgument() {
    const grouped = this.rawGroup();
    if (grouped !== null) return grouped;
    if (this.i >= this.n) return '';
    if (this.s[this.i] === BS) {
      const match = COMMAND.exec(this.s.slice(this.i));
      if (match) { this.i += match[0].length; return match[0]; }
    }
    const char = this.s[this.i];
    this.i += 1;
    return char;
  }

  argument() { return this.child(this.rawArgument()); }

  sequence() {
    const out = [];
    while (this.i < this.n) this.readOne(out);
    return out.join('').trim();
  }

  readOne(out) {
    const char = this.s[this.i];
    if (/\s/.test(char)) {
      while (this.i < this.n && /\s/.test(this.s[this.i])) this.i += 1;
      const last = out.length ? out[out.length - 1] : '';
      if (out.length && this.i < this.n && !')],;'.includes(this.s[this.i]) && !/[(\[]$/.test(last)) out.push(' ');
      return;
    }
    if (char === '{') { out.push(this.child(this.rawGroup() || '')); return; }
    if (char === '}') { this.i += 1; return; }
    if (char === '^' || char === '_') { this.script(out); return; }
    if (char === BS) { this.command(out); return; }
    if (/[A-Za-z]/.test(char)) {
      const run = /^[A-Za-z]+/.exec(this.s.slice(this.i))[0];
      this.i += run.length;
      if (this.upright) out.push(mxEscape(run));
      else if (run.length >= 3) out.push(`<span class="mx-word">${mxEscape(run)}</span>`);
      else run.split('').forEach((letter) => out.push(`<i>${mxEscape(letter)}</i>`));
      return;
    }
    if (/[0-9]/.test(char)) {
      const number = /^\d+(?:\.\d+)?/.exec(this.s.slice(this.i))[0];
      this.i += number.length;
      out.push(number);
      return;
    }
    if (ARABIC.test(char)) {
      let end = this.i;
      while (end < this.n) {
        if (ARABIC.test(this.s[end])) end += 1;
        else if (this.s[end] === ' ' && end + 1 < this.n && ARABIC.test(this.s[end + 1])) end += 1;
        else break;
      }
      out.push(mxTextHtml(this.s.slice(this.i, end)));
      this.i = end;
      return;
    }
    this.i += 1;
    if (char === '$' || char === '`') return;
    if (char === '-') out.push('−');
    else if (char === '*') out.push('×');
    else if (char === '~') out.push(' ');
    else if (char === "'") out.push('′');
    else if (char === '=' || char === '<' || char === '>') out.push(`<span class="mx-rel">${mxEscape(char)}</span>​`);
    else if (char === '+') out.push('<span class="mx-bin">+</span>​');
    else if (char === '&') out.push('&amp;');
    else if (char >= 'α' && char <= 'ω') out.push(this.upright ? char : `<i>${char}</i>`);
    else out.push(mxEscape(char));
  }

  script(out) {
    let sup = null;
    let sub = null;
    while (this.i < this.n) {
      this.skipSpace();
      if (this.i >= this.n || (this.s[this.i] !== '^' && this.s[this.i] !== '_')) break;
      const kind = this.s[this.i];
      this.i += 1;
      this.skipSpace();
      const argument = this.argument();
      if (kind === '^') sup = argument; else sub = argument;
    }
    let base = '';
    let atLimit = false;
    if (out.length) {
      atLimit = Boolean(this.limitAt && this.limitAt.out === out && this.limitAt.index === out.length - 1);
      base = out.pop();
    }
    if (atLimit) {
      const top = sup ? `<span class="mx-limit-top">${sup}</span>` : '';
      const bottom = sub ? `<span class="mx-limit-bottom">${sub}</span>` : '';
      out.push(`<span class="mx-limits">${top}<span class="mx-limit-base">${base}</span>${bottom}</span>`);
    } else if (sup && sub) {
      out.push(base, `<span class="mx-scripts"><sup>${sup}</sup><sub>${sub}</sub></span>`);
    } else if (sup) out.push(base, `<sup>${sup}</sup>`);
    else if (sub) out.push(base, `<sub>${sub}</sub>`);
    else out.push(base);
  }

  delimiter() {
    this.skipSpace();
    if (this.i >= this.n) return '';
    if (this.s[this.i] === BS) {
      const match = COMMAND.exec(this.s.slice(this.i));
      if (match) {
        this.i += match[0].length;
        const token = match[0];
        const name = match[1];
        if (Object.prototype.hasOwnProperty.call(MX.delimiters, token)) return MX.delimiters[token];
        return MX.ordinary[name] || MX.relations[name] || '';
      }
    }
    const char = this.s[this.i];
    this.i += 1;
    if (Object.prototype.hasOwnProperty.call(MX.delimiters, char)) return MX.delimiters[char];
    return /[A-Za-z0-9]/.test(char) ? '' : char;
  }

  command(out) {
    const match = COMMAND.exec(this.s.slice(this.i));
    if (!match) { this.i += 1; return; }
    this.i += match[0].length;
    let name = match[1];
    if (name === BS) {
      const rest = ROW_SPACING.exec(this.s.slice(this.i));
      if (rest) this.i += rest[0].length;
      out.push('<br>');
      return;
    }
    if (name.length === 1 && !/\p{L}/u.test(name)) { this.escapedCharacter(out, name); return; }
    if (name.endsWith('*')) name = name.slice(0, -1);
    this.named(out, this.resolveGlued(name));
  }

  resolveGlued(name) {
    if (MX_KNOWN.has(name)) return name;
    for (let cut = name.length - 1; cut > 1; cut -= 1) {
      const prefix = name.slice(0, cut);
      let rest = name.slice(cut);
      if (!MX_KNOWN.has(prefix)) continue;
      if (MX_KNOWN.has(rest)) rest = BS + rest;
      else if (rest.length > 2) continue;
      const start = this.i - name.length + cut;
      this.s = `${this.s.slice(0, start)} ${rest} ${this.s.slice(this.i)}`;
      this.n = this.s.length;
      this.i = start;
      return prefix;
    }
    return name;
  }

  escapedCharacter(out, char) {
    if (Object.prototype.hasOwnProperty.call(MX.spacing, char)) out.push(MX.spacing[char]);
    else if (char === '|') out.push('‖');
    else if (char === '-' || char === '/' || char === '\n') return;
    else out.push(mxEscape(char));
  }

  named(out, name) {
    const has = (table) => Object.prototype.hasOwnProperty.call(table, name);
    if (has(MX.spacing)) out.push(MX.spacing[name]);
    else if (has(MX.greek)) out.push(this.upright ? MX.greek[name] : `<i>${MX.greek[name]}</i>`);
    else if (has(MX.ordinary)) out.push(mxEscape(MX.ordinary[name]));
    else if (name === 'circ' && (!out.length || /[\d)]\s*$/.test(out.slice(-3).join('').replace(/<[^>]*>/g, '')))) {
      while (out.length && !out[out.length - 1].trim()) out.pop();
      out.push('°');
    } else if (has(MX.binary)) out.push(`<span class="mx-bin">${mxEscape(MX.binary[name])}</span>​`);
    else if (has(MX.relations)) out.push(`<span class="mx-rel">${mxEscape(MX.relations[name])}</span>​`);
    else if (has(MX.bigOperators)) {
      out.push(`<span class="mx-op">${MX.bigOperators[name]}</span>`);
      if (MX_LIMIT_OPERATORS.has(name)) this.limitAt = { out, index: out.length - 1 };
    } else if (MX_FUNCTIONS.has(name)) {
      out.push(`<span class="mx-fn">${name}</span>`);
      if (MX_LIMIT_FUNCTIONS.has(name)) this.limitAt = { out, index: out.length - 1 };
      else if (!['(', '[', '|'].includes(this.s.slice(this.i, this.i + 1))) out.push(' ');
    } else if (['frac', 'dfrac', 'tfrac', 'cfrac'].includes(name)) {
      const numerator = this.argument();
      const denominator = this.argument();
      out.push(`<span class="mx-frac"><span class="mx-num">${numerator}</span><span class="mx-den">${denominator}</span></span>`);
    } else if (['binom', 'dbinom', 'tbinom'].includes(name)) {
      const top = this.argument();
      const bottom = this.argument();
      out.push(`<span class="mx-delim">(</span><span class="mx-frac mx-frac--bare"><span class="mx-num">${top}</span><span class="mx-den">${bottom}</span></span><span class="mx-delim">)</span>`);
    } else if (name === 'sqrt') {
      const index = this.rawOptional();
      const radicand = this.argument();
      const indexHtml = index ? `<sup class="mx-root-index">${this.child(index)}</sup>` : '';
      out.push(`<span class="mx-root">${indexHtml}<span class="mx-radical">√</span><span class="mx-radicand">${radicand}</span></span>`);
    } else if (MX_TEXT.has(name)) {
      let grouped = this.rawGroup();
      if (grouped === null) {
        const word = ARABIC_RUN.exec(this.s.slice(this.i));
        if (word) { grouped = word[0]; this.i += word[0].length; }
        else if (this.s.slice(this.i, this.i + 1) === BS) grouped = '';
        else grouped = this.rawArgument();
      }
      out.push(mxTextHtml(grouped));
    } else if (MX_UPRIGHT.has(name)) {
      this.upright += 1;
      const inner = this.argument();
      this.upright -= 1;
      out.push(`<span class="mx-rm">${inner}</span>`);
    } else if (MX_BOLD.has(name)) {
      this.bold += 1;
      const inner = this.argument();
      this.bold -= 1;
      out.push(`<b>${inner}</b>`);
    } else if (MX_ITALIC.has(name)) out.push(`<i>${this.argument()}</i>`);
    else if (name === 'mathbb') {
      out.push(mxEscape(Array.from(this.rawArgument()).map((char) => MX.blackboard[char] || char).join('')));
    } else if (MX_PLAIN.has(name)) out.push(this.argument());
    else if (has(MX.accents)) out.push(`<span class="mx-acc mx-acc--${MX.accents[name]}">${this.argument()}</span>`);
    else if (MX_SIZING.has(name)) {
      const delimiter = this.delimiter();
      if (delimiter) out.push(`<span class="mx-delim">${mxEscape(delimiter)}</span>`);
    } else if (name === 'begin') this.environment(out);
    else if (name === 'end') this.rawGroup();
    else if (name === 'tag') out.push(`<span class="mx-tag">(${this.argument()})</span>`);
    else if (['boxed', 'fbox', 'framebox'].includes(name)) out.push(`<span class="mx-boxed">${this.argument()}</span>`);
    else if (['cancel', 'xcancel', 'bcancel', 'sout'].includes(name)) out.push(`<span class="mx-cancel">${this.argument()}</span>`);
    else if (name === 'textcolor' || name === 'colorbox') { this.rawGroup(); out.push(this.argument()); }
    else if (name === 'overset' || name === 'stackrel') {
      const above = this.argument();
      const base = this.argument();
      out.push(`<span class="mx-limits"><span class="mx-limit-top">${above}</span><span class="mx-limit-base">${base}</span></span>`);
    } else if (name === 'underset') {
      const below = this.argument();
      const base = this.argument();
      out.push(`<span class="mx-limits"><span class="mx-limit-base">${base}</span><span class="mx-limit-bottom">${below}</span></span>`);
    } else if (name === 'overbrace' || name === 'underbrace') {
      out.push(`<span class="mx-${name === 'overbrace' ? 'over' : 'under'}brace">${this.argument()}</span>`);
      this.limitAt = { out, index: out.length - 1 };
    } else if (has(MX.longArrows)) {
      const below = this.rawOptional();
      const above = this.argument();
      const belowHtml = below ? `<span class="mx-limit-bottom">${this.child(below)}</span>` : '';
      out.push(`<span class="mx-limits"><span class="mx-limit-top">${above}</span><span class="mx-limit-base mx-rel">${MX.longArrows[name]}</span>${belowHtml}</span>`);
    } else if (name === 'bmod' || name === 'mod') out.push(' mod ');
    else if (name === 'pmod' || name === 'pod') out.push(` (mod ${this.argument()})`);
    else if (name === 'substack') out.push(this.grid(mxSplitRows(this.rawGroup() || ''), 'stack', ''));
    else if (MX_SKIP_ARG.has(name)) { this.rawOptional(); this.rawGroup(); }
    else if (MX_SKIP_BARE.has(name)) return;
    else out.push(`<span class="mx-fn">${mxEscape(name)}</span> `);
  }

  environment(out) {
    const name = (this.rawGroup() || '').trim();
    let body;
    [body, this.i] = mxEnvironmentBody(this.s, this.i, name);
    const base = name.replace(/\*+$/, '');
    if (['array', 'tabular', 'alignedat', 'subarray'].includes(base)) {
      this.rawGroup();
      body = body.replace(/^\s+/, '');
      if (body.startsWith('{')) {
        let depth = 0;
        for (let cursor = 0; cursor < body.length; cursor += 1) {
          if (body[cursor] === '{') depth += 1;
          if (body[cursor] === '}') depth -= 1;
          if (depth === 0) { body = body.slice(cursor + 1); break; }
        }
      }
    }
    const rows = mxSplitRows(body);
    const has = (key) => Object.prototype.hasOwnProperty.call(MX.matrixDelimiters, key);
    if (has(name) || has(base)) out.push(this.grid(rows, 'matrix', has(name) ? MX.matrixDelimiters[name] : MX.matrixDelimiters[base]));
    else if (['cases', 'dcases', 'rcases'].includes(base)) out.push(this.grid(rows, 'cases', `brace-${base === 'rcases' ? 'right' : 'left'}`));
    else if (['equation', 'displaymath', 'math', 'split', 'gathered', 'gather'].includes(base)) out.push(this.grid(rows, 'gather', ''));
    else out.push(this.grid(rows, 'align', ''));
  }

  grid(rows, kind, delimiter) {
    const renderedRows = rows.map((row) => {
      const cells = row.map((raw, column) => {
        let side = '';
        if (kind === 'align') side = column % 2 === 0 ? ' mx-cell--r' : ' mx-cell--l';
        else if (kind === 'cases') side = ' mx-cell--l';
        return `<span class="mx-cell${side}">${this.child(raw)}</span>`;
      });
      return `<span class="mx-mrow">${cells.join('')}</span>`;
    });
    const grid = `<span class="mx-grid">${renderedRows.join('')}</span>`;
    const height = Math.min(Math.max(rows.length, 1), 4);
    let left = '';
    let right = '';
    const brace = (glyph) => `<span class="mx-bigbrace mx-bigbrace--${height}">${glyph}</span>`;
    if (delimiter.startsWith('brace-')) {
      if (delimiter.endsWith('left')) left = brace('{'); else right = brace('}');
    } else if (delimiter === 'B') { left = brace('{'); right = brace('}'); }
    else if (delimiter) {
      left = `<span class="mx-bracket mx-bracket--${delimiter}-l"></span>`;
      right = `<span class="mx-bracket mx-bracket--${delimiter}-r"></span>`;
    }
    return `<span class="mx-matrix mx-matrix--${kind}">${left}${grid}${right}</span>​`;
  }
}

function typesetMath(source) {
  return `<span class="mx" dir="ltr">${new MxTypesetter(mxRepair(source)).sequence()}</span>`;
}
"""


def math_engine_js() -> str:
    tables = {
        "greek": m._GREEK_VARIABLES,
        "ordinary": m._ORDINARY,
        "binary": m._BINARY,
        "relations": m._RELATIONS,
        "bigOperators": m._BIG_OPERATORS,
        "limitOperators": sorted(m._LIMIT_OPERATORS),
        "functions": sorted(m._FUNCTIONS),
        "limitFunctions": sorted(m._LIMIT_FUNCTIONS),
        "delimiters": m._DELIMITERS,
        "spacing": m._SPACING,
        "skipArgument": sorted(m._SKIP_WITH_ARGUMENT),
        "skipBare": sorted(m._SKIP_BARE),
        "upright": sorted(m._UPRIGHT_COMMANDS),
        "bold": sorted(m._BOLD_COMMANDS),
        "italic": sorted(m._ITALIC_COMMANDS),
        "plain": sorted(m._PLAIN_WRAPPERS),
        "text": sorted(m._TEXT_COMMANDS),
        "blackboard": m._BLACKBOARD,
        "accents": m._ACCENTS,
        "matrixDelimiters": m._MATRIX_DELIMITERS,
        "longArrows": m._LONG_ARROWS,
        "known": sorted(m._known_command_names()),
        "controlRepairs": [list(item) for item in m._CONTROL_REPAIRS],
    }
    return _JS_BODY.replace("__TABLES__", json.dumps(tables, ensure_ascii=False))
