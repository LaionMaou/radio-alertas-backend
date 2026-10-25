#!/usr/bin/env bash
# Instalador del stack de alertas para el VPS.
#
#   cd /var/alertas
#   sudo ./install.sh
#
# Deja funcionando el backend P2P/JMA, la web para locutores y, si está
# disponible, la cadena TTS. Todo arranca con DRY_RUN=true: no sale audio al
# aire. No toca AzuraCast ni reinicia el daemon de Docker si ya estaba andando.
#
# Opciones:
#   --no-tts              instalar solo backend + web
#   --tts-src RUTA        copiar la cadena TTS desde RUTA a ./tts antes de instalar
#   --open-firewall       abrir 8081/tcp en ufw (si ufw está activo)
#   -h, --help            mostrar esta ayuda
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_COMPOSE="$BASE_DIR/deploy/docker-compose.yml"
BACKEND_ENV="$BASE_DIR/config/.env"
BACKEND_ENV_EXAMPLE="$BASE_DIR/config/.env.example"
TTS_DIR="$BASE_DIR/tts"
TTS_COMPOSE="$TTS_DIR/docker-compose.yml"
TTS_ENV="$TTS_DIR/.env"

WITH_TTS=1
TTS_SRC=""
OPEN_FIREWALL=0
FAILURES=0

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
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-tts) WITH_TTS=0 ;;
        --tts-src)
            [ $# -ge 2 ] || die "--tts-src necesita una ruta"
            TTS_SRC="$2"; shift ;;
        --open-firewall) OPEN_FIREWALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "opción desconocida: $1 (usá --help)" ;;
    esac
    shift
done

# ── Utilidades ───────────────────────────────────────────────────────────────

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

port_busy() {
    command -v ss >/dev/null 2>&1 || return 1
    ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
}

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
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

# ── 1. Comprobaciones previas ────────────────────────────────────────────────

log "Instalador de alertas P2P/JMA en $BASE_DIR"

[ "$(id -u)" -eq 0 ] || die "ejecutá con sudo: sudo ./install.sh"
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
    command -v systemctl >/dev/null 2>&1 && systemctl enable --now docker
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

if [ "$WITH_TTS" -eq 1 ] && [ -n "$TTS_SRC" ]; then
    [ -f "$TTS_SRC/docker-compose.yml" ] || die "$TTS_SRC no parece el proyecto radio-alerts-tts"
    log "Copiando la cadena TTS desde $TTS_SRC"
    copy_tree "$TTS_SRC" "$TTS_DIR"
    ok "cadena TTS copiada a ./tts"
fi

if [ "$WITH_TTS" -eq 1 ] && [ ! -f "$TTS_COMPOSE" ]; then
    warn "no encontré ./tts/docker-compose.yml; sigo solo con backend + web"
    WITH_TTS=0
fi

if [ "$WITH_TTS" -eq 1 ] && [ ! -f "$TTS_DIR/.env.example" ] && [ ! -f "$TTS_ENV" ]; then
    die "tts/.env.example no existe; copiá la cadena TTS completa o usá --no-tts"
fi

if [ "$WITH_TTS" -eq 1 ]; then
    mkdir -p "$TTS_DIR/data"
    chown -R 10001:10001 "$TTS_DIR/data"
fi

# ── 5. Directorios y configuración ───────────────────────────────────────────

mkdir -p "$BASE_DIR/data" "$BASE_DIR/logs" "$BASE_DIR/config"
chown -R 10001:10001 "$BASE_DIR/data" "$BASE_DIR/logs"

if [ ! -f "$BACKEND_ENV" ]; then
    cp "$BACKEND_ENV_EXAMPLE" "$BACKEND_ENV"
    ok "config/.env creado desde el ejemplo"
fi
normalize_env_file "$BACKEND_ENV"
chmod 600 "$BACKEND_ENV"

ensure_secret "$BACKEND_ENV" INTERNAL_TOKEN

if [ "$WITH_TTS" -eq 1 ]; then
    if [ ! -f "$TTS_ENV" ]; then
        cp "$TTS_DIR/.env.example" "$TTS_ENV"
        ok "tts/.env creado desde el ejemplo"
    fi
    normalize_env_file "$TTS_ENV"
    chmod 600 "$TTS_ENV"

    ensure_secret "$TTS_ENV" API_TOKEN

    tts_port="$(env_get "$TTS_ENV" API_PORT)"
    tts_port="${tts_port:-8090}"
    env_set "$BACKEND_ENV" TTS_API_TOKEN "$(env_get "$TTS_ENV" API_TOKEN)"
    env_set "$BACKEND_ENV" TTS_API_URL "http://alerts-tts:$tts_port"
    ok "TTS_API_TOKEN y TTS_API_URL sincronizados con la cadena TTS"

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

backend_dry="$(env_get "$BACKEND_ENV" DRY_RUN)"
[ "$backend_dry" = "true" ] || warn "config/.env tiene DRY_RUN=$backend_dry: se anunciaría al aire"

# ── 6. Puertos ───────────────────────────────────────────────────────────────

if port_busy 8081 && ! container_running kuma-alerts-web; then
    die "el puerto 8081 está ocupado por otro proceso; liberalo o cambiá el mapeo en deploy/docker-compose.yml"
fi
if [ "$WITH_TTS" -eq 1 ] && port_busy 8090 && ! container_running kuma-alerts-tts; then
    warn "el puerto 8090 está ocupado; la cadena TTS no va a poder publicarse en loopback"
fi

# ── 7. Backend + web ─────────────────────────────────────────────────────────

log "Construyendo backend y web (la primera vez descarga la imagen base de Python)"
docker compose -f "$BACKEND_COMPOSE" up -d --build \
    || die "falló el build del backend; revisá el error de arriba"

if ! wait_healthy kuma-alerts-backend 180; then
    FAILURES=$((FAILURES + 1))
    warn "el backend no llegó a 'healthy'; revisá: docker logs kuma-alerts-backend"
else
    ok "backend healthy"
fi

if container_running kuma-alerts-web; then
    ok "web (nginx) corriendo"
else
    FAILURES=$((FAILURES + 1))
    warn "la web no está corriendo; revisá: docker logs kuma-alerts-web"
fi

# ── 8. Cadena TTS ────────────────────────────────────────────────────────────

if [ "$WITH_TTS" -eq 1 ]; then
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

if curl -fsS --max-time 5 http://127.0.0.1:3000/health >/dev/null 2>&1; then
    ok "GET /health del backend responde"
else
    FAILURES=$((FAILURES + 1))
    warn "el backend no responde en 127.0.0.1:3000"
fi

internal_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    http://127.0.0.1:8081/internal/trigger-tts 2>/dev/null || echo 000)"
if [ "$internal_code" = "404" ]; then
    ok "nginx bloquea /internal/* (404)"
else
    warn "nginx devolvió $internal_code en /internal/trigger-tts; debería ser 404"
fi

if [ "$OPEN_FIREWALL" -eq 1 ] && command -v ufw >/dev/null 2>&1; then
    ufw allow 8081/tcp >/dev/null && ok "ufw: 8081/tcp habilitado"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw status 2>/dev/null | grep -q '8081' \
        || warn "ufw está activo y 8081 no está abierto; usá --open-firewall o 'ufw allow 8081/tcp'"
fi

ip_address="$(hostname -I 2>/dev/null | awk '{print $1}')"
ip_address="${ip_address:-IP-DEL-VPS}"

if [ "$WITH_TTS" -eq 1 ]; then
    tts_status="http://127.0.0.1:${tts_port:-8090}/health"
else
    tts_status="no instalada"
fi
if [ "$(env_get "$BACKEND_ENV" DRY_RUN)" = "false" ]; then
    dry_state="DRY_RUN=false (audio al aire)"
else
    dry_state="DRY_RUN=true (sin audio al aire)"
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf '%sInstalación completa.%s\n\n' "$C_GREEN" "$C_RESET"
else
    printf '%sInstalación terminada con %s problema(s).%s\n\n' "$C_YELLOW" "$FAILURES" "$C_RESET"
fi

cat <<EOF
  Web para locutores   http://$ip_address:8081/
  Backend (loopback)   http://127.0.0.1:3000/health
  Cadena TTS           $tts_status

  Modo actual          $dry_state

  Todo corre en segundo plano y vuelve a levantarse al reiniciar el VPS.

Siguientes pasos
  1. Revisar config/.env (umbrales, TYPHOON_AREAS, Discord) y tts/.env.
  2. Simulacro sin audio:
       TOKEN=\$(grep '^INTERNAL_TOKEN=' config/.env | cut -d= -f2-)
       curl -s -X POST http://127.0.0.1:3000/internal/simulate \\
         -H "Authorization: Bearer \$TOKEN" -H 'Content-Type: application/json' \\
         -d '{"fixture":"eew"}'
     (requiere ENABLE_SIMULATE=true y recrear el backend)
  3. Paso a audio real: docs/OPERACION.md, sección 6, solo con autorización.

  Reejecutar este script es seguro: no pisa config/.env, tts/.env ni data/.
EOF

if [ "$FAILURES" -ne 0 ]; then
    exit 1
fi
