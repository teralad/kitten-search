#!/usr/bin/env python3
"""Unit tests for ksearch_core.

These run under a plain CPython interpreter -- the core module has no kitty
imports precisely so that the parsing, search and scroll arithmetic can be
verified without a running terminal.

    python3 -m pytest tests -q
    python3 tests/test_core.py        # works without pytest too
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksearch_core import (  # noqa: E402
    align_bottom,
    all_matches_pattern,
    build_buffer,
    current_match_pattern,
    marker_spec,
    highlight_spans,
    is_case_sensitive,
    next_mode,
    rows_of,
    scroll_offset_for_row,
    search,
    split_cursor,
    visible_slice,
    window_text,
)


# --------------------------------------------------------------------------
# cursor / parsing
# --------------------------------------------------------------------------


def test_split_cursor_extracts_position() -> None:
    raw = 'hello\nworld\n\x1b[?25h\x1b[7;3H\x1b[?12h'
    text, cy, cx = split_cursor(raw)
    assert text == 'hello\nworld\n'
    assert (cy, cx) == (6, 2)


def test_split_cursor_absent() -> None:
    text, cy, cx = split_cursor('a\nb\n')
    assert (text, cy, cx) == ('a\nb\n', None, None)


def test_hard_breaks_use_crlf_and_soft_wraps_use_bare_cr() -> None:
    # kitty terminates every line with \r\n once --add-wrap-markers is on, so a
    # lone \r is the only thing that actually means "wrapped"
    raw = 'aaaa\rbbbb\r\ncccc\r\n'
    assert rows_of(raw) == ['aaaa', 'bbbb', 'cccc']
    buf = build_buffer(raw, screen_rows=5, screen_cols=4)
    assert len(buf.lines) == 2
    assert buf.lines[0].text == 'aaaabbbb'
    assert buf.lines[1].text == 'cccc'
    assert buf.lines[1].row_start == 2


def test_crlf_does_not_inflate_row_indices() -> None:
    raw = ''.join(f'line {i}\r\n' for i in range(100))
    assert len(rows_of(raw)) == 100
    buf = build_buffer(raw, screen_rows=10, screen_cols=80)
    assert len(buf.rows) == 100
    hits = search(buf, 'line 42', 'smart')
    assert [h.row for h in hits] == [42]


def test_wrapped_line_maps_to_multiple_rows() -> None:
    # one logical line of 10 chars wrapped at 4 columns -> 3 screen rows
    raw = 'abcd\refgh\rij\nnext\n'
    buf = build_buffer(raw, screen_rows=5, screen_cols=4)
    assert buf.rows == ['abcd', 'efgh', 'ij', 'next']
    assert buf.lines[0].text == 'abcdefghij'
    assert buf.lines[0].n_rows == 3
    assert buf.row_for(0, 0) == 0
    assert buf.row_for(0, 3) == 0
    assert buf.row_for(0, 4) == 1
    assert buf.row_for(0, 8) == 2
    assert buf.lines[1].row_start == 3


def test_align_bottom_exact_screenful() -> None:
    all_rows = [f'line{i}' for i in range(100)]
    visible = all_rows[-10:]
    assert align_bottom(all_rows, visible, 10) == 90


def test_align_bottom_ignores_phantom_trailing_rows() -> None:
    # get-text --extent=all appends rows that are not actually on screen
    all_rows = [f'line{i}' for i in range(100)] + ['']
    visible = all_rows[90:100]
    assert align_bottom(all_rows, visible, 10) == 90


def test_align_bottom_with_stripped_blank_rows() -> None:
    # --extent=screen dropped the blank rows below the last output
    all_rows = [f'line{i}' for i in range(100)]
    visible = all_rows[-4:]
    assert align_bottom(all_rows, visible, 10) == 96


def test_align_bottom_prefers_the_most_recent_alignment() -> None:
    # the same block appears earlier in the scrollback (repeated output)
    block = ['same', 'same', 'same']
    all_rows = ['x'] * 20 + block + ['y'] * 20 + block
    assert align_bottom(all_rows, block, 5) == len(all_rows) - 3


def test_align_bottom_falls_back_when_nothing_matches() -> None:
    all_rows = [f'line{i}' for i in range(50)]
    assert align_bottom(all_rows, ['totally', 'different'], 10) == 48


# --------------------------------------------------------------------------
# scrolling
# --------------------------------------------------------------------------


def test_scroll_offset_centres_the_row() -> None:
    top_row, height = 980, 20
    row = 500
    off = scroll_offset_for_row(row, top_row, height, place=0.5)
    top = top_row - off
    assert top <= row < top + height
    assert row - top == round((height - 1) * 0.5)


def test_scroll_offset_clamped_at_both_ends() -> None:
    top_row, height = 80, 20
    # a row already in the bottom screenful needs no scrolling
    assert scroll_offset_for_row(99, top_row, height, 0.5) == 0
    # the very first row cannot be centred, only brought to the top
    assert scroll_offset_for_row(0, top_row, height, 0.5) == top_row


def test_scroll_offset_place_top_and_bottom() -> None:
    top_row, height = 980, 20
    row = 500
    assert top_row - scroll_offset_for_row(row, top_row, height, 0.0) == row
    assert top_row - scroll_offset_for_row(row, top_row, height, 1.0) == row - (height - 1)


def test_scroll_offset_is_the_inverse_of_the_view() -> None:
    top_row, height = 500, 30
    for row in range(0, 520, 7):
        for place in (0.0, 0.25, 0.5, 1.0):
            off = scroll_offset_for_row(row, top_row, height, place)
            top = top_row - off
            assert 0 <= off <= top_row
            if 0 < off < top_row:
                assert row - top == round((height - 1) * place)


# --------------------------------------------------------------------------
# search engines
# --------------------------------------------------------------------------


def make_buf(lines: list[str], screen_rows: int = 10, screen_cols: int = 80):
    return build_buffer('\n'.join(lines) + '\n', screen_rows, screen_cols)


def test_smart_case_is_insensitive_for_lowercase_query() -> None:
    buf = make_buf(['ERROR here', 'error there', 'Error again'])
    assert not is_case_sensitive('error')
    assert len(search(buf, 'error', 'smart')) == 3


def test_smart_case_is_sensitive_when_query_has_uppercase() -> None:
    buf = make_buf(['ERROR here', 'error there', 'Error again'])
    assert is_case_sensitive('Error')
    hits = search(buf, 'Error', 'smart')
    assert [h.line for h in hits] == [2]


def test_matches_never_span_a_line_break() -> None:
    buf = make_buf(['foo', 'bar'])
    assert search(buf, 'foo\nbar', 'smart') == []
    assert search(buf, 'foo.bar', 'regex') == []


def test_non_overlapping_literal_matches() -> None:
    buf = make_buf(['aaaa'])
    hits = search(buf, 'aa', 'smart')
    assert [(h.start, h.end) for h in hits] == [(0, 2), (2, 4)]


def test_regex_anchors_work_per_line() -> None:
    buf = make_buf(['alpha', 'beta', 'alphabet'])
    hits = search(buf, r'^alpha$', 'regex')
    assert [h.line for h in hits] == [0]


def test_regex_bad_pattern_raises() -> None:
    buf = make_buf(['x'])
    try:
        search(buf, '(unclosed', 'regex')
    except Exception as err:
        assert 'SearchError' in type(err).__name__
    else:
        raise AssertionError('expected SearchError')


def test_fuzzy_matches_subsequence_within_one_line() -> None:
    buf = make_buf(['src/components/Button.tsx', 'unrelated', 'scb'])
    hits = search(buf, 'scb', 'fuzzy')
    assert {h.line for h in hits} == {0, 2}


def test_fuzzy_does_not_cross_lines() -> None:
    buf = make_buf(['a', 'b', 'c'])
    assert search(buf, 'abc', 'fuzzy') == []


def test_fuzzy_highlight_spans_are_per_character() -> None:
    buf = make_buf(['x-a-b-c-y'])
    hits = search(buf, 'abc', 'fuzzy')
    assert len(hits) == 1
    spans = highlight_spans(buf, hits[0], 'abc', 'fuzzy')
    text = buf.lines[0].text
    assert ''.join(text[s:e] for s, e in spans) == 'abc'


def test_match_row_follows_wrapping() -> None:
    raw = 'aaaa\rbbbb\rneedle\nz\n'
    buf = build_buffer(raw, screen_rows=6, screen_cols=4)
    hits = search(buf, 'needle', 'smart')
    assert len(hits) == 1
    assert hits[0].row == 2  # third screen row of the wrapped logical line


def test_empty_query_returns_nothing() -> None:
    buf = make_buf(['anything'])
    assert search(buf, '', 'smart') == []


def test_large_buffer_search_is_correct() -> None:
    lines = [f'line {i} of noise' for i in range(20000)]
    lines[12345] = 'the needle is here'
    buf = make_buf(lines, screen_rows=40)
    hits = search(buf, 'needle', 'smart')
    assert [h.line for h in hits] == [12345]


# --------------------------------------------------------------------------
# kitty marker patterns
# --------------------------------------------------------------------------


def test_all_matches_pattern_escapes_literals() -> None:
    import re
    pat = all_matches_pattern('a.b*c', 'smart')
    assert re.search(pat, 'x a.b*c y')
    assert not re.search(pat, 'xaXbYc')


def test_all_matches_pattern_folds_case_only_for_lowercase_queries() -> None:
    import re
    assert re.search(all_matches_pattern('error', 'smart'), 'ERROR')
    assert not re.search(all_matches_pattern('Error', 'smart'), 'ERROR')


def test_all_matches_pattern_rejects_broken_regex() -> None:
    assert all_matches_pattern('(unclosed', 'regex') is None


def test_current_match_pattern_selects_one_occurrence() -> None:
    import re
    row = 'foo bar foo baz'
    pat = current_match_pattern(row, 8, 'foo')
    assert [m.span() for m in re.finditer(pat, row)] == [(8, 11)]


def test_current_match_pattern_highlights_only_the_hit_not_the_prefix() -> None:
    import re
    row = 'prefix NEEDLE'
    m = re.search(current_match_pattern(row, 7, 'NEEDLE'), row)
    assert m.group(0) == 'NEEDLE'


def test_current_match_pattern_at_column_zero_uses_an_anchor() -> None:
    import re
    row = 'NEEDLE trailing'
    pat = current_match_pattern(row, 0, 'NEEDLE')
    assert pat.startswith('^')
    assert re.search(pat, row).span() == (0, 6)


def test_marker_spec_lists_the_current_hit_first() -> None:
    buf = make_buf(['aaa target bbb', 'ccc target ddd'])
    hits = search(buf, 'target', 'smart')
    spec = marker_spec(buf, hits[1], 'target', 'smart')
    assert spec[0] == 'regex'
    # colour 2 (current) must precede colour 1 (all) because kitty resolves the
    # combined alternation left to right
    assert spec[1] == '2'
    assert spec[3] == '1'


def test_marker_spec_current_pattern_matches_only_the_selected_row() -> None:
    import re
    buf = make_buf(['aaa target bbb', 'ccc target ddd'])
    hits = search(buf, 'target', 'smart')
    spec = marker_spec(buf, hits[1], 'target', 'smart')
    cur = re.compile(spec[2])
    assert not cur.search('aaa target bbb')
    assert cur.search('ccc target ddd')


def test_marker_spec_survives_a_hit_on_a_wrapped_row() -> None:
    import re
    raw = 'aaaa\rbbtargetbb\r\n'
    buf = build_buffer(raw, screen_rows=5, screen_cols=4)
    hits = search(buf, 'target', 'smart')
    assert len(hits) == 1
    spec = marker_spec(buf, hits[0], 'target', 'smart')
    # the pattern is anchored against the wrapped row, not the logical line
    assert re.search(spec[2], buf.rows[hits[0].row])


# --------------------------------------------------------------------------
# display helpers
# --------------------------------------------------------------------------


def test_window_text_keeps_short_lines_intact() -> None:
    chunk, off = window_text('short line', 0, 5, 40)
    assert (chunk, off) == ('short line', 0)


def test_window_text_scrolls_to_reveal_a_far_right_match() -> None:
    text = 'x' * 200 + 'NEEDLE' + 'y' * 200
    chunk, off = window_text(text, 200, 206, 40)
    assert 'NEEDLE' in chunk
    assert len(chunk) == 40
    assert chunk.startswith('\u2026')


def test_window_text_marks_truncation_on_the_right() -> None:
    text = 'NEEDLE' + 'y' * 200
    chunk, off = window_text(text, 0, 6, 40)
    assert off == 0
    assert chunk.startswith('NEEDLE')
    assert chunk.endswith('\u2026')


def test_visible_slice_keeps_selection_in_view() -> None:
    for sel in range(0, 100):
        start, end = visible_slice(sel, 100, 10)
        assert end - start == 10
        assert start <= sel < end


def test_visible_slice_short_list() -> None:
    assert visible_slice(0, 3, 10) == (0, 3)


def test_mode_cycle_is_a_ring() -> None:
    m = 'smart'
    seen = [m]
    for _ in range(3):
        m = next_mode(m)
        seen.append(m)
    assert seen == ['smart', 'regex', 'fuzzy', 'smart']


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f'FAIL {fn.__name__}: {type(err).__name__}: {err}')
        else:
            print(f'ok   {fn.__name__}')
    print(f'\n{len(fns) - failed}/{len(fns)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(_run_all())
