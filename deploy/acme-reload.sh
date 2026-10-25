#!/usr/bin/env bash
# Recarga nginx después de instalar un certificado. Lo invoca acme.sh, también
# durante la primera emisión, cuando el contenedor de la web todavía no existe.
set -uo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

running="$(docker inspect -f '{{.State.Running}}' kuma-alerts-web 2>/dev/null || echo false)"
if [ "$running" = "true" ]; then
    docker exec kuma-alerts-web nginx -s reload || exit 1
fi

exit 0
