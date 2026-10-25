"""Comprobación de salud para Docker Compose.

Falla cuando el backend no responde o cuando se declara `degraded`
(P2P desconectado o JMA con errores). No hace llamadas externas.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_TIMEOUT = 5.0


def main() -> int:
    port = os.environ.get("API_PORT") or "3000"
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            status = response.status
            body = response.read(64_000)
    except (urllib.error.URLError, OSError) as exc:
        print(f"healthcheck: sin respuesta de {url}: {exc}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"healthcheck: HTTP {status}", file=sys.stderr)
        return 1

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("healthcheck: respuesta no es JSON", file=sys.stderr)
        return 1

    state = str(data.get("status") or "")
    if state != "ok":
        detail = {"p2p": data.get("p2p"), "jma": data.get("jma")}
        print(f"healthcheck: estado {state or 'desconocido'} {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
