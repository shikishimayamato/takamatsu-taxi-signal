#!/usr/bin/env python3
"""公渕森林公園の公式告知から開催日時が明記された催事を取得する。"""
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

ORIGIN = 'https://kinbuchi-shinrin.jp'
SCHEDULE = ORIGIN + '/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '公渕森林公園'
SOURCE = '公式・公渕森林公園'
PREFIX = 'kinbuchi-park-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
DAY = re.compile(r'(?:(20\d{2})年|令和(元|\d{1,2})年)\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*\(([月火水木金土日])(?:曜日)?(?:・祝(?:日)?)?\))?')
CLOCK = r'(午前|午後)?\s*(\d{1,2})(?:[:：]([0-5]\d)|時(?:\s*([0-5]?\d)分)?)'
HOURS = re.compile(CLOCK + r'\s*[~〜～]\s*' + CLOCK)
START = re.compile(CLOCK + r'\s*[~〜～]\s*$')
WHEN = re.compile(r'^(?:\d+\s*)?\[?\s*(?:開催\s*)?日\s*時\s*\]?\s*[:：]?\s*')
CANCEL = re.compile(r'開催中止|開催を中止|中止となりました|開催延期|開催を延期')
WEEK = '月火水木金土日'


def norm(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def clock(parts: tuple, default: str | None = None) -> str | None:
    meridiem, hour, minute, japanese_minute = parts
    hour = int(hour)
    meridiem = meridiem or default
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == '午後' else 0)
    if hour >= 24:
        return None
    return f'{hour:02d}:{int(minute or japanese_minute or 0):02d}'


def parse_page(html: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    cards = soup.select('.h-accordion-item')
    if not cards or '公渕森林公園' not in (soup.title.get_text() if soup.title else ''):
        raise ValueError('公渕森林公園の公式告知欄を確認できません')
    rows = []
    for card in cards:
        header = card.select_one('a.h-accordion-item-title > span')
        content = card.select_one('.h-accordion-item-content__container')
        if header is None or content is None:
            continue
        raw_title = ' '.join(header.get_text(' ', strip=True).split())
        title_match = re.match(r'^[『「]([^』」]+)[』」]', raw_title)
        if not title_match or re.search(r'中止|延期|終了', raw_title):
            continue
        paragraphs = [norm(p.get_text(' ', strip=True)) for p in content.select('p')]
        if any(CANCEL.search(p) for p in paragraphs):
            continue
        for line in paragraphs:
            label = WHEN.match(line)
            if not label:
                continue
            match = DAY.match(line[label.end():])
            if not match:
                continue
            western, era, month, dom, weekday = match.groups()
            year = int(western) if western else 2018 + (1 if era == '元' else int(era))
            day = date(year, int(month), int(dom))
            if (weekday and WEEK[day.weekday()] != weekday) or not today <= day <= today + timedelta(days=120):
                continue
            tail = line[label.end() + match.end():].strip()
            # 複数日程や複数回の記載を単日の時刻として取り込まない。
            if re.search(r'\d{1,2}月|\d{1,2}日|部制|回制|[~〜～]\s*\d+\s*日', tail):
                continue
            span = HOURS.match(tail)
            start_only = START.fullmatch(tail)
            start = end = None
            if span and not re.search(r'\d', tail[span.end():]):
                start = clock(span.groups()[:4])
                end = clock(span.groups()[4:], span[1])
                if end and (not start or start >= end):
                    end = None
            elif start_only:
                start = clock(start_only.groups())
            title = title_match[1]
            ds = day.isoformat()
            event_id = hashlib.sha256(f'{norm(title)}|{ds}'.encode()).hexdigest()[:16]
            rows.append({
                'id': PREFIX + event_id, 'date': ds, 'title': title,
                'venue': VENUE, 'venueArea': '東植田町', 'zone': 'other',
                'source': SOURCE, 'url': SCHEDULE + ('#' + content['id'] if content.get('id') else ''),
                'doorAt': None, 'startAt': f'{ds}T{start}:00+09:00' if start else None,
                'endAt': f'{ds}T{end}:00+09:00' if end else None,
                'endTimeType': '公式掲載の開催時間終了' if end else None,
                'note': '出典：公渕森林公園公式サイト。集合場所・申込方法は公式案内参照。',
            })
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots is not None else []
    parser = RobotFileParser()
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(rules)
    if not parser.can_fetch(AGENT, SCHEDULE):
        raise PermissionError('robots.txtが公式告知の取得を拒否')
    return parse_page(get(session, SCHEDULE, 4_000_000), today)


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
