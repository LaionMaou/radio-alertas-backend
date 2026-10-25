class AlertsError(Exception):
    """Error base del backend."""


class ConfigError(AlertsError):
    """Configuración inválida."""


class UpstreamError(AlertsError):
    """Fallo de P2P o JMA."""


class TTSError(AlertsError):
    """Fallo al hablar con la cadena TTS."""
