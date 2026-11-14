#!/usr/bin/env python3
"""高松シンボルタワー 展示場の公式サイトから公開催事を取得する。"""
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

ORIGIN = 'https://www.symboltower.com'
SCHEDULE = ORIGIN + '/event/exhibition/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松シンボルタワー 展示場'
SOURCE = '公式・高松シンボルタワー展示場'
PREFIX = 'symboltower-exhibition-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
CLOCK = r'(?:[01]?\d|2[0-3]):[0-5]\d'
TIME = re.compile(f'({CLOCK})(?:[~〜～]({CLOCK})?)?')
DETAIL = re.compile(re.escape(SCHEDULE) + r'entry-(\d+)\.html')


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def parse_page(html, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    listing = soup.select_one('ul.event--list')
    if listing is None:
        if re.search(r'イベント(?:は|が)(?:ありません|ございません)', soup.get_text(' ', strip=True)):
            return []
        raise ValueError('展示場の公式催事欄を確認できません')
    rows = []
    for card in listing.select('li.event--item'):
        title = card.select_one('.event--title').get_text(' ', strip=True)
        place = card.select_one('td.place').get_text(' ', strip=True)
        category = card.select_one('.event--category').get_text(' ', strip=True)
        if (normalized(place) != '展示場(ホール棟1F)' or category == '関係者のみ'
                or re.search(r'中止|延期', title)):
            continue
        year, month = map(int, card.select_one('.event--yearAndMonth').get_text(strip=True).split('.'))
        day = date(year, month, int(card.select_one('.event--day').get_text(strip=True)))
        weekday = normalized(card.select_one('.event--week').get_text(strip=True))
        if weekday != f"({'月火水木金土日'[day.weekday()]})":
            raise ValueError('開催日と曜日が一致しません')
        if not today <= day <= today + timedelta(days=120):
            continue
        url = urljoin(SCHEDULE, card.select_one('a[href]')['href'])
        match = DETAIL.fullmatch(url)
        if not match:
            raise ValueError('展示場の公式詳細URLを確認できません')
        start = end = None
        for tr in card.select('.event--detail tr'):
            label, value = tr.find('th'), tr.find('td')
            if label is not None and value is not None and label.get_text(strip=True) == '時間':
                hours = TIME.fullmatch(normalized(value.get_text()).replace(' ', ''))
                if hours:
                    start = hours[1].zfill(5)
                    end = hours[2].zfill(5) if hours[2] else None
                    if end and end <= start:
                        end = None
                break
        ds = day.isoformat()
        rows.append({
            'id': PREFIX + match[1], 'date': ds, 'title': title, 'venue': VENUE,
            'venueArea': 'サンポート', 'zone': 'station', 'source': SOURCE,
            'url': url, 'doorAt': None,
            'startAt': f'{ds}T{start}:00+09:00' if start else None,
            'endAt': f'{ds}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の開催時間終了' if end else None,
            'note': '出典：高松シンボルタワー公式イベント案内。参加条件は公式詳細参照。',
        })
    return rows


def fetch(session, today):
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
    parser = RobotFileParser()
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)

    def read(url):
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    first = read(SCHEDULE)
    rows = {row['id']: row for row in parse_page(first, today)}
    soup = BeautifulSoup(first, 'html.parser', from_encoding='utf-8')
    months = {}
    pattern = re.compile(re.escape(SCHEDULE) + r'field/event-period/(20\d\d)-(0[1-9]|1[0-2])/')
    for link in soup.select('.schedules--month a[href], .schedules--nextYear a[href]'):
        url = urljoin(SCHEDULE, link['href'])
        m = pattern.fullmatch(url)
        if m and today.replace(day=1) <= date(int(m[1]), int(m[2]), 1) <= today + timedelta(days=120):
            months[url] = None
    for url in months:
        rows.update({row['id']: row for row in parse_page(read(url), today)})
    return list(rows.values())


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
