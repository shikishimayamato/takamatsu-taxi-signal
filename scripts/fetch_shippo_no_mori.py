#!/usr/bin/env python3
"""しっぽの森の公式サイトから単日の催事を取得する。"""
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
SCHEDULE = ORIGIN + '/s-doubutuaigo/sanukidouaicenter/event.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'しっぽの森（さぬき動物愛護センター）'
SOURCE = '公式・さぬき動物愛護センター'
PREFIX = 'shippo-no-mori-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
CLOCK = r'([01]?\d|2[0-3])時(?:([0-5]?\d)分)?'
TIME = re.compile(r'\(' + CLOCK + r'[~〜](?:' + CLOCK + r')?\)$')
LOCAL = re.compile(r'^(?:譲渡前講習・譲渡会|犬のしつけ方教室|冬休み動物あいご教室|動物愛護フェスティバル)$')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def parse_schedule(html, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    headings = soup.select('#tmp_contents h6')
    months = set()
    rows = []
    for heading in headings:
        month = re.fullmatch(r'令和(\d+)年(\d{1,2})月', norm(heading.get_text()))
        if not month:
            continue
        year, number = 2018 + int(month[1]), int(month[2])
        table = heading.find_next_sibling('table')
        if table is None:
            raise ValueError('公式月別表を確認できません')
        days = set()
        for tr in table.select('tr'):
            cells = tr.find_all(['td', 'th'], recursive=False)
            if len(cells) != 3 or not norm(cells[0].get_text()).isdecimal():
                continue
            day = date(year, number, int(norm(cells[0].get_text())))
            if day in days or norm(cells[1].get_text()) != '月火水木金土日'[day.weekday()]:
                raise ValueError('公式日程表の日付と曜日が一致しません')
            days.add(day)
            if not today <= day <= today + timedelta(days=120):
                continue
            # 出張告知は同じ日でも別行。センター開催が確認できる行だけ採用する。
            for br in cells[2].find_all('br'):
                br.replace_with('\n')
            for line in cells[2].get_text().splitlines():
                line = norm(line)
                hours = TIME.search(line)
                title = line[:hours.start()].strip() if hours else line
                if not LOCAL.fullmatch(title):
                    continue
                start = f'{int(hours[1]):02d}:{int(hours[2] or 0):02d}' if hours else None
                end = f'{int(hours[3]):02d}:{int(hours[4] or 0):02d}' if hours and hours[3] else None
                if end and end <= start:
                    end = None
                ds = day.isoformat()
                key = hashlib.sha256(f'{ds}|{title}'.encode()).hexdigest()[:16]
                rows.append({
                    'id': PREFIX + key, 'date': ds, 'title': title, 'venue': VENUE,
                    'venueArea': '東植田町', 'zone': 'other', 'source': SOURCE, 'url': SCHEDULE,
                    'doorAt': None, 'startAt': f'{ds}T{start}:00+09:00' if start else None,
                    'endAt': f'{ds}T{end}:00+09:00' if end else None,
                    'endTimeType': '公式掲載の開催時間終了' if end else None,
                    'note': '出典：しっぽの森公式スケジュール。申込・参加条件は公式案内参照。',
                })
        next_month = date(year + (number == 12), number % 12 + 1, 1)
        if len(days) != (next_month - date(year, number, 1)).days:
            raise ValueError('公式日程表の日数が一致しません')
        months.add((year, number))
    if (today.year, today.month) not in months:
        raise ValueError('今月の公式日程表がありません')
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
        raise PermissionError('robots.txtが公式日程の取得を拒否')
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
