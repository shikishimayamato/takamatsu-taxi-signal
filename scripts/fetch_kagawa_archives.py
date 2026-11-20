#!/usr/bin/env python3
"""香川県立文書館自身が公開する講座・企画展示を取得する。"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup
from fetch_kagawa_bunkakaikan import get

ORIGIN='https://www.pref.kagawa.lg.jp'
COURSE=ORIGIN+'/bunshokan/bunshokan/course/kfvn.html'
EXHIBIT=ORIGIN+'/bunshokan/bunshokan/exhibition/kfvn.html'
PUBLISHED='https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUTPUT=Path(__file__).resolve().parents[1]/'data'/'events.json'
VENUE='香川県立文書館'
SOURCE='公式・香川県立文書館'
PREFIX='kagawa-archives-'
JST=timezone(timedelta(hours=9))
AGENT='TakamatsuTaxiSignal/1.0 (+https://github.com/shikishimayamato/takamatsu-taxi-signal)'

def norm(text):
    return ' '.join(unicodedata.normalize('NFKC',text).split())

def main_content(body):
    content=BeautifulSoup(body,'html.parser').select_one('#tmp_contents')
    if not content:
        raise ValueError('文書館の公式本文を確認できません')
    return content

def fiscal_year(text):
    m=re.search(r'令和\s*(元|\d+)\s*年度',norm(text))
    if not m:
        raise ValueError('文書館の掲載年度を確認できません')
    return 2018+(1 if m[1]=='元' else int(m[1]))

def dates(text,year):
    # Handles a repeated month/year omitted in the host's session list.
    result=[]
    month=None
    explicit=False
    for m in re.finditer(r'(?:(?:令和(元|\d+)年|(20\d{2})年)\s*)?(?:(\d{1,2})月)?\s*(\d{1,2})日',norm(text)):
        if m[1]: year=2018+(1 if m[1]=='元' else int(m[1]));explicit=True
        elif m[2]: year=int(m[2]);explicit=True
        if m[3]: month=int(m[3])
        if month is not None:
            result.append(date(year if explicit or month>=4 else year+1,month,int(m[4])))
    return result

def clock(text):
    t=norm(text)
    if t=='正午': return '12:00'
    m=re.fullmatch(r'(午前|午後)?(\d{1,2})時(?:(\d{1,2})分)?',t)
    if not m: raise ValueError('講座の時刻を確認できません: '+t)
    hour=int(m[2]);minute=int(m[3] or 0)
    if m[1]=='午後' and hour<12:hour+=12
    if m[1]=='午前' and hour==12:hour=0
    if hour>23 or minute>59:raise ValueError('時刻の範囲が不正です')
    return f'{hour:02d}:{minute:02d}'

def times(text):
    atom=r'(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?|正午'
    m=re.search('('+atom+r')\s*[~〜～]\s*('+atom+')',norm(text))
    if not m: raise ValueError('講座の開始・終了を確認できません')
    a,b=clock(m[1]),clock(m[2])
    if a>=b:raise ValueError('講座の時刻の順序が不正です')
    return a,b

def event(title,day,url,kind,start=None,end=None,capacity=None):
    ds=day.isoformat()
    row={'id':PREFIX+kind+'-'+ds+'-'+hashlib.sha256(title.encode()).hexdigest()[:10],
         'title':title,'date':ds,'venue':VENUE,'venueArea':'林町','zone':'other',
         'source':SOURCE,'url':url,'doorAt':None,'startAt':None,'endAt':None}
    if start:row['startAt']=ds+'T'+start+':00+09:00'
    if end:
        row['endAt']=ds+'T'+end+':00+09:00'
        row['endTimeType']='公式の終了時刻'
    row['note']='定員'+str(capacity)+'名（実際の参加人数ではありません）。要申込。' if capacity else ''
    return row

def parse_courses(body,today):
    content=main_content(body)
    if '講座の案内' not in content.get_text():raise ValueError('講座案内ではありません')
    year=fiscal_year(content.get_text(' ',strip=True))
    rows=[]
    found=0
    for table in content.find_all('table'):
        cells={norm(tr.th.get_text(' ',strip=True)):tr.td for tr in table.find_all('tr') if tr.th and tr.td}
        if '日程' not in cells:continue
        found+=1
        caption=norm(table.caption.get_text(' ',strip=True)) if table.caption else ''
        title=caption.replace('の概要','').strip()
        if not title:raise ValueError('講座名を確認できません')
        capacity=re.search(r'(\d+)名',norm(cells['定員'].get_text())) if '定員' in cells else None
        text=norm(cells['日程'].get_text(' ',strip=True))
        if '中止' in text or '延期' in text:continue
        segments=re.split(r'(?=【[AB]日程】)',text)
        for segment in segments:
            ds=dates(segment,year)
            if not ds:continue
            a,b=times(segment)
            suffix=re.search(r'【([AB]日程)】',segment)
            name=title+(' '+suffix[1] if suffix else '')
            for day in ds:
                if today<=day<=today+timedelta(days=370):
                    rows.append(event(name,day,COURSE,'course',a,b,int(capacity[1]) if capacity else None))
    if not found:raise ValueError('講座の日程表を確認できません')
    return rows

def parse_exhibits(body,today):
    content=main_content(body)
    year=fiscal_year(content.get_text(' ',strip=True))
    rows=[]
    headings=content.find_all('h3')
    if not headings:raise ValueError('企画展示の見出しを確認できません')
    for heading in headings:
        title=re.sub(r'^\d+\s*','',norm(heading.get_text(' ',strip=True)))
        if title=='過去の展示':continue
        paragraph=heading.find_next_sibling('p')
        if paragraph is None:raise ValueError('展示の会期を確認できません')
        date_text=norm(paragraph.get_text(' ',strip=True))
        ds=dates(date_text,year)
        if len(ds)!=2 or ds[0]>ds[1]:raise ValueError('展示の会期を確認できません: '+date_text)
        if ds[1]<today or ds[0]>today+timedelta(days=370):continue
        row=event(title,ds[0],EXHIBIT,'exhibition')
        row['endDate']=ds[1].isoformat()
        row['note']='会期中の休館日・開館時間は公式案内をご確認ください。展示の会期であり、毎日一斉に終了する催しではありません。'
        rows.append(row)
        for sibling in heading.next_siblings:
            if getattr(sibling,'name',None) in {'h2','h3'}:break
            if not getattr(sibling,'get_text',None):continue
            text=norm(sibling.get_text(' ',strip=True))
            if not text.startswith('展示解説'):continue
            text=text.split('●関連講座')[0]
            a,b=times(text)
            for day in dates(text,year):
                if today<=day<=today+timedelta(days=370):
                    row=event('展示解説 '+title,day,EXHIBIT,'gallery-talk',a,b)
                    row['note']='文書館展示室での展示資料解説。申込不要。'
                    rows.append(row)
    return rows

def fetch(session,today):
    robot=RobotFileParser()
    body=get(session,ORIGIN+'/robots.txt',500_000,missing_ok=True)
    robot.parse(body.decode('utf-8-sig').splitlines() if body else [])
    for url in (COURSE,EXHIBIT):
        if not robot.can_fetch(AGENT,url):raise PermissionError('取得が許可されていません: '+url)
    rows=parse_courses(get(session,COURSE,2_000_000),today)
    exhibit_body=get(session,EXHIBIT,2_000_000)
    rows+=parse_exhibits(exhibit_body,today)
    # The course page gives the times; the exhibition page gives its theme.
    text=norm(main_content(exhibit_body).get_text(' ',strip=True))
    theme=re.search(r'関連講座[:：]\s*アーカイブズ講座「([^」]+)」',text)
    if theme:
        for row in rows:
            if row['title']=='アーカイブズ講座':
                row['title']+='「'+theme[1]+'」'
    return rows

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
                rows = []
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




