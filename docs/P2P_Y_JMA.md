# Contratos de P2PQuake y JMA

Resumen de los campos y límites que usa el backend. Cualquier cambio aquí debe
reflejarse en `backend/src/radio_alerts_backend/` y en sus tests.

## P2PQuake (JSON API v2)

- Producción: `wss://api.p2pquake.net/v2/ws`
- Sandbox: `wss://api-realtime-sandbox.p2pquake.net/v2/ws`
- Mensajes de texto JSON. **No hay replay**: tras una caída se pierden los eventos
  intermedios.
- Los duplicados son posibles; se deduplican por `id` con una LRU de 2000.
- Límite: 2 conexiones WebSocket por IP. Este backend usa **una sola**.

Códigos usados:

| Código | Evento | Acción |
| --- | --- | --- |
| `551` | 地震情報 | Normaliza, puede enriquecerse con JMA y correlacionarse con un 556 |
| `552` | 津波予報 | Normaliza y anuncia según grado |
| `556` | 緊急地震速報 (警報) | Normaliza y anuncia; `test=true` se ignora por completo |

### 551 — 地震情報

```
id, time,
issue{source,time,type,correct},
earthquake{
  time,
  hypocenter{name,latitude,longitude,depth,magnitude},
  maxScale, domesticTsunami, foreignTsunami
},
points[]{pref,addr,isArea,scale}
```

- `latitude`/`longitude` usan `-200` como “sin dato”.
- `maxScale = -1` significa sin dato; se usa el máximo de `points[].scale`.
- `domesticTsunami ∈ None | Unknown | Checking | NonEffective | Watch | Warning`.
  Solo `None` y `NonEffective` permiten decir «津波の心配はありません».

### 552 — 津波予報

```
cancelled, issue{time}, areas[]{grade,immediate,name,firstHeight,maxHeight}
```

- `grade ∈ MajorWarning | Warning | Watch | Unknown`.
- Si `cancelled=true`, `areas` viene vacío: se guarda como `cancelled` y **no** se anuncia.

### 556 — 緊急地震速報（警報）

```
test, cancelled,
earthquake{originTime,arrivalTime,condition,hypocenter{...}},
issue{time,eventId,serial},
areas[]{pref,name,scaleFrom,scaleTo,kindCode,arrivalTime}
```

- `test=true` se descarta antes de tocar el store.
- `scaleTo` admite `99` = «～程度以上». **Nunca se muestra como intensidad 99**.

### Escalas

| Valor P2P | Etiqueta | Código de orden |
| --- | --- | --- |
| `-1` | 不明 | 0 |
| `10` | 1 | 10 |
| `20` | 2 | 20 |
| `30` | 3 | 30 |
| `40` | 4 | 40 |
| `45` | 5弱 | 45 |
| `46` | 5弱以上 | 45 |
| `50` | 5強 | 50 |
| `55` | 6弱 | 55 |
| `60` | 6強 | 60 |
| `70` | 7 | 70 |
| `99` | 5弱以上 | 45 |

`46` y `99` no son intensidades exactas. Para ordenar y decidir escaladas se
comparan como el mínimo que garantizan (5弱), de modo que una observación real de
5強 o más sí puede escalar el mismo evento. Es una desviación deliberada del
borrador inicial, que los ordenaba como `46→45` y `99→75`: tratar `99` como «casi
6弱» habría bloqueado escaladas legítimas a 5強/6弱. Nunca se renderiza `99`.

### Grados de tsunami (orden)

`Unknown = None = NonEffective = 0`, `Watch = 1`, `Warning = 2`,
`MajorWarning = 3`.

## JMA

| Recurso | URL | Uso |
| --- | --- | --- |
| Lista de sismos | `https://www.jma.go.jp/bosai/quake/data/list.json` | Enriquecer un 551 |
| Lista de tsunamis | `https://www.jma.go.jp/bosai/tsunami/data/list.json` | Enriquecer un 552 |
| Feed Atom | `https://www.data.jma.go.jp/developer/xml/feed/extra.xml` | Tifones |
| XML de detalle | `https://www.data.jma.go.jp/developer/xml/data/<archivo>.xml` | VPTW61 / VPTA51 |

Campos de las listas: `ctt, rdt, eid, ttl, ift, ser, at, anm, acd, cod, mag,
maxi, int, json` (sismos) y `ctt, eid, rdt, ttl, ift, ser, at, anm, acd, cod, mag,
kind, json` (tsunamis).

- `maxi` usa `1..4, 5-, 5+, 6-, 6+, 7`; se traduce a los códigos de arriba.
- El feed `extra.xml` es Atom mixto: se filtra por título que contenga `台風` y
  enlace que contenga `VPTW61` o `VPTA51`.
- VPTW61: `Head/EventID` (`TC…`), `ReportDateTime`, `Serial`; en
  `Body/MeteorologicalInfos/MeteorologicalInfo` con `DateTime type=実況`:
  - `Type=呼称` → `TyphoonNamePart{Name,NameKana,Number}`
  - `Type=階級` → `ClassPart/TyphoonClass`
  - `Type=中心` → `CenterPart{Coordinate,Location,Direction,Speed,Pressure}`
  - `Type=風` → `WindPart{WindSpeed…}`
- VPTA51: `FiftyKtWindProbabilityPart/FiftyKtWindProbability refID=1..8 unit=%`
  con `Area{Name,Code,Prefecture,PrefectureCode}` y `TimeDefines` cada 3 h.

### Presupuesto de peticiones

- Mínimo **300 s entre peticiones a la misma URL** (`JMA_MIN_INTERVAL_SECONDS`).
- Caché: listas 300 s, feed 600 s, XML de detalle 600 s.
- Si la red falla se sirve la respuesta vieja; un fallo de JMA nunca bloquea un
  evento P2P.
- El feed se consulta como respaldo cada `TYPHOON_POLL_SECONDS` (600 s por
  defecto) y baja como máximo el último VPTW61 y el último VPTA51 por ciclo.

### Correlación

- **551 → lista de sismos**: ±120 s respecto de `earthquake.time`, con nombre
  idéntico o a menos de 50 km. Si hay ambigüedad sin nombre ni coordenadas, no se
  enriquece.
- **552 → lista de tsunamis**: ±300 s contra `rdt`/hora de origen.

## Claves de evento

| Tipo | Clave |
| --- | --- |
| EEW | `eew:{issue.eventId}` o hash de origen+hipocentro |
| Sismo | `quake:{eid}` si se enriqueció; si no `quake:{p2p_id}` |
| Tsunami | `tsunami:{eid}` si se enriqueció; si no hash de la hora de emisión |
| Tifón | `typhoon:{EventID}` (sin `Serial`, para que las actualizaciones reemplacen) |

`dedupe_key = "{kind}:{sha1(event_key)[:12]}:{rank}"`, recortada a 120 caracteres.

## Correlación 551 con 556 (interna, no de P2P)

Un `551` que llega a ≤15 min y ≤100 km de un EEW **ya anunciado** se adjunta a ese
evento como enriquecimiento (`enrichment.quake`) y solo se vuelve a anunciar si el
`rank` observado supera al del EEW. Un EEW nunca se suprime por esta heurística: si
no hay coincidencia confiable, el 551 se trata como evento independiente.
