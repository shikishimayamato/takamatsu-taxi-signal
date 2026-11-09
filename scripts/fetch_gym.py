#!/usr/bin/env python3
"""高松市スポーツ協会の月間PDFを施設別に抽出（v0.7u）。

当月・翌月・翌々月の公開済PDFを対象にする。2列×複数頁の行事予定から
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
UA = 'TakamatsuTaxiSignal/0.7u (scheduled public schedule downloader)'

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
    "高松市総合体育館": "福岡町",
    "香川総合体育館": "香川町川東下",
    "仏生山公園体育館": "仏生山町",
    "国分寺橘ノ丘総合運動公園": "国分寺町新名",
    "西部運動センター": "鬼無町鬼無",
    "かわなべスポーツセンター": "川部町",
    "東部運動公園": "高松町",
    "亀水運動センター": "亀水町",
    "りんくうスポーツ公園": "香南町岡",
    "牟礼総合体育館": "牟礼町牟礼",
    "香南体育館": "香南町横井",
    "ホタルと文化の里運動場グラウンド": "塩江町安原上",
    "牟礼中央公園運動センター": "牟礼町原",
}
MONTH_RX = re.compile(r'令和\s*(\d+)年\s*(\d{1,2})月')
DAY_RX = re.compile(r'^(\d{1,2})日\([月火水木金土日]\)$')
# 行末の「～29日」「・8日・15日」「・7日～8日」等だけ期間指定として読む。
TRAILING_DATES_RX = re.compile(r'((?:[・･]\s*\d{1,2}日(?:\s*[～~〜]\s*\d{1,2}日)?|[～~〜]\s*\d{1,2}日)\s*)+$')
MARK_RX = re.compile(r'([・･]|[～~〜])\s*(\d{1,2})日(?:\s*[～~〜]\s*(\d{1,2})日)?')


def tidy(s: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s)).strip()


def target_months(now: datetime) -> list[tuple[int, int]]:
    """当月・翌月・翌々月。12月から翌年への切替も正確に扱う。"""
    return [(now.year + (now.month - 1 + n) // 12,
             (now.month - 1 + n) % 12 + 1) for n in range(3)]


def discover_months(html: str, now: datetime) -> list[tuple[int, int, str]]:
    """公式サイトに掲載された当月から翌々月のPDFリンクだけを返す。"""
    wanted = set(target_months(now))
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
                    'note':'高松市スポーツ協会の月間行事予定（時刻・参加人数は未公表）',
                })
    # 公式の2列表で全施設が見えなくなった場合、誤データを正常更新扱いしない。
    if len(all_venues)<10:
        raise ValueError(f'施設別の抽出が不足: {len(all_venues)}施設。PDFの配置を再確認してください。')
    # v0.7t: 毎月共通のPDF脚注除外と保守的な同一催事統合
    def norm_event_title(value):
        s = unicodedata.normalize('NFKC', value).casefold()
        return re.sub(r'[\W_]+', '', s)

    def is_pdf_footer(value):
        s = tidy(value)
        # 連絡先・ウェブアドレス・所在地。特定の住所や年月には依存しない。
        contact = r'(?:https?\s*[:：]//|www\.|url\s*[:：]//|(?:^|\s)(?:tel|fax|電話|e-?mail)\s*[:：])'
        address = r'\d+\s*丁目\s*\d+\s*番(?:地)?\s*\d+\s*号'
        return bool(re.search(contact, s, re.I) or re.search(address, s))

    def shared_event_alias(base, detailed):
        # 「地域名（公式大会名）」のように、短い大会名が括弧内に丸ごとある場合のみ。
        m = re.fullmatch(r'\s*(.*?)\s*[（(]\s*([^()（）]+)\s*[)）]\s*(.*?)\s*', detailed)
        if not m or norm_event_title(m.group(2)) != norm_event_title(base):
            return None
        extra = tidy(m.group(1) + ' ' + m.group(3))
        if len(norm_event_title(base)) < 10 or not 2 <= len(extra) <= 20:
            return None
        # 別競技・別部門・別の試合段階は誤統合を避ける。
        independent = r'予選|決勝|準決勝|決定戦|敗者復活|女子|男子|個人|団体|午前|午後|第[0-9一二三四五六七八九十]+部|U[-ー]?[0-9]+'
        if re.search(independent, extra, re.I):
            return None
        return extra

    footer_rows = [e for e in parsed if is_pdf_footer(e['title'])]
    unique = {e['id']: e for e in parsed if not is_pdf_footer(e['title'])}
    merged_rows = []
    groups = {}
    for event in unique.values():
        key = (event['date'], event.get('endDate', event['date']), event['venue'])
        groups.setdefault(key, []).append(event)
    for group in groups.values():
        for first in group:
            if first['id'] not in unique:
                continue
            for second in group:
                if first is second or second['id'] not in unique:
                    continue
                # 時刻が両方に存在して相違する場合は独立開催とみなす。
                if any(first.get(k) and second.get(k) and first[k] != second[k]
                       for k in ('startAt', 'endAt', 'doorAt')):
                    continue
                short, long = sorted((first, second), key=lambda e: len(norm_event_title(e['title'])))
                norm_short, norm_long = norm_event_title(short['title']), norm_event_title(long['title'])
                alias = None if norm_short == norm_long else shared_event_alias(short['title'], long['title'])
                if norm_short != norm_long and not alias:
                    continue
                if alias:
                    # 元の短い催事のIDは変えず、端末内の補正履歴との対応を保持する。
                    short['title'] = short['title'] + '（' + alias + '）'
                    short['note'] += '／公式PDF内の同一大会表記を統合'
                merged_rows.append((short['date'], short['venue'], long['title'], short['title']))
                del unique[long['id']]
                if long is first:
                    break
    print(f'PDF脚注の除外: {len(footer_rows)}件 / 同一催事の統合: {len(merged_rows)}件', flush=True)
    for when, venue, duplicate_title, main_title in merged_rows:
        print(f'統合: {when} {venue} / {duplicate_title} → {main_title}', flush=True)
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
             'events':[],'months':[],'monthStatus':{},
             'warnings':[],'venues':list(VENUES)}
    with requests.Session() as session:
        session.headers.update({'User-Agent':UA,'Accept':'text/html,application/pdf'})
        try:
            html=fetch(session,INDEX_URL,1_000_000)
            months=discover_months(html,now)
            # 「未掲載」と「PDFは掲載されているが取得失敗」を厳密に区別する。
            # 一覧ページが取得できない場合は未掲載と推定しない。
            payload['monthStatus']={f'{y}-{m:02d}':'not_listed'
                                    for y,m in target_months(now)}
            if not months:
                raise ValueError('当月から翌々月の公式PDFリンクが見つかりません')
            records={}
            for year,month,url in months:
                key=f'{year}-{month:02d}'
                try:
                    payload['monthStatus'][key]='pending'
                    events=parse_pdf(fetch(session,url,8_000_000),year,month,url)
                    records.update({e['id']:e for e in events})
                    payload['months'].append(key)
                    payload['monthStatus'][key]='ok'
                except Exception as err:
                    payload['monthStatus'][key]='error'
                    msg=f'{key}: {type(err).__name__}: {err}'
                    payload['warnings'].append(msg)
                    print('注意: '+msg,flush=True)
            payload['events']=sorted(records.values(),key=lambda e:(e['date'],e['venue'],e['title']))
            print('対象月の判定: '+json.dumps(payload['monthStatus'],ensure_ascii=False),flush=True)
            payload['status']=('partial' if payload['warnings'] else 'ok') if records else 'error'
        except Exception as err:
            msg=f'{type(err).__name__}: {err}'
            payload['warnings'].append(msg)
            print('取得失敗: '+msg,flush=True)
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'保存: {OUT} / {len(payload["events"])}件 / 状態 {payload["status"]}',flush=True)

if __name__=='__main__':main()
