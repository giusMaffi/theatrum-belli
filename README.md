# THEATRUM BELLI — Intelligence Feed

Aggregatore RSS specializzato in geopolitica e conflitti armati.
Dashboard Flask con aggiornamento automatico orario.

## Fonti incluse

**Mainstream:** ANSA, Repubblica, Corriere, BBC, Reuters, Al Jazeera, The Guardian  
**Geopolitica:** ISW, Foreign Affairs, The Diplomat, Defense One, War on the Rocks, Limes  
**Alternative:** The Cradle, MintPress News, Scenari Economici, Il Fatto Quotidiano  

## Features

- Fetch RSS automatico ogni ora
- Filtro keyword per guerra/geopolitica
- Categorizzazione automatica (Russia-Ucraina, Medio Oriente, Cina, Africa, NATO)
- Dashboard dark con sidebar statistiche
- Filtri per categoria e fonte
- Paginazione (load more)
- Refresh manuale

## Run locale

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Deploy su Render

1. Push su GitHub
2. Vai su render.com → New Web Service
3. Collega il repo
4. Render rileva automaticamente il `render.yaml`
5. Deploy

> **Nota:** Su Render free tier il DB SQLite è volatile (si resetta a ogni deploy).
> Per persistenza usa Render Disk ($7/mo) oppure migra a PostgreSQL.

## Aggiungere fonti

Modifica il dizionario `FEEDS` in `app.py`:

```python
FEEDS = {
    "Nome Fonte": "https://url-del-feed.xml",
    ...
}
```

## Aggiungere keyword

Modifica `KEYWORDS_IT` o `KEYWORDS_EN` in `app.py`.
