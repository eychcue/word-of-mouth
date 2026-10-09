#!/usr/bin/env bash
# Expose Word of Mouth (and nothing else) at a public https address so guests'
# phones can open pass-along links without a workspace account. Uses a
# Cloudflare quick tunnel: no account, but the address is random and changes
# every time this program restarts. Restarting the app itself does not change it.
set -euo pipefail
DATA_DIR="${WORD_OF_MOUTH_DATA_DIR:-data/.apps/word-of-mouth}"
BIN_DIR="$DATA_DIR/bin"
URL_FILE="$DATA_DIR/public_url.txt"
PORT="${WORD_OF_MOUTH_PORT:-8080}"
mkdir -p "$BIN_DIR"
touch "$BIN_DIR/.nobackup"
if [ ! -x "$BIN_DIR/cloudflared" ]; then
  curl -fsSL -o "$BIN_DIR/cloudflared.part" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x "$BIN_DIR/cloudflared.part"
  mv "$BIN_DIR/cloudflared.part" "$BIN_DIR/cloudflared"
fi
rm -f "$URL_FILE"
exec "$BIN_DIR/cloudflared" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
    echo "${BASH_REMATCH[1]}" > "$URL_FILE"
    echo "PUBLIC ADDRESS: ${BASH_REMATCH[1]}"
  fi
done
