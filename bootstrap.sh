#!/usr/bin/env bash
# Instalador de arranque: baja este repo (y la cadena TTS si los servicios la
# incluyen) en el directorio actual y ejecuta install.sh. Mismo flujo que
# AzuraCast:
#   mkdir -p /var/alertas && cd /var/alertas
#   curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh -o bootstrap.sh
#   chmod a+x bootstrap.sh
#   sudo ./bootstrap.sh install
# Para actualizar, como AzuraCast:
#   cd /var/alertas && sudo ./bootstrap.sh update-self && sudo ./bootstrap.sh update
# También se puede pipear, con --dir para elegir la ruta:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --dir /srv/alertas --services all ...
# Sin TTY no hay asistente: pasá las opciones o dejá config/install.conf.
set -euo pipefail

REPO_OWNER="LaionMaou"
REPO_NAME="radio-alertas-backend"
TTS_OWNER="LaionMaou"
TTS_NAME="radio-alertas-tts"
DEST="$(pwd)"
BRANCH="main"
RAW_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$BRANCH/bootstrap.sh"

die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }

has_arg() {
    local want="$1" arg
    for arg in "${args[@]}"; do
        [ "$arg" = "$want" ] && return 0
    done
    return 1
}

# Reemplaza este script por la última versión de la rama elegida. Se escribe en
# un archivo nuevo y se renombra, así bash termina de leer el original.
update_self() {
    [ -f "$0" ] || die "update-self necesita ./bootstrap.sh, no el modo pipeado"
    local target="$0" new
    case "$target" in /*) ;; *) target="$PWD/$target" ;; esac
    new="$target.new.$$"
    printf 'bootstrap: actualizando %s (%s)\n' "$target" "$BRANCH" >&2
    curl -fsSL "$RAW_URL" -o "$new" \
        || { rm -f "$new"; die "no pude descargar $RAW_URL"; }
    chmod +x "$new"
    mv -f "$new" "$target"
    printf 'bootstrap: script actualizado\n' >&2
}

usage() {
    cat <<'EOF'
Uso: sudo ./bootstrap.sh [install|update|update-self] [opciones] [opciones de install.sh]
     curl -fsSL .../bootstrap.sh | sudo bash -s -- [opciones] [opciones de install.sh]

  install           instala en el directorio actual (es el modo por defecto)
  update            baja la última versión y reconstruye los contenedores
  update-self       actualiza este script, sin tocar la instalación
  --dir RUTA        dónde instalar (absoluta; por defecto, el directorio actual)
  --branch NOMBRE   rama del repo a bajar (por defecto main)
  -h, --help        esta ayuda

Todo lo demás se le pasa a install.sh (--services, --domain, --port, --tls...).

La cadena TTS se baja sola de LaionMaou/radio-alertas-tts cuando los servicios
la incluyen (all por defecto). Con --tts-src RUTA se usa esa copia y no se
descarga nada; con --no-tts no se instala.
EOF
}

args=()
MODE="install"
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)
            [ $# -ge 2 ] || die "--dir necesita una ruta"
            DEST="$2"; shift ;;
        --branch)
            [ $# -ge 2 ] || die "--branch necesita un nombre"
            BRANCH="$2"
            RAW_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$BRANCH/bootstrap.sh"
            shift ;;
        -h|--help) usage; exit 0 ;;
        install) MODE="install" ;;
        update) MODE="update" ;;
        update-self) MODE="update-self" ;;
        *) args+=("$1") ;;
    esac
    shift
done

if [ "$MODE" = "update-self" ]; then
    update_self
    exit 0
fi

case "$DEST" in
    /*) ;;
    *) die "--dir tiene que ser una ruta absoluta: $DEST" ;;
esac
[ "$DEST" != "/" ] || die "no instalo en /; usá una carpeta como /var/alertas"

# La cadena TTS vive en otro repo. Si install.sh la va a instalar y no se pasó
# una copia local, se baja acá y se le entrega con --tts-src.
services="all"
services_explicit=0
want_tts=1
i=0
while [ "$i" -lt "${#args[@]}" ]; do
    case "${args[$i]}" in
        --services)
            services="${args[$((i + 1))]:-all}"
            services_explicit=1
            i=$((i + 1)) ;;
        --services=*) services="${args[$i]#--services=}"; services_explicit=1 ;;
        --no-tts|--tts-src|--tts-src=*) want_tts=0 ;;
    esac
    i=$((i + 1))
done

# En una actualización manda lo que ya está instalado: si la instalación no
# tiene la cadena TTS, no hay por qué bajarla.
if [ "$services_explicit" -eq 0 ] && [ -f "$DEST/config/install.conf" ]; then
    saved="$(sed -n 's/^SERVICES=//p' "$DEST/config/install.conf" | tail -n 1)"
    [ -n "$saved" ] && services="$saved"
fi

case ",$services," in
    *",all,"*|*",tts,"*) ;;
    *) want_tts=0 ;;
esac

# Una actualización no pregunta: install.sh reusa config/install.conf.
if [ "$MODE" = "update" ] && ! has_arg --non-interactive && ! has_arg --configure; then
    args+=(--non-interactive)
fi

[ "$(id -u)" -eq 0 ] || die "ejecutá con sudo: sudo ./bootstrap.sh install"
command -v curl >/dev/null 2>&1 || die "falta curl"
command -v tar >/dev/null 2>&1 || die "falta tar"

if [ ! -t 0 ]; then
    printf 'bootstrap: sin terminal interactiva no hay asistente; usá --services, --domain, --tls, --acme-email, ...\n' >&2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

url="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/refs/heads/$BRANCH"
printf 'bootstrap: bajando %s (%s)\n' "$REPO_NAME" "$BRANCH" >&2
curl -fsSL "$url" -o "$tmp/repo.tar.gz" || die "no pude descargar $url"

mkdir -p "$tmp/src" "$DEST"
tar -xzf "$tmp/repo.tar.gz" -C "$tmp/src" --strip-components=1 \
    || die "el tarball no se pudo extraer"

# Si el script corre desde el destino, no se sobrescribe a sí mismo mientras
# bash todavía lo está leyendo.
if [ "$(cd "$(dirname "$0")" 2>/dev/null && pwd)" = "$DEST" ]; then
    tar -C "$tmp/src" -cf - --exclude=./bootstrap.sh . | tar -C "$DEST" -xf -
else
    tar -C "$tmp/src" -cf - . | tar -C "$DEST" -xf -
fi
chmod +x "$DEST/install.sh" "$DEST"/deploy/*.sh 2>/dev/null || true

if [ "$want_tts" -eq 1 ]; then
    tts_url="https://codeload.github.com/$TTS_OWNER/$TTS_NAME/tar.gz/refs/heads/main"
    printf 'bootstrap: bajando %s (main)\n' "$TTS_NAME" >&2
    mkdir -p "$tmp/tts"
    curl -fsSL "$tts_url" -o "$tmp/tts.tar.gz" \
        || die "no pude descargar $tts_url; usá --tts-src RUTA o --no-tts"
    tar -xzf "$tmp/tts.tar.gz" -C "$tmp/tts" --strip-components=1 \
        || die "el tarball del TTS no se pudo extraer"
    args+=(--tts-src "$tmp/tts")
fi

printf 'bootstrap: proyecto en %s\n' "$DEST" >&2
cd "$DEST"
./install.sh "${args[@]}"
