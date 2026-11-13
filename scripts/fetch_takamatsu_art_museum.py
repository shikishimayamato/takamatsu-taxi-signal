#!/usr/bin/env python3
"""高松市美術館の公式展覧会を既存の events.json に統合する。"""
from __future__ import annotations

import hashlib
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

ORIGIN = 'https://www.city.takamatsu.kagawa.jp'
SCHEDULE = ORIGIN + '/museum/takamatsu/event/index.html'
DETAIL = re.compile(r'/museum/takamatsu/event/exhibitions/[\w/-]+\.html')
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '高松市美術館'
SOURCE = '公式・高松市美術館'
PREFIX = 'takamatsu-art-museum-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
DATE = r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日?'
WEEKDAY = r'\([月火水木金土日](?:曜日)?(?:・祝日?|・休日)?\)'
CLOCK = r'(午前|午後)(\d{1,2})時(?:(\d{1,2})分)?'
CANCELLED = re.compile(r'(?:開催|展覧会)(?:を)?(?:中止|延期)|^(?:【|\[)?(?:中止|延期)')


def norm(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def calendar_day(match, default_year: int) -> date:
    year, month, day = match.groups()
    return date(int(year or default_year), int(month), int(day))


def date_span(value: str, year: int | None = None) -> tuple[date, date]:
    value = re.sub(WEEKDAY, '', norm(value))
    match = re.fullmatch(DATE + r'[~〜～]' + DATE, value)
    if not match:
        raise ValueError(f'会期の形式を確認してください: {value[:100]}')
    left, right = value.replace('〜', '~').replace('～', '~').split('~')
    first, last = re.fullmatch(DATE, left), re.fullmatch(DATE, right)
    if not first[1] and year is None:
        raise ValueError('会期の開催年が未掲載')
    start = calendar_day(first, year)
    end = calendar_day(last, start.year + (int(last[2]) < start.month))
    if end < start:
        raise ValueError('会期の終了日が開始日より前です')
    return start, end


def clock(value: tuple) -> str:
    meridiem, hour, minute = value
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or minute > 59:
        raise ValueError('公式時刻を確認してください')
    return f'{hour % 12 + (12 if meridiem == "午後" else 0):02d}:{minute:02d}'


def closures(value: str, first: date) -> tuple[bool, set, set]:
    value = re.sub(WEEKDAY, '', value)
    if value == '会期中無休':
        return False, set(), set()
    if value == '月曜日':
        return True, set(), set()
    match = re.fullmatch(r'月曜日\(ただし、(.+?)は?開館、(.+?)は?休館\)', value)
    if not match:
        raise ValueError(f'休館日の表記を確認してください: {value[:100]}')
    def listed(text: str) -> set:
        return {calendar_day(m, first.year + (int(m[2]) < first.month))
                for item in re.split(r'[・、]', text)
                if (m := re.fullmatch(DATE, item))}
    opened, closed = map(listed, match.groups())
    if not opened or not closed or any(
        not re.fullmatch(DATE, item) for group in match.groups() for item in re.split(r'[・、]', group)
    ):
        raise ValueError('例外の開館日・休館日を確認してください')
    return True, opened, closed


def opening_hours(value: str, first: date, last: date) -> tuple:
    # 入室締切の注記を除き、明示された時間帯と金・土の延長だけ使う。
    value = re.sub(r'\(入室は閉館\d+分前まで\)', '', value)
    match = re.fullmatch(CLOCK + r'[~〜～]' + CLOCK + r'(.*)', value)
    if not match:
        return None, None, None
    start, end = clock(match.groups()[:3]), clock(match.groups()[3:6])
    extra = match[7]
    if not extra:
        return start, end, None
    late = re.fullmatch(r'\(ただし、(?:特別展開催期間中の)?金曜日・土曜日(?:\(([^()]+)\))?は、?'
                        + CLOCK + r'(?:まで|閉館)\)', extra)
    if not late:
        return start, None, None
    since, until = date_span(late[1], first.year) if late[1] else (first, last)
    return start, end, (since, until, clock(late.groups()[1:]))


def exhibition_links(html: bytes, today: date) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    cards = soup.select('.slid_box')
    if not cards or not soup.find('h1', string=re.compile('展覧会')):
        raise ValueError('美術館の展覧会一覧を確認できません')
    links = []
    for card in cards:
        if card.select_one('.l-open, .l-next') is None:
            continue
        link = card.select_one('a[href]')
        if not link:
            continue  # 詳細未掲載の予告
        url = urljoin(SCHEDULE, link['href'])
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) != ('https', urlsplit(ORIGIN).netloc) or parts.query or parts.fragment or not DETAIL.fullmatch(parts.path):
            raise ValueError('展覧会詳細URLの形式が変わりました')
        span = card.select_one('.slid-txt')
        date_line = list(span.stripped_strings)[-1] if span else ''
        first, last = date_span(date_line)
        if last >= today and first <= today + timedelta(days=120) and url not in links:
            links.append(url)
    if len(links) > 12:
        raise ValueError('展覧会詳細リンクの上限を超えました')
    return links


def parse_exhibition(html: bytes, url: str, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    title_tag = soup.find('h1')
    title = ' '.join(title_tag.get_text(' ', strip=True).split()) if title_tag else ''
    heading = next((h for h in soup.find_all('h2') if norm(h.get_text()) == '展覧会基本情報'), None)
    if CANCELLED.search(title):
        return []
    if not title or heading is None:
        raise ValueError('展覧会基本情報が見つかりません')
    paragraphs = []
    for tag in heading.find_all_next(['h2', 'p']):
        if tag.name == 'h2':
            break
        paragraphs.append(norm(tag.get_text(' ', strip=True)))
    fields = dict(re.findall(r'(会期|休館日|開館時間|会場|主催):(.+?)(?=(?:会期|休館日|開館時間|会場|主催|観覧料|助成):|$)',
                             '\n'.join(paragraphs).replace('\n', '')))
    if CANCELLED.search(fields.get('会期', '')):
        return []
    if not {'会期', '休館日'} <= fields.keys():
        raise ValueError('会期・休館日を確認できません')
    first, last = date_span(fields['会期'])
    monday, opened, closed = closures(fields['休館日'], first)
    start, end, extension = opening_hours(fields.get('開館時間', ''), first, last)
    day = max(today, first)
    rows = []
    event_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    while day <= min(last, today + timedelta(days=120)):
        if day not in closed and (not monday or day.weekday() != 0 or day in opened):
            ds = day.isoformat()
            stop = end
            if extension and day.weekday() in {4, 5} and extension[0] <= day <= extension[1]:
                stop = extension[2]
            rows.append({
                'id': f'{PREFIX}{event_key}-{ds}', 'title': title, 'date': ds,
                'venue': VENUE, 'venueArea': '紺屋町', 'zone': 'other',
                'source': SOURCE, 'url': url, 'doorAt': None,
                'startAt': f'{ds}T{start}:00+09:00' if start else None,
                'endAt': f'{ds}T{stop}:00+09:00' if stop else None,
                'endTimeType': '公式掲載の開館時間終了' if stop else None,
                'note': '出典：高松市美術館公式サイト。',
            })
        day += timedelta(days=1)
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

    def page(url: str) -> bytes:
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 2_000_000)

    urls = exhibition_links(page(SCHEDULE), today)
    return [row for url in urls for row in parse_exhibition(page(url), url, today)]


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
