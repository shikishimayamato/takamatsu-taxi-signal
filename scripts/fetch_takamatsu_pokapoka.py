#!/usr/bin/env python3
"""高松ぽかぽか温泉の公式サイトからアウフグースの催事を取得する。"""
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

ORIGIN = 'https://pokapoka-takamatsu.com'
SCHEDULE = ORIGIN + '/category/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松ぽかぽか温泉'
SOURCE = '公式・高松ぽかぽか温泉'
PREFIX = 'takamatsu-pokapoka-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(\d{1,2})[月/](\d{1,2})日?\(([月火水木金土日])(?:曜日)?\)'
CLOCK = r'((?:[01]?\d|2[0-3]):[0-5]\d)'
SLOT = re.compile(CLOCK + r'\s*[~〜-]\s*' + CLOCK + '?')
POST = re.compile(re.escape(ORIGIN) + r'/post-(\d+)/')
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|中止となりました|延期となりました')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def parse_article(html, url, day):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    content = soup.select_one('.p-single-content')
    heading = content.select_one('.p-single-head') if content else None
    if not heading:
        raise ValueError(f'催事詳細を確認できません: {url}')
    title = norm(heading.get_text(' ', strip=True))
    if re.search('中止|延期', title) or CANCEL.search(content.get_text()):
        return []
    if not re.search(r'アウフグース|熱波', title):
        return []
    lines = [norm(tag.get_text()) for tag in content.select('p')]
    if not any((m := re.match(DAY, line)) and (int(m[1]), int(m[2]), m[3]) ==
               (day.month, day.day, '月火水木金土日'[day.weekday()]) and '開催' in line for line in lines):
        return []
    time = next((line.split(':', 1)[1] for line in lines if re.match(r'(?:開催)?時間\s*:', line)), '')
    title = re.sub(r'^【[^】]+】|開催について$', '', title).strip()
    ds = day.isoformat()
    slots = list(SLOT.finditer(time))
    rows = []
    for index, slot in enumerate(slots, 1):
        start = slot[1].zfill(5)
        end = slot[2].zfill(5) if slot[2] else None
        if end and end <= start:
            end = None
        rows.append({
            'id': f'{PREFIX}{POST.fullmatch(url)[1]}-{index}',
            'date': ds, 'title': f'{title}（第{index}回）' if len(slots) > 1 else title,
            'venue': VENUE, 'venueArea': '多肥上町', 'zone': 'other', 'source': SOURCE,
            'url': url, 'doorAt': None, 'startAt': f'{ds}T{start}:00+09:00',
            'endAt': f'{ds}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の開催時間終了' if end else None,
            'note': '出典：高松ぽかぽか温泉公式サイト。参加券・入館条件は公式詳細参照。',
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

    soup = BeautifulSoup(read(SCHEDULE), 'html.parser', from_encoding='utf-8')
    cards = soup.select('.p-archive-post')
    if not cards:
        raise ValueError('公式イベント一覧を確認できません')
    rows = []
    for card in cards:
        title = card.select_one('.p-archive-ttl')
        stamp = card.select_one('.p-archive-date')
        anchor = card.select_one('a[href]')
        if not title or not stamp or not anchor or not re.search('アウフグース|熱波', title.get_text()):
            continue
        match = re.match(r'【' + DAY, norm(title.get_text()))
        if not match:
            continue
        published = datetime.strptime(stamp.get_text(strip=True), '%Y.%m.%d').date()
        day = date(published.year, int(match[1]), int(match[2]))
        if day < published - timedelta(days=7) and published.month >= 11 and day.month <= 2:
            day = day.replace(year=day.year + 1)
        if not today <= day <= today + timedelta(days=120) or '月火水木金土日'[day.weekday()] != match[3]:
            continue
        url = urljoin(SCHEDULE, anchor['href'])
        if not POST.fullmatch(url):
            raise ValueError('催事詳細リンクを確認できません')
        rows.extend(parse_article(read(url), url, day))
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
