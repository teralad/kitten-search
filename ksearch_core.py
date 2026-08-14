#!/usr/bin/env python3
"""Pure-logic core for the ksearch kitty kitten.

This module deliberately has **zero** kitty imports so it can be unit tested
with a plain CPython interpreter.  It owns:

* parsing the output of ``kitty @ get-text --extent=all --add-wrap-markers``
  into an exact screen-row model,
* calibrating the true buffer height (kitty strips trailing blank rows),
* the three search engines (literal smart-case / regex / fuzzy),
* the scroll arithmetic used to place a match at a chosen screen row,
* text windowing/truncation helpers used by the renderer.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Iterator, NamedTuple, Sequence

__all__ = [
    'MODES',
    'Line',
    'Match',
    'Buffer',
    'SearchError',
    'align_bottom',
    'all_matches_pattern',
    'build_buffer',
    'current_match_pattern',
    'marker_spec',
    'is_case_sensitive',
    'mode_label',
    'next_mode',
    'normalize_newlines',
    'rows_of',
    'scroll_offset_for_row',
    'search',
    'split_cursor',
    'strip_ansi',
    'visible_slice',
    'window_text',
]

# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

MODES = ('smart', 'regex', 'fuzzy')

_MODE_LABELS = {
    'smart': 'smart',
    'regex': 'regex',
    'fuzzy': 'fuzzy',
}

_MODE_GLYPHS = {
    'smart': '=',
    'regex': '.*',
    'fuzzy': '~',
}


def next_mode(mode: str) -> str:
    """Cycle to the next search mode."""
    try:
        i = MODES.index(mode)
    except ValueError:
        return MODES[0]
    return MODES[(i + 1) % len(MODES)]


def mode_label(mode: str) -> str:
    return _MODE_LABELS.get(mode, mode)


def mode_glyph(mode: str) -> str:
    return _MODE_GLYPHS.get(mode, '?')


class SearchError(Exception):
    """Raised when a user supplied pattern cannot be compiled."""


# --------------------------------------------------------------------------
# ANSI / cursor handling
# --------------------------------------------------------------------------

# Matches CSI sequences plus the ``CSI Ps SP q`` cursor-shape form that
# ``get-text --add-cursor`` appends.
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]')
_CURSOR_POS_RE = re.compile(r'\x1b\[(\d+);(\d+)H')

# ``--add-cursor`` appends at most a handful of short escapes; 128 bytes is a
# generous bound that keeps us from ever touching real buffer content.
_CURSOR_TAIL_BYTES = 128


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def split_cursor(raw: str) -> tuple[str, int | None, int | None]:
    """Split the cursor escape block appended by ``get-text --add-cursor``.

    Returns ``(text_without_cursor_block, cursor_y, cursor_x)`` with both
    coordinates zero-based and screen relative.  If no cursor block is present
    the text is returned untouched and the coordinates are ``None``.
    """
    tail_start = max(0, len(raw) - _CURSOR_TAIL_BYTES)
    tail = raw[tail_start:]
    last = None
    for m in _CURSOR_POS_RE.finditer(tail):
        last = m
    if last is None:
        return raw, None, None
    cy = int(last.group(1)) - 1
    cx = int(last.group(2)) - 1
    return raw[:tail_start] + strip_ansi(tail), cy, cx


# --------------------------------------------------------------------------
# buffer model
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Line:
    """A *logical* line, i.e. one ``\\n`` terminated line of the buffer.

    A logical line occupies one or more screen rows when it is wider than the
    window.  ``row_start`` is the absolute index of its first screen row and
    ``row_cum`` holds the exclusive prefix sums of the per-row lengths, which
    lets us map a character offset back to the exact screen row it lands on.
    """

    text: str
    row_start: int
    row_cum: list[int] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.row_cum)

    def row_for_col(self, col: int) -> int:
        """Absolute screen row containing character offset ``col``."""
        if len(self.row_cum) == 1:
            return self.row_start
        # row_cum is the start offset of each row within the logical line.
        k = bisect_right(self.row_cum, col) - 1
        if k < 0:
            k = 0
        return self.row_start + k


class Match(NamedTuple):
    """A single hit.

    ``line`` indexes into :attr:`Buffer.lines`, ``start``/``end`` are character
    offsets inside that logical line and ``row`` is the absolute screen row the
    hit starts on (which is what the scroll arithmetic consumes).
    """

    line: int
    start: int
    end: int
    row: int


@dataclass(slots=True)
class Buffer:
    rows: list[str]
    lines: list[Line]
    joined: str
    line_starts: list[int]
    screen_rows: int
    screen_cols: int
    #: Index into :attr:`rows` of the row displayed at the top of the parent
    #: window when it is scrolled all the way down.  Everything about
    #: positioning is expressed relative to this, because it is measured rather
    #: than derived -- see :func:`align_bottom`.
    top_row: int = 0
    _lowered: str | None = None

    @property
    def joined_lower(self) -> str:
        if self._lowered is None:
            self._lowered = self.joined.lower()
        return self._lowered

    @property
    def history_rows(self) -> int:
        """Maximum scroll offset, i.e. rows above the bottom-most screenful."""
        return max(0, self.top_row)

    def line_for_offset(self, off: int) -> tuple[int, int]:
        """Map an offset in :attr:`joined` to ``(line_index, column)``."""
        i = bisect_right(self.line_starts, off) - 1
        if i < 0:
            i = 0
        return i, off - self.line_starts[i]

    def row_for(self, line_idx: int, col: int) -> int:
        return self.lines[line_idx].row_for_col(col)


def normalize_newlines(text: str) -> str:
    """Collapse kitty's hard line breaks to a bare ``\\n``.

    With ``--add-wrap-markers`` kitty terminates *every* line with ``\\r\\n``
    and marks a soft wrap with a lone ``\\r``.  Treating every ``\\r`` as a wrap
    -- or letting Python's universal-newline translation rewrite them -- makes
    each line look like two screen rows and doubles every row index.
    """
    return text.replace('\r\n', '\n')


def rows_of(text: str) -> list[str]:
    """Split ``get-text`` output into exact screen rows.

    A lone ``\\r`` is a soft wrap inserted by ``--add-wrap-markers``; ``\\r\\n``
    and ``\\n`` are real line breaks.
    """
    text = normalize_newlines(text)
    if text.endswith('\n'):
        text = text[:-1]
    out: list[str] = []
    for logical in text.split('\n'):
        out.extend(logical.split('\r'))
    return out


def build_buffer(
    raw: str,
    screen_rows: int,
    screen_cols: int,
    top_row: int | None = None,
) -> Buffer:
    """Parse ``get-text --extent=all --add-wrap-markers`` output.

    A lone ``\\r`` marks a soft wrap (same logical line, next screen row) while
    ``\\r\\n`` marks a real line break.  Keeping the distinction lets us search
    logical lines -- so a hit is never split by the window edge -- while still
    knowing the exact screen row of every character.
    """
    raw, _cy, _cx = split_cursor(raw)
    raw = normalize_newlines(raw)
    if raw.endswith('\n'):
        raw = raw[:-1]

    rows: list[str] = []
    lines: list[Line] = []
    line_starts: list[int] = []
    offset = 0

    for logical in raw.split('\n'):
        parts = logical.split('\r')
        row_start = len(rows)
        rows.extend(parts)
        cum: list[int] = []
        acc = 0
        for p in parts:
            cum.append(acc)
            acc += len(p)
        text = logical.replace('\r', '')
        lines.append(Line(text=text, row_start=row_start, row_cum=cum))
        line_starts.append(offset)
        offset += len(text) + 1  # +1 for the '\n' rejoin separator

    joined = '\n'.join(ln.text for ln in lines)

    if top_row is None:
        top_row = max(0, len(rows) - screen_rows)

    return Buffer(
        rows=rows,
        lines=lines,
        joined=joined,
        line_starts=line_starts,
        screen_rows=screen_rows,
        screen_cols=screen_cols,
        top_row=top_row,
    )


def align_bottom(all_rows: Sequence[str], screen_rows_text: Sequence[str], screen_rows: int) -> int:
    """Index in ``all_rows`` of the top visible row when scrolled to the bottom.

    Deriving this from row counts does not work: ``get-text --extent=all``
    appends a phantom trailing row and strips blank rows, each of which shifts
    the answer by an amount that varies with what the shell last printed.  Every
    such error translates directly into the current hit landing on the wrong
    screen line.  So instead of modelling kitty's behaviour we measure it --
    ``--extent=screen`` tells us exactly which rows are visible, and the offset
    at which that block sits inside the full buffer is the answer.
    """
    n_all = len(all_rows)
    n_scr = len(screen_rows_text)
    if n_scr == 0 or n_all == 0:
        return max(0, n_all - screen_rows)
    block = list(screen_rows_text)
    guess = n_all - n_scr
    lo = max(0, guess - max(screen_rows, 8))
    hi = min(n_all - n_scr, guess + 8)
    # Prefer the most recent alignment: identical blocks can occur earlier in
    # the scrollback (repeated command output, runs of blank rows).
    for t in range(hi, lo - 1, -1):
        if list(all_rows[t:t + n_scr]) == block:
            return t
    return max(0, guess)


# --------------------------------------------------------------------------
# scrolling
# --------------------------------------------------------------------------


def scroll_offset_for_row(
    row: int,
    top_row: int,
    screen_rows: int,
    place: float = 0.5,
) -> int:
    """Scroll offset (rows above the bottom) that puts ``row`` on screen.

    With the parent scrolled down by ``s`` rows the top of the window shows
    ``top_row - s``, so landing ``row`` at screen position ``p`` needs
    ``s = top_row + p - row``.

    ``place`` is the fractional screen position to aim for -- ``0.0`` top,
    ``0.5`` centre, ``1.0`` bottom.  Always landing the current hit on the same
    screen row is the single biggest readability win over kitty's built-in
    ``scroll_to_mark``, which drops the hit wherever it happens to fall.
    """
    if screen_rows <= 0:
        return 0
    p = int(round((screen_rows - 1) * min(max(place, 0.0), 1.0)))
    offset = top_row + p - row
    return max(0, min(offset, max(0, top_row)))


# --------------------------------------------------------------------------
# search engines
# --------------------------------------------------------------------------

MAX_MATCHES = 200_000
_FUZZY_MAX_CHARS = 64


def is_case_sensitive(query: str) -> bool:
    """Smart case: a query typed in all lower case matches case insensitively."""
    return any(c.isupper() for c in query)


def _emit(buf: Buffer, spans: Iterable[tuple[int, int]]) -> list[Match]:
    out: list[Match] = []
    line_starts = buf.line_starts
    lines = buf.lines
    append = out.append
    for gstart, gend in spans:
        i = bisect_right(line_starts, gstart) - 1
        if i < 0:
            i = 0
        base = line_starts[i]
        start = gstart - base
        end = gend - base
        ln = lines[i]
        if end > len(ln.text):
            end = len(ln.text)
        if end <= start:
            continue
        append(Match(i, start, end, ln.row_for_col(start)))
        if len(out) >= MAX_MATCHES:
            break
    return out


def _literal_spans(hay: str, needle: str) -> Iterator[tuple[int, int]]:
    n = len(needle)
    pos = 0
    find = hay.find
    while True:
        i = find(needle, pos)
        if i < 0:
            return
        yield i, i + n
        pos = i + n


def _compile_fuzzy(query: str, flags: int) -> re.Pattern[str]:
    chars = [c for c in query if not c.isspace()][:_FUZZY_MAX_CHARS]
    if not chars:
        raise SearchError('empty fuzzy query')
    # ``[^\n]*?`` keeps a fuzzy hit inside a single logical line and the lazy
    # quantifier keeps the highlighted span as tight as possible.
    pattern = '[^\\n]*?'.join(re.escape(c) for c in chars)
    return re.compile(pattern, flags)


def search(buf: Buffer, query: str, mode: str = 'smart') -> list[Match]:
    """Run ``query`` over ``buf`` and return hits in buffer order.

    All three engines run a single C-level regex/``str.find`` pass over the
    whole buffer joined into one string, then map offsets back to lines with a
    bisect.  That is what makes typing feel instant even on a 100k line
    scrollback -- the previous implementation issued a remote-control round
    trip per keystroke instead.
    """
    if not query:
        return []

    cs = is_case_sensitive(query)

    if mode == 'smart':
        if '\n' in query:
            return []
        hay = buf.joined if cs else buf.joined_lower
        needle = query if cs else query.lower()
        return _emit(buf, _literal_spans(hay, needle))

    flags = re.MULTILINE if cs else (re.MULTILINE | re.IGNORECASE)

    if mode == 'regex':
        try:
            pat = re.compile(query, flags)
        except re.error as err:
            raise SearchError(str(err)) from err
    elif mode == 'fuzzy':
        pat = _compile_fuzzy(query, flags)
    else:
        raise SearchError(f'unknown mode: {mode}')

    def spans() -> Iterator[tuple[int, int]]:
        for m in pat.finditer(buf.joined):
            s, e = m.span()
            if e <= s:
                continue
            nl = buf.joined.find('\n', s, e)
            if nl != -1:
                e = nl  # never let a hit straddle a line break
                if e <= s:
                    continue
            yield s, e

    return _emit(buf, spans())


def highlight_spans(buf: Buffer, m: Match, query: str, mode: str) -> list[tuple[int, int]]:
    """Sub-spans of ``m`` to highlight, relative to the logical line.

    Literal and regex hits are one contiguous span.  Fuzzy hits highlight only
    the characters that actually matched, which is what makes fuzzy results
    readable rather than a smear across half the line.
    """
    if mode != 'fuzzy':
        return [(m.start, m.end)]
    text = buf.lines[m.line].text
    chars = [c for c in query if not c.isspace()][:_FUZZY_MAX_CHARS]
    if not chars:
        return [(m.start, m.end)]
    fold = not is_case_sensitive(query)
    hay = text.lower() if fold else text
    out: list[tuple[int, int]] = []
    pos = m.start
    for c in chars:
        c2 = c.lower() if fold else c
        i = hay.find(c2, pos, m.end)
        if i < 0:
            return [(m.start, m.end)]
        if out and out[-1][1] == i:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
        pos = i + 1
    return out


# --------------------------------------------------------------------------
# display helpers
# --------------------------------------------------------------------------

ELLIPSIS = '\u2026'


# --------------------------------------------------------------------------
# kitty marker patterns
# --------------------------------------------------------------------------


def all_matches_pattern(query: str, mode: str) -> str | None:
    """Regex handed to kitty so it paints every hit in mark colour 1."""
    if not query:
        return None
    ci = not is_case_sensitive(query)
    if mode == 'smart':
        body = re.escape(query)
    elif mode == 'regex':
        try:
            re.compile(query)
        except re.error:
            return None
        body = query
    elif mode == 'fuzzy':
        chars = [c for c in query if not c.isspace()][:32]
        if not chars:
            return None
        body = '[^\\n]*?'.join(re.escape(c) for c in chars)
    else:
        return None
    # Scoped inline flags: kitty applies one flag set to the whole combined
    # marker regex, so case folding has to be expressed per alternative.
    return f'(?i:{body})' if ci else body


def current_match_pattern(row_text: str, col: int, text: str) -> str | None:
    """Regex matching exactly one occurrence on one row, for mark colour 2.

    kitty markers are per-row regexes with no notion of "the third hit", so the
    current hit is singled out by anchoring on the literal text in front of it
    with a fixed-width lookbehind.  A lookbehind rather than a capture group
    because kitty highlights the whole match, and we do not want the prefix lit
    up too.
    """
    if not text:
        return None
    needle = re.escape(text)
    pattern = f'^{needle}' if col <= 0 else f'(?<={re.escape(row_text[:col])}){needle}'
    try:
        re.compile(pattern)
    except re.error:
        return None
    return pattern


def marker_spec(buf: Buffer, m: Match, query: str, mode: str) -> list[str] | None:
    """Build the argument list for ``kitty @ create-marker``.

    Colour 2 (the current hit) is listed first because kitty combines the
    groups into one alternation and resolves it left to right, so whichever
    pattern comes first wins where both match.
    """
    all_pat = all_matches_pattern(query, mode)
    if all_pat is None:
        return None
    spec: list[str] = ['regex']
    line = buf.lines[m.line]
    row = m.row
    if 0 <= row < len(buf.rows):
        row_text = buf.rows[row]
        k = row - line.row_start
        row_off = line.row_cum[k] if 0 <= k < len(line.row_cum) else 0
        col = m.start - row_off
        # a hit can straddle a soft wrap; only the part on this row is markable
        text = line.text[m.start:m.end][: max(0, len(row_text) - col)]
        cur = current_match_pattern(row_text, col, text)
        if cur is not None:
            spec.extend(['2', cur])
    spec.extend(['1', all_pat])
    return spec


def window_text(text: str, start: int, end: int, width: int, left_pad: int = 8) -> tuple[str, int]:
    """Slide a ``width`` wide window over ``text`` so ``start:end`` is visible.

    Returns the display string and the offset that was applied, so the caller
    can shift highlight spans by the same amount.  Long lines -- build logs,
    minified JSON -- are the exact case where kitty's native search leaves you
    staring at a hit that scrolled off the right edge.
    """
    if width <= 0:
        return '', 0
    text = text.replace('\t', '    ')
    if len(text) <= width:
        return text, 0

    if end - start >= width:
        offset = start
    elif start <= left_pad:
        offset = 0
    else:
        offset = start - left_pad
        # prefer showing the whole hit if it fits
        if offset + width < end:
            offset = end - width
        offset = max(0, min(offset, len(text) - width))

    chunk = text[offset:offset + width]
    if offset > 0:
        chunk = ELLIPSIS + chunk[1:]
    if offset + width < len(text):
        chunk = chunk[:-1] + ELLIPSIS
    return chunk, offset


def visible_slice(selected: int, total: int, height: int, scroll_off: int = 2) -> tuple[int, int]:
    """Window of list indices to render, keeping ``selected`` comfortably inside."""
    if height <= 0 or total <= 0:
        return 0, 0
    if total <= height:
        return 0, total
    off = min(scroll_off, max(0, (height - 1) // 2))
    start = selected - off
    start = max(0, min(start, total - height))
    if selected >= start + height - off:
        start = min(total - height, selected - height + 1 + off)
    start = max(0, min(start, total - height))
    return start, start + height
