import feedparser
import threading
import os
import re
import json
import uuid
import unicodedata
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, Response
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
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "theatrum2026")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

jobs = {}

# ─────────────────────────────────────────────
# POOL AUTORI
# ─────────────────────────────────────────────
AUTHORS = [
    {
        "nome": "Marco Ferretti",
        "ruolo": "Analista geopolitico — Area Medio Oriente e Golfo Persico",
        "temi": ["medio oriente", "iran", "israele", "palestina", "gaza", "hamas",
                 "hezbollah", "golfo", "arabia", "yemen", "houthi", "siria", "libano"],
    },
    {
        "nome": "Giulia Marchetti",
        "ruolo": "Analista — Russia, Europa Orientale e NATO",
        "temi": ["russia", "ucraina", "nato", "europa", "mosca", "putin", "zelensky",
                 "donbass", "bielorussia", "moldavia", "balcani", "crimea"],
    },
    {
        "nome": "Lorenzo Conti",
        "ruolo": "Analista — Indo-Pacifico, Cina e Taiwan",
        "temi": ["cina", "china", "taiwan", "indo-pacifico", "corea", "giappone",
                 "japan", "beijing", "pechino", "xi jinping", "hong kong", "myanmar"],
    },
    {
        "nome": "Sara Romani",
        "ruolo": "Analista — Africa subsahariana e conflitti asimmetrici",
        "temi": ["africa", "sahel", "mali", "niger", "sudan", "etiopia", "somalia",
                 "congo", "burkina", "senegal", "libia", "jihadismo"],
    },
    {
        "nome": "Andrea Bassi",
        "ruolo": "Analista — Economia di guerra, energia e sanzioni",
        "temi": ["energia", "gas", "petrolio", "sanzioni", "embargo", "economia",
                 "brics", "dollaro", "commercio", "supply chain", "fertilizzanti",
                 "lockheed", "raytheon", "difesa industriale"],
    },
    {
        "nome": "Chiara Vitale",
        "ruolo": "Analista — Diritto internazionale e diplomazia multilaterale",
        "temi": ["diplomazia", "onu", "g7", "g20", "diritto internazionale",
                 "accordo", "trattato", "corte", "icc", "summit", "negoziato"],
    },
]

def assign_author(keywords_str, theme_tag=""):
    """Assegna l'autore più pertinente in base ai keyword e al tema."""
    text = (keywords_str + " " + theme_tag).lower()
    scores = []
    for author in AUTHORS:
        score = sum(1 for t in author["temi"] if t in text)
        scores.append((score, author))
    scores.sort(key=lambda x: x[0], reverse=True)
    # Se nessun match, usa Andrea Bassi come default (economia di guerra — tema trasversale)
    if scores[0][0] == 0:
        return AUTHORS[4]
    return scores[0][1]

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
    for col in ["narrative_map","convergences","divergences","thread","instagram_script","legal"]:
        c.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {col} TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS theme_tag TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS visual_prompts TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS articles_json TEXT")

    # Tabella articoli editoriali
    c.execute("""
        CREATE TABLE IF NOT EXISTS articoli (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE,
            titolo TEXT,
            categoria TEXT,
            tags TEXT,
            sezione_dati TEXT,
            sezione_analisi TEXT,
            sezione_conseguenze TEXT,
            immagine_prompt TEXT,
            immagine_url TEXT,
            post_social TEXT,
            autore_nome TEXT,
            autore_ruolo TEXT,
            analisi_id INTEGER,
            status TEXT DEFAULT 'bozza',
            created_at TEXT,
            published_at TEXT
        )
    """)
    # Migrazione: aggiungi colonne autore se non esistono (per DB già esistenti)
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS autore_nome TEXT")
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS autore_ruolo TEXT")
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS immagine_hero TEXT DEFAULT ''")
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS immagine_inline1 TEXT DEFAULT ''")
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS immagine_inline2 TEXT DEFAULT ''")
    c.execute("ALTER TABLE articoli ADD COLUMN IF NOT EXISTS immagine_prompt_mid TEXT DEFAULT ''")

    source_map = {
        "ANSA Mondo":"italian_mainstream","Repubblica Esteri":"italian_mainstream",
        "Corriere Esteri":"italian_mainstream","Il Sole 24 Ore Mondo":"italian_mainstream",
        "Il Fatto Quotidiano Esteri":"italian_mainstream","Limes":"think_tank",
        "BBC World":"western_mainstream","Reuters World":"western_mainstream",
        "The Guardian World":"western_mainstream","AP News":"western_mainstream",
        "DW World":"western_mainstream","France24 EN":"western_mainstream",
        "Euronews EN":"western_mainstream","Jerusalem Post":"pro_israel",
        "Times of Israel":"pro_israel","Haaretz EN":"pro_israel","i24 News":"pro_israel",
        "Al Jazeera English":"arab_media","Middle East Eye":"arab_media",
        "The Cradle":"alternative_left","MintPress News":"alternative_left",
        "Multipolarista":"alternative_left","Consortium News":"alternative_left",
        "Antiwar.com":"alternative_left","Responsible Statecraft":"alternative_left",
        "Scenari Economici":"alternative_left","TASS English":"russian_state",
        "RT World":"russian_state","Sputnik World":"russian_state",
        "Global Times EN":"chinese_state","CGTN World":"chinese_state",
        "SCMP World":"chinese_state","ISW":"think_tank",
        "Foreign Affairs":"think_tank","The Diplomat":"think_tank",
        "Defense One":"think_tank","War on the Rocks":"think_tank",
        "Geopolitical Futures":"think_tank",
    }
    for source, persp in source_map.items():
        c.execute(
            "UPDATE articles SET perspective=%s WHERE source=%s AND (perspective IS NULL OR perspective='other')",
            (persp, source)
        )
    conn.commit()
    conn.close()

def save_article(source, title, link, summary, published, category, perspective):
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO articles (source,title,link,summary,published,category,perspective,fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (link) DO NOTHING
        """, (source, title, link[:500] if link else "", summary[:500] if summary else "",
              published, category, perspective, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception as e:
        print(f"DB error: {e}"); conn.rollback()
    finally:
        conn.close()

def save_analysis(keywords, article_count, narrative_map, convergences, divergences,
                  legal, thread, instagram_script, theme_tag="", visual_prompts="", articles_json=""):
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        INSERT INTO analyses (keywords,article_count,narrative_map,convergences,divergences,
            legal,thread,instagram_script,created_at,theme_tag,visual_prompts,articles_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (keywords, article_count, narrative_map, convergences, divergences,
          legal, thread, instagram_script, datetime.now(timezone.utc).isoformat(),
          theme_tag, visual_prompts, articles_json))
    new_id = c.fetchone()[0]
    conn.commit(); conn.close()
    return new_id

# ─────────────────────────────────────────────
# FONTI RSS
# ─────────────────────────────────────────────
FEEDS = {
    "ANSA Mondo":("https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml","italian_mainstream"),
    "Repubblica Esteri":("https://www.repubblica.it/rss/esteri/rss2.0.xml","italian_mainstream"),
    "Corriere Esteri":("https://xml2.corriereobjects.it/rss/esteri.xml","italian_mainstream"),
    "Il Sole 24 Ore Mondo":("https://www.ilsole24ore.com/rss/mondo.xml","italian_mainstream"),
    "Il Fatto Quotidiano":("https://www.ilfattoquotidiano.it/feed/","italian_mainstream"),
    "Limes":("https://news.google.com/rss/search?q=site:limesonline.com&hl=it&gl=IT&ceid=IT:it","think_tank"),
    "BBC World":("http://feeds.bbci.co.uk/news/world/rss.xml","western_mainstream"),
    "Reuters World":("https://news.google.com/rss/search?q=site:reuters.com+world&hl=en-US&gl=US&ceid=US:en","western_mainstream"),
    "The Guardian World":("https://www.theguardian.com/world/rss","western_mainstream"),
    "AP News":("https://news.google.com/rss/search?q=site:apnews.com+world&hl=en-US&gl=US&ceid=US:en","western_mainstream"),
    "DW World":("https://rss.dw.com/rdf/rss-en-world","western_mainstream"),
    "France24 EN":("https://www.france24.com/en/rss","western_mainstream"),
    "Euronews EN":("https://www.euronews.com/rss","western_mainstream"),
    "Jerusalem Post":("https://www.jpost.com/rss/rssfeedsfrontpage.aspx","pro_israel"),
    "Times of Israel":("https://news.google.com/rss/search?q=site:timesofisrael.com&hl=en-US&gl=US&ceid=US:en","pro_israel"),
    "Haaretz EN":("https://news.google.com/rss/search?q=site:haaretz.com&hl=en-US&gl=US&ceid=US:en","pro_israel"),
    "i24 News":("https://news.google.com/rss/search?q=site:i24news.tv&hl=en-US&gl=US&ceid=US:en","pro_israel"),
    "Al Jazeera English":("https://www.aljazeera.com/xml/rss/all.xml","arab_media"),
    "Middle East Eye":("https://www.middleeasteye.net/rss","arab_media"),
    "The New Arab":("https://www.newarab.com/rss","arab_media"),
    "Al-Monitor":("https://www.al-monitor.com/rss","arab_media"),
    "Egypt Independent":("https://egyptindependent.com/feed/","arab_media"),
    "Global Times EN":("https://www.globaltimes.cn/rss/outbrain.xml","chinese_state"),
    "CGTN World":("https://www.cgtn.com/subscribe/rss/section/world.xml","chinese_state"),
    "China Daily World":("http://www.chinadaily.com.cn/rss/world_rss.xml","chinese_state"),
    "The Moscow Times":("https://www.themoscowtimes.com/rss/news","alternative_left"),
    "Press TV":("https://www.presstv.ir/rss.xml","iran_media"),
    "Tehran Times":("https://www.tehrantimes.com/rss","iran_media"),
    "Mehr News EN":("https://en.mehrnews.com/rss","iran_media"),
    "The Hindu Intl":("https://www.thehindu.com/news/international/feeder/default.rss","india_media"),
    "Hindustan Times World":("https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml","india_media"),
    "Daily Sabah":("https://www.dailysabah.com/rssFeed/9","turkey_media"),
    "The Cradle":("https://thecradle.co/feed","alternative_left"),
    "MintPress News":("https://www.mintpressnews.com/feed/","alternative_left"),
    "Multipolarista":("https://multipolarista.com/feed/","alternative_left"),
    "Consortium News":("https://consortiumnews.com/feed/","alternative_left"),
    "Antiwar.com":("https://www.antiwar.com/blog/feed/","alternative_left"),
    "Responsible Statecraft":("https://responsiblestatecraft.org/feed/","alternative_left"),
    "Scenari Economici":("https://scenarieconomici.it/feed/","alternative_left"),
    "TASS English":("https://tass.com/rss/v2.xml","russian_state"),
    "RT World":("https://www.rt.com/rss/news/","russian_state"),
    "Xinhua EN":("http://www.xinhuanet.com/english/rss/worldrss.xml","chinese_state"),
    "SCMP World":("https://www.scmp.com/rss/91/feed","chinese_state"),
    "ISW":("https://news.google.com/rss/search?q=site:understandingwar.org&hl=en-US&gl=US&ceid=US:en","think_tank"),
    "Foreign Affairs":("https://www.foreignaffairs.com/rss.xml","think_tank"),
    "The Diplomat":("https://thediplomat.com/feed/","think_tank"),
    "Defense One":("https://www.defenseone.com/rss/all/","think_tank"),
    "War on the Rocks":("https://warontherocks.com/feed/","think_tank"),
    "Geopolitical Futures":("https://geopoliticalfutures.com/feed/","think_tank"),
}

PERSPECTIVE_LABELS = {
    "western_mainstream":"Mainstream Occidentale","italian_mainstream":"Stampa Italiana",
    "pro_israel":"Stampa Israeliana","arab_media":"Media Arabi",
    "alternative_left":"Critica Alternativa","russian_state":"Media Russi",
    "chinese_state":"Media Cinesi/Asiatici","think_tank":"Think Tank & Analisi",
    "iran_media":"Stampa Iraniana","india_media":"Stampa Indiana","turkey_media":"Stampa Turca",
    "other":"Altro",
}

KEYWORDS_IT = [
    "guerra","conflitto","militare","esercito","nato","ucraina","russia","cina","taiwan",
    "israele","palestina","gaza","siria","iran","medio oriente","geopolitica","sanzioni",
    "missili","bombe","attacco","offensiva","difesa","diplomazia","accordo","trattato",
    "embargo","cremlino","zelensky","putin","brics","g7","g20","balcani","africa","sahel",
    "houthi","hezbollah","armi","nucleare","droni","esercitazione","invasione","truppe","fronte",
]
KEYWORDS_EN = [
    "war","conflict","military","army","nato","ukraine","russia","china","taiwan","israel",
    "palestine","gaza","syria","iran","middle east","geopolitics","sanctions","missile","bomb",
    "attack","offensive","defense","diplomacy","treaty","embargo","kremlin","zelensky","putin",
    "brics","g7","g20","balkans","africa","sahel","houthi","hezbollah","weapons","nuclear",
    "drone","exercise","troops","forces","invasion","ceasefire","peace talks","coup","airstrike",
    "frontline","casualties","geopolitical","security council","pentagon","warfare",
]
ALL_KEYWORDS = set(KEYWORDS_IT + KEYWORDS_EN)

CATEGORY_TAGS = {
    "🔴 Russia-Ucraina":["ucraina","ukraine","russia","zelensky","putin","donbass","kharkiv","kherson","crimea","kyiv"],
    "🟠 Medio Oriente":["israel","israele","palestin","gaza","hamas","hezbollah","iran","libano","lebanon","houthi","yemen","siria","syria","netanyahu"],
    "🟡 Cina & Indo-Pacifico":["china","cina","taiwan","indo-pacific","south china sea","japan","giappone","corea","korea","beijing","pechino","xi jinping"],
    "🟢 Africa & Sahel":["africa","sahel","mali","niger","sudan","ethiopia","somalia","congo","burkina"],
    "🔵 NATO & Occidente":["nato","g7","eu","ue","europa","europe","difesa","defense","pentagon","washington","bruxelles"],
    "⚪ Altro":[],
}

def _kw_match(keyword, text):
    # Match per parola intera quando la keyword e' alfabetica e senza spazi/trattini,
    # altrimenti (frasi tipo "south china sea", "middle east", "indo-pacific") match diretto.
    if " " in keyword or "-" in keyword or not keyword.isalpha():
        return keyword in text
    return re.search(r"\b" + re.escape(keyword) + r"\b", text) is not None

def categorize(text):
    t = text.lower()
    for cat, keys in CATEGORY_TAGS.items():
        if cat == "⚪ Altro": continue
        for k in keys:
            if _kw_match(k, t): return cat
    return "⚪ Altro"

def is_relevant(title, summary=""):
    text = (title + " " + summary).lower()
    return any(_kw_match(kw, text) for kw in ALL_KEYWORDS)

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
# SELEZIONE BILANCIATA
# ─────────────────────────────────────────────
def select_balanced_articles(all_articles, max_total=25, max_per_perspective=4):
    by_perspective = defaultdict(list)
    for a in all_articles:
        by_perspective[a.get('perspective','other')].append(a)
    selected = []; seen_links = set()
    for a in all_articles[:10]:
        if a['link'] not in seen_links:
            selected.append(a); seen_links.add(a['link'])
    perspectives = list(by_perspective.keys())
    per_perspective_count = defaultdict(int)
    for a in selected:
        per_perspective_count[a.get('perspective','other')] += 1
    remaining = [a for a in all_articles[10:] if a['link'] not in seen_links]
    i = 0
    while len(selected) < max_total and i < len(remaining) * 2:
        for persp in perspectives:
            if len(selected) >= max_total: break
            candidates = [a for a in remaining if a.get('perspective')==persp
                          and a['link'] not in seen_links
                          and per_perspective_count[persp] < max_per_perspective]
            if candidates:
                a = candidates[0]; selected.append(a)
                seen_links.add(a['link']); per_perspective_count[persp] += 1
        i += 1
    return selected

# ─────────────────────────────────────────────
# CLAUDE API
# ─────────────────────────────────────────────
def call_claude(prompt, max_tokens=5000):
    if not ANTHROPIC_API_KEY: return "API key non configurata."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role":"user","content":prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}"); return f"Errore API Claude: {e}"

def generate_theme_tag(keywords_str):
    prompt = (
        f"Dato il tema geopolitico \"{keywords_str}\", assegna UN SOLO tag tematico in italiano (massimo 3 parole). "
        "Esempi: Medio Oriente, Russia-Ucraina, Indo-Pacifico, Africa Subsahariana, Europa-NATO, "
        "Golfo Persico, Asia Centrale, Competizione Tecnologica, Energia Globale. "
        "Rispondi SOLO con il tag, niente altro."
    )
    try:
        tag = call_claude(prompt, max_tokens=20).strip().strip('.')
        return tag if len(tag) <= 40 and '\n' not in tag else "Altro"
    except Exception:
        return "Altro"

def generate_visual_prompts(keywords_str, script_it, analysis_text, duration_seconds=132):
    prompt = f"""You are a world-class cinematographer and AI prompt engineer specializing in geopolitical documentary visuals for Instagram reels.

Create 15 image prompts for Leonardo AI (2:3 vertical format, 1024x1792px) for a {duration_seconds}-second reel about: {keywords_str}

VOICEOVER SCRIPT (Italian):
{script_it}

---
PROMPT STRUCTURE — follow this exact formula for every prompt:
[SUBJECT & ACTION] + [LOCATION/GEOGRAPHY] + [LIGHTING] + [ATMOSPHERE/MOOD] + [CAMERA] + [TECHNICAL STYLE]

REFERENCE EXAMPLES of high-quality prompts (use as quality benchmark):
Example 1 — aerial military: "Aerial drone view of a massive oil tanker navigating through the narrow Strait of Hormuz at dusk, surrounding rocky coastlines barely visible through industrial haze, dramatic side-lighting casting long shadows across the ship's deck, oppressive atmosphere of geopolitical tension, shot on Phase One IQ4 150MP, ultra-sharp details, 2:3 vertical composition, documentary photojournalism style, muted teal and burnt amber palette, no text, no recognizable faces"

Example 2 — governmental interior: "Empty government war room at 3am, long conference table reflecting harsh fluorescent overhead lighting, abandoned coffee cups and classified folders scattered across polished mahogany surface, single window showing dark city skyline, extreme wide angle lens distortion, hyper-realistic architectural photography, cold blue-grey palette with amber desk lamp accents, 2:3 vertical format, cinematic documentary still, no people, no readable text"

Example 3 — financial/abstract: "Extreme close-up of a defense contract document being signed, fountain pen tip pressing into cream paper, shallow depth of field blurring the printed text beyond recognition, single dramatic spotlight from above, deep black background, macro photography on Hasselblad, crisp ink texture detail, cold clinical atmosphere, desaturated palette with only the gold pen nib catching warm light, 2:3 vertical composition"

Example 4 — geographic/map: "Backlit satellite map of the Persian Gulf region projected onto a frosted glass screen in a dark intelligence briefing room, glowing cyan geographic contours against deep navy background, military grid overlays barely visible, dramatic rim lighting, cinematic spy thriller aesthetic, photorealistic render, 2:3 vertical format, no readable labels, no faces"

Example 5 — street/civilian: "Long exposure night photography of a European petrol station, blurred car light trails on wet asphalt reflecting neon price signs showing high fuel costs, lone attendant silhouette out of focus in background, rainy urban atmosphere, Sony A7R shot at f/1.4, cinematic grain, desaturated with only fuel price display glowing amber-orange, 2:3 vertical, no readable text"

---
VISUAL RULES:
- NO recognizable faces, NO readable text anywhere in the image
- Palette: dark navy, industrial amber, smoke grey, slate — zero saturated colors
- Alternate between: wide establishing shots, medium environmental shots, extreme close-up details
- Every prompt must specify: camera/lens type, lighting direction, depth of field, color palette
- Movements: 1 (7 images, 8-12s each) = raw news footage aesthetic / 2 (5 images, 8-10s each) = analytical cold observation / 3 (3 images, 5-7s each) = forensic financial detail
- Total duration must sum to exactly {duration_seconds} seconds
- Movement 3 must feel forensic and cold: contracts, stock tickers, factory floors, balance sheets (all unreadable)

Respond ONLY with valid JSON, no text before or after:
{{
  "total_duration": {duration_seconds},
  "prompts": [
    {{
      "id": 1,
      "movimento": 1,
      "label": "scene name (3-4 words)",
      "timing_start": 0,
      "duration": 10,
      "ken_burns": "slow zoom toward center / pan left to right / subtle zoom out",
      "prompt_en": "[60-100 word professional prompt]"
    }}
  ]
}}"""
    raw = call_claude(prompt, max_tokens=4000)
    try:
        json_match = re.search(r'\{[\s\S]*\}', raw)
        return json_match.group(0) if json_match else raw
    except Exception:
        return raw

def generate_analysis(keywords_list, articles, previous_analyses=None):
    by_perspective = defaultdict(list)
    for a in articles:
        by_perspective[a.get('perspective','other')].append(a)
    articles_text = ""
    for persp, arts in by_perspective.items():
        label = PERSPECTIVE_LABELS.get(persp, persp)
        articles_text += f"\n\n=== {label.upper()} ===\n"
        for a in arts:
            articles_text += f"• [{a['source']}] {a['title']}\n  {a['summary'][:150]}\n"
    perspectives_present = [PERSPECTIVE_LABELS.get(p,p) for p in by_perspective.keys()]
    perspectives_missing = [PERSPECTIVE_LABELS.get(p,p) for p in PERSPECTIVE_LABELS.keys() if p not in by_perspective]
    keywords_str = ", ".join(keywords_list)
    history_context = ""
    if previous_analyses:
        history_context = "\n\nANALISI PRECEDENTI SULLO STESSO TEMA:\n"
        for pa in previous_analyses[:2]:
            history_context += f"\n[{pa['created_at'][:10]}]\n{pa['narrative_map'][:400]}...\n"

    # ── 1. ANALISI INTELLIGENCE ──────────────────────────────────────
    prompt_analysis = f"""Sei un analista di intelligence geopolitica. Metodo: MAPPATURA DELLE NARRATIVE.

TEMA: {keywords_str}
PROSPETTIVE PRESENTI: {', '.join(perspectives_present)}
PROSPETTIVE ASSENTI: {', '.join(perspectives_missing) if perspectives_missing else 'nessuna'}
{history_context}
ARTICOLI:
{articles_text}

Produci analisi in 5 sezioni con ESATTAMENTE questi titoli:

## 1. MAPPA DELLE NARRATIVE
Per ogni prospettiva, 2-3 frasi. Formato: **[Prospettiva]**: testo.

## 2. CONVERGENZE
Fatti concordati da tutte le prospettive. Max 150 parole.

## 3. DIVERGENZE E CONFLITTI NARRATIVI
Contraddizioni radicali e domande aperte. Max 200 parole.

## 4. PROSPETTIVA DEL DIRITTO INTERNAZIONALE
Valutazione su fatti convergenti. Max 150 parole.

## 5. FILO NARRATIVO
Evoluzione rispetto ad analisi precedenti, o marcatori per il futuro. Max 150 parole.

Rispondi SOLO con le 5 sezioni."""
    raw_analysis = call_claude(prompt_analysis)

    # ── 2. ARTICOLO EDITORIALE ────────────────────────────────────────
    # Script e visual prompts sono on-demand (bottoni separati)
    author = assign_author(keywords_str)
    prompt_articolo = f"""Sei un giornalista geopolitico di lungo corso. Scrivi un articolo editoriale completo per Theatrum Belli.

TEMA: {keywords_str}
ANALISI INTELLIGENCE:
{raw_analysis}

Scrivi UN SOLO titolo editoriale — non descrittivo, non scolastico, colpisci come un headline di guerra fredda, max 12 parole.
Poi l'articolo in 3 sezioni esatte.

TITOLO: [titolo impattante]

## IL DATO CHE CONTA
Fatti nudi incrociati da più fonti. Apri con il dato più anomalo o inatteso.
Cita le testate esplicitamente. Almeno un riferimento storico preciso.
Niente interpretazioni — solo fatti verificati e la tensione tra di essi. 200-280 parole.

## THEATRUM BELLI — ANALISI
Meccanismi nascosti, contraddizioni strutturali, paradossi di potere.
Chi guadagna, chi perde, quali architetture di interesse sono in gioco.
Voce: osservatore freddo che conosce la storia. Niente "dovremmo", niente moralismo. 200-280 parole.

## COSA SIGNIFICA PER TE
Conseguenze concrete per il cittadino europeo/italiano. Catena causale geopolitica→economia domestica.
Bollette, prezzi, lavoro, logistica. Numeri specifici dove possibile.
Chiudi con UN fatto economico secco — titolo azionario, contratto firmato, percentuale.
Poi su riga separata: "— {author['nome']} continua a monitorare."
Tono: referto medico. 120-160 parole.

POST_SOCIAL: [post Instagram 150 parole max, stesso tono, chiudi con "🔗 theatrumbelli.com" — max 3 hashtag]

PROMPT_HEADER: [UNA SOLA RIGA di JSON valido (nessun a-capo dentro le graffe), valori in inglese, per l'immagine hero in cima all'articolo — panoramica d'impatto sul tema. Schema esatto: {{"scene":"...","main_subject":"...","secondary_subjects":["...","..."],"lighting":"...","color_palette":["...","..."],"composition":"cinematic wide shot, 16:9","mood":"...","constraints":["no faces","no readable text","no logos"]}}. main_subject = soggetto concreto dominante della scena. dark aesthetic, 16:9, niente volti, niente testo leggibile, niente loghi.]

PROMPT_MIDBODY: [UNA SOLA RIGA di JSON valido (nessun a-capo dentro le graffe), valori in inglese, stesso schema di PROMPT_HEADER {{"scene":"...","main_subject":"...","secondary_subjects":["...","..."],"lighting":"...","color_palette":["...","..."],"composition":"cinematic wide shot, 16:9","mood":"...","constraints":["no faces","no readable text","no logos"]}}, per l'immagine a metà articolo — dettaglio complementare alla hero (scena specifica o oggetto-simbolo). main_subject = soggetto concreto dominante della scena. dark aesthetic, 16:9, niente volti, niente testo leggibile, niente loghi.]

Rispondi in questo formato esatto — niente altro."""

    raw_articolo = call_claude(prompt_articolo, max_tokens=3000)
    return (raw_analysis, raw_articolo, author)

def clean_image_prompt(captured):
    """Pulisce il blocco PROMPT_HEADER/PROMPT_MIDBODY catturato.
    Se e' JSON valido lo normalizza su riga singola; altrimenti ritorna la stringa grezza (fallback)."""
    s = captured.strip().strip('[]').strip('*').strip()
    try:
        return json.dumps(json.loads(s), ensure_ascii=False)
    except (ValueError, TypeError):
        return s


def run_analysis_job(job_id, keywords, articles, previous):
    jobs[job_id]["status"] = "running"
    try:
        keywords_str = ", ".join(keywords)
        raw_analysis, raw_articolo, author = generate_analysis(keywords, articles, previous)

        def extract_section(text, title):
            m = re.search(rf"## {re.escape(title)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
            return m.group(1).strip() if m else ""

        narrative_map  = extract_section(raw_analysis, "1. MAPPA DELLE NARRATIVE")
        convergences   = extract_section(raw_analysis, "2. CONVERGENZE")
        divergences    = extract_section(raw_analysis, "3. DIVERGENZE E CONFLITTI NARRATIVI")
        legal          = extract_section(raw_analysis, "4. PROSPETTIVA DEL DIRITTO INTERNAZIONALE")
        thread         = extract_section(raw_analysis, "5. FILO NARRATIVO")

        # Estrai sezioni articolo per salvarle nel DB
        def extract_art_sec(text, header):
            m = re.search(rf"## {re.escape(header)}\n(.*?)(?=\n## |\n?\*{{0,2}}POST_SOCIAL:|\n?\*{{0,2}}PROMPT_HEADER:|\n?\*{{0,2}}PROMPT_MIDBODY:|\n?\*{{0,2}}PROMPT_IMMAGINE:|\Z)", text, re.DOTALL)
            return m.group(1).strip() if m else ""

        titolo_m = re.search(r'TITOLO:\s*(.+)', raw_articolo)
        art_titolo = titolo_m.group(1).strip().strip('[]').strip('*').strip() if titolo_m else keywords_str
        art_dati        = extract_art_sec(raw_articolo, "IL DATO CHE CONTA")
        art_analisi     = extract_art_sec(raw_articolo, "THEATRUM BELLI — ANALISI")
        art_conseguenze = extract_art_sec(raw_articolo, "COSA SIGNIFICA PER TE")
        m_social = re.search(r'\*{0,2}POST_SOCIAL:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_HEADER:|\n?\*{0,2}PROMPT_MIDBODY:|\n?\*{0,2}PROMPT_IMMAGINE:|\Z)', raw_articolo, re.DOTALL)
        art_social = m_social.group(1).strip().strip('[]').strip('*').strip() if m_social else ""
        m_prompt = re.search(r'\*{0,2}PROMPT_HEADER:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_MIDBODY:|\n?\*{0,2}PROMPT_IMMAGINE:|\Z)', raw_articolo, re.DOTALL)
        if not m_prompt:
            m_prompt = re.search(r'\*{0,2}PROMPT_IMMAGINE:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_MIDBODY:|\Z)', raw_articolo, re.DOTALL)
        art_prompt_img = clean_image_prompt(m_prompt.group(1)) if m_prompt else ""
        m_prompt_mid = re.search(r'\*{0,2}PROMPT_MIDBODY:\*{0,2}\s*(.*?)\Z', raw_articolo, re.DOTALL)
        art_prompt_mid = clean_image_prompt(m_prompt_mid.group(1)) if m_prompt_mid else ""

        by_perspective = defaultdict(list)
        for a in articles:
            by_perspective[a.get('perspective','other')].append(a)
        perspectives_used = {p: PERSPECTIVE_LABELS.get(p,p) for p in by_perspective.keys()}
        theme_tag = generate_theme_tag(keywords_str)
        articles_compact = [{"source":a["source"],"title":a["title"],"link":a["link"]} for a in articles]

        # Salva analisi e ottieni il suo ID reale
        analysis_id = save_analysis(", ".join(keywords), len(articles), narrative_map, convergences,
                      divergences, legal, thread, "", theme_tag, "",
                      json.dumps(articles_compact, ensure_ascii=False))

        # Salva articolo come bozza collegato all'analisi
        slug_base = make_slug(art_titolo or keywords_str)
        conn_art = get_conn(); c_art = conn_art.cursor()
        final_slug = slug_base; counter = 1
        while True:
            c_art.execute("SELECT id FROM articoli WHERE slug=%s", (final_slug,))
            if not c_art.fetchone(): break
            final_slug = f"{slug_base}-{counter}"; counter += 1
        categoria = categorize(keywords_str)
        c_art.execute("""
            INSERT INTO articoli (slug,titolo,categoria,tags,sezione_dati,sezione_analisi,
                sezione_conseguenze,immagine_prompt,immagine_prompt_mid,immagine_url,post_social,
                autore_nome,autore_ruolo,analisi_id,status,created_at,published_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'bozza',%s,NULL)
            RETURNING id
        """, (final_slug, art_titolo, categoria, keywords_str,
              art_dati, art_analisi, art_conseguenze,
              art_prompt_img, art_prompt_mid, "", art_social,
              author['nome'], author['ruolo'], analysis_id,
              datetime.now(timezone.utc).isoformat()))
        art_id = c_art.fetchone()[0]
        conn_art.commit(); conn_art.close()

        # Fase 2: genera hero automaticamente (non-bloccante: se fallisce, bozza resta senza immagine)
        try:
            _hero_flux = _prompt_to_flux_string(art_prompt_img)
            if _hero_flux:
                _hero_b64 = _genera_hero_b64(_hero_flux)
                if _hero_b64:
                    _conn_h = get_conn(); _c_h = _conn_h.cursor()
                    _c_h.execute("UPDATE articoli SET immagine_hero = %s WHERE id = %s", (_hero_b64, art_id))
                    _conn_h.commit(); _c_h.close()
                    print(f"[HERO] generata per articolo {art_id}")
                else:
                    print(f"[HERO] generazione fallita per articolo {art_id}, bozza senza immagine")
        except Exception as _e:
            print(f"[HERO] errore non-bloccante articolo {art_id}: {_e}")

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
            "instagram_script": "",
            "analysis_id": analysis_id,
            "articolo": {
                "id": art_id,
                "slug": final_slug,
                "titolo": art_titolo,
                "sezione_dati": art_dati,
                "sezione_analisi": art_analisi,
                "sezione_conseguenze": art_conseguenze,
                "autore_nome": author['nome'],
                "autore_ruolo": author['ruolo'],
            },
            "has_history": len(previous) > 0,
            "theme_tag": theme_tag,
            "visual_prompts": None,
        }
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        print(f"[ERROR] Job {job_id}: {e}")

# ─────────────────────────────────────────────
# SISTEMA EDITORIALE — helper functions
# ─────────────────────────────────────────────
def make_slug(titolo):
    s = unicodedata.normalize('NFD', titolo)
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s_-]+', '-', s)
    s = s[:80].strip('-')
    ts = datetime.now().strftime('%Y%m%d')
    return f"{ts}-{s}"

def parse_articolo_response(raw, analisi_id, keywords_str, author):
    """Parsa la risposta Claude e restituisce dict con tutti i campi."""
    titolo = ""
    m = re.search(r'TITOLO:\s*(.+)', raw)
    if m:
        titolo = m.group(1).strip().strip('[]').strip('*').strip()

    def extract_sec(text, header):
        m2 = re.search(rf"## {re.escape(header)}\n(.*?)(?=\n## |\n?\*{{0,2}}POST_SOCIAL:|\n?\*{{0,2}}PROMPT_HEADER:|\n?\*{{0,2}}PROMPT_MIDBODY:|\n?\*{{0,2}}PROMPT_IMMAGINE:|\Z)", text, re.DOTALL)
        return m2.group(1).strip() if m2 else ""

    sezione_dati        = extract_sec(raw, "IL DATO CHE CONTA")
    sezione_analisi     = extract_sec(raw, "THEATRUM BELLI — ANALISI")
    sezione_conseguenze = extract_sec(raw, "COSA SIGNIFICA PER TE")

    m_social = re.search(r'\*{0,2}POST_SOCIAL:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_HEADER:|\n?\*{0,2}PROMPT_MIDBODY:|\n?\*{0,2}PROMPT_IMMAGINE:|\Z)', raw, re.DOTALL)
    post_social = m_social.group(1).strip().strip('[]').strip('*').strip() if m_social else ""

    m_prompt = re.search(r'\*{0,2}PROMPT_HEADER:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_MIDBODY:|\n?\*{0,2}PROMPT_IMMAGINE:|\Z)', raw, re.DOTALL)
    if not m_prompt:
        m_prompt = re.search(r'\*{0,2}PROMPT_IMMAGINE:\*{0,2}\s*(.*?)(?=\n?\*{0,2}PROMPT_MIDBODY:|\Z)', raw, re.DOTALL)
    immagine_prompt = clean_image_prompt(m_prompt.group(1)) if m_prompt else ""

    m_prompt_mid = re.search(r'\*{0,2}PROMPT_MIDBODY:\*{0,2}\s*(.*?)\Z', raw, re.DOTALL)
    immagine_prompt_mid = clean_image_prompt(m_prompt_mid.group(1)) if m_prompt_mid else ""

    categoria = categorize(keywords_str)
    slug = make_slug(titolo or keywords_str)

    return {
        "slug": slug,
        "titolo": titolo,
        "categoria": categoria,
        "tags": keywords_str,
        "sezione_dati": sezione_dati,
        "sezione_analisi": sezione_analisi,
        "sezione_conseguenze": sezione_conseguenze,
        "immagine_prompt": immagine_prompt,
        "immagine_prompt_mid": immagine_prompt_mid,
        "immagine_url": "",
        "post_social": post_social,
        "autore_nome": author["nome"],
        "autore_ruolo": author["ruolo"],
        "analisi_id": analisi_id,
        "status": "bozza",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "published_at": None,
    }

# ─────────────────────────────────────────────
# ROUTES PUBBLICHE
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/news")
def api_news():
    category = request.args.get("category","all"); source = request.args.get("source","all")
    limit = int(request.args.get("limit",60)); offset = int(request.args.get("offset",0))
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    query = "SELECT source,title,link,summary,published,category,fetched_at FROM articles WHERE 1=1"
    params = []
    if category != "all": query += " AND category=%s"; params.append(category)
    if source != "all": query += " AND source=%s"; params.append(source)
    query += " ORDER BY fetched_at DESC LIMIT %s OFFSET %s"; params.extend([limit, offset])
    c.execute(query, params); rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/stats")
def api_stats():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles"); total = c.fetchone()[0]
    c.execute("SELECT category,COUNT(*) FROM articles GROUP BY category ORDER BY COUNT(*) DESC")
    by_cat = {r[0]:r[1] for r in c.fetchall()}
    c.execute("SELECT source,COUNT(*) FROM articles GROUP BY source ORDER BY COUNT(*) DESC")
    by_source = {r[0]:r[1] for r in c.fetchall()}
    c.execute("SELECT MAX(fetched_at) FROM articles"); last_update = c.fetchone()[0]
    conn.close()
    return jsonify({"total":total,"by_category":by_cat,"by_source":by_source,"last_update":last_update})

@app.route("/api/refresh", methods=["POST"])
def manual_refresh():
    t = threading.Thread(target=fetch_all); t.daemon=True; t.start()
    return jsonify({"status":"refresh started"})

@app.route("/api/categories")
def api_categories():
    return jsonify(list(CATEGORY_TAGS.keys()))

@app.route("/api/sources")
def api_sources():
    return jsonify(list(FEEDS.keys()))

@app.route("/articoli")
def articoli_lista():
    categoria = request.args.get("categoria", "all")
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    if categoria != "all":
        c.execute("""SELECT id,slug,titolo,categoria,tags,created_at,published_at,immagine_url,autore_nome
                     FROM articoli WHERE status='pubblicato' AND categoria=%s
                     ORDER BY published_at DESC LIMIT 50""", (categoria,))
    else:
        c.execute("""SELECT id,slug,titolo,categoria,tags,created_at,published_at,immagine_url,autore_nome
                     FROM articoli WHERE status='pubblicato'
                     ORDER BY published_at DESC LIMIT 50""")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return render_template("articoli_lista.html", articoli=rows,
                           categoria_filtro=categoria, categorie=list(CATEGORY_TAGS.keys()))

@app.route("/articoli/<slug>")
def articolo_detail(slug):
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM articoli WHERE slug=%s AND status='pubblicato'", (slug,))
    row = c.fetchone(); conn.close()
    if not row: return "Articolo non trovato", 404
    return render_template("articolo_detail.html", articolo=dict(row))

# ─────────────────────────────────────────────
# ROUTES ADMIN
# ─────────────────────────────────────────────
@app.route("/admin")
def admin():
    if not session.get("admin"): return redirect(url_for("admin_login"))
    return render_template("analisi.html")

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True; session.permanent = True
            return redirect(url_for("admin"))
        error = "Password errata."
    return render_template("login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None); return redirect(url_for("index"))

@app.route("/admin/articoli")
def admin_articoli():
    if not session.get("admin"): return redirect(url_for("admin_login"))
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("""SELECT id,slug,titolo,categoria,status,created_at,published_at,autore_nome
                 FROM articoli ORDER BY created_at DESC LIMIT 100""")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return render_template("admin_articoli.html", articoli=rows)

@app.route("/api/admin/analyze", methods=["POST"])
def api_analyze():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    data = request.json
    keywords = [k.strip().lower() for k in data.get("keywords",[]) if k.strip()]
    if not keywords: return jsonify({"error":"Inserisci almeno una keyword"}), 400
    days = int(data.get("days", 7))
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    conditions = " OR ".join(["(LOWER(title) LIKE %s OR LOWER(summary) LIKE %s)" for _ in keywords])
    params = []
    for kw in keywords: params.extend([f"%{kw}%", f"%{kw}%"])
    date_filter = ""
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        date_filter = " AND fetched_at >= %s"; params.append(cutoff)
    c.execute(f"""SELECT source,title,link,summary,published,category,perspective
                  FROM articles WHERE ({conditions}){date_filter}
                  ORDER BY id DESC LIMIT 500""", params)
    all_articles = [dict(r) for r in c.fetchall()]
    kw_conditions = " OR ".join(["LOWER(keywords) LIKE %s" for _ in keywords])
    kw_params = [f"%{kw}%" for kw in keywords]
    c.execute(f"SELECT narrative_map,created_at FROM analyses WHERE {kw_conditions} ORDER BY created_at DESC LIMIT 2", kw_params)
    previous = [dict(r) for r in c.fetchall()]
    conn.close()
    if not all_articles:
        return jsonify({"error":f"Nessun articolo trovato per: {', '.join(keywords)}"}), 404
    articles = select_balanced_articles(all_articles, max_total=25, max_per_perspective=4)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status":"pending"}
    t = threading.Thread(target=run_analysis_job, args=(job_id, keywords, articles, previous))
    t.daemon = True; t.start()
    return jsonify({"job_id":job_id,"article_count":len(all_articles),"selected":len(articles)})

@app.route("/api/admin/job/<job_id>")
def api_job_status(job_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    job = jobs.get(job_id)
    if not job: return jsonify({"error":"Job non trovato"}), 404
    return jsonify(job)

@app.route("/api/admin/analyses")
def api_analyses_history():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT id,keywords,article_count,created_at,theme_tag FROM analyses ORDER BY created_at DESC LIMIT 50")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/admin/analyses/<int:analysis_id>")
def api_analysis_detail(analysis_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM analyses WHERE id=%s", (analysis_id,))
    row = c.fetchone(); conn.close()
    if not row: return jsonify({"error":"Non trovata"}), 404
    return jsonify(dict(row))

@app.route("/api/admin/analyses/<int:analysis_id>", methods=["DELETE"])
def api_analysis_delete(analysis_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM analyses WHERE id=%s", (analysis_id,))
    conn.commit(); conn.close()
    return jsonify({"deleted":analysis_id})

@app.route("/api/admin/articoli/genera/<int:analisi_id>", methods=["POST"])
def api_genera_articolo(analisi_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM analyses WHERE id=%s", (analisi_id,))
    analisi = c.fetchone()
    if not analisi: conn.close(); return jsonify({"error":"Analisi non trovata"}), 404
    analisi = dict(analisi)
    keywords_str = analisi.get('keywords', '')
    conn.close()
    try:
        author = assign_author(keywords_str, analisi.get('theme_tag',''))
        prompt_articolo = f"""Sei un giornalista geopolitico di lungo corso. Scrivi un articolo editoriale completo per Theatrum Belli.

TEMA: {keywords_str}

MAPPA NARRATIVE:
{analisi.get('narrative_map','')}

CONVERGENZE:
{analisi.get('convergences','')}

DIVERGENZE:
{analisi.get('divergences','')}

DIRITTO INTERNAZIONALE:
{analisi.get('legal','')}

FILO NARRATIVO:
{analisi.get('thread','')}

Scrivi UN SOLO titolo editoriale — non descrittivo, non scolastico, colpisci come un headline di guerra fredda, max 12 parole.
Poi l'articolo in 3 sezioni esatte.

TITOLO: [titolo impattante]

## IL DATO CHE CONTA
Fatti nudi incrociati da più fonti. Apri con il dato più anomalo o inatteso.
Cita le testate esplicitamente. Almeno un riferimento storico preciso.
Niente interpretazioni — solo fatti verificati e la tensione tra di essi. 200-280 parole.

## THEATRUM BELLI — ANALISI
Meccanismi nascosti, contraddizioni strutturali, paradossi di potere.
Chi guadagna, chi perde, quali architetture di interesse sono in gioco.
Voce: osservatore freddo che conosce la storia. Niente "dovremmo", niente moralismo. 200-280 parole.

## COSA SIGNIFICA PER TE
Conseguenze concrete per il cittadino europeo/italiano. Catena causale geopolitica→economia domestica.
Bollette, prezzi, lavoro, logistica. Numeri specifici dove possibile.
Chiudi con UN fatto economico secco — titolo azionario, contratto firmato, percentuale.
Poi su riga separata: "— {author['nome']} continua a monitorare."
Tono: referto medico. 120-160 parole.

POST_SOCIAL: [post Instagram 150 parole max, stesso tono, chiudi con "🔗 theatrumbelli.com" — max 3 hashtag]

PROMPT_HEADER: [UNA SOLA RIGA di JSON valido (nessun a-capo dentro le graffe), valori in inglese, per l'immagine hero in cima all'articolo — panoramica d'impatto sul tema. Schema esatto: {{"scene":"...","main_subject":"...","secondary_subjects":["...","..."],"lighting":"...","color_palette":["...","..."],"composition":"cinematic wide shot, 16:9","mood":"...","constraints":["no faces","no readable text","no logos"]}}. main_subject = soggetto concreto dominante della scena. dark aesthetic, 16:9, niente volti, niente testo leggibile, niente loghi.]

PROMPT_MIDBODY: [UNA SOLA RIGA di JSON valido (nessun a-capo dentro le graffe), valori in inglese, stesso schema di PROMPT_HEADER {{"scene":"...","main_subject":"...","secondary_subjects":["...","..."],"lighting":"...","color_palette":["...","..."],"composition":"cinematic wide shot, 16:9","mood":"...","constraints":["no faces","no readable text","no logos"]}}, per l'immagine a metà articolo — dettaglio complementare alla hero (scena specifica o oggetto-simbolo). main_subject = soggetto concreto dominante della scena. dark aesthetic, 16:9, niente volti, niente testo leggibile, niente loghi.]

Rispondi in questo formato esatto — niente altro."""

        raw = call_claude(prompt_articolo, max_tokens=3000)
        art = parse_articolo_response(raw, analisi_id, keywords_str, author)

        conn2 = get_conn(); c2 = conn2.cursor()
        base_slug = art['slug']; final_slug = base_slug; counter = 1
        while True:
            c2.execute("SELECT id FROM articoli WHERE slug=%s", (final_slug,))
            if not c2.fetchone(): break
            final_slug = f"{base_slug}-{counter}"; counter += 1
        art['slug'] = final_slug

        c2.execute("""
            INSERT INTO articoli (slug,titolo,categoria,tags,sezione_dati,sezione_analisi,
                sezione_conseguenze,immagine_prompt,immagine_prompt_mid,immagine_url,post_social,
                autore_nome,autore_ruolo,analisi_id,status,created_at,published_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (art['slug'], art['titolo'], art['categoria'], art['tags'],
              art['sezione_dati'], art['sezione_analisi'], art['sezione_conseguenze'],
              art['immagine_prompt'], art.get('immagine_prompt_mid', ''), art['immagine_url'], art['post_social'],
              art['autore_nome'], art['autore_ruolo'],
              art['analisi_id'], art['status'], art['created_at'], art['published_at']))
        new_id = c2.fetchone()[0]
        conn2.commit(); conn2.close()

        return jsonify({
            "success": True,
            "articolo_id": new_id,
            "slug": final_slug,
            "titolo": art['titolo'],
            "autore_nome": art['autore_nome'],
            "autore_ruolo": art['autore_ruolo'],
        })
    except Exception as e:
        print(f"[ERROR] genera_articolo: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/articoli")
def api_admin_articoli_list():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    analisi_id = request.args.get("analisi_id", type=int)
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    if analisi_id:
        c.execute("""SELECT id,slug,titolo,categoria,status,created_at,published_at,autore_nome,autore_ruolo
                     FROM articoli WHERE analisi_id=%s ORDER BY created_at DESC LIMIT 10""", (analisi_id,))
    else:
        c.execute("""SELECT id,slug,titolo,categoria,status,created_at,published_at,autore_nome
                     FROM articoli ORDER BY created_at DESC LIMIT 100""")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/admin/articoli/<int:articolo_id>")
def api_admin_articolo_detail(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM articoli WHERE id=%s", (articolo_id,))
    row = c.fetchone(); conn.close()
    if not row: return jsonify({"error":"Non trovato"}), 404
    return jsonify(dict(row))

@app.route("/api/admin/articoli/<int:articolo_id>/pubblica", methods=["POST"])
def api_pubblica_articolo(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE articoli SET status='pubblicato', published_at=%s WHERE id=%s",
              (datetime.now(timezone.utc).isoformat(), articolo_id))
    conn.commit(); conn.close()
    return jsonify({"success": True, "status": "pubblicato"})

@app.route("/api/admin/articoli/<int:articolo_id>/archivia", methods=["POST"])
def api_archivia_articolo(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE articoli SET status='archiviato' WHERE id=%s", (articolo_id,))
    conn.commit(); conn.close()
    return jsonify({"success": True, "status": "archiviato"})

@app.route("/api/admin/articoli/<int:articolo_id>", methods=["PUT"])
def api_update_articolo(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    data = request.json
    allowed = ['titolo','sezione_dati','sezione_analisi','sezione_conseguenze',
               'post_social','immagine_prompt','immagine_prompt_mid','immagine_url','categoria','tags',
               'autore_nome','autore_ruolo','immagine_hero','immagine_inline1','immagine_inline2']
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates: return jsonify({"error":"Nessun campo valido"}), 400
    set_clause = ", ".join([f"{k}=%s" for k in updates])
    conn = get_conn(); c = conn.cursor()
    c.execute(f"UPDATE articoli SET {set_clause} WHERE id=%s", list(updates.values()) + [articolo_id])
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/articoli/<int:articolo_id>", methods=["DELETE"])
def api_delete_articolo(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM articoli WHERE id=%s", (articolo_id,))
    conn.commit(); conn.close()
    return jsonify({"deleted": articolo_id})

@app.route("/api/admin/articoli/<int:articolo_id>/genera-script", methods=["POST"])
def api_genera_script(articolo_id):
    """Genera lo script audio on-demand dall'articolo già salvato."""
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM articoli WHERE id=%s", (articolo_id,))
    art = c.fetchone(); conn.close()
    if not art: return jsonify({"error":"Articolo non trovato"}), 404
    art = dict(art)
    autore_nome = art.get('autore_nome', 'Theatrum Belli')
    articolo_testo = f"""TITOLO: {art.get('titolo','')}

IL DATO CHE CONTA:
{art.get('sezione_dati','')}

THEATRUM BELLI — ANALISI:
{art.get('sezione_analisi','')}

COSA SIGNIFICA PER TE:
{art.get('sezione_conseguenze','')}"""

    prompt = f"""Sei un giornalista geopolitico con vent'anni di esperienza.
Hai scritto questo articolo editoriale per Theatrum Belli:

{articolo_testo}

Trasformalo in uno script audio di 2 minuti e 12 secondi (132 secondi) per un reel Instagram.
SOLO in italiano. Versione parlata dello stesso articolo — stessa voce, stessi fatti.

Tre movimenti continui, senza titoli né markdown nel testo:

MOVIMENTO 1 (220-250 parole): Cronaca respirata. Apri col fatto più anomalo. Cita le testate. Un riferimento storico preciso. Ritmo variabile.
MOVIMENTO 2 (80-100 parole): Conseguenze concrete per italiani/europei. Catena causale geopolitica→economia. Numeri. Tono: diagnosi medica.
MOVIMENTO 3 (20-30 parole): Chiusura umana — una sola frase che chiude la prospettiva. NON ripetere dati già detti. Poi su riga separata: "— {autore_nome} continua a monitorare."

Rispondi SOLO con lo script. Nient'altro."""

    try:
        script = call_claude(prompt, max_tokens=2000)
        # Salva lo script nell'analisi collegata se esiste
        if art.get('analisi_id'):
            conn2 = get_conn(); c2 = conn2.cursor()
            c2.execute("UPDATE analyses SET instagram_script=%s WHERE id=%s",
                       (script, art['analisi_id']))
            conn2.commit(); conn2.close()
        return jsonify({"success": True, "script": script})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# ELEVENLABS TTS
# ─────────────────────────────────────────────
@app.route("/api/admin/tts", methods=["POST"])
def api_tts():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    if not ELEVENLABS_API_KEY: return jsonify({"error":"ElevenLabs API key non configurata"}), 500
    if not ELEVENLABS_VOICE_ID: return jsonify({"error":"ElevenLabs Voice ID non configurato"}), 500
    data = request.json
    text = (data.get("text") or "").strip()
    stability = float(data.get("stability", 0.5))
    similarity = float(data.get("similarity_boost", 0.75))
    speed = float(data.get("speed", 1.15))
    if not text: return jsonify({"error":"Testo vuoto"}), 400
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {"xi-api-key":ELEVENLABS_API_KEY,"Content-Type":"application/json","Accept":"audio/mpeg"}
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability":stability,"similarity_boost":similarity,"speed":speed}
        }
        r = req_lib.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            return jsonify({"error":f"ElevenLabs error {r.status_code}: {r.text[:200]}"}), 500
        return Response(r.content, mimetype="audio/mpeg",
                        headers={"Content-Disposition":"attachment; filename=theatrum_belli_script.mp3"})
    except Exception as e:
        print(f"TTS error: {e}"); return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────
# VISUAL PROMPTS (on-demand)
# ─────────────────────────────────────────────
@app.route("/api/admin/visual-prompts", methods=["POST"])
def api_visual_prompts():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    data = request.json
    script_it = (data.get("script_it") or "").strip()
    keywords = (data.get("keywords") or "").strip()
    duration = int(data.get("duration_seconds", 132))
    if not script_it: return jsonify({"error":"Script vuoto"}), 400
    try:
        raw_json = generate_visual_prompts(keywords, script_it, "", duration_seconds=duration)
        parsed = json.loads(raw_json)
        return jsonify(parsed)
    except json.JSONDecodeError:
        return jsonify({"error":"Risposta JSON non valida da Claude", "raw": raw_json[:500]}), 500
    except Exception as e:
        print(f"Visual prompts error: {e}")
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# AUTOMAZIONE: GENERAZIONE ANALISI SCHEDULATA
# ─────────────────────────────────────────────
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")

# Liste-filtro per asse tematico: pescano i titoli pertinenti dal DB.
# Claude poi sceglie il tema caldo DENTRO questo sottoinsieme.
ASSI = {
    "geo": ["war","conflict","military","nato","russia","ukraine","china","taiwan",
            "israel","iran","gaza","missile","troops","border","escalation","strike",
            "defense","weapons","attack","invasion","ceasefire"],
    "politico": ["election","government","summit","treaty","alliance","sanctions",
                 "diplomacy","minister","parliament","vote","coup","protest","negotiation",
                 "agreement","president","policy","resignation","referendum"],
    "economico": ["oil","gas","energy","dollar","brics","trade","tariff","inflation",
                  "market","sanctions","supply chain","semiconductor","commodities",
                  "central bank","recession","export","gdp","debt","currency"],
}

def _prompt_to_flux_string(raw):
    if not raw or not str(raw).strip():
        return None
    raw = str(raw).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        parti = []
        for k in ("scene", "main_subject", "secondary_subjects", "lighting",
                  "color_palette", "composition", "mood", "constraints"):
            v = data.get(k)
            if not v:
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            parti.append(str(v))
        appiattito = ", ".join(parti).strip()
        return appiattito if appiattito else raw
    return raw


STILE_FOTO = ("photorealistic, realistic photography, shot on a full-frame DSLR, 35mm lens, "
              "natural lighting, fine detail, photojournalism, documentary style, "
              "no illustration, no painting, no 3d render, no cartoon")


def _genera_hero_b64(flux_prompt):
    """Genera l'hero via Together FLUX.1-schnell. Ritorna b64 (str) o None.
    Helper puro: niente DB, niente request/session. Usato da endpoint e job."""
    if not flux_prompt:
        return None
    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        print("[HERO] TOGETHER_API_KEY non configurata")
        return None
    prompt_finale = f"{flux_prompt}. {STILE_FOTO}"
    payload = {
        "model": "black-forest-labs/FLUX.1-schnell",
        "prompt": prompt_finale,
        "width": 1344, "height": 768, "steps": 4, "n": 1,
        "response_format": "b64_json",
    }
    try:
        r = req_lib.post(
            "https://api.together.xyz/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
    except Exception as e:
        print(f"[HERO] chiamata Together fallita: {e}")
        return None
    if r.status_code != 200:
        print(f"[HERO] Together status {r.status_code}: {r.text[:200]}")
        return None
    try:
        b64 = r.json()["data"][0]["b64_json"]
    except Exception:
        print("[HERO] risposta senza b64_json atteso")
        return None
    return b64 or None


@app.route("/api/admin/articoli/<int:art_id>/genera-immagine", methods=["POST"])
def api_genera_immagine(art_id):
    if not session.get("admin"):
        return jsonify({"error": "Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT immagine_prompt FROM articoli WHERE id = %s", (art_id,))
    row = c.fetchone()
    if not row:
        c.close(); conn.close()
        return jsonify({"error": "Articolo non trovato"}), 404
    flux_prompt = _prompt_to_flux_string(row[0])
    if not flux_prompt:
        c.close(); conn.close()
        return jsonify({"error": "Nessun prompt immagine presente per questo articolo"}), 400
    b64 = _genera_hero_b64(flux_prompt)
    if not b64:
        c.close(); conn.close()
        return jsonify({"error": "Generazione immagine fallita (vedi log server)"}), 502
    c.execute("UPDATE articoli SET immagine_hero = %s WHERE id = %s", (b64, art_id))
    conn.commit()
    c.close(); conn.close()
    return jsonify({"success": True, "bytes_b64": len(b64)}), 200

def estrai_tema_caldo(asse, titoli):
    """Claude sceglie il tema piu' rilevante del giorno tra i titoli filtrati per asse."""
    if not titoli:
        return None
    titoli_txt = "\n".join(f"- {t}" for t in titoli[:60])
    già_fatti = ""
    prompt = (f"Sei un caporedattore di intelligence geopolitica. Dai seguenti titoli di oggi "
              f"sull'asse '{asse}', identifica IL SINGOLO tema piu' rilevante e caldo per un'analisi.\n\n"
              f"TITOLI:\n{titoli_txt}\n\n"
              f"Rispondi SOLO con 2-4 parole chiave separate da virgola che catturano il tema "
              f"(es. 'iran, nucleare, negoziati' oppure 'taiwan, cina, semiconduttori'). "
              f"Nessuna spiegazione, solo le keyword.")
    out = call_claude(prompt, max_tokens=50)
    kws = [k.strip().lower() for k in out.split(",") if k.strip() and len(k.strip()) < 30]
    return kws[:4] if kws else None

@app.route("/api/cron/genera")
def api_cron_genera():
    # Autenticazione via token (non sessione: lo chiama una macchina)
    token = request.headers.get("X-Cron-Token", "") or request.args.get("token", "")
    if not CRON_TOKEN or token != CRON_TOKEN:
        return jsonify({"error": "Token non valido"}), 403
    asse = request.args.get("asse", "").strip().lower()
    if asse not in ASSI:
        return jsonify({"error": f"asse sconosciuto: {asse}", "disponibili": list(ASSI.keys())}), 400

    filtro = ASSI[asse]
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)

    # 1) Pesca titoli pertinenti all'asse nelle ultime 24h, con fallback a 48h se pochi
    def pesca_titoli(ore):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ore)).isoformat()
        cond = " OR ".join(["LOWER(title) LIKE %s" for _ in filtro])
        params = [f"%{k}%" for k in filtro] + [cutoff]
        c.execute(f"SELECT title FROM articles WHERE ({cond}) AND fetched_at >= %s ORDER BY id DESC LIMIT 80", params)
        return [r["title"] for r in c.fetchall()]

    titoli = pesca_titoli(24)
    finestra = 24
    if len(titoli) < 10:
        titoli = pesca_titoli(48); finestra = 48

    if len(titoli) < 5:
        conn.close()
        return jsonify({"error": "Troppo pochi titoli per l'asse", "asse": asse, "titoli": len(titoli)}), 200

    # 2) Claude sceglie il tema caldo
    keywords = estrai_tema_caldo(asse, titoli)
    if not keywords:
        conn.close()
        return jsonify({"error": "Estrazione tema fallita", "asse": asse}), 200

    # 3) Anti-doppione: evita temi già coperti oggi
    oggi = datetime.now(timezone.utc).date().isoformat()
    c.execute("SELECT keywords FROM analyses WHERE created_at >= %s", (oggi,))
    temi_oggi = " ".join(r["keywords"].lower() for r in c.fetchall() if r["keywords"])
    sovrapposte = [k for k in keywords if k in temi_oggi]
    if len(sovrapposte) >= len(keywords):
        conn.close()
        return jsonify({"skip": True, "motivo": "tema già coperto oggi", "asse": asse, "keywords": keywords}), 200

    # 4) Recupera articoli pertinenti al tema (stessa logica di api_analyze)
    cond2 = " OR ".join(["(LOWER(title) LIKE %s OR LOWER(summary) LIKE %s)" for _ in keywords])
    params2 = []
    for kw in keywords: params2.extend([f"%{kw}%", f"%{kw}%"])
    cutoff7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    params2.append(cutoff7)
    c.execute(f"""SELECT source,title,link,summary,published,category,perspective
                  FROM articles WHERE ({cond2}) AND fetched_at >= %s
                  ORDER BY id DESC LIMIT 500""", params2)
    all_articles = [dict(r) for r in c.fetchall()]
    kw_cond = " OR ".join(["LOWER(keywords) LIKE %s" for _ in keywords])
    c.execute(f"SELECT narrative_map,created_at FROM analyses WHERE {kw_cond} ORDER BY created_at DESC LIMIT 2",
              [f"%{kw}%" for kw in keywords])
    previous = [dict(r) for r in c.fetchall()]
    conn.close()

    if len(all_articles) < 3:
        return jsonify({"skip": True, "motivo": "pochi articoli sul tema", "asse": asse,
                        "keywords": keywords, "trovati": len(all_articles)}), 200

    # 5) Lancia la pipeline (salva come BOZZA, esattamente come la generazione manuale)
    articles = select_balanced_articles(all_articles, max_total=25, max_per_perspective=4)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}
    t = threading.Thread(target=run_analysis_job, args=(job_id, keywords, articles, previous))
    t.daemon = True; t.start()
    return jsonify({"ok": True, "asse": asse, "keywords": keywords, "finestra_ore": finestra,
                    "articoli": len(all_articles), "selezionati": len(articles), "job_id": job_id}), 200

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

