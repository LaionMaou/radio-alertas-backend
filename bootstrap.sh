#!/usr/bin/env bash
# Arranque de una línea: baja este repo y ejecuta install.sh con los mismos
# argumentos. Pensado para correr sin clonar nada:
#   curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh \
#     | sudo bash -s -- --dir /var/alertas --services all --domain alertas.ejemplo.com ...
# Sin TTY no hay asistente: pasá las opciones o dejá config/install.conf.
set -euo pipefail

REPO_OWNER="LaionMaou"
REPO_NAME="radio-alertas-backend"
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

printf 'bootstrap: proyecto en %s\n' "$DEST" >&2
cd "$DEST"
exec ./install.sh "${args[@]}"
