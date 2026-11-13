#!/usr/bin/env python3
"""高松TOONICEの公式公演一覧を催事データに統合する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://impulse-records.main.jp'
SCHEDULE = ORIGIN + '/toonice/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松TOONICE'
SOURCE = '公式・高松TOONICE'
PREFIX = 'takamatsu-toonice-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
TIME = r'((?:[01]?\d|2[0-9])):([0-5]\d)'
SKIP = re.compile(r'中止|延期|CANCELLED|POSTPONED|STUDIO\s*DAY|貸切|非公開|^(?:CLOSED?|店休|休業|HOLD|HALL\s+RENTAL)(?:\s|$)', re.I)


def norm(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def timings(day: date, text: str) -> tuple[str | None, str | None]:
    pair = re.fullmatch(rf'({TIME}|TBA|[-–—]+)\s*/\s*({TIME}|TBA|[-–—]+)', norm(text), re.I)
    if not pair:
        return None, None

    def at(hour: str | None, minute: str | None) -> str | None:
        if hour is None:
            return None
        return (datetime(day.year, day.month, day.day, tzinfo=JST)
                + timedelta(hours=int(hour), minutes=int(minute))).isoformat()

    return at(pair[2], pair[3]), at(pair[5], pair[6])


def parse_page(html: bytes, month: date, page: int, today: date) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    current = soup.select_one('#primary .wp-pagenavi .current')
    if (not re.match(rf'{month.year}\s+0?{month.month}月\s*\|\s*TOONICE', title)
            or current is None or current.get_text(strip=True) != str(page)):
        raise ValueError(f'公式月間一覧を確認できません: {month:%Y-%m} / {page}')
    following = [int(a.get_text(strip=True)) for a in soup.select('#primary .wp-pagenavi a.page')
                 if a.get_text(strip=True).isdigit()]
    last_page = max([page, *following])
    if last_page > 10:
        raise ValueError('公式月間一覧のページ数が上限を超えました')
    rows = []
    for article in soup.select('#scheduleBox article'):
        link = article.select_one('h1 a[href]')
        date_tag = article.select_one('p.date')
        if link is None or date_tag is None:
            raise ValueError('公式公演のタイトルまたは日付欄を確認できません')
        title = ' '.join(link.get_text(' ', strip=True).split())
        if title.startswith('"') and title.endswith('"'):
            title = title[1:-1].strip()
        if not title or SKIP.search(norm(title)):
            continue
        date_match = re.match(r'(20\d{2})\.+(\d{1,2})\.+(\d{1,2})(?!\d)', norm(date_tag.get_text(' ')))
        if not date_match:
            raise ValueError(f'公演日の形式を確認してください: {title}')
        day = date(*map(int, date_match.groups()))
        if not today <= day <= today + timedelta(days=120):
            continue
        target = urlsplit(link['href'])
        post = parse_qs(target.query).get('p', [])
        if (target.scheme, target.netloc, target.path) != ('https', urlsplit(ORIGIN).netloc, '/toonice/') or len(post) != 1 or not post[0].isdigit():
            raise ValueError('公式公演URLの形式が変わりました')
        fields = {}
        for label in article.select('.detail dt'):
            value = label.find_next_sibling('dd')
            if value is not None:
                fields[norm(label.get_text(' '))] = value.get_text(' ', strip=True)
        if re.search(r'公演中止|開催中止|公演延期|開催延期', fields.get('ACT', '')):
            continue
        door, start = timings(day, fields.get('OPEN / START', ''))
        ds = day.isoformat()
        rows.append({
            'id': f'{PREFIX}{post[0]}-{ds}', 'date': ds, 'title': title,
            'venue': VENUE, 'venueArea': '亀井町', 'zone': 'other', 'source': SOURCE,
            'url': f'{SCHEDULE}?p={post[0]}', 'doorAt': door, 'startAt': start,
            'endAt': None, 'endTimeType': None, 'note': '出典：高松TOONICE公式公演一覧。',
        })
    return rows, last_page


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots is not None else []
    parser = RobotFileParser()
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)
    # 月別一覧は掲載月と公演月が異なるため、前月の掲載分も見る。
    month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    rows = []
    while month <= today + timedelta(days=120):
        page, last_page = 1, 1
        while page <= last_page:
            url = f'{SCHEDULE}?cat=9&m={month:%Y%m}'
            if page > 1:
                url += f'&paged={page}'
            if not parser.can_fetch(AGENT, url):
                raise PermissionError(f'robots.txtが取得を拒否: {url}')
            part, seen_last = parse_page(get(session, url, 2_000_000), month, page, today)
            rows.extend(part)
            last_page = max(last_page, seen_last)
            page += 1
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
