#!/usr/bin/env python3
"""レクザムホール公式イベントカレンダー（安全優先の暫定版）

公式カレンダー内のイベント詳細から、日付・会場が確定できるものだけ収録。
非公開催事や休館日を公演として登録しない。未公表の終演時間を推定しない。
未検証のHTML構造に対しては無理な抽出をせず、取得状態をJSONに記録する。
"""
from __future__ import annotations
import hashlib, json, re, sys, time, unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

BASE='https://kenminhall.com/'
INDEX=BASE+'visitors/event/'
OUTPUT=Path(__file__).resolve().parents[1]/'data'/'rexam_events.json'
AGENT='TakamatsuTaxiSignal/0.7c (official calendar; twice daily)'
JST=ZoneInfo('Asia/Tokyo')
SESSION=requests.Session()
SESSION.headers.update({'User-Agent':AGENT,'Accept-Language':'ja-JP,ja;q=0.9'})
ROBOTS=None
DATE=re.compile(r'(20\d{2})\s*(?:年|[/.-])\s*(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})\s*日?')
REIWA=re.compile(r'令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
TIME=r'([01]?\d|2[0-3])\s*[：:]\s*([0-5]\d)'

def norm(text):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(text or ''))).strip()

def get_html(url):
    global ROBOTS
    p=urlparse(url)
    if p.scheme!='https' or p.netloc!='kenminhall.com':
        raise ValueError('公式サイト以外へのアクセスを拒否')
    if ROBOTS is None:
        res=SESSION.get(BASE+'robots.txt',timeout=(10,20))
        if res.status_code==404:
            ROBOTS=RobotFileParser(); ROBOTS.parse([])
        elif res.status_code==200 and len(res.content)<500000:
            ROBOTS=RobotFileParser(); ROBOTS.parse(res.text.splitlines())
        else:
            raise ValueError('robots.txt確認できません: '+str(res.status_code))
    if not ROBOTS.can_fetch(AGENT,url):
        raise PermissionError('robots.txtで許可されていません: '+url)
    res=SESSION.get(url,timeout=(12,25));res.raise_for_status()
    if len(res.content)>2500000: raise ValueError('公式ページが2.5MBを超過')
    if 'charset=' not in res.headers.get('Content-Type','').lower():
        res.encoding=res.apparent_encoding or 'utf-8'
    return res.text

def find_dates(text, now):
    found=[]
    for rx,offset in ((DATE,0),(REIWA,2018)):
        for m in rx.finditer(norm(text)):
            try:
                d=date(int(m[1])+offset,int(m[2]),int(m[3]))
                if now-timedelta(days=1)<=d<=now+timedelta(days=120) and d not in found:
                    found.append(d)
            except ValueError: pass
    return found

def times(text,day):
    value=norm(text)
    def lookup(label):
        m=re.search(r'(?:'+label+r')\s*(?:時間|予定)?\s*[:：]?\s*'+TIME,value)
        return f'{day.isoformat()}T{int(m[1]):02d}:{int(m[2]):02d}:00+09:00' if m else None
    return lookup('開場'),lookup('開演|開始'),lookup('終演|終了')

def candidate_links(html):
    soup=BeautifulSoup(html,'html.parser')
    found=[]
    for a in soup.select('a[href]'):
        url=urljoin(INDEX,a['href']);p=urlparse(url)
        if p.netloc!='kenminhall.com' or p.scheme!='https':continue
        if p.path.rstrip('/')!='/visitors/event_detail':continue
        if url not in found:found.append(url)
    return found[:90]

def parse_detail(html,url,now):
    soup=BeautifulSoup(html,'html.parser')
    for el in soup.select('script,style,nav,footer,header'):
        el.decompose()
    main=soup.find('main') or soup.find('article') or soup.body
    if not main:return []
    headings=main.find_all(['h1','h2','h3'])
    title=next((norm(h.get_text(' ',strip=True)) for h in headings
                if norm(h.get_text(' ',strip=True)) not in ('イベント詳細','コンサート＆イベントカレンダー','イベントカレンダー')
                and len(norm(h.get_text(' ',strip=True)))>=4),None)
    if not title or len(title)>120 or title=='非公開':return []
    lines=[norm(x) for x in main.get_text('\n',strip=True).splitlines()]
    lines=[x for x in lines if x]
    first=' '.join(lines[:90])
    ds=find_dates(first[:2000],now)
    if len(ds)!=1:return []  # 複数日程なら誤った分解を避けて除外
    day=ds[0]
    # レクザムホール公式「イベント詳細」であり、会場欄が大／小ホールと確認できたものだけ採用
    room=None
    for i,line in enumerate(lines[:95]):
        m=re.match(r'^(?:会\s*場|場\s*所)\s*[:：]?\s*(.*)',line)
        if not m:continue
        v=norm(m[1] or (lines[i+1] if i+1<len(lines) else ''))[:100]
        if '小ホール' in v:room='小ホール'
        elif '大ホール' in v:room='大ホール'
        elif 'レクザムホール' in v or '香川県県民ホール' in v:room='区分未確認'
        else:return []
        break
    if room is None:return []
    if room=='小ホール' and date(2027,1,1)<=day<=date(2027,9,30):return []
    if room=='大ホール' and date(2027,10,1)<=day<=date(2029,9,30):return []
    door,start,end=times(first[:1500],day)
    key=hashlib.sha256(f'{day}|{title}|{room}'.encode()).hexdigest()[:16]
    return [{'id':'rexam-'+key,'date':day.isoformat(),'endDate':day.isoformat(),
             'title':title,'venue':'レクザムホール','zone':'station',
             'url':url,'doorAt':door,'startAt':start,'endAt':end,
             'participants':None,
             'note':f'レクザムホール公式催事・{room}／公表済み時刻だけを表示。来場人数は未確認。'}]

def main():
    now=datetime.now(JST)
    payload={'schema':1,'updatedAt':now.isoformat(timespec='seconds'),
             'status':'error','source':INDEX,'sourceName':'レクザムホール公式',
             'events':[],'warnings':[],'checked':0}
    try:
        links=candidate_links(get_html(INDEX))
        if not links:raise ValueError('公式カレンダーにイベント詳細へのリンクが見つかりません')
        found={}
        for url in links:
            try:
                entries=parse_detail(get_html(url),url,now.date())
                payload['checked']+=1
                for item in entries:found[item['id']]=item
            except Exception as exc:
                payload['warnings'].append(f'{url}: {type(exc).__name__}: {exc}')
            time.sleep(0.3)
        payload['events']=sorted(found.values(),key=lambda x:(x['date'],x['title']))
        if not found:raise ValueError('日付とレクザムホール会場を確認できる催事が0件です。HTML構造の調査が必要')
        payload['status']='partial' if payload['warnings'] else 'ok'
    except Exception as exc:
        payload['warnings'].append(f'{type(exc).__name__}: {exc}')
        # 自動抽出不能時には「0件=催事なし」と断定させない。
        if OUTPUT.exists():
            try:
                prior=json.loads(OUTPUT.read_text(encoding='utf-8'))
                if prior.get('events'):
                    payload['events']=prior['events']
                    payload['updatedAt']=prior.get('updatedAt')
                    payload['status']='stale'
            except (ValueError,OSError):pass
    OUTPUT.parent.mkdir(exist_ok=True,parents=True)
    OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'レクザムホール: {payload["status"]} / {len(payload["events"])}件 / 詳細照会 {payload["checked"]}件')
    for w in payload['warnings'][:5]:print('注意:',w,file=sys.stderr)

if __name__=='__main__':main()
