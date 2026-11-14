#!/usr/bin/env python3
"""讃岐国分寺跡資料館の公式サイトから公式企画展を取得する。"""
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

ORIGIN = 'https://www.city.takamatsu.kagawa.jp'
ROOT = ORIGIN + '/smph/kurashi/kosodate/bunka/kokubunjiato/'
SCHEDULE = ROOT + 'sanuki-tennji.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '讃岐国分寺跡資料館'
SOURCE = '公式・讃岐国分寺跡資料館'
PREFIX = 'sanuki-kokubunji-museum-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = r'(?:令和(\d+)年)?(\d{1,2})月(\d{1,2})日\(([月火水木金土日])(?:曜日)?(?:・祝日?)?\)'
SPAN = re.compile(r'(?:開催期間\s*)?(?:\((前期|後期)\)\s*)?' + DAY + r'\s*[~〜]\s*' + DAY)


def day_from(parts, default=None):
    era, month, day, weekday = parts
    year = 2018 + int(era) if era else default
    if year is None:
        raise ValueError('開催年が確認できません')
    result = date(year, int(month), int(day))
    if '月火水木金土日'[result.weekday()] != weekday:
        raise ValueError('開催日と曜日が一致しません')
    return result


def parse_schedule(html, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    header = soup.find('h3', string=re.compile(r'^令和\d+年度の企画展予定$'))
    section = header.find_parent('div', class_='h3bg') if header else None
    if not section:
        raise ValueError('資料館の年度別企画展予定を確認できません')
    rows = []
    recognized = 0
    for block in section.next_siblings:
        if getattr(block, 'name', None) != 'div':
            continue
        if block.find(['h2', 'h3']):
            break
        for paragraph in block.find_all('p'):
            for br in paragraph.find_all('br'):
                br.replace_with('\n')
            lines = [' '.join(line.split()) for line in paragraph.get_text().splitlines() if line.strip()]
            if len(lines) < 2 or re.search(r'中止|延期', ' '.join(lines)):
                continue
            title = lines[0].lstrip('■ ')
            link = paragraph.find('a', href=True)
            url = urljoin(SCHEDULE, link['href']) if link else SCHEDULE
            if not re.fullmatch(re.escape(ROOT) + r'[\w-]+\.html', url):
                url = SCHEDULE
            for line in lines[1:]:
                match = SPAN.fullmatch(unicodedata.normalize('NFKC', line))
                if not match:
                    continue
                first = day_from(match.groups()[1:5])
                last = day_from(match.groups()[5:], first.year + (int(match[7]) < first.month))
                if last < first:
                    raise ValueError('会期の順序が不正です')
                recognized += 1
                if last < today or first > today + timedelta(days=120):
                    continue
                label = title + (f'（{match[1]}）' if match[1] else '')
                key = hashlib.sha256(f'{first}|{label}'.encode()).hexdigest()[:16]
                rows.append({
                    'id': PREFIX + key, 'date': first.isoformat(), 'endDate': last.isoformat(),
                    'title': label, 'venue': VENUE, 'venueArea': '国分寺町国分', 'zone': 'other',
                    'source': SOURCE, 'url': url, 'doorAt': None, 'startAt': None,
                    'endAt': None, 'endTimeType': None,
                    'note': '出典：高松市の資料館公式展示予定。休館日・観覧時間は公式案内参照。',
                })
    if not recognized:
        raise ValueError('会期が明記された企画展を確認できません')
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
    if not parser.can_fetch(AGENT, SCHEDULE):
        raise PermissionError('robots.txtが展示予定の取得を拒否')
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
