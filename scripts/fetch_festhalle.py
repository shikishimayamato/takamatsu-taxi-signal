#!/usr/bin/env python3
"""festhalle（高松市常磐町）の一般公開催事を公式月別ページから取得する。

- カレンダーの 08:00〜17:00 等はWordPress管理上の便宜的時刻の可能性がある。
  取得に使うのは公演欄の「OPEN/START」のみ。終演時刻は絶対に推定しない。
- 中止・延期公演、月間日付だけの空欄、個人練習等は除く。
- 既存 data/events.json の当施設分だけ差し替える。他施設は保持。
- HTTPS証明書検証とrobots.txtの確認を省略しない。
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
VENUE = 'festhalle（フェストハレ）'
BASE = 'https://festhalle.jp'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
JST = ZoneInfo('Asia/Tokyo')
AGENT = 'TakamatsuTaxiSignal/1.7 (+official public event metadata; twice daily)'
DATE_LINE = re.compile(r'^(\d{1,2})\s*/\s*(\d{1,2})$')
HM = r'((?:[01]?\d|2[0-3]):[0-5]\d)'
OPEN_START = re.compile(r'OPEN\s*/\s*START\s*[:：]?\s*' + HM + r'\s*/\s*' + HM, re.I)
SKIP = re.compile(r'(?:中止|延期|振替公演調整中|公演見合わせ|練習会|個人利用日)')
IGNORE = re.compile(r'^(?:PRICE|チケット|お問い合わせ|公式サイト|INFO|\[INFO\]|ACCESS|もっと見る|READ MORE)', re.I)
WEEKDAY = re.compile(r'^(?:mon|tue|wed|thu|fri|sat|sun|月|火|水|木|金|土|日|祝)(?:\s*[|｜])?$', re.I)


def norm(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip()


def month_url(now: datetime, offset: int) -> str:
    total = now.year * 12 + now.month - 1 + offset
    return f'{BASE}/schedule/{total//12:04d}-{total%12+1:02d}/'


def stable_id(title: str, day: str) -> str:
    key = re.sub(r'\s+', '', norm(title))
    return 'festhalle-' + hashlib.sha256(f'{VENUE}|{day}|{key}'.encode()).hexdigest()[:16]


def html_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'nav', 'footer']):
        tag.decompose()
    return [norm(x) for x in soup.get_text('\n', strip=True).splitlines() if norm(x)]


def parse_month(html: str, now: datetime, year: int, month: int, url: str) -> list[dict]:
    """公演説明ブロックの OPEN/START を持つ日付行だけ採用。カレンダー時刻は除外。"""
    lines = html_lines(html)
    out = {}
    for i, line in enumerate(lines):
        m = DATE_LINE.fullmatch(line)
        if not m:
            continue
        mm, dd = int(m[1]), int(m[2])
        # ページのカレンダーには前月/翌月の予定も入る。該当月だけ採用。
        if mm != month:
            continue
        try:
            dt = date(year, mm, dd)
        except ValueError:
            continue
        if not (now.date()-timedelta(days=1) <= dt <= now.date()+timedelta(days=120)):
            continue
        # 次の日付／共通フッター／今月ナビゲーションまでにある公演詳細のみ。
        block = []
        for part in lines[i+1:i+42]:
            if DATE_LINE.fullmatch(part):
                break
            if part.lower().startswith(('copyright', 'festhalle=', 'お問い合せ')):
                break
            block.append(part)
        door = start = None
        time_pos = -1
        for j, part in enumerate(block):
            if not part.upper().startswith('OPEN'):
                continue
            # HTMLタグの分割により OPEN/START と時刻が別要素になる場合も扱う。
            tm = OPEN_START.search(' '.join(block[j:j+6]))
            if tm:
                door, start = tm.group(1), tm.group(2)
                time_pos = j
                break
        if time_pos < 0:
            continue
        # Web標準カレンダーの自動時間（08:00 / 17:00）は絶対に利用しない。
        detail = block[:time_pos]
        candidates = []
        for part in detail:
            if DATE_LINE.fullmatch(part) or WEEKDAY.fullmatch(part) or part in {'|', '│'}:
                continue
            if re.fullmatch(r'\d{1,2}/\d{1,2}\s*\([a-z]{3}\)', part, re.I):
                continue
            if part.startswith('OPEN/START') or IGNORE.match(part):
                continue
            if re.fullmatch(r'\d{1,2}:\d{2}\s*[-~〜]\s*\d{1,2}:\d{2}', part):
                continue
            if len(part) >= 2:
                candidates.append(part)
        if not candidates:
            continue
        # 典型構造: 演奏者名→公演タイトル→OPEN/START。タイトルを優先する。
        # 途中に「(sat)」等の曜日説明が混じっても除外される。
        title = ' '.join(candidates[1:]) if len(candidates)>=2 else candidates[0]
        # 公演タイトルが長過ぎるときは文字を切る。中止・延期を含む場合は非掲載。
        context = ' '.join(block[:min(time_pos+2,len(block))])
        if SKIP.search(context):
            continue
        if not (2 <= len(title) <= 180):
            continue
        ds = dt.isoformat()
        ev = {
            'id':stable_id(title,ds), 'title':title[:110], 'date':ds,
            'venue':VENUE, 'venueArea':'常磐町', 'zone':'other',
            'source':'公式・festhalle', 'url':url,
            'doorAt':f'{ds}T{door}:00+09:00',
            'startAt':f'{ds}T{start}:00+09:00',
            'endAt':None, 'endTimeType':None,
            'note':'公式発表の開場・開演時刻。終演時間は未発表。'
        }
        out[ev['id']] = ev
    return sorted(out.values(), key=lambda x:(x['date'], x['title']))


def get_month(session: requests.Session, now: datetime, offset: int) -> tuple[list[dict], str]:
    url = month_url(now, offset)
    r = session.get(url,timeout=(10,24),allow_redirects=True)
    r.raise_for_status()
    final = urlparse(r.url)
    if final.scheme!='https' or final.hostname not in ('festhalle.jp','www.festhalle.jp'):
        raise ValueError('他サイトへリダイレクトされたため取得中止')
    if len(r.content)>2_500_000 or 'html' not in r.headers.get('Content-Type','').lower():
        raise ValueError('HTML形式/容量が不正')
    # HTML metaを尊重。文字コードを誤解釈すると日本語の公演名が壊れる。
    if not re.search(r'charset=', r.headers.get('Content-Type',''), re.I):
        m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?([\w-]+)',r.content[:10000],re.I)
        r.encoding = m[1].decode('ascii') if m else 'utf-8'
    total=now.year*12+now.month-1+offset
    return parse_month(r.text,now,total//12,total%12+1,url),url


def check_robots(session: requests.Session) -> None:
    r=session.get(BASE+'/robots.txt',timeout=(10,15))
    if r.status_code == 404:
        lines=[]
    elif r.status_code == 200 and len(r.content)<500_000:
        lines=r.text.splitlines()
    else:
        raise RuntimeError(f'robots.txtを確認できません (HTTP {r.status_code})')
    rp=RobotFileParser();rp.parse(lines)
    for offset in range(4):
        if not rp.can_fetch(AGENT, month_url(datetime.now(JST),offset)):
            raise PermissionError('robots.txtで公式スケジュール取得が許可されていません')


def fetch(now: datetime) -> tuple[list[dict],str]:
    session=requests.Session()
    session.headers.update({'User-Agent':AGENT,'Accept-Language':'ja-JP,ja;q=0.9'})
    check_robots(session)
    rows=[]; errors=[]; success=0
    for offset in range(4):
        try:
            found,url=get_month(session,now,offset)
            success+=1
            rows.extend(found)
            print(f'フェストハレ公式: {url}: {len(found)}件')
        except Exception as ex:
            errors.append(f'{offset}か月後:{type(ex).__name__}:{str(ex)[:80]}')
    events={e['id']:e for e in rows}
    if not events:
        raise RuntimeError('有効な公演を抽出できません。'+('; '.join(errors)))
    if errors: print('フェストハレ部分取得: '+'; '.join(errors),file=sys.stderr)
    return sorted(events.values(),key=lambda x:(x['date'],x['title'])), ('partial' if errors else 'ok')


def backup(now: datetime) -> list[dict]:
    try:
        r=requests.get(PUBLISHED,params={'v':now.strftime('%Y%m%d%H%M')},timeout=(8,12),headers={'User-Agent':AGENT})
        r.raise_for_status()
        if len(r.content)>1_500_000:return []
        payload=r.json()
        ts=datetime.fromisoformat(payload['updatedAt'])
        if ts.tzinfo is None:ts=ts.replace(tzinfo=JST)
        if not timedelta(0)<=now-ts<timedelta(days=7):return []
        return [e for e in payload.get('events',[]) if e.get('venue')==VENUE and isinstance(e.get('date'),str) and e['date']>=(now.date()-timedelta(days=1)).isoformat()]
    except Exception as ex:
        print(f'フェストハレ前回データ取得不可: {type(ex).__name__}',file=sys.stderr)
        return []


def merge(payload:dict, records:list[dict], status:str, now:datetime)->dict:
    if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
        raise ValueError('既存データ形式が不正。上書きを中止')
    data=dict(payload)
    preserved=[e for e in data['events'] if e.get('venue')!=VENUE]
    data['events']=sorted(preserved+records,key=lambda e:(e.get('date',''),e.get('venue',''),e.get('title','')))
    sources=[s for s in data.get('sources',[]) if isinstance(s,dict) and s.get('venue')!=VENUE]
    sources.append({'venue':VENUE,'status':status,'count':len(records),'checkedAt':now.isoformat(timespec='seconds')})
    data['sources']=sources
    data['partial']=any(s.get('status') in {'stale','partial','error'} for s in sources)
    if records and VENUE not in str(data.get('sourceName','')):
        data['sourceName']=str(data.get('sourceName','複数公式サイト'))+'・'+VENUE
    return data


def main() -> int:
    now=datetime.now(JST)
    if not OUTPUT.exists():
        print('先に scripts/fetch_events.py を実行してください',file=sys.stderr)
        return 1
    payload=json.loads(OUTPUT.read_text(encoding='utf-8'))
    try:
        records,status=fetch(now)
    except Exception as ex:
        print(f'フェストハレ取得失敗: {type(ex).__name__}: {ex}',file=sys.stderr)
        records=backup(now)
        status='stale' if records else 'error'
    data=merge(payload,records,status,now)
    OUTPUT.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'フェストハレ統合完了: {len(records)}件 / {status} / 全施設合計 {len(data["events"])}件')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
