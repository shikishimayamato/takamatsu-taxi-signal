#!/usr/bin/env python3
"""高松市石の民俗資料館の公式サイトから展示予定を取得する。"""
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

import requests
from bs4 import BeautifulSoup

from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://www.city.takamatsu.kagawa.jp'
ROOT = ORIGIN + '/smph/kurashi/kosodate/bunka/ishimin/'
SCHEDULE = ROOT + 'tenjiyotei.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松市石の民俗資料館'
SOURCE = '公式・高松市石の民俗資料館'
PREFIX = 'takamatsu-stone-museum-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(\d{1,2})月(\d{1,2})日\(([月火水木金土日])曜日(?:・祝日?)?\)'
PERIOD = re.compile(DAY + 'から' + DAY + 'まで')


def normalized(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def day_in_year(parts: tuple, fiscal_year: int) -> date:
    month, day, weekday = parts
    month = int(month)
    result = date(fiscal_year + (month <= 3), month, int(day))
    if '月火水木金土日'[result.weekday()] != weekday:
        raise ValueError('展示日程の曜日と日付が一致しません')
    return result


def parse_schedule(html: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = next((tag for tag in soup.select('#contents-area h2')
                    if re.fullmatch(r'令和\d+年度展示予定', normalized(tag.get_text()))), None)
    if heading is None:
        raise ValueError('資料館の展示予定年度を確認できません')
    fiscal_year = 2018 + int(re.search(r'\d+', normalized(heading.get_text()))[0])
    table = heading.find_next('table')
    if table is None or not {'期間', '内容'} <= {tag.get_text(strip=True) for tag in table.select('th')}:
        raise ValueError('資料館の展示日程表を確認できません')
    rows = []
    recognized = 0
    for tr in table.select('tr'):
        cells = tr.find_all('td', recursive=False)
        if len(cells) != 2:
            continue
        title = ' '.join(cells[0].get_text(' ', strip=True).split())
        if re.search(r'中止|延期|未定', title):
            continue
        span = PERIOD.fullmatch(normalized(cells[1].get_text(strip=True)))
        if span is None:
            continue
        first = day_in_year(span.groups()[:3], fiscal_year)
        last = day_in_year(span.groups()[3:], fiscal_year)
        if last < first:
            raise ValueError('会期の終了日が開始日より前です')
        recognized += 1
        if last < today or first > today + timedelta(days=150):
            continue
        event_id = hashlib.sha256(f'{fiscal_year}|{normalized(title)}'.encode()).hexdigest()[:16]
        rows.append({
            'id': PREFIX + event_id, 'title': title, 'date': first.isoformat(),
            'endDate': last.isoformat(), 'venue': VENUE, 'venueArea': '牟礼町牟礼',
            'zone': 'other', 'source': SOURCE, 'url': SCHEDULE, 'doorAt': None,
            'startAt': None, 'endAt': None, 'endTimeType': None,
            'note': '出典：高松市公式の展示予定。開館時間・休館日・予定変更は公式案内を参照。',
        })
    if not recognized:
        raise ValueError('開催日が記載された展示を確認できません')
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
    parser = RobotFileParser()
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)
    if not parser.can_fetch(AGENT, SCHEDULE):
        raise PermissionError('robots.txtが展示予定の取得を拒否')
    return parse_schedule(get(session, SCHEDULE, 4_000_000), today)


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
        rows = [row for row in payload['events'] if owned(row) and (row.get('endDate') or row['date']) >= now.date().isoformat()]
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
                rows = [row for row in payload['events'] if owned(row) and (row.get('endDate') or row['date']) >= now.date().isoformat()]
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
