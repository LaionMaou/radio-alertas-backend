# Operación

Runbook del backend de alertas. Todo el procedimiento se ejecuta **desde el
directorio del repositorio** y no toca AzuraCast, su base de datos, su nginx ni el
daemon de Docker.

## 0. Antes de empezar

- Acceso root al VPS. Si AzuraCast ya corre ahí, Docker y Compose también están.
- `docker network ls` no muestra `radio-alerts` en la primera instalación.
- `127.0.0.1:3000` libre y **el puerto elegido para la web** libre
  (`8081` por defecto en HTTP, `8443` en HTTPS). Los puertos 80 y 443 son de
  AzuraCast: este stack no los publica nunca.
- La API del TTS usa el 8090 en loopback. **AzuraCast también publica el 8090**
  (parte de su rango fijo de puertos de DJ), así que en un VPS con AzuraCast el
  instalador mueve la API a otro puerto y lo guarda en `tts/.env` (`API_PORT`);
  el backend apunta ahí por la red de Docker. Si querés fijarlo,
  `--tts-port 18090`. `docker ps` no debe mostrar otro proceso en el puerto
  elegido.
- `DRY_RUN=true` en `config/.env`. No se cambia hasta la prueba audible autorizada
  (sección 6).

## 1. Instalación

Igual que AzuraCast, el instalador se baja dentro de la carpeta elegida:

```bash
sudo mkdir -p /var/alertas
cd /var/alertas
curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh -o bootstrap.sh
chmod a+x bootstrap.sh
sudo ./bootstrap.sh install
```

`bootstrap.sh` baja el repo en el directorio actual (o en `--dir RUTA`, una ruta
absoluta) y ejecuta `install.sh`, que con TTY arranca el asistente. Al pipear no
hay TTY, así que ahí las opciones son obligatorias:

```bash
curl -fsSL https://raw.githubusercontent.com/LaionMaou/radio-alertas-backend/main/bootstrap.sh \
  | sudo bash -s -- --dir /srv/alertas --services all --domain alertas.ejemplo.com \
    --port 8443 --tls letsencrypt --acme-email ops@ejemplo.com --cf-token-file /root/cf-token.env
```

Si los servicios incluyen la cadena TTS (`all` por defecto), el bootstrap baja
también `LaionMaou/radio-alertas-tts` y la pasa como `--tts-src`; con `--tts-src
RUTA` usa esa copia local y con `--no-tts` no instala la cadena.

Con el proyecto ya subido al VPS:

```bash
sudo mkdir -p /var/alertas
sudo rsync -a --exclude .git --exclude .venv ./ /var/alertas/
cd /var/alertas
sudo ./install.sh
```

Con terminal interactiva el script pregunta qué instalar (servicios, HTTP o
HTTPS, CA, credenciales, puerto y dominio) y guarda las respuestas en
`config/install.conf` (0600). Los reejecutados reusan ese archivo sin preguntar;
para volver al asistente, `sudo ./install.sh --configure`. Sin TTY hay que pasar
las opciones o tener ya un `install.conf`.

`install.sh` es idempotente y hace, en orden:

1. Comprueba root, espacio en disco, RAM y que no se esté instalando dentro de AzuraCast.
2. Instala Docker y el plugin Compose **solo si faltan**; si el daemon ya corre, no
   lo toca.
3. Crea la red externa `radio-alerts`.
4. Copia la cadena TTS desde `--tts-src` a `./tts` (omite `.git`, `.venv`, `data`),
   solo si la cadena está seleccionada.
5. Crea `data/`, `logs/`, `config/.env` y `tts/.env` si no existen, genera
   `INTERNAL_TOKEN` y el `API_TOKEN` compartido (el mismo en `tts/.env` y en
   `TTS_API_TOKEN` del backend), y fija `TTS_API_URL` con el `API_PORT` del TTS.
   Estos pasos solo corren para los servicios que se instalan.
6. Si hay web, renderiza `config/nginx/site.conf` desde `deploy/nginx`, lo valida
   con `nginx -t` en un contenedor efímero y, con HTTPS, emite el certificado
   antes de levantar nada (sección 1.1). El token interno queda dentro de
   `site.conf` (0600) para el botón de stop, y se crea
   `config/nginx/operator.htpasswd` vacío: nadie puede cortar la alerta hasta
   que se dé de alta un operador (sección 3.1).
7. Construye y levanta **solo los servicios elegidos** y espera el `healthy` de
   cada contenedor. Los que se hayan deseleccionado quedan detenidos.
8. Verifica `/health`, que nginx devuelva 404 en `/internal/*` sobre el esquema y
   puerto elegidos, y avisa si el firewall bloquea el puerto web.

Nunca imprime tokens. Si algo falla, termina con código 1 y los últimos logs del
contenedor problemático.

Si al copiar el proyecto desde Windows el script llega sin permiso de ejecución
(`Permission denied`), alcanza con `sudo chmod +x install.sh` o `sudo bash
install.sh`.

Sin `--tts-src` usa un `./tts` ya existente; con `--no-tts` (o sin la cadena en
la lista de servicios) instala solo lo demás. `--open-firewall` abre en ufw
**solo** el puerto web elegido.

### 1.1 HTTPS (Let's Encrypt o Google Trust Services)

El certificado se emite por **DNS-01**, que no necesita puertos entrantes: no se
publica el 80 ni se toca el nginx de AzuraCast. Requiere que el subdominio esté
en una zona de Cloudflare y apunte al VPS con un registro `A` en modo **DNS
only** (nube gris).

1. Crear un token de API de Cloudflare con permiso `Zone.DNS:Edit` acotado a esa
   zona y guardarlo en un archivo, por ejemplo `/root/cf-token.env` (0600):

   ```
   CF_Token=xxxxxxxxxxxxxxxxxxxxxxxx
   ```

2. Instalar con HTTPS:

   ```bash
   sudo ./install.sh --services all --domain alertas.ejemplo.com --port 8443 \
     --tls letsencrypt --acme-email ops@ejemplo.com \
     --cf-token-file /root/cf-token.env --tts-src /ruta/a/radio-alerts-tts
   ```

3. El script registra la cuenta ACME, emite el certificado, lo deja en
   `config/certs/` (`fullchain.pem` + `key.pem`, 0600 la clave), programa la
   renovación en `/etc/cron.d/radio-alertas` (03:17) y recién entonces levanta
   nginx. El token queda en `config/acme/cloudflare.env` (0600).

Con Google Trust Services en lugar de Let's Encrypt: `--tls gts --gts-eab-kid
KID --gts-eab-hmac-file RUTA`. El proyecto de Google Cloud necesita **Public CA**
habilitada para poder pedir el KID y la clave HMAC.

Para volver a HTTP: `sudo ./install.sh --tls none --port 8081`. El cron de
renovación se borra solo. `--staging` usa el entorno de pruebas de Let's
Encrypt: sirve para validar la cadena completa sin gastar el límite de
emisiones reales.

`ACME_STAGING` queda guardado en `config/install.conf`, así que después de una
prueba hay que salir explícitamente de staging; el cambio de CA fuerza la
reemisión:

```bash
sudo ./install.sh --tls letsencrypt --no-staging
```

Comprobar con `openssl x509 -in config/certs/fullchain.pem -noout -issuer`: en
staging el emisor es *Fake LE Intermediate*, en producción *Let's Encrypt*.

La renovación corre a diario y deja el log en `logs/acme.log`. Si el DNS cambia
de proveedor o el token se revoca, falla en silencio: revisar ese log y
`config/acme/home/acme.sh --list`.

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

El script ya deja todo levantado. Para traer la última versión publicada está
`sudo ./bootstrap.sh update` (sección 8). Para reconstruir tras cambiar código a
mano:

```bash
docker compose --env-file config/install.conf -f deploy/docker-compose.yml up -d --build
docker compose -f tts/docker-compose.yml up -d --build   # solo si hay TTS
docker compose --env-file config/install.conf -f deploy/docker-compose.yml ps
```

El `--env-file config/install.conf` no es opcional: de ahí salen `WEB_PORT` y
`WEB_INTERNAL_PORT`. Sin él, Compose usa los valores por defecto y recrea la web
en `8081:80` aunque se haya elegido otro puerto o HTTPS.

El `Dockerfile` corre `HEALTHCHECK` cada 30 s; `ps` muestra `healthy` cuando
`/health` responde `status=ok`. No usar `down -v`: borra los volúmenes de datos.

## 3. Verificación

```bash
docker compose --env-file config/install.conf -f deploy/docker-compose.yml exec web nginx -t
curl -s http://127.0.0.1:3000/health

# HTTP (puerto 8081 por defecto)
curl -s http://127.0.0.1:8081/api/alert/latest
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/internal/trigger-tts

# HTTPS (dominio y puerto elegidos)
curl -s --resolve alertas.ejemplo.com:8443:127.0.0.1 https://alertas.ejemplo.com:8443/api/alert/latest
curl -s -o /dev/null -w '%{http_code}\n' \
  --resolve alertas.ejemplo.com:8443:127.0.0.1 https://alertas.ejemplo.com:8443/internal/trigger-tts
openssl x509 -in config/certs/fullchain.pem -noout -issuer -enddate
```

Esperado:

- `health`: `{"status":"ok","p2p":{"connected":true,...},"jma":{...},"tts":{...}}`.
  `degraded` al arrancar mientras P2P conecta es normal durante unos segundos.
- `latest`: `{"alert":null}` si no hubo eventos todavía.
- `/internal/*`: **404**. Si devuelve otra cosa, nginx no está aplicando la config.
- `POST /api/stop` sin credenciales: **401**. Es el único POST público y exige
  la cuenta de un operador (sección 3.1).
- El certificado muestra el emisor de la CA elegida y un `notAfter` a ~90 días.

Abrir `http://IP-DEL-VPS:8081/` (o `https://alertas.ejemplo.com:8443/`): la
cabecera debe mostrar `ライブ` (SSE) y `P2P 接続中`. Si el navegador no llega,
revisar el firewall del VPS: solo se publica el puerto web; 3000 y 8090 quedan en
loopback, y 80/443 no se tocan.

Logs:

```bash
docker compose -f deploy/docker-compose.yml logs -f --tail=100 backend
docker compose -f deploy/docker-compose.yml logs -f --tail=50 web
tail -f logs/backend.log     # rotación 10 MB × 3
```

### 3.1 Operadores y botón de stop

Una 大津波警報 (`MajorWarning`) se repite hasta que alguien la corta. El portal
tiene el botón para hacerlo sin abrir un SSH: el locutor lee la alerta completa,
pulsa **放送を停止**, confirma con **本当に停止しますか？** y el AutoDJ retoma.

- La barra roja aparece **solo** mientras `tts.loop_active` sea `true`; no es un
  pop-up: se suma arriba de la tarjeta y el detalle de la alerta sigue completo.
- El primer clic pide la cuenta del navegador (basic auth). Si se cancela o se
  equivoca, `取消` o `Escape` vuelven atrás sin cortar nada.
- Si la cadena TTS no confirma el corte, el portal responde 502 y la barra
  **sigue visible** con el mensaje de reintento: nunca se oculta una alerta que
  puede seguir sonando.

Las cuentas son por locutor, para poder dar de baja a una persona sin tocar al
resto. La contraseña se escribe a mano (no se imprime ni queda en el historial)
y se guarda como hash apr1 en `config/nginx/operator.htpasswd` (0644).

```bash
sudo ./deploy/operators.sh add nombre       # pide la contraseña dos veces
sudo ./deploy/operators.sh list             # usuarios, nunca los hashes
sudo ./deploy/operators.sh remove nombre    # baja individual
```

Cada cambio valida y recarga el contenedor web. Sin operadores el archivo queda
vacío y el botón responde 401 a todo el mundo; nginx arranca igual. El stop por
SSH del README del TTS sigue disponible como respaldo si el portal no carga.

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
| `/internal` responde 200 por el puerto web | config de nginx no montada | `docker compose ... exec web nginx -T \| grep internal` |
| `el puerto N está ocupado` al instalar | otro servicio lo tomó | elegir otro con `sudo ./install.sh --port 9443` |
| `502 Bad Gateway` en la web | backend caído o reiniciando | `docker ps`; logs del backend |
| La barra de stop no aparece con una 大津波警報 sonando | el evento no tiene `tts.loop_active` | ver `tts.status` y `logs/backend.log`; el stop por SSH sigue disponible |
| `POST /api/stop` da 401 con la contraseña correcta | la cuenta no está en `operator.htpasswd` o perdió el 0644 | `./deploy/operators.sh list` y `ls -l config/nginx/operator.htpasswd` |
| El stop responde 502 | la cadena TTS no confirmó `/stop` | logs del TTS; reintentar desde el portal |
| La barra sigue visible después de `停止しました` | el SSE se cayó y no llegó el flag | recargar la página; el portal ofrece reintentar a los 15 s |
| `nginx: [emerg] cannot load certificate` | `config/certs/` vacío o emisión fallida | revisar `logs/acme.log` y reemitir con `--configure` |
| `--cf-token-file espera la RUTA de un archivo` | se pasó el token como valor, no como ruta | crear `/root/cf-token.env` con `CF_Token=...` (0600) y pasar esa ruta |
| La emisión ACME falla con el dominio | el subdominio no está en la zona del token | token de la zona correcta, o `--tls letsencrypt` |
| El certificado no se renueva | token revocado o DNS movido de proveedor | `tail -50 logs/acme.log`; `config/acme/home/acme.sh --list` |

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
- Actualizar, como AzuraCast: `cd /var/alertas && sudo ./bootstrap.sh
  update-self && sudo ./bootstrap.sh update` (sin `down -v`, sin borrar
  volúmenes). Baja el código nuevo del backend y del TTS, reusa
  `config/install.conf` y reconstruye solo los servicios instalados; no toca
  `config/.env`, `tts/.env`, `data/` ni `logs/`.
- No hay volúmenes nombrados de Docker con estado: `data/`, `logs/` y `tts/data/`
  son bind mounts dentro del directorio de instalación (dueño `10001`). El backup
  es un `tar` de esa carpeta y mover el stack es mover la carpeta completa; si se
  reinstala en otra ruta, los datos no se copian solos.

## 9. Límites que no se tocan

- **Una sola** conexión WebSocket a P2P. No escalar horizontalmente sin rediseñar.
- JMA como mínimo 300 s entre peticiones a la misma URL.
- `POST /internal/*` nunca se publica en nginx. El único POST público es
  `/api/stop`, detrás de la cuenta de un operador.
- Los puertos 80 y 443 son de AzuraCast: no se publican y no se activa HSTS, que
  rompería el 443 de la radio.
- No reiniciar el daemon de Docker ni el contenedor de AzuraCast como parte de un
  despliegue de alertas.
