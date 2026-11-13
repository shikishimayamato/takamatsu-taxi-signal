#!/usr/bin/env python3
"""香川県水泳協会の公式競技会日程から、香川県立総合水泳プール開催の大会だけ取得する。

URL: https://kagawa-swim.com/tournament_prefecture/
・一般利用日、教室、広告ではなく、競技会日程表の「大会」のみ対象。
・公表されていない開場・開始・終了時刻や人数は推測しない。
・既存の data/events.json に当施設のデータのみ統合。失敗はログに明記。
・SSL検証と robots.txt 確認を省略しない。公式サーバの障害は迂回しない。
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
INDEX = 'https://kagawa-swim.com/tournament_prefecture/'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
VENUE = '香川県立総合水泳プール'
SOURCE = '香川県水泳協会・競技会日程'
JST = ZoneInfo('Asia/Tokyo')
AGENT = 'TakamatsuTaxiSignal/1.5 (+public meet dates; twice-daily metadata fetch)'
DATES = re.compile(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
SKIP_TITLE = re.compile(r'(?:個人(?:使用|利用)|レッスン|教室|予約状況|スクール|練習会|休館|記録一覧|過去の大会)', re.I)


def norm(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip()


def parsed_days(value: str) -> list[date]:
    """公式表の複数日大会を日ごとに展開する。過大な期間は誤抽出として拒否。"""
    dates = []
    for y, m, d in DATES.findall(norm(value)):
        try:
            dates.append(date(int(y), int(m), int(d)))
        except ValueError:
            return []
    if not dates:
        return []
    if len(dates) == 1:
        return dates
    first, last = dates[0], dates[-1]
    if last < first or (last-first).days > 3:
        return []
    return [first + timedelta(days=i) for i in range((last-first).days+1)]


def event_id(title: str, day: str) -> str:
    compact = re.sub(r'\s+', '', norm(title))
    return 'swim-' + hashlib.sha256(f'{VENUE}|{day}|{compact}'.encode('utf-8')).hexdigest()[:16]


def parse_html(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    found: dict[str,dict] = {}
    for tr in soup.select('table tr'):
        cells = tr.find_all(['th', 'td'], recursive=False)
        if len(cells) < 3:
            continue
        when, title, location = [norm(c.get_text(' ', strip=True)) for c in cells[:3]]
        if VENUE not in location:
            continue
        if not 3 <= len(title) <= 145 or SKIP_TITLE.search(title):
            continue
        # 協会の「競技会情報」表だけを対象にする。大会名でない欄は読み飛ばす。
        if not re.search(r'大会|選手権|記録会|競技会|競技|チャレンジ|グランプリ|カップ', title):
            continue
        for day in parsed_days(when):
            if not (now.date()-timedelta(days=1) <= day <= now.date()+timedelta(days=120)):
                continue
            ds = day.isoformat()
            row = {
                'id':event_id(title, ds),
                'title':title, 'date':ds, 'venue':VENUE,
                'venueArea':'御厩町', 'zone':'other',
                'source':SOURCE, 'url':INDEX,
                'doorAt':None, 'startAt':None, 'endAt':None,
                'endTimeType':None,
                'note':'香川県水泳協会の大会日程。',
            }
            found[row['id']] = row
    return sorted(found.values(), key=lambda e:(e['date'],e['title']))


def download_html() -> str:
    session = requests.Session()
    session.headers.update({'User-Agent':AGENT,'Accept-Language':'ja-JP,ja;q=0.9'})
    u = urlparse(INDEX)
    if u.scheme != 'https' or u.netloc != 'kagawa-swim.com':
        raise ValueError('取得先が想定外です')
    robots = session.get(f'{u.scheme}://{u.netloc}/robots.txt', timeout=(10,15))
    if robots.status_code == 404:
        rules = []
    elif robots.status_code == 200 and len(robots.content) < 500_000:
        rules = robots.text.splitlines()
    else:
        raise RuntimeError(f'robots.txtが確認できません HTTP {robots.status_code}')
    rp = RobotFileParser(); rp.parse(rules)
    if not rp.can_fetch(AGENT, INDEX):
        raise PermissionError('robots.txtが大会情報の自動取得を許可していません')
    r = session.get(INDEX, timeout=(10,25), allow_redirects=True)
    r.raise_for_status()
    actual = urlparse(r.url)
    if actual.scheme != 'https' or actual.hostname != u.hostname:
        raise ValueError('予期しないドメインへの転送を検出したため中止')
    if len(r.content) > 2_000_000:
        raise ValueError('HTMLが2MBを超えています')
    if 'html' not in r.headers.get('Content-Type','').lower():
        raise ValueError('取得結果がHTMLではありません')
    if 'charset=' not in r.headers.get('Content-Type','').lower():
        r.encoding = r.apparent_encoding or 'utf-8'
    return r.text


def previous_records(now: datetime) -> list[dict]:
    try:
        r = requests.get(PUBLISHED, timeout=(8,12),params={'v':now.strftime('%Y%m%d%H%M')},headers={'User-Agent':AGENT})
        r.raise_for_status()
        if len(r.content)>1_500_000:
            return []
        payload = r.json()
        if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
            return []
        latest = datetime.fromisoformat(payload['updatedAt'])
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=JST)
        if not (timedelta(0) <= now-latest < timedelta(days=7)):
            return []
        return [e for e in payload['events'] if isinstance(e,dict) and e.get('venue')==VENUE
                and isinstance(e.get('date'),str) and e['date']>=(now.date()-timedelta(days=1)).isoformat()]
    except Exception as exc:
        print('水泳大会: 前回公開データ参照不可 '+type(exc).__name__, file=sys.stderr)
        return []


def merge(payload: dict, records: list[dict], status: str, now: datetime) -> dict:
    if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
        raise ValueError('events.json が想定外の形式のため、上書きを中止します')
    changed = dict(payload)
    prior = [e for e in changed['events'] if isinstance(e,dict) and e.get('venue')!=VENUE]
    changed['events'] = sorted(prior + records, key=lambda e:(e.get('date',''),e.get('venue',''),e.get('title','')))
    statuses = [s for s in changed.get('sources',[]) if isinstance(s,dict) and s.get('venue')!=VENUE]
    statuses.append({'venue':VENUE,'status':status,'count':len(records),'checkedAt':now.isoformat(timespec='seconds')})
    changed['sources'] = statuses
    changed['partial'] = any(s.get('status') in ('error','stale','partial') for s in statuses)
    if records and VENUE not in str(changed.get('sourceName','')):
        changed['sourceName'] = str(changed.get('sourceName','複数公式サイト'))+'・'+VENUE
    return changed


def main() -> int:
    now=datetime.now(JST)
    if not OUTPUT.exists():
        print('水泳大会: events.json がありません。既存の fetch_events.py を先に実行してください。',file=sys.stderr)
        return 1
    try:
        data=json.loads(OUTPUT.read_text(encoding='utf-8'))
        rows=parse_html(download_html(),now)
        if not rows:
            raise RuntimeError('今後120日間の大会を抽出できません（掲載内容を確認）')
        state='ok'
    except Exception as exc:
        print(f'水泳大会取得失敗: {type(exc).__name__}: {str(exc)[:240]}',file=sys.stderr)
        rows=previous_records(now)
        state='stale' if rows else 'error'
    merged=merge(data,rows,state,now)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'水泳大会統合完了: {len(rows)}件 / {state} / 全施設合計 {len(merged["events"])}件')
    # 既存施設の自動更新を阻害しない（取得失敗はsourcesとログに明示）。
    return 0

if __name__=='__main__':
    raise SystemExit(main())
