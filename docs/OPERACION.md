# Operación

Runbook del backend de alertas. Todo el procedimiento se ejecuta **desde el
directorio del repositorio** y no toca AzuraCast, su base de datos, su nginx ni el
daemon de Docker.

## 0. Antes de empezar

- Acceso root al VPS. Si AzuraCast ya corre ahí, Docker y Compose también están.
- `docker network ls` no muestra `radio-alerts` en la primera instalación.
- Los puertos `8081` y `127.0.0.1:3000` están libres.
- `DRY_RUN=true` en `config/.env`. No se cambia hasta la prueba audible autorizada
  (sección 6).

## 1. Instalación

```bash
sudo mkdir -p /var/alertas
sudo rsync -a --exclude .git --exclude .venv ./ /var/alertas/
cd /var/alertas
sudo ./install.sh --tts-src /ruta/a/radio-alerts-tts
```

`install.sh` es idempotente y hace, en orden:

1. Comprueba root, espacio en disco y que no se esté instalando dentro de AzuraCast.
2. Instala Docker y el plugin Compose **solo si faltan**; si el daemon ya corre, no
   lo toca.
3. Crea la red externa `radio-alerts`.
4. Copia la cadena TTS desde `--tts-src` a `./tts` (omite `.git`, `.venv`, `data`).
5. Crea `data/`, `logs/`, `config/.env` y `tts/.env` si no existen, genera
   `INTERNAL_TOKEN` y el `API_TOKEN` compartido (el mismo en `tts/.env` y en
   `TTS_API_TOKEN` del backend), y fija `TTS_API_URL` con el `API_PORT` del TTS.
6. Construye y levanta backend + web, después la cadena TTS, y espera el `healthy`
   de cada contenedor.
7. Verifica `/health`, que nginx devuelva 404 en `/internal/*` y avisa si ufw
   bloquea 8081.

Nunca imprime tokens. Si algo falla, termina con código 1 y los últimos logs del
contenedor problemático.

Si al copiar el proyecto desde Windows el script llega sin permiso de ejecución
(`Permission denied`), alcanza con `sudo chmod +x install.sh` o `sudo bash
install.sh`.

Sin `--tts-src` usa un `./tts` ya existente; con `--no-tts` instala solo
backend + web. `--open-firewall` abre 8081/tcp en ufw.

Después de instalar, revisar:

- `config/.env`: `TYPHOON_AREAS` (vacío si no se quiere audio de tifón) y
  `DISCORD_WEBHOOK_URL` (vacío para desactivar). `INTERNAL_TOKEN` y
  `TTS_API_TOKEN` ya los generó y sincronizó el script.
- `tts/.env`: para el paso a audio real hacen falta `AZURACAST_BASE_URL`,
  `AZURACAST_STATION_SHORT_NAME`, `AZURACAST_STREAMER_USER`,
  `AZURACAST_STREAMER_PASSWORD`, `AZURACAST_DJ_HOST` y `AZURACAST_DJ_PORT`. Con
  `DRY_RUN=true` arranca igual sin credenciales.

El `docker-compose.yml` de la cadena TTS ya declara la red compartida. Si usás
otra copia, comprobá que tenga:

```yaml
services:
  alerts-tts:
    networks: [default, radio-alerts]
networks:
  radio-alerts:
    external: true
    name: radio-alerts
```

## 2. Rearranque y actualización

El script ya deja todo levantado. Para reconstruir tras cambiar código:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f tts/docker-compose.yml up -d --build   # solo si hay TTS
docker compose -f deploy/docker-compose.yml ps
```

El `Dockerfile` corre `HEALTHCHECK` cada 30 s; `ps` muestra `healthy` cuando
`/health` responde `status=ok`. No usar `down -v`: borra los volúmenes de datos.

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
