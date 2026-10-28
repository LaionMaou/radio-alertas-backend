#!/usr/bin/env bash
# Operadores del portal: quiénes pueden detener el bucle de 大津波警報.
#
#   sudo ./deploy/operators.sh add <usuario>
#   sudo ./deploy/operators.sh remove <usuario>
#   sudo ./deploy/operators.sh list
#
# Una cuenta por locutor: cuando alguien deja el equipo se borra la suya y el
# resto sigue entrando. El archivo lo lee nginx por request, así que el cambio
# se aplica con una recarga, sin reiniciar el contenedor.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HT_FILE="$BASE_DIR/config/nginx/operator.htpasswd"
CONTAINER="kuma-alerts-web"
USER_RE='^[A-Za-z0-9._-]{1,32}$'

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat >&2 <<'EOF'
Uso:
  operators.sh add <usuario>      agrega un operador (pide la contraseña)
  operators.sh remove <usuario>   lo da de baja
  operators.sh list               lista los usuarios
EOF
    exit 2
}

need_tty() {
    [ -t 0 ] || die "hay que ejecutarlo desde una terminal interactiva"
}

command -v openssl >/dev/null 2>&1 || die "falta openssl"

validate_user() {
    [[ "$1" =~ $USER_RE ]] || die "usuario inválido: $USER_RE"
}

require_writable() {
    mkdir -p "$(dirname "$HT_FILE")"
    touch "$HT_FILE" 2>/dev/null || die "no puedo escribir $HT_FILE; probá con sudo"
}

has_user() {
    grep -q "^$1:" "$HT_FILE" 2>/dev/null
}

reload_web() {
    local running
    running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
    if [ "$running" != "true" ]; then
        printf 'el contenedor %s no está corriendo; el cambio se aplica cuando arranque\n' \
            "$CONTAINER" >&2
        return 0
    fi
    if docker exec "$CONTAINER" nginx -t >/dev/null 2>&1; then
        docker exec "$CONTAINER" nginx -s reload >/dev/null 2>&1 \
            || die "nginx no pudo recargar; revisá docker logs $CONTAINER"
    else
        printf 'nginx rechazó la configuración; no recargo (revisá docker logs %s)\n' \
            "$CONTAINER" >&2
    fi
}

cmd_add() {
    local user="$1" pass="" confirm="" hash
    validate_user "$user"
    require_writable
    need_tty
    has_user "$user" && die "el usuario $user ya existe; borralo primero con remove"

    printf 'Contraseña para %s: ' "$user" >&2
    IFS= read -rs pass || die "no pude leer la contraseña"
    printf '\nRepetir contraseña: ' >&2
    IFS= read -rs confirm || die "no pude leer la contraseña"
    printf '\n' >&2

    [ -n "$pass" ] || die "la contraseña no puede estar vacía"
    [ "$pass" = "$confirm" ] || die "las contraseñas no coinciden"

    # -stdin: la contraseña no aparece en `ps` como argumento.
    hash="$(printf '%s\n' "$pass" | openssl passwd -apr1 -stdin)" \
        || die "no pude calcular el hash"
    unset pass confirm

    # Si el archivo quedó sin newline final, la línea nueva se pegaría a la vieja.
    if [ -s "$HT_FILE" ] && [ -n "$(tail -c 1 "$HT_FILE")" ]; then
        printf '\n' >> "$HT_FILE"
    fi
    printf '%s:%s\n' "$user" "$hash" >> "$HT_FILE"
    chmod 644 "$HT_FILE"
    reload_web
    printf 'operador %s agregado\n' "$user"
}

cmd_remove() {
    local user="$1" tmp
    validate_user "$user"
    require_writable
    has_user "$user" || die "el usuario $user no existe"

    tmp="$(mktemp)"
    grep -v "^$user:" "$HT_FILE" > "$tmp" || true
    cat "$tmp" > "$HT_FILE"
    rm -f "$tmp"
    chmod 644 "$HT_FILE"
    reload_web
    printf 'operador %s dado de baja\n' "$user"
}

cmd_list() {
    if [ ! -s "$HT_FILE" ]; then
        printf 'no hay operadores; agregá uno con: sudo %s add <usuario>\n' \
            "${BASH_SOURCE[0]#"$BASE_DIR"/}" >&2
        return 0
    fi
    cut -d: -f1 "$HT_FILE"
}

[ $# -ge 1 ] || usage
case "$1" in
    add) [ $# -eq 2 ] || usage; cmd_add "$2" ;;
    remove) [ $# -eq 2 ] || usage; cmd_remove "$2" ;;
    list) [ $# -eq 1 ] || usage; cmd_list ;;
    *) usage ;;
esac
