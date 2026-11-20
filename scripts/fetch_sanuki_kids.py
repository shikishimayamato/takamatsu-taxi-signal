#!/usr/bin/env python3
"""さぬきこどもの国の公式カレンダーから特別催事を取得する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = "https://www.sanuki.or.jp"
CALENDAR = ORIGIN + "/event/"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "events.json"
VENUE = "さぬきこどもの国"
SOURCE = "公式・さぬきこどもの国"
PREFIX = "sanuki-kids-"
JST = timezone(timedelta(hours=9))
AGENT = "TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)"
SPECIAL = re.compile(
    r"コンサート|人形劇|フェスティバル|音楽会|演奏会|映画会|特別(?:上映|投映|公演|プログラム)|熟睡プラネタリウム|星空クリスマス|キッズプラネタリウム|工作マーケット|防災とボランティアのつどい|おばけやしき"
)
EXCLUDE = re.compile(r"中止|延期|作品募集|作品受付|申込締切|出張|離島")
DAY = r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:\([^)]{1,8}\))?"
DATES = re.compile(DAY + r"(?:\s*[~〜～–―-]\s*" + DAY + r")?")
HM = r"(?:[01]?\d|2[0-3]):[0-5]\d"
TIME = re.compile(rf"(?<!\d)({HM})\s*[~〜～–―-]\s*({HM})(?!\d)")
DETAIL = re.compile(r"https://(?:www\.)?sanuki\.or\.jp/event/detail\.php\?id=(\d+)")


def norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def detail_links(soup: BeautifulSoup, page: str) -> dict[str, str]:
    links = {}
    for anchor in soup.select("a[href]"):
        url = urljoin(page, anchor["href"])
        match = DETAIL.fullmatch(url)
        if match and SPECIAL.search(norm(anchor.get_text(" ", strip=True))):
            links[match[1]] = CALENDAR + "detail.php?id=" + match[1]
    return links


def parse_detail(soup: BeautifulSoup, event_id: str, url: str, today: date) -> list[dict]:
    title = (
        norm(soup.title.get_text(" ", strip=True)).split("|")[0].split("｜")[0].strip()
        if soup.title
        else ""
    )
    if not SPECIAL.search(title) or EXCLUDE.search(title):
        return []
    fields = {}
    for tr in soup.select("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            fields[norm(cells[0].get_text(" ", strip=True)).rstrip(":")] = norm(
                cells[1].get_text(" ", strip=True)
            )
    place = fields.get("開催場所") or fields.get("場所") or fields.get("会場") or ""
    if re.search(r"児童館|公民館|交流センター|ホール|図書館|小豆島", place) and VENUE not in place:
        return []
    date_text = fields.get("開催日", "")
    match = DATES.fullmatch(date_text)
    if not match:
        raise ValueError(f"開催日を読み取れません: {title}")
    first = date(*map(int, match.groups()[:3]))
    last = date(*map(int, match.groups()[3:])) if match[4] else first
    if last < today or first > today + timedelta(days=60):
        return []
    if not first <= last <= first + timedelta(days=31):
        raise ValueError(f"開催期間が想定外です: {title}")
    time_text = fields.get("開催時間") or fields.get("時間") or ""
    times = list(TIME.finditer(time_text))
    sessions = []
    if first == last:
        sessions = sorted(
            {
                tuple(f"{int(h):02d}:{m}" for h, m in (value.split(":") for value in tm.groups()))
                for tm in times
            }
        )
        sessions = [(start, end) for start, end in sessions if start < end]
    ds = first.isoformat()
    return [
        {
            "id": PREFIX + event_id + (f"-{index}" if index else ""),
            "title": title,
            "date": ds,
            "endDate": last.isoformat() if last != first else None,
            "venue": VENUE,
            "venueArea": "香南町由佐",
            "zone": "other",
            "source": SOURCE,
            "url": url,
            "doorAt": None,
            "startAt": f"{ds}T{start}:00+09:00" if start else None,
            "endAt": f"{ds}T{end}:00+09:00" if end else None,
            "endTimeType": "公式掲載の開催時間終了" if end else None,
        }
        for index, (start, end) in enumerate(sessions or [(None, None)])
    ]


def fetch(session: requests.Session, today: date) -> list[dict]:
    response = session.get(ORIGIN + "/robots.txt", timeout=15)
    if response.status_code != 404:
        response.raise_for_status()
    robot = RobotFileParser()
    robot.parse([] if response.status_code == 404 else response.text.splitlines())

    def read(url):
        if not robot.can_fetch(AGENT, url):
            raise PermissionError(f"取得が許可されていません: {url}")
        response = session.get(url, timeout=18)
        response.raise_for_status()
        if (
            not response.url.startswith((ORIGIN + "/", "https://sanuki.or.jp/"))
            or len(response.content) > 2_000_000
        ):
            raise ValueError("公式サイトの応答が想定外です")
        return BeautifulSoup(response.content, "html.parser")

    monday = today - timedelta(days=today.weekday())
    links = {}
    for offset in (0, 4, 8):
        week = monday + timedelta(weeks=offset)
        page = CALENDAR + f"?week={week.year}-{week.month}-{week.day:02d}"
        soup = read(page)
        if not soup.title or "イベントカレンダー" not in soup.title.get_text():
            raise ValueError(f"公式イベントカレンダーを確認できません: {page}")
        links.update(detail_links(soup, page))
    rows = []
    for event_id, url in links.items():
        rows.extend(parse_detail(read(url), event_id, url, today))
    return rows


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
