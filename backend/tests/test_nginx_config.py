from __future__ import annotations

import fnmatch
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


def test_nginx_templates_keep_set_inside_the_server_block() -> None:
    for text in _variants():
        preamble = text.split("\nserver {", 1)[0]

        assert "set " not in preamble, (
            "los archivos de conf.d viven en el contexto http, donde `set` no existe"
        )


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
        "--no-staging",
        "--tts-port",
        "--non-interactive",
    ):
        assert flag in text, f"falta {flag} en install.sh"


def test_installer_explains_that_the_cf_flag_takes_a_path() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "--cf-token-file espera la RUTA" in text, (
        "si se pega el token en lugar de la ruta, el error tiene que decirlo"
    )


def test_installer_can_leave_the_staging_ca() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert '[ "$NO_STAGING" -eq 1 ] && ACME_STAGING=0' in text, (
        "ACME_STAGING queda en install.conf: sin --no-staging no se puede volver a producción"
    )


def test_installer_never_publishes_azuracast_ports_silently() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "--port" in text
    assert "8443" in text, "HTTPS tiene que usar un puerto propio por defecto"


def test_bootstrap_script_fetches_the_repository() -> None:
    path = ROOT / "bootstrap.sh"

    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "codeload.github.com" in text, "bootstrap.sh baja el tarball"
    assert "--dir" in text, "sudo limpia el entorno: el destino va por argumento"
    assert 'DEST="$(pwd)"' in text, "sin --dir instala en el directorio actual, como AzuraCast"
    assert 'install) MODE="install" ;;' in text, "./bootstrap.sh install es el flujo documentado"
    assert "./install.sh" in text, "bootstrap.sh ejecuta el instalador"
    assert "--exclude=./bootstrap.sh" in text, "no se sobrescribe mientras se ejecuta"
    assert "radio-alertas-tts" in text, "bootstrap.sh baja también la cadena TTS"
    assert "--tts-src" in text, "la cadena TTS se le entrega a install.sh con --tts-src"
    if os.name != "nt":
        assert path.stat().st_mode & 0o111, "bootstrap.sh tiene que ser ejecutable"


def test_bootstrap_script_can_update_itself_and_the_install() -> None:
    text = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")

    assert "update-self)" in text, "falta el subcomando update-self"
    assert "update)" in text, "falta el subcomando update"
    assert "RAW_URL" in text, "update-self vuelve a bajar bootstrap.sh"
    assert 'args+=(--non-interactive)' in text, "update no pregunta: reusa install.conf"
    assert "$DEST/config/install.conf" in text, "update respeta los servicios instalados"


def test_installer_pins_the_acme_release_and_names_it_on_failure() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "acme.sh/archive/refs/tags/" in text, "acme.sh se fija por tag, no por master"
    assert "no pude descargar acme.sh: $ACME_URL" in text, (
        "si el tag desaparece, el error tiene que mostrar la URL que falló"
    )
    assert text.count('--home "$ACME_HOME" --config-home "$ACME_HOME"') >= 4, (
        "registro, emisión e instalación del certificado tienen que usar el home "
        "de config/acme, igual que el cron de renovación"
    )
    assert "no pude copiar el certificado a config/certs" in text, (
        "--install-cert fallando no puede reportarse como instalación correcta"
    )


def test_installer_regenerates_every_example_token() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = text.split("is_placeholder() {", 1)[1].split("}", 1)[0]
    patterns = [
        part.strip().strip('"')
        for part in next(line for line in body.splitlines() if "return 0" in line)
        .split(")")[0]
        .split("|")
    ]

    for value in ("", "cambiar-por-un-token-largo", "cambiar-esto", "changeme"):
        assert any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns), (
            f"{value!r} tiene que contar como placeholder: es un valor público del "
            "repositorio y no puede quedar como secreto de la API"
        )


def test_acme_helpers_are_present() -> None:
    for name in ("acme-reload.sh", "acme-renew.sh"):
        path = ROOT / "deploy" / name
        assert path.is_file(), f"falta deploy/{name}"
        if os.name != "nt":
            assert path.stat().st_mode & 0o111, f"deploy/{name} tiene que ser ejecutable"
