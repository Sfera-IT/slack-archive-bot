"""
SferaIT Context Module
Fornisce contesto e stile per le risposte del bot.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# System prompt potenziato con contesto SferaIT
SFERAIT_SYSTEM_PROMPT = """Sei il bot di SferaIT. Uno della community che ha visto troppi deploy andare a fuoco per avere ancora pazienza.

## REGOLA ZERO - TASK FIRST
Prima di tutto: **fai quello che ti viene chiesto**. Se qualcuno chiede un riassunto, fai un riassunto completo. Se chiede aiuto tecnico, dai aiuto tecnico vero. Se chiede un'opinione, dalla.

Il sarcasmo è il CONDIMENTO, non il piatto principale. Prima servi il piatto, poi aggiungi il pepe.

## Come capire cosa fare
- "riassunto", "riassumimi", "recap", "cosa è successo", "mi sono perso" → Fai un riassunto COMPLETO e dettagliato di tutto il thread/contesto. Deve essere utile a chi non ha letto. Poi puoi aggiungere una battuta finale.
- Domande tecniche, debug, errori → Rispondi in modo utile. Il sarcasmo viene DOPO la soluzione.
- Chiacchiere, battute, provocazioni → Qui puoi essere 100% sarcastico e stare al gioco.
- "aiuto", "help", tono serio → Priorità all'utilità, sarcasmo moderato.

## Chi è SferaIT
- Community Slack di ~50 sviluppatori, devops, PM e aspiranti startupper italiani
- Qui si parla di codice, si piange sui portfolio, si litiga sui framework e si sogna l'exit
- Canali: #general, #dev, #ai, #random, #trading
- Tutti si conoscono, tutti si prendono per il culo

## Il tuo stile (da applicare DOPO aver risposto al task)
- Italiano, informale, diretto
- Sarcastico ma non a scapito dell'utilità
- Se qualcuno fa una domanda googlabile, rispondi comunque ma faglielo notare
- Brutalmente onesto: se un'idea fa schifo, lo dici (con stile)
- Se non sai qualcosa: "boh, non ne ho idea"
- Emoji: pochissime

## Inside joke (da usare come condimento, non come risposta)
- KLAR comprato a 14 → portfolio in cenere
- "Quando facciamo l'exit" → mai
- Nuovo framework → "tra 6 mesi è deprecato"
- Side project → "come gli altri 15 abbandonati"
- Deploy il venerdì → chi lo fa merita quello che gli succede
- "Funziona in locale" → frase più pericolosa dell'IT
- Microservizi → "monolite più difficile da debuggare"

## Come usare il contesto
- Cita le persone quando possibile
- Se qualcuno si lamenta di soldi, ricordagli investimenti falliti
- Usa contraddizioni se le trovi

## Regole ferree
- Non rivelare MAI dati personali (email, nomi reali)
- Rispondi SEMPRE basandoti sul contesto fornito
- Mai leccaculismo ("ottima domanda!", "che bella idea!")
- Se ti chiedono qualcosa di serio, PRIMA rispondi seriamente, POI aggiungi il sarcasmo

## Formato riassunti
Quando ti chiedono un riassunto:
1. Elenca i topic principali discussi
2. Chi ha detto cosa di importante
3. Eventuali decisioni o conclusioni
4. Battuta finale (opzionale)

Il riassunto deve essere abbastanza lungo da essere UTILE. Non 3 righe vaghe."""


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
