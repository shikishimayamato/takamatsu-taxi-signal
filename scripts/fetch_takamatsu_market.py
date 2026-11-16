#!/usr/bin/env python3
"""高松市中央卸売市場の公式特別開放日程を既存データに統合する。"""
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

ORIGIN = 'https://www.city.takamatsu.kagawa.jp'
SCHEDULE = ORIGIN + '/smph/kurashi/kurashi/shisetsu/chuoichiba/kaihou.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松市中央卸売市場'
SOURCE = '公式・高松市中央卸売市場'
PREFIX = 'takamatsu-market-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DATE = re.compile(r'令和(元|\d+)年\s*(\d{1,2})月\s*(\d{1,2})日')
CLOCK = re.compile(r'(午前|午後)\s*(\d{1,2})時(?:\s*(\d{1,2})分)?')


def section(soup: BeautifulSoup, label: str) -> list[str]:
    heading = next((h for h in soup.select('.h3bg h3') if h.get_text(strip=True) == label), None)
    if heading is None:
        return []
    content = heading.find_parent(class_='h3bg').find_next_sibling()
    if content is None or 'smph-mt' not in content.get('class', []):
        raise ValueError(f'公式ページの{label}欄を確認できません')
    return [unicodedata.normalize('NFKC', value) for value in content.stripped_strings]


def clocks(text: str) -> list[str]:
    result = []
    for period, hour, minute in CLOCK.findall(text):
        h, m = int(hour), int(minute or 0)
        if not 0 <= h <= 12 or not 0 <= m < 60:
            raise ValueError('公式ページの時刻が想定外です')
        result.append(f'{h%12+(12 if period=="午後" else 0):02d}:{m:02d}')
    return result


def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, 'html.parser')
    heading = soup.select_one('h1.index_omidashi')
    if not heading or '市場de特別開放' not in heading.get_text(strip=True):
        raise ValueError('市場de特別開放の公式ページを確認できません')
    current = ' '.join(section(soup, '日時'))
    future = section(soup, '今後の開催予定')
    if len(DATE.findall(current)) != 1:
        raise ValueError('直近の特別開放の開催日を確認できません')
    rows = {}
    for entry, announced in [(line, False) for line in future]+[(current, True)]:
        if '中止' in entry or '延期' in entry:
            continue
        times = clocks(entry) if announced else []
        for era, month, day in DATE.findall(entry):
            when = date(2018+(1 if era=='元' else int(era)),int(month),int(day))
            if not today <= when <= today+timedelta(days=180):
                continue
            ds = when.isoformat()
            start = times[0] if times else None
            finish = times[1] if len(times)==2 and times[1]>times[0] and re.search(r'[~〜～]',entry) else None
            note = '販売は売り切れ次第終了。' if '売り切れ次第終了' in entry else ''
            if not announced:
                note = '公式ページの今後の開催予定。開催時刻は未発表です。'
            rows[ds] = {
                'id':PREFIX+ds, 'title':'市場de特別開放', 'date':ds, 'venue':VENUE,
                'venueArea':'瀬戸内町', 'zone':'other', 'source':SOURCE, 'url':SCHEDULE,
                'doorAt':None, 'startAt':f'{ds}T{start}:00+09:00' if start else None,
                'endAt':f'{ds}T{finish}:00+09:00' if finish else None,
                'endTimeType':'公式掲載の終了時刻' if finish else None, 'note':note,
            }
    return list(rows.values())

def fetch(session: requests.Session, today: date) -> list[dict]:
    body = get(session, ORIGIN+'/robots.txt', 500_000, missing_ok=True)
    robot = RobotFileParser()
    robot.parse(body.decode('utf-8-sig').splitlines() if body else [])
    if not robot.can_fetch(AGENT, SCHEDULE):
        raise PermissionError(f'取得が許可されていません: {SCHEDULE}')
    return parse_schedule(get(session, SCHEDULE, 2_000_000), today)

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




