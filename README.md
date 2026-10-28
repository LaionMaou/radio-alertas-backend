# Backend de alertas P2P/JMA

Servicio que escucha los avisos sísmicos y de tsunami de P2PQuake, los enriquece
con datos de la JMA, decide si corresponde anunciarlos y dispara la cadena TTS
(`radio-alerts-tts`). Incluye una página estática para que los locutores vean el
estado sin llamar nunca a P2P ni a la JMA.

Este repositorio es independiente de la app de Kuma y de la cadena TTS. La cadena
TTS se reutiliza por HTTP; aquí no se duplica Kokoro, ffmpeg ni WebDJ.

## Arquitectura

```
P2PQuake WS (1 conexión)          JMA (solo por evento / cada 10 min)
        │                                      │
        └──────────────► backend FastAPI ◄─────┘
                         (normaliza, filtra, guarda, decide)
                           │          │
             SSE / HTTP    │          │ POST /alert + GET /alert/{id}
                           ▼          ▼
                     nginx :8081   alerts-tts :8090
                       (o 8443 TLS)   │
                           │          │
                     web estática     └──► Kokoro → WebDJ → AzuraCast
```

- Red Docker compartida `radio-alerts`: `backend` ↔ `alerts-tts`.
- Red `default` de este compose: `web` ↔ `backend`.
- Puertos publicados: el de la web (`8081` en HTTP, `8443` en HTTPS por defecto,
  configurable) y `127.0.0.1:3000` (backend, solo depuración).
  `POST /internal/*` nunca se publica.

## Estructura

```
install.sh                instalador para el VPS (estilo AzuraCast)
bootstrap.sh              baja el repo desde GitHub y ejecuta install.sh
backend/                  servicio FastAPI (Python 3.11+)
  src/radio_alerts_backend/
  scripts/healthcheck.py
  tests/                  pytest, sin red
web/                      página estática (sin frameworks ni CDN)
deploy/docker-compose.yml
deploy/nginx/radio-alerts.conf
config/.env.example
docs/P2P_Y_JMA.md         contratos externos verificados
docs/OPERACION.md         runbook de despliegue y operación
tts/                      cadena TTS copiada por install.sh (no versionada)
config/install.conf       lo que se eligió al instalar (no versionado)
deploy/nginx/snippets/    headers y proxy compartidos por las dos variantes
deploy/nginx/radio-alerts-tls.conf  variante HTTPS (listen 443 ssl)
deploy/acme-reload.sh     recarga nginx tras emitir/renovar el certificado
deploy/acme-renew.sh      renovación diaria (la llama /etc/cron.d/radio-alertas)
```

## Desarrollo local

```bash
cd backend
python -m venv .venv
./.venv/Scripts/pip install -r requirements-dev.txt   # Windows
# ./.venv/bin/pip install -r requirements-dev.txt     # Linux
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m ruff check .
```

El backend se arranca con `python -m radio_alerts_backend` (un solo worker de
Uvicorn) leyendo `config/.env` si existe. Para pruebas locales conviene
`DRY_RUN=true`, `JMA_ENABLED=false` y un `P2P_WS_URL` inalcanzable, así no se
consulta ningún servicio externo.

## API

| Método | Ruta | Notas |
| --- | --- | --- |
| GET | `/api/alert/latest?kind=` | `{"alert": ...}` o `null`; 200 siempre |
| GET | `/api/alert/history?limit=20&kind=` | 1..50; `{"alerts": [...], "count": n}` |
| GET | `/api/alert/stream` | SSE: `snapshot`, `alert`, `status`; `retry: 5000` |
| GET | `/health` | `status=ok|degraded`, estado P2P/JMA/TTS/store |
| POST | `/internal/trigger-tts` | Bearer `INTERNAL_TOKEN`; respeta `DRY_RUN` |
| POST | `/internal/simulate` | Solo con `ENABLE_SIMULATE=true`; Bearer |

`kind` admite `eew`, `earthquake`, `tsunami` y `typhoon`. Los endpoints internos
no se publican en nginx y no deben exponerse nunca a Internet.

## Página para locutores

`web/` se sirve como archivos estáticos y se reconecta sola:

- SSE contra `api/alert/stream`; si falla tres veces pasa a `fetch` cada 10 s.
- Todo el DOM se construye con `textContent`/`createElement`; nunca `innerHTML`.
- Sin frameworks, sin CDN, sin fuentes externas. CSP en `deploy/nginx`.

## Despliegue

Igual que AzuraCast: el instalador se baja dentro de la carpeta donde querés el
proyecto y se ejecuta desde ahí.

```bash
sudo mkdir -p /var/alertas
cd /var/alertas
curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh -o bootstrap.sh
chmod a+x bootstrap.sh
sudo ./bootstrap.sh install
```

Con terminal interactiva `install.sh` pregunta qué instalar (servicios, puerto,
dominio, HTTP o HTTPS, CA y credenciales) y guarda las respuestas en
`config/install.conf`. Sin TTY, como al pipear, hay que pasar las opciones en la
línea:

```bash
curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh | sudo bash -s -- --services all --domain alertas.ejemplo.com --port 8443 --tls letsencrypt --acme-email ops@ejemplo.com --cf-token-file /root/cf-token.env
```

El proyecto puede vivir en cualquier ruta absoluta: la carpeta actual es el
destino por defecto y `--dir` la cambia (por ejemplo `--dir /srv/alertas`, útil
cuando se pipea). `--branch` elige la rama. Los datos quedan dentro de esa misma
carpeta (`data/`, `logs/`, `tts/data`), así que moverla mueve todo.

Cuando los servicios incluyen la cadena TTS (`all` por defecto), `bootstrap.sh`
baja también `LaionMaou/radio-alertas-tts` y se la entrega al instalador, así que
no hay que clonar nada más. Con `--tts-src RUTA` usa esa copia local y con
`--no-tts` no la instala.

```bash
curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh | sudo bash -s -- --dir /srv/alertas --services backend,web
```

Equivalente manual, con el proyecto ya subido a `/var/alertas`:

```bash
sudo mkdir -p /var/alertas
sudo rsync -a --exclude .git --exclude .venv ./ /var/alertas/
cd /var/alertas
sudo ./install.sh --tts-src /ruta/a/radio-alerts-tts
```

`install.sh` instala Docker y Compose si faltan (nunca reinicia un daemon que ya
está corriendo), crea la red `radio-alerts`, genera `INTERNAL_TOKEN` y el
`API_TOKEN` compartido, construye y levanta todo, y espera el `healthy` de cada
servicio. Se puede reejecutar sin pisar `config/.env`, `tts/.env` ni `data/`.

### Actualizar

Igual que AzuraCast, desde la carpeta de la instalación:

```bash
cd /var/alertas
sudo ./bootstrap.sh update-self   # actualiza este script
sudo ./bootstrap.sh update        # baja el código nuevo y reconstruye
```

`update` vuelve a bajar el backend (y el TTS si está instalado), copia el código
sobre la instalación sin tocar `config/.env`, `tts/.env`, `data/`, `logs/` ni
`config/install.conf`, y reejecuta `install.sh --non-interactive`: reconstruye
las imágenes y reinicia solo los servicios ya elegidos. `update-self` no toca la
instalación, solo el script.

Sin flags y con terminal interactiva pregunta qué instalar; las respuestas
quedan en `config/install.conf` (0600) y los reejecutados las reusan. Para
cambiarlas: `sudo ./install.sh --configure`. Todo lo demás es equivalente pero
no interactivo:

```bash
sudo ./install.sh --services backend,web --tts-src /ruta/a/radio-alerts-tts
```

| Flag | Efecto |
| --- | --- |
| `--services LISTA` | `all` (defecto), `backend`, `web`, `tts` o combinación con comas; `web` arrastra el backend |
| `--port N` | puerto público de la web (`8081` en HTTP, `8443` en HTTPS) |
| `--domain DOM` | subdominio de la web; obligatorio con HTTPS |
| `--tls none\|letsencrypt\|gts` | HTTP, o HTTPS con la CA elegida |
| `--acme-email EMAIL` | contacto de la cuenta ACME |
| `--cf-token-file RUTA` | archivo con `CF_Token=...` de Cloudflare |
| `--gts-eab-kid`, `--gts-eab-hmac-file` | credenciales EAB de Google Trust Services |
| `--staging` | entorno de pruebas de Let's Encrypt |
| `--no-staging` | vuelve a la CA de producción (obligatorio tras una prueba staging) |
| `--non-interactive` | no pregunta: usa flags + `config/install.conf` |
| `--tts-src RUTA`, `--no-tts` | copiar la cadena TTS / no instalarla |
| `--tts-port N` | puerto de la API del TTS (8090 si está libre; si no, el instalador busca otro) |
| `--open-firewall` | abre en ufw solo el puerto web elegido |

HTTPS se valida por **DNS-01 contra Cloudflare**, así que no necesita publicar
los puertos 80/443 (que usa AzuraCast ni se tocan):

```bash
sudo ./install.sh --services all --domain alertas.ejemplo.com --port 8443 \
  --tls letsencrypt --acme-email ops@ejemplo.com \
  --cf-token-file /root/cf-token.env --tts-src /ruta/a/radio-alerts-tts
```

El subdominio tiene que apuntar al VPS (registro `A`, modo *DNS only*). El
certificado queda en `config/certs/` y se renueva solo a las 03:17 vía
`/etc/cron.d/radio-alertas` (log en `logs/acme.log`). Para Google Trust
Services: `--tls gts --gts-eab-kid KID --gts-eab-hmac-file RUTA`, con Public CA
habilitada en el proyecto de Google Cloud.

El procedimiento completo, con diagnóstico y renovación, está en
[docs/OPERACION.md](docs/OPERACION.md).

Equivalente manual, si se prefiere paso a paso:

```bash
docker network create radio-alerts      # una sola vez, si no existe
mkdir -p data logs config/nginx/snippets config/certs
cp deploy/nginx/snippets/*.inc config/nginx/snippets/
cp deploy/nginx/radio-alerts.conf config/nginx/site.conf   # variante HTTP
printf 'WEB_PORT=8081\nWEB_INTERNAL_PORT=80\n' > config/install.conf
sudo chown -R 10001:10001 data logs     # el contenedor corre sin root
cp config/.env.example config/.env      # completar secretos y chmod 600
docker compose --env-file config/install.conf -f deploy/docker-compose.yml up -d --build
curl -s http://127.0.0.1:3000/health
```

Para HTTPS el paso manual no alcanza (hay que emitir el certificado y renderizar
`deploy/nginx/radio-alerts-tls.conf` con el dominio): usá el instalador.

El contenedor TTS debe estar en la misma red `radio-alerts` y usar el mismo
`TTS_API_TOKEN` / `API_TOKEN`.

## Seguridad

- `DRY_RUN=true` por defecto: sin audio hasta que se autorice explícitamente.
- El backend nunca loguea tokens, contraseñas de streamer ni el webhook.
- `INTERNAL_TOKEN` y `TTS_API_TOKEN` obligatorios antes de salir de `DRY_RUN`.
- Las comparaciones de tokens usan `hmac.compare_digest`.
- Una sola conexión WebSocket a P2P y backoff exponencial.
- JMA se consulta por evento o como respaldo cada 5–10 minutos.

## Estado

`DRY_RUN=true` en ambos servicios hasta completar la prueba de tono autorizada.
