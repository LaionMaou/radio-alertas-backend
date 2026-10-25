from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError
from .normalize import TSUNAMI_GRADE_RANK

_ALLOWED_P2P_CODES = frozenset({551, 552, 556})
_TSUNAMI_GRADES = {"Watch": "Watch", "Warning": "Warning", "MajorWarning": "MajorWarning"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    dry_run: bool = True

    p2p_ws_url: str = "wss://api.p2pquake.net/v2/ws"
    p2p_codes: str = "551,552,556"
    p2p_backoff_initial: float = Field(default=1.0, ge=0.1)
    p2p_backoff_factor: float = Field(default=2.0, ge=1.0)
    p2p_backoff_max: float = Field(default=30.0, ge=1.0)
    p2p_backoff_jitter: float = Field(default=0.2, ge=0.0, le=1.0)
    p2p_alert_after_attempts: int = Field(default=10, ge=1)
    p2p_stale_event_seconds: int = Field(default=600, ge=30)

    quake_min_scale: int = 45
    quake_min_magnitude: float = Field(default=7.0, ge=0.0, le=12.0)
    eew_enabled: bool = True
    tsunami_min_grade: str = "Watch"

    typhoon_enabled: bool = True
    typhoon_ann_tts: bool = False
    typhoon_areas: str = ""
    typhoon_probability_threshold: int = Field(default=30, ge=1, le=100)
    typhoon_poll_seconds: int = Field(default=600, ge=300)

    jma_enabled: bool = True
    jma_user_agent: str = "KumaRadioAlerts/0.1 (+https://radio.kumakuma.fm)"
    jma_min_interval_seconds: int = Field(default=300, ge=300)

    tts_api_url: str = "http://alerts-tts:8090"
    tts_api_token: str = ""
    tts_result_timeout_seconds: int = Field(default=180, ge=10)

    internal_token: str = ""
    enable_simulate: bool = False
    discord_webhook_url: str = ""

    history_limit: int = Field(default=50, ge=1, le=500)
    history_days: int = Field(default=30, ge=1)
    api_port: int = Field(default=3000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_dir: Path = Path("/logs")
    station_name: str = "Kuma Radio"

    @field_validator("p2p_ws_url", "tts_api_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("p2p_ws_url")
    @classmethod
    def _check_ws_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("P2P_WS_URL debe empezar con ws:// o wss://")
        return value

    @field_validator("tts_api_url")
    @classmethod
    def _check_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("TTS_API_URL debe empezar con http:// o https://")
        return value

    @field_validator("p2p_codes")
    @classmethod
    def _check_p2p_codes(cls, value: str) -> str:
        raw = [part.strip() for part in str(value).split(",") if part.strip()]
        parsed: list[int] = []
        for part in raw:
            try:
                code = int(part)
            except ValueError as exc:
                raise ValueError("P2P_CODES debe contener números separados por coma") from exc
            if code not in _ALLOWED_P2P_CODES:
                raise ValueError("P2P_CODES solo admite 551, 552 y 556")
            if code not in parsed:
                parsed.append(code)
        if not parsed:
            raise ValueError("P2P_CODES no puede quedar vacío")
        return ",".join(str(code) for code in parsed)

    @field_validator("tsunami_min_grade")
    @classmethod
    def _check_tsunami_grade(cls, value: str) -> str:
        text = str(value).strip()
        normalized = text[:1].upper() + text[1:]
        if normalized not in _TSUNAMI_GRADES:
            raise ValueError("TSUNAMI_MIN_GRADE debe ser Watch, Warning o MajorWarning")
        return _TSUNAMI_GRADES[normalized]

    @field_validator("typhoon_areas")
    @classmethod
    def _check_typhoon_areas(cls, value: str) -> str:
        areas: list[str] = []
        for part in str(value).split(","):
            area = part.strip()
            if not area:
                continue
            if not (len(area) == 6 and area.isdigit()):
                raise ValueError("TYPHOON_AREAS debe usar códigos JMA de 6 dígitos")
            if area not in areas:
                areas.append(area)
        return ",".join(areas)

    @model_validator(mode="after")
    def _check_backoff(self) -> Settings:
        if self.p2p_backoff_max < self.p2p_backoff_initial:
            raise ValueError("P2P_BACKOFF_MAX no puede ser menor que P2P_BACKOFF_INITIAL")
        return self

    @property
    def p2p_code_set(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.p2p_codes.split(",") if part)

    @property
    def typhoon_area_set(self) -> frozenset[str]:
        return frozenset(part for part in self.typhoon_areas.split(",") if part)

    @property
    def tsunami_min_rank(self) -> int:
        return TSUNAMI_GRADE_RANK.get(self.tsunami_min_grade, 1)

    def validate_runtime(self, environ: dict[str, str] | None = None) -> list[str]:
        errors: list[str] = []
        env = os.environ if environ is None else environ

        if not self.internal_token:
            errors.append("INTERNAL_TOKEN es obligatorio")
        if not self.dry_run and not self.tts_api_token:
            errors.append("TTS_API_TOKEN es obligatorio cuando DRY_RUN=false")
        if not self.jma_user_agent.strip():
            errors.append("JMA_USER_AGENT es obligatorio")
        if self.typhoon_ann_tts and not self.typhoon_area_set:
            errors.append("TYPHOON_ANN_TTS=true requiere TYPHOON_AREAS")

        workers = env.get("WEB_CONCURRENCY")
        if workers not in (None, "", "1"):
            errors.append("WEB_CONCURRENCY debe ser 1: dos workers duplican el WebSocket de P2P")
        return errors

    def safe_summary(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "p2p_ws_url": self.p2p_ws_url,
            "p2p_codes": self.p2p_codes,
            "jma_enabled": self.jma_enabled,
            "quake_min_scale": self.quake_min_scale,
            "quake_min_magnitude": self.quake_min_magnitude,
            "tsunami_min_grade": self.tsunami_min_grade,
            "typhoon_enabled": self.typhoon_enabled,
            "typhoon_ann_tts": self.typhoon_ann_tts,
            "typhoon_areas": len(self.typhoon_area_set),
            "history_limit": self.history_limit,
            "history_days": self.history_days,
            "tts_api_url": self.tts_api_url,
            "tts_token_configured": bool(self.tts_api_token),
            "internal_token_configured": bool(self.internal_token),
            "discord_configured": bool(self.discord_webhook_url),
        }


def load_settings(env_file: str | Path | None = None) -> Settings:
    path = env_file
    if path is None:
        candidate = Path("config/.env")
        path = candidate if candidate.is_file() else None
    try:
        settings = Settings(_env_file=path)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(details or "configuración inválida") from None

    errors = settings.validate_runtime()
    if errors:
        raise ConfigError("; ".join(errors))
    return settings
