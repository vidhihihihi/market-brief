"""Daily Market Brief (free Gemini version, wide news coverage).

Numbers come from yfinance. News comes from free RSS feeds grouped into
categories. Gemini (free tier) picks the stories that matter and explains how
they could affect markets. If Gemini fails, a no-AI fallback keeps the page alive.
"""
import calendar
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf

# ---- CONFIG: edit these --------------------------------------------------
CURATOR = "Your Name"
TZ = ZoneInfo("Asia/Kolkata")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
MAX_AGE_HOURS = 72      # ignore stories older than this
FRESH_HOURS = 36        # tagged FRESH if newer than this, else CONTEXT

SECTIONS = {
    "India": {"Nifty 50": "^NSEI", "Sensex": "^BSESN", "USD/INR": "INR=X"},
    "United States": {"S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Dow": "^DJI", "US 10Y yield": "^TNX"},
    "Europe": {"FTSE 100": "^FTSE", "DAX": "^GDAXI", "CAC 40": "^FCHI"},
    "Asia": {"Nikkei 225": "^N225", "Hang Seng": "^HSI"},
    "Commodities & FX": {"Gold": "GC=F", "Crude oil (WTI)": "CL=F", "Dollar index": "DX-Y.NYB"},
    "Crypto": {"Bitcoin": "BTC-USD", "Ethereum": "ETH-USD"},
}


def gnews(query):
    """Google News RSS search for a topic (India edition)."""
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"


# category -> (how many stories to show, feeds)
NEWS = {
    "Geopolitics": (5, [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.thehindu.com/news/international/feeder/default.rss",
        gnews("war OR ceasefire OR sanctions OR summit OR tariffs OR \"trade deal\""),
    ]),
    "Central banks & policy": (4, [
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.rbi.org.in/pressreleases_rss.xml",
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
        gnews("Federal Reserve OR RBI OR ECB OR \"interest rate\" OR inflation"),
    ]),
    "Energy & commodities": (4, [
        "https://oilprice.com/rss/main",
        gnews("oil prices OR OPEC OR crude OR gold price OR LNG"),
    ]),
    "India markets & economy": (5, [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/business.xml",
        "https://www.livemint.com/rss/markets",
        "https://www.thehindu.com/business/feeder/default.rss",
    ]),
    "Global markets & companies": (5, [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        gnews("earnings OR IPO OR merger OR \"stock market\""),
    ]),
    "Tech & crypto": (3, [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        gnews("AI chips OR semiconductor OR crypto regulation OR Bitcoin ETF"),
    ]),
}
# --------------------------------------------------------------------------


def fetch_quote(symbol):
    try:
        hist = yf.Ticker(symbol).history(period="7d", interval="1d").dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        last, prev = hist.iloc[-1], hist.iloc[-2]
        ts = hist.index[-1].to_pydatetime()
        age_h = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600
        return {
            "price": float(last["Close"]),
            "change_pct": (float(last["Close"]) / float(prev["Close"]) - 1) * 100,
            "bar_date": ts.strftime("%a %d %b"),
            "status": "STALE" if age_h > 96 else "LAST CLOSE",
        }
    except Exception as exc:
        print(f"quote failed for {symbol}: {exc}")
        return None


def fetch_all_news(per_feed=6):
    """Return {category: [items]}; dedupes titles and drops very old stories."""
    now = datetime.now(timezone.utc)
    seen, out, n = set(), {}, 0
    for cat, (_, feeds) in NEWS.items():
        out[cat] = []
        for url in feeds:
            try:
                feed = feedparser.parse(url)
                source = feed.feed.get("title", "") or url
                for e in feed.entries[:per_feed]:
                    title = e.get("title", "").strip()
                    key = re.sub(r"\W+", "", title.lower())[:60]
                    if not title or key in seen:
                        continue
                    age_h = None
                    if e.get("published_parsed"):
                        pub = datetime.fromtimestamp(calendar.timegm(e.published_parsed), timezone.utc)
                        age_h = (now - pub).total_seconds() / 3600
                        if age_h > MAX_AGE_HOURS:
                            continue
                    seen.add(key)
                    summary = re.sub(r"<[^>]+>", "", html.unescape(e.get("summary", ""))).strip()
                    out[cat].append({
                        "id": f"N{n}", "source": source[:40], "title": title,
                        "summary": summary[:220], "link": e.get("link", ""),
                        "age_h": None if age_h is None else round(age_h, 1),
                    })
                    n += 1
            except Exception as exc:
                print(f"feed failed {url}: {exc}")
        print(f"{cat}: {len(out[cat])} stories")
    return out


def ask_gemini(data, news):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    slim = {c: [{k: i[k] for k in ("id", "source", "title", "summary", "age_h")} for i in items]
            for c, items in news.items()}
    picks = {c: NEWS[c][0] for c in NEWS}
    prompt = f"""You are writing a daily market brief for an Indian retail investor.
Use ONLY the data and headlines below. Never invent prices, percentages, dates or events.
If a link between news and markets is speculative, say "may" or "could".
Prefer fresh stories (smaller age_h). Skip stories with no plausible market relevance.

MARKET DATA:
{json.dumps(data, indent=1)}

NEWS BY CATEGORY (age_h = hours since published):
{json.dumps(slim, indent=1)}

Return JSON in exactly this shape:
{{
  "tagline": "one sentence capturing the day",
  "commentary": "4-6 sentences explaining what moved in markets and the likely reasons, using only figures given",
  "categories": {{
    "<category name exactly as given>": [
      {{"id": "<story id>", "title": "short headline",
        "explanation": "1-2 sentences on what happened",
        "market_impact": "1-2 sentences on how it could affect markets (oil, gold, rupee, equities, rates, crypto)"}}
    ]
  }},
  "watchlist": ["up to 5 short items to watch today, only if supported by the headlines"]
}}
Maximum stories per category: {json.dumps(picks)}. Use fewer if few are relevant."""
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.3, "maxOutputTokens": 12000},
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text.strip().replace("```json", "").replace("```", ""))


def fallback(data, news):
    movers = sorted([(n, q["change_pct"]) for rows in data.values() for n, q in rows.items() if q],
                    key=lambda x: x[1])
    if movers:
        worst, best = movers[0], movers[-1]
        tagline = f"{best[0]} leads, {worst[0]} lags."
        commentary = (f"Biggest gainer: {best[0]} ({best[1]:+.2f}%). "
                      f"Biggest decliner: {worst[0]} ({worst[1]:+.2f}%). (AI commentary unavailable today.)")
    else:
        tagline, commentary = "Market data unavailable right now.", ""
    cats = {c: [{"id": i["id"], "title": i["title"], "explanation": i["summary"], "market_impact": ""}
                for i in items[:NEWS[c][0]]] for c, items in news.items()}
    return {"tagline": tagline, "commentary": commentary, "categories": cats, "watchlist": []}


def esc(s):
    return html.escape(str(s))


def render(data, ai, news, now):
    lookup = {i["id"]: i for items in news.values() for i in items}

    cards = ""
    for section, rows in data.items():
        cards += f"<h2>{esc(section)}</h2><div class='grid'>"
        for name, q in rows.items():
            if q is None:
                cards += f"<div class='card'><div class='n'>{esc(name)}</div><div class='p'>UNAVAILABLE</div></div>"
                continue
            cls = "up" if q["change_pct"] >= 0 else "down"
            cards += (f"<div class='card'><div class='n'>{esc(name)}</div>"
                      f"<div class='p'>{q['price']:,.2f}</div>"
                      f"<div class='c {cls}'>{q['change_pct']:+.2f}%</div>"
                      f"<div class='t'>{esc(q['status'])} · {esc(q['bar_date'])} · Yahoo Finance</div></div>")
        cards += "</div>"

    stories = ""
    for cat in NEWS:
        items = ai.get("categories", {}).get(cat, [])
        if not items:
            continue
        stories += f"<h2>{esc(cat)}</h2>"
        for s in items:
            src = lookup.get(s.get("id"))
            tag, link = "", ""
            if src:
                age = src.get("age_h")
                if age is not None:
                    fresh = age <= FRESH_HOURS
                    tag = f"<span class='tag2 {'fresh' if fresh else 'ctx'}'>{'FRESH' if fresh else 'CONTEXT'} · {int(age)}h ago</span>"
                link = f"<a href='{esc(src['link'])}'>{esc(src['source'])} ↗</a>"
            impact = (f"<p class='impact'><b>Market angle:</b> {esc(s['market_impact'])}</p>"
                      if s.get("market_impact") else "")
            stories += (f"<div class='story'>{tag}<h3>{esc(s.get('title',''))}</h3>"
                        f"<p>{esc(s.get('explanation',''))}</p>{impact}{link}</div>")

    watch = ""
    if ai.get("watchlist"):
        watch = "<h2>What to watch today</h2><ul>" + "".join(f"<li>{esc(w)}</li>" for w in ai["watchlist"]) + "</ul>"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Market Brief</title><style>
body{{background:#07080c;color:#cfd3dc;font-family:system-ui,sans-serif;margin:0}}
.wrap{{max-width:860px;margin:24px auto;padding:28px;background:#0e1016;border-radius:16px}}
.by{{font-size:13px;color:#8b90a0}} .by b{{color:#a78bfa}}
.kicker{{font-size:12px;letter-spacing:.12em;color:#8b90a0;font-weight:700;margin-top:6px}}
h1{{color:#fff;margin:6px 0}} .tag{{font-style:italic;color:#9aa0b0}}
hr{{border:0;height:2px;background:linear-gradient(90deg,#3b82f6,#8b5cf6,transparent);margin:20px 0}}
h2{{font-size:13px;letter-spacing:.1em;color:#8b90a0;text-transform:uppercase;margin:28px 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}}
.card{{background:#12151d;border:1px solid #232734;border-radius:10px;padding:12px}}
.n{{font-size:12px;color:#8b90a0}} .p{{font-size:20px;color:#fff;font-weight:600}}
.up{{color:#34d399}} .down{{color:#f87171}} .t{{font-size:10px;color:#6b7080;margin-top:6px}}
.story{{background:#12151d;border-left:3px solid #3b82f6;border-radius:8px;padding:12px 16px;margin:10px 0}}
.story h3{{margin:6px 0;color:#fff;font-size:16px}} .story p{{margin:0 0 6px;font-size:14px}}
.impact{{color:#fbbf24}} a{{color:#60a5fa;font-size:12px}}
.tag2{{font-size:10px;font-weight:700;letter-spacing:.08em;padding:2px 8px;border-radius:99px}}
.fresh{{background:#06281c;color:#34d399}} .ctx{{background:#2a2410;color:#fbbf24}}
li{{margin:6px 0;font-size:14px}} .note{{font-size:12px;color:#6b7080;margin-top:30px}}
</style></head><body><div class="wrap">
<div class="by">Curated by <b>{esc(CURATOR)}</b></div>
<div class="kicker">DAILY MARKET BRIEF</div>
<h1>{now.strftime('%A, %B %d, %Y')}</h1>
<div class="tag">{esc(ai.get('tagline',''))}</div><hr>
<p>{esc(ai.get('commentary',''))}</p>
{watch}
{cards}
{stories}
<p class="note">Generated {now.strftime('%d %b %Y, %H:%M')} IST. Prices from Yahoo Finance may be delayed.
FRESH = published within {FRESH_HOURS}h; CONTEXT = older. Explanations are AI-written from the data and
headlines above and can be wrong. Not investment advice.</p>
</div></body></html>"""


def main():
    now = datetime.now(TZ)
    data = {s: {n: fetch_quote(t) for n, t in rows.items()} for s, rows in SECTIONS.items()}
    news = fetch_all_news()
    try:
        ai = ask_gemini(data, news)
    except Exception as exc:
        print(f"Gemini failed, using fallback: {exc}")
        ai = fallback(data, news)
    out = Path("docs")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(render(data, ai, news, now), encoding="utf-8")
    print("Wrote docs/index.html")


if __name__ == "__main__":
    main()
