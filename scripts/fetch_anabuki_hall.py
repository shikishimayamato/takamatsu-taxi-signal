#!/usr/bin/env python3
"""穴吹学園ホール（旧高松テルサ音楽ホール）の公式一般公開公演を収集。

出典: せとうち観光専門職短期大学の穴吹学園ホール イベントガイド。
- 開催日・時刻は公表されたもののみ使用し、終演時刻を予測しない。
- ピアノ練習会・個人練習・予約枠等の不特定多数の集客を伴わない行事を除外。
- events.json へ当会場のみ統合し、他の会場は維持する。
- 取得不能時は新鮮な公開済みデータだけ暫定保持、偽データで置き換えない。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'events.json'
GUIDE_URL = 'https://web.seto.ac.jp/hall-news/8803/'
INDEX_URL = 'https://web.seto.ac.jp/hall-news/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
VENUE = '穴吹学園ホール'
AGENT = 'TakamatsuTaxiSignal/1.6 (+public-event-metadata; twice-daily)'
JST = ZoneInfo('Asia/Tokyo')
DATE_RE = re.compile(r'^\s*[■●]?\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:\([^)]{0,12}\)|（[^）]{0,12}）)?(.*)$')
TIME_RE = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)')
TIME_RANGE = re.compile(r'((?:[01]?\d|2[0-3]):[0-5]\d)\s*[-–―〜～~]\s*((?:[01]?\d|2[0-3]):[0-5]\d)')
SKIP_RE = re.compile(r'ピアノ練習会|個人練習|施設利用|空き状況|予約枠|練習室|レッスン専用', re.I)
ALLOW_HOST = 'web.seto.ac.jp'


def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s or '')).strip()


def utc_date_to_iso(ds: date, hm: str | None) -> str | None:
    return f'{ds.isoformat()}T{hm}:00+09:00' if hm else None


def times_from_heading(h: str) -> tuple[str | None, str | None, str | None]:
    """開催時刻のみ。『～』だけで終わる場合は終演不明。"""
    h = clean(h)
    door_match = re.search(r'(?:開場|開門)\s*[:：]?\s*('+TIME_RE.pattern+')', h)
    door = TIME_RE.search(door_match.group(0)).group(0) if door_match else None
    h2 = re.sub(r'(?:開場|開門|受付)\s*[:：]?\s*(?:[01]?\d|2[0-3]):[0-5]\d', '', h)
    rng = TIME_RANGE.search(h2)
    if rng and rng[1] <= rng[2]:
        return door, rng[1], rng[2]
    # 『午前の部10:30 午後の部13:30』は第一公演の開始時刻のみ使用。
    start_match = re.search(r'(?:開演|開始|開会)\s*[:：]?\s*('+TIME_RE.pattern+')', h2)
    start = TIME_RE.search(start_match.group(0)).group(0) if start_match else None
    if not start:
        first = TIME_RE.search(h2)
        start = first.group(0) if first else None
    ending = re.search(r'(?:終演|終了|閉演)\s*[:：]?\s*('+TIME_RE.pattern+')', h)
    end = TIME_RE.search(ending.group(0)).group(0) if ending else None
    return door, start, end


def id_for(title: str, d: date) -> str:
    title_key = re.sub(r'\s+', '', clean(title))
    return 'anabuki-' + hashlib.sha256(f'{VENUE}|{d.isoformat()}|{title_key}'.encode()).hexdigest()[:16]


def parse_guide(html: str, now: datetime, url: str = GUIDE_URL) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'noscript', 'nav', 'footer']):
        node.decompose()
    # イベント本文は見出しの『■20XX年X月X日』を区切りにする。
    lines = [clean(x) for x in soup.get_text('\n', strip=True).splitlines()]
    lines = [x for x in lines if x]
    sections = []
    current = None
    for line in lines:
        m = DATE_RE.match(line)
        if m:
            if current:
                sections.append(current)
            current = (m, [], line)
        elif current:
            current[1].append(line)
    if current:
        sections.append(current)

    events = {}
    for m, body, heading in sections:
        if not body:
            continue
        title = body[0]
        # 例: 2026年10月12日～2027年3月27日 はピアノ練習会なので取り込まない。
        if SKIP_RE.search(title) or SKIP_RE.search(heading) or not 3 <= len(title) <= 125:
            continue
        if re.search(r'イベントガイド|お問い合わせ先|開催イベント', title):
            continue
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        if not now.date() - timedelta(days=1) <= d <= now.date() + timedelta(days=180):
            continue
        suffix = m[4] or ''
        door, start, end = times_from_heading(suffix)
        # 時刻が見出しに書かれていない場合、直後の開催日案内1行だけで調べる。
        if not start and len(body) > 1 and re.match(r'^(?:開催時間|開演時間|日時)\s*[:：]', body[1]):
            door, start, end = times_from_heading(body[1])
        end = end if start is None or end is None or end >= start else None
        ds = d.isoformat()
        key = id_for(title, d)
        events[key] = {
            'id': key,
            'title': title,
            'date': ds,
            'doorAt': utc_date_to_iso(d, door),
            'startAt': utc_date_to_iso(d, start),
            'endAt': utc_date_to_iso(d, end),
            'endTimeType': '公式掲載の終了時間' if end else None,
            'venue': VENUE,
            'venueArea': '屋島西町',
            'zone': 'other',
            'source': '公式・穴吹学園ホール',
            'url': url,
            'note': '一般公開のホール催事。' + ('公式掲載の終了時刻。' if end else '終演時刻は未公表。'),
        }
    return sorted(events.values(), key=lambda x: (x['date'], x['title']))


def get_html(session: requests.Session, url: str) -> str:
    u = urlparse(url)
    if u.scheme != 'https' or u.hostname != ALLOW_HOST or not u.path.startswith('/hall-news/'):
        raise ValueError('対象外のURLです')
    robots_url = f'{u.scheme}://{u.netloc}/robots.txt'
    r = session.get(robots_url, timeout=(8,15))
    if r.status_code == 404:
        rules = []
    elif r.status_code == 200 and len(r.content) <= 500_000:
        rules = r.text.splitlines()
    else:
        raise RuntimeError(f'robots.txtを確認できません HTTP {r.status_code}')
    p = RobotFileParser(); p.parse(rules)
    if not p.can_fetch(AGENT, url):
        raise PermissionError('robots.txtが収集を許可していません')
    response = session.get(url, timeout=(10,25))
    response.raise_for_status()
    if response.url != url and urlparse(response.url).hostname != ALLOW_HOST:
        raise ValueError('別サイトへの移動がありました')
    if len(response.content) > 2_000_000:
        raise ValueError('公式ページが大きすぎます')
    if 'html' not in response.headers.get('Content-Type','').lower():
        raise ValueError('HTML以外の応答です')
    if not re.search(r'charset=',response.headers.get('Content-Type',''),re.I):
        response.encoding = response.apparent_encoding or 'utf-8'
    return response.text


def fetch_official(now: datetime) -> list[dict]:
    session = requests.Session()
    session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'ja-JP,ja;q=0.9'})
    # 現行の公演ガイドに加え、カテゴリ一覧にある新しい『イベントガイド』を優先できるようにする。
    guide_urls = [GUIDE_URL]
    try:
        html = get_html(session, INDEX_URL)
        soup = BeautifulSoup(html,'html.parser')
        for a in soup.select('a[href]'):
            label = clean(a.get_text(' ',strip=True))
            uri = urljoin(INDEX_URL,a.get('href',''))
            path = urlparse(uri).path
            if 'イベントガイド' in label and '穴吹学園ホール' in label and re.fullmatch(r'/hall-news/\d+/',path) and urlparse(uri).hostname == ALLOW_HOST:
                if uri not in guide_urls:
                    guide_urls.insert(0,uri)
    except Exception as exc:
        print('穴吹学園ホール: 新しいガイドの探索不可（既知の公式ページを確認）: '+type(exc).__name__,file=sys.stderr)
    all_rows = {}
    success = 0
    for uri in guide_urls[:3]:
        try:
            rows = parse_guide(get_html(session,uri),now,uri)
            print(f'穴吹学園ホール: {uri} / {len(rows)}件')
            success += 1
            for row in rows:
                all_rows[row['id']] = row
        except Exception as exc:
            print('穴吹学園ホール: ガイド取得失敗 '+uri+' / '+type(exc).__name__+': '+str(exc)[:160],file=sys.stderr)
    if success == 0:
        raise RuntimeError('公式イベントガイドに接続できません')
    if not all_rows:
        raise ValueError('公式イベントガイドから今後の一般公開催事を抽出できません')
    return sorted(all_rows.values(),key=lambda e:(e['date'],e['title']))


def fallback(now: datetime) -> list[dict]:
    try:
        response = requests.get(PUBLISHED,params={'v':now.strftime('%Y%m%d%H%M')},timeout=(8,12),headers={'User-Agent':AGENT})
        response.raise_for_status()
        if len(response.content) > 1_500_000: return []
        data = response.json()
        age = datetime.fromisoformat(data['updatedAt'])
        if age.tzinfo is None: age = age.replace(tzinfo=JST)
        if not timedelta(0) <= now - age < timedelta(days=7): return []
        return [x for x in data.get('events',[]) if isinstance(x,dict) and x.get('venue') == VENUE
                and x.get('date','') >= (now.date()-timedelta(days=1)).isoformat()]
    except Exception as exc:
        print('穴吹学園ホール: バックアップ利用不可 '+type(exc).__name__,file=sys.stderr)
    return []


def merge(data: dict, events: list[dict], status: str, now: datetime) -> dict:
    if data.get('schema') != 1 or not isinstance(data.get('events'),list):
        raise ValueError('events.jsonの形式が異なります。上書きを中止')
    output = dict(data)
    output['events'] = sorted([x for x in data['events'] if x.get('venue') != VENUE] + events,
                              key=lambda e:(e.get('date',''),e.get('venue',''),e.get('title','')))
    statuses = [x for x in data.get('sources',[]) if isinstance(x,dict) and x.get('venue') != VENUE]
    statuses.append({'venue':VENUE,'status':status,'count':len(events),'checkedAt':now.isoformat(timespec='seconds')})
    output['sources'] = statuses
    output['partial'] = any(x.get('status') in ('error','stale','partial') for x in statuses)
    if events and VENUE not in str(output.get('sourceName','')):
        output['sourceName'] = str(output.get('sourceName','複数公式サイト'))+'・'+VENUE
    return output


def main() -> int:
    now = datetime.now(JST)
    if not OUTPUT.exists():
        print('先にfetch_events.pyを実行してください',file=sys.stderr)
        return 1
    data = json.loads(OUTPUT.read_text(encoding='utf-8'))
    try:
        events = fetch_official(now)
        status = 'ok'
    except Exception as exc:
        print('穴吹学園ホール取得失敗: '+type(exc).__name__+': '+str(exc)[:200],file=sys.stderr)
        events = fallback(now)
        status = 'stale' if events else 'error'
    result = merge(data,events,status,now)
    OUTPUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'穴吹学園ホール統合完了: {len(events)}件 / {status} / 全施設合計 {len(result["events"])}件')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
