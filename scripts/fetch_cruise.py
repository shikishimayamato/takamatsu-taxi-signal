#!/usr/bin/env python3
"""香川県の高松港クルーズ客船公式PDFを、GitHub Pages向けイベントJSONへ変換する。

依存: requests, beautifulsoup4, pymupdf。OCR不要の文字PDFのみ対象。
表形式が変わり解釈できなくなった場合、以前のデータを消さず stale にする。
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import fitz
import requests
from bs4 import BeautifulSoup

INDEX = 'https://www.pref.kagawa.lg.jp/kouryu/cruise/jisseki.html'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'cruise_events.json'
JST = ZoneInfo('Asia/Tokyo')
AGENT = 'TakamatsuTaxiSignal/1.0 (+official source attribution; twice-daily metadata fetch)'
PDF_LIMIT = 5_000_000
PUBLISHED_JSON = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/cruise_events.json'
# 列の相対座標は香川県公式「クルーズ客船高松港入港一覧」の表を参照。
COL = {'number':(.020,.048), 'arrive_date':(.048,.111), 'arrive_time':(.147,.184),
       'depart_date':(.184,.247), 'depart_time':(.284,.320), 'berth':(.320,.371),
       'ship':(.371,.655), 'tonnage':(.655,.725), 'capacity':(.795,.865),
       'previous':(.863,.922), 'next':(.922,.983)}


def normalize(s):
    return unicodedata.normalize('NFKC', str(s or '')).replace('\u3000',' ').strip()


def discover_pdfs(html: str, now_year: int) -> list[tuple[int, str]]:
    soup = BeautifulSoup(html,'html.parser')
    found = []
    for link in soup.select('a[href]'):
        name = normalize(link.get_text(' ',strip=True))
        m = re.search(r'(20\d{2})年',name)
        if not m or 'PDF' not in name.upper():
            continue
        year = int(m.group(1))
        if year not in (now_year,now_year+1):
            continue
        url = urljoin(INDEX, link.get('href',''))
        parts = urlparse(url)
        if (parts.scheme!='https' or parts.netloc!='www.pref.kagawa.lg.jp'
              or not parts.path.startswith('/documents/') or not parts.path.lower().endswith('.pdf')):
            continue
        found.append((year,url))
    return sorted(set(found))


def date_from_cell(raw):
    m = re.search(r'(20\d{2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})',normalize(raw))
    if not m:return None
    try:return datetime(int(m[1]),int(m[2]),int(m[3])).date().isoformat()
    except ValueError:return None


def time_from_cell(raw):
    m = re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)',normalize(raw))
    return f'{int(m[1]):02d}:{m[2]}' if m else None


def cell_words(words, width, left, right, y0, y1):
    selected=[]
    for w in words:
        x0,ty0,x1,ty1,txt,*_=w
        xc=(x0+x1)/2/width
        yc=(ty0+ty1)/2
        if left<=xc<right and y0<=yc<y1:
            selected.append((ty0,x0,txt))
    selected.sort()
    # 同じ行の複数トークンを結合し、船名の改行はスペースで保持する。
    return normalize(' '.join(x[2] for x in selected))


def page_rows(page):
    words=page.get_text('words',sort=False)
    if not words:
        return []
    width,height=page.rect.width,page.rect.height
    anchors=[]
    for w in words:
        x0,y0,x1,y1,t,*_=w
        if not re.fullmatch(r'\d{1,3}', normalize(t)):
            continue
        xc=(x0+x1)/(width*2)
        if .020<=xc<.048 and .135*height<y0<.94*height:
            no=int(normalize(t))
            if 1<=no<=150:
                anchors.append((y0,no))
    anchors.sort()
    out=[]
    seen=set()
    for idx,(anchor,no) in enumerate(anchors):
        if no in seen:continue
        seen.add(no)
        top=anchor-2.5
        bottom=anchors[idx+1][0]-2.5 if idx+1<len(anchors) else min(.96*height,anchor+.07*height)
        if bottom<=top:continue
        fields={key:cell_words(words,width,*rng,top,bottom) for key,rng in COL.items()}
        fields['number']=str(no)
        if date_from_cell(fields['arrive_date']) is not None:
            out.append(fields)
    return out


def make_event(fields, pdf_url):
    arrival=date_from_cell(fields['arrive_date'])
    departure=date_from_cell(fields['depart_date']) or arrival
    if not arrival or not departure or departure<arrival:
        return None
    ship=normalize(fields['ship']).replace('★','').strip(' -')
    if not ship or len(ship)>130:
        return None
    # 日本語併記がある場合はカードでは日本語名を優先する。
    jp=re.search(r'[（(]([^（）()]{2,60})[）)]',ship)
    name=jp.group(1).strip() if jp and re.search('[\u3040-\u30ff\u3400-\u9fff]',jp.group(1)) else ship
    name=name.strip()
    arrival_time=time_from_cell(fields['arrive_time'])
    depart_time=time_from_cell(fields['depart_time'])
    berth=fields['berth'] or '高松港'
    capacity_text=re.sub(r'[^0-9]','',fields['capacity'])
    capacity=int(capacity_text) if capacity_text else None
    if capacity is not None and capacity>10000:
        capacity=None
    source_hash=hashlib.sha256(f'{arrival}|{ship}|{berth}'.encode()).hexdigest()[:16]
    notes=['香川県公式の寄港予定（変更・中止の可能性あり）']
    if ship != name:notes.append('船名原文：'+ship)
    if capacity is not None:notes.append(f'乗客定員：{capacity:,}人（実際の下船人数ではありません）')
    if fields['previous']:notes.append('前港：'+fields['previous'])
    if fields['next']:notes.append('次港：'+fields['next'])
    return {'id':'cruise-'+source_hash,'title':'クルーズ船寄港：'+name,
            'date':arrival,'endDate':departure,'venue':'高松港',
            'venueArea':berth,'startAt':arrival+'T'+arrival_time+':00+09:00' if arrival_time else None,
            'endAt':departure+'T'+depart_time+':00+09:00' if depart_time else None,
            'doorAt':None,'participants':None,'passengerCapacity':capacity,
            'scaleOverride':'auto','url':pdf_url,'source':'香川県公式・高松港クルーズ船入港一覧',
            'note':'／'.join(notes)}


def parse_pdf(data:bytes, pdf_url:str):
    if not data.startswith(b'%PDF') or len(data)>PDF_LIMIT:
        raise ValueError('PDF形式またはサイズ不正')
    with fitz.open(stream=data,filetype='pdf') as document:
        if document.page_count<1 or document.page_count>12:
            raise ValueError('PDFのページ数が想定外')
        fields=[]
        for page in document:
            fields.extend(page_rows(page))
    if not fields:
        raise ValueError('公式PDFの表が読めません。レイアウト変更の可能性')
    events=[]
    for row in fields:
        item=make_event(row,pdf_url)
        if item:events.append(item)
    if not events:
        raise ValueError('日付・船名などを解釈できません')
    # PDFに1件以上あるのに変換率が低すぎる場合は誤読・破損として扱う。
    if len(events)*2<len(fields):
        raise ValueError('行の変換に失敗した件数が多いため、公開せず維持')
    return events


def read_previous():
    # Actionsは毎回checkoutするため、前回の公開JSONはリポジトリに存在しない。
    # まず手元のJSON、なければ現在公開中のPages版を読む。
    candidates = []
    try:
        candidates.append(json.loads(OUTPUT.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError):
        pass
    try:
        resp=requests.get(PUBLISHED_JSON,timeout=12,headers={'User-Agent':AGENT})
        if resp.ok and len(resp.content)<600_000:
            candidates.append(resp.json())
    except (requests.RequestException,ValueError):
        pass
    for obj in candidates:
        if (isinstance(obj,dict) and obj.get('schema')==1
              and obj.get('status') in ('ok','stale')
              and isinstance(obj.get('events'),list)
              and len(obj['events'])<=120):
            return obj
    return None


def write(payload):
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def run():
    now=datetime.now(JST)
    previous=read_previous()
    session=requests.Session()
    session.headers.update({'User-Agent':AGENT,'Accept':'text/html,application/pdf;q=0.9'})
    try:
        response=session.get(INDEX,timeout=25)
        response.raise_for_status()
        response.encoding=response.apparent_encoding or response.encoding
        links=discover_pdfs(response.text,now.year)
        if not links:
            raise ValueError(f'{now.year}年・{now.year+1}年の公式PDFリンクが見つかりません')
        records=[]
        for year,link in links:
            data=session.get(link,timeout=35)
            data.raise_for_status()
            if len(data.content)>PDF_LIMIT:raise ValueError('PDFがサイズ上限を超過')
            part=parse_pdf(data.content,link)
            records.extend(part)
            print(f'{year}年：{len(part)}件 {link}')
        items={ev['id']:ev for ev in records}
        # PDFの新規更新で未来予定が減った場合も公式の最新予定を優先する。
        payload={'schema':1,'updatedAt':now.isoformat(),'status':'ok','source':INDEX,
                 'sourceName':'香川県・高松港クルーズ船寄港予定',
                 'pdfSources':[u for _,u in links],
                 'events':sorted(items.values(),key=lambda x:(x['date'],x['startAt'] or '',x['title']))}
        write(payload)
        print(f'クルーズ客船：合計{len(items)}件、JSON出力: {OUTPUT}')
    except Exception as exc:
        print(f'クルーズ客船PDFの取得失敗: {exc}',file=sys.stderr)
        # 既存の成功分を保存したまま、更新失敗を明示する。
        if previous is not None:
            previous.update(status='stale',error='公式PDFの更新確認に失敗しました')
            write(previous)
        else:
            write({'schema':1,'updatedAt':None,'status':'error','source':INDEX,
                   'sourceName':'香川県・高松港クルーズ船寄港予定','events':[],
                   'error':'公式PDFの取得に失敗しました'})
        # 公式サイトの一時障害でワークフロー全体の公開を止めない。
        return 0
    return 0


if __name__=='__main__':
    sys.exit(run())
