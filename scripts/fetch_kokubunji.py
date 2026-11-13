#!/usr/bin/env python3
"""朝日段ボールホール国分寺（旧・高松国分寺ホール）の公式一般催事を収集。

対象: 公式イベント一覧 https://www.kokubunji-hall.jp/event/ にある日付付き催事。
対象外: 貸館・空き状況、個人使用日、ロビーの小規模催事、日付が不明な告知。
開催終了時刻は公式掲載の終了時刻だけを使用し、開演時間から終演時刻を推測しない。
既存の公開データ events.json に当施設分だけ統合する（他施設に手を触れない）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "events.json"
LIST_URL = "https://www.kokubunji-hall.jp/event/"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
VENUE = "朝日段ボールホール国分寺"
SOURCE = "公式・高松国分寺ホール"
JST = ZoneInfo("Asia/Tokyo")
AGENT = "TakamatsuTaxiSignal/1.3 (+public event listings; twice-daily)"

DATE_RX = re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日")
DATE_YMD = re.compile(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
HM = r"((?:[01]?\d|2[0-3]):[0-5]\d)"
RANGE_RX = re.compile(HM + r"\s*[～~〜―－–—-]\s*" + HM)
DOOR_RX = re.compile(r"(?:開場|開門|入場)\s*[:：]?\s*" + HM)
START_RX = re.compile(r"(?:開演|開始|開会|公演開始)\s*[:：]?\s*" + HM)
END_RX = re.compile(r"(?:終演|終了|閉演|閉会)\s*[:：]?\s*" + HM)
SKIP_TITLE = re.compile(r"個人(?:使用|利用)日|利用予約|練習日|施設空き状況|一般開放", re.I)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def get_date(value: str) -> date | None:
    value = clean(value)
    m = DATE_RX.search(value) or DATE_YMD.search(value)
    if m is None:
        return None
    try:
        return date(*map(int, m.groups()))
    except ValueError:
        return None


def time_fields(value: str, day: str) -> dict[str, str | None]:
    """イベント公開時間は二部制なら開始は最初、終了は最後。開演だけでは終演不明。"""
    value = clean(value)
    doors = DOOR_RX.search(value)
    begins = START_RX.search(value)
    ends = END_RX.search(value)
    ranges = list(RANGE_RX.finditer(value))
    door = doors.group(1) if doors else None
    start = begins.group(1) if begins else (ranges[0].group(1) if ranges else None)
    end = ends.group(1) if ends else (ranges[-1].group(2) if ranges else None)
    if start and end and start > end:
        # 23時跨ぎなどの特別時刻は誤って同日終了を記録しない。
        end = None
    dt = lambda hm: f"{day}T{hm}:00+09:00" if hm else None
    return {"doorAt": dt(door), "startAt": dt(start), "endAt": dt(end)}


def event_id(title: str, day: str) -> str:
    key = re.sub(r"\s+", "", clean(title))
    return "kokubunji-" + hashlib.sha256(f"{VENUE}|{day}|{key}".encode()).hexdigest()[:16]


def allowed_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "www.kokubunji-hall.jp"


def parse_html(html: str, now: datetime) -> list[dict]:
    """公開催事の表だけ読む。単に利用予約がある日や期日不明は採用しない。"""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < 4:
                continue
            ds = get_date(cells[0].get_text(" ", strip=True))
            if not ds or not (now.date()-timedelta(days=1) <= ds <= now.date()+timedelta(days=120)):
                continue
            title = clean(cells[1].get_text(" ", strip=True))
            if not (2 <= len(title) <= 120) or SKIP_TITLE.search(title):
                continue
            place = clean(cells[2].get_text(" ", strip=True))
            # ロビーの小規模催事、講座や空き状況を需要集中イベントとして登録しない。
            if not re.search(r"ホール|大ホール|小ホール", place):
                continue
            hours = clean(cells[3].get_text(" ", strip=True))
            ref = cells[1].find("a", href=True)
            url = urljoin(LIST_URL, ref["href"]) if ref else LIST_URL
            if not allowed_detail_url(url):
                url = LIST_URL
            day = ds.isoformat()
            fields = time_fields(hours, day)
            ev = {
                "id": event_id(title, day), "title": title, "date": day,
                "venue": VENUE, "venueArea": "国分寺町新名", "zone": "other",
                "source": SOURCE, "url": url,
                "doorAt": fields["doorAt"], "startAt": fields["startAt"],
                "endAt": fields["endAt"],
                "endTimeType": "公式掲載の開催終了時間" if fields["endAt"] else None,
                "note": "ホールの一般公開催事。" + ("終了時刻は公式掲載の開催時間。" if fields["endAt"] else "終演時刻は未確認。"),
            }
            out.setdefault(ev["id"], ev)
    return sorted(out.values(), key=lambda x: (x["date"], x["title"]))


def official_html() -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": AGENT, "Accept-Language": "ja-JP,ja;q=0.9"})
    parsed = urlparse(LIST_URL)
    robot_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    r = session.get(robot_url, timeout=(10,15))
    if r.status_code == 404:
        rules = []
    elif r.status_code == 200 and len(r.content) <= 500_000:
        rules = r.text.splitlines()
    else:
        raise PermissionError(f"robots.txtの確認ができません: HTTP {r.status_code}")
    robot = RobotFileParser(); robot.parse(rules)
    if not robot.can_fetch(AGENT, LIST_URL):
        raise PermissionError("robots.txtが公式催事ページの取得を許可していません")
    res = session.get(LIST_URL, timeout=(10,25))
    res.raise_for_status()
    if not allowed_detail_url(res.url):
        raise ValueError("別ドメインへのリダイレクトを検出")
    if len(res.content) > 2_000_000:
        raise ValueError("公式ページが2MB超")
    if "html" not in res.headers.get("Content-Type", "").lower():
        raise ValueError("公式ページがHTMLではありません")
    res.encoding = res.apparent_encoding if not re.search(r"charset\s*=", res.headers.get("Content-Type", ""), re.I) else res.encoding
    return res.text


def previous_events(now: datetime) -> list[dict]:
    """サイトが一時的に使えないとき、7日以内の以前の公開記録だけ保持。"""
    try:
        r = requests.get(PUBLISHED, params={"v": now.strftime("%Y%m%d%H%M")},
                         headers={"User-Agent":AGENT},timeout=(8,12))
        r.raise_for_status()
        if len(r.content) > 1_500_000: return []
        old = r.json()
        timestamp = datetime.fromisoformat(old["updatedAt"])
        if timestamp.tzinfo is None: timestamp=timestamp.replace(tzinfo=JST)
        if not (timedelta(0) <= now-timestamp < timedelta(days=7)): return []
        return [e for e in old.get("events",[]) if e.get("venue")==VENUE and e.get("date", "") >= (now.date()-timedelta(days=1)).isoformat()]
    except Exception as exc:
        print("国分寺ホールの過去データ取得不可: "+type(exc).__name__,file=sys.stderr)
        return []


def merge(payload: dict, events: list[dict], status: str, now: datetime) -> dict:
    if payload.get("schema") != 1 or not isinstance(payload.get("events"), list):
        raise ValueError("events.jsonが想定外です。上書きを中止")
    copied = dict(payload)
    copied["events"] = sorted(
        [e for e in copied["events"] if e.get("venue") != VENUE] + events,
        key=lambda e:(e.get("date", ""), e.get("venue", ""), e.get("title", ""))
    )
    sources = [s for s in copied.get("sources", []) if isinstance(s,dict) and s.get("venue")!=VENUE]
    sources.append({"venue":VENUE,"status":status,"count":len(events),"checkedAt":now.isoformat(timespec="seconds")})
    copied["sources"] = sources
    copied["partial"] = any(s.get("status") in {"error","partial","stale"} for s in sources)
    if events and VENUE not in copied.get("sourceName", ""):
        copied["sourceName"] = str(copied.get("sourceName", "複数公式サイト"))+"・"+VENUE
    return copied


def main() -> int:
    now = datetime.now(JST)
    if not OUTPUT.exists():
        print("先にscripts/fetch_events.pyを実行してください。",file=sys.stderr)
        return 1
    payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    try:
        events = parse_html(official_html(),now)
        if not events:
            raise RuntimeError("公式の一覧表から将来催事を抽出できません。サイト構造を要確認")
        status = "ok"
    except Exception as exc:
        print("国分寺ホール取得失敗: "+type(exc).__name__+": "+str(exc)[:250],file=sys.stderr)
        events = previous_events(now)
        status = "stale" if events else "error"
    merged = merge(payload,events,status,now)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"国分寺ホール統合完了: {len(events)}件 / {status} / 全施設合計 {len(merged['events'])}件")
    # 他施設の更新は成功させる。国分寺の取得失敗はログとsourcesに記録。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
