#!/usr/bin/env python3
"""屋島レクザムフィールドの公式行事予定を既存の events.json に統合する。"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser

import hashlib
from calendar import monthrange
from urllib.parse import urljoin

import fitz
import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN = 'https://www.yashima-f.jp'
SCHEDULE = ORIGIN + '/index.php'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '屋島レクザムフィールド'
SOURCE = '公式・屋島レクザムフィールド'
PREFIX = 'yashima-field-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
SPORT = re.compile(r'リーグ|選手権|記録会|競技大会|陸上大会|陸上競技会|陸上カーニバル|カップ|駅伝')
SKIP = re.compile(r'中止|延期|準備|撤収|設営|練習|講習|教室|予備日|工事')


def parse_pdf(body: bytes, month: date, url: str, today: date) -> list[dict]:
    rows = []
    seen = set()
    with fitz.open(stream=body, filetype='pdf') as document:
        if len(document) != 1:
            raise ValueError('月間予定表のページ構成が変わりました')
        tables = document[0].find_tables().tables
        for table in tables:
            cells = table.extract()
            if not cells or table.col_count < 4:
                continue
            heading = re.sub(r'\s+', '', unicodedata.normalize('NFKC', cells[0][0] or ''))
            if not re.match(rf'{month.year}年0?{month.month}月行事予定', heading):
                continue
            if not any(r[0] == '日' and r[1] == '曜日' and '行事' in re.sub(r'\s+', '', r[2] or '') for r in cells[:4]):
                raise ValueError('日付・行事の列が確認できません')
            for row in cells:
                day_number = (row[0] or '').strip()
                if not day_number.isdecimal():
                    continue
                day = month.replace(day=int(day_number))
                if row[1] != '月火水木金土日'[day.weekday()] or day in seen:
                    raise ValueError('行事予定の曜日または日付が一致しません')
                seen.add(day)
                if not today <= day <= today + timedelta(days=120):
                    continue
                # 右側の個人利用欄は競技の開始・終了時刻ではない。
                entries = (row[2] or '').splitlines()
                if entries and re.search(r'リーグ$', entries[0]) and len(entries) > 1 and re.search(r'\bVS\b', entries[1], re.I):
                    entries = [' '.join(entries[:2]), *entries[2:]]
                for entry in entries:
                    title = ' '.join(entry.split())
                    if not SPORT.search(title) or SKIP.search(title):
                        continue
                    ds = day.isoformat()
                    key = hashlib.sha256(f'{ds}|{unicodedata.normalize("NFKC", title)}'.encode()).hexdigest()[:16]
                    rows.append({
                        'id': PREFIX + key, 'date': ds, 'title': title,
                        'venue': VENUE, 'venueArea': '屋島中町', 'zone': 'other',
                        'source': SOURCE, 'url': url, 'doorAt': None, 'startAt': None,
                        'endAt': None, 'endTimeType': None,
                        'note': '出典：屋島レクザムフィールド公式行事予定。試合・競技の時刻は主催者の案内を参照。',
                    })
    if len(seen) != monthrange(month.year, month.month)[1]:
        raise ValueError('公式予定表の日付をすべて確認できません')
    return rows


def fetch(session: requests.Session, today: date) -> list[dict]:
    rules = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    lines = rules.decode('utf-8-sig').splitlines() if rules is not None else []
    for line in lines:
        field, _, value = line.partition('#')[0].partition(':')
        if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
            raise PermissionError('robots.txtのパターン規則を要確認')
    parser.parse(lines)

    def read(url: str) -> bytes:
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return get(session, url, 4_000_000)

    soup = BeautifulSoup(read(SCHEDULE), 'html.parser', from_encoding='utf-8')
    months = {}
    for link in soup.select('a[href]'):
        label = re.sub(r'\s+', '', unicodedata.normalize('NFKC', link.get_text()))
        match = re.fullmatch(r'令和(元|\d{1,2})年(\d{1,2})月の行事予定カレンダー\(PDF\)', label)
        if not match:
            continue
        month = date(2018 + (1 if match[1] == '元' else int(match[1])), int(match[2]), 1)
        if month < today.replace(day=1) or month > today + timedelta(days=120):
            continue
        url = urljoin(SCHEDULE, link['href'])
        if not re.fullmatch(re.escape(ORIGIN) + r'/edit/pdf/gyouji[\w-]+\.pdf', url):
            raise ValueError('公式PDFのURL形式が変わりました')
        if month in months and months[month] != url:
            raise ValueError('同じ月の予定表が複数あります')
        months[month] = url
    if not months:
        raise ValueError('対象月の公式予定表が確認できません')
    return [row for month, url in months.items() for row in parse_pdf(read(url), month, url, today)]


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
