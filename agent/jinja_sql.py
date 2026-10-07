"""Turn dbt (Jinja) SQL into SQL a parser can read, without inventing logic.

`strip_jinja` deletes Jinja outright. On real dbt projects that leaves SQL
the parser cannot read at all: a `{{ config(post_hook='{{ macro() }}') }}`
header loses its inner `}}` and strands a quote; a macro used as a CTE body
leaves `with a as ( )`; `{% if %}` / `{% for %}` tags leave a trailing
`UNION ALL`. The model then reports "could not parse" and no AST check runs.

This module keeps the SQL around the Jinja and replaces each Jinja construct
with something syntactically valid in the position it occupies:

  {{ ref('x') }} / {{ source('a','x') }}  the relation name (as before)
  {{ config(...) }}                       removed: it is not part of the query
  {{ macro(args) }} as a statement        a placeholder SELECT
  {{ macro(args) }} in an expression      an ordinary function call, arguments kept
  {{ anything_else }} in an expression    a placeholder identifier
  {{ ... }} inside a string literal       a placeholder inside the quotes
  {% ... %} tags                          removed, the SQL they wrap is kept

Nothing is deleted to make the parser happy: the only repairs are to
structure Jinja itself created (a dangling set operator or boolean left by a
removed `{% if %}` branch, for instance).

Placeholders are deliberately recognisable (`relium_jinja*`). A value Relium
cannot see is not evidence of a risk, so detectors skip expressions that
contain one instead of asserting a finding about a macro's output.
"""

import re

#: Identifier standing in for a Jinja expression used as a value.
EXPRESSION_PLACEHOLDER = "relium_jinja_expr"
#: Text standing in for Jinja interpolated inside a SQL string literal.
STRING_PLACEHOLDER = "relium_jinja_value"
#: Statement standing in for a macro that generates a whole query.
STATEMENT_PLACEHOLDER = "select null as relium_jinja_block"
#: Any placeholder shares this prefix, so detectors can recognise one.
PLACEHOLDER_PREFIX = "relium_jinja"
#: A macro call keeps its name behind this prefix: `{{ f(x) }}` becomes
#: `relium_jinja_call_f(x)`. The call and its arguments stay visible, but
#: what the macro evaluates to does not, so detectors treat it as unknown.
CALL_PLACEHOLDER_PREFIX = "relium_jinja_call_"

#: The model has no SQL of its own to analyze (its body is a macro call).
NO_SQL_TO_ANALYZE = "no_sql_to_analyze"

_QUOTES = "'\"`"
_REF_RE = re.compile(r"^ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)$", re.IGNORECASE | re.DOTALL)
_SOURCE_RE = re.compile(
    r"^source\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)$", re.IGNORECASE | re.DOTALL)
_CONFIG_RE = re.compile(r"^config\s*\(", re.IGNORECASE)
_MACRO_CALL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*)\)$", re.DOTALL)
_SQL_KEYWORD_RE = re.compile(
    r"\b(select|with|insert|update|delete|merge|create|union|from)\b", re.IGNORECASE)
_MACRO_KWARG_RE = re.compile(r"(?<![<>!=])=(?!=)")


def _find_close(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past `closer` that matches the `opener` at `start`.

    Nested openers are counted, and quoted sections are skipped, so the
    inner `}}` of `{{ config(post_hook='{{ macro() }}') }}` does not end the
    outer expression. Returns len(text) when it is never closed.
    """
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _skip_string(text, index)
            continue
        if text.startswith(opener, index):
            depth += 1
            index += len(opener)
            continue
        if text.startswith(closer, index):
            depth -= 1
            index += len(closer)
            if depth == 0:
                return index
            continue
        index += 1
    return len(text)


def _skip_string(text: str, start: int) -> int:
    """Index just past the string literal starting at `start`."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _statement_position(before: str, after: str) -> bool:
    """True when a Jinja expression stands where a whole query belongs."""
    head = before.rstrip()
    tail = after.lstrip()
    if not head or head.endswith("("):
        return tail.startswith(")") or not tail
    return bool(re.search(r"\b(union|all|intersect|except)\s*$", head, re.IGNORECASE))


def _replace_expression(inner: str, before: str, after: str) -> str:
    inner = inner.strip()
    if not inner:
        return " "
    ref = _REF_RE.match(inner) or _SOURCE_RE.match(inner)
    if ref:
        return ref.group(1)
    if _CONFIG_RE.match(inner):
        return " "
    if _statement_position(before, after):
        return f" {STATEMENT_PLACEHOLDER} "
    macro = _MACRO_CALL_RE.match(inner)
    if macro:
        name, arguments = macro.group(1), macro.group(2).strip()
        # dbt keyword arguments are not SQL; the call keeps its shape without
        # them rather than parsing as a comparison.
        if _MACRO_KWARG_RE.search(arguments) or "{" in arguments:
            arguments = ""
        else:
            arguments = normalise(arguments)
        # The macro's name is kept so evidence stays readable, behind the
        # placeholder prefix so detectors know the value is not visible.
        return f" {CALL_PLACEHOLDER_PREFIX}{name.replace('.', '_')}({arguments}) "
    return f" {EXPRESSION_PLACEHOLDER} "


_TAG_NAME_RE = re.compile(r"^-?\s*(\w+)")


def normalise(sql: str) -> str:
    """Jinja-free SQL text that keeps the surrounding statement intact.

    Only one branch of an `{% if %}` survives. Both branches kept would put
    two alternatives side by side (`AND a = 1 AND b = 2` where the author
    meant one or the other), which is not SQL the author could ever run;
    the first branch is the one whose structure is preserved.
    """
    text = str(sql or "")
    out = []
    blocks: list[str] = []
    skipping_from: int | None = None
    index = 0
    while index < len(text):
        emit = skipping_from is None
        char = text[index]
        # Comments are passed through untouched: an apostrophe in `-- don't`
        # does not open a string literal, and Jinja inside a comment is not
        # part of the query.
        if text.startswith("--", index):
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            if emit:
                out.append(text[index:end])
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index)
            end = len(text) if end == -1 else end + 2
            if emit:
                out.append(text[index:end])
            index = end
            continue
        if char in _QUOTES:
            end = _skip_string(text, index)
            if emit:
                out.append(_normalise_string_literal(text[index:end]))
            index = end
            continue
        if text.startswith("{#", index):
            end = text.find("#}", index)
            index = len(text) if end == -1 else end + 2
            if emit:
                out.append(" ")
            continue
        if text.startswith("{{", index):
            end = _find_close(text, index, "{{", "}}")
            inner = text[index + 2: max(index + 2, end - 2)]
            if emit:
                out.append(_replace_expression(inner, "".join(out), text[end:]))
            index = end
            continue
        if text.startswith("{%", index):
            end = text.find("%}", index)
            end = len(text) if end == -1 else end + 2
            name = (_TAG_NAME_RE.match(text[index + 2: end - 2]) or [None, ""])[1].lower()
            if name in ("if", "for"):
                blocks.append(name)
            elif name in ("else", "elif") and blocks and blocks[-1] == "if" and skipping_from is None:
                skipping_from = len(blocks)
            elif name == "endif":
                if skipping_from is not None and skipping_from == len(blocks):
                    skipping_from = None
                if blocks and blocks[-1] == "if":
                    blocks.pop()
            elif name == "endfor":
                if blocks and blocks[-1] == "for":
                    blocks.pop()
            if emit:
                # The tag goes; whatever SQL it wrapped stays.
                out.append(" ")
            index = end
            continue
        if emit:
            out.append(char)
        index += 1
    return _repair_structure("".join(out))


#: Structure that only a removed Jinja tag can leave behind.
_REPAIRS = (
    # `{% if not loop.last %} union all {% endif %}` at the end of a loop body
    (re.compile(r"\b(union|intersect|except)(\s+all)?\s*(?=\)|$)", re.IGNORECASE), ""),
    (re.compile(r"^\s*(union|intersect|except)(\s+all)?\b", re.IGNORECASE), ""),
    # `where {% if ... %}a = 1{% endif %}` and friends
    (re.compile(r"\b(where|and|or|on)\s*(?=\)|$)", re.IGNORECASE), ""),
    (re.compile(r"\b(where|having)\s+(and|or)\b", re.IGNORECASE), r"\1"),
    (re.compile(r"\b(and|or)\s+(and|or)\b", re.IGNORECASE), r"\1"),
    (re.compile(r"\(\s*(and|or)\b", re.IGNORECASE), "("),
    # `select a, {% if ... %}b,{% endif %} from t`
    (re.compile(r",\s*(?=\)|$)"), ""),
    (re.compile(r",\s*,"), ","),
    (re.compile(r",\s*(?=\bfrom\b)", re.IGNORECASE), " "),
    (re.compile(r"\bselect\s*,", re.IGNORECASE), "select "),
)


def _repair_structure(text: str) -> str:
    """Repair only what removing a Jinja tag can break.

    Each rule targets a specific leftover -- a set operator with nothing to
    combine, a boolean with nothing to test, a list with a missing element.
    None of them remove SQL the author wrote.
    """
    for _ in range(3):
        before = text
        for pattern, replacement in _REPAIRS:
            text = pattern.sub(replacement, text)
        if text == before:
            break
    return text


def _normalise_string_literal(literal: str) -> str:
    """Keep a string literal a string literal even if Jinja is inside it."""
    if "{{" not in literal and "{%" not in literal:
        return literal
    quote = literal[0]
    body = literal[1:-1] if len(literal) > 1 and literal.endswith(quote) else literal[1:]
    body = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", STRING_PLACEHOLDER, body, flags=re.DOTALL)
    return f"{quote}{body}{quote}"


def normalise_sql_for_analysis(sql: str) -> tuple[str, str | None]:
    """(SQL text to parse, status).

    The status is NO_SQL_TO_ANALYZE when the model has no SQL of its own --
    a body that is only a macro call. That is a coverage limit, not a parse
    failure: there is nothing the author wrote for Relium to read.
    """
    text = normalise(sql)
    if not text.strip():
        return text, NO_SQL_TO_ANALYZE
    without_placeholders = text.replace(STATEMENT_PLACEHOLDER, " ")
    if not _SQL_KEYWORD_RE.search(without_placeholders):
        return text, NO_SQL_TO_ANALYZE
    return text, None


def contains_placeholder(text) -> bool:
    """True when this SQL fragment stands in for something Relium cannot see."""
    return PLACEHOLDER_PREFIX in str(text or "").lower()
