from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .models import Event, EventKind
from .normalize import format_jst_time, scale_code

MAX_CHARS = 280

_EEW_INTRO = "緊急地震速報です。強い揺れに警戒してください。"
_EEW_MAJOR = "大地震です。大地震です。身の安全を確保してください。"
_EEW_STRONG = "緊急地震速報です。落ち着いて行動してください。まもなく地震が来ます。"
_MAJOR_TSUNAMI = "大津波警報が発表されました。海岸付近の方は高台に避難してください。"

_TSUNAMI_LABELS = {
    "MajorWarning": "大津波警報",
    "Warning": "津波警報",
    "Watch": "津波注意報",
}

_TSUNAMI_ACTIONS = {
    "MajorWarning": "直ちに高台へ避難してください。",
    "Warning": "海岸から離れてください。",
    "Watch": "海から離れてください。",
}

_QUAKE_TSUNAMI_SENTENCES = {
    "None": "津波の心配はありません。",
    "NonEffective": "津波の心配はありません。",
    "Watch": "津波注意報が発表されています。海から離れてください。",
    "Warning": "津波警報が発表されています。海岸から離れてください。",
    "MajorWarning": "大津波警報が発表されています。直ちに高台へ避難してください。",
    "Unknown": "津波情報を確認してください。",
    "Checking": "津波情報を確認してください。",
}


def render(event: Event) -> str:
    if event.kind == EventKind.EEW:
        return _eew(event)
    if event.kind == EventKind.EARTHQUAKE:
        return _quake(event)
    if event.kind == EventKind.TSUNAMI:
        return _tsunami(event)
    if event.kind == EventKind.TYPHOON:
        return _typhoon(event)
    return "警報が発表されました。"


def _eew(event: Event) -> str:
    pieces = (
        _EEW_INTRO,
        _EEW_INTRO,
        _eew_variant(event.max_intensity),
        _eew_data(event),
    )
    return _compose([(piece, False) for piece in pieces if piece])


def _eew_variant(intensity: dict[str, Any] | None) -> str:
    code = scale_code((intensity or {}).get("value"))
    if code >= 70:
        return _EEW_MAJOR
    if 55 <= code <= 60:
        return _EEW_STRONG
    return ""


def _eew_data(event: Event) -> str:
    epicenter = event.epicenter or {}
    pieces: list[str] = []
    name = _clip(epicenter.get("name") or "", 24)
    if name:
        pieces.append(f"震源は{name}。")
    magnitude = _magnitude(epicenter)
    if magnitude is not None:
        pieces.append(f"マグニチュード{magnitude}。")
    intensity = _intensity_sentence(event.max_intensity)
    if intensity:
        pieces.append(intensity)
    return "".join(pieces)


def _quake(event: Event) -> str:
    epicenter = event.epicenter or {}
    name = _clip(epicenter.get("name") or "震源不明")
    time_text = format_jst_time(event.origin_at or event.issued_at)
    parts: list[tuple[str, bool]] = [("地震情報。", False)]
    if time_text:
        parts.append((f"{time_text}頃、{name}で地震がありました。", False))
    else:
        parts.append((f"{name}で地震がありました。", False))
    intensity = _intensity_sentence(event.max_intensity)
    if intensity:
        parts.append((intensity, False))
    magnitude = _magnitude(epicenter)
    if magnitude is not None:
        parts.append((f"マグニチュード{magnitude}。", True))
    tsunami = _quake_tsunami_sentence(event.tsunami)
    if tsunami:
        parts.append((tsunami, False))
    return _compose(parts)


def _tsunami(event: Event) -> str:
    data = event.tsunami or {}
    grade = str(data.get("grade") or data.get("domestic") or "Unknown")
    if grade == "MajorWarning":
        return _MAJOR_TSUNAMI
    label = _TSUNAMI_LABELS.get(grade, "津波情報")
    action = _TSUNAMI_ACTIONS.get(grade, "最新の情報を確認してください。")
    area_text = _tsunami_areas(data.get("areas"), grade)
    if area_text:
        target = f"{area_text}に{label}が発表されました。"
    else:
        target = f"{label}が発表されました。"
    return _compose([(f"{label}。", False), (target, False), (action, False)])


def _typhoon(event: Event) -> str:
    data = event.typhoon or {}
    head = _typhoon_head(data)
    location = _clip(data.get("location") or "海上")
    parts: list[tuple[str, bool]] = [
        ("台風情報。", False),
        (f"{head}、{location}にあります。", False),
    ]
    measurements = _typhoon_measurements(data)
    if measurements:
        parts.append((measurements, True))
    area = _typhoon_area(data)
    if area:
        parts.append((f"{area}では暴風に警戒してください。", False))
    else:
        parts.append(("暴風や高波に警戒してください。", False))
    return _compose(parts)


def _intensity_sentence(intensity: dict[str, Any] | None) -> str:
    label = str((intensity or {}).get("label") or "")
    value = (intensity or {}).get("value")
    if not label or label == "不明":
        return ""
    try:
        open_ended = int(value) in (46, 99)
    except (TypeError, ValueError):
        open_ended = False
    if open_ended:
        return f"最大震度{label}と予想されます。"
    return f"最大震度{label}。"


def _quake_tsunami_sentence(tsunami: dict[str, Any] | None) -> str:
    if not tsunami:
        return ""
    grade = str(tsunami.get("domestic") or tsunami.get("grade") or "")
    return _QUAKE_TSUNAMI_SENTENCES.get(grade, "")


def _typhoon_head(data: dict[str, Any]) -> str:
    label = str(data.get("number_label") or "").strip()
    number = "".join(char for char in str(data.get("number") or "") if char.isdigit())
    name = _clip(data.get("name") or "", 24)
    if not label and len(number) >= 2:
        value = int(number[-2:])
        if value:
            label = f"台風{value}号"
    return f"{label or '台風'}{name}は"


def _typhoon_measurements(data: dict[str, Any]) -> str:
    pieces: list[str] = []
    pressure = _as_int(data.get("pressure_hpa"))
    if pressure is not None:
        pieces.append(f"中心気圧{pressure}ヘクトパスカル")
    wind = _as_int(data.get("wind_mps"))
    if wind is not None:
        pieces.append(f"最大風速{wind}メートル")
    return "、".join(pieces) + "。" if pieces else ""


def _typhoon_area(data: dict[str, Any]) -> str:
    areas = data.get("areas")
    if not isinstance(areas, list):
        return ""
    best: dict[str, Any] | None = None
    best_probability = -1
    for area in areas:
        if not isinstance(area, dict):
            continue
        probability = _as_int(area.get("probability"))
        if probability is None or probability <= best_probability:
            continue
        best = area
        best_probability = probability
    if best is None:
        return ""
    return _clip(best.get("prefecture") or best.get("name") or "", 24)


def _tsunami_areas(areas: Any, grade: str) -> str:
    if not isinstance(areas, list):
        return ""
    items = [area for area in areas if isinstance(area, dict)]
    same = [area for area in items if str(area.get("grade") or "") == grade]
    chosen = same or items
    names: list[str] = []
    for area in chosen:
        name = _clip(area.get("name") or "", 20)
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    if len(names) <= 3:
        return "、".join(names)
    return "、".join(names[:3]) + f"ほか{len(names) - 3}地域"


def _magnitude(epicenter: dict[str, Any]) -> str | None:
    try:
        value = float(epicenter.get("magnitude"))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return f"{value:.1f}"


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _clip(value: Any, limit: int = 24) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _compose(parts: Sequence[tuple[str, bool]]) -> str:
    text = "".join(sentence for sentence, _ in parts)
    if len(text) <= MAX_CHARS:
        return text
    text = "".join(sentence for sentence, optional in parts if not optional)
    if len(text) <= MAX_CHARS:
        return text
    result = ""
    for sentence, _ in parts:
        if len(result) + len(sentence) > MAX_CHARS:
            break
        result += sentence
    return result
