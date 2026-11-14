#!/usr/bin/env python3
"""瀬戸内海歴史民俗資料館の公式サイトから企画展と展示解説を取得する。"""
from __future__ import annotations

import hashlib
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

ORIGIN = 'https://www.pref.kagawa.lg.jp'
ROOT = ORIGIN + '/kmuseum/setorekishi/exhib/'
SCHEDULE = ROOT + 'setoutigyarari.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '瀬戸内海歴史民俗資料館'
SOURCE = '公式・瀬戸内海歴史民俗資料館'
PREFIX = 'setouchi-history-museum-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日\(([月火水木金土日])曜日(?:・祝日?)?\)'
SPAN = re.compile(DAY + r'~' + DAY)
CLOCK = r'([01]?\d|2[0-3])時(?:([0-5]?\d)分)?'
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|中止のお知らせ|延期のお知らせ')


def compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).replace('〜', '~')


def schedule_day(parts: tuple, fiscal_year: int) -> date:
    year, month, day, weekday = parts
    month = int(month)
    result = date(int(year) if year else fiscal_year + (month <= 3), month, int(day))
    if '月火水木金土日'[result.weekday()] != weekday:
        raise ValueError('日付と曜日が一致しません')
    return result


def event_row(title, url, first, last, start=None, end=None):
    key = hashlib.sha256(f'{first}|{compact(title)}'.encode()).hexdigest()[:16]
    row = {
        'id': PREFIX + key, 'title': title, 'date': first.isoformat(),
        'venue': VENUE, 'venueArea': '亀水町', 'zone': 'other',
        'source': SOURCE, 'url': url, 'doorAt': None,
        'startAt': f'{first}T{start}:00+09:00' if start else None,
        'endAt': f'{last}T{end}:00+09:00' if end else None,
        'endTimeType': '公式掲載の開催時間終了' if end else None,
        'note': '出典：瀬戸内海歴史民俗資料館公式サイト。休館日・入館条件は公式詳細参照。',
    }
    if last != first:
        row['endDate'] = last.isoformat()
    return row


def parse_gallery(html, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.find('h4', string=re.compile(r'^令和\d+年度$'))
    table = heading.find_next('table') if heading else None
    if table is None or not {'テーマ', '会期'} <= {h.get_text(strip=True) for h in table.select('th')}:
        raise ValueError('瀬戸内ギャラリーの年度別企画展を確認できません')
    fiscal_year = 2018 + int(re.search(r'\d+', heading.get_text())[0])
    for tr in table.select('tr'):
        title_cell = tr.find('td', recursive=False)
        date_cell = tr.find('th', recursive=False)
        if title_cell is None or date_cell is None:
            continue
        title = ' '.join(tag.get_text(strip=True) for tag in title_cell.select('strong'))
        match = SPAN.fullmatch(compact(date_cell.get_text()))
        if not title or not match or re.search('中止|延期', title_cell.get_text(strip=True)):
            continue
        first = schedule_day(match.groups()[:4], fiscal_year)
        last = schedule_day(match.groups()[4:], fiscal_year)
        if last < first:
            raise ValueError('会期の順序が異なります')
        if last < today or first > today + timedelta(days=120):
            continue
        link = title_cell.select_one('a[href]')
        url = urljoin(SCHEDULE, link['href']) if link else SCHEDULE
        if not re.fullmatch(re.escape(ROOT) + r'[\w-]+\.html', url):
            raise ValueError('企画展リンクの形式が異なります')
        yield title, url, first, last, fiscal_year


def parse_talks(html, title, url, first, last, fiscal_year, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.find('h3', string='展示解説')
    table = heading.find_next_sibling('table') if heading else None
    if table is None:
        return []
    rows = []
    for tr in table.select('tr'):
        cells = tr.find_all(['td', 'th'], recursive=False)
        if len(cells) != 2 or compact(cells[0].get_text()) != '日時':
            continue
        lines = [compact(p.get_text()) for p in cells[1].find_all('p')]
        if not lines or any(CANCEL.search(line) for line in lines):
            continue
        # 「・祝日」は曜日注記であり、開催日の区切りではない。
        matches = [re.fullmatch(DAY, item) for item in re.split(r'(?<=\))・', lines[0])]
        if not all(matches):
            continue
        hours = re.fullmatch(r'(?:各日)?' + CLOCK + r'~' + CLOCK, lines[1]) if len(lines) > 1 else None
        start = end = None
        if hours:
            start = f'{int(hours[1]):02d}:{int(hours[2] or 0):02d}'
            end = f'{int(hours[3]):02d}:{int(hours[4] or 0):02d}'
            if end <= start:
                end = None
        for match in matches:
            day = schedule_day(match.groups(), fiscal_year)
            if first <= day <= last and today <= day <= today + timedelta(days=120):
                rows.append(event_row(title.split(' -', 1)[0] + ' 展示解説', url, day, day, start, end))
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

    rows = []
    for title, url, first, last, year in parse_gallery(read(SCHEDULE), today):
        rows.append(event_row(title, url, first, last))
        if url != SCHEDULE:
            rows.extend(parse_talks(read(url), title, url, first, last, year, today))
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
