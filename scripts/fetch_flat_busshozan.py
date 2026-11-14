#!/usr/bin/env python3
"""ふらっと仏生山の公式サイトから単日の催事を取得する。"""
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

ORIGIN = 'https://www.busshozan-kc.com'
SCHEDULE = ORIGIN + '/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'ふらっと仏生山'
SOURCE = '公式・ふらっと仏生山'
PREFIX = 'flat-busshozan-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日'
DATE_SPAN = re.compile(DAY + r'(?:\s*[~〜～]\s*' + DAY + r')?')
CLOCK = r'(?:[01]?\d|2[0-3]):[0-5]\d'
TIME_SPAN = re.compile(f'({CLOCK})(?:\\s*[~〜～]\\s*({CLOCK})?)?')
DETAIL_PATH = re.compile(re.escape(ORIGIN) + r'/event/event-(\d+)/')
LIST_PATH = re.compile(re.escape(SCHEDULE) + r'(?:page/[1-9]\d*/)?')
CANCELLED = re.compile(r'開催(?:を)?(?:中止|延期)|中止(?:となりました|のお知らせ)|延期(?:となりました|のお知らせ)')


def normalized(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def date_span(value: str) -> tuple[date, date]:
    match = DATE_SPAN.fullmatch(normalized(value))
    if not match:
        raise ValueError(f'開催日を読み取れません: {value}')
    start = date(*map(int, match.groups()[:3]))
    end = date(*map(int, match.groups()[3:])) if match[4] else start
    if end < start:
        raise ValueError(f'開催日の順序が不正です: {value}')
    return start, end


def parse_detail(html: bytes, url: str, expected_day: date) -> dict | None:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.select_one('h1 .jp')
    fields = {}
    for row in soup.select('main table tr'):
        label = row.find('th', recursive=False)
        value = row.find('td', recursive=False)
        if label is not None and value is not None:
            fields[label.get_text(strip=True)] = value.get_text(' ', strip=True)
    if heading is None or '開催日' not in fields:
        raise ValueError(f'催事詳細を確認できません: {url}')
    title = heading.get_text(' ', strip=True)
    notice = ' '.join(fields.get(key, '') for key in ('その他', '申し込み方法'))
    if '中止' in title or '延期' in title or CANCELLED.search(notice):
        return None
    start_day, end_day = date_span(fields['開催日'])
    if start_day != end_day or start_day != expected_day:
        raise ValueError(f'一覧と詳細の開催日が一致しません: {url}')

    day = start_day.isoformat()
    start = end = None
    match = TIME_SPAN.fullmatch(normalized(fields.get('開催時間', '')))
    if match:
        start = match[1].zfill(5)
        if match[2] and match[2].zfill(5) > start:
            end = match[2].zfill(5)
    return {
        'id': PREFIX + DETAIL_PATH.fullmatch(url)[1],
        'date': day, 'title': title, 'venue': VENUE, 'venueArea': '仏生山町',
        'zone': 'other', 'source': SOURCE, 'url': url, 'doorAt': None,
        'startAt': f'{day}T{start}:00+09:00' if start else None,
        'endAt': f'{day}T{end}:00+09:00' if end else None,
        'endTimeType': '公式掲載の開催時間終了' if end else None,
        'note': '出典：高松市仏生山交流センター公式サイト。会場内の場所・参加方法は公式詳細参照。',
    }


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots is not None else []
    parser = RobotFileParser()
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)

    def read(url: str) -> bytes:
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    schedules = set()
    details = {}
    url = SCHEDULE
    while url:
        if not LIST_PATH.fullmatch(url) or url in schedules or len(schedules) >= 8:
            raise ValueError(f'催事一覧のページ送りを確認できません: {url}')
        schedules.add(url)
        soup = BeautifulSoup(read(url), 'html.parser', from_encoding='utf-8')
        cards = soup.select('main ul.event_list > li > a[href]')
        if not cards:
            raise ValueError('ふらっと仏生山の公式イベント一覧を確認できません')
        for card in cards:
            link = urljoin(url, card['href'])
            label = card.select_one('.date')
            title = card.select_one('.title')
            if not DETAIL_PATH.fullmatch(link) or label is None or title is None:
                raise ValueError('催事一覧の項目形式が変わりました')
            if re.search(r'中止|延期', title.get_text(strip=True)):
                continue
            start, end = date_span(label.get_text(' ', strip=True))
            if start == end and today <= start <= today + timedelta(days=120):
                if details.setdefault(link, start) != start:
                    raise ValueError(f'同じ催事に異なる日付が掲載されています: {link}')
        next_page = soup.select_one('a[rel=next][href], a.next[href], a.nextpostslink[href]')
        url = urljoin(url, next_page['href']) if next_page else None

    rows = []
    for link, day in details.items():
        row = parse_detail(read(link), link, day)
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
