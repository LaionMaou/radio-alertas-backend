#!/usr/bin/env bash
# Instalador del stack de alertas para el VPS.
#
#   cd /var/alertas
#   sudo ./install.sh
#
# Instala los servicios elegidos (backend P2P/JMA, web para locutores y cadena
# TTS), emite el certificado HTTPS si se pide y deja todo con DRY_RUN=true: no
# sale audio al aire. No toca AzuraCast ni reinicia un daemon de Docker que ya
# estaba andando. Las respuestas quedan en config/install.conf.
#
# Opciones:
#   --services LISTA          all (defecto), backend, web, tts o combinación con comas
#   --port N                  puerto público de la web (8081 en HTTP, 8443 en HTTPS)
#   --domain DOMINIO          subdominio de la web; obligatorio con HTTPS
#   --tls CA                  none (defecto), letsencrypt o gts
#   --acme-email EMAIL        contacto de la cuenta ACME
#   --cf-token-file RUTA      archivo con el token de Cloudflare (CF_Token=...)
#   --gts-eab-kid KID         credencial EAB de Google Trust Services
#   --gts-eab-hmac-file RUTA  archivo con la clave HMAC de Google Trust Services
#   --staging                 usar el entorno de pruebas de Let's Encrypt
#   --no-staging              volver a la CA de producción tras una prueba staging
#   --configure               volver a preguntar aunque ya exista config/install.conf
#   --non-interactive         no preguntar; usar las opciones y config/install.conf
#   --tts-src RUTA            copiar la cadena TTS desde RUTA a ./tts
#   --no-tts                  no instalar la cadena TTS
#   --tts-port N              puerto de la API del TTS (8090 si está libre)
#   --open-firewall           abrir el puerto web elegido en ufw (si ufw está activo)
#   -h, --help                mostrar esta ayuda
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_COMPOSE="$BASE_DIR/deploy/docker-compose.yml"
BACKEND_ENV="$BASE_DIR/config/.env"
BACKEND_ENV_EXAMPLE="$BASE_DIR/config/.env.example"
CONFIG_DIR="$BASE_DIR/config"
INSTALL_CONF="$CONFIG_DIR/install.conf"
NGINX_SRC_DIR="$BASE_DIR/deploy/nginx"
NGINX_RENDERED_DIR="$CONFIG_DIR/nginx"
CERTS_DIR="$CONFIG_DIR/certs"
ACME_DIR="$CONFIG_DIR/acme"
ACME_HOME="$ACME_DIR/home"
ACME_BIN="$ACME_HOME/acme.sh"
ACME_CF_ENV="$ACME_DIR/cloudflare.env"
ACME_GTS_ENV="$ACME_DIR/gts.env"
ACME_LOG="$BASE_DIR/logs/acme.log"
CRON_FILE="/etc/cron.d/radio-alertas"
ACME_URL="https://github.com/acmesh-official/acme.sh/archive/refs/tags/v3.1.6.tar.gz"
LE_SERVER="https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING="https://acme-staging-v02.api.letsencrypt.org/directory"
GTS_SERVER="https://dv.acme-v02.api.pki.goog/directory"
TTS_DIR="$BASE_DIR/tts"
TTS_COMPOSE="$TTS_DIR/docker-compose.yml"
TTS_ENV="$TTS_DIR/.env"

SERVICES=""; WEB_PORT=""; DOMAIN=""; TLS_CA=""; ACME_DNS="cloudflare"
ACME_EMAIL=""; ACME_STAGING="0"; SCHEME="http"; INTERNAL_PORT="80"
PREV_TLS_CA=""; PREV_STAGING=""

FLAG_SERVICES=""; FLAG_PORT=""; FLAG_DOMAIN=""; FLAG_TLS=""
FLAG_EMAIL=""; FLAG_CF_FILE=""; FLAG_EAB_KID=""; FLAG_EAB_HMAC_FILE=""
FLAG_TTS_PORT=""
STAGING=0; NO_STAGING=0; CONFIGURE=0; NON_INTERACTIVE=0; NO_TTS=0; OPEN_FIREWALL=0
TTS_SRC=""; TTS_EXPLICIT=0; BACKEND=0; WEB=0; TTS=0
SERVICES_NORM=""; FAILURES=0; tts_port=""

if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'
    C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
else
    C_RESET=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s  !!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Instalador del stack de alertas para el VPS.

  cd <directorio del proyecto>
  sudo ./install.sh

Opciones:
  --services LISTA          all (defecto), backend, web, tts o combinación con comas
  --port N                  puerto público de la web (8081 en HTTP, 8443 en HTTPS)
  --domain DOMINIO          subdominio de la web; obligatorio con HTTPS
  --tls CA                  none (defecto), letsencrypt o gts
  --acme-email EMAIL        contacto de la cuenta ACME
  --cf-token-file RUTA      archivo con el token de Cloudflare (CF_Token=...)
  --gts-eab-kid KID         credencial EAB de Google Trust Services
  --gts-eab-hmac-file RUTA  archivo con la clave HMAC de Google Trust Services
  --staging                 usar el entorno de pruebas de Let's Encrypt
  --no-staging              volver a la CA de producción tras una prueba staging
  --configure               volver a preguntar aunque ya exista config/install.conf
  --non-interactive         no preguntar; usar las opciones y config/install.conf
  --tts-src RUTA            copiar la cadena TTS desde RUTA a ./tts
  --no-tts                  no instalar la cadena TTS
  --tts-port N              puerto de la API del TTS (8090 si está libre)
  --open-firewall           abrir el puerto web elegido en ufw (si ufw está activo)
  -h, --help                mostrar esta ayuda

Todo arranca con DRY_RUN=true. No toca AzuraCast ni reinicia Docker.
EOF
}

# ── Utilidades ───────────────────────────────────────────────────────────────

need_arg() {
    [ $# -ge 2 ] || die "$1 necesita un valor"
}

pkg_install() {
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q "$@"
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q "$@"
    else
        die "no reconozco el gestor de paquetes; instalá $* a mano"
    fi
}

random_token() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
    fi
}

normalize_env_file() {
    [ -f "$1" ] || return 0
    sed -i 's/\r$//' "$1"
}

env_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    KEY="$key" awk '
        BEGIN { k = ENVIRON["KEY"] }
        index($0, k "=") == 1 { v = substr($0, length(k) + 2) }
        END { sub(/\r$/, "", v); print v }
    ' "$file"
}

env_set() {
    local file="$1" key="$2" value="$3" tmp
    tmp="$(mktemp)"
    KEY="$key" VALUE="$value" awk '
        BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
        index($0, k "=") == 1 { print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

is_placeholder() {
    case "$1" in
        ""|cambiar-por-un-token|changeme) return 0 ;;
    esac
    return 1
}

ensure_secret() {
    local file="$1" key="$2"
    if is_placeholder "$(env_get "$file" "$key")"; then
        env_set "$file" "$key" "$(random_token)"
        ok "$key generado en ${file#"$BASE_DIR"/}"
    fi
}

ask() {
    local prompt="$1" answer=""
    printf '%s' "$prompt" >&2
    IFS= read -r answer || answer=""
    printf '%s' "$answer"
}

services_include_web() {
    case ",$1," in *,web,*|*,all,*) return 0 ;; esac
    return 1
}

services_include_tts() {
    case ",$1," in *,tts,*|*,all,*) return 0 ;; esac
    return 1
}

validate_port() {
    case "$1" in
        ""|*[!0-9]*) die "puerto inválido: $1" ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ] || die "puerto fuera de rango: $1"
}

validate_domain() {
    printf '%s' "$1" | grep -Eq \
        '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' \
        || die "dominio inválido: $1 (tiene que ser un nombre como alertas.ejemplo.com)"
}

parse_services() {
    local raw="$1" item
    BACKEND=0; WEB=0; TTS=0
    if [ "$raw" = "all" ]; then
        BACKEND=1; WEB=1; TTS=1
    else
        local IFS=','
        for item in $raw; do
            item="${item// /}"
            case "$item" in
                "") ;;
                backend) BACKEND=1 ;;
                web) WEB=1 ;;
                tts) TTS=1 ;;
                *) die "servicio desconocido: $item (backend, web, tts o all)" ;;
            esac
        done
    fi
    if [ "$NO_TTS" -eq 1 ]; then
        TTS=0
    fi
    if [ "$WEB" -eq 1 ] && [ "$BACKEND" -eq 0 ]; then
        BACKEND=1
        warn "la web necesita el backend; se instala también"
    fi
    [ "$BACKEND" -eq 1 ] || [ "$WEB" -eq 1 ] || [ "$TTS" -eq 1 ] \
        || die "no hay ningún servicio para instalar"
}

port_busy() {
    command -v ss >/dev/null 2>&1 || return 1
    ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
}

find_free_port() {
    local port="$1"
    while [ "$port" -le 65535 ]; do
        port_busy "$port" || { printf '%s' "$port"; return 0; }
        port=$((port + 1))
    done
    return 1
}

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
}

container_exists() {
    docker inspect -f '{{.Name}}' "$1" >/dev/null 2>&1
}

wait_healthy() {
    local container="$1" timeout="$2" elapsed=0 status
    while [ "$elapsed" -lt "$timeout" ]; do
        status="$(docker inspect -f \
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$container" 2>/dev/null || echo missing)"
        case "$status" in
            healthy|running) return 0 ;;
        esac
        sleep 3
        elapsed=$((elapsed + 3))
    done
    warn "últimos logs de $container:"
    docker logs --tail 30 "$container" >&2 2>/dev/null || true
    return 1
}

copy_tree() {
    local src="$1" dst="$2"
    mkdir -p "$dst"
    tar -C "$src" \
        --exclude=.git --exclude=.venv --exclude=.pytest_cache \
        --exclude=.ruff_cache --exclude=__pycache__ --exclude=data \
        -cf - . | tar -C "$dst" -xf -
}

compose() {
    docker compose --env-file "$INSTALL_CONF" -f "$BACKEND_COMPOSE" "$@"
}

# ── Argumentos ───────────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        --services) need_arg "$@"; FLAG_SERVICES="$2"; shift ;;
        --port) need_arg "$@"; FLAG_PORT="$2"; shift ;;
        --domain) need_arg "$@"; FLAG_DOMAIN="$2"; shift ;;
        --tls) need_arg "$@"; FLAG_TLS="$2"; shift ;;
        --acme-email) need_arg "$@"; FLAG_EMAIL="$2"; shift ;;
        --cf-token-file) need_arg "$@"; FLAG_CF_FILE="$2"; shift ;;
        --gts-eab-kid) need_arg "$@"; FLAG_EAB_KID="$2"; shift ;;
        --gts-eab-hmac-file) need_arg "$@"; FLAG_EAB_HMAC_FILE="$2"; shift ;;
        --tts-src) need_arg "$@"; TTS_SRC="$2"; shift ;;
        --staging) STAGING=1 ;;
        --no-staging) NO_STAGING=1 ;;
        --configure) CONFIGURE=1 ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        --no-tts) NO_TTS=1 ;;
        --tts-port) need_arg "$@"; FLAG_TTS_PORT="$2"; shift ;;
        --open-firewall) OPEN_FIREWALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "opción desconocida: $1 (usá --help)" ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "ejecutá con sudo: sudo ./install.sh"

# ── Configuración: archivo, flags y asistente ────────────────────────────────

if [ -f "$INSTALL_CONF" ]; then
    normalize_env_file "$INSTALL_CONF"
    PREV_TLS_CA="$(env_get "$INSTALL_CONF" TLS_CA)"
    PREV_STAGING="$(env_get "$INSTALL_CONF" ACME_STAGING)"
    SERVICES="$(env_get "$INSTALL_CONF" SERVICES)"
    WEB_PORT="$(env_get "$INSTALL_CONF" WEB_PORT)"
    DOMAIN="$(env_get "$INSTALL_CONF" DOMAIN)"
    TLS_CA="$(env_get "$INSTALL_CONF" TLS_CA)"
    ACME_DNS="$(env_get "$INSTALL_CONF" ACME_DNS)"
    ACME_EMAIL="$(env_get "$INSTALL_CONF" ACME_EMAIL)"
    ACME_STAGING="$(env_get "$INSTALL_CONF" ACME_STAGING)"
fi

[ -n "$FLAG_SERVICES" ] && SERVICES="$FLAG_SERVICES"
[ -n "$FLAG_PORT" ] && WEB_PORT="$FLAG_PORT"
[ -n "$FLAG_DOMAIN" ] && DOMAIN="$FLAG_DOMAIN"
[ -n "$FLAG_TLS" ] && TLS_CA="$FLAG_TLS"
[ -n "$FLAG_EMAIL" ] && ACME_EMAIL="$FLAG_EMAIL"
[ "$STAGING" -eq 1 ] && ACME_STAGING=1
[ "$NO_STAGING" -eq 1 ] && ACME_STAGING=0

SERVICES="${SERVICES:-all}"
TLS_CA="${TLS_CA:-none}"
ACME_DNS="${ACME_DNS:-cloudflare}"
ACME_STAGING="${ACME_STAGING:-0}"

want_wizard=0
if [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
    if [ "$CONFIGURE" -eq 1 ] || [ ! -f "$INSTALL_CONF" ]; then
        want_wizard=1
    fi
fi

if [ "$want_wizard" -eq 1 ]; then
    printf '\n%sConfiguración de la instalación%s\n' "$C_BLUE" "$C_RESET"
    printf '  Servicios:\n'
    printf '    1) Todo (backend + web + TTS)\n'
    printf '    2) Backend + web\n'
    printf '    3) Solo backend\n'
    printf '    4) Solo TTS\n'
    printf '    5) Elegir una combinación\n'

    current=1
    case "$SERVICES" in
        backend,web) current=2 ;;
        backend) current=3 ;;
        tts) current=4 ;;
    esac
    answer="$(ask "  Elegí [${current}]: ")"
    answer="${answer:-$current}"
    case "$answer" in
        1) SERVICES=all ;;
        2) SERVICES=backend,web ;;
        3) SERVICES=backend ;;
        4) SERVICES=tts ;;
        5) answer="$(ask "  Servicios (backend,web,tts): ")"; [ -n "$answer" ] || die "lista vacía"; SERVICES="$answer" ;;
        *) die "opción inválida: $answer" ;;
    esac

    if services_include_web "$SERVICES"; then
        scheme=1
        [ "$TLS_CA" != "none" ] && scheme=2
        answer="$(ask "  Salida: 1) HTTP  2) HTTPS [${scheme}]: ")"
        answer="${answer:-$scheme}"
        case "$answer" in
            1) TLS_CA=none ;;
            2)
                ca=1
                [ "$TLS_CA" = "gts" ] && ca=2
                answer="$(ask "  CA: 1) Let's Encrypt  2) Google Trust Services [${ca}]: ")"
                answer="${answer:-$ca}"
                case "$answer" in
                    1) TLS_CA=letsencrypt ;;
                    2) TLS_CA=gts ;;
                    *) die "opción inválida: $answer" ;;
                esac
                answer="$(ask "  Email de contacto ACME [${ACME_EMAIL}]: ")"
                [ -n "$answer" ] && ACME_EMAIL="$answer"
                if [ "$TLS_CA" = "gts" ]; then
                    if [ ! -f "$ACME_GTS_ENV" ]; then
                        answer="$(ask "  KID de Google Trust Services: ")"
                        [ -n "$answer" ] && FLAG_EAB_KID="$answer"
                        answer="$(ask "  Archivo con la clave HMAC de GTS: ")"
                        [ -n "$answer" ] && FLAG_EAB_HMAC_FILE="$answer"
                    fi
                elif [ ! -f "$ACME_CF_ENV" ]; then
                    answer="$(ask "  Archivo con el token de Cloudflare (CF_Token=...): ")"
                    [ -n "$answer" ] && FLAG_CF_FILE="$answer"
                fi
                ;;
            *) die "opción inválida: $answer" ;;
        esac

        default_port=8081
        [ "$TLS_CA" != "none" ] && default_port=8443
        answer="$(ask "  Puerto público de la web [${WEB_PORT:-$default_port}]: ")"
        [ -n "$answer" ] && WEB_PORT="$answer"

        answer="$(ask "  Dominio o subdominio (vacío = sin dominio) [${DOMAIN}]: ")"
        [ -n "$answer" ] && DOMAIN="$answer"
    fi

    if services_include_tts "$SERVICES" && [ ! -f "$TTS_COMPOSE" ] && [ -z "$TTS_SRC" ]; then
        answer="$(ask "  Ruta del proyecto radio-alerts-tts (vacío = sin TTS): ")"
        [ -n "$answer" ] && TTS_SRC="$answer"
    fi
fi

case "$TLS_CA" in
    none|letsencrypt|gts) ;;
    *) die "--tls admite none, letsencrypt o gts" ;;
esac

TTS_EXPLICIT=0
if [ "$SERVICES" != "all" ] && services_include_tts "$SERVICES"; then
    TTS_EXPLICIT=1
fi

parse_services "$SERVICES"

if [ "$TLS_CA" = "none" ]; then
    SCHEME=http
else
    SCHEME=https
fi

if [ "$WEB" -eq 0 ]; then
    [ "$SCHEME" = "https" ] && warn "HTTPS sin web: se ignora"
    SCHEME=http; TLS_CA=none; DOMAIN=""; WEB_PORT=""
elif [ "$SCHEME" = "https" ]; then
    WEB_PORT="${WEB_PORT:-8443}"
    INTERNAL_PORT=443
    [ -n "$DOMAIN" ] || die "HTTPS necesita --domain (por ejemplo alertas.kumakuma.fm)"
    [ -n "$ACME_EMAIL" ] || die "HTTPS necesita --acme-email"
else
    WEB_PORT="${WEB_PORT:-8081}"
    INTERNAL_PORT=80
fi

if [ -n "$WEB_PORT" ]; then
    validate_port "$WEB_PORT"
fi

if [ -n "$DOMAIN" ]; then
    DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
    validate_domain "$DOMAIN"
fi

# ── 1. Comprobaciones previas ────────────────────────────────────────────────

log "Instalador de alertas P2P/JMA en $BASE_DIR"

[ -f "$BACKEND_COMPOSE" ] || die "no encuentro deploy/docker-compose.yml; corré el script desde la raíz del proyecto"
[ -f "$BACKEND_ENV_EXAMPLE" ] || die "no encuentro config/.env.example"

case "$(printf '%s' "$BASE_DIR" | tr '[:upper:]' '[:lower:]')" in
    */azuracast*) die "este stack no se instala dentro de AzuraCast" ;;
esac

free_mb="$(df -Pk "$BASE_DIR" | awk 'NR == 2 { print int($4 / 1024) }')"
if [ "${free_mb:-0}" -lt 2048 ]; then
    die "quedan ${free_mb} MB libres; se necesitan al menos 2 GB para las imágenes"
elif [ "${free_mb:-0}" -lt 5120 ]; then
    warn "quedan ${free_mb} MB libres; con Kokoro conviene tener más de 5 GB"
fi

if [ -f /proc/meminfo ]; then
    mem_mb="$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)"
    [ "${mem_mb:-0}" -ge 3500 ] || \
        warn "solo ${mem_mb} MB de RAM; la cadena TTS pide 2 GB y el resto del servidor sigue funcionando"
fi

case "$WEB_PORT" in
    80|443) warn "el puerto $WEB_PORT lo usa normalmente AzuraCast; comprobá que esté libre" ;;
esac

ok "espacio y permisos correctos"

# ── 2. Docker ────────────────────────────────────────────────────────────────

if command -v docker >/dev/null 2>&1; then
    ok "Docker presente: $(docker --version | cut -d, -f1)"
else
    log "Instalando Docker (no estaba instalado)"
    command -v curl >/dev/null 2>&1 || pkg_install curl ca-certificates
    tmp_script="$(mktemp)"
    curl -fsSL https://get.docker.com -o "$tmp_script"
    sh "$tmp_script"
    rm -f "$tmp_script"
    ok "Docker instalado"
fi

if ! docker compose version >/dev/null 2>&1; then
    log "Instalando el plugin de Docker Compose"
    pkg_install docker-compose-plugin >/dev/null 2>&1 \
        || pkg_install docker-compose-v2 >/dev/null 2>&1 \
        || die "no pude instalar docker compose; instalá docker-compose-plugin a mano"
fi

if ! docker info >/dev/null 2>&1; then
    log "El daemon de Docker no responde; lo arranco (no se reinicia nada más)"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now docker || true
    fi
    docker info >/dev/null 2>&1 || die "Docker no arranca; revisá 'systemctl status docker'"
fi
compose_version="$(docker compose version --short 2>/dev/null || true)"
ok "Docker ${compose_version:+$compose_version }listo"

# ── 3. Red compartida ────────────────────────────────────────────────────────

if docker network inspect radio-alerts >/dev/null 2>&1; then
    ok "red radio-alerts ya existe"
else
    docker network create radio-alerts >/dev/null
    ok "red radio-alerts creada"
fi

# ── 4. Cadena TTS ────────────────────────────────────────────────────────────

if [ "$TTS" -eq 1 ] && [ -n "$TTS_SRC" ]; then
    [ -f "$TTS_SRC/docker-compose.yml" ] || die "$TTS_SRC no parece el proyecto radio-alerts-tts"
    log "Copiando la cadena TTS desde $TTS_SRC"
    copy_tree "$TTS_SRC" "$TTS_DIR"
    ok "cadena TTS copiada a ./tts"
fi

if [ "$TTS" -eq 1 ] && [ ! -f "$TTS_COMPOSE" ]; then
    if [ "$TTS_EXPLICIT" -eq 1 ]; then
        die "pediste la cadena TTS pero no encuentro ./tts/docker-compose.yml; usá --tts-src"
    fi
    warn "no encontré ./tts/docker-compose.yml; sigo sin la cadena TTS"
    TTS=0
fi

if [ "$TTS" -eq 1 ] && [ ! -f "$TTS_DIR/.env.example" ] && [ ! -f "$TTS_ENV" ]; then
    die "tts/.env.example no existe; copiá la cadena TTS completa o usá --no-tts"
fi

if [ "$TTS" -eq 1 ]; then
    mkdir -p "$TTS_DIR/data"
    chown -R 10001:10001 "$TTS_DIR/data"
fi

if [ "$BACKEND" -eq 1 ]; then SERVICES_NORM="backend"; fi
if [ "$WEB" -eq 1 ]; then SERVICES_NORM="${SERVICES_NORM:+$SERVICES_NORM,}web"; fi
if [ "$TTS" -eq 1 ]; then SERVICES_NORM="${SERVICES_NORM:+$SERVICES_NORM,}tts"; fi
[ -n "$SERVICES_NORM" ] || die "no quedó ningún servicio para instalar"

# ── 5. Directorios y configuración ───────────────────────────────────────────

mkdir -p "$CONFIG_DIR" "$BASE_DIR/logs"

if [ "$BACKEND" -eq 1 ]; then
    mkdir -p "$BASE_DIR/data"
    chown -R 10001:10001 "$BASE_DIR/data" "$BASE_DIR/logs"
fi

: > "$INSTALL_CONF"
env_set "$INSTALL_CONF" SERVICES "$SERVICES_NORM"
env_set "$INSTALL_CONF" WEB_PORT "$WEB_PORT"
env_set "$INSTALL_CONF" WEB_INTERNAL_PORT "$INTERNAL_PORT"
env_set "$INSTALL_CONF" SCHEME "$SCHEME"
env_set "$INSTALL_CONF" DOMAIN "$DOMAIN"
env_set "$INSTALL_CONF" TLS_CA "$TLS_CA"
env_set "$INSTALL_CONF" ACME_DNS "$ACME_DNS"
env_set "$INSTALL_CONF" ACME_EMAIL "$ACME_EMAIL"
env_set "$INSTALL_CONF" ACME_STAGING "$ACME_STAGING"
chmod 600 "$INSTALL_CONF"
ok "configuración guardada en config/install.conf ($SERVICES_NORM)"

if [ "$BACKEND" -eq 1 ]; then
    if [ ! -f "$BACKEND_ENV" ]; then
        cp "$BACKEND_ENV_EXAMPLE" "$BACKEND_ENV"
        ok "config/.env creado desde el ejemplo"
    fi
    normalize_env_file "$BACKEND_ENV"
    chmod 600 "$BACKEND_ENV"

    ensure_secret "$BACKEND_ENV" INTERNAL_TOKEN

    backend_dry="$(env_get "$BACKEND_ENV" DRY_RUN)"
    [ "$backend_dry" = "true" ] || warn "config/.env tiene DRY_RUN=$backend_dry: se anunciaría al aire"
fi

if [ "$TTS" -eq 1 ]; then
    if [ ! -f "$TTS_ENV" ]; then
        cp "$TTS_DIR/.env.example" "$TTS_ENV"
        ok "tts/.env creado desde el ejemplo"
    fi
    normalize_env_file "$TTS_ENV"
    chmod 600 "$TTS_ENV"

    ensure_secret "$TTS_ENV" API_TOKEN

    tts_port="$(env_get "$TTS_ENV" API_PORT)"
    tts_port="${tts_port:-8090}"
    if [ -n "$FLAG_TTS_PORT" ]; then
        validate_port "$FLAG_TTS_PORT"
        tts_port="$FLAG_TTS_PORT"
    fi

    if port_busy "$tts_port" && ! container_running kuma-alerts-tts; then
        if [ -n "$FLAG_TTS_PORT" ]; then
            die "el puerto $tts_port para el TTS está ocupado"
        fi
        free_tts_port="$(find_free_port $((tts_port + 10000)))" \
            || die "no encuentro un puerto libre para la API del TTS; pasá --tts-port"
        warn "el puerto $tts_port está ocupado (AzuraCast publica 8090); la API del TTS pasa a $free_tts_port"
        tts_port="$free_tts_port"
    fi
    env_set "$TTS_ENV" API_PORT "$tts_port"

    if [ "$BACKEND" -eq 1 ]; then
        env_set "$BACKEND_ENV" TTS_API_TOKEN "$(env_get "$TTS_ENV" API_TOKEN)"
        env_set "$BACKEND_ENV" TTS_API_URL "http://alerts-tts:$tts_port"
        ok "TTS_API_TOKEN y TTS_API_URL sincronizados con la cadena TTS"
    fi

    tts_dry="$(env_get "$TTS_ENV" DRY_RUN)"
    if [ "$tts_dry" != "true" ]; then
        warn "tts/.env tiene DRY_RUN=$tts_dry: el audio saldría al aire"
    fi
    tts_password="$(env_get "$TTS_ENV" AZURACAST_STREAMER_PASSWORD)"
    case "$tts_password" in
        ""|cambiar-esto)
            warn "tts/.env sin AZURACAST_STREAMER_PASSWORD; con DRY_RUN=true arranca igual" ;;
    esac
fi

# ── 6. Web y certificado ─────────────────────────────────────────────────────

prepare_cf_credentials() {
    if [ -n "$FLAG_CF_FILE" ]; then
        if [ ! -r "$FLAG_CF_FILE" ]; then
            # Un token pegado por error no se imprime: quedaría en el log y en
            # el historial de la terminal.
            case "$FLAG_CF_FILE" in
                */*) die "no puedo leer $FLAG_CF_FILE" ;;
                *) die "--cf-token-file espera la RUTA de un archivo con CF_Token=..., no el token; guardalo en /root/cf-token.env" ;;
            esac
        fi
        local token
        token="$(awk 'NF { sub(/^CF_Token=/, ""); print; exit }' "$FLAG_CF_FILE" \
            | tr -d '\r' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -n "$token" ] || die "el archivo del token de Cloudflare está vacío"
        ( umask 077; printf 'CF_Token=%s\n' "$token" > "$ACME_CF_ENV" )
        unset token
        ok "token de Cloudflare guardado en config/acme/cloudflare.env"
    fi
    [ -f "$ACME_CF_ENV" ] || die "falta el token de Cloudflare (pasá --cf-token-file)"
    chmod 600 "$ACME_CF_ENV"
}

prepare_gts_credentials() {
    [ -f "$ACME_GTS_ENV" ] || : > "$ACME_GTS_ENV"
    chmod 600 "$ACME_GTS_ENV"
    if [ -n "$FLAG_EAB_KID" ]; then
        env_set "$ACME_GTS_ENV" GTS_EAB_KID "$FLAG_EAB_KID"
    fi
    if [ -n "$FLAG_EAB_HMAC_FILE" ]; then
        [ -r "$FLAG_EAB_HMAC_FILE" ] || die "no puedo leer $FLAG_EAB_HMAC_FILE"
        local hmac
        hmac="$(awk 'NF { sub(/^GTS_EAB_HMAC_KEY=/, ""); print; exit }' "$FLAG_EAB_HMAC_FILE" \
            | tr -d '\r' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -n "$hmac" ] || die "el archivo HMAC de GTS está vacío"
        env_set "$ACME_GTS_ENV" GTS_EAB_HMAC_KEY "$hmac"
        unset hmac
        ok "credenciales EAB guardadas en config/acme/gts.env"
    fi
    [ -n "$(env_get "$ACME_GTS_ENV" GTS_EAB_KID)" ] || die "falta --gts-eab-kid"
    [ -n "$(env_get "$ACME_GTS_ENV" GTS_EAB_HMAC_KEY)" ] || die "falta --gts-eab-hmac-file"
}

ensure_acme() {
    [ -x "$ACME_BIN" ] && return 0
    log "Instalando acme.sh en config/acme"
    command -v curl >/dev/null 2>&1 || pkg_install curl ca-certificates
    command -v openssl >/dev/null 2>&1 || pkg_install openssl
    local tmp
    tmp="$(mktemp -d)"
    if ! curl -fsSL "$ACME_URL" -o "$tmp/acme.tar.gz"; then
        rm -rf "$tmp"
        die "no pude descargar acme.sh: $ACME_URL"
    fi
    if ! tar -xzf "$tmp/acme.tar.gz" -C "$tmp" --strip-components=1; then
        rm -rf "$tmp"
        die "no pude descomprimir acme.sh"
    fi
    ( cd "$tmp" && ./acme.sh --install --home "$ACME_HOME" --config-home "$ACME_HOME" --nocron >/dev/null )
    rm -rf "$tmp"
    [ -x "$ACME_BIN" ] || die "acme.sh no quedó instalado en config/acme/home"
    ok "acme.sh instalado"
}

issue_certificate() {
    local ca_server="$LE_SERVER" force=""

    if [ "$TLS_CA" = "gts" ]; then
        ca_server="$GTS_SERVER"
    elif [ "$ACME_STAGING" = "1" ]; then
        ca_server="$LE_STAGING"
    fi

    if [ -n "$PREV_TLS_CA" ] && \
        { [ "$PREV_TLS_CA" != "$TLS_CA" ] || [ "$PREV_STAGING" != "$ACME_STAGING" ]; }; then
        force="--force"
    fi

    log "Registrando la cuenta ACME ($TLS_CA)"
    if [ "$TLS_CA" = "gts" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ACME_GTS_ENV"
        set +a
        "$ACME_BIN" --register-account -m "$ACME_EMAIL" --server "$ca_server" \
            --eab-kid "$GTS_EAB_KID" --eab-hmac-key "$GTS_EAB_HMAC_KEY" >/dev/null
        unset GTS_EAB_HMAC_KEY GTS_EAB_KID
    else
        "$ACME_BIN" --register-account -m "$ACME_EMAIL" --server "$ca_server" >/dev/null
    fi

    log "Emitiendo el certificado para $DOMAIN (DNS-01 en $ACME_DNS)"
    set -a
    # shellcheck disable=SC1090
    . "$ACME_CF_ENV"
    set +a
    if ! "$ACME_BIN" --issue --server "$ca_server" -d "$DOMAIN" --dns dns_cf \
            --keylength ec-256 ${force:+$force} >>"$ACME_LOG" 2>&1; then
        unset CF_Token
        warn "últimas líneas del intento de emisión:"
        tail -n 20 "$ACME_LOG" >&2 || true
        die "no se pudo emitir el certificado; revisá el token y que el subdominio esté en esa zona"
    fi
    unset CF_Token

    "$ACME_BIN" --install-cert -d "$DOMAIN" --ecc \
        --fullchain-file "$CERTS_DIR/fullchain.pem" \
        --key-file "$CERTS_DIR/key.pem" \
        --reloadcmd "$BASE_DIR/deploy/acme-reload.sh" >/dev/null
    chmod 644 "$CERTS_DIR/fullchain.pem"
    chmod 600 "$CERTS_DIR/key.pem"
    ok "certificado instalado en config/certs"
}

write_cron() {
    if [ "$TLS_CA" = "letsencrypt" ] && [ "$ACME_STAGING" = "1" ]; then
        return 0
    fi
    [ -d /etc/cron.d ] || { warn "no hay /etc/cron.d; la renovación queda manual"; return 0; }
    {
        printf '# Renovación de certificados del stack de alertas (install.sh)\n'
        printf 'SHELL=/bin/sh\n'
        printf 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
        printf '17 3 * * * root %s/deploy/acme-renew.sh\n' "$BASE_DIR"
    } > "$CRON_FILE"
    chmod 644 "$CRON_FILE"
    ok "renovación automática programada (03:17, logs/acme.log)"
}

validate_nginx() {
    local output
    output="$(docker run --rm --entrypoint nginx \
        -v "$NGINX_RENDERED_DIR:/etc/nginx/conf.d:ro" \
        -v "$CERTS_DIR:/etc/certs:ro" \
        nginx:1.27-alpine -t 2>&1)" || {
        printf '%s\n' "$output" >&2
        die "nginx -t falló; no se levanta nada"
    }
    ok "configuración de nginx válida"
}

if [ "$WEB" -eq 1 ]; then
    mkdir -p "$NGINX_RENDERED_DIR/snippets" "$CERTS_DIR"
    chmod 700 "$CERTS_DIR"
    cp "$NGINX_SRC_DIR/snippets/"*.inc "$NGINX_RENDERED_DIR/snippets/"
    chmod 644 "$NGINX_RENDERED_DIR/snippets/"*.inc

    if [ "$SCHEME" = "https" ]; then
        sed "s|__DOMAIN__|$DOMAIN|g" "$NGINX_SRC_DIR/radio-alerts-tls.conf" \
            > "$NGINX_RENDERED_DIR/site.conf"
    else
        sed "s|^    server_name _;|    server_name ${DOMAIN:-_};|" \
            "$NGINX_SRC_DIR/radio-alerts.conf" > "$NGINX_RENDERED_DIR/site.conf"
    fi
    chmod 644 "$NGINX_RENDERED_DIR/site.conf"
    ok "configuración de nginx generada para ${SCHEME}://${DOMAIN:-_}"

    if [ "$SCHEME" = "https" ]; then
        mkdir -p "$ACME_DIR"
        chmod 700 "$ACME_DIR"
        # acme.sh y cron los invocan directo: no dependen del bit que traiga git.
        chmod +x "$BASE_DIR/deploy/acme-reload.sh" "$BASE_DIR/deploy/acme-renew.sh"
        if [ "$TLS_CA" = "gts" ]; then
            prepare_gts_credentials
        else
            prepare_cf_credentials
        fi
        ensure_acme
        issue_certificate
        write_cron
    elif [ -f "$CRON_FILE" ]; then
        rm -f "$CRON_FILE"
    fi

    validate_nginx
fi

# ── 7. Puertos y arranque ────────────────────────────────────────────────────

if [ "$WEB" -eq 1 ] && port_busy "$WEB_PORT"; then
    # Si el puerto lo publica nuestro propio contenedor, se recrea sin problema.
    if ! { container_running kuma-alerts-web \
        && docker port kuma-alerts-web 2>/dev/null | grep -q ":$WEB_PORT\$"; }; then
        case "$WEB_PORT" in
            80|443) die "el puerto $WEB_PORT está ocupado (AzuraCast); elegí otro, por ejemplo 8443" ;;
            *) die "el puerto $WEB_PORT está ocupado; elegí otro con --port" ;;
        esac
    fi
fi
if [ "$TTS" -eq 1 ] && port_busy "${tts_port:-8090}" && ! container_running kuma-alerts-tts; then
    warn "el puerto ${tts_port:-8090} está ocupado; la cadena TTS no va a poder publicarse en loopback"
fi

if [ "$WEB" -eq 0 ] && container_exists kuma-alerts-web; then
    compose stop web >/dev/null 2>&1 || true
    ok "web detenida (no está en la selección)"
fi
if [ "$BACKEND" -eq 0 ] && container_exists kuma-alerts-backend; then
    compose stop backend >/dev/null 2>&1 || true
    ok "backend detenido (no está en la selección)"
fi
if [ "$TTS" -eq 0 ] && [ -f "$TTS_COMPOSE" ] && container_exists kuma-alerts-tts; then
    docker compose -f "$TTS_COMPOSE" stop >/dev/null 2>&1 || true
    ok "cadena TTS detenida (no está en la selección)"
fi

up_services=()
[ "$BACKEND" -eq 1 ] && up_services+=(backend)
[ "$WEB" -eq 1 ] && up_services+=(web)

if [ "${#up_services[@]}" -gt 0 ]; then
    log "Construyendo y levantando: $SERVICES_NORM (la primera vez descarga la imagen base)"
    compose up -d --build "${up_services[@]}" \
        || die "falló el build; revisá el error de arriba"
fi

if [ "$BACKEND" -eq 1 ]; then
    if ! wait_healthy kuma-alerts-backend 180; then
        FAILURES=$((FAILURES + 1))
        warn "el backend no llegó a 'healthy'; revisá: docker logs kuma-alerts-backend"
    else
        ok "backend healthy"
    fi
fi

if [ "$WEB" -eq 1 ]; then
    if container_running kuma-alerts-web; then
        ok "web (nginx) corriendo"
    else
        FAILURES=$((FAILURES + 1))
        warn "la web no está corriendo; revisá: docker logs kuma-alerts-web"
    fi
fi

# ── 8. Cadena TTS ────────────────────────────────────────────────────────────

if [ "$TTS" -eq 1 ]; then
    log "Construyendo la cadena TTS (el modelo Kokoro se arma en la imagen; tarda varios minutos)"
    if docker compose -f "$TTS_COMPOSE" up -d --build; then
        if wait_healthy kuma-alerts-tts 300; then
            ok "cadena TTS healthy"
        else
            FAILURES=$((FAILURES + 1))
            warn "la cadena TTS no llegó a 'healthy'; revisá: docker logs kuma-alerts-tts"
        fi
    else
        FAILURES=$((FAILURES + 1))
        warn "falló el build de la cadena TTS"
    fi
fi

# ── 9. Verificación ──────────────────────────────────────────────────────────

ip_address="$(hostname -I 2>/dev/null | awk '{print $1}')"
ip_address="${ip_address:-IP-DEL-VPS}"

if [ "$BACKEND" -eq 1 ]; then
    if curl -fsS --max-time 5 http://127.0.0.1:3000/health >/dev/null 2>&1; then
        ok "GET /health del backend responde"
    else
        FAILURES=$((FAILURES + 1))
        warn "el backend no responde en 127.0.0.1:3000"
    fi
fi

web_url=""
cert_line=""
if [ "$WEB" -eq 1 ]; then
    if [ "$SCHEME" = "https" ]; then
        web_url="https://$DOMAIN:$WEB_PORT/"
        check_opts=(--resolve "$DOMAIN:$WEB_PORT:127.0.0.1")
    else
        web_url="http://${DOMAIN:-$ip_address}:$WEB_PORT/"
        if [ -n "$DOMAIN" ]; then
            check_opts=(--resolve "$DOMAIN:$WEB_PORT:127.0.0.1")
        else
            check_opts=()
        fi
    fi

    internal_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
        "${check_opts[@]}" "${web_url}internal/trigger-tts" 2>/dev/null || true)"
    internal_code="${internal_code:-000}"
    if [ "$internal_code" = "404" ]; then
        ok "nginx bloquea /internal/* (404)"
    else
        warn "nginx devolvió $internal_code en /internal/trigger-tts; debería ser 404"
    fi

    if [ "$SCHEME" = "https" ]; then
        if command -v openssl >/dev/null 2>&1; then
            cert_line="$(openssl x509 -in "$CERTS_DIR/fullchain.pem" -noout -issuer -enddate 2>/dev/null \
                | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
        fi
        if [ -f "$CERTS_DIR/fullchain.pem" ]; then
            ok "certificado en config/certs ($DOMAIN)"
        else
            FAILURES=$((FAILURES + 1))
            warn "no encuentro config/certs/fullchain.pem"
        fi
    fi

    if [ "$OPEN_FIREWALL" -eq 1 ] && command -v ufw >/dev/null 2>&1; then
        ufw allow "$WEB_PORT/tcp" >/dev/null && ok "ufw: $WEB_PORT/tcp habilitado"
    elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
        ufw status 2>/dev/null | grep -q "$WEB_PORT" \
            || warn "ufw está activo y $WEB_PORT no está abierto; usá --open-firewall o 'ufw allow $WEB_PORT/tcp'"
    fi
fi

if [ "$TTS" -eq 1 ]; then
    tts_status="http://127.0.0.1:${tts_port:-8090}/health"
else
    tts_status="no instalada"
fi

if [ "$BACKEND" -eq 1 ]; then
    if [ "$(env_get "$BACKEND_ENV" DRY_RUN)" = "false" ]; then
        dry_state="DRY_RUN=false (audio al aire)"
    else
        dry_state="DRY_RUN=true (sin audio al aire)"
    fi
else
    dry_state="sin backend en esta instalación"
fi

if [ "$WEB" -eq 1 ]; then
    web_status="$web_url"
else
    web_status="no instalada"
fi
if [ "$BACKEND" -eq 1 ]; then
    backend_status="http://127.0.0.1:3000/health"
else
    backend_status="no instalado"
fi

cert_block=""
if [ "$SCHEME" = "https" ]; then
    cert_block="  Certificado          ${cert_line:-ver config/certs}
  Renovación           automática 03:17 (logs/acme.log)"
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf '%sInstalación completa.%s\n\n' "$C_GREEN" "$C_RESET"
else
    printf '%sInstalación terminada con %s problema(s).%s\n\n' "$C_YELLOW" "$FAILURES" "$C_RESET"
fi

cat <<EOF
  Servicios            $SERVICES_NORM
  Web para locutores   $web_status
  Backend (loopback)   $backend_status
  Cadena TTS           $tts_status
$cert_block

  Modo actual          $dry_state

  Lo elegido quedó en config/install.conf; reejecutar el script no pisa
  config/.env, tts/.env ni data/. Para cambiar algo: sudo ./install.sh --configure

Siguientes pasos
  1. Revisar config/.env (umbrales, TYPHOON_AREAS, Discord) y tts/.env.
  2. Simulacro sin audio:
       TOKEN=\$(grep '^INTERNAL_TOKEN=' config/.env | cut -d= -f2-)
       curl -s -X POST http://127.0.0.1:3000/internal/simulate \\
         -H "Authorization: Bearer \$TOKEN" -H 'Content-Type: application/json' \\
         -d '{"fixture":"eew"}'
     (requiere ENABLE_SIMULATE=true y recrear el backend)
  3. Paso a audio real: docs/OPERACION.md, sección 6, solo con autorización.
EOF

if [ "$FAILURES" -ne 0 ]; then
    exit 1
fi
