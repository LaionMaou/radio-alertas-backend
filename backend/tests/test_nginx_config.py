from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from radio_alerts_backend.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
NGINX_DIR = ROOT / "deploy" / "nginx"
NGINX = NGINX_DIR / "radio-alerts.conf"
NGINX_TLS = NGINX_DIR / "radio-alerts-tls.conf"
SNIPPETS = NGINX_DIR / "snippets"
SECURITY_HEADERS = SNIPPETS / "security-headers.inc"
ENV_EXAMPLE = ROOT / "config" / ".env.example"

_LOCATION_RE = re.compile(r"location\s+([^{]+?)\s*\{")


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _nginx() -> str:
    return NGINX.read_text(encoding="utf-8")


def _nginx_tls() -> str:
    return NGINX_TLS.read_text(encoding="utf-8")


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


def test_web_port_is_configurable() -> None:
    web = _compose()["services"]["web"]

    assert web["ports"] == ["${WEB_PORT:-8081}:${WEB_INTERNAL_PORT:-80}"]


def test_web_mounts_rendered_directories_read_only() -> None:
    web = _compose()["services"]["web"]
    mounts = {mount.split(":")[0]: mount for mount in web["volumes"]}

    assert "../config/nginx" in mounts, "el site.conf lo renderiza install.sh"
    assert "../config/certs" in mounts, "los certificados no se montan desde el home de acme.sh"
    assert mounts["../config/nginx"].endswith(":ro")
    assert mounts["../config/certs"].endswith(":ro")
    assert mounts["../web"].endswith(":ro")


def _variants() -> list[str]:
    return [_nginx(), _nginx_tls()]


def test_nginx_blocks_internal_endpoints() -> None:
    for text in _variants():
        blocks = _location_blocks(text)

        for name in ("/internal", "/internal/"):
            assert name in blocks, f"falta el bloque {name}"
            assert "return 404" in blocks[name]
            assert "proxy_pass" not in blocks[name]


def test_nginx_stream_disables_buffering() -> None:
    for text in _variants():
        block = _location_blocks(text)["/api/alert/stream"]

        assert "proxy_buffering off" in block
        assert "proxy_cache off" in block
        assert "gzip off" in block
        assert "proxy_read_timeout 3600s" in block


def test_nginx_api_is_read_only() -> None:
    for text in _variants():
        api = _location_blocks(text)["/api/"]

        assert "limit_except GET" in api
        assert "deny all" in api


def test_nginx_csp_forbids_inline_code() -> None:
    text = SECURITY_HEADERS.read_text(encoding="utf-8")

    assert "Content-Security-Policy" in text
    assert "script-src 'self'" in text
    assert "unsafe-inline" not in text
    assert "unsafe-eval" not in text
    for variant in _variants():
        assert "include /etc/nginx/conf.d/snippets/security-headers.inc;" in variant


def test_tls_variant_terminates_tls_for_the_chosen_domain() -> None:
    text = _nginx_tls()

    assert "listen 443 ssl;" in text
    assert "server_name __DOMAIN__;" in text
    assert "ssl_certificate /etc/certs/fullchain.pem;" in text
    assert "ssl_certificate_key /etc/certs/key.pem;" in text
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in text
    assert "http2 on;" in text


def test_tls_variant_avoids_hsts() -> None:
    for text in _variants():
        assert "Strict-Transport-Security" not in text, "HSTS rompería el 443 de AzuraCast"


def test_http_variant_listens_on_plain_http() -> None:
    text = _nginx()

    assert "listen 80;" in text
    assert "ssl_certificate" not in text


def test_snippets_exist_and_are_shared() -> None:
    assert (SNIPPETS / "proxy-headers.inc").is_file()
    for variant in _variants():
        assert "include /etc/nginx/conf.d/snippets/proxy-headers.inc;" in variant


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

    for entry in (
        "config/.env",
        "tts/",
        "data/",
        "logs/",
        "config/install.conf",
        "config/nginx/",
        "config/certs/",
        "config/acme/",
    ):
        assert entry in text


def test_installer_exposes_the_new_options() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    for flag in (
        "--services",
        "--port",
        "--domain",
        "--tls",
        "--acme-email",
        "--cf-token-file",
        "--gts-eab-kid",
        "--staging",
        "--non-interactive",
    ):
        assert flag in text, f"falta {flag} en install.sh"


def test_installer_never_publishes_azuracast_ports_silently() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "--port" in text
    assert "8443" in text, "HTTPS tiene que usar un puerto propio por defecto"


def test_acme_helpers_are_present() -> None:
    for name in ("acme-reload.sh", "acme-renew.sh"):
        path = ROOT / "deploy" / name
        assert path.is_file(), f"falta deploy/{name}"
        if os.name != "nt":
            assert path.stat().st_mode & 0o111, f"deploy/{name} tiene que ser ejecutable"
