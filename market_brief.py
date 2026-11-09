"""
코인 시황 브리핑 봇
- 하루 3번(06시 / 18시 / 23시 KST) 텔레그램으로 시장 전체 상태를 요약해서 보냅니다.
- 데이터: CoinGecko(시총·도미넌스·BTC 추세), Coinalyze 또는 OKX(펀딩비·OI),
  alternative.me(공포·탐욕), Yahoo Finance(DXY·나스닥·미국 10년물),
  ForexFactory(이번 주 미국 주요 경제 일정), 코인 뉴스 RSS.
- 하나의 데이터가 실패해도 나머지는 그대로 보냅니다.

필요한 환경변수 (GitHub Secrets)
  BRIEF_BOT_TOKEN   새로 만든 시황용 텔레그램 봇 토큰 (필수)
  BRIEF_CHAT_ID     메시지를 받을 채팅 ID (필수)
  COINALYZE_API_KEY 있으면 펀딩비·OI를 Coinalyze에서 가져옴 (선택)
"""

import html
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (market-brief-bot)"}
STATE_FILE = os.environ.get("BRIEF_STATE_FILE", "state/last_state.json")

# 2026년 FOMC 금리 결정 발표일 (미국 동부시간 14:00). 매년 연준 일정 공개 후 갱신하세요.
FOMC_DECISIONS_ET = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]


# ---------------------------------------------------------------- 공통 유틸
def get_json(url, headers=None, params=None, timeout=15):
    h = dict(UA)
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def safe(fn, *args, **kwargs):
    """데이터 하나가 실패해도 전체가 멈추지 않도록 감쌉니다."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {fn.__name__} 실패: {e}", file=sys.stderr)
        return None


def fmt_usd(v):
    if v is None:
        return "-"
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    return f"${v:,.0f}"


def fmt_pct(v, digits=2, sign=True):
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


def arrow(v, eps=0.0):
    if v is None:
        return ""
    if v > eps:
        return "🔺"
    if v < -eps:
        return "🔻"
    return "➖"


def delta(cur, prev):
    if cur is None or prev is None:
        return None
    return cur - prev


# ---------------------------------------------------------------- 데이터 수집
def fetch_global():
    d = get_json("https://api.coingecko.com/api/v3/global")["data"]
    total = d["total_market_cap"]["usd"]
    pct = d["market_cap_percentage"]
    btc_d, eth_d, usdt_d = pct.get("btc"), pct.get("eth"), pct.get("usdt")
    return {
        "total": total,
        "total_chg24": d.get("market_cap_change_percentage_24h_usd"),
        "total2": total * (1 - btc_d / 100),
        "total3": total * (1 - (btc_d + eth_d) / 100),
        "btc_d": btc_d,
        "eth_d": eth_d,
        "usdt_d": usdt_d,
    }


def fetch_prices():
    d = get_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"},
    )
    btc, eth = d["bitcoin"], d["ethereum"]
    return {
        "btc": btc["usd"],
        "btc_chg24": btc.get("usd_24h_change"),
        "eth": eth["usd"],
        "eth_chg24": eth.get("usd_24h_change"),
        "ethbtc": eth["usd"] / btc["usd"],
    }


def fetch_btc_trend():
    """일봉 종가로 50일·200일 이동평균과 7일 등락을 계산합니다."""
    d = get_json(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": "210", "interval": "daily"},
    )
    closes = [p[1] for p in d["prices"]]
    if len(closes) < 200:
        raise ValueError("일봉 데이터 부족")
    last = closes[-1]
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200
    chg7 = (last / closes[-8] - 1) * 100
    return {"ma50": ma50, "ma200": ma200, "chg7": chg7, "last": last}


def fetch_derivs_coinalyze(api_key):
    base = "https://api.coinalyze.net/v1"
    h = {"api_key": api_key}
    syms = "BTCUSDT_PERP.A,ETHUSDT_PERP.A"  # 바이낸스 무기한
    fr = get_json(f"{base}/funding-rate", headers=h, params={"symbols": syms})
    now = int(time.time())
    oi_hist = get_json(
        f"{base}/open-interest-history",
        headers=h,
        params={"symbols": syms, "interval": "1hour", "from": now - 26 * 3600,
                "to": now, "convert_to_usd": "true"},
    )
    out = {}
    for row in fr:
        coin = row["symbol"][:3]
        out.setdefault(coin, {})["funding"] = row["value"] * 100  # %
    for row in oi_hist:
        coin = row["symbol"][:3]
        hist = row.get("history") or []
        if len(hist) >= 2:
            first, last = hist[0]["c"], hist[-1]["c"]
            out.setdefault(coin, {})["oi"] = last
            out[coin]["oi_chg24"] = (last / first - 1) * 100 if first else None
    return out


def fetch_derivs_okx():
    out = {}
    for coin in ("BTC", "ETH"):
        inst = f"{coin}-USDT-SWAP"
        fr = get_json("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
        out[coin] = {"funding": float(fr["data"][0]["fundingRate"]) * 100}
    return out


def fetch_derivs():
    key = os.environ.get("COINALYZE_API_KEY")
    if key:
        r = safe(fetch_derivs_coinalyze, key)
        if r:
            return r
    return safe(fetch_derivs_okx)


def fetch_fng():
    d = get_json("https://api.alternative.me/fng/", params={"limit": "2"})["data"]
    return {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
            "prev": int(d[1]["value"]) if len(d) > 1 else None}


def fetch_yahoo(symbol):
    d = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "5d", "interval": "1d"},
    )
    res = d["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    last = res["meta"].get("regularMarketPrice") or closes[-1]
    prev = closes[-2] if len(closes) >= 2 else None
    chg = (last / prev - 1) * 100 if prev else None
    return {"last": last, "chg": chg}


def fetch_macro():
    return {
        "DXY": safe(fetch_yahoo, "DX-Y.NYB"),
        "나스닥": safe(fetch_yahoo, "^IXIC"),
        "미 10년물": safe(fetch_yahoo, "^TNX"),
    }


def fetch_calendar(now_kst, hours=36):
    """ForexFactory 이번 주 일정 중 미국 '높음' 중요도만, 앞으로 N시간 이내."""
    rows = get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    out = []
    for r in rows:
        if r.get("country") != "USD" or r.get("impact") != "High":
            continue
        try:
            t = datetime.fromisoformat(r["date"]).astimezone(KST)
        except Exception:  # noqa: BLE001
            continue
        if now_kst - timedelta(hours=1) <= t <= now_kst + timedelta(hours=hours):
            out.append({"time": t, "title": r.get("title", ""),
                        "forecast": r.get("forecast") or "", "previous": r.get("previous") or ""})
    out.sort(key=lambda x: x["time"])
    return out


def next_fomc(now_kst):
    for d in FOMC_DECISIONS_ET:
        # 동부시간 14:00 → 서머타임(3월 둘째 일요일~11월 첫째 일요일)은 UTC-4, 아니면 UTC-5
        y, m, day = map(int, d.split("-"))
        dst = 3 < m < 11 or (m == 3 and day >= 8) or (m == 11 and day < 7)
        et = timezone(timedelta(hours=-4 if dst else -5))
        t = datetime(y, m, day, 14, 0, tzinfo=et).astimezone(KST)
        if t > now_kst - timedelta(hours=2):
            return t
    return None


def fetch_news(since_utc, limit=5):
    items = []
    for src, url in NEWS_FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate")
                try:
                    from email.utils import parsedate_to_datetime
                    t = parsedate_to_datetime(pub).astimezone(timezone.utc)
                except Exception:  # noqa: BLE001
                    continue
                if t >= since_utc and title:
                    items.append({"src": src, "title": title, "link": link, "time": t})
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 뉴스 {src} 실패: {e}", file=sys.stderr)
    items.sort(key=lambda x: x["time"], reverse=True)
    seen, out = set(), []
    for it in items:
        k = it["title"].lower()[:60]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out[:limit]


# ---------------------------------------------------------------- 상태 저장 (직전 브리핑과 비교)
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------------------------------------------------------------- 종합 판단 (규칙 기반)
def assess(g, p, trend, deriv, fng, prev, events, fomc_t, now_kst):
    score, notes, warns = 0, [], []

    if trend and p:
        if p["btc"] > trend["ma200"]:
            score += 1
            notes.append("BTC 200일선 위")
        else:
            score -= 1
            notes.append("BTC 200일선 아래")
        if trend["ma50"] > trend["ma200"]:
            score += 1
        else:
            score -= 1

    if g:
        if g.get("total_chg24") is not None:
            score += 1 if g["total_chg24"] > 1 else (-1 if g["total_chg24"] < -1 else 0)
        d_usdt = delta(g.get("usdt_d"), prev.get("usdt_d"))
        if d_usdt is not None and abs(d_usdt) >= 0.05:
            if d_usdt > 0:
                score -= 1
                notes.append("USDT.D 상승(현금 피신)")
            else:
                score += 1
                notes.append("USDT.D 하락(코인으로 유입)")
        d_btcd = delta(g.get("btc_d"), prev.get("btc_d"))
        if d_btcd is not None and d_btcd < -0.2 and (g.get("total_chg24") or 0) > 0:
            notes.append("BTC.D 하락 + 시총 상승 → 알트 강세 조짐")

    if deriv and "BTC" in deriv:
        f = deriv["BTC"].get("funding")
        if f is not None:
            if f >= 0.03:
                warns.append(f"BTC 펀딩비 {f:.3f}% — 롱 과열, 롱 청산 주의")
            elif f <= -0.01:
                warns.append(f"BTC 펀딩비 {f:.3f}% — 숏 쏠림, 숏 스퀴즈 가능")

    if fng:
        if fng["value"] >= 80:
            warns.append(f"공포·탐욕 {fng['value']} (극단적 탐욕)")
        elif fng["value"] <= 20:
            warns.append(f"공포·탐욕 {fng['value']} (극단적 공포)")

    for e in events:
        hrs = (e["time"] - now_kst).total_seconds() / 3600
        if -1 <= hrs <= 12:
            when = f"{e['time']:%H:%M}" if e["time"].date() == now_kst.date() else f"{e['time']:%m/%d %H:%M}"
            warns.append(f"{when} {e['title']} 발표 — 전후 1~2시간 변동성 주의")
    if fomc_t:
        hrs = (fomc_t - now_kst).total_seconds() / 3600
        if -2 <= hrs <= 48:
            warns.append(f"FOMC 결정 {fomc_t:%m/%d %H:%M} — 신규 진입·레버리지 신중")

    if score >= 3:
        mood = "🟢 우호적"
    elif score >= 1:
        mood = "🟡 약한 우호"
    elif score > -1:
        mood = "⚪ 중립"
    elif score > -3:
        mood = "🟠 약한 경계"
    else:
        mood = "🔴 경계"
    return mood, score, notes, warns


# ---------------------------------------------------------------- 메시지 조립
SLOT_TITLE = {6: "🌅 아침 시황 (미국장 마감 후)", 18: "🌆 저녁 시황 (유럽장 개장)", 23: "🌙 밤 시황 (미국장 개장 전후)"}


def slot_title(now_kst):
    h = now_kst.hour
    best = min(SLOT_TITLE, key=lambda s: min(abs(h - s), 24 - abs(h - s)))
    return SLOT_TITLE[best]


def build_message(now_kst, g, p, trend, deriv, fng, macro, events, fomc_t, news, prev):
    e = html.escape
    L = [f"<b>{slot_title(now_kst)}</b>", f"<i>{now_kst:%Y-%m-%d %H:%M} KST</i>", ""]

    mood, score, notes, warns = assess(g, p, trend, deriv, fng, prev, events, fomc_t, now_kst)
    L.append(f"<b>종합: {mood}</b> (점수 {score:+d})")
    if notes:
        L.append("· " + " / ".join(notes))
    if warns:
        for w in warns:
            L.append(f"⚠️ {e(w)}")
    L.append("")

    # 비트코인
    L.append("<b>₿ 비트코인</b>")
    if p:
        L.append(f"가격 ${p['btc']:,.0f} ({fmt_pct(p['btc_chg24'])} 24h)")
    if trend:
        pos200 = (p["btc"] / trend["ma200"] - 1) * 100 if p else None
        L.append(f"7일 {fmt_pct(trend['chg7'])} · 50일선 ${trend['ma50']:,.0f} · 200일선 ${trend['ma200']:,.0f}")
        if pos200 is not None:
            L.append(f"200일선 대비 {fmt_pct(pos200)}  {'골든(50>200)' if trend['ma50'] > trend['ma200'] else '데드(50<200)'}")
    if not p and not trend:
        L.append("데이터 없음")
    L.append("")

    # 시총·도미넌스
    L.append("<b>📊 시총 · 도미넌스</b>")
    if g:
        L.append(f"TOTAL {fmt_usd(g['total'])} ({fmt_pct(g['total_chg24'])} 24h)")
        L.append(f"TOTAL2 {fmt_usd(g['total2'])} · TOTAL3 {fmt_usd(g['total3'])}")
        for key, name in (("usdt_d", "USDT.D"), ("btc_d", "BTC.D")):
            d = delta(g.get(key), prev.get(key))
            dtxt = f" {arrow(d, 0.01)}{d:+.2f}p (직전 대비)" if d is not None else ""
            L.append(f"{name} {g[key]:.2f}%{dtxt}")
        if p:
            d = None
            if prev.get("ethbtc"):
                d = (p["ethbtc"] / prev["ethbtc"] - 1) * 100
            dtxt = f" {arrow(d, 0.1)}{d:+.2f}% (직전 대비)" if d is not None else ""
            L.append(f"ETH/BTC {p['ethbtc']:.5f}{dtxt}")
    else:
        L.append("데이터 없음")
    L.append("")

    # 파생
    L.append("<b>⚡ 선물 심리</b>")
    if deriv:
        for coin in ("BTC", "ETH"):
            d = deriv.get(coin) or {}
            parts = []
            if d.get("funding") is not None:
                parts.append(f"펀딩 {d['funding']:.4f}%")
            if d.get("oi") is not None:
                parts.append(f"OI {fmt_usd(d['oi'])} ({fmt_pct(d.get('oi_chg24'))} 24h)")
            if parts:
                L.append(f"{coin}: " + " · ".join(parts))
    if fng:
        chg = f" (어제 {fng['prev']})" if fng.get("prev") is not None else ""
        L.append(f"공포·탐욕 {fng['value']} {fng['label']}{chg}")
    if not deriv and not fng:
        L.append("데이터 없음")
    L.append("")

    # 거시
    L.append("<b>🌐 거시</b>")
    any_macro = False
    for name, v in (macro or {}).items():
        if v:
            any_macro = True
            val = f"{v['last']:.2f}%" if name == "미 10년물" else f"{v['last']:,.2f}"
            L.append(f"{name} {val} ({fmt_pct(v['chg'])})")
    if not any_macro:
        L.append("데이터 없음")
    L.append("")

    # 일정
    L.append("<b>🗓 주요 일정 (36시간 이내, 미국 고중요도)</b>")
    if events:
        for ev in events:
            extra = []
            if ev["forecast"]:
                extra.append(f"예상 {ev['forecast']}")
            if ev["previous"]:
                extra.append(f"이전 {ev['previous']}")
            tail = f" ({', '.join(extra)})" if extra else ""
            wd = "월화수목금토일"[ev["time"].weekday()]
            L.append(f"· {ev['time']:%m/%d}({wd}) {ev['time']:%H:%M} {e(ev['title'])}{e(tail)}")
    elif events is None:
        L.append("일정 데이터를 가져오지 못했어요")
    else:
        L.append("· 큰 일정 없음")
    if fomc_t:
        days = (fomc_t.date() - now_kst.date()).days
        L.append(f"· 다음 FOMC 결정: {fomc_t:%m/%d %H:%M} (D-{days})" if days > 0
                 else f"· FOMC 결정: 오늘 {fomc_t:%H:%M}")
    L.append("")

    # 뉴스
    L.append("<b>📰 최근 헤드라인</b>")
    if news:
        for n in news:
            L.append(f"· <a href=\"{e(n['link'])}\">{e(n['title'])}</a> <i>({n['src']})</i>")
    else:
        L.append("· 새 헤드라인 없음")
    L.append("")
    L.append("<i>규칙 기반 자동 요약이며 투자 조언이 아닙니다.</i>")
    return "\n".join(L)


# ---------------------------------------------------------------- 텔레그램
def send_telegram(text):
    token = os.environ["BRIEF_BOT_TOKEN"]
    chat = os.environ["BRIEF_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):  # 4096자 제한 대비
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        r = requests.post(url, data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                     "disable_web_page_preview": "true"}, timeout=20)
        if not r.ok:
            raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")


# ---------------------------------------------------------------- 실행
def main():
    now_kst = datetime.now(KST)
    prev = load_state()

    g = safe(fetch_global)
    p = safe(fetch_prices)
    trend = safe(fetch_btc_trend)
    deriv = fetch_derivs()
    fng = safe(fetch_fng)
    macro = safe(fetch_macro)
    events = safe(fetch_calendar, now_kst)
    fomc_t = next_fomc(now_kst)

    since = prev.get("ts")
    since_utc = (datetime.fromisoformat(since).astimezone(timezone.utc) if since
                 else datetime.now(timezone.utc) - timedelta(hours=8))
    news = safe(fetch_news, since_utc) or []

    msg = build_message(now_kst, g, p, trend, deriv, fng, macro, events, fomc_t, news, prev)
    print(msg)

    if os.environ.get("DRY_RUN") != "1":
        send_telegram(msg)

    new_state = {"ts": now_kst.isoformat()}
    if g:
        new_state.update({k: g[k] for k in ("usdt_d", "btc_d", "total")})
    else:
        new_state.update({k: prev.get(k) for k in ("usdt_d", "btc_d", "total")})
    new_state["ethbtc"] = p["ethbtc"] if p else prev.get("ethbtc")
    save_state(new_state)


if __name__ == "__main__":
    main()
