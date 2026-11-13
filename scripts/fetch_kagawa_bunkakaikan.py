#!/usr/bin/env python3
"""香川県文化会館の公式催物表を既存の events.json に統合する。"""
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

ORIGIN = 'https://www.pref.kagawa.lg.jp'
SCHEDULE = ORIGIN + '/kmuseum/kmuseum/bunkakaikan/kfvn.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '香川県文化会館'
SOURCE = '公式・香川県文化会館'
PREFIX = 'kagawa-bunkakaikan-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
YEAR = r'(?:令和(元|\d{1,2})年|(20\d{2})年)'
DAY = re.compile(r'(?:(?:翌年)|' + YEAR + r')?\s*(\d{1,2})月\s*(\d{1,2})日')
WEEKDAY = re.compile(r'\([月火水木金土日](?:曜日)?\)')
HOUR = r'((?:[01]?\d|2[0-3]))(?:時(?:\s*([0-5]?\d)分)?|:([0-5]\d))'
RANGE = re.compile(HOUR + r'\s*(?:から|[~〜～–―－-])\s*' + HOUR + r'(?:\s*まで)?')
LABELED_TIME = re.compile(r'(開場|開演|開始|終演|終了)\s*[:：]?\s*' + HOUR)
CANCELLED = re.compile(r'(?:開催|公演)?(?:中止|延期)(?:のお知らせ|となりました|します|$)|^\s*[\[【(](?:中止|延期)')
CLOSED = re.compile(r'休館|休廊|休催|休み|開催なし')


def norm(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def year_from(era: str | None, western: str | None) -> int | None:
    if western:
        return int(western)
    if era:
        return 2018 + (1 if era == '元' else int(era))
    return None


def days_from(text: str, months: dict[int, int]) -> list[date]:
    text = WEEKDAY.sub('', text).strip()
    first = DAY.match(text)
    if not first:
        raise ValueError('開催日の表記を確認できません')
    era, western, month, day = first.groups()
    month = int(month)
    year = year_from(era, western) or months.get(month)
    if year is None:
        raise ValueError('開催年を確認できません')
    start = date(year, month, int(day))
    tail = text[first.end():].strip()
    if not tail:
        return [start]
    separator = re.match(r'(?:から|[~〜～–―－-])\s*', tail)
    if not separator:
        raise ValueError('開催日の区切りを確認できません')
    rest = tail[separator.end():]
    end_match = DAY.fullmatch(re.sub(r'\s*まで$', '', rest))
    if not end_match:
        raise ValueError('会期末の日付を確認できません')
    era, western, end_month, end_day = end_match.groups()
    end_month = int(end_month)
    end_year = year_from(era, western) or (year + 1 if rest.startswith('翌年') else months.get(end_month))
    if end_year is None:
        raise ValueError('会期末の年を確認できません')
    span = (date(end_year, end_month, int(end_day)) - start).days
    if not 0 <= span <= 120:
        raise ValueError('会期の範囲を確認してください')
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def clock(parts: tuple) -> str:
    hour, minute, colon_minute = parts
    return f'{int(hour):02d}:{int(minute or colon_minute or 0):02d}'


def times_for(text: str, day: date, days: list[date], gallery: bool) -> tuple:
    # 親括弧内の最終入場・日別注記を、共通時間と混同しない。
    text = WEEKDAY.sub('', text)
    notes = re.findall(r'\(([^()]*)\)', text)
    common = re.sub(r'\([^()]*\)', ' ', text).strip()
    door = start = end = None
    # 「開場時間」の範囲は展示の公開時間として扱う。公演は明示ラベルだけ使う。
    span = RANGE.fullmatch(common)
    if gallery and span:
        start, end = clock(span.groups()[:3]), clock(span.groups()[3:])
    elif gallery:
        single = re.fullmatch(HOUR + r'\s*(から|まで)', common)
        if single:
            if single[4] == 'から':
                start = clock(single.groups()[:3])
            else:
                end = clock(single.groups()[:3])
    for label, *parts in LABELED_TIME.findall(common):
        if label == '開場':
            door = clock(parts)
        elif label in {'開演', '開始'}:
            start = clock(parts)
        else:
            end = clock(parts)
    for note in notes:
        if re.fullmatch(r'(?:最終)?入場は?' + HOUR + r'\s*まで', note):
            continue
        special = re.fullmatch(r'(?:(\d{1,2})月(\d{1,2})日|初日|最終日)は?\s*' + HOUR + r'\s*(から|まで)', note)
        if gallery and special:
            month, dom = special.groups()[:2]
            applies = ((day.month, day.day) == (int(month), int(dom)) if month else
                       day == (days[0] if note.startswith('初日') else days[-1]))
            if applies and special[6] == 'から':
                start = clock(special.groups()[2:5])
            elif applies:
                end = clock(special.groups()[2:5])
        elif re.search(HOUR, note):
            # 未対応の時刻注記がある日は、共通時間の適用を避ける。
            return None, None, None
    if start and end and start >= end:
        end = None
    return door, start, end


def record(title: str, room: str, day: date, times: tuple, gallery: bool) -> dict:
    ds = day.isoformat()
    identity = re.sub(r'\s+', '', norm(f'{room}|{title}'))
    event_id = PREFIX + hashlib.sha256(f'{ds}|{identity}'.encode()).hexdigest()[:16]
    door, start, end = (f'{ds}T{value}:00+09:00' if value else None for value in times)
    return {
        'id': event_id, 'date': ds, 'title': title, 'venue': VENUE, 'venueArea': '番町',
        'zone': 'other', 'source': SOURCE, 'url': SCHEDULE,
        'doorAt': door, 'startAt': start, 'endAt': end,
        'endTimeType': ('公式掲載の開催時間終了' if gallery else '公式掲載の終演時間') if end else None,
        'note': f'会場：{room}。出典：香川県ホームページ（文化会館）。',
    }


def section_records(nodes: list, room: str, months: dict, today: date) -> list[dict]:
    tables = [table for node in nodes for table in ([node] if node.name == 'table' else node.find_all('table'))]
    if not tables:
        if any('掲載を希望した催物はありません' in norm(node.get_text(' ')) for node in nodes):
            return []
        raise ValueError(f'{room}の催物表を確認できません')
    gallery = room == '県民ギャラリー'
    records = []
    for table in tables:
        rows = table.find_all('tr')
        for index, row in enumerate(rows):
            headers = [norm(cell.get_text(' ', strip=True)) for cell in row.find_all(['td', 'th'], recursive=False)]
            title_col = next((i for i, h in enumerate(headers) if h in {'催物名・内容', '催物名', '催事名'}), None)
            if '開催日' in headers and title_col is not None:
                break
        else:
            raise ValueError(f'{room}の表の見出しが変わりました')
        date_col = headers.index('開催日')
        room_col = next((i for i, h in enumerate(headers) if h in {'ギャラリー', '会場', '施設'}), None)
        time_col = next((i for i, h in enumerate(headers) if h in {'開場時間', '開催時間', '時間', '開演時間'}), None)
        if time_col is None:
            raise ValueError(f'{room}の時刻欄を確認できません')
        if not rows[index + 1:]:
            raise ValueError(f'{room}の催物表が空です')
        for row in rows[index + 1:]:
            cells = row.find_all(['td', 'th'], recursive=False)
            if len(cells) != len(headers) or any(cell.has_attr('rowspan') or cell.has_attr('colspan') for cell in cells):
                raise ValueError(f'{room}の表の行が変わりました')
            values = [norm(cell.get_text(' ', strip=True)) for cell in cells]
            title = ' '.join(cells[title_col].get_text(' ', strip=True).split())
            dates, times = values[date_col], values[time_col]
            if any(CANCELLED.search(value) for value in (title, dates, times)):
                continue
            if not title or CLOSED.search(dates + times) or re.search(r'休館|休廊|休催', norm(title)):
                raise ValueError(f'{room}の催物名または休催日を確認してください')
            subroom = values[room_col] if room_col is not None else ''
            location = f'{room} {subroom}' if subroom and subroom in {'2階', '3階', '2階・3階'} else subroom or room
            days = days_from(dates, months)
            records.extend(record(title, location, day, times_for(times, day, days, gallery), gallery)
                           for day in days if today <= day <= today + timedelta(days=120))
    return records


def parse_schedule(html: bytes, today: date) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    content = soup.select_one('#tmp_contents')
    if content is None:
        raise ValueError('公式ページ本文を確認できません')
    heading = next((tag for tag in content.find_all('h2') if '催物情報' in tag.get_text()), None)
    match = re.match(YEAR + r'(.*?)の催物情報', norm(heading.get_text(' '))) if heading else None
    if not match:
        raise ValueError('公式日程の対象月を確認できません')
    year = year_from(*match.groups()[:2])
    months = {}
    previous = 0
    period = re.compile(r'(?:' + YEAR + r'|(翌年))?\s*(\d{1,2})月')
    for era, western, next_year, value in period.findall(match[3]):
        month = int(value)
        explicit = year_from(era, western)
        if explicit:
            year = explicit
        elif next_year or month < previous:
            year += 1
        if not 1 <= month <= 12 or month in months:
            raise ValueError('対象月の表記を確認できません')
        months[month] = year
        previous = month
    if not months:
        raise ValueError('対象月がありません')
    sections = {}
    active = None
    for node in heading.find_next_siblings():
        if node.name == 'h2':
            break
        if node.name == 'h3':
            label = norm(node.get_text(' '))
            active = next((name for name in ('県民ギャラリー', '芸能ホール', '和室')
                           if label.startswith(name)), None)
            if active:
                sections.setdefault(active, [])
        elif active:
            sections[active].append(node)
    if '県民ギャラリー' not in sections or not ({'芸能ホール', '和室'} & sections.keys()):
        raise ValueError('公式日程の会場見出しを確認できません')
    return [row for room, nodes in sections.items() for row in section_records(nodes, room, months, today)]


def get(session: requests.Session, url: str, limit: int, *, missing_ok: bool = False) -> bytes | None:
    with session.get(url, timeout=(8, 15), allow_redirects=False, stream=True) as response:
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ValueError(f'HTTP {response.status_code} ({url})')
        body = bytearray()
        for chunk in response.iter_content(16384):
            body.extend(chunk)
            if len(body) > limit:
                raise ValueError('応答サイズ上限超過')
        return bytes(body)


def fetch(session: requests.Session, today: date) -> list[dict]:
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    if robots is not None:
        rules = robots.decode('utf-8-sig').splitlines()
        parser = RobotFileParser()
        for line in rules:
            field, _, value = line.partition('#')[0].partition(':')
            if field.strip().lower() in {'allow', 'disallow'} and re.search(r'[*$]', value):
                raise PermissionError('robots.txtのパターン規則を要確認')
        parser.parse(rules)
        if not parser.can_fetch(AGENT, SCHEDULE):
            raise PermissionError('robots.txtが公式日程の取得を拒否')
    return parse_schedule(get(session, SCHEDULE, 2_000_000), today)


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
