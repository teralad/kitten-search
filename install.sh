#!/bin/sh
# Install ksearch into the kitty configuration directory.
set -eu

SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=${KITTY_CONFIG_DIRECTORY:-${XDG_CONFIG_HOME:-$HOME/.config}/kitty}

if [ ! -d "$DEST" ]; then
    printf 'kitty config directory not found: %s\n' "$DEST" >&2
    exit 1
fi

for f in ksearch.py ksearch_core.py; do
    cp "$SRC/$f" "$DEST/$f"
    printf 'installed %s\n' "$DEST/$f"
done

cat <<'EOF'

Add a binding to kitty.conf, then reload it with ctrl+shift+f5:

  map super+shift+f launch --location=hsplit --allow-remote-control kitty +kitten ksearch.py @active-kitty-window-id

--allow-remote-control is required; the kitten scrolls and highlights the
parent window through kitty's remote control protocol.
EOF
