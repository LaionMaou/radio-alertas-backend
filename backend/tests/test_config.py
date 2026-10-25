from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from radio_alerts_backend.config import Settings, load_settings
from radio_alerts_backend.errors import ConfigError


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


def _settings(**overrides: object) -> Settings:
    values = {"internal_token": "i" * 32}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.dry_run is True
    assert settings.p2p_code_set == {551, 552, 556}
    assert settings.typhoon_areas == ""
    assert settings.typhoon_area_set == frozenset()
    assert settings.typhoon_ann_tts is False
    assert settings.tsunami_min_rank == 1
    assert settings.quake_min_scale == 45
    assert settings.quake_min_magnitude == 7.0
    assert settings.p2p_backoff_max == 30.0
    assert settings.jma_min_interval_seconds == 300


def test_p2p_codes_are_normalized_and_deduplicated() -> None:
    settings = _settings(p2p_codes="556, 551,556")
    assert settings.p2p_codes == "556,551"
    assert settings.p2p_code_set == {551, 556}


@pytest.mark.parametrize("value", ["", "550", "551,abc"])
def test_invalid_p2p_codes_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(p2p_codes=value)


def test_typhoon_areas_are_normalized() -> None:
    settings = _settings(typhoon_areas="130000, 140000,130000")
    assert settings.typhoon_areas == "130000,140000"
    assert settings.typhoon_area_set == {"130000", "140000"}


@pytest.mark.parametrize("value", ["13000", "abcdef", "130000,14"])
def test_invalid_typhoon_areas_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(typhoon_areas=value)


def test_tsunami_grade_is_normalized() -> None:
    assert _settings(tsunami_min_grade="warning").tsunami_min_grade == "Warning"
    assert _settings(tsunami_min_grade="MajorWarning").tsunami_min_rank == 3


def test_invalid_tsunami_grade_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(tsunami_min_grade="None")


def test_jma_polling_below_five_minutes_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(jma_min_interval_seconds=60)
    with pytest.raises(ValidationError):
        _settings(typhoon_poll_seconds=60)


def test_backoff_max_must_not_be_lower_than_initial() -> None:
    with pytest.raises(ValidationError):
        _settings(p2p_backoff_initial=10, p2p_backoff_max=5)


def test_runtime_requires_internal_token() -> None:
    errors = _settings(internal_token="").validate_runtime({})
    assert any("INTERNAL_TOKEN" in error for error in errors)


def test_runtime_requires_tts_token_when_live() -> None:
    errors = _settings(dry_run=False, tts_api_token="").validate_runtime({})
    assert any("TTS_API_TOKEN" in error for error in errors)


def test_runtime_requires_areas_for_typhoon_audio() -> None:
    errors = _settings(typhoon_ann_tts=True, typhoon_areas="").validate_runtime({})
    assert any("TYPHOON_AREAS" in error for error in errors)


def test_runtime_rejects_multiple_workers() -> None:
    errors = _settings().validate_runtime({"WEB_CONCURRENCY": "2"})
    assert any("WEB_CONCURRENCY" in error for error in errors)
    assert _settings().validate_runtime({"WEB_CONCURRENCY": "1"}) == []


def test_load_settings_reads_dotenv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "\n".join(
            [
                "INTERNAL_TOKEN=" + "i" * 32,
                "P2P_CODES=551,556",
                "TYPHOON_AREAS=130000,140000",
                "TYPHOON_ANN_TTS=true",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.p2p_code_set == {551, 556}
    assert settings.typhoon_area_set == {"130000", "140000"}
    assert settings.typhoon_ann_tts is True


def test_load_settings_rejects_missing_token(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("DRY_RUN=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="INTERNAL_TOKEN"):
        load_settings()


def test_load_settings_does_not_echo_invalid_values(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "INTERNAL_TOKEN=" + "i" * 32 + "\nP2P_CODES=999\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError) as caught:
        load_settings()

    assert "999" not in str(caught.value)


def test_safe_summary_hides_secrets() -> None:
    settings = _settings(
        internal_token="internal-secret-value",
        tts_api_token="tts-secret-value",
        discord_webhook_url="https://discord.example/hook/secret",
    )

    summary = json.dumps(settings.safe_summary())

    assert "internal-secret-value" not in summary
    assert "tts-secret-value" not in summary
    assert "discord.example" not in summary
    assert '"internal_token_configured": true' in summary
