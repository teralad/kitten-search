# ksearch

A scrollback search kitten for [kitty](https://sw.kovidgoyal.net/kitty/) that
behaves like the search bar in Ghostty and iTerm2.

```
= error                                                    12/347  smart
▸  8412 │ [warn] connection reset by peer, retrying in 2s
```

## Why

kitty's built-in `search_scrollback` and the common `create-marker` +
`scroll_to_mark` kittens make you step blindly through hits: no count, no
overview, and the hit lands wherever it happens to fall on screen. That is fine
for three matches and painful for three hundred.

ksearch fetches the scrollback **once** and searches it in-process, so a
keystroke costs a `str.find` over one joined string instead of a remote-control
round trip plus a full rescan inside kitty. On a 100k line buffer that is about
6 ms per keystroke.

| | built-in | ksearch |
|---|---|---|
| match count | none | `12/347` live |
| current hit | same colour as the rest | second mark colour |
| hit position | wherever it lands | always the same screen row |
| overview | one at a time | `ctrl+l` result list |
| long lines | hit can be off-screen | line windowed around the hit |
| per keystroke | remote-control round trip + rescan | in-process search |

## Install

```sh
./install.sh
```

That copies `ksearch.py` and `ksearch_core.py` into your kitty config directory
and prints the key binding to add. Or do it by hand:

```sh
cp ksearch.py ksearch_core.py ~/.config/kitty/
```

Then in `kitty.conf`:

```conf
map super+shift+f launch --location=hsplit --allow-remote-control kitty +kitten ksearch.py @active-kitty-window-id
```

`--allow-remote-control` is required: the kitten drives the parent window's
scrolling and highlighting through kitty's remote control protocol.

Both files must sit in the same directory; `ksearch.py` adds its own directory to
`sys.path` to import `ksearch_core`.

## Keys

| key | action |
|---|---|
| type | search as you type |
| `enter` / `ctrl+n` / `down` | next match |
| `shift+enter` / `ctrl+p` / `up` | previous match |
| `page down` / `page up` | jump 10 matches |
| `ctrl+home` / `ctrl+end` | first / last match |
| `tab` | cycle smart / regex / fuzzy |
| `ctrl+l` | toggle the result list |
| `ctrl+y` | copy the matched line |
| `ctrl+r` | re-read the scrollback |
| `alt+up` / `alt+down` | previous searches |
| `ctrl+u` / `ctrl+w` | clear / delete word |
| `esc` | cancel, restoring the original scroll position |
| `ctrl+enter` | accept and leave the highlights on |

In the result list `enter` accepts the selected hit and leaves the parent window
scrolled there.

## Modes

**smart** (default) — literal substring, case insensitive unless your query
contains an upper case letter.

**regex** — Python regular expressions. `^` and `$` anchor to a line.

**fuzzy** — characters must appear in order on one line, so `scb` finds
`src/components/Button.tsx`. Only the characters that actually matched are
highlighted.

## Configuration

| variable | default | meaning |
|---|---|---|
| `KSEARCH_PLACE` | `0.5` | where the current hit sits, `0.0` top, `1.0` bottom |
| `KSEARCH_DEBUG` | unset | path to append a trace to |

```conf
map super+shift+f launch --location=hsplit --allow-remote-control --env KSEARCH_PLACE=0.35 kitty +kitten ksearch.py @active-kitty-window-id
```

## Tests

```sh
python3 tests/test_core.py     # no pytest needed
python3 -m pytest tests -q
```

`ksearch_core.py` has no kitty imports, so the parsing, search and scroll
arithmetic are testable without a terminal. That matters more than it sounds:
the two bugs that took longest to find were both in this layer.

## How it works, and the parts that are not obvious

**Rows versus lines.** `get-text --extent=all --add-wrap-markers` is the only
way to learn where kitty wrapped a long line. The catch is that with wrap
markers on, kitty ends *every* line with `\r\n` and marks a soft wrap with a
lone `\r`. Treating all `\r` as wraps makes each line look like two screen rows
and doubles every row index. Related trap: reading that output through Python's
universal-newline translation silently rewrites `\r\n` and lone `\r` to `\n`,
destroying the wrap information while still looking plausible.

ksearch keeps both views: logical lines for searching, so a hit is never split
by the window edge, and exact screen rows for scrolling.

**Finding the bottom.** Placing a hit at a chosen screen row needs to know which
buffer row is at the top of the window when scrolled all the way down. Deriving
it from row counts does not work — `--extent=all` appends a phantom trailing row
and strips blank ones, by an amount that depends on what the shell last printed.
So ksearch measures instead: `--extent=screen` returns the rows that are
actually visible, and the offset where that block sits inside the full buffer is
the answer (`align_bottom`). Every error here shows up directly as the hit
landing on the wrong line.

**Marking the current hit.** kitty markers are per-row regexes with no notion of
"the third hit". To colour just the current one, ksearch anchors on the literal
text preceding it with a fixed-width lookbehind — a lookbehind rather than a
capture group because kitty highlights the whole match and the prefix should not
light up. That pattern is passed as mark colour 2 *before* the colour 1
paint-everything pattern, since kitty combines the groups into a single
alternation and resolves it left to right.

**Talking to kitty.** Remote-control commands go out as in-band DCS escapes:
no process spawn, no socket, and replies arrive on the kitten's own input
stream. If that is not permitted the kitten notices on a startup probe and falls
back to spawning `kitty @`. Note that the wire format is not the command line
format — `scroll-window` takes a signed number of lines, not the CLI's
`"120l-"` string.

**Resizing.** Opening the result list grows this pane, which shrinks the parent,
which reflows its text and invalidates every row index — so the buffer is
re-read after a resize. A reload is skipped when the parent's geometry did not
actually change. `resize-window` also takes a relative increment and silently
clamps at the layout's limits, so a single request routinely lands short and has
to be re-issued until it converges.
