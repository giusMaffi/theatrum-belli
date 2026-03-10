import feedparser
import threading
import os
import re
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
from psycopg2.extras import RealDictCursor
import anthropic
import requests as req_lib
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "theatrum-belli-secret-2026")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7 giorni
from datetime import timedelta

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "theatrum2026")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

jobs = {}

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            source TEXT, title TEXT, link TEXT UNIQUE,
            summary TEXT, published TEXT, category TEXT,
            perspective TEXT, fetched_at TEXT
        )
    """)
    c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS perspective TEXT DEFAULT 'other'")
    c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id SERIAL PRIMARY KEY,
            keywords TEXT, article_count INTEGER,
            narrative_map TEXT, convergences TEXT, divergences TEXT,
            legal TEXT, thread TEXT, instagram_script TEXT, created_at TEXT
        )
    """)
    for col in ["narrative_map", "convergences", "divergences", "thread", "instagram_script", "legal"]:
        c.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {col} TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS theme_tag TEXT")

    source_map = {
        "ANSA Mondo": "italian_mainstream", "Repubblica Esteri": "italian_mainstream",
        "Corriere Esteri": "italian_mainstream", "Il Sole 24 Ore Mondo": "italian_mainstream",
        "Il Fatto Quotidiano Esteri": "italian_mainstream", "Limes": "think_tank",
        "BBC World": "western_mainstream", "Reuters World": "western_mainstream",
        "The Guardian World": "western_mainstream", "AP News": "western_mainstream",
        "DW World": "western_mainstream", "France24 EN": "western_mainstream",
        "Euronews EN": "western_mainstream", "Jerusalem Post": "pro_israel",
        "Times of Israel": "pro_israel", "Haaretz EN": "pro_israel", "i24 News": "pro_israel",
        "Al Jazeera English": "arab_media", "Middle East Eye": "arab_media",
        "The Cradle": "alternative_left", "MintPress News": "alternative_left",
        "Multipolarista": "alternative_left", "Consortium News": "alternative_left",
        "Antiwar.com": "alternative_left", "Responsible Statecraft": "alternative_left",
        "Scenari Economici": "alternative_left", "TASS English": "russian_state",
        "RT World": "russian_state", "Sputnik World": "russian_state",
        "Global Times EN": "chinese_state", "CGTN World": "chinese_state",
        "SCMP World": "chinese_state", "ISW": "think_tank", "Foreign Affairs": "think_tank",
        "The Diplomat": "think_tank", "Defense One": "think_tank",
        "War on the Rocks": "think_tank", "Geopolitical Futures": "think_tank",
    }
    for source, persp in source_map.items():
        c.execute(
            "UPDATE articles SET perspective = %s WHERE source = %s AND (perspective IS NULL OR perspective = 'other')",
            (persp, source)
        )
    conn.commit()
    conn.close()

def save_article(source, title, link, summary, published, category, perspective):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO articles (source, title, link, summary, published, category, perspective, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (link) DO NOTHING
        """, (source, title, link[:500] if link else "", summary[:500] if summary else "",
              published, category, perspective, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception as e:
        print(f"DB error: {e}")
        conn.rollback()
    finally:
        conn.close()

def save_analysis(keywords, article_count, narrative_map, convergences, divergences,
                  legal, thread, instagram_script, theme_tag=""):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO analyses
        (keywords, article_count, narrative_map, convergences, divergences,
         legal, thread, instagram_script, created_at, theme_tag)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (keywords, article_count, narrative_map, convergences, divergences,
          legal, thread, instagram_script, datetime.now(timezone.utc).isoformat(), theme_tag))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# FONTI RSS
# ─────────────────────────────────────────────
FEEDS = {
    "ANSA Mondo": ("https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml", "italian_mainstream"),
    "Repubblica Esteri": ("https://www.repubblica.it/rss/esteri/rss2.0.xml", "italian_mainstream"),
    "Corriere Esteri": ("https://xml2.corriereobjects.it/rss/esteri.xml", "italian_mainstream"),
    "Il Sole 24 Ore Mondo": ("https://www.ilsole24ore.com/rss/mondo.xml", "italian_mainstream"),
    "Il Fatto Quotidiano": ("https://www.ilfattoquotidiano.it/category/esteri/feed/", "italian_mainstream"),
    "Limes": ("https://www.limesonline.com/feed", "think_tank"),
    "BBC World": ("http://feeds.bbci.co.uk/news/world/rss.xml", "western_mainstream"),
    "Reuters World": ("https://feeds.reuters.com/reuters/worldNews", "western_mainstream"),
    "The Guardian World": ("https://www.theguardian.com/world/rss", "western_mainstream"),
    "AP News": ("https://feeds.apnews.com/rss/APNewsTop25Stories", "western_mainstream"),
    "DW World": ("https://rss.dw.com/rdf/rss-en-world", "western_mainstream"),
    "France24 EN": ("https://www.france24.com/en/rss", "western_mainstream"),
    "Euronews EN": ("https://www.euronews.com/rss", "western_mainstream"),
    "Jerusalem Post": ("https://www.jpost.com/rss/rssfeedsfrontpage.aspx", "pro_israel"),
    "Times of Israel": ("https://www.timesofisrael.com/feed/", "pro_israel"),
    "Haaretz EN": ("https://www.haaretz.com/cmlink/1.628765", "pro_israel"),
    "i24 News": ("https://www.i24news.tv/en/rss", "pro_israel"),
    "Al Jazeera English": ("https://www.aljazeera.com/xml/rss/all.xml", "arab_media"),
    "Middle East Eye": ("https://www.middleeasteye.net/rss", "arab_media"),
    "The Cradle": ("https://thecradle.co/feed", "alternative_left"),
    "MintPress News": ("https://www.mintpressnews.com/feed/", "alternative_left"),
    "Multipolarista": ("https://multipolarista.com/feed/", "alternative_left"),
    "Consortium News": ("https://consortiumnews.com/feed/", "alternative_left"),
    "Antiwar.com": ("https://www.antiwar.com/blog/feed/", "alternative_left"),
    "Responsible Statecraft": ("https://responsiblestatecraft.org/feed/", "alternative_left"),
    "Scenari Economici": ("https://scenarieconomici.it/feed/", "alternative_left"),
    "TASS English": ("https://tass.com/rss/v2.xml", "russian_state"),
    "RT World": ("https://www.rt.com/rss/news/", "russian_state"),
    "Xinhua EN": ("http://www.xinhuanet.com/english/rss/worldrss.xml", "chinese_state"),
    "SCMP World": ("https://www.scmp.com/rss/91/feed", "chinese_state"),
    "ISW": ("https://www.understandingwar.org/rss.xml", "think_tank"),
    "Foreign Affairs": ("https://www.foreignaffairs.com/rss.xml", "think_tank"),
    "The Diplomat": ("https://thediplomat.com/feed/", "think_tank"),
    "Defense One": ("https://www.defenseone.com/rss/all/", "think_tank"),
    "War on the Rocks": ("https://warontherocks.com/feed/", "think_tank"),
    "Geopolitical Futures": ("https://geopoliticalfutures.com/feed/", "think_tank"),
}

PERSPECTIVE_LABELS = {
    "western_mainstream": "Mainstream Occidentale",
    "italian_mainstream": "Stampa Italiana",
    "pro_israel": "Stampa Israeliana",
    "arab_media": "Media Arabi",
    "alternative_left": "Critica Alternativa",
    "russian_state": "Media Russi",
    "chinese_state": "Media Cinesi/Asiatici",
    "think_tank": "Think Tank & Analisi",
    "other": "Altro",
}

KEYWORDS_IT = [
    "guerra", "conflitto", "militare", "esercito", "nato", "ucraina", "russia", "cina",
    "taiwan", "israele", "palestina", "gaza", "siria", "iran", "medio oriente", "geopolitica",
    "sanzioni", "missili", "bombe", "attacco", "offensiva", "difesa", "diplomazia", "accordo",
    "trattato", "embargo", "cremlino", "zelensky", "putin", "brics", "g7", "g20", "balcani",
    "africa", "sahel", "houthi", "hezbollah", "armi", "nucleare", "droni", "esercitazione",
    "invasione", "truppe", "fronte",
]
KEYWORDS_EN = [
    "war", "conflict", "military", "army", "nato", "ukraine", "russia", "china", "taiwan",
    "israel", "palestine", "gaza", "syria", "iran", "middle east", "geopolitics", "sanctions",
    "missile", "bomb", "attack", "offensive", "defense", "diplomacy", "treaty", "embargo",
    "kremlin", "zelensky", "putin", "brics", "g7", "g20", "balkans", "africa", "sahel",
    "houthi", "hezbollah", "weapons", "nuclear", "drone", "exercise", "troops", "forces",
    "invasion", "ceasefire", "peace talks", "coup", "airstrike", "frontline", "casualties",
    "geopolitical", "security council", "pentagon", "warfare",
]
ALL_KEYWORDS = set(KEYWORDS_IT + KEYWORDS_EN)

CATEGORY_TAGS = {
    "🔴 Russia-Ucraina": ["ucraina", "ukraine", "russia", "zelensky", "putin", "donbass", "kharkiv", "kherson", "crimea", "kyiv"],
    "🟠 Medio Oriente": ["israel", "israele", "palestin", "gaza", "hamas", "hezbollah", "iran", "libano", "lebanon", "houthi", "yemen", "siria", "syria", "netanyahu"],
    "🟡 Cina & Indo-Pacifico": ["china", "cina", "taiwan", "indo-pacific", "south china sea", "japan", "giappone", "corea", "korea", "beijing", "pechino", "xi jinping"],
    "🟢 Africa & Sahel": ["africa", "sahel", "mali", "niger", "sudan", "ethiopia", "somalia", "congo", "burkina"],
    "🔵 NATO & Occidente": ["nato", "g7", "eu", "ue", "europa", "europe", "difesa", "defense", "pentagon", "washington", "bruxelles"],
    "⚪ Altro": [],
}

def categorize(text):
    text_lower = text.lower()
    for cat, keys in CATEGORY_TAGS.items():
        if cat == "⚪ Altro": continue
        for k in keys:
            if k in text_lower: return cat
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
    for source, (url, perspective) in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))
                published = entry.get("published", datetime.now().isoformat())
                if not link or not title: continue
                if not is_relevant(title, summary): continue
                category = categorize(title + " " + summary)
                save_article(source, title, link, summary, published, category, perspective)
                count += 1
        except Exception as e:
            print(f"Error fetching {source}: {e}")
    print(f"[DONE] Saved {count} relevant articles.")

# ─────────────────────────────────────────────
# SELEZIONE BILANCIATA PER PROSPETTIVA
# ─────────────────────────────────────────────
def select_balanced_articles(all_articles, max_total=25, max_per_perspective=4):
    by_perspective = defaultdict(list)
    for a in all_articles:
        by_perspective[a.get('perspective', 'other')].append(a)
    selected = []
    seen_links = set()
    for a in all_articles[:10]:
        if a['link'] not in seen_links:
            selected.append(a)
            seen_links.add(a['link'])
    perspectives = list(by_perspective.keys())
    per_perspective_count = defaultdict(int)
    for a in selected:
        per_perspective_count[a.get('perspective', 'other')] += 1
    remaining = [a for a in all_articles[10:] if a['link'] not in seen_links]
    i = 0
    while len(selected) < max_total and i < len(remaining) * 2:
        for persp in perspectives:
            if len(selected) >= max_total: break
            candidates = [a for a in remaining if a.get('perspective') == persp
                          and a['link'] not in seen_links
                          and per_perspective_count[persp] < max_per_perspective]
            if candidates:
                a = candidates[0]
                selected.append(a)
                seen_links.add(a['link'])
                per_perspective_count[persp] += 1
        i += 1
    return selected

# ─────────────────────────────────────────────
# CLAUDE API
# ─────────────────────────────────────────────
def call_claude(prompt, max_tokens=5000):
    if not ANTHROPIC_API_KEY:
        return "API key non configurata."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        return f"Errore API Claude: {e}"

def generate_theme_tag(keywords_str):
    prompt = (
        f"Dato il tema geopolitico \"{keywords_str}\", assegna UN SOLO tag tematico in italiano (massimo 3 parole). "
        "Esempi validi: Medio Oriente, Russia-Ucraina, Indo-Pacifico, Africa Subsahariana, Europa-NATO, "
        "America Latina, Balcani, Golfo Persico, Asia Centrale, Competizione Tecnologica, Energia Globale. "
        "Rispondi SOLO con il tag, niente altro, niente punteggiatura finale."
    )
    try:
        tag = call_claude(prompt, max_tokens=20).strip().strip('.')
        if len(tag) > 40 or '\n' in tag:
            return "Altro"
        return tag
    except Exception:
        return "Altro"

def generate_analysis(keywords_list, articles, previous_analyses=None):
    by_perspective = defaultdict(list)
    for a in articles:
        by_perspective[a.get('perspective', 'other')].append(a)

    articles_text = ""
    for persp, arts in by_perspective.items():
        label = PERSPECTIVE_LABELS.get(persp, persp)
        articles_text += f"\n\n=== {label.upper()} ===\n"
        for a in arts:
            articles_text += f"• [{a['source']}] {a['title']}\n  {a['summary'][:150]}\n"

    perspectives_present = [PERSPECTIVE_LABELS.get(p, p) for p in by_perspective.keys()]
    perspectives_missing = [PERSPECTIVE_LABELS.get(p, p) for p in PERSPECTIVE_LABELS.keys() if p not in by_perspective]
    keywords_str = ", ".join(keywords_list)

    history_context = ""
    if previous_analyses:
        history_context = "\n\nANALISI PRECEDENTI SULLO STESSO TEMA:\n"
        for pa in previous_analyses[:2]:
            history_context += f"\n[{pa['created_at'][:10]}]\n{pa['narrative_map'][:400]}...\n"

    # ── CHIAMATA 1: sezioni 1-5 ──────────────────────────────────────────────
    prompt_analysis = f"""Sei un analista di intelligence geopolitica. Il tuo metodo è la MAPPATURA DELLE NARRATIVE: non cerchi una verità unica, ma mappi cosa dice ogni prospettiva editoriale, dove convergono e dove divergono.

TEMA: {keywords_str}
PROSPETTIVE PRESENTI: {', '.join(perspectives_present)}
PROSPETTIVE ASSENTI (nessun articolo disponibile): {', '.join(perspectives_missing) if perspectives_missing else 'nessuna'}
{history_context}
ARTICOLI PER PROSPETTIVA:
{articles_text}

Produci un'analisi in 5 sezioni. Usa ESATTAMENTE questi titoli di sezione:

## 1. MAPPA DELLE NARRATIVE
Per ogni prospettiva presente, sintetizza in 2-3 frasi cosa dice e quale frame interpretativo usa. Sii preciso e fedele a ciò che le fonti dicono realmente — non attribuire posizioni non documentate.
Formato: **[Prospettiva]**: testo.

## 2. CONVERGENZE
Cosa concordano tutte o quasi tutte le prospettive? Questi sono i fatti più solidi. Max 150 parole.

## 3. DIVERGENZE E CONFLITTI NARRATIVI
Dove le prospettive si contraddicono radicalmente? Quali sono i punti di scontro narrativo più rilevanti? Quali domande rimangono aperte? Max 200 parole.

## 4. PROSPETTIVA DEL DIRITTO INTERNAZIONALE
Valutazione basata su fatti convergenti (non su narrative di parte): Carta ONU, Convenzioni di Ginevra, diritto umanitario. Max 150 parole.

## 5. FILO NARRATIVO
Se esistono analisi precedenti, come si è evoluta la situazione? Quali previsioni si sono avverate? Cosa è cambiato nel conflitto narrativo? Se è la prima analisi, stabilisci i marcatori per il futuro. Max 150 parole.

Rispondi SOLO con le 5 sezioni. Usa ESATTAMENTE i titoli indicati sopra."""

    raw_analysis = call_claude(prompt_analysis)

    # ── CHIAMATA 2: script con tutto il contesto elaborato ───────────────────
    prompt_script = f"""Sei un giornalista geopolitico con vent'anni di esperienza. Devi scrivere uno script audio di 90 secondi per un canale Instagram/LinkedIn di intelligence geopolitica.

Hai a disposizione l'analisi completa di questa settimana sul tema: {keywords_str}

═══ ANALISI COMPLETA ═══
{raw_analysis}
═══ FINE ANALISI ═══

Scrivi lo script usando TUTTO il materiale dell'analisi — non solo gli articoli grezzi. Il diritto internazionale, il filo narrativo, le convergenze e le divergenze sono il tuo materiale principale: usali per costruire profondità e contesto che i titoli di giornale non danno.

Struttura in TRE MOVIMENTI OBBLIGATORI. Niente titoli, niente intestazioni nel testo finale.

MOVIMENTO 1 — CRONACA RESPIRATA
Racconta i fatti con respiro. Ogni fatto ha spazio per atterrare. Non correre da un dato all'altro.
— Apri con il fatto più anomalo o pesante. Concreto, specifico, misurabile.
— Sviluppa il contesto usando le prospettive della Mappa delle Narrative come materiale per costruire la scena.
— Ogni elemento importante ha almeno due frasi. Lascia che atterri prima di passare al prossimo.
— Nomina sempre le testate esplicitamente quando citi comportamenti editoriali.
— Usa il Filo Narrativo per aggiungere profondità storica: cosa era stato previsto, cosa si è avverato.
— Almeno un riferimento storico preciso ("come nel [anno], quando [fatto specifico]").
— Lunghezza: 220-250 parole per lingua. Ritmo variabile: frasi brevi per i fatti, più distese per il contesto.

MOVIMENTO 2 — ANALISI CRUDA: COSA SIGNIFICA PER NOI
Non ideologia. Non opinione. Conseguenze concrete per chi ascolta.
— Usa le Convergenze e le Divergenze per identificare i fatti più solidi su cui basare l'analisi.
— Usa la Prospettiva del Diritto Internazionale per mostrare il quadro legale — non come astrazione ma come conseguenza pratica per i cittadini.
— Cosa cambia nella vita reale di un cittadino europeo, italiano, russo, indiano?
— Traduci la geopolitica in economia domestica: bollette, inflazione, prezzi, lavoro, migrazioni.
— Mostra la catena causale: questa violazione del diritto → questo effetto geopolitico → questa conseguenza per te.
— Sii specifico con i numeri quando disponibili nell'analisi.
— Includi almeno due geografie diverse per mostrare che le conseguenze non sono uniformi.
— Tono: diagnosi. Come un medico che ti dice cosa hai, senza urlare e senza minimizzare.
— Lunghezza: 80-100 parole per lingua.

MOVIMENTO 3 — IL COMMENTO: IL DISAGIO
Breve. Freddo. Zero retorica. Zero moralismo.
— Non fare domande retoriche. Non predicare.
— Trova UN fatto economico concreto emerso dall'analisi — un titolo azionario salito, un contratto firmato, un'azienda che ha guadagnato, un numero preciso — che rivela chi ha tratto vantaggio da questa settimana.
— Nominalo. Descrivi il fatto. Chiudi. L'ascoltatore ci arriva da solo.
— Esempio del tono: "I contratti per quelle bombe sono già stati firmati. Lockheed Martin ha chiuso la settimana in rialzo del 7%." Nessuna accusa, solo fatti.
— Tono: cronista, freddo come un referto.
— Lunghezza: 30-50 parole per lingua. Meno è più.

Prima italiano (tutti e tre i movimenti in sequenza), poi inglese (tutti e tre i movimenti in sequenza).
Niente elenchi, niente titoletti, niente markdown nel testo finale.
Rispondi SOLO con lo script. Nessun titolo, nessuna premessa."""

    raw_script = call_claude(prompt_script)

    # Combina i due output in un unico testo con il titolo della sezione 6
    return raw_analysis + "\n\n## 6. SCRIPT (90 secondi, bilingue IT/EN)\n" + raw_script


def run_analysis_job(job_id, keywords, articles, previous):
    jobs[job_id]["status"] = "running"
    try:
        raw = generate_analysis(keywords, articles, previous)

        def extract_section(text, title):
            pattern = rf"## {re.escape(title)}\n(.*?)(?=\n## |\Z)"
            match = re.search(pattern, text, re.DOTALL)
            return match.group(1).strip() if match else ""

        def extract_fuzzy(text, keyword):
            pattern = rf"## [^\n]*{re.escape(keyword)}[^\n]*\n(.*?)(?=\n## |\Z)"
            match = re.search(pattern, text, re.DOTALL)
            return match.group(1).strip() if match else ""

        narrative_map = extract_section(raw, "1. MAPPA DELLE NARRATIVE") or ""
        convergences  = extract_section(raw, "2. CONVERGENZE") or ""
        divergences   = extract_section(raw, "3. DIVERGENZE E CONFLITTI NARRATIVI") or ""
        legal         = extract_section(raw, "4. PROSPETTIVA DEL DIRITTO INTERNAZIONALE") or ""
        thread        = extract_section(raw, "5. FILO NARRATIVO") or ""
        instagram     = extract_fuzzy(raw, "SCRIPT") or ""

        if not narrative_map and not convergences:
            print(f"[WARN] Sezioni non estratte. Raw output (primi 500 char): {raw[:500]}")

        by_perspective = defaultdict(list)
        for a in articles:
            by_perspective[a.get('perspective', 'other')].append(a)
        perspectives_used = {p: PERSPECTIVE_LABELS.get(p, p) for p in by_perspective.keys()}

        keywords_str = ", ".join(keywords)
        theme_tag = generate_theme_tag(keywords_str)

        save_analysis(", ".join(keywords), len(articles), narrative_map, convergences,
                      divergences, legal, thread, instagram, theme_tag)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = {
            "keywords": keywords,
            "article_count": len(articles),
            "articles": articles[:15],
            "perspectives_used": perspectives_used,
            "narrative_map": narrative_map,
            "convergences": convergences,
            "divergences": divergences,
            "legal": legal,
            "thread": thread,
            "instagram_script": instagram,
            "has_history": len(previous) > 0,
            "theme_tag": theme_tag,
        }
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        print(f"[ERROR] Job {job_id}: {e}")

# ─────────────────────────────────────────────
# ROUTES PUBBLICHE
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/news")
def api_news():
    category = request.args.get("category", "all")
    source   = request.args.get("source", "all")
    limit    = int(request.args.get("limit", 60))
    offset   = int(request.args.get("offset", 0))
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    query = "SELECT source, title, link, summary, published, category, fetched_at FROM articles WHERE 1=1"
    params = []
    if category != "all": query += " AND category = %s"; params.append(category)
    if source != "all":   query += " AND source = %s";   params.append(source)
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
    c.execute("SELECT COUNT(*) FROM articles"); total = c.fetchone()[0]
    c.execute("SELECT category, COUNT(*) FROM articles GROUP BY category ORDER BY COUNT(*) DESC")
    by_cat = {r[0]: r[1] for r in c.fetchall()}
    c.execute("SELECT source, COUNT(*) FROM articles GROUP BY source ORDER BY COUNT(*) DESC")
    by_source = {r[0]: r[1] for r in c.fetchall()}
    c.execute("SELECT MAX(fetched_at) FROM articles"); last_update = c.fetchone()[0]
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
    if not session.get("admin"): return redirect(url_for("admin_login"))
    return render_template("analisi.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        error = "Password errata."
    return render_template("login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))

@app.route("/api/admin/analyze", methods=["POST"])
def api_analyze():
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    data = request.json
    keywords = [k.strip().lower() for k in data.get("keywords", []) if k.strip()]
    if not keywords: return jsonify({"error": "Inserisci almeno una keyword"}), 400
    days = int(data.get("days", 7))  # default 7 giorni; 0 = tutto
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    conditions = " OR ".join(["(LOWER(title) LIKE %s OR LOWER(summary) LIKE %s)" for _ in keywords])
    params = []
    for kw in keywords: params.extend([f"%{kw}%", f"%{kw}%"])
    date_filter = ""
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        date_filter = " AND fetched_at >= %s"
        params.append(cutoff)
    c.execute(f"""SELECT source, title, link, summary, published, category, perspective
                  FROM articles WHERE ({conditions}){date_filter} ORDER BY id DESC LIMIT 500""", params)
    all_articles = [dict(r) for r in c.fetchall()]
    kw_conditions = " OR ".join(["LOWER(keywords) LIKE %s" for _ in keywords])
    kw_params = [f"%{kw}%" for kw in keywords]
    c.execute(f"SELECT narrative_map, created_at FROM analyses WHERE {kw_conditions} ORDER BY created_at DESC LIMIT 2", kw_params)
    previous = [dict(r) for r in c.fetchall()]
    conn.close()
    if not all_articles:
        return jsonify({"error": f"Nessun articolo trovato per: {', '.join(keywords)}"}), 404
    articles = select_balanced_articles(all_articles, max_total=25, max_per_perspective=4)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}
    t = threading.Thread(target=run_analysis_job, args=(job_id, keywords, articles, previous))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id, "article_count": len(all_articles), "selected": len(articles)})

@app.route("/api/admin/job/<job_id>")
def api_job_status(job_id):
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    job = jobs.get(job_id)
    if not job: return jsonify({"error": "Job non trovato"}), 404
    return jsonify(job)

@app.route("/api/admin/analyses")
def api_analyses_history():
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT id, keywords, article_count, created_at, theme_tag FROM analyses ORDER BY created_at DESC LIMIT 50")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/admin/analyses/<int:analysis_id>")
def api_analysis_detail(analysis_id):
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM analyses WHERE id = %s", (analysis_id,))
    row = c.fetchone()
    conn.close()
    if not row: return jsonify({"error": "Non trovata"}), 404
    return jsonify(dict(row))

@app.route("/api/admin/analyses/<int:analysis_id>", methods=["DELETE"])
def api_analysis_delete(analysis_id):
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM analyses WHERE id = %s", (analysis_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": analysis_id})

# ─────────────────────────────────────────────
# ELEVENLABS TTS
# ─────────────────────────────────────────────
@app.route("/api/admin/tts", methods=["POST"])
def api_tts():
    if not session.get("admin"): return jsonify({"error": "Non autorizzato"}), 403
    if not ELEVENLABS_API_KEY: return jsonify({"error": "ElevenLabs API key non configurata"}), 500
    if not ELEVENLABS_VOICE_ID: return jsonify({"error": "ElevenLabs Voice ID non configurato"}), 500
    data = request.json
    text = (data.get("text") or "").strip()
    if not text: return jsonify({"error": "Testo vuoto"}), 400
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg"
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": 1.15
            }
        }
        r = req_lib.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            return jsonify({"error": f"ElevenLabs error {r.status_code}: {r.text[:200]}"}), 500
        from flask import Response
        return Response(
            r.content,
            mimetype="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=theatrum_belli_script.mp3"}
        )
    except Exception as e:
        print(f"TTS error: {e}")
        return jsonify({"error": str(e)}), 500

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
