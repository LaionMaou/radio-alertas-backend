from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from xml.etree.ElementTree import Element

from .common import attr, parse_cod, parse_time, text, to_int

_TYPHOON_MARKERS = ("VPTW61", "VPTA51")
_TYPHOON_TITLE = "台風"


@dataclass(frozen=True)
class FeedEntry:
    title: str
    url: str
    updated_at: datetime | None


@dataclass(frozen=True)
class TyphoonInfo:
    event_id: str
    report_at: datetime | None
    serial: str
    info_type: str
    name: str
    name_kana: str
    number: str
    class_text: str
    location: str
    direction: str
    pressure_hpa: int | None
    wind_mps: int | None
    gust_mps: int | None
    lat: float | None
    lon: float | None
    source_url: str

    @property
    def number_label(self) -> str | None:
        digits = "".join(char for char in self.number if char.isdigit())
        if len(digits) < 2:
            return None
        number = int(digits[-2:])
        return f"台風{number}号" if number else None


@dataclass(frozen=True)
class TyphoonAreaProbability:
    code: str
    name: str
    prefecture: str
    probability: int
    at: datetime | None
    window: str


@dataclass(frozen=True)
class TyphoonProbabilities:
    event_id: str
    report_at: datetime | None
    areas: list[TyphoonAreaProbability]


def parse_feed(root: Element) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    for entry in root.iter():
        if not entry.tag.endswith("}entry") and entry.tag != "entry":
            continue
        title = text(entry, "{*}title")
        link = entry.find("{*}link")
        url = attr(link, "href")
        if not url:
            continue
        entries.append(
            FeedEntry(
                title=title,
                url=url,
                updated_at=parse_time(text(entry, "{*}updated")),
            )
        )
    return entries


def select_latest(
    entries: Iterable[FeedEntry],
    markers: Sequence[str] = _TYPHOON_MARKERS,
) -> dict[str, FeedEntry]:
    latest: dict[str, FeedEntry] = {}
    for marker in markers:
        candidates = [
            entry for entry in entries if marker in entry.url and _TYPHOON_TITLE in entry.title
        ]
        if not candidates:
            continue
        best = candidates[0]
        for candidate in candidates[1:]:
            if _newer(candidate, best):
                best = candidate
        latest[marker] = best
    return latest


def parse_vptw61(root: Element, *, source_url: str = "") -> TyphoonInfo | None:
    head = root.find("{*}Head")
    body = root.find("{*}Body")
    if body is None:
        return None
    info = _current_forecast(body)
    if info is None:
        return None

    properties = _properties(info)
    name_part = properties.get("呼称")
    class_part = properties.get("階級")
    center_part = properties.get("中心")
    wind_part = properties.get("風")

    coordinate = _coordinate(center_part)
    lat, lon = parse_cod(coordinate)

    return TyphoonInfo(
        event_id=text(head, "{*}EventID"),
        report_at=parse_time(text(head, "{*}ReportDateTime")),
        serial=text(head, "{*}Serial"),
        info_type=text(head, "{*}InfoType"),
        name=text(name_part, "{*}TyphoonNamePart/{*}Name"),
        name_kana=text(name_part, "{*}TyphoonNamePart/{*}NameKana"),
        number=text(name_part, "{*}TyphoonNamePart/{*}Number"),
        class_text=text(class_part, "{*}ClassPart/{*}TyphoonClass")
        or text(info, "{*}Item/{*}Area/{*}Name"),
        location=text(center_part, "{*}CenterPart/{*}Location"),
        direction=text(center_part, "{*}CenterPart/{*}Direction"),
        pressure_hpa=to_int(text(center_part, "{*}CenterPart/{*}Pressure")),
        wind_mps=_wind_speed(wind_part, unit="m/s", condition="中心付近"),
        gust_mps=_wind_speed(wind_part, gust=True),
        lat=lat,
        lon=lon,
        source_url=source_url,
    )


def parse_vpta51(root: Element, area_codes: Iterable[str]) -> TyphoonProbabilities:
    codes = {str(code).strip() for code in area_codes if str(code).strip()}
    head = root.find("{*}Head")
    event_id = text(head, "{*}EventID")
    report_at = parse_time(text(head, "{*}ReportDateTime"))
    if not codes:
        return TyphoonProbabilities(event_id=event_id, report_at=report_at, areas=[])

    best: dict[str, TyphoonAreaProbability] = {}

    for info in root.iter():
        if not info.tag.endswith("}MeteorologicalInfo") and info.tag != "MeteorologicalInfo":
            continue
        info_type = attr(info, "type")
        if info_type == "台風呼称":
            continue
        info_time = parse_time(text(info, "{*}DateTime"))
        window = _window_label(info_type)
        for item in info.findall("{*}Item"):
            matched = _match_area(item, codes)
            if matched is None:
                continue
            code, label = matched
            for probability in item.iter():
                if not probability.tag.endswith("FiftyKtWindProbability"):
                    continue
                value = to_int(probability.text)
                if value is None:
                    continue
                _keep_best(
                    best,
                    TyphoonAreaProbability(
                        code=code,
                        name=label,
                        prefecture=text(item, "{*}Area/{*}Prefecture"),
                        probability=value,
                        at=info_time,
                        window=window,
                    ),
                )

    series = root.find("{*}Body/{*}MeteorologicalInfos/{*}TimeSeriesInfo")
    if series is not None:
        times: dict[str, datetime | None] = {}
        for define in series.findall("{*}TimeDefines/{*}TimeDefine"):
            times[attr(define, "timeId")] = parse_time(text(define, "{*}DateTime"))
        for item in series.findall("{*}Item"):
            matched = _match_area(item, codes)
            if matched is None:
                continue
            code, label = matched
            for probability in item.iter():
                if not probability.tag.endswith("FiftyKtWindProbability"):
                    continue
                value = to_int(probability.text)
                if value is None:
                    continue
                ref = attr(probability, "refID")
                _keep_best(
                    best,
                    TyphoonAreaProbability(
                        code=code,
                        name=label,
                        prefecture=text(item, "{*}Area/{*}Prefecture"),
                        probability=value,
                        at=times.get(ref),
                        window="3時間",
                    ),
                )

    areas = sorted(best.values(), key=lambda area: (-area.probability, area.code))
    return TyphoonProbabilities(event_id=event_id, report_at=report_at, areas=areas)


def _current_forecast(body: Element) -> Element | None:
    infos = body.findall("{*}MeteorologicalInfos/{*}MeteorologicalInfo")
    for info in infos:
        if attr(info.find("{*}DateTime"), "type") == "実況":
            return info
    return infos[0] if infos else None


def _properties(info: Element) -> dict[str, Element]:
    result: dict[str, Element] = {}
    for prop in info.findall("{*}Item/{*}Kind/{*}Property"):
        kind = text(prop, "{*}Type")
        if kind:
            result[kind] = prop
    return result


def _coordinate(part: Element | None) -> str:
    if part is None:
        return ""
    fallback = ""
    for coordinate in part.findall("{*}CenterPart/{*}Coordinate"):
        if not fallback:
            fallback = (coordinate.text or "").strip()
        if attr(coordinate, "type") == "中心位置（度）":
            return (coordinate.text or "").strip()
    return fallback


def _wind_speed(
    part: Element | None,
    *,
    unit: str | None = None,
    condition: str | None = None,
    gust: bool = False,
) -> int | None:
    if part is None:
        return None
    for speed in part.findall("{*}WindPart/{*}WindSpeed"):
        if unit is not None and attr(speed, "unit") != unit:
            continue
        if condition is not None and attr(speed, "condition") != condition:
            continue
        wanted = attr(speed, "type") == "最大瞬間風速"
        if gust != wanted:
            continue
        if unit is None and attr(speed, "unit") != "m/s":
            continue
        value = to_int(speed.text)
        if value is not None:
            return value
    return None


def _match_area(item: Element, codes: set[str]) -> tuple[str, str] | None:
    area = item.find("{*}Area")
    if area is None:
        return None
    code = text(area, "{*}Code")
    prefecture_code = text(area, "{*}PrefectureCode")
    name = text(area, "{*}Name")
    prefecture = text(area, "{*}Prefecture")
    if prefecture_code in codes:
        return prefecture_code, prefecture or name
    if code in codes:
        return code, name
    return None


def _keep_best(
    best: dict[str, TyphoonAreaProbability],
    candidate: TyphoonAreaProbability,
) -> None:
    current = best.get(candidate.code)
    if current is None or candidate.probability > current.probability:
        best[candidate.code] = candidate


def _window_label(info_type: str) -> str:
    for label in ("5日", "4日", "3日", "2日", "1日"):
        if f"{label}積算" in info_type:
            return f"{label}間"
    return "予報"


def _newer(candidate: FeedEntry, current: FeedEntry) -> bool:
    if candidate.updated_at is None:
        return False
    if current.updated_at is None:
        return True
    return candidate.updated_at > current.updated_at
