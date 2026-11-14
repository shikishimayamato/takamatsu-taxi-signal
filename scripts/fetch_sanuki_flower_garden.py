#!/usr/bin/env python3
"""さぬきフラワーガーデンの公式サイトから公開催事を取得する。"""
from __future__ import annotations

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
ROOT = ORIGIN + '/engeicenter/'
SCHEDULE = ROOT + 'index.html'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = 'さぬきフラワーガーデン'
SOURCE = '公式・さぬきフラワーガーデン'
PREFIX = 'sanuki-flower-garden-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
DAY = r'(?:(令和\d+|20\d\d)年)?(\d{1,2})月(\d{1,2})日\(([月火水木金土日])曜日(?:・祝日?)?\)'
CLOCK = r'(午前|午後)(\d{1,2})時(?:(\d{1,2})分)?'
PERIOD = re.compile(DAY + r'(?:から|[~〜])' + DAY)
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|中止となりました|延期となりました|開催しません')


def compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def day_from(parts, default=None):
    year, month, dom, weekday = parts
    if year is None and default is None:
        raise ValueError('公式開催年が確認できません')
    year = default if year is None else (2018 + int(year[2:]) if year.startswith('令和') else int(year))
    day = date(year, int(month), int(dom))
    if '月火水木金土日'[day.weekday()] != weekday:
        raise ValueError('開催日と曜日が一致しません')
    return day


def clock(parts):
    period, hour, minute = parts
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise ValueError('公式時刻を確認できません')
    return f'{hour % 12 + (12 if period == "午後" else 0):02d}:{minute:02d}'


def parse_article(html, url, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    main = soup.select_one('#tmp_contents')
    heading = main.find('h1') if main else None
    if not heading:
        raise ValueError(f'公式催事の本文を確認できません: {url}')
    title = heading.get_text(' ', strip=True)
    if re.search(r'中止|延期', title) or CANCEL.search(main.get_text()):
        return []
    title = re.sub(r'(?:を開催します|の参加者を募集します)[。！!]*$', '', title)
    title = re.sub(r'^さぬきフラワーガーデン', '', title).strip()
    if title.startswith('「') and title.endswith('」'):
        title = title[1:-1]
    fields = {}
    for h in main.find_all('h2'):
        p = h.find_next_sibling()
        if p is not None and p.name == 'p':
            fields[compact(h.get_text())] = compact(p.get_text())
    dates = next((fields[key] for key in ('開催期間', '開催日', '日付', '開催日時', '日時') if key in fields), '')
    span = PERIOD.search(dates)
    if span:
        first = day_from(span.groups()[:4])
        last = day_from(span.groups()[4:], first.year + (int(span[6]) < first.month))
        if last < first:
            raise ValueError('開催期間の順序が不正です')
        periods = [(first, last)]
    else:
        periods = [(day, day) for m in re.finditer(DAY, dates) if m[1] and (day := day_from(m.groups()))]
    times = fields.get('開催時間', fields.get('時間', ''))
    hours = re.fullmatch(CLOCK + r'(?:から|[~〜])' + CLOCK + r'(?:まで)?(?:\(受付開始:[^()]+\))?', times)
    start = clock(hours.groups()[:3]) if hours else None
    end = clock(hours.groups()[3:]) if hours else None
    if end and end <= start:
        end = None
    rows = []
    for first, last in periods:
        if last < today or first > today + timedelta(days=120):
            continue
        ds = first.isoformat()
        key = url.rsplit('/', 1)[-1].removesuffix('.html')
        row = {
            'id': f'{PREFIX}{key}-{ds}', 'date': ds, 'title': title, 'venue': VENUE,
            'venueArea': '香南町岡', 'zone': 'airport', 'source': SOURCE, 'url': url,
            'doorAt': None, 'startAt': f'{ds}T{start}:00+09:00' if start else None,
            'endAt': f'{last}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の開催時間終了' if end else None,
            'note': '出典：さぬきフラワーガーデン公式のお知らせ。受付・参加条件は公式詳細参照。',
        }
        if first != last:
            row['endDate'] = last.isoformat()
        rows.append(row)
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

    soup = BeautifulSoup(read(SCHEDULE), 'html.parser', from_encoding='utf-8')
    links = soup.select('#tmp_main .tab_info_link a[href]')
    if not links:
        raise ValueError('施設公式のお知らせ一覧を確認できません')
    selected = {}
    for link in links:
        title = link.get_text(strip=True)
        if re.search(r'を開催します|の参加者を募集します', title) and not re.search(r'委託|公告|公募', title):
            url = urljoin(SCHEDULE, link['href'])
            if re.fullmatch(re.escape(ROOT) + r'[\w-]+\.html', url):
                selected[url] = None
    return [row for url in selected for row in parse_article(read(url), url, today)]


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
