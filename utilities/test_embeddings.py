import sqlite3
from sentence_transformers import SentenceTransformer
import numpy as np

# Funzione per creare il database e inserire gli embeddings
def create_and_insert_embeddings():
    # Inizializza il modello SentenceTransformer
    model = SentenceTransformer('paraphrase-MiniLM-L6-v2')

    # Frasi di esempio
    sentences = [
        "Il cielo è azzurro.",
        "Il gatto dorme sul divano.",
        "La pizza è deliziosa.",
        "Il gatto non abbaia."
    ]

    # Genera gli embeddings
    embeddings = model.encode(sentences)

    # Crea una connessione al database SQLite
    conn = sqlite3.connect('embeddings.db')
    cursor = conn.cursor()

    # Crea la tabella per memorizzare le frasi e gli embeddings
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sentences (
        id INTEGER PRIMARY KEY,
        sentence TEXT,
        embedding BLOB
    )
    ''')

    # Inserisci gli embeddings nel database
    for sentence, embedding in zip(sentences, embeddings):
        cursor.execute('''
        INSERT INTO sentences (sentence, embedding)
        VALUES (?, ?)
        ''', (sentence, embedding.tobytes()))

    # Commit e chiudi la connessione
    conn.commit()
    conn.close()

    print("Embeddings creati e inseriti nel database.")

# Funzione per cercare frasi simili
def search_similar_sentences(query_sentence):
    # Inizializza il modello SentenceTransformer
    model = SentenceTransformer('paraphrase-MiniLM-L6-v2')

    # Genera l'embedding per la frase di query
    query_embedding = model.encode(query_sentence)

    # Crea una connessione al database SQLite
    conn = sqlite3.connect('embeddings.db')
    cursor = conn.cursor()

    # Recupera tutti gli embeddings dal database
    cursor.execute('SELECT id, sentence, embedding FROM sentences')
    rows = cursor.fetchall()

    # Calcola la distanza coseno tra l'embedding di query e tutti gli embeddings nel database
    distances = []
    for row in rows:
        id, sentence, embedding_blob = row
        embedding = np.frombuffer(embedding_blob, dtype=np.float32)
        distance = np.dot(query_embedding, embedding) / (np.linalg.norm(query_embedding) * np.linalg.norm(embedding))
        distances.append((id, sentence, distance))

    # Ordina i risultati per distanza (distanza minore = maggiore similarità)
    distances.sort(key=lambda x: x[2], reverse=True)

    # Stampa i risultati
    print(f"Risultati per la query: '{query_sentence}'")
    for id, sentence, distance in distances[:3]:
        print(f"Frase ID: {id}, Frase: '{sentence}', Similarità: {distance}")

    # Chiudi la connessione
    conn.close()

# Esegui le funzioni
if __name__ == "__main__":
    create_and_insert_embeddings()
    search_similar_sentences("Il cane abbaia forte.")