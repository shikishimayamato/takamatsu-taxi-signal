#!/usr/bin/env python3
"""玉藻公園の公式大会日程を既存の events.json に統合する。"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser


import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = 'http://www.takamatsujyo.com'
SCHEDULE = ORIGIN + '/yotei.htm'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '玉藻公園'
SOURCE = '公式・玉藻公園年間行事予定'
PREFIX = 'tamamo-park-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DATE_RANGE = re.compile(r'(\d{1,2})月(\d{1,2})日(?:(?:~|～|〜)(?:(\d{1,2})月)?(\d{1,2})日)?')


def norm(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def parse_schedule(body: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(body, 'html.parser')
    heading = re.compile(r'玉藻公園主な年間行事予定\(令和(\d+)年度\)')
    tables = [(table, heading.fullmatch(norm(first.get_text())))
              for table in soup.find_all('table')
              if (first := table.find('td')) is not None]
    matches = [(table, match) for table, match in tables if match]
    if len(matches) != 1:
        raise ValueError('玉藻公園の年間行事予定表を確認できません')
    table, match = matches[0]
    fiscal = 2018 + int(match[1])
    if fiscal not in {today.year, today.year - 1} or today > date(fiscal + 1, 3, 31):
        raise ValueError('年間行事予定表が更新されていません')
    events = []
    exact_dates = 0
    for row in table.find_all('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 3:
            continue
        published, title, location = (' '.join(cell.stripped_strings) for cell in cells)
        dates = re.sub(r'\([月火水木金土日祝・振休]+\)', '', norm(published))
        match = DATE_RANGE.fullmatch(dates)
        if not match or not title:
            continue
        month, day = int(match[1]), int(match[2])
        # 春の桜見物は年度表の冒頭に、年始行事は末尾に掲載される。
        start = date(fiscal + (month <= 2 or (month == 3 and exact_dates > 0)), month, day)
        end_month = int(match[3] or month)
        end = date(start.year + (end_month < month), end_month, int(match[4])) if match[4] else start
        exact_dates += 1
        if end < start:
            raise ValueError(f'開催日と終了日の順序を確認してください: {title}')
        if end < today or re.search(r'中止|延期|未定', title):
            continue
        ds = start.isoformat()
        key = hashlib.sha256(f'{ds}|{norm(title)}'.encode()).hexdigest()[:16]
        events.append({
            'id': PREFIX + key, 'date': ds, 'endDate': end.isoformat() if end != start else None,
            'title': unicodedata.normalize('NFKC', title), 'venue': VENUE, 'venueArea': '玉藻町',
            'zone': 'station', 'source': SOURCE, 'url': SCHEDULE,
            'doorAt': None, 'startAt': None, 'endAt': None, 'endTimeType': None,
            'note': f'園内会場・備考（公式年間行事予定）: {location}' if location else '玉藻公園公式年間行事予定。',
        })
    if not exact_dates:
        raise ValueError('年間行事予定表から開催日を確認できません')
    return events


def read_official(url: str, limit: int, *, missing_ok: bool = False) -> bytes | None:
    try:
        response = subprocess.run(
            ['curl', '--silent', '--show-error', '--max-time', '12', '--max-filesize',
             str(limit), '--user-agent', AGENT, '--write-out', '%{http_code}', url],
            capture_output=True, check=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'玉藻公園の公式サイトに接続できません: {url}') from exc
    body, status = response[:-3], response[-3:]
    if status == b'404' and missing_ok:
        return None
    if status != b'200' or len(body) > limit:
        raise ValueError(f'公式サイトの応答を確認してください: {url}')
    return body


def fetch(session: requests.Session, today: date) -> list[dict]:
    body = read_official(ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    lines = body.decode('utf-8-sig').splitlines() if body is not None else []
    for line in lines:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser = RobotFileParser()
    parser.parse(lines)
    if not parser.can_fetch(AGENT, SCHEDULE):
        raise PermissionError('robots.txtが年間行事予定表の取得を拒否')
    return parse_schedule(read_official(SCHEDULE, 1_000_000), today)


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
