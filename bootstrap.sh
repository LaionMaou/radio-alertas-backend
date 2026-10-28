#!/usr/bin/env bash
# Arranque de una línea: baja este repo (y la cadena TTS si los servicios la
# incluyen) y ejecuta install.sh con los mismos argumentos. Pensado para correr
# sin clonar nada:
#   curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh \
#     | sudo bash -s -- --dir /var/alertas --services all --domain alertas.ejemplo.com ...
# Sin TTY no hay asistente: pasá las opciones o dejá config/install.conf.
set -euo pipefail

REPO_OWNER="LaionMaou"
REPO_NAME="radio-alertas-backend"
TTS_OWNER="LaionMaou"
TTS_NAME="radio-alertas-tts"
DEST="/var/alertas"
BRANCH="main"

die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Uso: curl -fsSL .../bootstrap.sh | sudo bash -s -- [opciones] [opciones de install.sh]

  --dir RUTA        dónde instalar (absoluta; por defecto /var/alertas)
  --branch NOMBRE   rama del repo a bajar (por defecto main)
  -h, --help        esta ayuda

Todo lo demás se le pasa a install.sh (--services, --domain, --port, --tls...).

La cadena TTS se baja sola de LaionMaou/radio-alertas-tts cuando los servicios
la incluyen (all por defecto). Con --tts-src RUTA se usa esa copia y no se
descarga nada; con --no-tts no se instala.
EOF
}

args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)
            [ $# -ge 2 ] || die "--dir necesita una ruta"
            DEST="$2"; shift ;;
        --branch)
            [ $# -ge 2 ] || die "--branch necesita un nombre"
            BRANCH="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) args+=("$1") ;;
    esac
    shift
done

case "$DEST" in
    /*) ;;
    *) die "--dir tiene que ser una ruta absoluta: $DEST" ;;
esac

# La cadena TTS vive en otro repo. Si install.sh la va a instalar y no se pasó
# una copia local, se baja acá y se le entrega con --tts-src.
services="all"
want_tts=1
i=0
while [ "$i" -lt "${#args[@]}" ]; do
    case "${args[$i]}" in
        --services)
            services="${args[$((i + 1))]:-all}"
            i=$((i + 1)) ;;
        --services=*) services="${args[$i]#--services=}" ;;
        --no-tts|--tts-src|--tts-src=*) want_tts=0 ;;
    esac
    i=$((i + 1))
done

case ",$services," in
    *",all,"*|*",tts,"*) ;;
    *) want_tts=0 ;;
esac

[ "$(id -u)" -eq 0 ] || die "ejecutá con sudo: curl ... | sudo bash -s -- ..."
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
tar -C "$tmp/src" -cf - . | tar -C "$DEST" -xf -
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
