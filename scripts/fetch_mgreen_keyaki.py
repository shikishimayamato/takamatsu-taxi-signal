#!/usr/bin/env python3
"""丸亀町グリーン けやき広場の公式サイトから公式催事を取得する。"""
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

ORIGIN = 'https://mgreen.jp'
SCHEDULE = ORIGIN + '/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '丸亀町グリーン けやき広場'
SOURCE = '公式・丸亀町グリーン'
PREFIX = 'mgreen-keyaki-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(?:(20\d\d)年)?(\d{1,2})[./](\d{1,2})\(([月火水木金土日])(?:・祝)?\)'
PERIOD = re.compile(DAY + r'(?:[~〜]' + DAY + r')?')
CLOCK = r'(?:[01]?\d|2[0-3])(?:時(?:[0-5]?\d分)?|:[0-5]\d)'
HOURS = re.compile(f'({CLOCK})[-~〜]({CLOCK})')
DETAIL = re.compile(re.escape(SCHEDULE) + r'(\d+)/?')


def norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def clock(value):
    parts = re.split(r'時|:', value.removesuffix('分'))
    return f'{int(parts[0]):02d}:{int(parts[1] or 0):02d}'


def read_day(parts, year):
    explicit, month, dom, weekday = parts
    day = date(int(explicit) if explicit else year, int(month), int(dom))
    return day if '月火水木金土日'[day.weekday()] == weekday else None


def parse_detail(html, url, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.select_one('.event-info__heading')
    fields = {}
    for item in soup.select('.event-info__item'):
        label, value = item.select_one('.event-info__label'), item.select_one('.event-info__text')
        if label and value:
            fields[label.get_text(strip=True)] = norm(value.get_text(' ', strip=True))
    if not heading or not re.fullmatch(r'(?:丸亀町グリーン)?けやき広場', fields.get('場所', '')):
        return []
    title = ' '.join(heading.get_text(' ', strip=True).split())
    if re.search(r'中止|延期', title):
        return []
    period = PERIOD.fullmatch(fields.get('期間', ''))
    metadata = soup.select_one('.yoast-schema-graph')
    if not period or not metadata:
        return []
    published = next((node.get('datePublished') for node in json.loads(metadata.get_text())['@graph']
                      if node.get('datePublished')), None)
    if not published:
        return []
    year = datetime.fromisoformat(published).astimezone(JST).year
    first = read_day(period.groups()[:4], year)
    if first is None:
        return []
    last = read_day(period.groups()[4:], first.year + (int(period[6]) < first.month)) if period[6] else first
    if last is None or last < first or (last - first).days > 60:
        return []
    time = fields.get('時間', '')
    uniform = HOURS.fullmatch(time)
    weekdays = re.fullmatch(r'平日:' + HOURS.pattern + r'/土日:' + HOURS.pattern + r'※毎週月曜日はお休み', time)
    final_day = re.fullmatch(HOURS.pattern + r'※最終日(\d+)日\([月火水木金土日]\)は(' + CLOCK + r')まで', time)
    if final_day and int(final_day[3]) != last.day:
        final_day = None
    if first != last and not (uniform or weekdays or final_day):
        return []
    rows = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        if not today <= day <= today + timedelta(days=120) or (weekdays and day.weekday() == 0):
            continue
        start = end = None
        if weekdays:
            i = 3 if day.weekday() >= 5 else 1
            start, end = clock(weekdays[i]), clock(weekdays[i + 1])
        elif final_day:
            start, end = clock(final_day[1]), clock(final_day[4] if day == last else final_day[2])
        elif uniform:
            start, end = map(clock, uniform.groups())
        if end and end <= start:
            end = None
        ds = day.isoformat()
        rows.append({
            'id': f'{PREFIX}{DETAIL.fullmatch(url)[1]}-{ds}', 'date': ds, 'title': title,
            'venue': VENUE, 'venueArea': '丸亀町', 'zone': 'downtown', 'source': SOURCE,
            'url': url, 'doorAt': None, 'startAt': f'{ds}T{start}:00+09:00' if start else None,
            'endAt': f'{ds}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の開催時間終了' if end else None,
            'note': '出典：丸亀町グリーン公式イベント案内。参加条件は公式詳細参照。',
        })
    return rows


def fetch(session, today):
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)

    def read(url):
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    page = SCHEDULE
    urls = {}
    for index in range(1, 5):
        soup = BeautifulSoup(read(page), 'html.parser', from_encoding='utf-8')
        links = soup.select('.post-list-1__item a[href]')
        if not links:
            raise ValueError('公式イベント一覧を確認できません')
        for link in links:
            url = urljoin(page, link['href']).rstrip('/')
            if DETAIL.fullmatch(url):
                urls[url] = None
        next_page = soup.select_one('a.next.page-numbers[href]')
        if not next_page or index == 4:
            break
        page = urljoin(page, next_page['href'])
        if page != f'{SCHEDULE}page/{index + 1}/':
            raise ValueError('公式イベント一覧のページ送りを確認できません')
    return [row for url in urls for row in parse_detail(read(url), url, today)]


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
