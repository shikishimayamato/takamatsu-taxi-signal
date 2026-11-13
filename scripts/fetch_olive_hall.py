#!/usr/bin/env python3
"""高松オリーブホール公式月別ライブ予定を events.json に統合。

URL: https://www.olivehall.net/archives/date/YYYY/MM
安全設計: robots.txt・TLS検証必須。月別公式一覧のみ読む。
公演の終了時刻は掲載されないので推測しない。失敗時は既存施設の催事を保持。
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'events.json'
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
BASE = 'https://www.olivehall.net/'
VENUE = '高松オリーブホール'
SOURCE = '公式・高松オリーブホール'
UA = 'TakamatsuTaxiSignal/1.9 (+public live schedule; twice daily)'
JST = ZoneInfo('Asia/Tokyo')
# 公式の月間アーカイブは「2026.11.14.土」のような表記。
DAY_RX = re.compile(r'^(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?:\s*[.．]\s*[月火水木金土日])?\s*$')
HM = r'((?:[01]?\d|2[0-3]):[0-5]\d)'
DOOR_RX = re.compile(r'(?:開場|OPEN)\s*[:：]?\s*'+HM, re.I)
START_RX = re.compile(r'(?:開演|START)\s*[:：]?\s*'+HM, re.I)
STOP_RX = re.compile(r'公演中止|開催中止|公演延期|開催延期|本公演は中止|本公演は延期|中止となりました|延期となりました')
NOISE_RX = re.compile(r'^(?:Official Website|ARTIST LIVE LIST|Image:|Live Information|このイベントは終了しました|スケジュール|最新情報|予約はこちら|出演者紹介)$',re.I)


def norm(s: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s or '')).strip()


def month_url(now: datetime, offset: int) -> str:
    count = now.year*12 + now.month - 1 + offset
    return BASE + f'archives/date/{count//12:04d}/{count%12+1:02d}'


def eid(day: str, title: str) -> str:
    k = re.sub(r'\s+', '', norm(title))
    return 'olivehall-' + hashlib.sha256(f'{VENUE}|{day}|{k}'.encode()).hexdigest()[:16]


def safe_month_url(url: str) -> bool:
    p=urlparse(url)
    return p.scheme=='https' and p.hostname=='www.olivehall.net' and bool(re.fullmatch(r'/archives/date/20\d{2}/\d{2}/?',p.path))


def is_noise(s: str) -> bool:
    return bool(NOISE_RX.match(s) or s in {'ライブ情報','公式サイト','お知らせ'} or s.startswith('ARTIST LIVE'))


def event_title(lines: list[str]) -> str | None:
    """掲載日時の直前にある出演者名とツアー名だけを使用する。"""
    tail=[norm(v) for v in lines[-6:] if norm(v) and not is_noise(norm(v))]
    if not tail: return None
    chosen=[]
    for s in tail[::-1]:
        # 作品説明や外部サイトの見出しはタイトル候補ではない。
        if len(s)>110 or re.match(r'^(?:お問合せ|チケット|前売|当日|OPEN|開催時間|開場|開演)',s):
            break
        if DAY_RX.fullmatch(s):
            break
        chosen.insert(0,s)
        if len(chosen)>=2: break
    if not chosen: return None
    if len(chosen)==2:
        a,b=chosen
        # 同じ公演名が2回繰り返されたら1回にする。
        if a in b or b in a: chosen=[b if len(b)>len(a) else a]
    title=norm(' / '.join(chosen))
    return title[:110] if 2<=len(title)<=150 else None


def parse_month(html: str, url: str, now: datetime) -> list[dict]:
    soup=BeautifulSoup(html,'html.parser')
    for t in soup(['script','style','nav','footer','noscript']): t.decompose()
    # 公演カレンダーのHTMLは日付と出演者がテキストとして公開されている。
    lines=[norm(x) for x in soup.get_text('\n',strip=True).splitlines()]
    lines=[s for s in lines if s]
    days=[]
    for i,line in enumerate(lines):
        m=DAY_RX.fullmatch(line)
        if not m: continue
        try: d=date(*map(int,m.groups()))
        except ValueError: continue
        days.append((i,d))
    records=[]
    for k,(idx,d) in enumerate(days):
        if not (now.date()-timedelta(days=1) <= d <= now.date()+timedelta(days=120)): continue
        title=event_title(lines[max(0,idx-7):idx])
        if not title or STOP_RX.search(title): continue
        stop_idx=days[k+1][0] if k+1<len(days) else len(lines)
        # 説明内の販売日などを別公演と混同せず、掲載時刻は開催日直後に限定。
        after=lines[idx+1:min(stop_idx,idx+12)]
        post=norm(' '.join(after))
        door=DOOR_RX.search(post)
        start=START_RX.search(post)
        # 次の公演名の『中止』を直前の正常な公演に誤適用しない。
        # 現公演の開催時刻までに書かれた中止・延期通知だけを検出。
        before_time = min([v.start() for v in (door,start) if v] or [min(180,len(post))])
        if STOP_RX.search(post[:before_time]): continue
        # 補助テキストに時刻なしの項目はイベントとして扱わない。
        if not (door or start): continue
        day=d.isoformat()
        dt=lambda m: f"{day}T{int(m.group(1).split(':')[0]):02d}:{m.group(1).split(':')[1]}:00+09:00" if m else None
        rec={
            'id':eid(day,title), 'title':title, 'date':day,
            'venue':VENUE, 'venueArea':'南新町', 'zone':'other',
            'source':SOURCE,'url':url,
            'doorAt':dt(door),'startAt':dt(start),
            'endAt':None,'endTimeType':None,
            'note':'公式月別公演一覧に掲載。終了時刻は未公表。',
        }
        records.append(rec)
    return records


def check_robots(sess: requests.Session, url: str) -> None:
    r=sess.get(BASE+'robots.txt',timeout=(10,15))
    if r.status_code==404: rules=[]
    elif r.status_code==200 and len(r.content)<=500_000: rules=r.text.splitlines()
    else: raise PermissionError(f'robots.txtを確認できません (HTTP {r.status_code})')
    parser=RobotFileParser();parser.parse(rules)
    if not parser.can_fetch(UA,url): raise PermissionError(f'robots.txtで許可されていません: {url}')


class NoEventsMonth(Exception):
    """未掲載の将来月（公式サイトの404）。"""
    pass

def get_month(sess: requests.Session,url: str) -> str:
    if not safe_month_url(url): raise ValueError('予定外のホスト・パスへのアクセスを拒否')
    check_robots(sess,url)
    r=sess.get(url,timeout=(10,25),allow_redirects=True)
    if r.status_code==404:
        raise NoEventsMonth("月間ページ未掲載（404）")
    r.raise_for_status()
    if not safe_month_url(r.url): raise ValueError(f'公式以外のURLに転送: {r.url[:120]}')
    if len(r.content)>2_000_000: raise ValueError('HTMLが大きすぎます')
    if 'html' not in r.headers.get('Content-Type','').lower(): raise ValueError('HTML以外の応答です')
    if 'charset=' not in r.headers.get('Content-Type','').lower():
        # UTF-8を優先し、判別できない場合はBeautifulSoupで変換。
        r.encoding='utf-8'
    return r.text


def fetch(now: datetime) -> tuple[list[dict],list[str]]:
    sess=requests.Session();sess.headers.update({'User-Agent':UA,'Accept-Language':'ja-JP,ja;q=0.9'})
    found=[];fail=[]
    for offset in range(4):
        url=month_url(now,offset)
        try:
            html=get_month(sess,url)
            part=parse_month(html,url,now)
            found.extend(part)
            print(f'オリーブホール公式: {url} → {len(part)}件')
        except NoEventsMonth:
            print(f'オリーブホール公式: {url} → 未掲載（0件）')
        except Exception as exc:
            fail.append(f'{url}: {type(exc).__name__}: {str(exc)[:170]}')
            print('オリーブホール月間取得警告: '+fail[-1],file=sys.stderr)
        time.sleep(.4)
    unique={r['id']:r for r in found}
    if not unique: raise RuntimeError('公式月別ページから公演を抽出できませんでした。'+ '; '.join(fail[:4]))
    return sorted(unique.values(),key=lambda r:(r['date'],r['title'])),fail


def backup(now: datetime) -> list[dict]:
    try:
        r=requests.get(PUBLISHED,params={'v':now.strftime('%Y%m%d%H%M')},timeout=(8,12),headers={'User-Agent':UA})
        r.raise_for_status()
        if len(r.content)>1_500_000:return []
        payload=r.json();ts=datetime.fromisoformat(payload['updatedAt'])
        if ts.tzinfo is None:ts=ts.replace(tzinfo=JST)
        if not timedelta(0)<=now-ts<timedelta(days=7):return []
        return [v for v in payload.get('events',[]) if isinstance(v,dict) and v.get('venue')==VENUE
                and v.get('date','') >= (now.date()-timedelta(days=1)).isoformat()]
    except Exception as exc:
        print('オリーブホール前回データ取得不可: '+type(exc).__name__,file=sys.stderr)
        return []


def merge(payload: dict, rows: list[dict], status: str, now: datetime) -> dict:
    if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
        raise ValueError('既存events.json形式が異なります。更新を中止')
    merged=dict(payload)
    merged['events']=sorted([v for v in payload['events'] if v.get('venue')!=VENUE]+rows,
            key=lambda v:(v.get('date',''),v.get('venue',''),v.get('title','')))
    sources=[v for v in payload.get('sources',[]) if isinstance(v,dict) and v.get('venue')!=VENUE]
    sources.append({'venue':VENUE,'status':status,'count':len(rows),'checkedAt':now.isoformat(timespec='seconds')})
    merged['sources']=sources
    merged['partial']=any(v.get('status') in {'error','partial','stale'} for v in sources)
    if rows and VENUE not in str(merged.get('sourceName','')):
        merged['sourceName']=str(merged.get('sourceName','複数公式サイト'))+'・'+VENUE
    return merged


def main() -> int:
    now=datetime.now(JST)
    if not OUTPUT.exists():
        print('先にfetch_events.pyを実行してください',file=sys.stderr)
        return 1
    payload=json.loads(OUTPUT.read_text(encoding='utf-8'))
    try:
        rows,errors=fetch(now)
        status='partial' if errors else 'ok'
    except Exception as exc:
        print('オリーブホール取得失敗: '+str(exc),file=sys.stderr)
        rows=backup(now)
        status='stale' if rows else 'error'
    merged=merge(payload,rows,status,now)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'オリーブホール統合完了: {len(rows)}件 / {status} / 全施設合計 {len(merged["events"])}件')
    # 残りの施設の取込処理を阻害しない。
    return 0

if __name__=='__main__':
    raise SystemExit(main())
