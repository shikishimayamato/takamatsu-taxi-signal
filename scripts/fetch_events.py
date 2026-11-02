#!/usr/bin/env python3
"""公式催事のメタデータ取得: あなぶきアリーナ香川 + サンメッセ香川。

公表された日程・開始・開催終了時刻のみを取得し、乗車需要は推測しない。
ソースの一方が取得不能なら公開済みの直近データを補助的に保持。
両方取得不能で有効なバックアップがないときは失敗させ、空データを公開しない。
サイト利用条件とrobots.txtを尊重し、低頻度の取得に限る。
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, date
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo('Asia/Tokyo')
ARENA = 'https://kagawa-arena.com/'
SUNMESSE = 'https://www.sunmesse.com/'
# 公式施設サイトの同一内容を公開しているサブドメイン。
# TLS 証明書エラーの場合のみ次のホストを試す。各ホストのrobots.txtは個別に検証する。
SUNMESSE_HOSTS = (SUNMESSE, 'https://pop.sunmesse.com/', 'https://ftp.sunmesse.com/')
SOURCES = {'arena': ARENA, 'sunmesse': SUNMESSE}
ALLOWED_ORIGINS = frozenset((ARENA,) + SUNMESSE_HOSTS)

class TLSVerifyError(RuntimeError):
    """HTTPS 証明書の検証に失敗。アクセス拒否/robotsの拒否とは区別する。"""
    pass
PUBLIC_BACKUP = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
USER_AGENT = 'TakamatsuTaxiSignal/0.3.2 (+public source attribution; twice-daily metadata fetch)'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'ja-JP,ja;q=0.9'})
ROBOTS: dict[str, RobotFileParser] = {}


def can_fetch(url: str) -> bool:
    parsed = urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}/'
    if origin not in ALLOWED_ORIGINS:
        raise ValueError('許可されていない公式サイト: '+origin)
    if origin not in ROBOTS:
        rp = RobotFileParser()
        robots_url = urljoin(origin, 'robots.txt')
        rp.set_url(robots_url)
        try:
            # urllib.robotparser.read() は urllib の証明書ストアを使う。
            # 催事ページと同じ requests / certifi の通常のTLS検証で取得し、
            # RobotFileParser に解析させる。verify=False は使用しない。
            response = SESSION.get(robots_url, timeout=15)
            if response.status_code == 200:
                if len(response.content) > 500_000:
                    raise ValueError('robots.txtが大きすぎます')
                rp.parse(response.text.splitlines())
            elif response.status_code == 404:
                # robots.txt が存在しない場合は制限の記載なしとして扱う。
                # ページの利用条件やサーバーの応答には引き続き従う。
                rp.parse([])
            elif response.status_code in (401, 403):
                raise PermissionError(f'robots.txtへのアクセス拒否 (HTTP {response.status_code})')
            else:
                # 429/5xx/リダイレクト異常/その他は一時的に取得を中止。
                raise RuntimeError(f'robots.txtを確認できません (HTTP {response.status_code})')
        except requests.exceptions.SSLError as exc:
            raise TLSVerifyError(f'robots.txtのTLS証明書検証失敗: {origin} / {exc}') from exc
        except Exception as exc:
            raise PermissionError(f'robots.txtを安全に確認できません: {origin} / {exc}') from exc
        ROBOTS[origin] = rp
    return ROBOTS[origin].can_fetch(USER_AGENT, url)


def get_html(url: str) -> str:
    if not can_fetch(url):
        raise PermissionError('robots.txtで取得が許可されていません: '+url)
    try:
        r = SESSION.get(url, timeout=22)
    except requests.exceptions.SSLError as exc:
        raise TLSVerifyError(f'催事ページのTLS証明書検証失敗: {url} / {exc}') from exc
    r.raise_for_status()
    if len(r.content) > 2_000_000:
        raise ValueError('ページが2MBを超えています')
    return r.text


def stable_id(venue: str, title: str, day: str) -> str:
    title = re.sub(r'[\s\u3000]+', '', unicodedata.normalize('NFKC', title))
    return hashlib.sha256(f'{venue}|{title}|{day}'.encode()).hexdigest()[:16]


def dedupe(records: list[dict]) -> list[dict]:
    """同一施設、同一名称、同一開催日の重複を取り除く。"""
    by_key: dict[tuple, dict] = {}
    for r in records:
        key = (r.get('venue'), r.get('date'), re.sub(r'\s+', '', unicodedata.normalize('NFKC', r['title'])))
        if key not in by_key:
            by_key[key] = r
        else:
            # より多くの時刻情報が載ったレコードを採用
            old = by_key[key]
            if int(bool(r.get('startAt'))) + int(bool(r.get('endAt'))) > int(bool(old.get('startAt'))) + int(bool(old.get('endAt'))):
                by_key[key] = r
    result = list(by_key.values())
    for r in result:
        r['id'] = stable_id(r['venue'], r['title'], r['date'])
    return sorted(result, key=lambda x: (x['date'], x['venue'], x['title']))


# ---- Arena: keep v0.2's successful event-detail flow, with duplicate cleanup ----
def arena_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for tag in soup.select('a[href]'):
        url = urljoin(ARENA, tag.get('href', ''))
        if re.fullmatch(r'https://kagawa-arena\.com/event/\d+/?', url) and url not in links:
            links.append(url)
    return links[:40]


def arena_dates(text: str, now: datetime) -> list[date]:
    found = []
    for yy, mm, dd in re.findall(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日', text):
        try:
            dt = date(int(yy), int(mm), int(dd))
            if now.date()-timedelta(days=1) <= dt <= now.date()+timedelta(days=120) and dt not in found:
                found.append(dt)
        except ValueError:
            pass
    if not found:
        for mm, dd in re.findall(r'(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)', text[:600]):
            for yy in (now.year, now.year+1):
                try:
                    dt = date(yy, int(mm), int(dd))
                    if now.date()-timedelta(days=1) <= dt <= now.date()+timedelta(days=120) and dt not in found:
                        found.append(dt)
                        break
                except ValueError:
                    continue
    return sorted(found)[:10]


def arena_starts(text: str) -> dict[str, str]:
    pattern = r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日[^\n]{0,45}?(?:開演|試合開始|開始)\s*[/：:\s]\s*([01]?\d|2[0-3]):([0-5]\d)'
    starts = {}
    for yy, mm, dd, hh, minute in re.findall(pattern, text):
        try:
            starts[date(int(yy),int(mm),int(dd)).isoformat()] = f'{int(hh):02d}:{minute}'
        except ValueError:
            continue
    return starts


def parse_arena_event(html: str, url: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html,'html.parser')
    h = soup.find('h1')
    if not h:
        return []
    title = re.sub(r'\s+', ' ', h.get_text(' ',strip=True)).strip()
    if not title or len(title)>160:
        return []
    text = soup.get_text('\n',strip=True)
    start = text.find(title)
    relevant = text[max(0,start-120):start+1600] if start>=0 else text[:1600]
    starts = arena_starts(relevant)
    records = []
    for day in arena_dates(relevant,now):
        ds = day.isoformat()
        st = starts.get(ds)
        records.append({'id':stable_id('あなぶきアリーナ香川',title,ds), 'title':title[:110],
            'date':ds,'startAt':f'{ds}T{st}:00+09:00' if st else None,'endAt':None,
            'endTimeType':None, 'venue':'あなぶきアリーナ香川','zone':'station',
            'source':'公式・あなぶきアリーナ香川','url':url,
            'note':'終演時刻未確認。会場周辺での駐停車・送迎規制に注意。'})
    return records


def fetch_arena(now: datetime) -> list[dict]:
    links = arena_links(get_html(ARENA))
    if not links:
        raise RuntimeError('催事リンク0件（サイト構造を確認）')
    print(f'アリーナ：詳細リンク {len(links)}件')
    rows = []
    failed = []
    for url in links:
        try:
            time.sleep(.6)
            rows.extend(parse_arena_event(get_html(url),url,now))
        except Exception as exc:
            failed.append(f'{url}: {exc}')
    for err in failed[:5]:
        print('アリーナ個別取得警告: '+err, file=sys.stderr)
    if not rows:
        raise RuntimeError(f'有効な催事0件、取得失敗リンク {len(failed)}件')
    return dedupe(rows)


# ---- Sunmesse: an event listing provides date range and published opening hours ----
DATE_SPAN = re.compile(
    r'(?P<year>20\d{2})\.(?P<mo>\d{1,2})\.(?P<day>\d{1,2})\s*\([^)]{0,4}\)\s*[-–—~〜～]\s*'
    r'(?:(?P<endyear>20\d{2})\.)?(?P<endmo>\d{1,2})\.(?P<endday>\d{1,2})\s*\([^)]{0,4}\)',
    re.M,
)
TIME_RANGE = re.compile(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*[～~〜―－–—-]\s*([01]?\d|2[0-3]):([0-5]\d)')
FINAL_DAY_END = re.compile(r'最終日は?\s*([01]?\d|2[0-3]):([0-5]\d)\s*まで')
IGNORABLE_TITLE = re.compile(r'^(?:一般|招待|予約|無料|有料|対象|主催|LINK|開催時間|会場|[○◯]|イベント情報|20\d{2}\.)')


def normalize_lines(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','nav','footer']):
        tag.decompose()
    text = soup.get_text('\n',strip=True)
    text = unicodedata.normalize('NFKC',text)
    text = text.replace('\r','')
    return '\n'.join(x.strip() for x in text.split('\n') if x.strip())


def sunmesse_month_url(now: datetime, offset: int, origin: str = SUNMESSE) -> str:
    month0 = (now.year * 12 + now.month-1)+offset
    return f'{origin}event/?date={month0//12:04d}{month0%12+1:02d}'


def parse_sunmesse_list(html: str, url: str, now: datetime) -> list[dict]:
    text = normalize_lines(html)
    spans = list(DATE_SPAN.finditer(text))
    records = []
    for idx, match in enumerate(spans):
        block = text[match.end():spans[idx+1].start() if idx+1<len(spans) else len(text)]
        # Limit post-date context so footer and unrelated article text cannot leak into it.
        block = block[:2200]
        lines = block.split('\n')
        title = None
        for line in lines[:12]:
            cleaned = line.strip('・●○◯ \t')
            if not cleaned or IGNORABLE_TITLE.match(cleaned) or cleaned in {'一般・無料','一般・有料','招待・無料','予約・無料','一般','招待'}:
                continue
            if re.fullmatch(r'[\s,、・/一-龯]+(?:無料|有料)',cleaned):
                continue
            title = cleaned
            break
        if not title or not 2<=len(title)<=150:
            continue
        try:
            first = date(int(match['year']),int(match['mo']),int(match['day']))
            lastyear = int(match['endyear']) if match['endyear'] else int(match['year'])
            last = date(lastyear,int(match['endmo']),int(match['endday']))
            if last<first:
                last = date(lastyear+1,int(match['endmo']),int(match['endday']))
        except ValueError:
            continue
        if (last-first).days>31:
            print('サンメッセ警告: 31日超の催事は省略 '+title[:30],file=sys.stderr)
            continue
        raw_hours = re.search(r'[◯○]?開催時間\s*[:：]\s*([^\n]{1,240})',block)
        hours = raw_hours.group(1) if raw_hours else ''
        ranges = TIME_RANGE.findall(hours)
        start_time, end_time = None, None
        if ranges:
            start_time = f'{int(ranges[0][0]):02d}:{ranges[0][1]}'
            end_time = f'{int(ranges[-1][2]):02d}:{ranges[-1][3]}'
        end_final = FINAL_DAY_END.search(hours)
        raw_place = re.search(r'[◯○]?会場\s*[:：]\s*([^\n]{1,110})',block)
        hall = raw_place.group(1).strip() if raw_place else ''
        day = first
        while day<=last:
            if now.date()-timedelta(days=1)<=day<=now.date()+timedelta(days=120):
                ds=day.isoformat()
                last_end = f'{int(end_final[1]):02d}:{end_final[2]}' if end_final and day==last else end_time
                valid_start = start_time and last_end and start_time<=last_end
                # Public hours are a scheduled session/window, not proof of when taxis are needed.
                records.append({'id':stable_id('サンメッセ香川',title,ds),
                    'title':title[:110],'date':ds,
                    'startAt':f'{ds}T{start_time}:00+09:00' if valid_start else None,
                    'endAt':f'{ds}T{last_end}:00+09:00' if valid_start else None,
                    'endTimeType':'公式掲載の開催時間終了' if valid_start else None,
                    'venue':'サンメッセ香川','zone':'sunmesse',
                    'source':'公式・サンメッセ香川','url':url,
                    'note':('会場：'+hall+'。 ' if hall else '') + ('開催時間 '+hours[:110]+'。 ' if hours else '開催時間未確認。 ') + '終了時刻は退場集中や乗車需要を保証しません。'})
            day += timedelta(days=1)
    return dedupe(records)


def fetch_sunmesse(now: datetime) -> list[dict]:
    """証明書検証が失敗した場合だけ、公式サイトの別ホストで再試行する。

    ホスト毎のrobots.txtを確認する。取得拒否/HTTPエラーなら他ホストへ逃げない。
    SSL証明書の検証を無効にはしない。
    """
    rows = []
    successful_pages = 0
    current_origin = SUNMESSE
    for offset in range(3):
        candidates = (current_origin,) + tuple(x for x in SUNMESSE_HOSTS if x != current_origin)
        for origin in candidates:
            url = sunmesse_month_url(now, offset, origin)
            try:
                html = get_html(url)
                current_origin = origin
                successful_pages += 1
                found = parse_sunmesse_list(html, url, now)
                print(f'サンメッセ：接続成功 {origin} / {len(found)}件（日別）')
                rows.extend(found)
                break
            except TLSVerifyError as exc:
                print(f'サンメッセTLS診断: {origin} の証明書検証に失敗。別ホストを確認: {exc}',file=sys.stderr)
                continue
            except Exception as exc:
                # robots.txtの拒否や403/429等なら別ドメインに迂回しない。
                print(f'サンメッセ月間ページ警告: {url}: {exc}',file=sys.stderr)
                break
        if offset < 2:
            time.sleep(.8)
    if successful_pages == 0:
        raise RuntimeError('サンメッセの月間ページを安全に取得できません。全候補の証明書・robots.txtを確認してください')
    if not rows:
        raise RuntimeError('サンメッセの催事情報を抽出できませんでした（仕様変更等を確認）')
    return dedupe(rows)


def read_backup(now: datetime) -> dict:
    # The Pages artifact is not committed back to the git repo; fetch its last published data.
    try:
        r=SESSION.get(PUBLIC_BACKUP,timeout=12,params={'v':now.strftime('%Y%m%d%H%M')})
        r.raise_for_status()
        if len(r.content)>600_000:
            return {}
        content=r.json()
        if content.get('schema')==1 and isinstance(content.get('events'),list) and len(content['events'])<=350:
            return content
    except Exception as exc:
        print(f'前回公開データの取得不可: {exc}',file=sys.stderr)
    return {}


def main() -> int:
    now=datetime.now(JST)
    previous = read_backup(now)
    results: list[dict] = []
    statuses = []
    for key, fetcher, venue in [('arena',fetch_arena,'あなぶきアリーナ香川'),('sunmesse',fetch_sunmesse,'サンメッセ香川')]:
        try:
            rows=fetcher(now)
            rows=dedupe(rows)
            results.extend(rows)
            statuses.append({'venue':venue,'status':'ok','count':len(rows),'checkedAt':now.isoformat(timespec='seconds')})
            print(f'{venue}: 新規取得 {len(rows)}件')
        except Exception as exc:
            print(f'警告: {venue} 取得失敗: {exc}',file=sys.stderr)
            prev_time=previous.get('updatedAt')
            try:
                recent=prev_time and (now-datetime.fromisoformat(prev_time)).total_seconds() < 7*24*3600
            except (ValueError,TypeError):
                recent=False
            old=[]
            if recent:
                old=[r for r in previous.get('events',[]) if r.get('venue')==venue and isinstance(r.get('date'),str) and r['date']>= (now.date()-timedelta(days=1)).isoformat()]
            if old:
                results.extend(old)
                statuses.append({'venue':venue,'status':'stale','count':len(old),'checkedAt':now.isoformat(timespec='seconds')})
                print(f'注意: {venue} は前回公開データを {len(old)}件 維持',file=sys.stderr)
            else:
                statuses.append({'venue':venue,'status':'error','count':0,'checkedAt':now.isoformat(timespec='seconds')})
    results=dedupe(results)[:300]
    if not results:
        print('エラー: 有効な催事が0件のため更新しません。現在公開済みのサイトを維持します。',file=sys.stderr)
        return 1
    payload={'schema':1,'updatedAt':now.isoformat(timespec='seconds'),
             'source':'複数公式サイト','sourceName':'あなぶきアリーナ香川・サンメッセ香川（公式）',
             'verifiedFinishTime':False,'events':results,
             'sources':statuses,
             'partial':any(x['status']!='ok' for x in statuses)}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(f'保存: {OUT} / {len(results)}件 / '+', '.join(f"{x['venue']}:{x['status']} {x['count']}件" for x in statuses))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
