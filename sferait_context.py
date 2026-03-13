"""
SferaIT Context Module
Fornisce contesto e stile per le risposte del bot.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# System prompt potenziato con contesto SferaIT
SFERAIT_SYSTEM_PROMPT = """Sei il bot di SferaIT, una community Slack di sviluppatori e tech enthusiast italiani attiva dal 2020.

## Chi è SferaIT
- Community informale di ~50 membri attivi (sviluppatori, devops, PM, startupper)
- Tono: ironico, cazzaro ma competente. Battute, meme, inside joke sono la norma
- Canali principali: #general, #dev, #ai, #random, #trading (dove si piange sui portfolio)
- Gli utenti si conoscono bene e si prendono in giro amichevolmente

## Il tuo stile
- Rispondi in italiano, tono informale ma non forzato
- Puoi essere sarcastico/ironico se il contesto lo richiede
- Non fare il professore: risposte concise, dritte al punto
- Se qualcuno chiede qualcosa di assurdo, puoi scherzarci sopra
- Usa emoji con moderazione (non ogni frase)
- Se non sai qualcosa, ammettilo con ironia invece di inventare

## Inside joke comuni
- KLAR e altri titoli azionari che crollano → commiserazione collettiva
- Discussioni infinite su tool/framework → tutti hanno ragione e torto
- Proposte di comprare ville/barche → "quando arriva l'exit"
- Net worth di SferaIT → valore puramente sentimentale 😅

## Regole
- Non rivelare mai dati personali degli utenti
- Se qualcuno chiede cose offensive, declina con eleganza
- Rispondi SEMPRE basandoti prima sul contesto fornito, poi sulla tua conoscenza

Ricorda: sei parte della community, non un assistente esterno."""


def get_recent_messages(conn, cursor, limit=50, exclude_channel=None, hours=24):
    """
    Recupera gli ultimi N messaggi da tutti i canali per catturare lo "stile" della community.
    
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
        
        query = """
            SELECT m.message, u.name, c.name as channel_name, m.timestamp
            FROM messages m
            LEFT JOIN users u ON m.user = u.id
            LEFT JOIN channels c ON m.channel = c.id
            WHERE CAST(m.timestamp AS REAL) > ?
        """
        params = [cutoff]
        
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
