#!/usr/bin/env python3
"""ラウンドワン高松店の公式一覧から主な競技会を既存データに統合する。"""
from __future__ import annotations

import json
import re
import ssl
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://rmc.round1.co.jp'
SCHEDULE = ORIGIN + '/contest/schedule_list.php?all=1&shop_id=69'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'ラウンドワンスタジアム高松店'
SOURCE = '公式・ラウンドワン高松店'
PREFIX = 'round1-takamatsu-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, 'html.parser')
    header = soup.find(string=re.compile(r'全競技会日程\s*[-－―]\s*高松店'))
    if not header:
        raise ValueError('高松店の競技会一覧を確認できません')
    rows = {}
    for tr in soup.select('tr'):
        cells = tr.find_all('td', recursive=False)
        if len(cells) < 5:
            continue
        stamp = unicodedata.normalize('NFKC', cells[0].get_text(' ', strip=True))
        match = re.fullmatch(r'(\d{1,2})/(\d{1,2})\s*\(([月火水木金土日])\)\s*((?:[01]?\d|2[0-3]):[0-5]\d)', stamp)
        if not match:
            continue
        title = cells[1].get_text(' ', strip=True)
        if not re.search(r'フェス|大会|選手権|チャンピオンシップ|グランドチャンピオン|プロ.*チャレンジ|ROUND1 LIVE',title,re.I):
            continue
        if re.search(r'限定|健康クラブ|練習会|中止|延期',title):
            continue
        detail = cells[-1].find('a', href=True)
        href = detail['href'] if detail else ''
        identifier = re.fullmatch(r'schedule_detail\.php\?contest_id=(\d+)&tournament_seq=\d+&shop_id=69&all=1', href)
        if not identifier:
            raise ValueError(f'高松店の競技会詳細リンクを確認できません: {title}')
        month, day = int(match[1]), int(match[2])
        for year in (today.year, today.year+1):
            try:
                when = date(year, month, day)
            except ValueError:
                continue
            if not today <= when <= today+timedelta(days=75) or '月火水木金土日'[when.weekday()] != match[3]:
                continue
            ds = when.isoformat()
            hour, minute = match[4].split(':')
            key = PREFIX+identifier[1]
            rows[key] = {
                'id':key, 'title':title, 'date':ds, 'venue':VENUE, 'venueArea':'松島町',
                'zone':'other', 'source':SOURCE, 'url':ORIGIN+'/contest/'+href,
                'doorAt':None, 'startAt':f'{ds}T{int(hour):02d}:{minute}:00+09:00',
                'endAt':None, 'note':'店舗公式の競技会開始時刻。終了時刻は一覧に掲載されていません。',
            }
    return list(rows.values())

class StoreScheduleAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        # Prefer modern ECDHE suites over the server's legacy DH parameters.
        context = ssl.create_default_context()
        context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20')
        return super().init_poolmanager(*args, ssl_context=context, **kwargs)


def fetch(session: requests.Session, today: date) -> list[dict]:
    session.mount(ORIGIN + '/', StoreScheduleAdapter())
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




