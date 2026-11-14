#!/usr/bin/env python3
"""香川県立ミュージアムの公式サイトから本館の展覧会を取得する。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://www.kmuseum.pref.kagawa.lg.jp'
SCHEDULE = ORIGIN + '/exhibition/schedule/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '香川県立ミュージアム'
SOURCE = '公式・香川県立ミュージアム'
PREFIX = 'kagawa-museum-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
DAY = r'(?:(20\d\d)\.)?(\d{1,2})/(\d{1,2})\(([月火水木金土日])(?:・祝)?\)'
SPAN = re.compile(r'(?:(.+?):\s*)?' + DAY + r'\s*[~〜]\s*' + DAY)
CANCEL = re.compile(r'中止|延期')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def read_day(parts, year):
    explicit, month, dom, weekday = parts
    day = date(int(explicit) if explicit else year, int(month), int(dom))
    return day if '月火水木金土日'[day.weekday()] == weekday else None


def parse_schedule(html, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.find('h2', string=re.compile(r'^20\d\d年度\s+展覧会年間スケジュール$'))
    special = soup.find('h3', string='特別展')
    permanent = soup.find('h3', string='常設展')
    if not (heading and special and permanent):
        raise ValueError('本館の展覧会年間スケジュールを確認できません')
    year = int(heading.get_text()[:4])
    candidates = []
    for block in special.next_siblings:
        if block is permanent:
            break
        if getattr(block, 'name', None) != 'div':
            continue
        title = block.find('h4')
        paragraph = title.find_next_sibling('p') if title else None
        if paragraph:
            candidates.append((title.get_text(' ', strip=True), list(paragraph.stripped_strings)))
    for block in permanent.next_siblings:
        if getattr(block, 'name', None) == 'h3':
            break
        if getattr(block, 'name', None) != 'figure':
            continue
        for tr in block.select('tbody tr'):
            cells = tr.find_all('td', recursive=False)
            if len(cells) == 2:
                candidates.append((cells[0].get_text(' ', strip=True), list(cells[1].stripped_strings)[:1]))
    rows = []
    recognized = 0
    for title, lines in candidates:
        if CANCEL.search(title) or any(CANCEL.search(line) for line in lines):
            continue
        for line in lines:
            match = SPAN.fullmatch(norm(line))
            if not match:
                continue
            phase = match[1]
            first = read_day(match.groups()[1:5], year)
            if first is None:
                continue
            last = read_day(match.groups()[5:], first.year + (int(match[7]) < first.month))
            if last is None or last < first:
                continue
            recognized += 1
            if last < today or first > today + timedelta(days=120):
                continue
            label = f'{title} {phase}' if phase else title
            key = hashlib.sha256(f'{year}|{norm(label)}'.encode()).hexdigest()[:16]
            rows.append({
                'id': PREFIX + key, 'date': first.isoformat(), 'endDate': last.isoformat(),
                'title': label, 'venue': VENUE, 'venueArea': '玉藻町', 'zone': 'station',
                'source': SOURCE, 'url': SCHEDULE, 'doorAt': None, 'startAt': None,
                'endAt': None, 'endTimeType': None,
                'note': '出典：香川県立ミュージアム本館の公式年間スケジュール。休館日・観覧時間は公式案内参照。',
            })
    if not recognized:
        raise ValueError('本館の展覧会会期を確認できません')
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
    if not parser.can_fetch(AGENT, SCHEDULE):
        raise PermissionError('robots.txtが展覧会一覧の取得を拒否')
    return parse_schedule(get(session, SCHEDULE, 4_000_000), today)


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
