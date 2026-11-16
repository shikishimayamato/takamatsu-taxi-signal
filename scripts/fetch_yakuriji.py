#!/usr/bin/env python3
"""八栗寺の公式年中行事から主な法会を既存データに統合する。"""
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

ORIGIN = 'https://yakuriji.jp'
SCHEDULE = ORIGIN + '/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '八栗寺'
SOURCE = '公式・八栗寺'
PREFIX = 'yakuriji-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, 'html.parser')
    hatsumode = soup.select_one('.event-box-01 .event01')
    services = soup.select_one('.event-box-01 .event02')
    if not hatsumode or not services:
        raise ValueError('公式年中行事の初詣・大般若法会欄を確認できません')
    normal = lambda text: unicodedata.normalize('NFKC', text)
    new_year_text = normal(hatsumode.get_text(' ', strip=True))
    if not re.search(r'1月1日\s*[~〜～]\s*3日', new_year_text) or '開扉' not in new_year_text:
        raise ValueError('初詣の期間を確認できません')
    entries = [('hatsumode','初詣・本尊御開扉',1,1,2,None)]
    if re.search(r'1月1日午前0時には、除夜の鐘', new_year_text):
        entries.append(('bell','除夜の鐘',1,1,0,'00:00'))
    festival = re.compile(r'^○\s*(\d{1,2})/(\d{1,2})\s+((?:[01]?\d|2[0-3]):[0-5]\d)から「(聖天尊(?:正月|春季)大縁日)」$')
    seasons = {'聖天尊正月大縁日':'new-year-service','聖天尊春季大縁日':'spring-service'}
    confirmed = {}
    for line in services.stripped_strings:
        match = festival.fullmatch(normal(line))
        if match:
            hour, minute = match[3].split(':')
            confirmed[match[4]] = (seasons[match[4]],match[4],int(match[1]),int(match[2]),0,f'{int(hour):02d}:{minute}')
    if set(confirmed) != set(seasons):
        raise ValueError('正月・春季大縁日の日付と開始時刻を確認できません')
    entries.extend(confirmed.values())
    rows = []
    for key,title,month,day,duration,start in entries:
        for year in (today.year-1,today.year,today.year+1):
            first = date(year,month,day)
            last = first+timedelta(days=duration)
            if last < today or first > today+timedelta(days=210):
                continue
            ds = first.isoformat()
            note = '乗車の目安は八栗ケーブル登山口駅。終了時刻は公式案内に掲載されていません。'
            if key in {'hatsumode','bell'}:
                note += ' 年末年始の裏参道の車両規制は公式案内をご確認ください。'
            rows.append({
                'id':f'{PREFIX}{key}-{ds}', 'title':title, 'date':ds, 'venue':VENUE,
                'venueArea':'牟礼町牟礼', 'zone':'other', 'source':SOURCE, 'url':SCHEDULE,
                'endDate':last.isoformat() if duration else None, 'doorAt':None,
                'startAt':f'{ds}T{start}:00+09:00' if start else None, 'endAt':None, 'note':note,
            })
    return rows

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



