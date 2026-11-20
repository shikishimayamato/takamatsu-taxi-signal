#!/usr/bin/env python3
"""イオンシネマ高松東の公式作品一覧から特別上映を既存データに統合する。"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = "https://www.aeoncinema.com"
MOVIES = "/cinema2/takamatsu/movie/"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "events.json"
VENUE = "イオンシネマ高松東"
SOURCE = "公式・イオンシネマ高松東"
PREFIX = "aeon-cinema-takamatsu-"
JST = timezone(timedelta(hours=9))
AGENT = "TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)"


def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    if (
        not soup.title
        or "高松東" not in soup.title.get_text()
        or not soup.select_one(".p-movie__main")
    ):
        raise ValueError("高松東の公式作品一覧を確認できません")
    rows = []
    for card in soup.select(".p-movie__list.js_livesports_list"):
        link = card.select_one("a[href]")
        title = card.select_one(".p-movie__title")
        path = link.get("href", "") if link else ""
        match = re.fullmatch(re.escape(MOVIES) + r"(\d+)/", path)
        label = title.get_text(" ", strip=True) if title else ""
        if not match or not label:
            raise ValueError("ライブ・スポーツ作品の詳細を確認できません")
        if re.search(r"中止|延期", label):
            continue
        first = date.fromisoformat(card.get("data-screening-start-date", "")[:10])
        finish = card.get("data-screening-end-date", "")
        last = date.fromisoformat(finish[:10]) if finish else first
        if last < first:
            raise ValueError("上映期間が想定外です")
        if last < today or first > today + timedelta(days=120):
            continue
        ds = first.isoformat()
        rows.append(
            {
                "id": f"{PREFIX}{match[1]}-{ds}",
                "title": label,
                "date": ds,
                "endDate": last.isoformat() if last != first else None,
                "venue": VENUE,
                "venueArea": "福岡町",
                "zone": "other",
                "source": SOURCE,
                "url": ORIGIN + path,
                "doorAt": None,
                "startAt": None,
                "endAt": None,
                "note": "イオンシネマ高松東のライブ・スポーツ特別上映。開始・終了時刻は劇場の上映スケジュールをご確認ください。",
            }
        )
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    body = get(session, ORIGIN + "/robots.txt", 500_000, missing_ok=True)
    robot = RobotFileParser()
    robot.parse(body.decode("utf-8-sig").splitlines() if body else [])
    rows = {}
    for suffix in ("", "comingsoon.html"):
        url = ORIGIN + MOVIES + suffix
        if not robot.can_fetch(AGENT, url):
            raise PermissionError(f"取得が許可されていません: {url}")
        for row in parse_schedule(get(session, url, 2_000_000), today):
            rows[row["id"]] = row
    return list(rows.values())


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
            row
            for row in payload["events"]
            if owned(row) and (row.get("endDate") or row["date"]) >= now.date().isoformat()
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
                    if owned(row) and (row.get("endDate") or row["date"]) >= now.date().isoformat()
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
