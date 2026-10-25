# Operación

Runbook del backend de alertas. Todo el procedimiento se ejecuta **desde el
directorio del repositorio** y no toca AzuraCast, su base de datos, su nginx ni el
daemon de Docker.

## 0. Antes de empezar

- El contenedor `alerts-tts` ya existe y está sano (`curl -s
  http://127.0.0.1:8090/health`).
- `docker network ls` no muestra `radio-alerts` todavía en la primera instalación.
- Los puertos `8081` y `127.0.0.1:3000` están libres.
- `DRY_RUN=true` en `config/.env`. No se cambia hasta la prueba audible autorizada
  (sección 6).

## 1. Instalación

```bash
docker network create radio-alerts      # solo si no existe
mkdir -p data logs
sudo chown -R 10001:10001 data logs     # el contenedor corre con UID 10001
cp config/.env.example config/.env
chmod 600 config/.env
```

Completar en `config/.env`:

| Variable | Valor |
| --- | --- |
| `TTS_API_TOKEN` | el mismo `API_TOKEN` del proyecto `radio-alerts-tts` |
| `INTERNAL_TOKEN` | `openssl rand -hex 32` |
| `TYPHOON_AREAS` | vacío si no se quiere audio de tifón |
| `DISCORD_WEBHOOK_URL` | vacío para desactivar |

La cadena TTS debe declarar la red compartida. En `radio-alerts-tts`:

```yaml
services:
  alerts-tts:
    networks: [default, radio-alerts]
networks:
  radio-alerts:
    external: true
    name: radio-alerts
```

y recrear solo ese servicio (`docker compose up -d alerts-tts`). No hace falta
reiniciar nada más.

## 2. Arranque

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

El `Dockerfile` corre `HEALTHCHECK` cada 30 s; `ps` muestra `healthy` cuando
`/health` responde `status=ok`.

## 3. Verificación

```bash
docker compose -f deploy/docker-compose.yml exec web nginx -t
curl -s http://127.0.0.1:3000/health
curl -s http://127.0.0.1:8081/api/alert/latest
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/internal/trigger-tts
```

Esperado:

- `health`: `{"status":"ok","p2p":{"connected":true,...},"jma":{...},"tts":{...}}`.
  `degraded` al arrancar mientras P2P conecta es normal durante unos segundos.
- `latest`: `{"alert":null}` si no hubo eventos todavía.
- `/internal/*`: **404**. Si devuelve otra cosa, nginx no está aplicando la config.

Abrir `http://IP-DEL-VPS:8081/`: la cabecera debe mostrar `ライブ` (SSE) y
`P2P 接続中`. Si el navegador no llega, revisar el firewall del VPS: solo se publica
8081; 3000 y 8090 quedan en loopback.

Logs:

```bash
docker compose -f deploy/docker-compose.yml logs -f --tail=100 backend
docker compose -f deploy/docker-compose.yml logs -f --tail=50 web
tail -f logs/backend.log     # rotación 10 MB × 3
```

## 4. Simulacro (sin audio)

Con `ENABLE_SIMULATE=true` en `config/.env` y el contenedor recreado:

```bash
TOKEN=$(grep '^INTERNAL_TOKEN=' config/.env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:3000/internal/simulate \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"fixture":"eew"}'
```

Fixtures: `eew`, `quake`, `tsunami`, `typhoon`. Alternativa: `{"payload":{...}}`
con un mensaje P2P crudo.

Comprobar:

1. La tarjeta aparece por SSE sin recargar y el historial suma una entrada.
2. `docker compose ... logs backend | grep tts.dry_run` muestra el texto japonés
   exacto que se habría locutado.
3. El badge TTS dice `ドライラン`; el audio **no** suena.
4. Colores por tipo: EEW rojo, sismo naranja, tsunami según grado, tifón azul.

`POST /internal/trigger-tts` sirve para reanunciar un evento existente; también
respeta `DRY_RUN`.

Al terminar, volver `ENABLE_SIMULATE=false` y recrear el backend.

## 5. Reconexión y backoff

```bash
# en config/.env: P2P_WS_URL=ws://127.0.0.1:9
docker compose -f deploy/docker-compose.yml up -d backend
docker compose -f deploy/docker-compose.yml logs -f backend | grep p2p.reconnect
```

Los reintentos deben espaciarse 1, 2, 4, 8, 16, 30, 30… segundos con jitter, sin
abrir más de una conexión a la vez. Tras 10 fallos llega un único aviso a Discord
(si está configurado) y los reintentos continúan con el tope.

Restaurar `P2P_WS_URL=wss://api.p2pquake.net/v2/ws` y recrear de nuevo.

## 6. Paso a audio real (solo con autorización explícita)

No ejecutar sin el visto bueno del responsable de la radio.

1. En `radio-alerts-tts`: `DRY_RUN=false`. Probar primero el tono con
   `check_streamer.py` y confirmar que se escucha en el stream y que el AutoDJ
   retoma solo.
2. En este backend: `DRY_RUN=false` y recrear el backend.
3. Simular `eew`: debe escucharse el anuncio, el AutoDJ retomar y el badge pasar a
   `放送済み`.
4. Revisar `logs/backend.log` para `tts.submit` / `tts.result` sin secretos.

Si algo falla: volver `DRY_RUN=true` en ambos servicios antes de seguir
investigando.

## 7. Diagnóstico

| Síntoma | Causa probable | Acción |
| --- | --- | --- |
| `health` en `degraded` con `p2p.connected=false` | red/DNS del VPS o P2P caído | revisar `p2p.last_error`; el backoff sigue solo |
| La página no actualiza pero carga | SSE bloqueado por un proxy intermedio | comprobar `X-Accel-Buffering`, `proxy_buffering off`; la web pasa a polling a los 3 errores |
| `tts` en `failed` con 401 | `TTS_API_TOKEN` distinto al `API_TOKEN` del TTS | igualar los valores y recrear el backend |
| `tts` en `unknown` | el TTS se reinició y perdió el resultado | no se reanuncia solo; usar `/internal/trigger-tts` si hace falta |
| `tts` en `failed` con 5xx | TTS caído o Kokoro sin modelos | `docker compose logs` del TTS |
| `Permission denied` en `/data` o `/logs` | dueño incorrecto del bind mount | `sudo chown -R 10001:10001 data logs` |
| Alertas duplicadas | dos workers o dos instancias | `WEB_CONCURRENCY` debe estar ausente o en `1`; `docker ps` no debe mostrar dos backends |
| `/internal` responde 200 por 8081 | config de nginx no montada | `docker compose ... exec web nginx -T \| grep internal` |

Comandos útiles:

```bash
curl -s http://127.0.0.1:3000/health | python -m json.tool
wc -l data/events-*.jsonl
docker compose -f deploy/docker-compose.yml exec backend python scripts/healthcheck.py
```

## 8. Rotación y mantenimiento

- `data/events-*.jsonl`: un archivo por día, retención `HISTORY_DAYS` (30).
- `logs/backend.log`: 10 MB × 3.
- Logs de Docker: 10 MB × 3 por contenedor.
- Al reiniciar, el backend reproduce los últimos 500 registros para no reanunciar
  el mismo evento.
- Actualizar: `git pull && docker compose -f deploy/docker-compose.yml up -d
  --build` (sin `down -v`, sin borrar volúmenes).

## 9. Límites que no se tocan

- **Una sola** conexión WebSocket a P2P. No escalar horizontalmente sin rediseñar.
- JMA como mínimo 300 s entre peticiones a la misma URL.
- `POST /internal/*` nunca se publica en nginx.
- No reiniciar el daemon de Docker ni el contenedor de AzuraCast como parte de un
  despliegue de alertas.
