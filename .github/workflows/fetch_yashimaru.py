#!/usr/bin/env python3
"""やしまーる・屋島山上の一般公開催事（公式）を既存events.jsonへ安全に統合。

出典: https://www.yashima-navi.jp/jp/event/mountain-top/
- 公式本文に年月日が明記された催事のみ採用。記事の公開日は開催日として使わない。
- 会場名がやしまーる、または屋島山上に明記される催事のみ。
- 営業時間や一般の展示・予約案内は催事と見なさない。
- 公式の開催時間は退場集中時間ではない（終了が不明ならendAtはnull）。
- TLS検証/robotsは必須。失敗した場合は前回の新しいデータを暫定保持。
- 既存施設のデータを変更しない。
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "events.json"
INDEX_URL = "https://www.yashima-navi.jp/jp/event/mountain-top/"
PUBLISHED = "https://shikishimayamato.github.io/takamatsu-taxi-signal/data/events.json"
VENUE = "やしまーる（屋島山上）"
SOURCE = "公式・やしまーる／屋島山上"
JST = ZoneInfo("Asia/Tokyo")
AGENT = "TakamatsuTaxiSignal/1.4 (+public-event-metadata; twice daily)"
ALLOWED_HOST = "www.yashima-navi.jp"
PATH_RX = re.compile(r"^/jp/event/(?:[a-z0-9-]+/)?entry-\d+\.html$")
FULL_DATE_RX = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ISO_DATE_RX = re.compile(r"(20\d{2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})(?!\d)")
HM = r"((?:[01]?\d|2[0-3]):[0-5]\d)"
RANGE_RX = re.compile(HM + r"\s*[～~〜―－–—-]\s*" + HM)
DATE_LABELS = ("開催日時", "開催期間", "開催日", "開催日程", "日時", "日程")
VENUE_LABELS = ("会場", "開催場所", "場所")
NOT_EVENT = re.compile(r"開館日|開館時間|休館日|利用案内|貸スペース|貸館|予約状況|通常営業|施設案内|定休日")
VENUE_RE = re.compile(r"やしまーる|屋島山上|屋島寺|獅子の霊巌|御成門前広場")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def safe_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and u.hostname == ALLOWED_HOST and bool(PATH_RX.fullmatch(u.path))


def to_date(y: str | int, m: str | int, d: str | int) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_dates(text: str) -> list[date]:
    """公式の開催日として明示された部分からだけ年月日を取る。"""
    s = norm(text)
    pairs: list[date] = []
    fulls = list(FULL_DATE_RX.finditer(s)) + list(ISO_DATE_RX.finditer(s))
    fulls.sort(key=lambda m: m.start())
    for m in fulls:
        d = to_date(*m.groups())
        if d and d not in pairs:
            pairs.append(d)
        # 2026年11月21日(土)・22日(日) または ～ 22日 の同月連続指定
        tail = s[m.end(): m.end()+46]
        extra = re.match(r"(?:\s*[（(][^()（）]{1,8}[）)])?\s*[、,・･]|(?:\s*[（(][^()（）]{1,8}[）)])?\s*[～〜~–—-]", tail)
        if not extra:
            continue
        n = re.match(r"\s*(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日", tail[extra.end():])
        if not n or not d:
            continue
        d2 = to_date(d.year, n[1] or d.month, n[2])
        if d2 is None:
            continue
        separator = tail[extra.start():extra.end()]
        if re.search(r"[～〜~–—-]",separator) and d <= d2 and (d2-d).days<=4:
            pairs.extend(d + timedelta(days=i) for i in range((d2-d).days+1))
        elif d2 not in pairs:
            pairs.append(d2)
    return sorted(set(pairs))


def detail_title(soup: BeautifulSoup) -> str:
    # 本文を優先。『event イベント』の共通見出しは採用しない。
    for css in ("article h1", "article h2", "main h1", "main h2", "h1", "h2"):
        for el in soup.select(css):
            title = norm(el.get_text(" ",strip=True))
            if 4 <= len(title) <= 150 and title not in {"イベント", "eventイベント", "屋島山上イベント", "お知らせ"}:
                return title
    meta = soup.find("meta",attrs={"property":"og:title"})
    return norm(meta.get("content","").split(" | ")[0])[:150] if meta else ""


def labeled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str:
    """table/dl/独立段落のラベルを探し、記事の公開日を混入させない。"""
    for tr in soup.select("tr"):
        cells = tr.find_all(["th","td"],recursive=False)
        if len(cells)>=2 and norm(cells[0].get_text(" ",strip=True)).rstrip("：:") in labels:
            return norm(" ".join(c.get_text(" ",strip=True) for c in cells[1:]))
    for dt in soup.select("dt"):
        if norm(dt.get_text(" ",strip=True)).rstrip("：:") in labels:
            dd=dt.find_next_sibling("dd")
            if dd: return norm(dd.get_text(" ",strip=True))
    # 一部の公式記事では表の代わりに『開催日時：...』という段落がある。
    rx = re.compile(r"^(?:"+"|".join(map(re.escape,labels))+r")\s*[:：]\s*(.+)$")
    for el in soup.select("p,li"):
        v = norm(el.get_text(" ",strip=True))
        match=rx.match(v)
        if match: return match.group(1)
    return ""


def parse_detail(html: str, url: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html,"html.parser")
    for e in soup(["script","style","nav","footer","noscript"]): e.decompose()
    title = detail_title(soup)
    if not title or NOT_EVENT.search(title): return []
    venue_val = labeled_value(soup,VENUE_LABELS)
    # 会場表記が別段落の場合にも対応。記事のタイトルだけを根拠にしない。
    scope = soup.select_one("article") or soup.select_one("main") or soup
    text = norm(scope.get_text(" ",strip=True))[:7000]
    if not VENUE_RE.search(venue_val) and not re.search(r"(?:会場|場所)\s*[:：]?\s*[^。]{0,80}(?:やしまーる|屋島山上|屋島寺|獅子の霊巌|御成門前広場)",text):
        return []
    date_val=labeled_value(soup,DATE_LABELS)
    # 『開催日時』がない記事は、公開日と開催日を誤認するため採用しない。
    if not date_val: return []
    days = [d for d in parse_dates(date_val) if now.date()-timedelta(days=1) <= d <= now.date()+timedelta(days=120)]
    if not days or len(days)>5: return []
    hours = RANGE_RX.search(date_val)
    start, finish = (hours[1], hours[2]) if hours else (None,None)
    if start and finish and start>finish: start=finish=None
    records=[]
    for day in days:
        ds=day.isoformat()
        sid=hashlib.sha256(f"{VENUE}|{ds}|{norm(title)}".encode()).hexdigest()[:16]
        start_at=f"{ds}T{start}:00+09:00" if start else None
        end_at=f"{ds}T{finish}:00+09:00" if finish else None
        records.append({
            "id":"yashimaru-"+sid,"title":title,"date":ds,
            "venue":VENUE,"venueArea":"屋島東町","zone":"other",
            "source":SOURCE,"url":url,
            "doorAt":None,"startAt":start_at,"endAt":end_at,
            "endTimeType":"公式掲載の開催時間終了" if end_at else None,
            "note":"屋島山上の一般公開催事。"+("終了時刻は公式掲載の開催時間。" if end_at else "終了時刻は未確認。")
        })
    return records


def find_links(html: str) -> list[str]:
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    for a in soup.select("a[href]"):
        u=urljoin(INDEX_URL,a["href"])
        if safe_url(u) and u not in out: out.append(u)
    return out


def fetch_official(now:datetime) -> tuple[list[dict],int]:
    session=requests.Session()
    session.headers.update({"User-Agent":AGENT,"Accept-Language":"ja-JP,ja;q=0.9"})
    rr=session.get("https://www.yashima-navi.jp/robots.txt",timeout=(10,15))
    if rr.status_code==404: rules=[]
    elif rr.status_code==200 and len(rr.content)<=500_000: rules=rr.text.splitlines()
    else: raise RuntimeError("robots.txtを確認できません (HTTP "+str(rr.status_code)+")")
    robot=RobotFileParser();robot.parse(rules)
    def page(url:str)->str:
        if not robot.can_fetch(AGENT,url): raise PermissionError("robots.txtの規則により取得を中止: "+url)
        r=session.get(url,timeout=(10,25))
        r.raise_for_status()
        if urlparse(r.url).hostname!=ALLOWED_HOST: raise ValueError("予期しない転送先")
        if len(r.content)>2_000_000: raise ValueError("公式ページ2MB超")
        if "html" not in r.headers.get("Content-Type","").lower():raise ValueError("公式ページがHTMLではありません")
        if not re.search(r"charset=",r.headers.get("Content-Type",""),re.I):
            meta=re.search(rb'<meta[^>]+charset\s*=\s*["\']?([\w-]+)',r.content[:12000],re.I)
            r.encoding=meta[1].decode("ascii") if meta else "utf-8"
        return r.text
    indexes=(INDEX_URL,"https://www.yashima-navi.jp/jp/event/")
    links=[]
    successful=0
    errors=[]
    for idx in indexes:
        try:
            found=find_links(page(idx)); successful+=1
            print(f"やしまーる候補一覧: {idx}: {len(found)}件")
            links.extend(u for u in found if u not in links)
        except Exception as e:
            errors.append(f"一覧 {idx}: {type(e).__name__}: {str(e)[:100]}")
    if not successful: raise RuntimeError("公式一覧取得不能: "+"; ".join(errors))
    # 多数の過去記事を毎回クロールしない。公式の新着順から最大24件。
    total_checked=0; failed=0; rows=[]
    for url in links[:24]:
        try:
            rows.extend(parse_detail(page(url),url,now))
            total_checked+=1
        except Exception as e:
            failed+=1
            if failed<=4: print(f"やしまーる詳細取得警告: {url} {type(e).__name__}: {str(e)[:90]}",file=sys.stderr)
        time.sleep(.25)
    if not rows: raise RuntimeError(f"将来の一般公開催事0件（一覧{len(links)}件、確認{total_checked}件、失敗{failed}件）。構造・対象日を要確認")
    unique={r['id']:r for r in rows}
    return sorted(unique.values(),key=lambda e:(e['date'],e['title'])), failed


def old_events(now:datetime)->list[dict]:
    try:
        r=requests.get(PUBLISHED,timeout=(8,12),params={"v":now.strftime("%Y%m%d%H%M")},headers={"User-Agent":AGENT})
        r.raise_for_status()
        if len(r.content)>1_500_000:return []
        p=r.json(); ts=datetime.fromisoformat(p["updatedAt"])
        if ts.tzinfo is None:ts=ts.replace(tzinfo=JST)
        if not timedelta(0)<=now-ts<timedelta(days=7):return []
        return [e for e in p.get("events",[]) if e.get("venue")==VENUE and isinstance(e.get("date"),str) and e["date"]>=(now.date()-timedelta(days=1)).isoformat()]
    except Exception as e:
        print("やしまーる旧データ確認不可: "+type(e).__name__,file=sys.stderr)
        return []


def merge(payload:dict, rows:list[dict],status:str,now:datetime)->dict:
    if payload.get("schema")!=1 or not isinstance(payload.get("events"),list):raise ValueError("events.json形式不正")
    out=dict(payload)
    out["events"]=sorted([x for x in out["events"] if x.get("venue")!=VENUE]+rows,key=lambda x:(x.get("date",""),x.get("venue",""),x.get("title","")))
    statuses=[s for s in out.get("sources",[]) if isinstance(s,dict) and s.get("venue")!=VENUE]
    statuses.append({"venue":VENUE,"status":status,"count":len(rows),"checkedAt":now.isoformat(timespec="seconds")})
    out["sources"]=statuses
    out["partial"]=any(s.get("status") in ("error","stale","partial") for s in statuses)
    if rows and VENUE not in out.get("sourceName",""):
        out["sourceName"]=out.get("sourceName","複数公式サイト")+"・"+VENUE
    return out


def main()->int:
    now=datetime.now(JST)
    if not OUTPUT.exists():
        print("既存data/events.jsonがありません。取得順を確認してください。",file=sys.stderr);return 1
    payload=json.loads(OUTPUT.read_text(encoding="utf-8"))
    try:
        records,failed=fetch_official(now)
        status="partial" if failed else "ok"
    except Exception as exc:
        print("やしまーる取得失敗: "+type(exc).__name__+": "+str(exc)[:350],file=sys.stderr)
        records=old_events(now)
        status="stale" if records else "error"
    merged=merge(payload,records,status,now)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"やしまーる統合完了: {len(records)}件 / {status} / 全施設合計 {len(merged['events'])}件")
    return 0

if __name__=="__main__":raise SystemExit(main())
