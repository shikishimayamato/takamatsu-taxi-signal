#!/usr/bin/env python3
"""高松市総合体育館の公式月間大会予定表だけを抽出する。

PDF全体には複数の施設が左右2列に掲載されているため、
1ページ目左列の「高松市総合体育館」の見出しから
「西部運動センター」の見出しまでを対象にする。
時刻・人数が未公表の予定は補完しない。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
VENUE = '高松市総合体育館'
HEAD = '高松市総合体育館'
TAIL = '西部運動センター'
HEADERS = {'香川総合体育館', '仏生山公園体育館', '西部運動センター',
           '国分寺橘ノ丘総合運動公園', '亀水運動センター', '牟礼総合体育館'}
UA = 'TakamatsuTaxiSignal/0.7e (+local community project, 2 requests/day)'
MONTH_RX = re.compile(r'令和\s*(\d+)年\s*(\d{1,2})月')
DAY_RX = re.compile(r'^([0-9]{1,2})日(?:\([月火水木金土日]\))?\s*(.+)$')
EXTRA_DAY_RX = re.compile(r'[・･]\s*([0-9]{1,2})日\s*$')
END_DAY_RX = re.compile(r'[〜～~]\s*([0-9]{1,2})日\s*$')


def tidy(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip()


def discover_months(html: str, now: datetime) -> list[tuple[int, int, str]]:
    """公開済みの当月・翌月に限定。存在しない月のURLは生成しない。"""
    soup = BeautifulSoup(html, 'html.parser')
    wanted = {(now.year, now.month)}
    nxt = now.replace(day=28) + timedelta(days=5)
    wanted.add((nxt.year, nxt.month))
    candidates = []
    for a in soup.select('a[href]'):
        target = tidy(a.get_text(' ', strip=True))
        m = MONTH_RX.search(target)
        url = urljoin(INDEX_URL, a.get('href', ''))
        parsed = urlparse(url)
        if not m or parsed.scheme != 'https' or parsed.netloc != 'www.taka-spo.or.jp':
            continue
        if not parsed.path.startswith(ALLOWED_PATH) or not parsed.path.lower().endswith('.pdf'):
            continue
        year = 2018 + int(m.group(1))  # 令和元年は2019年
        month = int(m.group(2))
        if 1 <= month <= 12 and (year, month) in wanted:
            candidates.append((year, month, url))
    return sorted(set(candidates))


def left_column_lines(page: fitz.Page) -> list[str]:
    """座標で左列だけを取り出し、PDF内の2列混在を防ぐ。"""
    left = fitz.Rect(0, 0, page.rect.width * 0.505, page.rect.height)
    words = page.get_text('words', clip=left, sort=True)
    grouped = []
    for w in sorted(words, key=lambda v: (v[1], v[0])):
        x0, y0, x1, _, token = w[:5]
        if not grouped or abs(grouped[-1]['y'] - y0) > 2.5:
            grouped.append({'y': y0, 'w': [(x0, x1, str(token))]})
        else:
            grouped[-1]['w'].append((x0, x1, str(token)))
    result = []
    for g in grouped:
        seq = sorted(g['w'])
        text = ''
        last_end = None
        for x0, x1, word in seq:
            # 英数字の大きな間隔だけ区切る。日本語の語間には空白不要。
            if last_end is not None and x0 - last_end > 5 and text and re.search(r'[A-Za-z0-9]$', text) and re.match(r'[A-Za-z0-9]', word):
                text += ' '
            text += word
            last_end = x1
        text = tidy(text)
        if text:
            result.append(text)
    return result


def parse_pdf(data: bytes, year: int, month: int, source_url: str) -> list[dict]:
    doc = fitz.open(stream=data, filetype='pdf')
    if len(doc) < 1:
        raise ValueError('PDFにページがありません')
    lines = left_column_lines(doc[0])
    # 設定した施設見出しがないとき、他施設へフォールバックしない。
    starts = [i for i, text in enumerate(lines) if HEAD in text]
    ends = [i for i, text in enumerate(lines) if TAIL in text]
    if not starts or not ends or ends[0] <= starts[0]:
        raise ValueError('高松市総合体育館の欄を独立して判別できません。PDFのレイアウトを確認してください。')
    section = lines[starts[0]+1:ends[0]]
    records = []
    current = None
    for line in section:
        if line in HEADERS or '現在' in line and ('令和' in line or '年' in line):
            continue
        matched = DAY_RX.match(line)
        if matched:
            if current:
                records.append(current)
            current = {'day': int(matched.group(1)), 'title': matched.group(2).strip()}
        elif current and line and not re.match(r'^[※*]', line):
            # 同じ日付の次行に続く催事名を保持。断定できない区切りは作らない。
            current['title'] = (current['title'] + ' ' + line).strip()
    if current:
        records.append(current)
    # v0.7d：公式PDFで日付が省略された別催事の誤結合を防止する。
    # この既知事例は、同日の「新人体育大会」と「レディース1日バドミントン教室」。
    # 当該語句が現れた位置でだけ分割し、他の折返し行は勝手に分割しない。
    fixed_records = []
    for item in records:
        pieces = re.split(r'(?=高松市レディース\s*1日バドミントン教室)', item['title'])
        for part in pieces:
            if part.strip():
                fixed_records.append({'day': item['day'], 'title': part.strip()})
    records = fixed_records
    # v0.7e: PDFの省略日付・折返し行で連結された別々の大会を切り離す。
    # 明らかな催事名の境界だけを分離し、曖昧な部分は推測して追加しない。
    cleaned = []
    for item in records:
        title = tidy(item['title'])
        title = re.sub(r'^\([月火水木金土日]\)\s*', '', title)
        # 11/1: 後半に別大会の途中までが入り込むので、確認できる前半のみ採用。
        title = re.sub(r'\s+第6回南海プライウッド杯兼\s*$', '', title)
        # 11/8: 「・15日」に続く別催事は15日に登録。
        next_event = re.search(r'\s*[・･]\s*(15)日\s*(キッズアカデミア.+)$', title)
        if next_event:
            first = title[:next_event.start()].strip()
            if first:
                cleaned.append({'day': item['day'], 'title': first})
            cleaned.append({'day': int(next_event.group(1)), 'title': next_event.group(2)})
            continue
        # 11/12・21: 日付が省略された二つ目の行事。
        for anchor in ('フォークダンス舞夢45周年記念大会', '第23回香川県小学生バドミントン普及大会'):
            pos = title.find(anchor)
            if pos > 0:
                first = title[:pos].strip()
                if first:
                    cleaned.append({'day': item['day'], 'title': first})
                title = title[pos:].strip()
        if title:
            cleaned.append({'day': item['day'], 'title': title})
    records = cleaned
    result = []
    for r in records:
        day = r['day']
        if day < 1 or day > 31:
            continue
        try:
            start = datetime(year, month, day, tzinfo=JST).date()
        except ValueError:
            continue
        title = tidy(r['title'])
        dates = [start]
        extra = EXTRA_DAY_RX.search(title)
        if extra:
            n = int(extra.group(1))
            if n > day and n <= 31:
                try:
                    dates.append(datetime(year, month, n, tzinfo=JST).date())
                    title = title[:extra.start()].rstrip(' ・･')
                except ValueError:
                    pass
        thru = END_DAY_RX.search(title)
        last = start
        if thru:
            n = int(thru.group(1))
            if n >= day and n <= 31:
                try:
                    last = datetime(year, month, n, tzinfo=JST).date()
                    title = title[:thru.start()].rstrip(' ~〜～')
                except ValueError:
                    pass
        if not title or len(title) > 120:
            print(f'除外: 催事名が不正・長すぎる（{year}-{month:02d}-{day:02d}）', flush=True)
            continue
        for date in dates:
            date_s = date.isoformat()
            end_s = (last if date == start else date).isoformat()
            key = hashlib.sha256(f'gym|{date_s}|{title}'.encode('utf-8')).hexdigest()[:16]
            result.append({
                'id': 'gym-'+key, 'date': date_s, 'endDate': end_s,
                'title': title, 'venue': VENUE, 'zone': 'station',
                'url': source_url, 'doorAt': None, 'startAt': None, 'endAt': None,
                'participants': None,
                'note': ('高松市スポーツ協会・公式月間行事予定（開催時刻・参加人数は未掲載）' + ('／催事名が途中で切れている可能性があります。公式PDFで再確認してください。' if title.endswith('香川県') else '')) ,
            })
    # 明らかに空なら成功とみなさない。
    if not result:
        raise ValueError('体育館の催事が1件も抽出できませんでした')
    return result


def fetch(session: requests.Session, url: str, max_bytes: int) -> bytes:
    # 証明書検証を無効化しない。ダウンロードサイズを制限。
    response = session.get(url, timeout=(12, 30), stream=True)
    response.raise_for_status()
    body = bytearray()
    for chunk in response.iter_content(16384):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError('公式ファイルが想定サイズを超過しました')
    return bytes(body)


def main() -> None:
    now = datetime.now(JST)
    payload = {'schema': 1, 'updatedAt': now.isoformat(timespec='seconds'),
               'status': 'error', 'source': INDEX_URL,
               'sourceName': '高松市スポーツ協会', 'events': [], 'months': [], 'warnings': []}
    session = requests.Session()
    session.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/pdf'})
    try:
        html = fetch(session, INDEX_URL, 1_000_000)  # BeautifulSoup側で文字コードを判定
        months = discover_months(html, now)
        if not months:
            raise ValueError('公式ページに当月・翌月のPDFがありません')
        seen = set()
        for year, month, url in months:
            try:
                pdf = fetch(session, url, 8_000_000)
                events = parse_pdf(pdf, year, month, url)
                for ev in events:
                    if ev['id'] not in seen:
                        payload['events'].append(ev)
                        seen.add(ev['id'])
                payload['months'].append(f'{year}-{month:02d}')
                print(f'高松市総合体育館 {year}-{month:02d}: {len(events)}件 / {url}', flush=True)
            except Exception as exc:
                msg = f'{year}-{month:02d}: {type(exc).__name__}: {exc}'
                payload['warnings'].append(msg)
                print('注意: '+msg, flush=True)
        if payload['events']:
            payload['status'] = 'partial' if payload['warnings'] else 'ok'
        else:
            payload['status'] = 'error'
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'
        payload['warnings'].append(msg)
        print('取得失敗: '+msg, flush=True)
    payload['events'].sort(key=lambda x:(x['date'],x['title']))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f'保存: {OUT} / {len(payload["events"])}件 / 状態 {payload["status"]}', flush=True)

if __name__ == '__main__':
    main()
