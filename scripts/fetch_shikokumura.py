#!/usr/bin/env python3
"""四国村ミウゼアムの公式サイトからギャラリーの展覧会を取得する。"""
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

ORIGIN = 'https://www.shikokumura.or.jp'
NEWS = ORIGIN + '/news/'
SCHEDULE = NEWS + '?cat=gallery'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
VENUE = '四国村ミウゼアム'
SOURCE = '公式・四国村ミウゼアム'
PREFIX = 'shikokumura-'
AGENT = 'TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'
JST = timezone(timedelta(hours=9))
DAY = r'(?:(20\d\d)年)?\s*(\d{1,2})月\s*(\d{1,2})日\s*\(([月火水木金土日])(?:曜日)?(?:・祝日?)?\)'
PERIOD = re.compile(DAY + r'\s*[~〜]\s*' + DAY)
CANCEL = re.compile(r'開催(?:を)?(?:中止|延期)|中止のお知らせ|延期のお知らせ')


def parse_article(html, url, published, today):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='utf-8')
    heading = soup.select_one('article.view h1.title')
    body = soup.select_one('article.view .flexible_contents')
    if not heading or not body:
        raise ValueError(f'展覧会のお知らせを確認できません: {url}')
    title = re.search(r'「([^」]+)」', heading.get_text())
    if not title or CANCEL.search(heading.get_text()):
        return None
    spans = set()
    for paragraph in body.select('p'):
        line = unicodedata.normalize('NFKC', paragraph.get_text())
        if CANCEL.search(line):
            return None
        if not re.search(r'会期|開催期間', line):
            continue
        for match in PERIOD.finditer(line):
            a_year, a_month, a_dom, a_week, b_year, b_month, b_dom, b_week = match.groups()
            first = date(int(a_year or published.year), int(a_month), int(a_dom))
            last = date(int(b_year) if b_year else first.year + (int(b_month) < first.month),
                        int(b_month), int(b_dom))
            if first <= last and '月火水木金土日'[first.weekday()] == a_week and '月火水木金土日'[last.weekday()] == b_week:
                spans.add((first, last))
    if len(spans) != 1:
        return None
    first, last = spans.pop()
    if last < today or first > today + timedelta(days=120):
        return None
    post_id = next((value[7:] for value in soup.body.get('class', [])
                    if re.fullmatch(r'postid-\d+', value)), None)
    if post_id is None:
        raise ValueError(f'展覧会の記事番号を確認できません: {url}')
    return {
        'id': PREFIX + post_id, 'date': first.isoformat(), 'endDate': last.isoformat(),
        'title': title[1], 'venue': VENUE, 'venueArea': '屋島中町', 'zone': 'yashima',
        'source': SOURCE, 'url': url, 'doorAt': None, 'startAt': None,
        'endAt': None, 'endTimeType': None,
        'note': '出典：四国村ミウゼアム公式サイト。休村日・観覧時間は公式案内参照。',
    }


def fetch(session, today):
    robots = get(session, ORIGIN + '/robots.txt', 500_000, missing_ok=True)
    rules = robots.decode('utf-8-sig').splitlines() if robots else []
    parser = RobotFileParser()
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
    cards = soup.select('h2.title')
    if not cards:
        raise ValueError('ギャラリーのお知らせ一覧を確認できません')
    rows = {}
    for card in cards:
        text = card.get_text(' ', strip=True)
        if not re.search(r'企画展|展覧会|展示会', text) or re.search(r'休館|クッキー|オープニング|販売|中止|延期', text):
            continue
        posted = card.select_one('.sp_date')
        link = card.find_parent('a', href=True)
        if not posted or not link:
            continue
        published = datetime.strptime(posted.get_text(strip=True), '%Y/%m/%d').date()
        if published < today - timedelta(days=240):
            continue
        url = urljoin(NEWS, link['href'])
        if not re.fullmatch(re.escape(NEWS) + r'[^/?#]+/', url):
            raise ValueError('ギャラリー詳細URLの形式が異なります')
        row = parse_article(read(url), url, published, today)
        if row:
            rows[row['id']] = row
    return list(rows.values())


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
