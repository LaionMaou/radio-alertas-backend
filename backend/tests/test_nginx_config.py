from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from radio_alerts_backend.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
NGINX = ROOT / "deploy" / "nginx" / "radio-alerts.conf"
ENV_EXAMPLE = ROOT / "config" / ".env.example"

_LOCATION_RE = re.compile(r"location\s+([^{]+?)\s*\{")


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _nginx() -> str:
    return NGINX.read_text(encoding="utf-8")


def _location_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in _LOCATION_RE.finditer(text):
        key = " ".join(match.group(1).split())
        if key.startswith(("= ", "^~ ")):
            key = key.split(" ", 1)[1]
        depth = 1
        index = match.end()
        while depth and index < len(text):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        blocks[key] = text[match.end() : index - 1]
    return blocks


def test_compose_declares_external_alert_network() -> None:
    networks = _compose()["networks"]

    assert networks["radio-alerts"]["external"] is True
    assert networks["radio-alerts"]["name"] == "radio-alerts"


def test_backend_joins_shared_network() -> None:
    backend = _compose()["services"]["backend"]

    assert "radio-alerts" in backend["networks"]
    assert "default" in backend["networks"]


def test_backend_port_is_loopback_only() -> None:
    backend = _compose()["services"]["backend"]

    assert backend["ports"] == ["127.0.0.1:3000:3000"]


def test_backend_drops_privileges_and_has_init() -> None:
    backend = _compose()["services"]["backend"]

    assert backend["init"] is True
    assert "no-new-privileges:true" in backend["security_opt"]
    assert backend["restart"] == "unless-stopped"


def test_web_mounts_nginx_config_read_only() -> None:
    web = _compose()["services"]["web"]
    mounts = {mount.split(":")[0] for mount in web["volumes"]}

    assert "./nginx/radio-alerts.conf" in mounts
    assert all(mount.endswith(":ro") for mount in web["volumes"] if "html" in mount)


def test_nginx_blocks_internal_endpoints() -> None:
    blocks = _location_blocks(_nginx())

    for name in ("/internal", "/internal/"):
        assert name in blocks, f"falta el bloque {name}"
        assert "return 404" in blocks[name]
        assert "proxy_pass" not in blocks[name]


def test_nginx_stream_disables_buffering() -> None:
    block = _location_blocks(_nginx())["/api/alert/stream"]

    assert "proxy_buffering off" in block
    assert "proxy_cache off" in block
    assert "gzip off" in block
    assert "proxy_read_timeout 3600s" in block


def test_nginx_api_is_read_only() -> None:
    text = _nginx()
    api = _location_blocks(text)["/api/"]

    assert "limit_except GET" in api
    assert "deny all" in api


def test_nginx_csp_forbids_inline_code() -> None:
    text = _nginx()

    assert "Content-Security-Policy" in text
    assert "script-src 'self'" in text
    assert "unsafe-inline" not in text
    assert "unsafe-eval" not in text


def test_env_example_covers_every_setting() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    keys = {
        line.split("=", 1)[0]
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }

    missing = {name.upper() for name in Settings.model_fields} - keys
    assert missing == set()


def test_env_example_ships_dry_run() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "DRY_RUN=true" in text


def test_install_script_is_executable() -> None:
    path = ROOT / "install.sh"

    assert path.is_file()
    if os.name == "nt":  # NTFS no guarda el bit de ejecución
        return
    assert path.stat().st_mode & 0o111, "install.sh tiene que ser ejecutable"


def test_gitignore_covers_generated_files() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    for entry in ("config/.env", "tts/", "data/", "logs/"):
        assert entry in text
