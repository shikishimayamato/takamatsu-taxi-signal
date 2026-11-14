#!/usr/bin/env python3
"""香川県営テニス場の公式大会日程を既存の events.json に統合する。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

from calendar import monthrange
from urllib.parse import urljoin

import pymupdf
import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://www.ikushima-amz.com'
SCHEDULE = ORIGIN + '/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '香川県営テニス場'
SOURCE = '公式・香川県総合運動公園'
PREFIX = 'ikushima-tennis-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
SPORT = re.compile(r'大会|選手権|リーグ|試合|杯|高校新人|交流戦|決定戦|フェスティバル|トーナメント|A[-—–]StepS')
SKIP = re.compile(r'\(予\)|予備日|中止|延期|準備|練習|教室|設営|撤収|工事')
WEEK = '月火水木金土日'


def norm(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def parse_pdf(body: bytes, fiscal: int, url: str, today: date) -> list[dict]:
    rows = []
    seen_months = set()
    with pymupdf.open(stream=body, filetype='pdf') as doc:
        if len(doc) > 12:
            raise ValueError('年間予定表のページ数を確認してください')
        for page in doc:
            heading = re.search(r'(\d{1,2})月予定表\((20\d{2})年度\)',
                                re.sub(r'\s+', '', norm(page.get_text())))
            if not heading or int(heading[2]) != fiscal:
                raise ValueError('年間予定表の対象年度・月を確認できません')
            month = int(heading[1])
            year = fiscal + (month < 4)
            first = date(year, month, 1)
            last = date(year, month, monthrange(year, month)[1])
            if first in seen_months:
                raise ValueError('年間予定表に重複月があります')
            seen_months.add(first)
            if last < today or first > today + timedelta(days=120):
                continue
            # 2.xの自動レイアウト判定は、このPDFの罫線表を見落とす。
            try:
                tables = page.find_tables(use_layout=False).tables
            except TypeError as exc:
                if 'use_layout' not in str(exc):
                    raise
                tables = page.find_tables().tables
            for table in tables:
                cells = table.extract()
                if not cells:
                    continue
                field = next((index for index, name in enumerate(cells[0])
                              if norm(name or '').startswith('県営テニス場')), None)
                if field is not None:
                    break
            else:
                raise ValueError(f'{month}月の県営テニス場欄が確認できません')
            seen_days = set()
            for row in cells[1:]:
                number, weekday = row[:2]
                cell = row[field]
                if not (number or '').strip().isdecimal():
                    continue
                day = first.replace(day=int(number))
                if day in seen_days or weekday != WEEK[day.weekday()]:
                    raise ValueError(f'{month}月の行事表の日付と曜日が一致しません')
                seen_days.add(day)
                if not today <= day <= today + timedelta(days=120):
                    continue
                # 丸数字は使用コート数。改行または丸数字で区切られた大会を個別に扱う。
                for entry in re.split(r'[①-⑳㉑-㉟㊱-㊿]+|\n', cell or ''):
                    title = norm(entry)
                    if not SPORT.search(norm(title)) or SKIP.search(norm(title)):
                        continue
                    ds = day.isoformat()
                    key = hashlib.sha256(f'{ds}|{norm(title)}'.encode()).hexdigest()[:16]
                    rows.append({
                        'id': PREFIX + key, 'date': ds, 'title': title,
                        'venue': VENUE, 'venueArea': '生島町', 'zone': 'other',
                        'source': SOURCE, 'url': f'{url}#page={page.number + 1}',
                        'doorAt': None, 'startAt': None, 'endAt': None, 'endTimeType': None,
                        'note': '香川県総合運動公園の年間予定表。大会時刻・観戦可否は主催者案内を参照。',
                    })
            if len(seen_days) != last.day:
                raise ValueError(f'{month}月の県営テニス場欄の日付が不足しています')
    if len(seen_months) != 12:
        raise ValueError('年間予定表の全12か月を確認できません')
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    rules = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    lines = rules.decode('utf-8-sig').splitlines() if rules is not None else []
    for line in lines:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(lines)

    def read(url: str) -> bytes:
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    soup = BeautifulSoup(read(SCHEDULE), 'html.parser', from_encoding='utf-8')
    fiscal = today.year - (today.month < 4)
    schedules = {}
    for link in soup.select('a[href]'):
        match = re.match(r'(20\d{2})年度\s*年間行事予定表', norm(link.get_text()))
        if not match or int(match[1]) not in {fiscal, fiscal + 1}:
            continue
        year = int(match[1])
        url = urljoin(SCHEDULE, link['href'])
        if not re.fullmatch(re.escape(ORIGIN) + rf'/event/schedule{year}(?:[-_][\w-]+)?\.pdf', url):
            raise ValueError('年間予定表のURL形式が変わりました')
        if year in schedules and schedules[year] != url:
            raise ValueError('同じ年度の年間予定表が複数あります')
        schedules[year] = url
    if fiscal not in schedules:
        raise ValueError('今年度の年間予定表が確認できません')
    return [row for year, url in schedules.items() if date(year, 4, 1) <= today + timedelta(days=120)
            for row in parse_pdf(read(url), year, url, today)]


def owned(event: dict) -> bool:
    return (event.get('venue') == VENUE and event.get('source') == SOURCE
            and str(event.get('id', '')).startswith(PREFIX)
            and event.get('sourceType') not in {'manual', 'admin'})


def previous_rows(session: requests.Session, now: datetime) -> tuple[list[dict], str | None]:
    try:
        payload = json.loads(get(session, PUBLISHED, 2_000_000))
        source = next(s for s in payload['sources'] if s.get('venue') == VENUE)
        last_ok = datetime.fromisoformat(source['lastSuccessAt'])
        if not timedelta(0) <= now - last_ok < timedelta(days=7):
            return [], None
        rows = [row for row in payload['events'] if owned(row) and row['date'] >= now.date().isoformat()]
        return rows, source['lastSuccessAt']
    except (requests.RequestException, ValueError, TypeError, KeyError, StopIteration) as exc:
        print(f'{VENUE}の前回データを利用できません: {type(exc).__name__}', file=sys.stderr)
        return [], None


def merge(payload: dict, rows: list[dict], status: str, now: datetime, last_success: str | None = None) -> dict:
    if payload.get('schema') != 1 or not isinstance(payload.get('events'), list):
        raise ValueError('既存events.jsonの形式が異なります')
    old_sources = payload.get('sources', [])
    current = next((source for source in old_sources if source.get('venue') == VENUE), {})
    sources = [source for source in old_sources if source.get('venue') != VENUE]
    checked = now.isoformat(timespec='seconds')
    events = [event for event in payload['events'] if not owned(event)]
    protected = {event.get('id') for event in events}
    official = {row['id']: row for row in rows if row['id'] not in protected}
    events.extend(official.values())
    events.sort(key=lambda e: (e.get('date', ''), e.get('venue', ''), e.get('title', '')))
    source = {'venue': VENUE, 'status': status, 'count': len(official), 'checkedAt': checked}
    if status == 'ok':
        source['lastSuccessAt'] = checked
    elif last_success or current.get('lastSuccessAt'):
        source['lastSuccessAt'] = last_success or current['lastSuccessAt']
    partial = any(s.get('status') in {'partial', 'stale', 'error'} for s in [*sources, source])
    result = dict(payload, events=events, sources=[*sources, source], partial=partial)
    if official and VENUE not in result.get('sourceName', ''):
        result['sourceName'] = result.get('sourceName', '複数公式サイト') + '・' + VENUE
    return result


def main() -> int:
    now = datetime.now(JST)
    try:
        payload = json.loads(OUTPUT.read_text(encoding='utf-8'))
        last_success = None
        with requests.Session() as session:
            session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'ja-JP,ja;q=0.9'})
            try:
                rows = fetch(session, now.date())
                status = 'ok'
            except (requests.RequestException, ValueError, PermissionError) as exc:
                print(f'{VENUE}取得警告: {exc}', file=sys.stderr)
                rows = [row for row in payload['events'] if owned(row) and row['date'] >= now.date().isoformat()]
                if not rows:
                    rows, last_success = previous_rows(session, now)
                status = 'stale' if rows else 'error'
        result = merge(payload, rows, status, now, last_success)
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', dir=OUTPUT.parent, encoding='utf-8',
                                    newline='\n', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            temporary.replace(OUTPUT)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f'{VENUE}統合完了: {result["sources"][-1]["count"]}件 / {status} / 全施設合計 {len(result["events"])}件')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'{VENUE}統合警告: {exc}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
