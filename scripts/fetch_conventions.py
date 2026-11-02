#!/usr/bin/env python3
"""高松観光コンベンション・ビューローの公開大会日程を低頻度で収集する。

月間の大会参加予定人数は大会全体の掲載値であり、日別人数や実人数ではない。
日時の記載がない場合は推測しない。取得不能なら以前のJSONを維持する。
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'conventions.json'
URL = 'https://takamatsu.or.jp/calendar/'
AGENT = 'TakamatsuTaxiSignal/0.6 (+official calendar attribution; max twice daily)'
JST = ZoneInfo('Asia/Tokyo')

def norm(s):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s or '')).strip()

def fetch_html():
    session = requests.Session()
    session.headers.update({'User-Agent': AGENT, 'Accept-Language':'ja-JP,ja;q=0.9'})
    # robotsが確認できない場合は取得を断念。証明書検証は無効化しない。
    robot_url = urljoin(URL, '/robots.txt')
    r = session.get(robot_url, timeout=15)
    if r.status_code == 404:
        rp = RobotFileParser(); rp.parse([])
    elif r.status_code == 200 and len(r.content) < 500_000:
        rp = RobotFileParser(); rp.parse(r.text.splitlines())
    else:
        raise RuntimeError(f'robots.txtを安全に確認できません: HTTP {r.status_code}')
    if not rp.can_fetch(AGENT, URL):
        raise PermissionError('robots.txtが自動取得を許可していません')
    response = session.get(URL, timeout=25)
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise RuntimeError('取得ページが大きすぎます')
    response.encoding = response.apparent_encoding if not re.search(r'charset=', response.headers.get('Content-Type',''), re.I) else response.encoding
    return response.text

def parse_date_span(month: int, raw: str, fiscal_start: int):
    value = norm(raw).replace('〜','~').replace('～','~').replace('−','-')
    value = re.sub(r'\s*(?:日|日間)\s*','',value)
    # 例: 5, 5~7, 30~12/4, 19~7/5
    pattern = r'^(\d{1,2})(?:\s*[~-]\s*(?:(\d{1,2})\s*/\s*)?(\d{1,2}))?$'
    m = re.fullmatch(pattern,value)
    if not m:
        return None
    day = int(m[1]); nextmonth = int(m[2]) if m[2] else month; endday = int(m[3]) if m[3] else day
    if endday < day and m[2] is None:
        nextmonth = 1 if month == 12 else month + 1
    year = fiscal_start + int(month < 4)
    endyear = fiscal_start + int(nextmonth < 4)
    if nextmonth < month and month != 12 and nextmonth >= 4:
        endyear += 1
    try:
        start = date(year,month,day)
        end = date(endyear,nextmonth,endday)
    except ValueError:
        return None
    if end < start or (end-start).days > 60:
        return None
    return start,end

def parse_calendar(html: str, fiscal_start: int):
    soup = BeautifulSoup(html,'html.parser')
    found=[]; skipped=0; seen=set()
    for table in soup.select('table'):
        last_month = None
        for tr in table.select('tr'):
            cells=tr.find_all(['td','th'],recursive=False)
            values=[norm(c.get_text(' ',strip=True)) for c in cells]
            if len(values)>=6 and re.fullmatch(r'\d{1,2}月',values[0]):
                month,span,title,kind,people,venue=values[:6]
                last_month=month
            elif len(values)>=5 and last_month:
                # 元のHTMLで月名がrowspanになっている場合、後続行は5列になる。
                month=last_month;span,title,kind,people,venue=values[:5]
            else:
                continue
            mm=re.fullmatch(r'(\d{1,2})月',month)
            if not mm or not title or title in ('大会名','未定'):
                continue
            dates=parse_date_span(int(mm[1]),span,fiscal_start)
            if not dates:
                skipped+=1; continue
            first,last=dates
            if (last-date.today()).days < -7: continue
            num=int(people.replace(',','')) if re.fullmatch(r'\d[\d,]*',people) else None
            if num is not None and (num<0 or num>1000000): num=None
            key=hashlib.sha256((title+'|'+first.isoformat()).encode('utf-8')).hexdigest()[:16]
            if key in seen: continue
            seen.add(key)
            found.append({'id':'convention-'+key,'title':title[:140],'date':first.isoformat(),
                          'endDate':last.isoformat(),'venue':venue[:180] or '会場未公表',
                          'participants':num,'category':kind[:20], 'url':URL,
                          'source':'高松観光コンベンション・ビューロー'})
    found.sort(key=lambda x:(x['date'],x['title']))
    return found,skipped

def main():
    now=datetime.now(JST)
    # 公表ページは4月〜翌3月の年度単位。2026年現在の頁が翌年度に切替わったら次版で検出方法を見直す。
    fiscal_start=now.year if now.month>=4 else now.year-1
    try:
        html=fetch_html()
        records,skipped=parse_calendar(html,fiscal_start)
        if not records: raise ValueError('公式テーブルから将来催事を抽出できません')
        payload={'schema':1,'updatedAt':now.isoformat(),'status':'ok','source':URL,
                 'sourceName':'高松観光コンベンション・ビューロー','events':records,
                 'notice':'掲載参加人数は大会全体の予定値。実際の来場者数や日別人数ではありません。'}
        OUTPUT.parent.mkdir(exist_ok=True,parents=True)
        OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(f'コンベンションカレンダー: 取得{len(records)}件, 日付不明スキップ{skipped}件, 出典 {URL}')
    except Exception as exc:
        # 既存ファイルを空のデータで上書きしない。
        print('警告: コンベンション取得失敗。前回の公開データを維持できる場合は保持します: '+str(exc),file=sys.stderr)
        if OUTPUT.exists():
            try:
                old=json.loads(OUTPUT.read_text(encoding='utf-8'))
                if old.get('events'):
                    old['status']='stale'
                    OUTPUT.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    return 0
            except Exception: pass
        payload={'schema':1,'updatedAt':None,'status':'error','source':URL,'sourceName':'高松観光コンベンション・ビューロー','events':[]}
        OUTPUT.parent.mkdir(exist_ok=True,parents=True)
        OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0

if __name__=='__main__':
    sys.exit(main())
