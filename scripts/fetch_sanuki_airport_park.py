#!/usr/bin/env python3
"""さぬき空港公園の公式サイトから単日の催事を取得する。"""
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

ORIGIN = 'https://www.sanuki-airport-park.com'
SCHEDULE = ORIGIN + '/event.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'さぬき空港公園'
SOURCE = '公式・さぬき空港公園'
PREFIX = 'sanuki-airport-park-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
MONTH_LINK = re.compile(r'event_(\d{1,2})[a-z]+_(\d{2})\.html')
MONTH_TITLE = re.compile(r'(20\d\d)年(\d{1,2})月')
CLOCK = r'(?:[01]?\d|2[0-3]):[0-5]\d'
TIMES = re.compile(r'^(' + CLOCK + r')(?:\s*[~〜～-]\s*(' + CLOCK + r'))?(?![\d:])')
EVENT = re.compile(r'大会|選手権|祭り|まつり|感謝祭|フェスティバル|マルシェ|運動会')
SKIP = re.compile(r'予備日|前日準備|練習|遠足|校外学習|中止|延期')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def parse_month(html, url, year, month, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = next((tag for tag in soup.select('b') if MONTH_TITLE.fullmatch(norm(tag.get_text()))), None)
    if heading is None or MONTH_TITLE.fullmatch(norm(heading.get_text())).groups() != (str(year), str(month)):
        raise ValueError(f'公式月別日程の年月を確認できません: {url}')
    if heading.find_parent('font') is None:
        raise ValueError(f'公式月別日程の構造が異なります: {url}')
    table = heading.find_parent('font').find_next_sibling('table')
    if table is None:
        if soup.select_one('img[src="img/ebent_sanutan.gif"]'):
            return []
        raise ValueError(f'公式月別日程表を確認できません: {url}')
    rows = []
    checked_days = set()
    for tr in table.find_all('tr'):
        cells = tr.find_all('td', recursive=False)
        if len(cells) != 3 or not norm(cells[0].get_text()).isdigit():
            continue
        day = date(year, month, int(norm(cells[0].get_text())))
        if norm(cells[1].get_text()) != '月火水木金土日'[day.weekday()] or day in checked_days:
            raise ValueError(f'公式日程の日付と曜日を確認できません: {url}')
        checked_days.add(day)
        if not today <= day <= today + timedelta(days=120):
            continue
        for block in re.split(r'\n\s*\n', cells[2].get_text(separator='', strip=False)
                              if not cells[2].find('br') else line_break_text(cells[2])):
            line = norm(block)
            title, sep, details = line.partition('(')
            title = title.strip()
            if not EVENT.search(title) or SKIP.search(line):
                continue
            hours = TIMES.match(details) if sep else None
            start = hours[1].zfill(5) if hours else None
            end = hours[2].zfill(5) if hours and hours[2] else None
            if end and end <= start:
                end = None
            people = re.search(r'・\s*([\d,]+)\s*人\s*\)$', details)
            ds = day.isoformat()
            event_id = hashlib.sha256(f'{ds}|{title}'.encode()).hexdigest()[:16]
            row = {
                'id': PREFIX + event_id, 'date': ds, 'title': title, 'venue': VENUE,
                'venueArea': '香南町由佐', 'zone': 'airport', 'source': SOURCE,
                'url': url, 'doorAt': None,
                'startAt': f'{ds}T{start}:00+09:00' if start else None,
                'endAt': f'{ds}T{end}:00+09:00' if end else None,
                'endTimeType': '公式掲載の開催時間終了' if end else None,
                'note': '出典：さぬき空港公園公式の月別イベント予定。',
            }
            if people:
                row['participants'] = int(people[1].replace(',', ''))
            rows.append(row)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    if len(checked_days) != (next_month - date(year, month, 1)).days:
        raise ValueError(f'公式日程表の日数を確認できません: {url}')
    return rows


def line_break_text(cell):
    for br in cell.find_all('br'):
        br.replace_with('\n')
    return cell.get_text()


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

    index = BeautifulSoup(read(SCHEDULE), 'html.parser', from_encoding='utf-8')
    links = {}
    for link in index.select('a[href]'):
        match = MONTH_LINK.fullmatch(link['href'])
        if match:
            month, year = int(match[1]), 2000 + int(match[2])
            links[(year, month)] = ORIGIN + '/' + link['href']
    if (today.year, today.month) not in links:
        raise ValueError('今月の公式イベントページが見つかりません')
    rows = []
    for (year, month), url in sorted(links.items()):
        first = date(year, month, 1)
        if today.replace(day=1) <= first <= today + timedelta(days=120):
            rows.extend(parse_month(read(url), url, year, month, today))
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
