#!/usr/bin/env python3
"""ksearch -- a fast scrollback search kitten for kitty.

Behaves like the search bar in Ghostty / iTerm2:

* every hit is highlighted at once and the counter tells you how many there are,
* the current hit is highlighted in a second colour and is always parked on the
  same screen row so your eye never has to hunt for it,
* ``ctrl+l`` opens an fzf-style result list that live-previews the parent
  window as you move through it, which is how you skim hundreds of hits without
  stepping through them one by one.

The reason this is quick where kitty's built-in search is not: the scrollback
is fetched **once** and then searched in-process, so a keystroke costs a
``str.find`` over a single joined string rather than a remote-control round trip
plus a full re-scan of the buffer inside kitty.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import deque
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksearch_core import (  # noqa: E402
    Buffer,
    Match,
    SearchError,
    align_bottom,
    build_buffer,
    highlight_spans,
    marker_spec,
    mode_glyph,
    mode_label,
    next_mode,
    rows_of,
    scroll_offset_for_row,
    search,
    split_cursor,
    visible_slice,
    window_text,
)

from kittens.tui.handler import Handler  # noqa: E402
from kittens.tui.line_edit import LineEdit  # noqa: E402
from kittens.tui.loop import Loop  # noqa: E402
from kittens.tui.operations import (  # noqa: E402
    clear_screen,
    cursor,
    set_line_wrapping,
    set_window_title,
    styled,
    write_to_clipboard,
)
from kitty.config import cached_values_for  # noqa: E402
from kitty.key_encoding import EventType  # noqa: E402
from kitty.typing_compat import KeyEventType, ScreenSize  # noqa: E402

# --------------------------------------------------------------------------
# tuning
# --------------------------------------------------------------------------

BAR_ROWS = 2
LIST_ROWS = 16
MARKER_DEBOUNCE = 0.09
SCROLL_DEBOUNCE = 0.016
# Literal search is a single str.find pass and is cheap enough to run on every
# keystroke; regex and fuzzy walk the whole buffer with the regex engine, so
# they get coalesced while you are still typing.
SEARCH_DEBOUNCE = 0.045
RESIZE_DEBOUNCE = 0.12
RESIZE_ATTEMPTS = 6
DCS_PROBE_TIMEOUT = 0.5
MAX_HISTORY = 50
PLACE_BAR = 0.5
PLACE_LIST = 0.5


DEBUG_LOG = os.environ.get('KSEARCH_DEBUG', '')


def debug(msg: str) -> None:
    """Append a line to ``$KSEARCH_DEBUG``; a no-op when the var is unset.

    A kitten owns the terminal, so print-debugging is not available; this is the
    only practical way to see what the event loop actually did.
    """
    if not DEBUG_LOG:
        return
    try:
        with open(DEBUG_LOG, 'a') as f:
            f.write(msg + '\n')
    except OSError:
        pass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


PLACE = min(max(_env_float('KSEARCH_PLACE', PLACE_BAR), 0.0), 1.0)


# --------------------------------------------------------------------------
# remote control transport
# --------------------------------------------------------------------------


class RemoteControl:
    """Sends kitty remote-control commands, preferring in-band DCS escapes.

    Escapes cost nothing: no process spawn, no socket, and replies arrive on the
    kitten's own input stream.  A subprocess fallback keeps the kitten working
    when in-band control is not permitted; we detect that by probing once at
    startup instead of guessing.
    """

    def __init__(self, handler: 'Search') -> None:
        self.handler = handler
        self.use_dcs = True
        self._pending: deque[Callable[[bool, Any], None]] = deque()

    # -- encoding ----------------------------------------------------------
    def send(
        self,
        name: str,
        payload: dict[str, Any],
        callback: Callable[[bool, Any], None] | None = None,
    ) -> None:
        if self.use_dcs:
            from kitty.remote_control import create_basic_command, encode_send

            cmd = create_basic_command(name, payload, no_response=callback is None)
            if callback is not None:
                self._pending.append(callback)
            self.handler.write(encode_send(cmd).decode('ascii'))
            return
        self._send_subprocess(name, payload, callback)

    def on_response(self, response: dict[str, Any]) -> None:
        if not self._pending:
            return
        cb = self._pending.popleft()
        cb(bool(response.get('ok')), response.get('data'))

    def drop_pending(self) -> list[Callable[[bool, Any], None]]:
        out = list(self._pending)
        self._pending.clear()
        return out

    # -- subprocess fallback ----------------------------------------------
    def _send_subprocess(
        self,
        name: str,
        payload: dict[str, Any],
        callback: Callable[[bool, Any], None] | None,
    ) -> None:
        args = _cli_args(name, payload)
        if args is None:
            if callback is not None:
                callback(False, None)
            return
        if callback is None:
            # fire and forget; waiting would stall the UI for no benefit
            try:
                subprocess.Popen(
                    ['kitty', '@', *args],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
            return
        try:
            proc = subprocess.run(
                ['kitty', '@', *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            callback(False, None)
            return
        if proc.returncode != 0:
            callback(False, None)
            return
        data = proc.stdout.decode('utf-8', 'replace')
        if name == 'ls':
            try:
                callback(True, json.loads(data))
            except ValueError:
                callback(False, None)
            return
        callback(True, data)


def _cli_args(name: str, payload: dict[str, Any]) -> list[str] | None:
    match = payload.get('match')
    if name == 'get-text':
        args = ['get-text', f'--extent={payload.get("extent", "screen")}']
        if payload.get('wrap_markers'):
            args.append('--add-wrap-markers')
        if payload.get('cursor'):
            args.append('--add-cursor')
        if match:
            args.append(f'--match={match}')
        return args
    if name == 'ls':
        return ['ls']
    if name == 'scroll-window':
        amount = payload.get('amount') or ['end', None]
        first = amount[0]
        if first in ('start', 'end'):
            spec = str(first)
        else:
            unit = amount[1] or 'l'
            n = float(first)
            spec = f'{abs(n):g}{unit}' + ('-' if n < 0 else '')
        args = ['scroll-window']
        if match:
            args.append(f'--match={match}')
        args.append(spec)
        return args
    if name == 'create-marker':
        args = ['create-marker']
        if match:
            args.append(f'--match={match}')
        args.extend(payload.get('marker_spec', []))
        return args
    if name == 'remove-marker':
        args = ['remove-marker']
        if match:
            args.append(f'--match={match}')
        return args
    if name == 'resize-window':
        args = ['resize-window', '--self', f'--axis={payload.get("axis", "vertical")}',
                f'--increment={payload.get("increment", 0)}']
        return args
    return None


# --------------------------------------------------------------------------
# the kitten
# --------------------------------------------------------------------------


class Search(Handler):
    def __init__(self, cached_values: dict[str, Any], window_id: int) -> None:
        self.cached_values = cached_values
        self.window_id = window_id
        self.match_spec = f'id:{window_id}'
        self.rc = RemoteControl(self)

        self.line_edit = LineEdit()
        self.mode: str = cached_values.get('mode', 'smart')
        if self.mode not in ('smart', 'regex', 'fuzzy'):
            self.mode = 'smart'
        self.history: list[str] = list(cached_values.get('history', []))[:MAX_HISTORY]
        self.history_pos = -1

        last = cached_values.get('last_search', '')
        if last:
            self.line_edit.add_text(last)
        self.preselect_all = bool(last)

        self.buf: Buffer | None = None
        self.matches: list[Match] = []
        self.idx = 0
        self.error = ''
        self.status = 'loading scrollback\u2026'

        self.list_mode = bool(cached_values.get('list_mode', False))
        self.parent_rows = 24
        self.parent_cols = 80
        self.cur_offset = 0
        self.markers_on = False

        self._marker_timer: Any = None
        self._scroll_timer: Any = None
        self._resize_timer: Any = None
        self._probe_timer: Any = None
        self._search_timer: Any = None
        self._want_offset: int | None = None
        self._last_marker_key: tuple[Any, ...] | None = None
        self._loading = False
        self._probed = False
        self._force_reload = False
        self._target_rows = LIST_ROWS if self.list_mode else BAR_ROWS
        self._resize_attempts = RESIZE_ATTEMPTS

    # -- lifecycle ---------------------------------------------------------
    def initialize(self) -> None:
        self.write(set_line_wrapping(False))
        self.write(set_window_title('search'))
        self.apply_pane_size()
        self._probe_timer = self.asyncio_loop.call_later(DCS_PROBE_TIMEOUT, self.on_probe_timeout)
        self.reload(force=True)
        self.draw_screen()

    def finalize(self) -> None:
        for t in (self._marker_timer, self._scroll_timer, self._resize_timer,
                  self._probe_timer, self._search_timer):
            if t is not None:
                t.cancel()

    def on_kitty_cmd_response(self, response: dict[str, Any]) -> None:
        if not self._probed:
            self._probed = True
            if self._probe_timer is not None:
                self._probe_timer.cancel()
                self._probe_timer = None
        self.rc.on_response(response)

    def on_probe_timeout(self) -> None:
        """No reply to in-band control -> fall back to spawning ``kitty @``."""
        self._probe_timer = None
        if self._probed:
            return
        self._probed = True
        self.rc.drop_pending()
        self.rc.use_dcs = False
        self._loading = False
        self.reload(force=True)

    # -- pane sizing -------------------------------------------------------
    def apply_pane_size(self) -> None:
        """Nudge this pane towards the row count the current mode wants.

        ``resize-window`` takes a relative increment and silently clamps at the
        layout's limits, so a single request routinely lands short.  We re-issue
        from ``on_resize`` until it converges, with a retry budget so a layout
        that simply cannot grow does not loop forever.
        """
        want = self._target_rows
        have = self.screen_size.rows
        if have == want or self._resize_attempts <= 0:
            return
        self._resize_attempts -= 1
        self.rc.send('resize-window', {'self': True, 'axis': 'vertical', 'increment': want - have})

    # -- loading -----------------------------------------------------------
    def reload(self, force: bool = False) -> None:
        if self._loading:
            return
        self._loading = True
        self._force_reload = force
        # A scroll queued against the old buffer would be applied as a delta
        # from a scroll position we are about to reset.
        if self._scroll_timer is not None:
            self._scroll_timer.cancel()
            self._scroll_timer = None
        self._want_offset = None
        self.rc.send('ls', {}, self.on_ls)

    def on_ls(self, ok: bool, data: Any) -> None:
        before = (self.parent_rows, self.parent_cols)
        if ok and data is not None:
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = None
            info = _find_window(data, self.window_id) if data else None
            if info:
                self.parent_rows = int(info.get('lines') or self.parent_rows)
                self.parent_cols = int(info.get('columns') or self.parent_cols)
        unchanged = (self.parent_rows, self.parent_cols) == before
        debug(f'on_ls ok={ok} parent={self.parent_rows}x{self.parent_cols} unchanged={unchanged}')
        if unchanged and self.buf is not None and not self._force_reload:
            # The parent did not reflow, so every row index still holds and
            # re-reading the whole scrollback would be pure latency.
            self._loading = False
            self.goto_current()
            return
        # Park the parent at the bottom so our offset bookkeeping starts at a
        # known point; kitty gives no way to query the current scroll position.
        self.scroll_amount(['end', None])
        self.cur_offset = 0
        self.rc.send(
            'get-text',
            {
                'match': self.match_spec,
                'extent': 'all',
                'ansi': False,
                'cursor': True,
                'wrap_markers': True,
                'clear_selection': False,
                'self': False,
            },
            self.on_all_text,
        )

    def on_all_text(self, ok: bool, data: Any) -> None:
        if not ok or not isinstance(data, str):
            self._loading = False
            self.status = ''
            self.error = 'could not read the scrollback of the parent window'
            self.draw_screen()
            return
        self._raw_all = data
        self.rc.send(
            'get-text',
            {
                'match': self.match_spec,
                'extent': 'screen',
                'ansi': False,
                'cursor': False,
                'wrap_markers': True,
                'clear_selection': False,
                'self': False,
            },
            self.on_screen_text,
        )

    def on_screen_text(self, ok: bool, data: Any) -> None:
        self._loading = False
        raw = getattr(self, '_raw_all', '')
        top_row = None
        if ok and isinstance(data, str):
            top_row = align_bottom(
                rows_of(split_cursor(raw)[0]), rows_of(data), self.parent_rows
            )
        self.buf = build_buffer(raw, self.parent_rows, self.parent_cols, top_row=top_row)
        debug(f'buffer rows={len(self.buf.rows)} top_row={self.buf.top_row} parent={self.parent_rows}x{self.parent_cols} ok={ok}')
        self.status = ''
        self.rerun(keep_index=True)

    # -- searching ---------------------------------------------------------
    @property
    def query(self) -> str:
        return self.line_edit.current_input

    def request_rerun(self) -> None:
        """Re-run the search, coalescing the expensive engines while typing."""
        if self._search_timer is not None:
            self._search_timer.cancel()
            self._search_timer = None
        if self.mode == 'smart':
            self.rerun()
            return
        self._search_timer = self.asyncio_loop.call_later(SEARCH_DEBOUNCE, self._fire_rerun)
        self.draw_screen()

    def _fire_rerun(self) -> None:
        self._search_timer = None
        self.rerun()

    def rerun(self, keep_index: bool = False, jump: bool = True) -> None:
        prev = self.matches[self.idx] if (keep_index and self.matches and self.idx < len(self.matches)) else None
        self.error = ''
        if self.buf is None or not self.query:
            self.matches = []
            self.idx = 0
            self.update_markers()
            self.draw_screen()
            if not self.query:
                self.request_offset(0)
            return
        try:
            self.matches = search(self.buf, self.query, self.mode)
        except SearchError as err:
            self.matches = []
            self.idx = 0
            self.error = str(err)
            self.update_markers()
            self.draw_screen()
            return
        if not self.matches:
            self.idx = 0
        elif prev is not None:
            self.idx = min(range(len(self.matches)), key=lambda i: abs(self.matches[i].row - prev.row))
        else:
            # Start from the newest hit: in a terminal what you just ran is
            # almost always what you are looking for.
            self.idx = len(self.matches) - 1
        self.update_markers()
        if jump:
            self.goto_current()
        self.draw_screen()

    def move(self, delta: int) -> None:
        if not self.matches:
            return
        n = len(self.matches)
        self.idx = (self.idx + delta) % n
        self.goto_current()
        self.update_markers()
        self.draw_screen()

    def move_to(self, index: int) -> None:
        if not self.matches:
            return
        self.idx = max(0, min(index, len(self.matches) - 1))
        self.goto_current()
        self.update_markers()
        self.draw_screen()

    # -- scrolling ---------------------------------------------------------
    def goto_current(self) -> None:
        if self.buf is None or not self.matches:
            return
        m = self.matches[self.idx]
        place = PLACE_LIST if self.list_mode else PLACE
        debug(f'goto row={m.row} top_row={self.buf.top_row} place={place}')
        self.request_offset(
            scroll_offset_for_row(m.row, self.buf.top_row, self.buf.screen_rows, place)
        )

    def request_offset(self, offset: int) -> None:
        """Coalesce scroll requests so held-down arrow keys never queue up."""
        self._want_offset = offset
        if self._scroll_timer is None:
            self._scroll_timer = self.asyncio_loop.call_later(SCROLL_DEBOUNCE, self.flush_scroll)

    def flush_scroll(self) -> None:
        self._scroll_timer = None
        target = self._want_offset
        self._want_offset = None
        if target is None or target == self.cur_offset:
            return
        if target == 0:
            self.scroll_amount(['end', None])
        else:
            # The wire format is a signed number of lines: negative scrolls up
            # into the scrollback.  Sending a delta from where we already are
            # keeps this to one command per jump.
            delta = target - self.cur_offset
            self.scroll_amount([float(-delta), 'l'])
        self.cur_offset = target

    def scroll_amount(self, amount: list[Any]) -> None:
        debug(f'scroll {amount} cur_offset={self.cur_offset}')
        self.rc.send('scroll-window', {'match': self.match_spec, 'self': False, 'amount': amount})

    # -- markers -----------------------------------------------------------
    def update_markers(self) -> None:
        if self._marker_timer is not None:
            self._marker_timer.cancel()
        self._marker_timer = self.asyncio_loop.call_later(MARKER_DEBOUNCE, self.flush_markers)

    def flush_markers(self) -> None:
        self._marker_timer = None
        if self.buf is None:
            return
        if not self.query or not self.matches:
            self.remove_markers()
            return
        spec = marker_spec(self.buf, self.matches[self.idx], self.query, self.mode)
        if spec is None:
            self.remove_markers()
            return
        key = tuple(spec)
        if key == self._last_marker_key:
            return
        self._last_marker_key = key
        self.rc.send('create-marker', {'match': self.match_spec, 'marker_spec': spec})
        self.markers_on = True

    def remove_markers(self) -> None:
        self._last_marker_key = None
        if not self.markers_on:
            return
        self.markers_on = False
        self.rc.send('remove-marker', {'match': self.match_spec})

    # -- rendering ---------------------------------------------------------
    def draw_screen(self) -> None:
        self.write(clear_screen())
        rows = max(1, self.screen_size.rows)
        cols = max(20, self.screen_size.cols)

        self.draw_prompt(cols)
        body = self.body_lines(rows - 1, cols)
        if body:
            with cursor(self.write):
                for ln in body:
                    self.print('')
                    self.write(ln)

    def draw_prompt(self, cols: int) -> None:
        plain, painted = self.status_text()
        prompt = f'{mode_glyph(self.mode)} '
        text = self.query
        if self.preselect_all and text:
            self.line_edit.current_input = styled(text, reverse=True)
        self.line_edit.write(self.write, prompt)
        self.line_edit.current_input = text
        if plain and len(plain) + len(prompt) + len(text) + 2 <= cols:
            # Position by column rather than padding with spaces, which would
            # overwrite the query that was just drawn.
            with cursor(self.write):
                self.write(f'\r\x1b[{cols - len(plain) + 1}G')
                self.write(painted)

    def status_text(self) -> tuple[str, str]:
        """Right hand status as ``(plain, painted)``.

        The plain form is what we measure for column alignment; measuring the
        painted form would count escape sequences as visible width.
        """
        if self.error:
            plain = f'{self.error[:40]}'
            return plain, styled(plain, fg='red')
        if self.status:
            return self.status, styled(self.status, dim=True)
        if not self.query:
            plain = f'{mode_label(self.mode)}  \u21e5 mode  ^L list'
            return plain, styled(plain, dim=True)
        n = len(self.matches)
        if n == 0:
            plain = f'no matches  {mode_label(self.mode)}'
            return plain, styled('no matches', fg='red') + styled(f'  {mode_label(self.mode)}', dim=True)
        plain = f'{self.idx + 1}/{n}  {mode_label(self.mode)}'
        return plain, styled(f'{self.idx + 1}/{n}', bold=True) + styled(f'  {mode_label(self.mode)}', dim=True)

    def body_lines(self, height: int, cols: int) -> list[str]:
        if height <= 0:
            return []
        if self.buf is None:
            return [styled('  reading scrollback\u2026', dim=True)]
        if not self.query:
            return [styled('  type to search  \u2022  \u21e5 cycles smart/regex/fuzzy  \u2022  ^L result list', dim=True)][:height]
        if not self.matches:
            return [styled('  no matches', dim=True)][:height]
        if self.list_mode:
            return self.list_lines(height, cols)
        return self.context_lines(height, cols)

    def gutter_width(self) -> int:
        assert self.buf is not None
        return max(4, len(str(len(self.buf.lines))))

    def render_match_line(self, i: int, cols: int, selected: bool) -> str:
        assert self.buf is not None
        m = self.matches[i]
        line = self.buf.lines[m.line]
        gw = self.gutter_width()
        marker = '\u25b8' if selected else ' '
        prefix = f'{marker} {m.line + 1:>{gw}} \u2502 '
        width = max(8, cols - len(prefix))
        chunk, off = window_text(line.text, m.start, m.end, width)
        spans = highlight_spans(self.buf, m, self.query, self.mode)
        body = _apply_spans(chunk, spans, off, selected)
        head = styled(prefix, dim=not selected)
        return head + body

    def list_lines(self, height: int, cols: int) -> list[str]:
        start, end = visible_slice(self.idx, len(self.matches), height)
        return [self.render_match_line(i, cols, i == self.idx) for i in range(start, end)]

    def context_lines(self, height: int, cols: int) -> list[str]:
        assert self.buf is not None
        out = [self.render_match_line(self.idx, cols, True)]
        if height <= 1:
            return out
        # Remaining rows show the raw buffer around the hit, so you get the
        # surrounding output without leaving the search bar.
        m = self.matches[self.idx]
        gw = self.gutter_width()
        prefix_w = gw + 5
        for k in range(1, height):
            r = m.row + k
            if r >= len(self.buf.rows):
                break
            text = self.buf.rows[r][: max(8, cols - prefix_w)]
            out.append(styled(' ' * prefix_w, dim=True) + styled(text, dim=True))
        return out[:height]

    # -- input -------------------------------------------------------------
    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        if self.preselect_all:
            self.preselect_all = False
            self.line_edit.clear()
        self.line_edit.on_text(text, in_bracketed_paste)
        self.history_pos = -1
        self.request_rerun()

    def on_key(self, key_event: KeyEventType) -> None:
        if key_event.type is EventType.RELEASE:
            return

        if self.preselect_all and not _is_modifier(key_event):
            self.preselect_all = False

        if self.handle_shortcut(key_event):
            return

        before = self.query
        if self.line_edit.on_key(key_event):
            if self.query != before:
                self.history_pos = -1
                self.request_rerun()
            else:
                self.draw_screen()
            return

    def handle_shortcut(self, ev: KeyEventType) -> bool:
        if ev.matches('esc'):
            self.quit(1)
        elif ev.matches('enter'):
            if self.list_mode:
                self.quit(0)
            else:
                self.move(1)
        elif ev.matches('ctrl+enter') or ev.matches('alt+enter'):
            self.quit(0, keep_markers=True)
        elif ev.matches('shift+enter') or ev.matches('shift+tab'):
            self.move(-1)
        elif ev.matches('down') or ev.matches('ctrl+n') or ev.matches('ctrl+g'):
            self.move(1)
        elif ev.matches('up') or ev.matches('ctrl+p') or ev.matches('ctrl+shift+g'):
            self.move(-1)
        elif ev.matches('page_down'):
            self.move(10)
        elif ev.matches('page_up'):
            self.move(-10)
        elif ev.matches('ctrl+home'):
            self.move_to(0)
        elif ev.matches('ctrl+end'):
            self.move_to(len(self.matches) - 1)
        elif ev.matches('tab'):
            self.mode = next_mode(self.mode)
            self.cached_values['mode'] = self.mode
            self._last_marker_key = None
            self.rerun(keep_index=True)
        elif ev.matches('ctrl+l'):
            self.toggle_list_mode()
        elif ev.matches('ctrl+r'):
            self.status = 'reloading\u2026'
            self.draw_screen()
            self.reload(force=True)
        elif ev.matches('ctrl+u'):
            self.line_edit.clear()
            self.rerun()
        elif ev.matches('ctrl+w'):
            self.delete_word()
        elif ev.matches('ctrl+y'):
            self.copy_current()
        elif ev.matches('alt+up'):
            self.recall_history(1)
        elif ev.matches('alt+down'):
            self.recall_history(-1)
        else:
            return False
        return True

    def delete_word(self) -> None:
        before, _after = self.line_edit.split_at_cursor()
        stripped = before.rstrip()
        idx = max(stripped.rfind(' '), stripped.rfind('/'), stripped.rfind('.'))
        n = len(before) - (idx + 1) if idx >= 0 else len(before)
        if n:
            self.line_edit.backspace(n)
            self.request_rerun()

    def recall_history(self, direction: int) -> None:
        if not self.history:
            return
        self.history_pos = max(-1, min(self.history_pos + direction, len(self.history) - 1))
        self.line_edit.clear()
        if self.history_pos >= 0:
            self.line_edit.add_text(self.history[self.history_pos])
        self.rerun()

    def copy_current(self) -> None:
        if self.buf is None or not self.matches:
            return
        text = self.buf.lines[self.matches[self.idx].line].text
        self.write(write_to_clipboard(text))
        self.status = 'copied line'
        self.draw_screen()
        self.asyncio_loop.call_later(1.2, self._clear_status)

    def _clear_status(self) -> None:
        if self.status == 'copied line':
            self.status = ''
            self.draw_screen()

    def toggle_list_mode(self) -> None:
        self.list_mode = not self.list_mode
        self.cached_values['list_mode'] = self.list_mode
        self._target_rows = LIST_ROWS if self.list_mode else BAR_ROWS
        self._resize_attempts = RESIZE_ATTEMPTS
        self.apply_pane_size()
        self.draw_screen()

    # -- resize ------------------------------------------------------------
    def on_resize(self, screen_size: ScreenSize) -> None:
        self.screen_size = screen_size
        if self._resize_timer is not None:
            self._resize_timer.cancel()
        # Growing this pane shrinks the parent, which reflows its text and
        # invalidates every row index, so the buffer has to be re-read.
        self._resize_timer = self.asyncio_loop.call_later(RESIZE_DEBOUNCE, self.after_resize)
        self.draw_screen()

    def after_resize(self) -> None:
        self._resize_timer = None
        self._last_marker_key = None
        self.apply_pane_size()
        self.reload()

    # -- exit --------------------------------------------------------------
    def on_interrupt(self) -> None:
        self.quit(1)

    def on_eot(self) -> None:
        self.quit(1)

    def quit(self, return_code: int, keep_markers: bool = False) -> None:
        q = self.query
        self.cached_values['last_search'] = q
        if q:
            hist = [h for h in self.history if h != q]
            hist.insert(0, q)
            self.cached_values['history'] = hist[:MAX_HISTORY]
        if not keep_markers:
            self.remove_markers()
        if return_code:
            # cancelled: put the parent back where we found it
            self.scroll_amount(['end', None])
        else:
            self.flush_scroll()
        self.quit_loop(return_code)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _is_modifier(ev: KeyEventType) -> bool:
    return ev.key in (
        'LEFT_CONTROL', 'RIGHT_CONTROL', 'LEFT_ALT', 'RIGHT_ALT',
        'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_SUPER', 'RIGHT_SUPER', 'TAB',
    )


def _find_window(ls_data: Any, window_id: int) -> dict[str, Any] | None:
    if not isinstance(ls_data, list):
        return None
    for os_window in ls_data:
        for tab in os_window.get('tabs', ()):
            for w in tab.get('windows', ()):
                if w.get('id') == window_id:
                    return w
    return None


def _apply_spans(chunk: str, spans: list[tuple[int, int]], offset: int, selected: bool) -> str:
    """Re-render ``chunk`` with the matched sub-spans highlighted."""
    out: list[str] = []
    pos = 0
    n = len(chunk)
    for s, e in spans:
        s -= offset
        e -= offset
        s = max(0, min(s, n))
        e = max(0, min(e, n))
        if e <= s:
            continue
        if s > pos:
            out.append(_plain(chunk[pos:s], selected))
        hit = chunk[s:e]
        out.append(styled(hit, fg='black', bg='yellow' if selected else 'blue', bold=selected))
        pos = e
    if pos < n:
        out.append(_plain(chunk[pos:], selected))
    return ''.join(out)


def _plain(text: str, selected: bool) -> str:
    return styled(text, bold=True) if selected else styled(text, dim=True)


def main(args: list[str]) -> None:
    if len(args) < 2 or not args[1].isdigit():
        raise SystemExit(
            'ksearch: the id of the window to search must be given as the first argument, '
            'e.g. `kitty +kitten ksearch.py @active-kitty-window-id`'
        )
    window_id = int(args[1])
    loop = Loop()
    with cached_values_for('ksearch') as cached_values:
        handler = Search(cached_values, window_id)
        loop.loop(handler)


if __name__ == '__main__':
    main(sys.argv)
