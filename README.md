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
                           │          │
                     web estática     └──► Kokoro → WebDJ → AzuraCast
```

- Red Docker compartida `radio-alerts`: `backend` ↔ `alerts-tts`.
- Red `default` de este compose: `web` ↔ `backend`.
- Puertos publicados: `8081` (web) y `127.0.0.1:3000` (backend, solo depuración).
  `POST /internal/*` nunca se publica.

## Estructura

```
install.sh                instalador para el VPS (estilo AzuraCast)
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

El proyecto vive en `/var/alertas`, igual que AzuraCast en `/var/AzuraCast`:

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

Opciones: `--no-tts` (solo backend + web) y `--open-firewall` (abre 8081/tcp en
ufw). El procedimiento completo está en [docs/OPERACION.md](docs/OPERACION.md).

Equivalente manual, si se prefiere paso a paso:

```bash
docker network create radio-alerts      # una sola vez, si no existe
mkdir -p data logs
sudo chown -R 10001:10001 data logs     # el contenedor corre sin root
cp config/.env.example config/.env      # completar secretos y chmod 600
docker compose -f deploy/docker-compose.yml up -d --build
curl -s http://127.0.0.1:3000/health
```

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
