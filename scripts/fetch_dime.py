#!/usr/bin/env python3
"""高松DIMEの公式公演情報を既存data/events.jsonへ統合する。

対象: https://www.dime-dime.com/cgi-bin/schedule.cgi?Sdate=YYYYMM
- 直近4か月。延期・中止・『ライブ予定』等の仮告知は除外。
- 開場・開演のみ、終演時刻は公表がない限り推定しない（常にnull）。
- 複数日公演は日別に分け、各日固有の時刻がなければ時刻は不明。
- 証明書検証とrobots.txtを尊重。取得不能時には他施設を消さない。
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
PUBLISHED = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
BASE = 'https://www.dime-dime.com'
PATH = '/cgi-bin/schedule.cgi'
VENUE = '高松DIME'
AGENT = 'TakamatsuTaxiSignal/1.8 (+public-show-metadata; twice daily)'
JST = ZoneInfo('Asia/Tokyo')

# The website includes a calendar, followed by the detailed performances.
# Anchor exclusively to MM/DD(weekday) followed by a quoted event title.
HEADER_RX = re.compile(
    r'(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})\s*\([A-Z]{2,4}\)'
    r'(?:\s*[～~〜–—-]\s*(?P<month2>\d{1,2})/(?P<day2>\d{1,2})\s*\([A-Z]{2,4}\))?'
    r'\s*[「｢](?P<title>[^」｣\n]{2,140})[」｣]', re.I,
)
HM = r'((?:[01]?\d|2[0-3]):[0-5]\d)'
OPEN_RX = re.compile(r'(?:\bopen\b|\bpen\b|開場(?:・パス交換)?|開門)\s*[:：]?\s*'+HM, re.I)
START_RX = re.compile(r'(?:\bstart\b|開演|開始)\s*[:：]?\s*'+HM, re.I)
JP_OPEN_AFTER_RX = re.compile(HM+r'\s*開場')
JP_START_AFTER_RX = re.compile(HM+r'\s*開演')
DAY_RX = re.compile(r'(?<!\d)(?:(?P<month>\d{1,2})\s*月\s*)?(?P<day>\d{1,2})\s*日\s*(?:[（(][^()（）]{0,8}[）)])?')
UNAVAILABLE_RX = re.compile(r'公演延期|公演中止|開催延期|開催中止|中止のお知らせ|延期のお知らせ|延期となりました|中止となりました|公演見合わせ')
PLACEHOLDER_RX = re.compile(r'^(?:ライブ予定|公演予定|未定|調整中|休館日|店休日|貸切予定)$')


def norm(s: str) -> str:
    return re.sub(r'[ \t\u3000]+',' ',unicodedata.normalize('NFKC',s or '')).strip()


def page_url(year: int, month: int) -> str:
    return f'{BASE}{PATH}?Sdate={year:04d}{month:02d}'


def month_shift(now: datetime, offset: int) -> tuple[int,int]:
    k = now.year*12 + now.month - 1 + offset
    return k//12, k%12+1


def parse_times(s: str) -> tuple[str|None,str|None]:
    s = norm(s)
    opening = OPEN_RX.search(s)
    start = START_RX.search(s)
    jp_open = JP_OPEN_AFTER_RX.search(s)
    jp_start = JP_START_AFTER_RX.search(s)
    door = opening.group(1) if opening else (jp_open.group(1) if jp_open else None)
    begin = start.group(1) if start else (jp_start.group(1) if jp_start else None)
    return door, begin


def date_times(time_text: str, days: list[date]) -> dict[str,tuple[str|None,str|None]]:
    # With two or more dates, do not guess which day a generic time applies to.
    if len(days) == 1:
        return {days[0].isoformat():parse_times(time_text)}
    out: dict[str,tuple[str|None,str|None]] = {}
    occurrences = list(DAY_RX.finditer(time_text))
    for i, match in enumerate(occurrences):
        mo = int(match['month']) if match['month'] else None
        d = int(match['day'])
        targets = [x for x in days if x.day == d and (mo is None or x.month == mo)]
        if len(targets)!=1:
            continue
        segment = time_text[match.end(): occurrences[i+1].start() if i+1<len(occurrences) else len(time_text)]
        out[targets[0].isoformat()] = parse_times(segment)
    return out


def time_section(block: str) -> str:
    # Source's detailed description contains a dedicated '時間' field.
    # Discard sale-start dates and postponement notices outside this field.
    m = re.search(r'(?:^|\n)\s*時間\s*[:：]?\s*([^\n]*(?:\n(?!\s*(?:\[INFO\]|INFO|出演|詳細|Title)\b)[^\n]*){0,3})', block, re.I)
    return norm(m.group(1)) if m else ''


def event_id(title: str, day: str) -> str:
    key = re.sub(r'\s+','',norm(title))
    return 'dime-'+hashlib.sha256(f'{VENUE}|{day}|{key}'.encode()).hexdigest()[:16]


def parse_page(html: str, year: int, month: int, now: datetime, url: str) -> list[dict]:
    soup = BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','noscript','nav','footer']):
        tag.decompose()
    # Keep line separators between published fields, even if they appear in tables.
    text = '\n'.join(norm(x) for x in soup.get_text('\n',strip=True).splitlines() if norm(x))
    if not re.search(rf'{year}年\s*{month}月', text[:2500]):
        raise ValueError(f'月別ページの年月が一致しません: {year}-{month:02d}')
    headers = list(HEADER_RX.finditer(text))
    if not headers:
        raise ValueError('公演詳細の見出しが見つからない（サイト構造を確認）')
    records = []
    for i,h in enumerate(headers):
        title = norm(h['title'])
        if PLACEHOLDER_RX.fullmatch(title) or UNAVAILABLE_RX.search(title):
            continue
        block = text[h.end():(headers[i+1].start() if i+1<len(headers) else len(text))]
        # Exclude a postponed/cancelled show even if times remain in its notice.
        if UNAVAILABLE_RX.search(block[:900]):
            continue
        try:
            first = date(year,int(h['month']),int(h['day']))
            last = first
            if h['day2']:
                end_month = int(h['month2'])
                end_year = year + (1 if end_month < first.month else 0)
                last = date(end_year,end_month,int(h['day2']))
            if last < first or (last-first).days>5:
                continue
        except ValueError:
            continue
        days = [first + timedelta(days=i) for i in range((last-first).days+1)]
        official_time = time_section(block)
        times = date_times(official_time,days)
        for day in days:
            if not (now.date()-timedelta(days=1) <= day <= now.date()+timedelta(days=150)):
                continue
            daystr = day.isoformat()
            door,begin = times.get(daystr,(None,None))
            dt = lambda x: f'{daystr}T{x}:00+09:00' if x else None
            records.append({
                'id':event_id(title,daystr), 'title':title[:110], 'date':daystr,
                'venue':VENUE, 'venueArea':'南新町', 'zone':'other',
                'source':'公式・高松DIME', 'url':url,
                'doorAt':dt(door), 'startAt':dt(begin), 'endAt':None,
                'endTimeType':None, 'note':'ライブハウス公式公演情報。終了時刻は未公表。',
            })
    return records


def checked_html(session:requests.Session,url:str) -> str:
    uri=urlparse(url)
    if uri.scheme != 'https' or uri.netloc != 'www.dime-dime.com' or uri.path != PATH:
        raise ValueError('公式サイト以外のURL')
    res=session.get(url,timeout=(10,25),allow_redirects=True)
    res.raise_for_status()
    final=urlparse(res.url)
    if final.scheme!='https' or final.netloc!='www.dime-dime.com' or final.path!=PATH:
        raise ValueError('異なるサイトへのリダイレクト')
    if len(res.content)>2_000_000:
        raise ValueError('サイトの容量が上限超')
    if 'html' not in res.headers.get('Content-Type','').lower():
        raise ValueError('HTML以外が返されました')
    # Avoid Latin-1 misdecoding from HTML endpoints without charset.
    if 'charset=' not in res.headers.get('Content-Type','').lower():
        res.encoding=res.apparent_encoding or 'utf-8'
    return res.text


def robots_check(session:requests.Session,urls:list[str]) -> None:
    r=session.get(f'{BASE}/robots.txt',timeout=(10,15))
    if r.status_code==404: rules=[]
    elif r.status_code==200 and len(r.content)<=500_000: rules=r.text.splitlines()
    else: raise RuntimeError(f'robots.txtを確認できません: HTTP {r.status_code}')
    rp=RobotFileParser();rp.parse(rules)
    for url in urls:
        if not rp.can_fetch(AGENT,url):
            raise PermissionError('robots.txtで取得が許可されていません: '+url)


def fetch(now:datetime) -> tuple[list[dict],list[str]]:
    session=requests.Session()
    session.headers.update({'User-Agent':AGENT,'Accept-Language':'ja-JP,ja;q=0.9'})
    months=[month_shift(now,i) for i in range(4)]
    urls=[page_url(y,m) for y,m in months]
    robots_check(session,urls)
    records=[];errors=[];successes=0
    for (year,month),url in zip(months,urls):
        try:
            part=parse_page(checked_html(session,url),year,month,now,url)
            records.extend(part);successes+=1
            print(f'DIME公式: {year}/{month:02d} → {len(part)}件')
        except Exception as exc:
            errors.append(f'{year}/{month:02d}: {type(exc).__name__}: {str(exc)[:120]}')
    if successes==0 or not records:
        raise RuntimeError('公式公演情報を取得できません。'+ ' / '.join(errors)[:600])
    return sorted({e['id']:e for e in records}.values(),key=lambda x:(x['date'],x['title'])),errors


def backup(now:datetime) -> list[dict]:
    try:
        r=requests.get(PUBLISHED,params={'v':now.strftime('%Y%m%d%H%M')}, timeout=(8,15),headers={'User-Agent':AGENT})
        r.raise_for_status()
        if len(r.content)>1_500_000:return []
        data=r.json()
        prev=datetime.fromisoformat(data['updatedAt'])
        if prev.tzinfo is None:prev=prev.replace(tzinfo=JST)
        if not (timedelta(0)<=now-prev<timedelta(days=7)):return []
        return [e for e in data.get('events',[]) if isinstance(e,dict) and e.get('venue')==VENUE
                and e.get('date','')>=(now.date()-timedelta(days=1)).isoformat()]
    except Exception as exc:
        print('DIME直近データの取得失敗: '+type(exc).__name__,file=sys.stderr)
        return []


def integrate(payload:dict,events:list[dict],status:str,now:datetime) -> dict:
    if payload.get('schema')!=1 or not isinstance(payload.get('events'),list):
        raise ValueError('events.jsonの形式が想定外です。更新を中止')
    copied=dict(payload)
    copied['events']=sorted([e for e in payload['events'] if e.get('venue')!=VENUE]+events,
                             key=lambda e:(e.get('date',''),e.get('venue',''),e.get('title','')))
    other=[s for s in payload.get('sources',[]) if isinstance(s,dict) and s.get('venue')!=VENUE]
    other.append({'venue':VENUE,'status':status,'count':len(events),'checkedAt':now.isoformat(timespec='seconds')})
    copied['sources']=other
    copied['partial']=any(s.get('status') in ('partial','stale','error') for s in other)
    if events and VENUE not in copied.get('sourceName',''):
        copied['sourceName']=str(copied.get('sourceName','複数公式サイト'))+'・'+VENUE
    return copied


def main()->int:
    now=datetime.now(JST)
    if not OUTPUT.exists():
        print('data/events.jsonがありません。先にfetch_events.pyを実行してください。',file=sys.stderr)
        return 1
    payload=json.loads(OUTPUT.read_text(encoding='utf-8'))
    try:
        rows,errors=fetch(now)
        state='partial' if errors else 'ok'
        if errors: print('DIME一部月失敗: '+' / '.join(errors),file=sys.stderr)
    except Exception as exc:
        print('DIME取得失敗: '+str(exc)[:700],file=sys.stderr)
        rows=backup(now)
        state='stale' if rows else 'error'
    new=integrate(payload,rows,state,now)
    OUTPUT.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'DIME統合完了: {len(rows)}件 / {state} / 全施設合計 {len(new["events"])}件')
    return 0  # Facility-specific errors do not block other feeds.


if __name__=='__main__':
    raise SystemExit(main())
