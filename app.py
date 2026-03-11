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
            source TEXT,
            title TEXT,
            link TEXT UNIQUE,
            summary TEXT,
            published TEXT,
            category TEXT,
            perspective TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS perspective TEXT DEFAULT 'other'")
    c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id SERIAL PRIMARY KEY,
            keywords TEXT,
            article_count INTEGER,
            narrative_map TEXT,
            convergences TEXT,
            divergences TEXT,
            legal TEXT,
            thread TEXT,
            instagram_script TEXT,
            created_at TEXT
        )
    """)
    for col in ["narrative_map","convergences","divergences","thread","instagram_script","legal"]:
        c.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {col} TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS theme_tag TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS visual_prompts TEXT")
    c.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS articles_json TEXT")

    # ── NUOVA tabella articoli editoriali ──
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
            analisi_id INTEGER,
            status TEXT DEFAULT 'bozza',
            created_at TEXT,
            published_at TEXT
        )
    """)

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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (link) DO NOTHING
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
    """, (keywords, article_count, narrative_map, convergences, divergences, legal,
          thread, instagram_script, datetime.now(timezone.utc).isoformat(),
          theme_tag, visual_prompts, articles_json))
    conn.commit(); conn.close()

# ─────────────────────────────────────────────
# FONTI RSS
# ─────────────────────────────────────────────
FEEDS = {
    "ANSA Mondo":("https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml","italian_mainstream"),
    "Repubblica Esteri":("https://www.repubblica.it/rss/esteri/rss2.0.xml","italian_mainstream"),
    "Corriere Esteri":("https://xml2.corriereobjects.it/rss/esteri.xml","italian_mainstream"),
    "Il Sole 24 Ore Mondo":("https://www.ilsole24ore.com/rss/mondo.xml","italian_mainstream"),
    "Il Fatto Quotidiano":("https://www.ilfattoquotidiano.it/category/esteri/feed/","italian_mainstream"),
    "Limes":("https://www.limesonline.com/feed","think_tank"),
    "BBC World":("http://feeds.bbci.co.uk/news/world/rss.xml","western_mainstream"),
    "Reuters World":("https://feeds.reuters.com/reuters/worldNews","western_mainstream"),
    "The Guardian World":("https://www.theguardian.com/world/rss","western_mainstream"),
    "AP News":("https://feeds.apnews.com/rss/APNewsTop25Stories","western_mainstream"),
    "DW World":("https://rss.dw.com/rdf/rss-en-world","western_mainstream"),
    "France24 EN":("https://www.france24.com/en/rss","western_mainstream"),
    "Euronews EN":("https://www.euronews.com/rss","western_mainstream"),
    "Jerusalem Post":("https://www.jpost.com/rss/rssfeedsfrontpage.aspx","pro_israel"),
    "Times of Israel":("https://www.timesofisrael.com/feed/","pro_israel"),
    "Haaretz EN":("https://www.haaretz.com/cmlink/1.628765","pro_israel"),
    "i24 News":("https://www.i24news.tv/en/rss","pro_israel"),
    "Al Jazeera English":("https://www.aljazeera.com/xml/rss/all.xml","arab_media"),
    "Middle East Eye":("https://www.middleeasteye.net/rss","arab_media"),
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
    "ISW":("https://www.understandingwar.org/rss.xml","think_tank"),
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
    "chinese_state":"Media Cinesi/Asiatici","think_tank":"Think Tank & Analisi","other":"Altro",
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

def categorize(text):
    t = text.lower()
    for cat, keys in CATEGORY_TAGS.items():
        if cat == "⚪ Altro": continue
        for k in keys:
            if k in t: return cat
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
      "prompt_en": "[60-100 word professional prompt — MUST include: specific subject + exact location/geography related to the theme + precise lighting description + lens/camera specification + color palette + mood + 2:3 vertical composition — follow the quality and length of the reference examples above]"
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
    perspectives_missing = [PERSPECTIVE_LABELS.get(p,p) for p in PERSPECTIVE_LABELS.keys()
                            if p not in by_perspective]
    keywords_str = ", ".join(keywords_list)
    history_context = ""
    if previous_analyses:
        history_context = "\n\nANALISI PRECEDENTI SULLO STESSO TEMA:\n"
        for pa in previous_analyses[:2]:
            history_context += f"\n[{pa['created_at'][:10]}]\n{pa['narrative_map'][:400]}...\n"

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

    prompt_script = f"""Sei un giornalista geopolitico con vent'anni di esperienza.
Scrivi uno script audio di 2 minuti e 12 secondi (132 secondi) per un reel Instagram di intelligence geopolitica.
Lo script è SOLO IN ITALIANO.

TEMA: {keywords_str}

ANALISI COMPLETA:
{raw_analysis}

TRE MOVIMENTI OBBLIGATORI (niente titoli nel testo, niente markdown):

MOVIMENTO 1 — CRONACA RESPIRATA (220-250 parole)
Apri con il fatto più anomalo. Ogni elemento ha spazio per atterrare. Nomina le testate esplicitamente.
Almeno un riferimento storico preciso. Usa il Filo Narrativo per la profondità storica. Ritmo variabile.

MOVIMENTO 2 — ANALISI CRUDA (80-100 parole)
Conseguenze concrete per cittadini europei e italiani. Catena causale: geopolitica → economia domestica.
Numeri specifici. Tono: diagnosi medica. Almeno due geografie diverse.

MOVIMENTO 3 — IL DISAGIO (30-50 parole)
UN solo fatto economico concreto — titolo azionario, contratto, numero preciso.
Freddo come un referto. Zero domande retoriche. Zero moralismo.
Esempio tono: "I contratti per quelle bombe sono già stati firmati. Lockheed Martin ha chiuso la settimana in rialzo del 7%."

Rispondi SOLO con lo script in italiano. Niente altro."""

    raw_script = call_claude(prompt_script)
    visual_prompts_json = generate_visual_prompts(keywords_str, raw_script, raw_analysis, duration_seconds=132)

    return (raw_analysis + "\n\n## 6. SCRIPT (2:12, IT)\n" + raw_script), visual_prompts_json

def run_analysis_job(job_id, keywords, articles, previous):
    jobs[job_id]["status"] = "running"
    try:
        raw, visual_prompts = generate_analysis(keywords, articles, previous)

        def extract_section(text, title):
            m = re.search(rf"## {re.escape(title)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
            return m.group(1).strip() if m else ""

        def extract_fuzzy(text, keyword):
            m = re.search(rf"## [^\n]*{re.escape(keyword)}[^\n]*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
            return m.group(1).strip() if m else ""

        narrative_map = extract_section(raw, "1. MAPPA DELLE NARRATIVE")
        convergences  = extract_section(raw, "2. CONVERGENZE")
        divergences   = extract_section(raw, "3. DIVERGENZE E CONFLITTI NARRATIVI")
        legal         = extract_section(raw, "4. PROSPETTIVA DEL DIRITTO INTERNAZIONALE")
        thread        = extract_section(raw, "5. FILO NARRATIVO")
        instagram     = extract_fuzzy(raw, "SCRIPT")

        by_perspective = defaultdict(list)
        for a in articles:
            by_perspective[a.get('perspective','other')].append(a)
        perspectives_used = {p: PERSPECTIVE_LABELS.get(p,p) for p in by_perspective.keys()}
        keywords_str = ", ".join(keywords)
        theme_tag = generate_theme_tag(keywords_str)
        articles_compact = [{"source":a["source"],"title":a["title"],"link":a["link"]}
                            for a in articles]

        save_analysis(", ".join(keywords), len(articles), narrative_map, convergences,
                      divergences, legal, thread, instagram, theme_tag, visual_prompts,
                      json.dumps(articles_compact, ensure_ascii=False))

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
            "visual_prompts": visual_prompts,
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

def generate_articolo_from_analisi(analisi, keywords_str):
    narrative_map     = analisi.get('narrative_map', '')
    convergences      = analisi.get('convergences', '')
    divergences       = analisi.get('divergences', '')
    legal             = analisi.get('legal', '')
    thread            = analisi.get('thread', '')
    instagram_script  = analisi.get('instagram_script', '')

    prompt = f"""Devi scrivere un articolo editoriale completo per Theatrum Belli sul tema: {keywords_str}

Hai a disposizione questa analisi intelligence:

MAPPA NARRATIVE:
{narrative_map}

CONVERGENZE:
{convergences}

DIVERGENZE:
{divergences}

DIRITTO INTERNAZIONALE:
{legal}

FILO NARRATIVO:
{thread}

SCRIPT AUDIO (per tono e ritmo):
{instagram_script[:800] if instagram_script else ''}

Scrivi UN SOLO titolo editoriale (NON descrittivo, NON scolastico — deve colpire come un headline di guerra fredda, max 12 parole), poi l'articolo in 3 sezioni ESATTE con questi titoli precisi.

TITOLO: [titolo impattante]

## IL DATO CHE CONTA
Fatti nudi incrociati da più fonti. Apri con il dato più anomalo o inatteso. Cita le testate esplicitamente. Almeno un riferimento storico preciso. Niente interpretazioni — solo fatti verificati e la tensione tra di essi. 200-280 parole.

## THEATRUM BELLI — ANALISI
Meccanismi nascosti, contraddizioni strutturali, paradossi di potere. Chi guadagna, chi perde, quali architetture di interesse sono in gioco. Voce: osservatore freddo che conosce la storia. Niente "dovremmo", niente moralismo. 200-280 parole.

## COSA SIGNIFICA PER TE
Conseguenze concrete per il cittadino europeo/italiano. Catena causale geopolitica→economia domestica. Bollette, prezzi, lavoro, logistica. Numeri specifici dove possibile. Chiudi con UN fatto economico secco — titolo azionario, contratto firmato, percentuale. Tono: referto medico. 120-160 parole.

POST_SOCIAL: [post Instagram 150 parole max, stesso tono, chiudi con "🔗 theatrumbelli.com" — al massimo 3 hashtag specifici]
PROMPT_IMMAGINE: [prompt in inglese per Flux AI, 60-80 parole, dark aesthetic, no faces, no readable text, 16:9, photojournalism style]

Rispondi in questo formato esatto — niente altro."""

    return call_claude(prompt, max_tokens=3000)

def parse_articolo_response(raw, analisi_id, keywords_str):
    """Parsa la risposta Claude e restituisce dict con tutti i campi."""
    # Estrai titolo
    titolo = ""
    m = re.search(r'TITOLO:\s*(.+)', raw)
    if m:
        titolo = m.group(1).strip().strip('[]')

    def extract_sec(text, header):
        m2 = re.search(rf"## {re.escape(header)}\n(.*?)(?=\n## |\nPOST_SOCIAL:|\nPROMPT_IMMAGINE:|\Z)",
                       text, re.DOTALL)
        return m2.group(1).strip() if m2 else ""

    sezione_dati        = extract_sec(raw, "IL DATO CHE CONTA")
    sezione_analisi     = extract_sec(raw, "THEATRUM BELLI — ANALISI")
    sezione_conseguenze = extract_sec(raw, "COSA SIGNIFICA PER TE")

    m_social = re.search(r'POST_SOCIAL:\s*(.*?)(?=\nPROMPT_IMMAGINE:|\Z)', raw, re.DOTALL)
    post_social = m_social.group(1).strip().strip('[]') if m_social else ""

    m_prompt = re.search(r'PROMPT_IMMAGINE:\s*(.*?)$', raw, re.DOTALL)
    immagine_prompt = m_prompt.group(1).strip().strip('[]') if m_prompt else ""

    # Categoria automatica dal keywords
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
        "immagine_url": "",
        "post_social": post_social,
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

# ── Pagine pubbliche articoli ──────────────────────────────────────────
@app.route("/articoli")
def articoli_lista():
    categoria = request.args.get("categoria", "all")
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    if categoria != "all":
        c.execute("""SELECT id,slug,titolo,categoria,tags,created_at,published_at,immagine_url
                     FROM articoli WHERE status='pubblicato' AND categoria=%s
                     ORDER BY published_at DESC LIMIT 50""", (categoria,))
    else:
        c.execute("""SELECT id,slug,titolo,categoria,tags,created_at,published_at,immagine_url
                     FROM articoli WHERE status='pubblicato'
                     ORDER BY published_at DESC LIMIT 50""")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return render_template("articoli_lista.html", articoli=rows, categoria_filtro=categoria,
                           categorie=list(CATEGORY_TAGS.keys()))

@app.route("/articoli/<slug>")
def articolo_detail(slug):
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM articoli WHERE slug=%s AND status='pubblicato'", (slug,))
    row = c.fetchone(); conn.close()
    if not row:
        return "Articolo non trovato", 404
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
    c.execute("""SELECT id,slug,titolo,categoria,status,created_at,published_at
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
    c.execute(f"SELECT narrative_map,created_at FROM analyses WHERE {kw_conditions} ORDER BY created_at DESC LIMIT 2",
              kw_params)
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

# ── Genera articolo da analisi ─────────────────────────────────────────
@app.route("/api/admin/articoli/genera/<int:analisi_id>", methods=["POST"])
def api_genera_articolo(analisi_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM analyses WHERE id=%s", (analisi_id,))
    analisi = c.fetchone()
    if not analisi:
        conn.close()
        return jsonify({"error":"Analisi non trovata"}), 404
    analisi = dict(analisi)
    keywords_str = analisi.get('keywords', '')
    conn.close()

    try:
        raw = generate_articolo_from_analisi(analisi, keywords_str)
        art = parse_articolo_response(raw, analisi_id, keywords_str)

        # Assicura slug univoco
        conn2 = get_conn(); c2 = conn2.cursor()
        base_slug = art['slug']
        final_slug = base_slug
        counter = 1
        while True:
            c2.execute("SELECT id FROM articoli WHERE slug=%s", (final_slug,))
            if not c2.fetchone(): break
            final_slug = f"{base_slug}-{counter}"; counter += 1
        art['slug'] = final_slug

        c2.execute("""
            INSERT INTO articoli (slug,titolo,categoria,tags,sezione_dati,sezione_analisi,
                                  sezione_conseguenze,immagine_prompt,immagine_url,post_social,
                                  analisi_id,status,created_at,published_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (art['slug'], art['titolo'], art['categoria'], art['tags'],
              art['sezione_dati'], art['sezione_analisi'], art['sezione_conseguenze'],
              art['immagine_prompt'], art['immagine_url'], art['post_social'],
              art['analisi_id'], art['status'], art['created_at'], art['published_at']))
        new_id = c2.fetchone()[0]
        conn2.commit(); conn2.close()

        return jsonify({"success": True, "articolo_id": new_id, "slug": final_slug, "articolo": art})
    except Exception as e:
        print(f"[ERROR] genera_articolo: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/articoli")
def api_admin_articoli_list():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("""SELECT id,slug,titolo,categoria,status,created_at,published_at
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
    c.execute("""UPDATE articoli SET status='pubblicato', published_at=%s WHERE id=%s""",
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
               'post_social','immagine_prompt','immagine_url','categoria','tags']
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates: return jsonify({"error":"Nessun campo valido"}), 400
    set_clause = ", ".join([f"{k}=%s" for k in updates])
    conn = get_conn(); c = conn.cursor()
    c.execute(f"UPDATE articoli SET {set_clause} WHERE id=%s",
              list(updates.values()) + [articolo_id])
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/articoli/<int:articolo_id>", methods=["DELETE"])
def api_delete_articolo(articolo_id):
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM articoli WHERE id=%s", (articolo_id,))
    conn.commit(); conn.close()
    return jsonify({"deleted": articolo_id})

# ─────────────────────────────────────────────
# ELEVENLABS TTS
# ─────────────────────────────────────────────
@app.route("/api/admin/tts", methods=["POST"])
def api_tts():
    if not session.get("admin"): return jsonify({"error":"Non autorizzato"}), 403
    if not ELEVENLABS_API_KEY: return jsonify({"error":"ElevenLabs API key non configurata"}), 500
    if not ELEVENLABS_VOICE_ID: return jsonify({"error":"ElevenLabs Voice ID non configurato"}), 500
    data = request.json
    text       = (data.get("text") or "").strip()
    stability  = float(data.get("stability", 0.5))
    similarity = float(data.get("similarity_boost", 0.75))
    speed      = float(data.get("speed", 1.15))
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
    keywords  = (data.get("keywords") or "").strip()
    duration  = int(data.get("duration_seconds", 132))
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
