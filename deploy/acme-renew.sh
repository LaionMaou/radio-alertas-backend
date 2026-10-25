#!/usr/bin/env bash
# Renueva los certificados ACME del stack. Lo llama /etc/cron.d/radio-alertas.
set -uo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ACME_HOME="$BASE_DIR/config/acme/home"
LOG="$BASE_DIR/logs/acme.log"

[ -x "$ACME_HOME/acme.sh" ] || exit 0
mkdir -p "$BASE_DIR/logs"

for env_file in "$BASE_DIR/config/acme/cloudflare.env" "$BASE_DIR/config/acme/gts.env"; do
    if [ -r "$env_file" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$env_file"
        set +a
    fi
done

{
    printf '\n=== %s ===\n' "$(date -Is)"
    "$ACME_HOME/acme.sh" --cron --home "$ACME_HOME" --config-home "$ACME_HOME"
} >> "$LOG" 2>&1

exit 0
