#!/usr/bin/env python3
"""栗林公園の公式サイトから単日の催事を取得する。"""
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

ORIGIN = 'https://www.my-kagawa.jp'
NEWS = ORIGIN + '/ritsuringarden/news/'
SCHEDULE = NEWS + 'category:information'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '栗林公園'
SOURCE = '公式・栗林公園'
PREFIX = 'ritsurin-garden-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DATE = r'(?:(20\d\d|令和(?:元|\d{1,2}))年)?\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*\([月火水木金土日](?:曜日)?(?:・祝)?\))?'
PERIOD = re.compile(DATE + r'(?:\s*[~〜～]\s*' + DATE + r')?(?:\s*※.*)?')
CLOCK = r'([01]?\d|2[0-3])(?:時(?:\s*([0-5]?\d)分)?|:([0-5]\d))'
HOURS = re.compile(r'(?:(\d+)回目\s*)?' + CLOCK + r'(?:\s*[~〜～]\s*(?:' + CLOCK + r')?)?(?:\s*\([^()]*\)|\s*※.*)?')
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|中止(?:となりました|のお知らせ)|延期(?:となりました|のお知らせ)')


def norm(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def calendar_day(parts: tuple, default_year: int | None = None) -> date:
    year, month, day = parts
    if year and year.startswith('令和'):
        era = year[2:]
        return date(2018 + (1 if era == '元' else int(era)), int(month), int(day))
    if not year and default_year is None:
        raise ValueError('開催年がありません')
    return date(int(year) if year else default_year, int(month), int(day))


def time_text(parts: tuple) -> str:
    hour, minute, colon_minute = parts
    return f'{int(hour):02d}:{int(minute or colon_minute or 0):02d}'


def parse_detail(html: bytes, url: str, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.select_one('.newsTitle h1')
    body = soup.select_one('.dtlMainBox .txtBox')
    if heading is None or body is None:
        raise ValueError(f'公式催事の本文を確認できません: {url}')
    title = re.sub(r'(?:のお知らせ|を開催します)[。！!]*$', '', heading.get_text(' ', strip=True))
    lines = [norm(line) for line in body.stripped_strings]
    if '中止' in title or '延期' in title or any(CANCEL.search(line) for line in lines):
        return []
    labels = {'開催日', '開催期間', '開催日時', '日時', '会期'}
    date_index = next((i for i, line in enumerate(lines) if line.rstrip(':') in labels), None)
    if date_index is None or date_index + 1 >= len(lines):
        return []
    match = PERIOD.fullmatch(lines[date_index + 1])
    if not match or not match[1]:
        return []
    first = calendar_day(match.groups()[:3])
    if match[5]:
        year = first.year + (not match[4] and int(match[5]) < first.month)
        last = calendar_day(match.groups()[3:], year)
    else:
        last = first
    if last < first or last < today or first > today + timedelta(days=120):
        return []

    times = []
    for line in lines[date_index + 2:]:
        if line.rstrip(':') in {'開催時間', '時間'} and not times:
            continue
        hours = HOURS.fullmatch(line)
        if not hours:
            break
        start = time_text(hours.groups()[1:4])
        end = time_text(hours.groups()[4:]) if hours[5] else None
        times.append((hours[1], start, end if end and end > start else None))
    if first != last and len(times) > 1:
        times = []
    rows = []
    for index, (session, start, end) in enumerate(times or [(None, None, None)], 1):
        event_id = PREFIX + url.rsplit('/', 1)[-1]
        if index > 1 or session:
            event_id += '-' + (session or str(index))
        row = {
            'id': event_id, 'date': first.isoformat(),
            'title': f'{title}（第{session}回）' if session else title,
            'venue': VENUE, 'venueArea': '栗林町', 'zone': 'ritsurin',
            'source': SOURCE, 'url': url, 'doorAt': None,
            'startAt': f'{first}T{start}:00+09:00' if start else None,
            'endAt': f'{last}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の開催時間終了' if end else None,
            'note': '出典：栗林公園公式のお知らせ。入園・参加条件は公式詳細参照。',
        }
        if first != last:
            row['endDate'] = last.isoformat()
        rows.append(row)
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
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

    seen = set()
    rows = []
    page = SCHEDULE
    for index in range(3):
        soup = BeautifulSoup(read(page), 'html.parser', from_encoding='utf-8')
        articles = soup.select('.digestBox article')
        if not articles:
            raise ValueError('栗林公園のお知らせ一覧を確認できません')
        for article in articles:
            if not any(label.get_text(strip=True) == 'イベント' for label in article.select('.labels span')):
                continue
            link = article.select_one('h3 a[href]')
            if link is None:
                raise ValueError('催事詳細へのリンクを確認できません')
            url = urljoin(page, link['href'])
            if not re.fullmatch(re.escape(NEWS) + r'\d+', url) or url in seen:
                continue
            seen.add(url)
            rows.extend(parse_detail(read(url), url, today))
        next_page = soup.select_one('li.next a[rel=next][href]')
        if not next_page or index == 2:
            break
        page = urljoin(page, next_page['href'])
        if not page.startswith(NEWS) or set(page[len(NEWS):].split('/')) != {
            'category:information', f'page:{index + 2}', 'sort:is_view_priority', 'direction:desc',
        }:
            raise ValueError('お知らせ一覧のページ送りを確認できません')
    if not seen:
        raise ValueError('イベント分類の記事を確認できません')
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
