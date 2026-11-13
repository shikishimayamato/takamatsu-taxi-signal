#!/usr/bin/env python3
"""高松MONSTERの公式月間公演表を催事データに統合する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from fetch_kagawa_bunkakaikan import get

# 会場が案内するHTTPの公開ページ。認証情報は使用しない。
ORIGIN = 'http://www.monster.cx'
VENUE = '高松MONSTER'
SOURCE = '公式・高松MONSTER'
PREFIX = 'takamatsu-monster-'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
CANCEL = re.compile(r'中止|延期|CANCELLED|POSTPONED', re.I)
TIME = r'([0-2]?\d):([0-5]\d)'
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def timestamp(day: date, hour: str, minute: str) -> str | None:
    hour = int(hour)
    if hour > 29:
        return None
    return (datetime(day.year, day.month, day.day, tzinfo=JST)
            + timedelta(hours=hour, minutes=int(minute))).isoformat()


def times(day: date, value: str) -> tuple:
    value = unicodedata.normalize('NFKC', value)
    pair = re.fullmatch(TIME + r'\s*(?:OPEN\s*)?/\s*' + TIME + r'(?:\s*START)?', value, re.I)
    if pair:
        return timestamp(day, *pair.groups()[:2]), timestamp(day, *pair.groups()[2:])
    return None, None


def parse_month(html: bytes, url: str, month: date, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    title = soup.select_one('.schedule_month')
    year = soup.select_one('.schedule_nav .year')
    if (not title or not year or title.get_text(strip=True).lstrip('0') != str(month.month)
            or year.get_text(strip=True) != str(month.year)):
        raise ValueError(f'公式月間表を確認できません: {url}')
    rows = []
    for box in soup.select('.schedule_sub_box'):
        category = box.select_one('.kind_inner')
        if category is None or category.get_text(strip=True) not in {'LIVE', 'CLUB'}:
            continue
        label = box.select_one('.live_info h2 a[href]')
        day_label = box.select_one('.date .day')
        if label is None or day_label is None:
            raise ValueError('公演表の構造が変わりました')
        day = month.replace(day=int(day_label.get_text(strip=True)))
        weekday = box.select_one('.date .week')
        if weekday and weekday.get_text(strip=True) != WEEKDAYS[day.weekday()]:
            raise ValueError(f'公演日と曜日が一致しません: {day}')
        if not today <= day <= today + timedelta(days=120):
            continue
        title = ' '.join(label.get_text(' ', strip=True).split())
        if not title or CANCEL.search(box.select_one('.live_info').get_text(' ', strip=True)):
            continue
        details = urlsplit(urljoin(url, label['href']))
        match = re.fullmatch(r'/archives/(\d+)/?', details.path)
        if details.netloc != urlsplit(ORIGIN).netloc or not match:
            raise ValueError('公演詳細URLの形式が変わりました')
        timing = box.select_one('.open_start .cont')
        door, start = times(day, timing.get_text(' ', strip=True) if timing else '')
        ds = day.isoformat()
        rows.append({
            'id': f'{PREFIX}{match[1]}-{ds}', 'date': ds, 'title': title,
            'venue': VENUE, 'venueArea': '瓦町', 'zone': 'other',
            'source': SOURCE, 'url': ORIGIN + details.path,
            'doorAt': door, 'startAt': start, 'endAt': None, 'endTimeType': None,
            'note': '出典：高松MONSTER公式月間公演表。深夜の24時以降は時刻のみ翌日として記録。',
        })
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    rules = robots.decode('utf-8-sig').splitlines() if robots is not None else []
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)
    month = today.replace(day=1)
    rows = []
    while month <= today + timedelta(days=120):
        url = f'{ORIGIN}/archives/date/{month:%Y/%m}?cat=2'
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        html = get(session, url, 2_000_000, missing_ok=month != today.replace(day=1))
        if html is not None:
            rows.extend(parse_month(html, url, month, today))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return rows


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
