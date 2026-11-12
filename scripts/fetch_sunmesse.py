#!/usr/bin/env python3
"""サンメッセ香川の公式公開イベントを取得し、既存 data/events.json に安全に統合する。

先行する scripts/fetch_events.py が更新したイベント情報を残し、サンメッセ分のみ追加。
月別URLの直近3か月を扱う。終了時刻は公式掲載の開催時間であり、実際の退場時刻ではない。
ネットワーク/SSL/robots/ページ仕様異常では推測せずエラー扱いにする。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'events.json'
PRIMARY = 'https://www.sunmesse.com/'
# 2026-11時点、公式サイトと同じ内容を掲載しているサブドメイン。TLS失敗時に限り利用。
TLS_FALLBACK = 'https://pop.sunmesse.com/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
VENUE = 'サンメッセ香川'
AGENT = 'TakamatsuTaxiSignal/1.1 (+official event metadata; twice-daily fetch)'
JST = ZoneInfo('Asia/Tokyo')

DATE_SPAN = re.compile(
    r'(?m)^(20\d{2})\.(\d{1,2})\.(\d{1,2})\([月火水木金土日]\)\s*[-–—~〜～]\s*'
    r'(?:(20\d{2})\.)?(\d{1,2})\.(\d{1,2})\([月火水木金土日]\)\s*$'
)
TIME_RANGE = re.compile(
    r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*[-–—~〜～]\s*'
    r'([01]?\d|2[0-3]):([0-5]\d)(?!\d)'
)
FINAL_DAY = re.compile(r'最終日(?:は)?\s*([01]?\d|2[0-3]):([0-5]\d)\s*まで')
PLACE = re.compile(r'^[○◯]?会場\s*[:：]\s*(.{1,100})$')
HOURS = re.compile(r'^[○◯]?開催時間\s*[:：]\s*(.{1,240})$')


def norm(s: str) -> str:
    return unicodedata.normalize('NFKC', s).strip()


def visible_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'noscript', 'nav', 'footer']):
        node.decompose()
    return [norm(x) for x in soup.get_text('\n', strip=True).splitlines() if norm(x)]


def month_url(origin: str, now: datetime, offset: int) -> str:
    count = now.year * 12 + now.month - 1 + offset
    return f'{origin}event/?date={count//12:04d}{count%12+1:02d}'


def clean_title(lines: list[str]) -> str | None:
    for raw in lines[:12]:
        s = raw.lstrip('・●○◯ ').strip()
        if not s or s.startswith(('一般', '招待', '予約', '無料', '有料')):
            continue
        if s in ('イベント情報','EVENT','ホーム') or re.match(r'^[○◯]?(?:開催時間|会場|対象|主催|問い合わせ)', s):
            continue
        if not 2 <= len(s) <= 150:
            continue
        return s
    return None


def hhmm(m: tuple[str, str]) -> str:
    return f'{int(m[0]):02d}:{m[1]}'


def hours_for_day(raw: str, day: date, last: date, multi: bool) -> tuple[str | None, str | None]:
    """特定日指定の掲載時間を優先する。推測できない日は時刻を設定しない。"""
    if not raw:
        return None, None
    # 受付時間は実際の催事開催時間と混同しない。
    raw = re.sub(r'[（(][^()（）]*受付[^()（）]*[)）]', '', raw)
    per_day: dict[int, tuple[str, str]] = {}
    # 「5日 11:00～18:00 / 6日 10:00～17:00」に対応。
    chunks = re.split(r'\s*[/／]\s*', raw)
    for chunk in chunks:
        date_match = re.search(r'(?<!\d)(\d{1,2})日\s*', chunk)
        interval = TIME_RANGE.search(chunk)
        if date_match and interval:
            per_day[int(date_match[1])] = (hhmm(interval.group(1,2)), hhmm(interval.group(3,4)))
    if per_day:
        return per_day.get(day.day, (None, None))
    ranges = list(TIME_RANGE.finditer(raw))
    if not ranges:
        return None, None
    # 一日で午前・午後の二部なら、最初の開始と最後の終了を採用。
    start, end = hhmm(ranges[0].group(1,2)), hhmm(ranges[-1].group(3,4))
    final = FINAL_DAY.search(raw)
    if multi and final and day == last:
        end = hhmm(final.group(1,2))
    if end < start:
        return None, None
    return start, end


def stable_id(title: str, day: str) -> str:
    cleaned = re.sub(r'[\s\u3000]+', '', unicodedata.normalize('NFKC', title))
    return hashlib.sha256(f'{VENUE}|{cleaned}|{day}'.encode()).hexdigest()[:16]


def parse_page(html: str, url: str, now: datetime) -> list[dict]:
    lines = visible_lines(html)
    text = '\n'.join(lines)
    spans = list(DATE_SPAN.finditer(text))
    events: list[dict] = []
    for i, match in enumerate(spans):
        block = text[match.end():(spans[i+1].start() if i+1<len(spans) else len(text))][:2400]
        rows = [s for s in block.splitlines() if s.strip()]
        title = clean_title(rows)
        if not title:
            continue
        try:
            first = date(int(match[1]),int(match[2]),int(match[3]))
            last_year = int(match[4]) if match[4] else first.year
            last = date(last_year,int(match[5]),int(match[6]))
            if last < first and not match[4]:
                last = date(first.year+1,int(match[5]),int(match[6]))
            if last < first or (last-first).days > 31:
                continue
        except ValueError:
            continue
        hours = ''
        place = ''
        for row in rows:
            h = HOURS.match(row)
            p = PLACE.match(row)
            if h and not hours: hours = h[1]
            if p and not place: place = p[1]
        day = first
        while day <= last:
            if now.date()-timedelta(days=1) <= day <= now.date()+timedelta(days=120):
                ds = day.isoformat()
                start, end = hours_for_day(hours, day, last, first != last)
                events.append({
                    'id':stable_id(title,ds), 'title':title[:110], 'date':ds,
                    'startAt':f'{ds}T{start}:00+09:00' if start else None,
                    'endAt':f'{ds}T{end}:00+09:00' if end else None,
                    'endTimeType':'公式掲載の開催時間終了' if end else None,
                    'venue':VENUE, 'venueArea':'林町', 'zone':'sunmesse',
                    'source':'公式・サンメッセ香川', 'url':url,
                    'note':(f'館内会場：{place}。' if place else '')
                })
            day += timedelta(days=1)
    return events


def robots_ok(session: requests.Session, url: str) -> None:
    origin = f'{urlparse(url).scheme}://{urlparse(url).netloc}'
    r = session.get(origin+'/robots.txt', timeout=15)
    if r.status_code == 404:
        lines = []
    elif r.status_code == 200 and len(r.content) <= 500_000:
        lines = r.text.splitlines()
    else:
        raise PermissionError(f'robots.txtを確認できない: HTTP {r.status_code} {origin}')
    parser = RobotFileParser()
    parser.parse(lines)
    if not parser.can_fetch(AGENT,url):
        raise PermissionError(f'robots.txtで取得が許可されていません: {url}')


def download_month(session: requests.Session, origin: str, now: datetime, offset: int) -> tuple[list[dict],str]:
    url = month_url(origin,now,offset)
    robots_ok(session,url)
    r = session.get(url,timeout=22)
    r.raise_for_status()
    if len(r.content) > 2_000_000: raise ValueError('ページサイズが大きすぎます')
    # 文字コードはHTMLのmeta宣言を優先。認識できなければUTF-8。
    if not re.search(r'charset=',r.headers.get('Content-Type',''),re.I):
        m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?([\w-]+)',r.content[:10000],re.I)
        r.encoding = m[1].decode('ascii') if m else 'utf-8'
    return parse_page(r.text,url,now),url


def fetch_events(now: datetime) -> tuple[list[dict],list[str]]:
    session = requests.Session()
    session.headers.update({'User-Agent':AGENT,'Accept-Language':'ja-JP,ja;q=0.9'})
    all_events = []
    failures = []
    selected_origin = PRIMARY
    for month in range(3):
        try:
            events,url = download_month(session, selected_origin, now, month)
        except requests.exceptions.SSLError as exc:
            if selected_origin != PRIMARY:
                failures.append(f'{month}: TLS ({type(exc).__name__})')
                continue
            # SSL検証を外さず、同じ運営の公式サブドメインを使う。
            try:
                events,url = download_month(session, TLS_FALLBACK, now, month)
                selected_origin = TLS_FALLBACK
            except Exception as backup_exc:
                failures.append(f'{month}: main TLS / backup {type(backup_exc).__name__}')
                continue
        except Exception as exc:
            failures.append(f'{month}: {type(exc).__name__}')
            continue
        all_events.extend(events)
        print(f'サンメッセ：{url} → {len(events)}件')
    unique = {r['id']:r for r in all_events}
    if not unique:
        raise ValueError('公式ページから将来催事を抽出できませんでした。'+', '.join(failures))
    return sorted(unique.values(), key=lambda x:(x['date'],x['title'])), failures


def recent_backup(now: datetime) -> list[dict]:
    try:
        r = requests.get(PUBLISHED,params={'v':now.strftime('%Y%m%d%H%M')},timeout=12,
                         headers={'User-Agent':AGENT})
        r.raise_for_status()
        if len(r.content)>1_000_000:return []
        payload = r.json()
        saved = datetime.fromisoformat(payload['updatedAt'])
        if saved.tzinfo is None: saved = saved.replace(tzinfo=JST)
        if not (timedelta(0)<=now-saved<timedelta(days=7)): return []
        return [e for e in payload.get('events',[]) if isinstance(e,dict) and e.get('venue')==VENUE
                and isinstance(e.get('date'),str) and e['date']>= (now.date()-timedelta(days=1)).isoformat()]
    except Exception as e:
        print('サンメッセ: バックアップ取得不可: '+type(e).__name__,file=sys.stderr)
        return []


def merge_into_feed(payload: dict, records: list[dict], status: str, now: datetime) -> dict:
    if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
        raise ValueError('data/events.json の形式が想定外。上書きを中止します')
    existing = [e for e in payload['events'] if e.get('venue')!=VENUE]
    merged = sorted(existing+records,key=lambda e:(e.get('date',''),e.get('venue',''),e.get('title','')))
    payload['events'] = merged
    statuses = [s for s in payload.get('sources',[]) if isinstance(s,dict) and s.get('venue')!=VENUE]
    statuses.append({'venue':VENUE,'status':status,'count':len(records),'checkedAt':now.isoformat(timespec='seconds')})
    payload['sources'] = statuses
    if records and VENUE not in payload.get('sourceName',''):
        payload['sourceName'] = str(payload.get('sourceName','複数公式サイト'))+'・'+VENUE
    payload['partial'] = any(s.get('status') in ('error','stale','partial') for s in statuses)
    return payload


def main() -> int:
    now = datetime.now(JST)
    # fetch_events.py が成功した後にだけ統合する。何もない初期状態で捏造しない。
    if not OUTPUT.exists():
        print('エラー: data/events.jsonが未生成です。先にfetch_events.pyを実行してください。',file=sys.stderr)
        return 1
    payload = json.loads(OUTPUT.read_text(encoding='utf-8'))
    try:
        records, errors = fetch_events(now)
        status = 'partial' if errors else 'ok'
        if errors:
            print('サンメッセ一部取得失敗: '+', '.join(errors),file=sys.stderr)
    except Exception as exc:
        print('サンメッセ取得失敗: '+str(exc),file=sys.stderr)
        records = recent_backup(now)
        status = 'stale' if records else 'error'
    payload = merge_into_feed(payload,records,status,now)
    OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'サンメッセ統合完了: {len(records)}件 / {status} / 全施設合計 {len(payload["events"])}件')
    # サンメッセだけが失敗しても他施設の収集を止めない。
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
