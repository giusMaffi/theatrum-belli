# THEATRUM BELLI - Roadmap Automazione Editoriale

Versione 1.0 - 6 giugno 2026
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

## Stato di partenza (verificato sul codice live, 6/6/2026)
- OK Motore generazione analisi+articolo funzionante (pannello manuale).
- OK Prompt immagini generati in formato JSON (fix 6/6, collaudato dal vivo).
- OK Contrasto WCAG sistemato su tutte le pagine pubbliche + admin.
- OK Test parsing permanente in repo.
- NO Nessuna immagine generata o salvata (campi immagine_* vuoti, Together inesistente).
- NO Nessun cron di generazione automatica (lo scheduler fa solo fetch_all dei feed).
- NO Nessuna pipeline social.

## FASE 1 - Generazione immagini (prossimo passo)
Obiettivo: dal prompt JSON gia' salvato, generare l'immagine hero e mostrarla nell'articolo.
- Modello: FLUX.1-schnell via Together, response_format b64_json.
- Storage: base64 nel campo TEXT immagine_hero esistente (decisione a1: zero modifiche al DB, niente rotta /img). Sostenibile oltre 18 mesi col solo hero.
- Display: template mostra img src data:image/jpeg;base64.
- UI: bottone Genera immagine nel pannello modifica articolo; mostra anche i prompt JSON (oggi invisibili).
- Prerequisito Beppe: TOGETHER_API_KEY nelle env var di Render.
- Solo hero per ora; le inline (immagine 1 e 2) si valutano dopo.

## FASE 2 - Cron di generazione (in bozza)
Obiettivo: generare automaticamente 3 articoli/giorno, in bozza, senza intervento.
- Orari: 07:00, 13:00, 17:00.
- Endpoint protetto /api/cron/genera-automatico (token segreto in header).
- Riusa il motore del pannello: nessun secondo modello, stesso codice.
- Logica hot-topic: Claude sceglie il tema caldo dai titoli del giorno gia' in tabella articles.
- Trigger esterno: cron-job.org agli orari fissi.
- Produce: analisi + articolo + immagine (Fase 1), salva in BOZZA.
- Nota infra: il dict jobs in memoria regge solo con 1 worker. Verificare render.yaml (2) vs Procfile (1) prima di scalare.

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
