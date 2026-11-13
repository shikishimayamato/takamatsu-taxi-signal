#!/usr/bin/env python3
"""ゆめタウン高松の公式一覧から来場型催事を取り込む。"""
from __future__ import annotations

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

ORIGIN = 'https://www.izumi.jp'
LIST = '/tenpo/takamatsu/event'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'ゆめタウン高松'
SOURCE = '公式・ゆめタウン高松'
PREFIX = 'youme-takamatsu-'
JST = timezone(timedelta(hours=9))
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
PUBLIC = re.compile(r'ステージ|フェス|プレコン|コンサート|ライブ|演奏|展示会|作品展|体験|ワークショップ|作ろう|イベント|えいごあそび|できるかなチェック|すくい|マルシェ|講演|上映')
EXCLUDE = re.compile(r'キャンペーン|抽選会|プレゼント|貸切|福袋|感謝祭|セール|お買得|中止|延期|POP.?UP', re.I)
DATE = re.compile(r'(20\d{2})\.(\d{1,2})\.(\d{1,2})~(?:(20\d{2})\.)?(\d{1,2})\.(\d{1,2})')
CLOCK = r'((?:[01]?\d|2[0-3]))(?:[:：]([0-5]\d)|時(?:([0-5]?\d)分)?)'
SESSION = re.compile(CLOCK + r'\s*[~〜～]\s*(?:' + CLOCK + r')?')
GENERIC = re.compile(r'(?:\d{1,2}/\d{1,2}(?:\([月火水木金土日]\))?\s*)?(?:イベント|催事)(?:の)?お知らせ')
# 名称が公式チラシ画像にのみ掲載されている催事。
VERIFIED_TITLES = {'20261030-612348': '名探偵プリキュア！ショー'}


def norm(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def period(value: str) -> tuple[date, date] | None:
    match = DATE.fullmatch(norm(value).replace('〜', '~'))
    if not match:
        return None
    y, m, d, ey, em, ed = match.groups()
    start = date(int(y), int(m), int(d))
    end = date(int(ey or (int(y) + (int(em) < int(m)))), int(em), int(ed))
    if end < start:
        raise ValueError('掲載日程の終了日が開始日より前です')
    return start, end


def sessions(soup: BeautifulSoup, span: tuple[date, date]) -> list[tuple]:
    content = soup.select_one('article .s_articleMain')
    if content is None:
        raise ValueError('催事詳細の本文を確認できません')
    lines = [norm(line) for line in content.get_text('\n', strip=True).splitlines()]
    if any(re.search(r'開催を中止|開催中止|開催を延期|開催延期|中止となりました', line) for line in lines):
        return []
    if span[0] != span[1]:
        return [(None, None)]
    for line in lines:
        label = re.match(r'^●?\s*(?:開催時間|時間|とき)\s*:?\s*(.*)$', line)
        if not label:
            continue
        parts = [part.strip(' /、,') for part in re.split(r'\(\d+\)', label[1]) if part.strip(' /、,')]
        matches = [SESSION.fullmatch(part) for part in parts]
        if not matches or any(match is None for match in matches):
            return [(None, None)]

        def time(groups: tuple) -> str | None:
            hour, minute, japanese_minute = groups
            return f'{int(hour):02d}:{int(minute or japanese_minute or 0):02d}' if hour else None

        found = []
        for match in matches:
            start, end = time(match.groups()[:3]), time(match.groups()[3:])
            found.append((start, end if end and start < end else None))
        return list(dict.fromkeys(found))
    return [(None, None)]


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    parser = RobotFileParser()
    rules = robots.decode('utf-8-sig').splitlines() if robots is not None else []
    for line in rules:
        field, _, value = line.partition('#')[0].partition(':')
        value = value.strip()
        if field.strip().lower() == 'disallow' and re.search(r'[*$]', value):
            literal = re.split(r'[*$]', value)[0]
            if LIST.startswith(literal) or literal.startswith(LIST):
                raise PermissionError('催事パスのrobots.txt規則を要確認')
    parser.parse(rules)

    def html(path: str) -> BeautifulSoup:
        url = ORIGIN + path
        if not parser.can_fetch(AGENT, url):
            raise PermissionError(f'robots.txtが取得を拒否: {url}')
        return BeautifulSoup(get(session, url, 2_000_000), 'html.parser', from_encoding='utf-8')

    page = 0
    candidates = {}
    while True:
        soup = html(LIST + (f'?page={page}' if page else ''))
        cards = soup.select('li.s_eventListItem')
        if not cards or not soup.select_one('h1.s_contentHeader__title'):
            raise ValueError('公式催事一覧の構造を確認できません')
        for card in cards:
            link = card.select_one('.f_title a[href]')
            stamp = card.find('time')
            if not link or not stamp:
                raise ValueError('公式催事一覧のタイトルまたは日付欄が見つかりません')
            title = ' '.join(link.get_text(' ', strip=True).split())
            if EXCLUDE.search(norm(title)) or not PUBLIC.search(norm(title)):
                continue
            dates = period(stamp.get_text(' ', strip=True))
            if dates is None or dates[1] < today or dates[0] > today + timedelta(days=120):
                continue
            path = re.sub(r'\s+', '', link['href'])
            if not re.fullmatch(LIST + r'/20\d{6}-\d+', path):
                raise ValueError('公式催事リンクの形式が変わりました')
            candidates[path] = (title, dates)
        next_page = soup.select_one('.pager a[rel=next]')
        if not next_page:
            break
        page += 1
        if next_page.get('href') != f'?page={page}' or page >= 8:
            raise ValueError('公式一覧の次ページを確認できません')
    if len(candidates) > 30:
        raise ValueError('公式催事の詳細取得件数が上限を超えました')

    rows = []
    for path, (title, dates) in candidates.items():
        detail = html(path)
        if not detail.select_one('article h1'):
            raise ValueError('公式催事詳細の見出しを確認できません')
        if GENERIC.fullmatch(norm(title)):
            title = VERIFIED_TITLES.get(path.rsplit('/', 1)[-1], '催事名未掲載（公式詳細参照）')
        slots = sessions(detail, dates)
        day = max(dates[0], today)
        post_id = path.rsplit('-', 1)[-1]
        while day <= min(dates[1], today + timedelta(days=120)):
            ds = day.isoformat()
            for index, (start, end) in enumerate(slots, 1):
                rows.append({
                    'id': f'{PREFIX}{post_id}-{ds}-{index}', 'date': ds, 'title': title,
                    'venue': VENUE, 'venueArea': '三条町', 'zone': 'other', 'source': SOURCE,
                    'url': ORIGIN + path, 'doorAt': None,
                    'startAt': f'{ds}T{start}:00+09:00' if start else None,
                    'endAt': f'{ds}T{end}:00+09:00' if end else None,
                    'endTimeType': '公式掲載の開催時間終了' if end else None,
                    'note': '出典：ゆめタウン高松公式サイト。会場内の場所と時刻は公式詳細を参照。',
                })
            day += timedelta(days=1)
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
