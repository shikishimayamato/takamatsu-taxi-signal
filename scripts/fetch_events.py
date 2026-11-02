#!/usr/bin/env python3
"""あなぶきアリーナ香川の公開催事の事実を取得する軽量試作。

開催日・開演時刻のみを抽出する。終演時刻を推測してはならない。
サイトの利用規約、robots.txt、掲載形態によっては取得できない。
取得に失敗した場合は既存の保存データを壊さない。
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo('Asia/Tokyo')
ROOT = 'https://kagawa-arena.com/'
OUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
USER_AGENT = 'TakamatsuTaxiSignal/0.2 (non-commercial event metadata demo; source links; low frequency)'
ROBOTS = None
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'ja-JP,ja;q=0.9'})


def respect_robots(url: str) -> bool:
    global ROBOTS
    rp = ROBOTS
    try:
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(urljoin(ROOT, 'robots.txt'))
            rp.read()
            ROBOTS = rp
        return rp.can_fetch(USER_AGENT, url)
    except Exception as exc:
        print(f'robots.txt確認失敗: {exc}', file=sys.stderr)
        return False  # fail closed


def get_html(url: str) -> str:
    if not url.startswith(ROOT):
        raise ValueError('許可されていないドメイン')
    if not respect_robots(url):
        raise PermissionError(f'robots.txt未確認またはアクセス不可: {url}')
    resp = SESSION.get(url, timeout=18)
    resp.raise_for_status()
    if len(resp.content) > 2_000_000:
        raise ValueError('ページ容量が大きすぎます')
    return resp.text


def discover_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    result: list[str] = []
    for link in soup.select('a[href]'):
        url = urljoin(ROOT, link.get('href', ''))
        if re.fullmatch(r'https://kagawa-arena\.com/event/\d+/?', url) and url not in result:
            result.append(url)
    return result[:35]


def event_dates(text: str, now: datetime) -> list[date]:
    """サイト本文中の短い掲載期間から開催日候補のみを拾う。"""
    found: list[date] = []
    for y, m, d in re.findall(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日', text):
        try:
            dt = date(int(y), int(m), int(d))
            if now.date() - timedelta(days=2) <= dt <= now.date() + timedelta(days=100) and dt not in found:
                found.append(dt)
        except ValueError:
            pass
    # site header: 11.21土 11.22日 / 12.5土 12.6日
    if not found:
        for m, d in re.findall(r'(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)', text[:600]):
            for year in (now.year, now.year + 1):
                try:
                    dt = date(year, int(m), int(d))
                    if now.date() - timedelta(days=2) <= dt <= now.date() + timedelta(days=100) and dt not in found:
                        found.append(dt)
                except ValueError:
                    continue
                if dt in found:
                    break
    return sorted(found)[:10]


def start_times(text: str) -> dict[str, str]:
    """YYYY-MM-DD -> HH:MM。確認できる『開演』『開始』だけ。"""
    starts: dict[str, str] = {}
    pat = r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日[^\n]{0,45}?(?:開演|試合開始|開始)\s*[/：:\s]\s*([01]?\d|2[0-3]):([0-5]\d)'
    for y, m, d, h, minute in re.findall(pat, text):
        try:
            dt = date(int(y), int(m), int(d))
            starts[dt.isoformat()] = f'{int(h):02d}:{minute}'
        except ValueError:
            continue
    return starts


def parse_event(html: str, url: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    h = soup.find('h1')
    if not h:
        return []
    title = re.sub(r'\s+', ' ', h.get_text(' ', strip=True)).strip()
    if not title or len(title) > 160:
        return []
    # The first 1,600 chars contain the event's date/start time; footer and nav are ignored.
    text = soup.get_text('\n', strip=True)
    start = text.find(title)
    relevant = text[max(0, start-120):start+1600] if start >= 0 else text[:1600]
    dates = event_dates(relevant, now)
    times = start_times(relevant)
    records = []
    for day in dates:
        daystr = day.isoformat()
        hhmm = times.get(daystr)
        records.append({
            'id': hashlib.sha256((url + '|' + daystr).encode()).hexdigest()[:16],
            'title': title[:110],
            'date': daystr,
            'startAt': daystr + 'T' + hhmm + ':00+09:00' if hhmm else None,
            'endAt': None,  # No assumption about the finish time
            'venue': 'あなぶきアリーナ香川',
            'zone': 'station',
            'source': '公式・あなぶきアリーナ香川',
            'url': url,
            'note': '終演・終了時刻は未確認。会場周辺の駐停車規制に注意。',
        })
    return records


def main() -> int:
    now = datetime.now(JST)
    try:
        home = get_html(ROOT)
        links = discover_links(home)
        print(f'催事詳細へのリンク: {len(links)} 件')
        if not links:
            raise RuntimeError('催事リンクが見つかりません。サイトの構造を確認してください。')
        records: list[dict] = []
        errs = []
        for url in links:
            try:
                time.sleep(0.8)
                records.extend(parse_event(get_html(url), url, now))
            except Exception as exc:
                errs.append(f'{url}: {exc}')
        if not records:
            raise RuntimeError('有効な開催日が見つかりません。旧データを維持します。')
        uniq = {r['id']: r for r in records}
        rows = sorted(uniq.values(), key=lambda r: (r['date'], r['title']))[:160]
        payload = {
            'schema': 1,
            'updatedAt': now.isoformat(timespec='seconds'),
            'source': ROOT,
            'sourceName': 'あなぶきアリーナ香川（公式）',
            'verifiedFinishTime': False,
            'events': rows,
            'partial': bool(errs),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print(f'保存: {OUT} / {len(rows)} 件')
        for err in errs:
            print('注意：' + err, file=sys.stderr)
        return 0
    except Exception as exc:
        print('取得失敗: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
