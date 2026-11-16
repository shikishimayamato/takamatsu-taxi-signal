#!/usr/bin/env python3
"""ホテルパールガーデンの公式一覧から公開公演を既存データに統合する。"""
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

import pymupdf
import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://hotel-pearlgarden.jp'
SCHEDULE = ORIGIN + '/category/event/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'ホテルパールガーデン'
SOURCE = '公式・ホテルパールガーデン'
PREFIX = 'pearlgarden-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
PERFORMANCE = re.compile(r'ディナー\s*ショー|コンサート|演奏会|ライブ公演|トークショー|舞台公演')
EVENT_DATE = re.compile(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日')
CLOCK = r'([01]?\d|2[0-3])\s*[:時]\s*([0-5]\d)\s*分?'
# Image-only poster. Use the manually verified transcription only for these exact bytes.
POSTERS = {
    'f44cdcf102a4bfbb6fd25c048e347172d16e9c6c89b024c98e7f32cd7760eef0':
        '2026年12月18日 18:00開場 18:30開演',
}


def candidates(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, 'html.parser')
    boxes = soup.select('.topics_box a[href] .topic_title')
    if not boxes:
        raise ValueError('公式イベント一覧の構造を確認できません')
    found = {}
    for heading in boxes:
        label = ' '.join(heading.stripped_strings)
        if not PERFORMANCE.search(unicodedata.normalize('NFKC', label)) or re.search(r'中止|延期', label):
            continue
        url = urljoin(SCHEDULE, heading.find_parent('a')['href'])
        if re.fullmatch(re.escape(ORIGIN)+r'/event/\d+/', url):
            found[url] = re.sub(r'(?:のご案内|開催のお知らせ)$', '', label)
    return found


def clocks(text: str, label: str) -> str | None:
    match = re.search(r'(?:'+label+r'\s*[:：]?\s*'+CLOCK+'|'+CLOCK+r'\s*(?:より\s*)?'+label+')', text)
    if not match:
        return None
    hour, minute = match.groups()[:2] if match[1] else match.groups()[2:]
    return f'{int(hour):02d}:{minute}'


def parse_performance(body: bytes, title: str, url: str, today: date) -> dict | None:
    if body.startswith(b'%PDF-'):
        with pymupdf.open(stream=body, filetype='pdf') as pdf:
            text = '\n'.join(page.get_text() for page in pdf)
        if not text.strip():
            text = POSTERS.get(hashlib.sha256(body).hexdigest(), '')
    else:
        soup = BeautifulSoup(body, 'html.parser')
        article = soup.select_one('article, main')
        if article is None:
            raise ValueError(f'公式告知本文を確認できません: {title}')
        for tag in article.select('time, script, style'):
            tag.decompose()
        text = article.get_text(' ', strip=True)
    text = ' '.join(unicodedata.normalize('NFKC', text).split())
    if re.search(r'(?:開催|公演)を中止|開催中止|公演中止|開催延期', text):
        return None
    dates = set(EVENT_DATE.findall(text))
    if len(dates) != 1:
        raise ValueError(f'開催日を確認できません: {title}')
    when = date(*map(int, dates.pop()))
    if not today <= when <= today+timedelta(days=180):
        return None
    ds = when.isoformat()
    door, start, end = (clocks(text, label) for label in ('開場','開演','終演'))
    return {
        'id':PREFIX+url.rstrip('/').rsplit('/',1)[-1]+'-'+ds, 'title':title, 'date':ds,
        'venue':VENUE, 'venueArea':'福岡町', 'zone':'other', 'source':SOURCE, 'url':url,
        'doorAt':f'{ds}T{door}:00+09:00' if door else None,
        'startAt':f'{ds}T{start}:00+09:00' if start else None,
        'endAt':f'{ds}T{end}:00+09:00' if end else None,
        'endTimeType':'公式掲載の終演時刻' if end else None,
    }


def fetch(session: requests.Session, today: date) -> list[dict]:
    rules = get(session, ORIGIN+'/robots.txt', 500_000, missing_ok=True)
    robot = RobotFileParser()
    robot.parse(rules.decode('utf-8-sig').splitlines() if rules else [])
    def read(url: str, limit: int) -> bytes:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != urlsplit(ORIGIN).netloc or not robot.can_fetch(AGENT,url):
            raise PermissionError(f'公式取得先を確認できません: {url}')
        return get(session, url, limit)
    rows = []
    for url,title in candidates(read(SCHEDULE,2_000_000)).items():
        if not robot.can_fetch(AGENT, url):
            raise PermissionError(f'取得が許可されていません: {url}')
        with session.get(url,timeout=(8,15),allow_redirects=False,stream=True) as response:
            if response.is_redirect:
                target = urljoin(url,response.headers['Location'])
                body = read(target,4_000_000)
            else:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_content(16384):
                    body.extend(chunk)
                    if len(body)>4_000_000:
                        raise ValueError('公式告知が大きすぎます')
        row = parse_performance(bytes(body),title,url,today)
        if row:
            rows.append(row)
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




