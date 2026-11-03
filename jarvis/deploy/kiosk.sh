#!/usr/bin/env bash
# Tela do Jarvis em tela cheia no Pi: chromium --kiosk dentro do cage.
# Chamado por jarvis-kiosk.service (ver deploy/README.md, seção 8). Rode como o
# usuário do serviço, não como root.
set -euo pipefail

URL="${JARVIS_URL:-http://localhost:8000/}"
# Perfil fixo: o token digitado uma vez fica salvo no localStorage e sobrevive a reboots.
PERFIL="${JARVIS_PERFIL:-$HOME/.config/jarvis-kiosk}"

# O backend pode estar subindo ao mesmo tempo (After=jarvis.service não espera o HTTP).
if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "$URL" 2>/dev/null; then break; fi
    sleep 1
  done
fi

CHROMIUM="${CHROMIUM:-$(command -v chromium || command -v chromium-browser || true)}"
if [ -z "$CHROMIUM" ]; then
  echo "chromium não encontrado: instale-o ou defina CHROMIUM=/caminho/do/binario" >&2
  exit 1
fi

exec "$CHROMIUM" \
  --kiosk \
  --ozone-platform-hint=auto \
  --user-data-dir="$PERFIL" \
  --password-store=basic \
  --no-first-run \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  "$URL"
