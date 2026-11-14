#!/usr/bin/env python3
"""高松市塩江美術館の公式サイトから単日の催事を取得する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://www.city.takamatsu.kagawa.jp'
ROOT = ORIGIN + '/museum/shionoe/'
SCHEDULE = ROOT + 'index.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data/events.json'
VENUE = '高松市塩江美術館'
SOURCE = '公式・高松市塩江美術館'
PREFIX = 'shionoe-museum-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
DAY = r'(?:(20\d\d)年)?(\d{1,2})月(\d{1,2})日\([月火水木金土日]曜日(?:・祝日?)?\)'
CLOCK = r'(午前|午後)(\d{1,2})時(?:(\d{1,2})分)?'
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|^(?:【|\[)?(?:中止|延期)')


def compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def clock(parts: tuple) -> str:
    period, hour, minute = parts
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise ValueError('公式時刻の形式が異なります')
    return f'{hour % 12 + (12 if period == "午後" else 0):02d}:{minute:02d}'


def read_cards(html: bytes, today: date):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    cards = soup.select('.slid_box')
    if not cards:
        raise ValueError('塩江美術館の展覧会／イベント欄を確認できません')
    for card in cards:
        text = card.select_one('.slid-txt')
        link = card.select_one('a[href]')
        if text is None or link is None:
            continue
        lines = list(text.stripped_strings)
        for i, line in enumerate(lines):
            match = re.fullmatch(DAY, compact(line))
            if match and match[1]:
                break
        else:
            continue
        first = date(*map(int, match.groups()))
        last = first
        if i + 1 < len(lines):
            end = re.fullmatch(r'[~〜～]' + DAY, compact(lines[i + 1]))
            if not end:
                continue
            last = date(int(end[1]) if end[1] else first.year + (int(end[2]) < first.month),
                        int(end[2]), int(end[3]))
        title = ' '.join(lines[:i])
        url = urljoin(SCHEDULE, link['href'])
        if (not title or CANCEL.search(title)
                or not re.fullmatch(re.escape(ROOT) + r'event/[\w/-]+\.html', url)
                or last < first or last < today or first > today + timedelta(days=120)):
            continue
        yield url, title, first, last


def detail_row(html: bytes, url: str, title: str, first: date, last: date) -> dict | None:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    main = soup.select_one('#main-nosub, #main')
    if main is None or main.find('h1') is None:
        raise ValueError(f'公式詳細を確認できません: {url}')
    lines = [compact(line) for line in main.stripped_strings]
    if any(CANCEL.search(line) for line in lines):
        return None
    fields = []
    for paragraph in main.find_all('p'):
        for fragment in paragraph.get_text('\n', strip=True).splitlines():
            fields.append(compact(fragment))
    start = end = None
    for i, field in enumerate(fields):
        label = re.match(r'^(開館時間|時間):?(.*)$', field)
        if not label:
            continue
        value = label[2].lstrip(':') or next(
            (item.lstrip(':') for item in fields[i + 1:i + 3] if item.strip(':')), '',
        )
        hours = re.fullmatch(CLOCK + r'[~〜～]' + CLOCK + r'(?:\(入室は[^()]*まで\))?', value)
        if hours:
            start = clock(hours.groups()[:3])
            end = clock(hours.groups()[3:])
            if end <= start:
                end = None
            break
    row = {
        'id': PREFIX + url.rsplit('/', 1)[-1].removesuffix('.html'),
        'date': first.isoformat(), 'title': title, 'venue': VENUE,
        'venueArea': '塩江町安原上', 'zone': 'other', 'source': SOURCE,
        'url': url, 'doorAt': None,
        'startAt': f'{first.isoformat()}T{start}:00+09:00' if start else None,
        'endAt': f'{last.isoformat()}T{end}:00+09:00' if end else None,
        'endTimeType': '公式掲載の開催時間終了' if end else None,
        'note': '出典：高松市塩江美術館公式サイト。会期中の休館日・入室締切は公式詳細参照。',
    }
    if first != last:
        row['endDate'] = last.isoformat()
    return row


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)

    def read(url: str) -> bytes:
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    rows = []
    for url, title, first, last in read_cards(read(SCHEDULE), today):
        row = detail_row(read(url), url, title, first, last)
        if row is not None:
            rows.append(row)
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
