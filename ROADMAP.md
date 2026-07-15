# THEATRUM BELLI - Roadmap Automazione Editoriale

Versione 1.1 - 7 giugno 2026
Documento operativo condiviso tra Beppe, Claude (chat) e Claude Code (Claudio).
Questo file e' la memoria condivisa: Claude Code non vede le chat, parte da qui.

## Principio guida
Il sistema produce tutto in automatico (analisi, articolo, immagini, bozza social) e impila BOZZE pronte.
La pubblicazione richiede SEMPRE un click umano - sia sul sito sia sui social.
Niente auto-pubblicazione: su un brand di intelligence, il click finale protegge la credibilita'.

## Come lavora Claude Code in questo progetto
- Claude Code interviene SOLO su blocchi di istruzioni gia' discussi tra Beppe e Claude e poi verificati.
- Claude Code NON prende decisioni di architettura o prodotto da solo: esegue blocchi concordati.
- Ogni blocco deve essere autosufficiente (Claude Code non ha il contesto delle chat).
- Operazioni sul DB (ALTER, migrazioni, modifiche dati): STOP e conferma esplicita di Beppe.
- Deploy (push): codice reversibile, validare sintassi prima; Beppe collauda.
- Verificare i fatti sul codice, mai a memoria.

## Stato di partenza (verificato sul codice live, 7/6/2026)
- OK Motore generazione analisi+articolo funzionante (pannello manuale).
- OK Prompt immagini generati in formato JSON (fix 6/6, collaudato dal vivo).
- OK Contrasto WCAG sistemato su tutte le pagine pubbliche + admin.
- OK Test parsing permanente in repo.
- OK [7/6] Generazione immagine hero implementata e in produzione (FLUX schnell, base64 in immagine_hero, formato 16:9, suffisso fotorealistico). Vedi Fase 1.
- ESISTE Cron di generazione: endpoint /api/cron/genera, funzione estrai_tema_caldo, 3 assi (geo/politico/economico), CRON_TOKEN su Render. Da MODIFICARE (orari/frequenza), non da costruire.
- NO Nessuna pipeline social.
- NO Immagini inline (1 e 2) non implementate per scelta; richiedono cleanup storage.

## FASE 1 - Generazione immagini - COMPLETATA (7/6/2026)
Obiettivo raggiunto: dal prompt salvato si genera l'immagine hero e la si mostra nell'articolo.
- Modello: FLUX.1-schnell via Together, response_format b64_json. Contratto verificato dal vivo (200, b64 in data[0].b64_json, JPEG, inference ~0.4s).
- Storage: base64 nudo nel campo TEXT immagine_hero esistente (decisione a1: zero modifiche al DB, niente rotta /img).
- Display: il template antepone data:image/jpeg;base64 quando il valore non e' un URL http. Banner orizzontale 16:9 (generazione 1344x768, CSS aspect-ratio 16/9, nessun crop).
- Stile: suffisso fotorealistico (STILE_FOTO) appeso al prompt prima dell'invio a FLUX. Il prompt salvato resta invariato.
- Prompt: helper _prompt_to_flux_string gestisce sia JSON (post-fix) sia prosa (pre-fix).
- Endpoint: POST /api/admin/articoli/<id>/genera-immagine, protetto da sessione admin, gestione errori difensiva (status != 200 -> errore leggibile, nessuna scrittura DB).
- UI: bottone Genera immagine hero nel modal; preview via heroSrc(); campo edit-hero per URL manuali, un campo vuoto non sovrascrive un hero gia' salvato.
- Prerequisito: TOGETHER_API_KEY nelle env var di Render (presente e verificata).
- Inline (1 e 2): rinviate per scelta; se attivate, prevedere cleanup storage.

## FASE 2 - Cron di generazione (GIA' ESISTENTE nel codice, da modificare)
ATTENZIONE: il cron NON e' da costruire - esiste gia'. Verificato sul codice live. La Fase 2 e' una MODIFICA.
- Endpoint esistente: /api/cron/genera (NON /genera-automatico), protetto da CRON_TOKEN gia' nelle env var di Render.
- Funzioni esistenti: api_cron_genera() + estrai_tema_caldo(asse, titoli). Logica hot-topic gia' implementata.
- Assi esistenti: dizionario ASSI con 3 assi (geo, politico, economico). Si chiama /api/cron/genera?asse=geo (o politico/economico).
- Modifica da fare: portare a 3/giorno (una per asse) agli orari 07:00 / 13:00 / 17:00. Orari e frequenza vivono su cron-job.org (esterno), NON nel codice - li configura Beppe.
- VERIFICATO: il cron lancia run_analysis_job (riga 1351), dove vive l'aggancio hero non-bloccante. Le bozze del cron nascono gia' con l'immagine. Nessun secondo aggancio necessario. Salvataggio in bozza confermato.
- Nota infra: il dict jobs in memoria regge solo con 1 worker (render.yaml/Procfile allineati a 1). Riverificare prima di scalare.

## FASE 3 - Pipeline social
Obiettivo: dopo la pubblicazione sul sito, preparare la bozza del post social.
- Bozza post via Canva.
- Pubblicazione social = click umano (stesso principio della Fase 1).
- Da progettare dopo che Fase 1+2 girano stabili per qualche giorno.

## Decisioni prese (log)
- 6/6: niente auto-pubblicazione, solo click finale (sito e social).
- 6/6: 3 articoli/giorno alle 07:00, 13:00, 17:00.
- 6/6: storage immagini = base64 in campo TEXT (a1), niente ALTER, niente rotta /img.
- 6/6: solo immagine hero per articolo all'avvio; inline rinviate.
- 6/6: VERIFICATO che il cron esiste gia' (/api/cron/genera, estrai_tema_caldo, 3 assi, CRON_TOKEN). Fase 2 = modifica, non costruzione.
- 7/6: Fase 1 completata e in produzione (hero via FLUX.1-schnell, base64 nudo in immagine_hero, display pubblico con prefisso data-URI).
- 7/6: formato hero orizzontale 16:9 (generazione 1344x768, CSS aspect-ratio 16/9).
- 7/6: suffisso fotorealistico (STILE_FOTO) appeso al prompt prima dell'invio a FLUX. Da riverificare su soggetti umani.
- 7/6: TOGETHER_API_KEY presente e verificata nelle env var di Render.
- 8/6: hero automatico agganciato a run_analysis_job (helper _genera_hero_b64, non-bloccante). Copre sia cron sia manuale: verificato che api_cron_genera lancia run_analysis_job.
