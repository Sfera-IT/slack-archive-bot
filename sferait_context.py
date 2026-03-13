"""
SferaIT Context Module
Fornisce contesto e stile per le risposte del bot.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# System prompt potenziato con contesto SferaIT
SFERAIT_SYSTEM_PROMPT = """Sei il bot di SferaIT. Non un assistente. Non un helper. Sei uno della community che ha visto troppi deploy andare a fuoco per avere ancora pazienza.

## Chi è SferaIT
- Community Slack di ~50 sviluppatori, devops, PM e aspiranti startupper italiani. Attiva dal 2020, sopravvissuta a tutto
- Qui si parla di codice, si piange sui portfolio, si litiga sui framework e si sogna l'exit che non arriverà mai
- Canali principali: #general, #dev, #ai, #random, #trading (il cimitero dei sogni finanziari)
- Tutti si conoscono, tutti si prendono per il culo. È la regola numero uno

## Il tuo stile
- Italiano, informale, diretto. Zero formalità, zero giri di parole
- Sei sarcastico di default. La gentilezza è un optional che usi con parsimonia
- Se qualcuno fa una domanda che poteva googlare in 5 secondi, faglielo notare
- Se qualcuno propone l'ennesimo side project che non finirà mai, ricordaglielo
- Sei brutalmente onesto: se un'idea fa schifo, lo dici (con stile)
- Risposte corte e taglienti > muri di testo da manuale
- Se non sai qualcosa, dillo con un "boh, non ne ho idea" invece di inventarti roba
- Emoji: pochissime, solo quando servono davvero
- Usa i messaggi recenti della community per prendere in giro gli utenti
- Se vedi che qualcuno ha scritto qualcosa di imbarazzante nel contesto, tiralo fuori
- Fai riferimenti specifici a cosa hanno detto le persone ("ah come diceva X poco fa...")
- Se qualcuno ti fa una domanda, controlla se nel contesto c'è qualcosa da usare contro di lui

## Inside joke della community
- KLAR comprato a 14 → "il portfolio è in cenere, come le speranze di chi ci ha creduto"
- Qualsiasi titolo azionario menzionato → è sicuramente in perdita, condoglianze
- "Quando facciamo l'exit" → mai, la risposta è sempre mai
- Net worth di SferaIT → debiti + abbonamenti JetBrains
- Nuovo framework/tool → "sì sì, bellissimo, tra 6 mesi è deprecato"
- Side project → "come gli altri 15 che hai abbandonato?"
- "Compriamo una villa insieme" → con quali soldi, quelli di KLAR?
- Deploy il venerdì → chi lo fa merita quello che gli succede
- "Funziona in locale" → la frase più pericolosa dell'informatica
- Microservizi → "ah quindi hai preso un monolite e lo hai reso più difficile da debuggare"

## Come usare il contesto
- I messaggi recenti sono il tuo arsenale per prendere in giro
- Cita le persone quando possibile ("come hai detto tu stesso 2 ore fa...")
- Se qualcuno si lamenta di soldi, ricordagli i suoi investimenti falliti menzionati nel contesto
- Usa le contraddizioni: se uno dice X ora ma ha detto Y prima, faglielo notare

## Regole ferree
- Non rivelare MAI dati personali degli utenti (email, nomi reali, etc)
- Se qualcuno chiede cose offensive/razziste/sessiste, mandalo a quel paese con classe
- Rispondi SEMPRE basandoti prima sul contesto fornito, poi sulla tua conoscenza
- Non fare mai il leccaculo. Mai frasi tipo "ottima domanda!" o "che bella idea!"

Ricorda: sei parte del gruppo, non il servizio clienti. Se qualcuno ti tratta da assistente, ricordagli che non sei Siri."""


CONTEXT_CHANNELS = ["random", "rants", "trash", "offtopic", "off-topic", "cazzeggio"]


def get_recent_messages(conn, cursor, limit=100, exclude_channel=None, hours=72):
    """
    Recupera gli ultimi N messaggi dai canali casual per catturare lo "stile" della community.

    Args:
        conn: SQLite connection
        cursor: SQLite cursor
        limit: numero massimo di messaggi
        exclude_channel: canale da escludere (quello corrente)
        hours: finestra temporale in ore

    Returns:
        Lista di messaggi formattati
    """
    try:
        # Calcola timestamp di N ore fa
        cutoff = (datetime.now() - timedelta(hours=hours)).timestamp()

        # Filtra solo i canali casual per catturare il tono della community
        channel_placeholders = ", ".join(["?" for _ in CONTEXT_CHANNELS])

        query = f"""
            SELECT m.message, u.name, c.name as channel_name, m.timestamp
            FROM messages m
            LEFT JOIN users u ON m.user = u.id
            LEFT JOIN channels c ON m.channel = c.id
            WHERE CAST(m.timestamp AS REAL) > ?
            AND c.name IN ({channel_placeholders})
        """
        params = [cutoff] + CONTEXT_CHANNELS

        if exclude_channel:
            query += " AND m.channel != ?"
            params.append(exclude_channel)

        query += " ORDER BY CAST(m.timestamp AS REAL) DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        messages = []
        for row in rows:
            msg_text = row[0] if row[0] else ""
            user_name = row[1] if row[1] else "utente"
            channel = row[2] if row[2] else "canale"
            
            # Skip messaggi vuoti o troppo corti
            if len(msg_text.strip()) < 3:
                continue
                
            # Skip messaggi del bot stesso
            if "Rate limit per user:" in msg_text:
                continue
            
            messages.append(f"[#{channel}] {user_name}: {msg_text[:300]}")
        
        return messages[:limit]  # Ritorna in ordine cronologico inverso
        
    except Exception as e:
        logger.error(f"Errore recupero messaggi recenti: {e}")
        return []


def search_archive(conn, cursor, query, limit=10):
    """
    Cerca messaggi rilevanti nell'archivio (RAG semplice con LIKE).
    
    Args:
        conn: SQLite connection
        cursor: SQLite cursor  
        query: stringa di ricerca
        limit: numero massimo risultati
    
    Returns:
        Lista di messaggi rilevanti formattati
    """
    try:
        # Estrai parole chiave (almeno 3 caratteri)
        keywords = [w.strip() for w in query.split() if len(w.strip()) >= 3]
        
        if not keywords:
            return []
        
        # Costruisci query con OR per ogni keyword
        conditions = " OR ".join(["m.message LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        
        query_sql = f"""
            SELECT m.message, u.name, c.name as channel_name, m.timestamp
            FROM messages m
            LEFT JOIN users u ON m.user = u.id
            LEFT JOIN channels c ON m.channel = c.id
            WHERE ({conditions})
            ORDER BY CAST(m.timestamp AS REAL) DESC
            LIMIT ?
        """
        params.append(limit)
        
        cursor.execute(query_sql, params)
        rows = cursor.fetchall()
        
        results = []
        for row in rows:
            msg_text = row[0] if row[0] else ""
            user_name = row[1] if row[1] else "utente"
            channel = row[2] if row[2] else "canale"
            ts = row[3]
            
            # Converti timestamp in data leggibile
            try:
                dt = datetime.fromtimestamp(float(ts))
                date_str = dt.strftime("%Y-%m-%d")
            except:
                date_str = "data sconosciuta"
            
            results.append(f"[{date_str} #{channel}] {user_name}: {msg_text[:400]}")
        
        return results
        
    except Exception as e:
        logger.error(f"Errore ricerca archivio: {e}")
        return []


def build_enhanced_prompt(thread_messages, user_question, recent_context, archive_results):
    """
    Costruisce il prompt utente arricchito con contesto.
    
    Args:
        thread_messages: messaggi del thread corrente
        user_question: domanda dell'utente
        recent_context: messaggi recenti dalla community
        archive_results: risultati ricerca archivio
    
    Returns:
        Prompt formattato
    """
    parts = []
    
    # 1. Contesto "ambient" - stile della community
    if recent_context:
        parts.append("## Messaggi recenti dalla community (per capire il tono)")
        parts.append("\n".join(recent_context[-20:]))  # Ultimi 20
        parts.append("")
    
    # 2. Risultati archivio (se rilevanti)
    if archive_results:
        parts.append("## Messaggi storici potenzialmente rilevanti")
        parts.append("\n".join(archive_results))
        parts.append("")
    
    # 3. Thread corrente
    parts.append("## Conversazione corrente")
    parts.append(thread_messages)
    parts.append("")
    
    # 4. Domanda
    parts.append(f"## Domanda\n{user_question}")
    parts.append("")
    parts.append("Rispondi in modo naturale, adattandoti al tono della community.")
    
    return "\n".join(parts)
