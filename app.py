import feedparser
import sqlite3
import threading
import time
import os
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import re

app = Flask(__name__)
DB_PATH = "news.db"

# ─────────────────────────────────────────────
# FONTI RSS
# ─────────────────────────────────────────────
FEEDS = {
    # MAINSTREAM ITALIANE
    "ANSA Mondo": "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml",
    "Repubblica Esteri": "https://www.repubblica.it/rss/esteri/rss2.0.xml",
    "Corriere Esteri": "https://xml2.corriereobjects.it/rss/esteri.xml",
    "Il Sole 24 Ore Mondo": "https://www.ilsole24ore.com/rss/mondo.xml",
    "Il Fatto Quotidiano Esteri": "https://www.ilfattoquotidiano.it/category/esteri/feed/",
    "Scenari Economici": "https://scenarieconomici.it/feed/",

    # MAINSTREAM INTERNAZIONALI
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters World": "https://feeds.reuters.com/reuters/worldNews",
    "Al Jazeera English": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian World": "https://www.theguardian.com/world/rss",
    "AP News": "https://feeds.apnews.com/rss/APNewsTop25Stories",
    "DW World": "https://rss.dw.com/rdf/rss-en-world",
    "France24 EN": "https://www.france24.com/en/rss",
    "Euronews EN": "https://www.euronews.com/rss",

    # PROSPETTIVA EST
    "TASS English": "https://tass.com/rss/v2.xml",
    "Xinhua EN": "http://www.xinhuanet.com/english/rss/worldrss.xml",
    "RT World": "https://www.rt.com/rss/news/",

    # GEOPOLITICA SPECIALIZZATA
    "ISW": "https://www.understandingwar.org/rss.xml",
    "Foreign Affairs": "https://www.foreignaffairs.com/rss.xml",
    "The Diplomat": "https://thediplomat.com/feed/",
    "Defense One": "https://www.defenseone.com/rss/all/",
    "War on the Rocks": "https://warontherocks.com/feed/",
    "Limes": "https://www.limesonline.com/feed",
    "Geopolitical Futures": "https://geopoliticalfutures.com/feed/",
    "Responsible Statecraft": "https://responsiblestatecraft.org/feed/",

    # ALTERNATIVE / MULTIPOLARE
    "The Cradle": "https://thecradle.co/feed",
    "MintPress News": "https://www.mintpressnews.com/feed/",
    "Multipolarista": "https://multipolarista.com/feed/",
    "Consortium News": "https://consortiumnews.com/feed/",
    "Antiwar.com": "https://www.antiwar.com/blog/feed/",
}

# ─────────────────────────────────────────────
# KEYWORD FILTER
# ─────────────────────────────────────────────
KEYWORDS_IT = [
    "guerra", "conflitto", "militare", "esercito", "nato", "ucraina", "russia",
    "cina", "taiwan", "israele", "palestina", "gaza", "siria", "iran", "medio oriente",
    "geopolitica", "sanzioni", "missili", "bombe", "attacco", "offensiva", "difesa",
    "diplomazia", "accordo", "trattato", "embargo", "cremlino", "zelensky", "putin",
    "brics", "g7", "g20", "balcani", "africa", "sahel", "houthi", "hezbollah",
    "armi", "nucleare", "droni", "esercitazione", "invasione", "truppe", "fronte",
]
KEYWORDS_EN = [
    "war", "conflict", "military", "army", "nato", "ukraine", "russia",
    "china", "taiwan", "israel", "palestine", "gaza", "syria", "iran", "middle east",
    "geopolitics", "sanctions", "missile", "bomb", "attack", "offensive", "defense",
    "diplomacy", "treaty", "embargo", "kremlin", "zelensky", "putin",
    "brics", "g7", "g20", "balkans", "africa", "sahel", "houthi", "hezbollah",
    "weapons", "nuclear", "drone", "exercise", "troops", "forces", "invasion",
    "ceasefire", "peace talks", "coup", "airstrike", "frontline", "casualties",
    "geopolitical", "security council", "pentagon", "nato", "warfare",
]
ALL_KEYWORDS = set(KEYWORDS_IT + KEYWORDS_EN)

CATEGORY_TAGS = {
    "🔴 Russia-Ucraina": ["ucraina", "ukraine", "russia", "zelensky", "putin", "donbass", "kharkiv", "kherson", "zaporizhzhia", "crimea", "mosca", "kiev", "kyiv"],
    "🟠 Medio Oriente": ["israel", "israele", "palestin", "gaza", "hamas", "hezbollah", "iran", "libano", "lebanon", "houthi", "yemen", "siria", "syria", "netanyahu"],
    "🟡 Cina & Indo-Pacifico": ["china", "cina", "taiwan", "asia", "indo-pacific", "south china sea", "japan", "giappone", "corea", "korea", "beijing", "pechino", "xi jinping"],
    "🟢 Africa & Sahel": ["africa", "sahel", "mali", "niger", "sudan", "ethiopia", "somalia", "congo", "burkina", "mozambico", "mozambique"],
    "🔵 NATO & Occidente": ["nato", "g7", "eu", "ue", "europa", "europe", "difesa", "defense", "allean", "pentagon", "washington", "bruxelles", "brussels"],
    "⚪ Altro": [],
}


def categorize(text):
    text_lower = text.lower()
    for cat, keys in CATEGORY_TAGS.items():
        if cat == "⚪ Altro":
            continue
        for k in keys:
            if k in text_lower:
                return cat
    return "⚪ Altro"


def is_relevant(title, summary=""):
    text = (title + " " + summary).lower()
    return any(kw in text for kw in ALL_KEYWORDS)


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            title TEXT,
            link TEXT UNIQUE,
            summary TEXT,
            published TEXT,
            category TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_article(source, title, link, summary, published, category):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR IGNORE INTO articles (source, title, link, summary, published, category, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (source, title, link, summary[:500] if summary else "", published, category,
              datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def fetch_all():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching feeds...")
    count = 0
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))
                published = entry.get("published", datetime.now().isoformat())

                if not link or not title:
                    continue
                if not is_relevant(title, summary):
                    continue

                category = categorize(title + " " + summary)
                save_article(source, title, link, summary, published, category)
                count += 1
        except Exception as e:
            print(f"Error fetching {source}: {e}")
    print(f"[DONE] Saved {count} relevant articles.")


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/news")
def api_news():
    category = request.args.get("category", "all")
    source = request.args.get("source", "all")
    limit = int(request.args.get("limit", 60))
    offset = int(request.args.get("offset", 0))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    query = "SELECT source, title, link, summary, published, category, fetched_at FROM articles WHERE 1=1"
    params = []

    if category != "all":
        query += " AND category = ?"
        params.append(category)
    if source != "all":
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    articles = [
        {"source": r[0], "title": r[1], "link": r[2],
         "summary": r[3], "published": r[4], "category": r[5], "fetched_at": r[6]}
        for r in rows
    ]
    return jsonify(articles)


@app.route("/api/stats")
def api_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles")
    total = c.fetchone()[0]
    c.execute("SELECT category, COUNT(*) FROM articles GROUP BY category ORDER BY COUNT(*) DESC")
    by_cat = {r[0]: r[1] for r in c.fetchall()}
    c.execute("SELECT source, COUNT(*) FROM articles GROUP BY source ORDER BY COUNT(*) DESC")
    by_source = {r[0]: r[1] for r in c.fetchall()}
    c.execute("SELECT MAX(fetched_at) FROM articles")
    last_update = c.fetchone()[0]
    conn.close()
    return jsonify({"total": total, "by_category": by_cat, "by_source": by_source, "last_update": last_update})


@app.route("/api/refresh", methods=["POST"])
def manual_refresh():
    thread = threading.Thread(target=fetch_all)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "refresh started"})


@app.route("/api/categories")
def api_categories():
    return jsonify(list(CATEGORY_TAGS.keys()))


@app.route("/api/sources")
def api_sources():
    return jsonify(list(FEEDS.keys()))


# ─────────────────────────────────────────────
# SCHEDULER + STARTUP
# ─────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_all, "interval", hours=1, id="fetch_feeds")
    scheduler.start()
    return scheduler


# Questo viene eseguito sia con `python app.py` che con gunicorn
init_db()
_startup_thread = threading.Thread(target=fetch_all)
_startup_thread.daemon = True
_startup_thread.start()
_scheduler = start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)