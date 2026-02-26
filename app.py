import feedparser
import threading
import os
import re
import json
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
from psycopg2.extras import RealDictCursor
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "theatrum-belli-secret-2026")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "theatrum2026")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────
# DATABASE POSTGRES
# ─────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            source TEXT,
            title TEXT,
            link TEXT UNIQUE,
            summary TEXT,
            published TEXT,
            category TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id SERIAL PRIMARY KEY,
            keywords TEXT,
            article_count INTEGER,
            geopolitical TEXT,
            ethical TEXT,
            legal TEXT,
            narrative TEXT,
            instagram_script TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_article(source, title, link, summary, published, category):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO articles (source, title, link, summary, published, category, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (link) DO NOTHING
        """, (source, title, link[:500] if link else "",
              summary[:500] if summary else "",
              published, category,
              datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception as e:
        print(f"DB error: {e}")
        conn.rollback()
    finally:
        conn.close()


def save_analysis(keywords, article_count, geopolitical, ethical, legal, narrative, instagram_script):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO analyses (keywords, article_count, geopolitical, ethical, legal, narrative, instagram_script, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (keywords, article_count, geopolitical, ethical, legal, narrative, instagram_script,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# FONTI RSS
# ─────────────────────────────────────────────
FEEDS = {
    "ANSA Mondo": "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml",
    "Repubblica Esteri": "https://www.repubblica.it/rss/esteri/rss2.0.xml",
    "Corriere Esteri": "https://xml2.corriereobjects.it/rss/esteri.xml",
    "Il Sole 24 Ore Mondo": "https://www.ilsole24ore.com/rss/mondo.xml",
    "Il Fatto Quotidiano Esteri": "https://www.ilfattoquotidiano.it/category/esteri/feed/",
    "Scenari Economici": "https://scenarieconomici.it/feed/",
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters World": "https://feeds.reuters.com/reuters/worldNews",
    "Al Jazeera English": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian World": "https://www.theguardian.com/world/rss",
    "AP News": "https://feeds.apnews.com/rss/APNewsTop25Stories",
    "DW World": "https://rss.dw.com/rdf/rss-en-world",
    "France24 EN": "https://www.france24.com/en/rss",
    "Euronews EN": "https://www.euronews.com/rss",
    "TASS English": "https://tass.com/rss/v2.xml",
    "Xinhua EN": "http://www.xinhuanet.com/english/rss/worldrss.xml",
    "RT World": "https://www.rt.com/rss/news/",
    "ISW": "https://www.understandingwar.org/rss.xml",
    "Foreign Affairs": "https://www.foreignaffairs.com/rss.xml",
    "The Diplomat": "https://thediplomat.com/feed/",
    "Defense One": "https://www.defenseone.com/rss/all/",
    "War on the Rocks": "https://warontherocks.com/feed/",
    "Limes": "https://www.limesonline.com/feed",
    "Geopolitical Futures": "https://geopoliticalfutures.com/feed/",
    "Responsible Statecraft": "https://responsiblestatecraft.org/feed/",
    "The Cradle": "https://thecradle.co/feed",
    "MintPress News": "https://www.mintpressnews.com/feed/",
    "Multipolarista": "https://multipolarista.com/feed/",
    "Consortium News": "https://consortiumnews.com/feed/",
    "Antiwar.com": "https://www.antiwar.com/blog/feed/",
}

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
    "geopolitical", "security council", "pentagon", "warfare",
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
# FETCH RSS
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
# CLAUDE API (SDK ufficiale)
# ─────────────────────────────────────────────
def call_claude(prompt):
    if not ANTHROPIC_API_KEY:
        return "API key non configurata."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        return f"Errore API Claude: {e}"


def generate_analysis(keywords_list, articles, previous_analyses=None):
    articles_text = ""
    for i, a in enumerate(articles[:20], 1):
        articles_text += f"\n[{i}] FONTE: {a['source']}\nTITOLO: {a['title']}\nRIEPILOGO: {a['summary'][:200]}\n"

    keywords_str = ", ".join(keywords_list)

    history_context = ""
    if previous_analyses:
        history_context = "\n\nCONTESTO STORICO (analisi precedenti sugli stessi temi):\n"
        for pa in previous_analyses[:3]:
            history_context += f"\n--- {pa['created_at'][:10]} ---\n{pa['geopolitical'][:300]}...\n"

    prompt = f"""Sei un analista geopolitico senior con expertise in diritto internazionale ed etica delle relazioni internazionali.

Hai raccolto {len(articles)} articoli da fonti diverse (mainstream, alternative, occidentali, orientali) sui temi: {keywords_str}
{history_context}

Articoli recenti:
{articles_text}

Produci un'analisi in 5 sezioni:

## 1. ANALISI GEOPOLITICA
Cosa sta succedendo realmente. Attori principali, interessi reali, dinamiche di potere. Prospettive multiple (occidentale, russa, cinese, Sud Globale). Max 300 parole.

## 2. DIMENSIONE ETICA E MORALE
Vittime, valori violati o difesi, responsabilità morali. Max 200 parole.

## 3. PROSPETTIVA DEL DIRITTO INTERNAZIONALE
Carta ONU, Convenzioni di Ginevra, diritto umanitario, eventuali violazioni. Max 200 parole.

## 4. FILO NARRATIVO
Collega gli eventi attuali a quelli passati. Come si è evoluta la situazione? Se è la prima analisi, stabilisci i punti di riferimento per il futuro. Max 150 parole.

## 5. SCRIPT INSTAGRAM (60 secondi, bilingue IT/EN)
Script per avatar AI su Instagram Reels. Tono autorevole ma accessibile. Struttura: hook 5 sec → contesto 15 sec → analisi 30 sec → conclusione 10 sec. Prima italiano, poi inglese. Max 150 parole per lingua.

Rispondi SOLO con le 5 sezioni."""

    return call_claude(prompt)


# ─────────────────────────────────────────────
# ROUTES PUBBLICHE
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
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    query = "SELECT source, title, link, summary, published, category, fetched_at FROM articles WHERE 1=1"
    params = []
    if category != "all":
        query += " AND category = %s"
        params.append(category)
    if source != "all":
        query += " AND source = %s"
        params.append(source)
    query += " ORDER BY fetched_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
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
# ROUTES ADMIN
# ─────────────────────────────────────────────
@app.route("/admin")
def admin():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return render_template("analisi.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        error = "Password errata."
    return render_template("login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/api/admin/analyze", methods=["POST"])
def api_analyze():
    if not session.get("admin"):
        return jsonify({"error": "Non autorizzato"}), 403
    data = request.json
    keywords = [k.strip().lower() for k in data.get("keywords", []) if k.strip()]
    if not keywords:
        return jsonify({"error": "Inserisci almeno una keyword"}), 400

    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    conditions = " OR ".join(["(LOWER(title) LIKE %s OR LOWER(summary) LIKE %s)" for _ in keywords])
    params = []
    for kw in keywords:
        params.extend([f"%{kw}%", f"%{kw}%"])
    c.execute(f"SELECT source, title, link, summary, published, category FROM articles WHERE {conditions} ORDER BY fetched_at DESC LIMIT 30", params)
    articles = [dict(r) for r in c.fetchall()]

    kw_conditions = " OR ".join(["LOWER(keywords) LIKE %s" for _ in keywords])
    kw_params = [f"%{kw}%" for kw in keywords]
    c.execute(f"SELECT geopolitical, created_at FROM analyses WHERE {kw_conditions} ORDER BY created_at DESC LIMIT 3", kw_params)
    previous = [dict(r) for r in c.fetchall()]
    conn.close()

    if not articles:
        return jsonify({"error": f"Nessun articolo trovato per: {', '.join(keywords)}"}), 404

    raw = generate_analysis(keywords, articles, previous)

    def extract_section(text, title):
        pattern = rf"## {re.escape(title)}\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else ""

    geopolitical = extract_section(raw, "1. ANALISI GEOPOLITICA")
    ethical = extract_section(raw, "2. DIMENSIONE ETICA E MORALE")
    legal = extract_section(raw, "3. PROSPETTIVA DEL DIRITTO INTERNAZIONALE")
    narrative = extract_section(raw, "4. FILO NARRATIVO")
    instagram = extract_section(raw, "5. SCRIPT INSTAGRAM (60 secondi, bilingue IT/EN)")

    save_analysis(", ".join(keywords), len(articles), geopolitical, ethical, legal, narrative, instagram)

    return jsonify({
        "keywords": keywords,
        "article_count": len(articles),
        "articles": articles[:10],
        "geopolitical": geopolitical,
        "ethical": ethical,
        "legal": legal,
        "narrative": narrative,
        "instagram_script": instagram,
        "has_history": len(previous) > 0
    })


@app.route("/api/admin/analyses")
def api_analyses_history():
    if not session.get("admin"):
        return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT id, keywords, article_count, created_at FROM analyses ORDER BY created_at DESC LIMIT 20")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/analyses/<int:analysis_id>")
def api_analysis_detail(analysis_id):
    if not session.get("admin"):
        return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM analyses WHERE id = %s", (analysis_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Non trovata"}), 404
    return jsonify(dict(row))


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
init_db()
_startup_thread = threading.Thread(target=fetch_all)
_startup_thread.daemon = True
_startup_thread.start()

_scheduler = BackgroundScheduler()
_scheduler.add_job(fetch_all, "interval", hours=1, id="fetch_feeds")
_scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
