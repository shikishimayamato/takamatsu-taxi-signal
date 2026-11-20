#!/usr/bin/env python3
"""香川県立武道館の公式月間予定表から大会を抽出する。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import pymupdf
import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = "https://taiikukan.jp"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "events.json"
VENUE = "香川県立武道館"
SOURCE = "公式・香川県立武道館"
PREFIX = "kagawa-budokan-"
JST = timezone(timedelta(hours=9))
AGENT = "TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)"
WEEK = "月火水木金土日"
CONTEST = re.compile(r"大会|選手権|予選|対抗戦|競技会|フェスティバル")
EXCLUDE = re.compile(r"中止|延期|予備日|監督会議|準備|練習会")


def norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def schedule_links(html: bytes, today: date) -> dict[tuple[int, int], str]:
    links = {}
    for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
        label = re.fullmatch(r"(20\d{2})年(\d{1,2})月の行事予定", norm(anchor.get_text(strip=True)))
        url = urljoin(ORIGIN + "/", anchor["href"])
        if label and re.fullmatch(r"https://taiikukan\.jp/file/[A-Za-z0-9._-]+", url):
            year, month = map(int, label.groups())
            if 1 <= month <= 12 and 0 <= (year - today.year) * 12 + month - today.month <= 2:
                links.setdefault((year, month), url)
    if (today.year, today.month) not in links:
        raise ValueError("公式サイトに今月の行事予定がありません")
    return links


def read_rows(body: bytes, year: int, month: int, url: str, today: date) -> list[dict]:
    with pymupdf.open(stream=body, filetype="pdf") as pdf:
        if len(pdf) != 1:
            raise ValueError("月間予定表のページ数が想定外です")
        page = pdf[0]
        words = [(pymupdf.Rect(*w[:4]), norm(w[4])) for w in page.get_text("words")]
        heading = re.search(
            r"(20\d{2})年(\d{1,2})月\s*香川県立武道館行事予定", norm(page.get_text(sort=True))
        )
        if not heading or int(heading[2]) != month:
            raise ValueError("武道館の月間予定表を確認できません")
        weekdays = [
            (r, v) for r, v in words if r.x0 < page.rect.width * 0.115 and v in WEEK and len(v) == 1
        ]
        days = {}
        for r, v in words:
            if r.x0 >= page.rect.width * 0.08 or not re.fullmatch(r"[1-9]|[12]\d|3[01]", v):
                continue
            neighbor = [weekday for rect, weekday in weekdays if abs(rect.y0 - r.y0) < 3]
            if len(neighbor) == 1:
                days[int(v)] = ((r.y0 + r.y1) / 2, neighbor[0])
        # 2026年11月号には表題年の誤記がある。公式リンクの年を全行の曜日で検証する。
        expected = range(1, monthrange(year, month)[1] + 1)
        if sorted(days) != list(expected) or any(
            days[d][1] != WEEK[date(year, month, d).weekday()] for d in expected
        ):
            raise ValueError("公式リンクの年月と予定表の曜日が一致しません")
        if int(heading[1]) != year:
            print(f"{VENUE}: PDF表題年 {heading[1]} / 公式リンクと曜日で {year} 年を確認")
        hour_labels = {
            v: r
            for r, v in reversed(sorted(words, key=lambda w: w[0].y0))
            if v in {"9:00", "17:00"}
        }
        if len(hour_labels) != 2:
            raise ValueError("時間軸を確認できません")
        x9 = hour_labels["9:00"].x0 - 2.6
        hour_width = (hour_labels["17:00"].x0 - hour_labels["9:00"].x0) / 8
        if hour_width <= 0:
            raise ValueError("時間軸の順序が想定外です")
        borders = [
            item["rect"]
            for item in page.get_drawings()
            if item["type"] == "f" and 1.5 <= item["rect"].width <= 4 and item["rect"].height >= 8
        ]
        rows = []
        for rect, title in words:
            if not CONTEST.search(title) or EXCLUDE.search(title):
                continue
            y = (rect.y0 + rect.y1) / 2
            day = min(days, key=lambda d: abs(days[d][0] - y))
            when = date(year, month, day)
            if when < today or abs(days[day][0] - y) > 8:
                continue
            enclosing = [(r.x0 + r.x1) / 2 for r in borders if r.y0 <= y <= r.y1]
            left = max((x for x in enclosing if x <= rect.x0 + 1), default=None)
            right = min((x for x in enclosing if x >= rect.x1 - 1), default=None)
            start = finish = None
            if left is not None and right is not None:
                a, b = (round((9 + (x - x9) / hour_width) * 2) * 30 for x in (left, right))
                if 420 <= a < b <= 1260 and all(
                    abs(x - (x9 + (m / 60 - 9) * hour_width)) <= 3
                    for x, m in ((left, a), (right, b))
                ):
                    start, finish = (f"{m//60:02d}:{m%60:02d}" for m in (a, b))
            ds = when.isoformat()
            key = hashlib.sha256(f"{ds}|{title}".encode()).hexdigest()[:16]
            rows.append(
                {
                    "id": PREFIX + key,
                    "title": title,
                    "date": ds,
                    "venue": VENUE,
                    "venueArea": "福岡町",
                    "zone": "other",
                    "source": SOURCE,
                    "url": url,
                    "doorAt": None,
                    "startAt": f"{ds}T{start}:00+09:00" if start else None,
                    "endAt": f"{ds}T{finish}:00+09:00" if finish else None,
                    "endTimeType": "公式予定表の利用終了時刻" if finish else None,
                }
            )
        return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    response = session.get(ORIGIN + "/robots.txt", timeout=15)
    if response.status_code != 404:
        response.raise_for_status()
    robot = RobotFileParser()
    robot.parse([] if response.status_code == 404 else response.text.splitlines())

    def read(url):
        if not robot.can_fetch(AGENT, url):
            raise PermissionError(f"取得が許可されていません: {url}")
        response = session.get(url, timeout=20)
        response.raise_for_status()
        if not response.url.startswith(ORIGIN + "/") or len(response.content) > 3_000_000:
            raise ValueError("公式サイトの応答が想定外です")
        return response.content

    links = schedule_links(read(ORIGIN + "/"), today)
    rows = []
    for (year, month), url in sorted(links.items()):
        rows.extend(read_rows(read(url), year, month, url, today))
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
