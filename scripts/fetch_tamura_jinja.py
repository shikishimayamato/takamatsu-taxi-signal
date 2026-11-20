#!/usr/bin/env python3
"""田村神社の公式年間行事から主な祭事を既存データに統合する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = "https://tamurajinja.com"
SCHEDULE = ORIGIN + "/event/"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "events.json"
VENUE = "讃岐國一宮 田村神社"
SOURCE = "公式・田村神社"
PREFIX = "tamura-jinja-"
JST = timezone(timedelta(hours=9))
AGENT = "TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)"
FESTIVALS = {
    "歳旦祭": "saitan",
    "御火焚き祭": "hitaki",
    "春季例大祭": "spring",
    "夏越祭": "nagoshisai",
    "七夕祭": "tanabata",
    "燈籠祭": "toro",
    "秋季例大祭": "autumn",
    "大祓式": "oharae",
}
DATED_HEADING = re.compile(
    r"(.+?)\(\s*(\d{1,2}月\d{1,2}(?:日)?(?:、(?:\d{1,2}月)?\d{1,2}(?:日)?)*)\s*\)"
)
DAY = re.compile(r"(?:(\d{1,2})月)?(\d{1,2})日?")


def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    rows = {}
    found = set()
    for heading in soup.find_all("h2"):
        value = unicodedata.normalize("NFKC", heading.get_text(strip=True))
        match = DATED_HEADING.fullmatch(value)
        if not match or (title := match[1].strip()) not in FESTIVALS:
            continue
        found.add(title)
        month = None
        for part in match[2].split("、"):
            day = DAY.fullmatch(part)
            month = int(day[1]) if day[1] else month
            for year in (today.year, today.year + 1):
                when = date(year, month, int(day[2]))
                if not today <= when <= today + timedelta(days=180):
                    continue
                ds = when.isoformat()
                key = f"{PREFIX}{FESTIVALS[title]}-{ds}"
                rows[key] = {
                    "id": key,
                    "title": title,
                    "date": ds,
                    "venue": VENUE,
                    "venueArea": "一宮町",
                    "zone": "other",
                    "source": SOURCE,
                    "url": SCHEDULE,
                    "doorAt": None,
                    "startAt": None,
                    "endAt": None,
                    "note": "神社公式の年間祭事予定。開催時刻は未掲載。",
                }
    if not {"歳旦祭", "大祓式"} <= found:
        raise ValueError("公式の年間祭事一覧を確認できません")
    return list(rows.values())


def fetch(session: requests.Session, today: date) -> list[dict]:
    body = get(session, ORIGIN + "/robots.txt", 500_000, missing_ok=True)
    robot = RobotFileParser()
    robot.parse(body.decode("utf-8-sig").splitlines() if body else [])
    if not robot.can_fetch(AGENT, SCHEDULE):
        raise PermissionError(f"取得が許可されていません: {SCHEDULE}")
    return parse_schedule(get(session, SCHEDULE, 2_000_000), today)


def owned(event: dict) -> bool:
    return (
        event.get("venue") == VENUE
        and event.get("source") == SOURCE
        and str(event.get("id", "")).startswith(PREFIX)
        and event.get("sourceType") not in {"manual", "admin"}
    )


def previous_rows(session: requests.Session, now: datetime) -> tuple[list[dict], str | None]:
    try:
        payload = json.loads(get(session, PUBLISHED, 2_000_000))
        source = next(s for s in payload["sources"] if s.get("venue") == VENUE)
        last_ok = datetime.fromisoformat(source["lastSuccessAt"])
        if not timedelta(0) <= now - last_ok < timedelta(days=7):
            return [], None
        rows = [
            row for row in payload["events"] if owned(row) and row["date"] >= now.date().isoformat()
        ]
        return rows, source["lastSuccessAt"]
    except (requests.RequestException, ValueError, TypeError, KeyError, StopIteration) as exc:
        print(f"{VENUE}の前回データを利用できません: {type(exc).__name__}", file=sys.stderr)
        return [], None


def merge(
    payload: dict, rows: list[dict], status: str, now: datetime, last_success: str | None = None
) -> dict:
    if payload.get("schema") != 1 or not isinstance(payload.get("events"), list):
        raise ValueError("既存events.jsonの形式が異なります")
    old_sources = payload.get("sources", [])
    current = next((source for source in old_sources if source.get("venue") == VENUE), {})
    sources = [source for source in old_sources if source.get("venue") != VENUE]
    checked = now.isoformat(timespec="seconds")
    events = [event for event in payload["events"] if not owned(event)]
    protected = {event.get("id") for event in events}
    official = {row["id"]: row for row in rows if row["id"] not in protected}
    events.extend(official.values())
    events.sort(key=lambda e: (e.get("date", ""), e.get("venue", ""), e.get("title", "")))
    source = {"venue": VENUE, "status": status, "count": len(official), "checkedAt": checked}
    if status == "ok":
        source["lastSuccessAt"] = checked
    elif last_success or current.get("lastSuccessAt"):
        source["lastSuccessAt"] = last_success or current["lastSuccessAt"]
    partial = any(s.get("status") in {"partial", "stale", "error"} for s in [*sources, source])
    result = dict(payload, events=events, sources=[*sources, source], partial=partial)
    if official and VENUE not in result.get("sourceName", ""):
        result["sourceName"] = result.get("sourceName", "複数公式サイト") + "・" + VENUE
    return result


def main() -> int:
    now = datetime.now(JST)
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        last_success = None
        with requests.Session() as session:
            session.headers.update({"User-Agent": AGENT, "Accept-Language": "ja-JP,ja;q=0.9"})
            try:
                rows = fetch(session, now.date())
                status = "ok"
            except (requests.RequestException, ValueError, PermissionError) as exc:
                print(f"{VENUE}取得警告: {exc}", file=sys.stderr)
                rows = [
                    row
                    for row in payload["events"]
                    if owned(row) and row["date"] >= now.date().isoformat()
                ]
                if not rows:
                    rows, last_success = previous_rows(session, now)
                status = "stale" if rows else "error"
        result = merge(payload, rows, status, now, last_success)
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", dir=OUTPUT.parent, encoding="utf-8", newline="\n", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(OUTPUT)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(
            f'{VENUE}統合完了: {result["sources"][-1]["count"]}件 / {status} / 全施設合計 {len(result["events"])}件'
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"{VENUE}統合警告: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
