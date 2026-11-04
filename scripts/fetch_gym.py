#!/usr/bin/env python3
"""高松市スポーツ協会の月間PDFを施設別に抽出（v0.7f）。

当月・翌月の公開済PDFのみを対象にする。2列×複数頁の行事予定から
掲載される全13施設の行事（小規模・個人使用日を含む）を分離する。
原PDFに存在しない人数・時刻・タクシー需要規模は作らない。
レイアウトが変わり見出しを検出できなければ、誤施設への割当を避ける。
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fitz
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
ROOT = 'https://www.taka-spo.or.jp/'
INDEX_URL = ROOT + 'schedule.html'
ALLOWED_PATH = '/pdf/schedule/'
OUT = Path(__file__).resolve().parent.parent / 'data' / 'gym_events.json'
UA = 'TakamatsuTaxiSignal/0.7f (scheduled public schedule downloader)'

VENUES = (
    '高松市総合体育館', '香川総合体育館', '仏生山公園体育館',
    '国分寺橘ノ丘総合運動公園', '西部運動センター',
    'かわなべスポーツセンター', '東部運動公園', '亀水運動センター',
    'りんくうスポーツ公園', '牟礼総合体育館', '香南体育館',
    'ホタルと文化の里運動場グラウンド', '牟礼中央公園運動センター',
)
VENUE_SET = set(VENUES)
# 地名は確実な範囲でのみ表示する。営業ゾーンへの機械的な誤割当は行わない。
VENUE_AREA = {
    '高松市総合体育館': '福岡町', '香川総合体育館': '香川町',
    '仏生山公園体育館': '仏生山', '国分寺橘ノ丘総合運動公園': '国分寺',
    '西部運動センター': '高松市西部', 'かわなべスポーツセンター': '川部町',
    '東部運動公園': '高松市東部', '亀水運動センター': '亀水町',
    'りんくうスポーツ公園': '高松空港周辺', '牟礼総合体育館': '牟礼町',
    '香南体育館': '香南町', 'ホタルと文化の里運動場グラウンド': '塩江町',
    '牟礼中央公園運動センター': '牟礼町',
}
MONTH_RX = re.compile(r'令和\s*(\d+)年\s*(\d{1,2})月')
DAY_RX = re.compile(r'^(\d{1,2})日\([月火水木金土日]\)$')
# 行末の「～29日」「・8日・15日」「・7日～8日」等だけ期間指定として読む。
TRAILING_DATES_RX = re.compile(r'((?:[・･]\s*\d{1,2}日(?:\s*[～~〜]\s*\d{1,2}日)?|[～~〜]\s*\d{1,2}日)\s*)+$')
MARK_RX = re.compile(r'([・･]|[～~〜])\s*(\d{1,2})日(?:\s*[～~〜]\s*(\d{1,2})日)?')


def tidy(s: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s)).strip()


def discover_months(html: str, now: datetime) -> list[tuple[int, int, str]]:
    """公式ページにリンクがある当月と翌月だけ。12月→1月も処理。"""
    wanted = {(now.year, now.month)}
    next_month = now.replace(day=28) + timedelta(days=5)
    wanted.add((next_month.year, next_month.month))
    result = set()
    for a in BeautifulSoup(html, 'html.parser').select('a[href]'):
        label = tidy(a.get_text(' ', strip=True))
        m = MONTH_RX.search(label)
        url = urljoin(INDEX_URL, a.get('href', ''))
        u = urlparse(url)
        if not m or u.scheme != 'https' or u.netloc != 'www.taka-spo.or.jp':
            continue
        if not u.path.startswith(ALLOWED_PATH) or not u.path.lower().endswith('.pdf'):
            continue
        year, month = 2018 + int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and (year, month) in wanted:
            result.add((year, month, url))
    return sorted(result)


def _date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def expand_dates(title: str, year: int, month: int, start_day: int):
    """会場・催事名を維持して、開始日・終了日・飛び日の列を作る。"""
    title = tidy(title)
    match = TRAILING_DATES_RX.search(title)
    tokens = match.group(0) if match else ''
    if match:
        title = title[:match.start()].rstrip(' ・･～~〜').strip()
    starts: list[tuple[date, date]] = []
    first = _date(year, month, start_day)
    if first is None:
        return title, []
    primary_end = first
    extras = []
    for tok in MARK_RX.finditer(tokens):
        marker, val, e = tok.groups()
        d = _date(year, month, int(val))
        if d is None:
            continue
        end = _date(year, month, int(e)) if e else d
        if end is None or end < d:
            continue
        if marker in ('~', '～', '〜'):
            if d >= first:
                primary_end = d
        else:
            extras.append((d, end))
    starts.append((first, primary_end))
    starts.extend(extras)
    return title, list(dict.fromkeys(starts))


def _page_rows(page: fitz.Page) -> list[tuple[str, str, int, str]]:
    """戻値は (left/right, venue, day-or-0, title) の行単位。

    横2列が混じらぬようX座標で切り離す。日付の無い行も独立行事として残す。
    文字の行高の微差（3日の2行折返し等）は月間表の定型行ピッチで吸収する。
    """
    words = [(x0, y0, x1, tidy(w)) for x0,y0,x1,_y1,w,*_ in page.get_text('words',sort=True)]
    collected = []
    for col, lo, hi in [('left', 16.0, 296.0), ('right', 300.0, 585.0)]:
        ws = [(x,y,t) for x,y,x1,t in words if lo <= x < hi and t]
        day_ys = sorted(y for _x,y,t in ws if DAY_RX.match(t))
        if not day_ys:
            continue
        diffs = [b-a for a,b in zip(day_ys,day_ys[1:]) if 14 <= b-a <= 20]
        pitch = statistics.median(diffs) if diffs else 17.27
        if not 15.0 <= pitch <= 19.5:
            raise ValueError(f'想定外のPDF行間です: {pitch}')
        anchor = day_ys[0]
        # 先頭の会場見出しは最初の日付の直前にある。
        hys = [y for _x,y,t in ws if t in VENUE_SET and y < anchor+2]
        first_header = min(hys) if hys else anchor-pitch
        buckets: dict[int,list[tuple[float,float,str]]] = {}
        for x,y,t in ws:
            if y < first_header-2 or y > 810:
                continue
            row = round((y-anchor)/pitch)
            if abs((y-anchor)-row*pitch) > 7.1:
                continue
            buckets.setdefault(row,[]).append((x,y,t))
        current_venue = None
        current_day = None
        for _row, parts in sorted(buckets.items()):
            parts.sort(key=lambda z:(round(z[1]/2.5),z[0]))
            headings = [t for _x,_y,t in parts if t in VENUE_SET]
            if headings:
                if len(headings)>1:raise ValueError(f'同じ列の同じ行に複数施設: {headings}')
                current_venue = headings[0]
                current_day = None
                # 見出しの横に行事本文があるPDFは異常として扱う。
                if len(parts)>1:
                    other=[t for _x,_y,t in parts if t not in VENUE_SET]
                    if other:
                        raise ValueError(f'施設見出しと本文が混在しています: {headings} {other[:2]}')
                continue
            if not current_venue:
                continue
            dates = [t for _x,_y,t in parts if DAY_RX.match(t)]
            if len(dates)>1:
                raise ValueError(f'同じ列の同じ行に日付が複数: {dates}')
            if dates:
                current_day = int(DAY_RX.match(dates[0]).group(1))
            content = [t for _x,_y,t in parts if t not in dates and not t.startswith(('※','〒'))]
            title = tidy(' '.join(content))
            if title and current_day:
                collected.append((col,current_venue,current_day,title))
    return collected


def parse_pdf(data: bytes, year: int, month: int, source_url: str) -> list[dict]:
    doc = fitz.open(stream=data, filetype='pdf')
    if not doc or len(doc)>8:
        raise ValueError('PDFのページ数が想定外です')
    parsed = []
    all_venues=set()
    for page in doc:
        for _col,venue,day,title in _page_rows(page):
            if venue not in VENUE_SET:
                raise ValueError('未登録の施設です: '+venue)
            all_venues.add(venue)
            cleaned, occurrences = expand_dates(title,year,month,day)
            if not cleaned or len(cleaned)>120 or not occurrences:
                print(f'注意: 行事名・日付不正のため除外: {venue} {day} {title}',flush=True)
                continue
            for start,end in occurrences:
                date_s=start.isoformat()
                key=hashlib.sha256(f'gym|{date_s}|{venue}|{cleaned}'.encode()).hexdigest()[:16]
                parsed.append({
                    'id':'gym-'+key, 'date':date_s, 'endDate':end.isoformat(),
                    'title':cleaned,'venue':venue,'venueArea':VENUE_AREA.get(venue,''),
                    'zone':'station' if venue=='高松市総合体育館' else 'other',
                    'url':source_url,'doorAt':None,'startAt':None,'endAt':None,
                    'participants':None,
                    'note':'高松市スポーツ協会の月間行事予定（時刻・参加人数は未公表）'+('／PDF上で催事名が途中で切れている可能性があります' if cleaned.endswith('香川県') else ''),
                })
    # 公式の2列表で全施設が見えなくなった場合、誤データを正常更新扱いしない。
    if len(all_venues)<10:
        raise ValueError(f'施設別の抽出が不足: {len(all_venues)}施設。PDFの配置を再確認してください。')
    unique={e['id']:e for e in parsed}
    if len(unique)<15:
        raise ValueError('催事の抽出が極端に少ないため更新を中止します。')
    print(f'{year}-{month:02d}: {len(unique)}件／{len(all_venues)}施設を抽出',flush=True)
    return sorted(unique.values(),key=lambda e:(e['date'],e['venue'],e['title']))


def fetch(session:requests.Session,url:str,max_bytes:int)->bytes:
    r=session.get(url,timeout=(12,30),stream=True)
    r.raise_for_status()
    content=bytearray()
    for chunk in r.iter_content(16384):
        content.extend(chunk)
        if len(content)>max_bytes:raise ValueError('公式ファイルがサイズ上限を超えました')
    return bytes(content)


def main()->None:
    now=datetime.now(JST)
    payload={'schema':1,'updatedAt':now.isoformat(timespec='seconds'),
             'status':'error','source':INDEX_URL,'sourceName':'高松市スポーツ協会',
             'events':[],'months':[],'warnings':[],'venues':list(VENUES)}
    with requests.Session() as session:
        session.headers.update({'User-Agent':UA,'Accept':'text/html,application/pdf'})
        try:
            html=fetch(session,INDEX_URL,1_000_000)
            months=discover_months(html,now)
            if not months:raise ValueError('当月・翌月の公式PDFリンクが見つかりません')
            records={}
            for year,month,url in months:
                try:
                    events=parse_pdf(fetch(session,url,8_000_000),year,month,url)
                    records.update({e['id']:e for e in events})
                    payload['months'].append(f'{year}-{month:02d}')
                except Exception as err:
                    msg=f'{year}-{month:02d}: {type(err).__name__}: {err}'
                    payload['warnings'].append(msg)
                    print('注意: '+msg,flush=True)
            payload['events']=sorted(records.values(),key=lambda e:(e['date'],e['venue'],e['title']))
            payload['status']=('partial' if payload['warnings'] else 'ok') if records else 'error'
        except Exception as err:
            msg=f'{type(err).__name__}: {err}'
            payload['warnings'].append(msg)
            print('取得失敗: '+msg,flush=True)
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'保存: {OUT} / {len(payload["events"])}件 / 状態 {payload["status"]}',flush=True)

if __name__=='__main__':main()
