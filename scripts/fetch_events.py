#!/usr/bin/env python3
"""公式催事のメタデータ取得: あなぶきアリーナ香川 + サンポートホール高松。

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
SUNPORT = 'https://www.sunport-hall.jp/'
# 公式施設サイトの同一内容を公開しているサブドメイン。
# TLS 証明書エラーの場合のみ次のホストを試す。各ホストのrobots.txtは個別に検証する。
SUNMESSE_HOSTS = (SUNMESSE, 'https://pop.sunmesse.com/', 'https://ftp.sunmesse.com/')
SOURCES = {'arena': ARENA, 'sunport': SUNPORT} # サンメッセは調査中につき停止
ALLOWED_ORIGINS = frozenset((ARENA, SUNPORT) + SUNMESSE_HOSTS)

class TLSVerifyError(RuntimeError):
    """HTTPS 証明書の検証に失敗。アクセス拒否/robotsの拒否とは区別する。"""
    pass
PUBLIC_BACKUP = 'https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json'
OUT = Path(__file__).resolve().parents[1] / 'data' / 'events.json'
USER_AGENT = 'TakamatsuTaxiSignal/0.4.1 (+public source attribution; twice-daily metadata fetch)'
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
    # Content-Type が charset を省略すると requests は Latin-1 と誤認することがある。
    # まず HTTP / HTML の明示的指定を尊重し、未指定ならHTMLの meta charset を調べる。
    # 文字化けすると日付の日本語「年/月/日」を正規表現が見つけられない。
    header = r.headers.get('Content-Type', '')
    if 'charset=' not in header.lower():
        detected = re.search(rb'<meta[^>]+charset\s*=\s*[\"\']?([\w-]+)', r.content[:10000], re.I)
        if not detected:
            detected = re.search(rb'<meta[^>]+content\s*=\s*[\"\'][^>]*charset=([\w-]+)', r.content[:10000], re.I)
        if detected:
            r.encoding = detected.group(1).decode('ascii', errors='ignore')
        elif r.encoding is None or r.encoding.lower() in ('iso-8859-1', 'latin-1'):
            try:
                r.content.decode('utf-8', errors='strict')
                r.encoding = 'utf-8'
            except UnicodeDecodeError:
                r.encoding = r.apparent_encoding or 'utf-8'
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



# ---- Sunport Hall Takamatsu: official, public hall calendar; Sunmesse is paused ----
# 公式ページの各催事には「催事名 / YYYY年M月D日 / 会場 / 時間」が表示される。
# 配信HTMLのクラス名変更に比較的強いテキスト解析と、詳細リンクの照合を併用。
SUNPORT_DATE_RE = re.compile(r'^\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:\([^)]{0,12}\))?\s*(.*)$')
SUNPORT_HALL_RE = re.compile(r'(?:大ホール|第[12]小ホール|市民ギャラリー|コミュニケーションプラザ|国際会議場|会議室|展示場|多目的室|ホール棟)')


def sunport_normalize(txt: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', re.sub(r'([①-⑳])', r' \1 ', txt or ''))).strip()


def parse_sunport_calendar(html: str, page_url: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    # 主サイト全体の案内メニュー・フッターを混入させない。
    main = soup.find('main') or soup.find(id='contents') or soup.body or soup
    for tag in main.select('script,style,nav,footer,header'):
        tag.decompose()
    lines = [sunport_normalize(x) for x in main.get_text('\n', strip=True).splitlines()]
    lines = [x for x in lines if x]
    links: dict[str,str] = {}
    for a in main.select('a[href]'):
        title = sunport_normalize(a.get_text(' ', strip=True))
        url = urljoin(SUNPORT, a['href'])
        if (urlparse(url).scheme == 'https' and urlparse(url).hostname == 'www.sunport-hall.jp'
                and re.search(r'/hall/20\d{2}/[0-9]+\.html$', urlparse(url).path)):
            links[title] = url
    out = []
    for i, line in enumerate(lines):
        match = SUNPORT_DATE_RE.match(line)
        if not match:
            continue
        try:
            day = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        if not (now.date()-timedelta(days=1) <= day <= now.date()+timedelta(days=120)):
            continue
        # 公式カレンダーはイベントタイトルを開催日時の直前に置く。
        recent_titles = []
        for old in lines[max(0, i-5):i]:
            if (2 <= len(old) <= 150 and not SUNPORT_DATE_RE.match(old)
                    and not re.match(r'^(?:20\d{2}年\d+月|ホールイベント|お知らせ|イベントカレンダー|入場料等|お問合せ)$',old)):
                recent_titles.append(old)
        if not recent_titles:
            continue
        title = recent_titles[-1]
        if (title in {'詳しくはこちら', '大ホール', '第1小ホール', '第2小ホール'}
                or re.fullmatch(r'\d{1,2}', title)):
            continue
        # 開催日直後から「入場料等」等の見出し手前までを掲載時刻とする。
        slot_lines = [match[4]]
        for future in lines[i+1:i+7]:
            if future in {'入場料等', 'お問合せ', '料金', '発売'} or SUNPORT_DATE_RE.match(future):
                break
            slot_lines.append(future)
        block = ' '.join(slot_lines)
        ranges = TIME_RANGE.findall(block)
        start, end = None, None
        if ranges:
            start = f'{int(ranges[0][0]):02d}:{ranges[0][1]}'
            end = f'{int(ranges[-1][2]):02d}:{ranges[-1][3]}'
            if start > end:
                start, end = None, None
        hall = SUNPORT_HALL_RE.search(block)
        room = hall.group(0) if hall else ''
        tag = '関係者のみ（一般来場不可）' if '関係者のみ' in ' '.join(lines[i+1:i+12]) else ''
        # 複数公演では、最初の開演と最後の掲載終了時刻。連続開催と誤認させない。
        multiple = len(ranges) > 1
        note = ('公式掲載の開催時間。' if start and end else '公表時刻未確認。')
        if multiple: note += ' 複数回公演（時間帯は連続開催ではありません）。'
        if room: note += f' 会場：{room}。'
        if tag: note += f' {tag}。'
        note += ' 掲載終了時刻は実際の退場時刻・タクシー需要を保証しません。'
        ds = day.isoformat()
        out.append({'id':stable_id('サンポートホール高松', title, ds),
             'title':title[:110], 'date':ds,
             'startAt':f'{ds}T{start}:00+09:00' if start else None,
             'endAt':f'{ds}T{end}:00+09:00' if end else None,
             'endTimeType':'公式掲載の催事終了予定' if end else None,
             'venue':'サンポートホール高松', 'zone':'station',
             'source':'公式・サンポートホール高松',
             'url':links.get(title, page_url), 'note':note})
    return dedupe(out)


# v0.4.1: 月間カレンダーの構造や改行に左右されない詳細ページ経由の予備取得。
SUNPORT_DETAIL_PATH = re.compile(r'^/hall/20\d{2}/[0-9]+\.html$')
SUNPORT_ANY_DATE = re.compile(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日')


def sunport_detail_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, 'html.parser')
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    # 本文の <main> に限定しない。サイトにより予定表が別コンテナにある。
    for tag in soup.select('a[href]'):
        uri = urljoin(SUNPORT, tag.get('href', ''))
        parsed = urlparse(uri)
        if parsed.hostname not in ('www.sunport-hall.jp', 'sunport-hall.jp'):
            continue
        if not SUNPORT_DETAIL_PATH.fullmatch(parsed.path):
            continue
        clean_uri = urljoin(SUNPORT, parsed.path.lstrip('/'))
        title = sunport_normalize(tag.get_text(' ', strip=True))
        if not title or clean_uri in seen:
            continue
        seen.add(clean_uri)
        found.append((clean_uri, title))
    return found


def parse_sunport_detail(html: str, uri: str, link_title: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    # 明確な日付が書かれた公式の個別催事ページだけを採用する。
    text = unicodedata.normalize('NFKC', soup.get_text(' ', strip=True))
    dates = list(SUNPORT_ANY_DATE.finditer(text))
    if not dates:
        return []
    # 個別ページにはタイトルが別表記で出ることもあるため、カレンダーのリンク名を採用。
    title = sunport_normalize(link_title)
    if not (2 <= len(title) <= 160):
        return []
    # タイトルを起点に「日付と開催時間」の部分を探す。最初の開催日を優先。
    output: list[dict] = []
    for m in dates[:5]:
        try:
            day = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        if not (now.date() - timedelta(days=1) <= day <= now.date() + timedelta(days=120)):
            continue
        # 別の文脈の日時を拾わないよう日付後200文字以内の時間帯に限定。
        after = text[m.end():m.end()+210]
        # 続く「入場料等」「お問合せ」以降は催事本文ではない。
        after = re.split(r'入場料等|お問合せ|問い合わせ|料金', after, maxsplit=1)[0]
        ranges = TIME_RANGE.findall(after)
        start = end = None
        if ranges:
            start = f'{int(ranges[0][0]):02d}:{ranges[0][1]}'
            end = f'{int(ranges[-1][2]):02d}:{ranges[-1][3]}'
            if start > end:
                start = end = None
        ds = day.isoformat()
        hall = SUNPORT_HALL_RE.search(after)
        note = '公式個別ページの掲載時間。' if start and end else '開催時刻未確認。'
        if len(ranges) > 1:
            note += ' 複数の開催時間帯が記載されています。'
        if hall:
            note += f' 会場：{hall.group(0)}。'
        if '関係者のみ' in text:
            note += ' 関係者のみ。'
        note += ' 掲載終了時刻は実際の退場時刻を保証しません。'
        output.append({
            'id': stable_id('サンポートホール高松', title, ds),
            'title': title[:110], 'date': ds,
            'startAt': f'{ds}T{start}:00+09:00' if start else None,
            'endAt': f'{ds}T{end}:00+09:00' if end else None,
            'endTimeType': '公式掲載の催事終了予定' if end else None,
            'venue': 'サンポートホール高松', 'zone': 'station',
            'source': '公式・サンポートホール高松', 'url': uri, 'note': note,
        })
        # 1つの詳細ページ内の別日付（お問合せ・告知等）の誤認を防ぐ。
        # 複数日開催を個別に掲載するサイトなので、基本的に最初の該当日だけ。
        break
    return output


def fetch_sunport(now: datetime) -> list[dict]:
    urls = [urljoin(SUNPORT, 'hall/'), urljoin(SUNPORT, 'hall/next.html')]
    rows: list[dict] = []
    detail_links: dict[str, str] = {}
    pages_fetched = 0
    for url in urls:
        try:
            html = get_html(url)
            pages_fetched += 1
            print(f'サンポート診断: {url}: HTML {len(html)}文字、日付表記 {html.count("年")}箇所、催事リンク候補 {len(sunport_detail_links(html))}件')
            part = parse_sunport_calendar(html, url, now)
            if part:
                rows.extend(part)
                print(f'サンポートホール: 月間ページから {len(part)}件抽出')
            else:
                print(f'サンポートホール: 月間ページの直接解析0件（詳細ページ方式で再試行）', file=sys.stderr)
            for uri, title in sunport_detail_links(html):
                detail_links[uri] = title
        except Exception as exc:
            print(f'サンポートホール月間ページ警告: {url}: {exc}',file=sys.stderr)
        time.sleep(.4)
    # 一覧解析が十分に取れた場合は詳細ページの大量アクセスをしない。
    # 取りこぼしがある場合だけ件数を抑えて個別ページから補完。
    if len(rows) < 5:
        print(f'サンポート診断: 個別催事リンク {len(detail_links)}件、詳細取得を最大40ページ試行')
        details_ok = 0
        errors = []
        for uri, title in list(detail_links.items())[:40]:
            try:
                part = parse_sunport_detail(get_html(uri), uri, title, now)
                if part:
                    details_ok += 1
                    rows.extend(part)
            except Exception as exc:
                if len(errors) < 5:
                    errors.append(f'{uri}: {exc}')
            time.sleep(.45)
        print(f'サンポート診断: 個別詳細の解析成功 {details_ok}件 / 候補 {min(len(detail_links), 40)}件')
        for msg in errors:
            print(f'サンポート詳細ページ警告: {msg}', file=sys.stderr)
    result = dedupe(rows)
    if not result:
        raise RuntimeError(f'サンポート公式ページ取得済 {pages_fetched}、詳細リンク {len(detail_links)}、抽出催事0件。HTML構造・文字コードを要調査')
    return result


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
    for key, fetcher, venue in [('arena',fetch_arena,'あなぶきアリーナ香川'),('sunport',fetch_sunport,'サンポートホール高松')]:
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
    # サンメッセはSSL問題の解決待ち。今回はアクセスせず、取得結果も公開しない。
    statuses.append({'venue':'サンメッセ香川','status':'paused','count':0,'checkedAt':now.isoformat(timespec='seconds')})
    results=dedupe(results)[:300]
    if not results:
        print('エラー: 有効な催事が0件のため更新しません。現在公開済みのサイトを維持します。',file=sys.stderr)
        return 1
    payload={'schema':1,'updatedAt':now.isoformat(timespec='seconds'),
             'source':'複数公式サイト','sourceName':'あなぶきアリーナ香川・サンポートホール高松（公式）',
             'verifiedFinishTime':False,'events':results,
             'sources':statuses,
             'partial':any(x['status'] in ('stale','error') for x in statuses)}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(f'保存: {OUT} / {len(results)}件 / '+', '.join(f"{x['venue']}:{x['status']} {x['count']}件" for x in statuses))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
