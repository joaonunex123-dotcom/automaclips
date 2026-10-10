#!/usr/bin/env bash
# Sobe/para o servidor de PO token (bgutil) que o yt-dlp usa para baixar em 1080p.
#   ops/pot_provider.sh subir | parar | status
# Instalação em .pot/ (node + código do servidor), fora do git. Ver README.
set -u
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PORTA="${BGUTIL_PORTA:-4416}"
NODE_BIN="$(ls -d "$RAIZ"/.pot/node-v*/bin 2>/dev/null | head -1)"
SERVIDOR="$RAIZ/.pot/src/server/build/main.js"
PIDFILE="$RAIZ/.pot/servidor.pid"
LOG="$RAIZ/.pot/servidor.log"

vivo() { curl -sf "http://127.0.0.1:$PORTA/ping" >/dev/null 2>&1; }

case "${1:-}" in
  subir)
    if vivo; then echo "já no ar na porta $PORTA"; exit 0; fi
    [ -f "$SERVIDOR" ] || { echo "servidor não compilado: $SERVIDOR (ver README, 'PO token')"; exit 1; }
    PATH="$NODE_BIN:$PATH" nohup node "$SERVIDOR" --port "$PORTA" >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    for _ in $(seq 1 20); do vivo && { echo "no ar na porta $PORTA (pid $(cat "$PIDFILE"))"; exit 0; }; sleep 0.5; done
    echo "não subiu; veja $LOG"; exit 1 ;;
  parar)
    if [ -f "$PIDFILE" ]; then kill "$(cat "$PIDFILE")" 2>/dev/null; rm -f "$PIDFILE"; fi
    echo "parado" ;;
  status)
    if vivo; then echo "no ar na porta $PORTA"; else echo "parado"; exit 1; fi ;;
  *) echo "uso: $0 subir|parar|status"; exit 2 ;;
esac
