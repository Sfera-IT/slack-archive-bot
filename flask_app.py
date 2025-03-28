from flask import Flask, jsonify, request, redirect, url_for, session
import sqlite3
import os
import requests
from dotenv import load_dotenv
import jwt
from slack_bolt.adapter.flask import SlackRequestHandler
from archivebot import app, update_users
handler = SlackRequestHandler(app)
import datetime
from sentence_transformers import SentenceTransformer
import numpy as np
import openai
from datetime import timedelta
import re
from functools import wraps
from flask import g, redirect, url_for
import csv
from io import StringIO
import openai
from pydub import AudioSegment
import io
from openai import OpenAI
from pathlib import Path
from flask import send_file

# Sposta l'array degli amministratori in una variabile globale
ADMIN_USERS = [
    'U011PQ7RHRT',
    'U011MV24J2W',
    'U0129HFHRJ4',
    'U011N8WRRD0',
    'U011Z26G449',
    'U011CKQ7D71',
    'U011KE4BF0W',
    'U011PN35BHT'
]

DEFAULT_OPENAI_MODEL = "gpt-4o-2024-08-06"

def auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        headers = get_slack_headers()
        g.headers = headers
        user_info = verify_token_and_get_user(headers)
        if not headers or not user_info:
            return redirect(url_for('login'))
        
        g.user_id = user_info['user_id']
        g.username = get_username(g.user_id)
        
        conn = get_db_connection()
        g.opted_out = conn.execute('SELECT * FROM optout WHERE user = ?', (g.user_id,)).fetchone() is not None
        g.opted_out_ai = conn.execute('SELECT * FROM optout_ai WHERE user = ?', (g.user_id,)).fetchone() is not None
        conn.close()
        
        return f(*args, **kwargs)
    return decorated_function

def optin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if check_optout(g.user_id):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


load_dotenv()

flask_app = Flask(__name__)
flask_app.secret_key = os.getenv('SECRET_KEY')
flask_app.config['PREFERRED_URL_SCHEME'] = 'https'

# Attenzione, sono i dati dell'applicazione slack-archive-gui e non slack-archive-bot
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
OAUTH_SCOPE = os.getenv('OAUTH_SCOPE')
EXPECTED_TEAM_ID = os.getenv('EXPECTED_TEAM_ID')
CLIENT_URL = os.getenv('CLIENT_URL')    

# default handler for slack events, through archivebot.py
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


# Middleware per aggiungere le intestazioni CORS a tutte le risposte
@flask_app.after_request
def apply_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization')
    return response


def get_response(data):
    response = jsonify(data)
    return response


def get_db_connection():
    cur_dir = os.path.dirname(__file__)
    db_path = os.getenv('DB_PATH', '/data/slack.sqlite')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@flask_app.route('/login')
def login():
    slack_auth_url = (
        f"https://slack.com/oauth/v2/authorize?client_id={CLIENT_ID}"
        f"&scope={OAUTH_SCOPE}&user_scope=identity.basic"
        f"&redirect_uri=https://slack-archive.sferait.org/oauth_callback"
    )
    return redirect(slack_auth_url)


@flask_app.route('/oauth_callback')
def oauth_callback():
    code = request.args.get('code')
    if not code:
        return 'Authorization failed.', 400

    response = requests.post('https://slack.com/api/oauth.v2.access', data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'redirect_uri': url_for('oauth_callback', _external=True, _scheme='https')
    })

    response_data = response.json() 

    if not response_data.get('ok'):
        return 'Failed to authenticate with Slack.', 400

    session['access_token'] = response_data['access_token'] # Attenzione, è un token applicazione e non un token utente, preferisco recuperare l'utente e farmi il mio token jwt

    # create a jwt token with expiration
    exp_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=86400)
    jwt_token = jwt.encode({'user_id': response_data['authed_user']['id'], 'exp': exp_time, 'slack_token': response_data['access_token']}, flask_app.secret_key, algorithm='HS256')
    
    return redirect(CLIENT_URL + "?token="+jwt_token)


def get_slack_headers():
    # get headers from the request
    headers = request.headers
    if 'Authorization' in headers:
        return {'Authorization': headers['Authorization']}
    return None


@flask_app.route('/emoji', methods=['GET'])
@auth_required
def get_emoji():
    slack_token = verify_token_and_get_user(g.headers)['slack_token']
        
    response = requests.get('https://slack.com/api/emoji.list', headers={'Authorization': 'Bearer ' + slack_token})
    data = response.json()

    if not data.get('ok'):
        return False
    
    return get_response(data)


def verify_token_and_get_user(headers):
    token = headers['Authorization']
    # remove Bearer
    token = token.split('Bearer ')[1]

    try:
        decoded = jwt.decode(token, flask_app.secret_key, algorithms=['HS256'], options={'verify_exp': True})
        user_id = decoded['user_id']
        # check if user_id exists in the database
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        if not user:
            return False
        else:
            return {'user_id': decoded['user_id'], 'slack_token': decoded['slack_token']}
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False
    return False


def get_username(user):
    conn = get_db_connection()
    user = conn.execute('SELECT name FROM users WHERE id = ?', (user,)).fetchone()
    conn.close()
    return user['name']


@flask_app.route('/channels', methods=['OPTIONS'])
def get_channels_options():
    return get_response({})


@flask_app.route('/whoami', methods=['GET'])
@auth_required
def whoami():
    user = g.user_id
    username = g.username
    opted_out_ai = g.opted_out_ai
    is_admin = user in ADMIN_USERS

    conn = get_db_connection()
    status = conn.execute('SELECT * FROM optout WHERE user = ?', (user,)).fetchone()

    conn.close()

    if status:
        return get_response({'user_id': user, 'username': username, 'opted_out': True, 'opted_out_ai': opted_out_ai, 'is_admin': is_admin})
    return get_response({'user_id': user, 'username': username, 'opted_out': False, 'opted_out_ai': opted_out_ai, 'is_admin': is_admin})


def notify_admins(text):
    for user in ADMIN_USERS:
        response = app.client.chat_postMessage(
            channel=user,
            text=text
        )


@flask_app.route('/optout', methods=['GET'])
@auth_required
def optout():
    user = g.user_id

    conn = get_db_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO optout (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)', (user,))
        cursor.execute('UPDATE messages SET message = "User opted out of archiving. This message has been deleted", user = "USLACKBOT", permalink = "" WHERE user = ?', (user,))
        conn.commit()

        notify_admins(
            "L'utente <@" + user + "> ha scelto di non essere più archiviato."
        )

    except Exception as e:
        # return the exception as an error
        if conn:
            conn.rollback()
        return get_response({'error': str(e)})
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return get_response({'user_id': user, 'opted_out': True})


@flask_app.route('/channels', methods=['GET'])
@auth_required
@optin_required
def get_channels():
    conn = get_db_connection()
    channels = conn.execute('''
        SELECT c.*, MAX(m.timestamp) as last_message_timestamp
        FROM channels c
        LEFT JOIN messages m ON c.id = m.channel
        WHERE c.is_private = 0
        GROUP BY c.id
        ORDER BY last_message_timestamp DESC, c.name
    ''').fetchall()
    conn.close()
    return get_response([dict(ix) for ix in channels])


@flask_app.route('/users', methods=['GET'])
@auth_required
@optin_required
def get_users():    
    conn = get_db_connection()
    users = conn.execute('SELECT * FROM users').fetchall()
    conn.close()
    return get_response([dict(ix) for ix in users])


def check_optout(user):
    conn = get_db_connection()
    status = conn.execute('SELECT * FROM optout WHERE user = ?', (user,)).fetchone()
    conn.close()
    if status:
        return True
    return False


@flask_app.route('/messages/<channel_id>', methods=['GET'])
@auth_required
@optin_required
def get_messages(channel_id):
    conn = get_db_connection()
    offset = request.args.get('offset', 0)
    limit = request.args.get('limit', 20)
    
    messages = conn.execute('''
        SELECT 
            m.message,
            m.user,
            m.channel,
            m.timestamp,
            m.permalink,
            m.thread_ts, 
            u.name as user_name,
            (SELECT COUNT(*) 
             FROM messages thread 
             WHERE thread.thread_ts = m.timestamp 
               AND thread.channel = m.channel
               AND thread.user NOT IN (SELECT user FROM optout)) as thread_count
        FROM messages m
        JOIN users u ON m.user = u.id
        WHERE m.channel = ?
          AND (m.thread_ts IS NULL OR m.thread_ts = m.timestamp)
          AND m.user NOT IN (SELECT user FROM optout)
        ORDER BY m.timestamp DESC
        LIMIT ? OFFSET ?
    ''', (channel_id, limit, offset)).fetchall()
    
    conn.close()
    
    # Convert row objects to dictionaries
    messages = [dict(msg) for msg in messages]
        
    return get_response([dict(ix) for ix in messages])


@flask_app.route('/thread/<message_id>', methods=['GET'])
@auth_required
@optin_required
def get_thread(message_id):
    conn = get_db_connection()
    thread = conn.execute('''
        SELECT
        messages.message,
        messages.user,
        messages.channel,
        messages.timestamp,
        messages.permalink,
        messages.thread_ts, 
        users.name as user_name 
        FROM messages 
        JOIN users ON messages.user = users.id 
        WHERE ( messages.timestamp = ? OR messages.thread_ts = ? )
        AND user NOT IN (SELECT user FROM optout)                  
        ''', 
        (message_id, message_id)).fetchall()
    conn.close()
    return get_response([dict(ix) for ix in thread])


@flask_app.route('/searchV2', methods=['GET'])
@auth_required
@optin_required
def search_messages_V2():
    # Get search parameters
    query = request.args.get('query', '')
    user_name = request.args.get('user_name', '')
    channel_name = request.args.get('channel_name', '')
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')

    conn = get_db_connection()
    
    # Build the SQL query
    sql = '''
    SELECT DISTINCT
    messages.message,
    messages.user,
    messages.channel,
    messages.timestamp,
    messages.permalink,
    messages.thread_ts,  
    
    users.name as user_name, channels.name as channel_name
    FROM messages
    JOIN users ON messages.user = users.id
    JOIN channels ON messages.channel = channels.id
    LEFT JOIN members ON messages.channel = members.channel
    WHERE 1=1
    '''
    params = []

    # query:
    # if the query is surrounded by quotes, search for the exact phrase
    # otherwise, search for each term separately
    if query:
        if query.startswith('"') and query.endswith('"'):
            query = query[1:-1]
            sql += ' AND messages.message LIKE ?'
            params.append('%' + query + '%')
        else:
            for term in query.split():
                sql += ' AND messages.message LIKE ?'
                params.append('%' + term + '%')

    if user_name:
        sql += ' AND users.name LIKE ?'
        params.append('%' + user_name + '%')
    
    if channel_name:
        sql += ' AND channels.name LIKE ?'
        params.append('%' + channel_name + '%')
    
    if start_time:
        start_timestamp = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
        sql += ' AND CAST(messages.timestamp AS FLOAT) >= ?'
        params.append(start_timestamp)
    
    if end_time:
        end_timestamp = datetime.datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
        sql += ' AND CAST(messages.timestamp AS FLOAT) <= ?'
        params.append(end_timestamp)

    sql += ' ORDER BY messages.timestamp DESC LIMIT 2000'

    messages = conn.execute(sql, params).fetchall()
    conn.close()

    return get_response([dict(ix) for ix in messages])


@flask_app.route('/searchEmbeddings', methods=['GET'])
@auth_required
@optin_required
def search_messages_embeddings():
    # Get search parameters
    query = request.args.get('query', '')
    user_name = request.args.get('user_name', '')
    channel_name = request.args.get('channel_name', '')
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')

    conn = get_db_connection()
    
    # Build the SQL query
    sql = '''
    SELECT DISTINCT 
    messages.message,
    messages.user,
    messages.channel,
    messages.timestamp,
    messages.permalink,
    messages.thread_ts,
    messages.embeddings,
    users.name as user_name, channels.name as channel_name
    FROM messages
    JOIN users ON messages.user = users.id
    JOIN channels ON messages.channel = channels.id
    LEFT JOIN members ON messages.channel = members.channel
    WHERE messages.embeddings IS NOT NULL
    '''
    params = []

    if user_name:
        sql += ' AND users.name LIKE ?'
        params.append('%' + user_name + '%')
    
    if channel_name:
        sql += ' AND channels.name LIKE ?'
        params.append('%' + channel_name + '%')
    
    if start_time:
        start_timestamp = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
        sql += ' AND CAST(messages.timestamp AS FLOAT) >= ?'
        params.append(start_timestamp)
    
    if end_time:
        end_timestamp = datetime.datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
        sql += ' AND CAST(messages.timestamp AS FLOAT) <= ?'
        params.append(end_timestamp)

    sql += ' ORDER BY messages.timestamp DESC'

    messages = conn.execute(sql, params).fetchall()

    # Inizializza il modello SentenceTransformer
    model = SentenceTransformer('paraphrase-MiniLM-L6-v2')

    # Genera l'embedding per la frase di query
    query_embedding = model.encode(query)

    # copy messages into an array of dictionaries
    messages = [dict(ix) for ix in messages]

    # Calcola la distanza coseno tra l'embedding di query e tutti gli embeddings nel database
    distances = []
    for row in messages:
        id = row['timestamp']
        sentence = row['message']
        embedding_blob = row['embeddings']

        embedding = np.frombuffer(embedding_blob, dtype=np.float32)
        distance = np.dot(query_embedding, embedding) / (np.linalg.norm(query_embedding) * np.linalg.norm(embedding))

        row['distance'] = distance
        row.pop('embeddings')

        distances.append(row)

    # Ordina i risultati per distanza (distanza minore = maggiore similarità)
    distances.sort(key=lambda x: x['distance'], reverse=True)

    # mantengo solo i primi 100 risultati
    distances = distances[:100]

    # itero su distances e converto la colonna distance in stringa
    for d in distances:
        d['distance'] = str(d['distance'])

    return get_response(distances)

def generate_podcast_audio(podcast_content):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    max_length = 4000  # Lasciamo un po' di margine
    segments = [podcast_content[i:i+max_length] for i in range(0, len(podcast_content), max_length)]
    
    audio_segments = []
    for segment in segments:
        response = client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=segment
        )
        
        # Salva il segmento audio temporaneamente
        temp_file = f"temp_segment_{len(audio_segments)}.mp3"
        response.stream_to_file(temp_file)
        
        # Carica il segmento audio e aggiungilo alla lista
        audio_segments.append(AudioSegment.from_mp3(temp_file))
        
        # Rimuovi il file temporaneo
        os.remove(temp_file)
    
    # Unisci tutti i segmenti audio
    combined_audio = sum(audio_segments)
    
    # Salva l'audio combinato
    combined_audio.export("podcast.mp3", format="mp3")

# Aggiungi questa funzione per generare il contenuto del podcast
def generate_podcast_content(formatted_messages):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    response = client.chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Sei un membro della Community Sfera IT che crea contenuti per podcast basati sulle conversazioni della community. Il tuo compito è creare un riassunto scorrevole e coinvolgente, adatto all'ascolto, come se stessi parlando con altri membri della community."},
            {"role": "user", "content": f"""
                Crea un podcast basato sulle seguenti conversazioni della Community Sfera IT. Il podcast deve:
                1. Essere scorrevole e naturale, come se stessi chiacchierando con altri membri della community
                2. Essere coinvolgente e interessante da ascoltare, riferendoti direttamente alla "Community Sfera IT"
                3. Menzionare i nickname di chi ha avviato le discussioni più interessanti
                4. Mantenere un tono informale e autentico, come se fossi "uno di noi", ma non esagerare con lo small talk, concentrati sugli argomenti e sulle conversazioni
                5. Raccontare in modo discorsivo e fluido cosa è accaduto nei thread della Community
                6. Evitare di suonare troppo artificiale o "finto"
                7. Avere una durata di circa 10 minuti quando letto ad un ritmo normale, indicativamente 1500 parole

                Presenta le informazioni come se fossi un membro della community che racconta gli ultimi sviluppi e discussioni ai suoi amici. Usa espressioni come "nella nostra community", "i nostri membri", "abbiamo discusso di", ecc.
                Dividi il podcast in 2 sezioni:
                    - una prima sezione in cui fai una carrellata veloce degli argomenti che sono stati trattati in tutti i thread, cercando di coprire il maggior numero di thread e argomenti, condensando il più possibile le tematiche con parole sistetiche e concise, con poco intercalare
                    - una seconda sezione in cui fai un discorso più approfondito sui 2-3 thread più coinvolgenti tra quelli trattati nella prima sezione, mostrando i dettagli più importanti e significativi, evidenziando le conversazioni più intense e coinvolgenti

                Ecco le conversazioni:
                {formatted_messages}
            """}
        ],
        max_tokens=8192,
        temperature=1.0,
    )
    
    return response.choices[0].message.content


@flask_app.route('/generate_digest', methods=['POST'])
@auth_required
@optin_required
def generate_digest():
    conn = get_db_connection()

    # before executing the query, check if a digest already exist in the last 24 hours. If yes, return the saved digest
    # unless there is a parameter "force_generate"

    existing_digest = conn.execute('''
    SELECT digest, period FROM digests
    WHERE timestamp >= datetime('now', '-1 day')
    ORDER BY timestamp DESC
    LIMIT 1
    ''').fetchone()

    force_generate = request.json.get('force_generate', False)
    send_to_channel = request.json.get('send_to_channel', False)
    if existing_digest and not force_generate and not send_to_channel:
        conn.close()
        return get_response({
            'status': 'success', 
            'digest': existing_digest['digest'],
            'period': existing_digest['period']
        })
    
    # If no existing digest, continue with the original logic to generate a new one
    messages = conn.execute(f'''
    SELECT 
        message,
        users.name as username,
        channels.name as channel_name,
        timestamp,
        CASE 
            WHEN thread_ts IS NULL THEN timestamp 
            ELSE thread_ts 
        END AS thread_ts
    FROM messages
    INNER JOIN users on users.id = messages.user
    INNER JOIN channels on channels.id = messages.channel
    WHERE 
        thread_ts in (
            SELECT DISTINCT thread_ts
            FROM messages
            WHERE datetime(timestamp, 'unixepoch') >= datetime('now', '-1 days')
            AND thread_ts IS NOT NULL
        )
        AND
        user != 'USLACKBOT'
        AND 
        user NOT IN (SELECT user FROM optout_ai)
        AND
        channels.id != 'C07F6RUTVQW'
    ORDER BY channel_name ASC, thread_ts ASC, timestamp ASC;
    ''').fetchall()

    # Format the messages for the OpenAI prompt, including all the columns
    formatted_messages = ""
    current_channel = None
    current_thread = None

    for message in messages:
        # Start a new channel section if needed
        if message['channel_name'] != current_channel:
            current_channel = message['channel_name']
            formatted_messages += f"\n\nChannel: {current_channel}\n"
            current_thread = None

        # Start a new thread section if needed
        if message['thread_ts'] != current_thread:
            current_thread = message['thread_ts']
            formatted_messages += f"\nThread started at {datetime.datetime.fromtimestamp(float(current_thread)).strftime('%Y-%m-%d %H:%M:%S')} with timestamp {current_thread}:\n"

        # Format the message
        timestamp = datetime.datetime.fromtimestamp(float(message['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
        formatted_messages += f"[{timestamp}] {message['username']}: {message['message']}\n"

    max_chars = 256000  # Approximate character limit (128000 tokens * 2 chars per token)
    if len(formatted_messages) > max_chars:
        formatted_messages = formatted_messages[:max_chars] + "...\n(truncated due to length)"
    
    
    # Generate summary using OpenAI
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    response = client.chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Sei un assistente che riassume le conversazioni di un workspace di Slack. Fornirai riassunti molto dettagliati, usando almeno 3000 parole, e sempre in italiano."},
            {"role": "user", "content": f"""
                Sei un assistente che riassume le conversazioni di un workspace di Slack. Fornirai riassunti molto dettagliati, usando almeno 3000 parole, e sempre in italiano.
                In allegato ti invio il tracciato delle ultime 24 ore di un workspace Slack. 

                Dettagli sull'estrazione:
                - L'estrazione contiene tutti i messaggi inviati sul workspace, suddivisi in canali e thread. 
                - Sono inclusi anche i thread più vecchi di 24 ore se hanno ricevuto una risposta nelle ultime 24 ore. 
                            
                Il tuo compito è creare un digest:
                - La prima parte del digest è un indice: deve contenere un elenco puntato, estremamente conciso ma dettagliato, di TUTTI gli argomenti trattati, TUTTI I THREAD, uno per uno. Per ogni argomento una breve descrizione, chi ha aperto il thread e link al thread (tutto sulla stessa riga)
                - La seconda parte del Digest è invece discorsiva, rimanendo sempre dettagliata e sui fatti, non essere troppo generico: racconta cosa è successo su ogni canale in maniera descrittiva, enfatizzando le conversazioni più coinvolgenti e partecipate se ci sono state, gli argomenti trattati (fornendo un buon numero di dettagli), inclusi i nomi dei partecipanti alle varie conversazioni, evidenziati. Anche in questo caso, inserisci sempre il link alle conversazioni citate.

                Altri importanti dettagli:
                - La risposta deve essere in formato markdown.
                - Inserisci sempre un link alle conversazioni più coinvolgenti di ogni canale, il link è nel formato [link](https://slack-archive.sferait.org/getlink?timestamp=MESSAGE_TIMESTAMP) dove MESSAGE_TIMESTAMP è il valore del timestamp del thread esattamente come riportato.
                - Evita commenti rispetto alla vivacita o varietà del gruppo, rimani sempre fattuale, parla dei fatti e delle conversazioni avvenute, non giudicarne il contenuto. 
                - È importante che il digest raccolga tutte le conversazioni delle ultime ore e non ne escluda nessuna.
                - Ricorda che il nome dell'utente che ha inviato il post o ha avviato la conversazione è sempre PRIMA del messaggio, non dopo

                {formatted_messages}"""}
        ],
        max_tokens=16384,
        temperature=0.7,
    )
    
    summary = response.choices[0].message.content

    # Calculate the period
    end_date = datetime.datetime.utcnow()
    start_date = end_date - timedelta(days=1)
    period = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"

    # Genera il contenuto del podcast
    podcast_content = generate_podcast_content(formatted_messages)

    # Genera l'audio del podcast utilizzando la nuova funzione
    generate_podcast_audio(podcast_content)

    # Inserisci il digest e il contenuto del podcast nel database
    conn.execute('''
    INSERT INTO digests (timestamp, period, digest, posts, podcast_content)
    VALUES (?, ?, ?, ?, ?)
    ''', (datetime.datetime.utcnow().isoformat(), period, summary, formatted_messages, podcast_content))
    conn.commit()
    conn.close()

    # If send_to_channel is set, send the digest to the channel
    if send_to_channel:
        try:
            slack_formatted_summary = convert_markdown_to_slack(summary)
            message = f"*Digest for {period}*\n\n{slack_formatted_summary} \n\n Puoi trovare maggiori informazioni ed eseguire opt-out dalle funzioni AI qui: https://sferaarchive-client.vercel.app/"
            response = app.client.chat_postMessage(
                channel='C07F6RUTVQW',
                text=message,
                parse="full"
            )
            if not response['ok']:
                return get_response({'status': 'error', 'message': 'Failed to send digest to channel'})
        except Exception as e:
            return get_response({'status': 'error', 'message': f'Error sending digest to channel: {str(e)}'})

    return get_response({'status': 'success', 'digest': summary, 'period': period})


@flask_app.route('/digest_details', methods=['POST'])
@auth_required
@optin_required
def digest_details():
    user = g.user_id

    # Get the query from the POST request
    data = request.get_json()
    query = data.get('query')
    if not query:
        return get_response({'error': 'No query provided'})

    conn = get_db_connection()
    
    # Get the latest digest
    latest_digest = conn.execute('''
    SELECT digest, posts, timestamp FROM digests
    ORDER BY timestamp DESC
    LIMIT 1
    ''').fetchone()

    if not latest_digest:
        conn.close()
        return get_response({'error': 'No digest available'})

    digest = latest_digest['digest']
    posts = latest_digest['posts']
    digest_timestamp = latest_digest['timestamp']

    # Generate details using OpenAI
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    response = client.chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Sei un assistente che fornisce dettagli sulle conversazioni di un workspace Slack in base a specifiche richieste."},
            {"role": "user", "content": f"""Dati i seguenti post originali, fornisci dettagli specifici in risposta alla query dell'utente. 
            Usa i post originali per fornire informazioni precise e dettagliate.

            Post originali:
            {posts}

            Query dell'utente: {query}

            Fornisci una risposta dettagliata, in italiano e in formato markdown."""}
        ],
        max_tokens=4096,
        temperature=0.7,
    )
    
    details = response.choices[0].message.content

    # Salva i dettagli generati nel database
    try:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO digest_details (user_id, query, details, timestamp, digest_timestamp)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (user, query, details, digest_timestamp))
        conn.commit()
    except Exception as e:
        conn.rollback()
        pass
    finally:
        cursor.close()

    conn.close()

    return get_response({'status': 'success', 'details': details})


@flask_app.route('/optout_ai', methods=['GET'])
@auth_required
def optout_ai():
    user = g.user_id

    conn = get_db_connection()
    cursor = None
    try:
        cursor = conn.cursor()

        opted_out_ai = conn.execute('SELECT * FROM optout_ai WHERE user = ?', (user,)).fetchone()
        if opted_out_ai:
            cursor.execute('DELETE FROM optout_ai WHERE user = ?', (user,))
            ret = False
        else:
            cursor.execute('INSERT INTO optout_ai (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)', (user,))
            ret = True
        
        conn.commit()

    except Exception as e:
        # return the exception as an error
        if conn:
            conn.rollback()
        return get_response({'error': str(e)})
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return get_response({'user_id': user, 'opted_out_ai': ret})


@flask_app.route('/getlink', methods=['GET'])
def get_link():
    timestamp = request.args.get('timestamp')
    if not timestamp:
        return jsonify({'error': 'No timestamp provided'}), 400

    conn = get_db_connection()
    try:
        message = conn.execute('SELECT permalink FROM messages WHERE thread_ts LIKE ? and permalink != "" order by timestamp', ('%'+timestamp+'%',)).fetchone()
        if message and message['permalink']:
            return redirect(message['permalink'])
        else:
            return jsonify({'error': 'Message not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


def convert_markdown_to_slack(text):
    # Convert headers
    text = re.sub(r'^#\s(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'^###\s(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert bold
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Convert italic
    text = re.sub(r'_(.+?)_', r'_\1_', text)

    # Convert links
    def replace_link(match):
        text = match.group(1)
        url = match.group(2)
        # Remove any surrounding angle brackets from the URL
        url = url.strip('<>')
        return f' {url} '

    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, text)

    # Convert code blocks
    text = re.sub(r'```(.+?)```', r'```\1```', text, flags=re.DOTALL)

    return text


@flask_app.route('/chat', methods=['POST'])
@auth_required
@optin_required
def chat():
    user = g.user_id
    data = request.get_json()
    message = data.get('message')
    context = data.get('context', [])
    conversation = data.get('conversation', [])
    if not message:
        return jsonify({'error': 'No message provided'}), 400

    # Prepare context for OpenAI
    context_text = "\n".join([f"{msg['user_name']}: {msg['message']}" for msg in context])
    conversation_text = "\n".join([f"{msg['user_name']}: {msg['message']}" for msg in conversation])
    prompt = f"Context:\n{context_text}\n\nConversation:\n{conversation_text}\n\nUser: {message}\nAI:"

    # Call OpenAI
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    response = client.chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Sei un assistente che risponde alle domande relative alle conversazioni di un workspace di Slack. Ti verranno passate delle conversazioni e una serie di domande a cui dovrai rispondere con precisione."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=4096,
        temperature=0.7,
    )

    ai_response = response.choices[0].message.content.strip()
    conversation.append({
        'user_name': 'AI',
        'message': ai_response,
        'timestamp': datetime.datetime.utcnow().timestamp()
    })

    return jsonify({'status': 'success', 'conversation': conversation})



@flask_app.route('/stats', methods=['GET'])
@auth_required
@optin_required
def get_stats():
    # Get the time period from the request, default to 30 days
    days = request.args.get('days', 30, type=int)

    conn = get_db_connection()

    # update the users table
    update_users(conn, conn.cursor())

    stats = {}

    # 1. User activity ranking (excluding deleted users)
    user_activity = conn.execute('''
        SELECT users.name, COUNT(*) as post_count
        FROM messages
        JOIN users ON messages.user = users.id
        WHERE datetime(messages.timestamp, 'unixepoch') > datetime('now', ?)
        AND users.is_deleted = FALSE
        GROUP BY users.id
        ORDER BY post_count DESC
    ''', (f'-{days} days',)).fetchall()
    stats['user_activity'] = [dict(row) for row in user_activity]

    # 2. Top 5 active channels
    top_channels = conn.execute('''
        SELECT channels.name, COUNT(*) as message_count
        FROM messages
        JOIN channels ON messages.channel = channels.id
        WHERE datetime(messages.timestamp, 'unixepoch') > datetime('now', ?)
        GROUP BY channels.id
        ORDER BY message_count DESC
        LIMIT 5
    ''', (f'-{days} days',)).fetchall()
    stats['top_channels'] = [dict(row) for row in top_channels]

    # 4. Most active hours
    active_hours = conn.execute('''
        SELECT 
            CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
            COUNT(*) as message_count
        FROM messages
        WHERE datetime(timestamp, 'unixepoch') > datetime('now', ?)
        GROUP BY hour
        ORDER BY message_count DESC
    ''', (f'-{days} days',)).fetchall()
    stats['active_hours'] = [dict(row) for row in active_hours]

    # 5. Emoji usage
    emoji_usage = conn.execute('''
        SELECT 
            substr(message, instr(message, ':') + 1, 
                   instr(substr(message, instr(message, ':') + 1), ':') - 1) as emoji,
            COUNT(*) as usage_count
        FROM messages
        WHERE message LIKE '%:%:%'
        AND datetime(timestamp, 'unixepoch') > datetime('now', ?)
        GROUP BY emoji
        ORDER BY usage_count DESC
        LIMIT 10
    ''', (f'-{days} days',)).fetchall()
    stats['emoji_usage'] = [dict(row) for row in emoji_usage]

    # immagini postate per autore - si identificano perchè nel testo c'è scritto "Il messaggio conteneva un media ma non è stato possibile salvarlo"
    images_by_author = conn.execute('''
        SELECT 
            users.name,
            COUNT(*) as image_count
        FROM messages
        JOIN users ON messages.user = users.id
        WHERE messages.message LIKE '%Il messaggio conteneva un media ma non è stato possibile salvarlo%'
        AND datetime(messages.timestamp, 'unixepoch') > datetime('now', ?)
        GROUP BY users.id
        ORDER BY image_count DESC
        LIMIT 10
    ''', (f'-{days} days',)).fetchall()
    stats['images_by_author'] = [dict(row) for row in images_by_author]


    # 10 thread più ingaggianti (con nome dell'autore e data del messaggio)
    engaging_threads = conn.execute('''
        SELECT 
            users.name AS author,
            channels.name AS channel,
            messages.message AS thread_start,
            datetime(messages.timestamp, 'unixepoch') AS thread_date,
            COUNT(*) AS reply_count,
            messages.timestamp as thread_ts
        FROM messages
        JOIN users ON messages.user = users.id
        JOIN channels ON messages.channel = channels.id
        WHERE messages.thread_ts IS NOT NULL
        AND datetime(messages.thread_ts, 'unixepoch') > datetime('now', ?)
        GROUP BY messages.thread_ts
        ORDER BY reply_count DESC
        LIMIT 10
    ''', (f'-{days} days',)).fetchall()
    stats['engaging_threads'] = [dict(row) for row in engaging_threads]

    # 10 autori con i thread più ingaggianti e lunghezza media dei loro thread
    engaging_authors = conn.execute('''
        WITH thread_stats AS (
            SELECT 
                users.name AS author,
                channels.name AS channel,
                messages.message AS thread_start,
                datetime(messages.timestamp, 'unixepoch') AS thread_date,
                COUNT(*) AS reply_count,
                messages.thread_ts
            FROM messages
            JOIN users ON messages.user = users.id
            JOIN channels ON messages.channel = channels.id
            WHERE messages.thread_ts IS NOT NULL
                AND datetime(messages.thread_ts, 'unixepoch') > datetime('now', ?)
                AND users.is_deleted = FALSE
            GROUP BY messages.thread_ts
            ORDER BY reply_count DESC
        )
        SELECT 
            COUNT(*) AS number_of_threads, 
            author,
            AVG(reply_count) AS avg_replies
        FROM thread_stats 
        WHERE author <> 'Slackbot'
        GROUP BY author
        ORDER BY avg_replies DESC;
    ''', (f'-{days} days',)).fetchall()
    stats['engaging_authors'] = [dict(row) for row in engaging_authors]

    # classifica degli utenti più attivi ma basata sul numero totale di parole scritte
    active_users_by_words = conn.execute('''
        SELECT 
            users.name AS author,
            SUM(LENGTH(messages.message) - LENGTH(REPLACE(messages.message, ' ', '')) + 1) AS total_words,
            COUNT(*) AS total_messages,
            AVG(LENGTH(messages.message) - LENGTH(REPLACE(messages.message, ' ', '')) + 1) AS avg_words_per_message
        FROM messages
        JOIN users ON messages.user = users.id
        WHERE datetime(messages.timestamp, 'unixepoch') > datetime('now', ?)
        AND users.is_deleted = FALSE
        GROUP BY users.id
        ORDER BY total_words DESC
        LIMIT 10
    ''', (f'-{days} days',)).fetchall()
    stats['active_users_by_words'] = [dict(row) for row in active_users_by_words]

    # Add this new query for inactive users
    inactive_users = conn.execute('''
        SELECT 
            users.real_name AS real_name,
            users.display_name AS display_name,
            CAST((julianday('now') - julianday(datetime(MAX(messages.timestamp), 'unixepoch'))) AS INTEGER) AS days_inactive
        FROM users
        LEFT JOIN messages ON users.id = messages.user
        WHERE users.name != 'Slackbot'
        AND users.is_deleted = FALSE
        GROUP BY users.id
        HAVING days_inactive > 120
        ORDER BY days_inactive DESC
    ''').fetchall()
    stats['inactive_users'] = [dict(row) for row in inactive_users]

    # Add a new query for deleted users
    deleted_users = conn.execute('''
        SELECT real_name, display_name, id
        FROM users
        WHERE is_deleted = TRUE
        ORDER BY real_name
    ''').fetchall()
    stats['deleted_users'] = [dict(row) for row in deleted_users]

    conn.close()

    return get_response(stats)


@flask_app.route('/download_users', methods=['GET'])
@auth_required
@optin_required
def download_users():
    user = g.user_id
    if user not in ADMIN_USERS:
        return get_response({'error': 'Unauthorized'}), 403

    conn = get_db_connection()

    update_users(conn, conn.cursor())

    users = conn.execute('''
        SELECT 
            users.name, 
            users.id, 
            users.real_name, 
            users.display_name, 
            users.email, 
            users.is_deleted,
            CAST((julianday('now') - julianday(datetime(MAX(messages.timestamp), 'unixepoch'))) AS INTEGER) AS days_since_last_activity,
            COUNT(messages.user) AS total_posts
        FROM users
        LEFT JOIN messages ON users.id = messages.user
        GROUP BY users.id
        ORDER BY users.name
    ''').fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'ID', 'Real Name', 'Display Name', 'Email', 'Is Deleted', 'Days Since Last Activity', 'Total Posts'])
    for user in users:
        writer.writerow(user)

    output.seek(0)
    return get_response({
        'csv': output.getvalue(),
        'filename': 'users.csv'
    })


@flask_app.route('/get_podcast_content', methods=['GET'])
@auth_required
@optin_required
def get_podcast_content():
    conn = get_db_connection()
    latest_digest = conn.execute('''
    SELECT podcast_content FROM digests
    ORDER BY timestamp DESC
    LIMIT 1
    ''').fetchone()
    conn.close()

    if latest_digest:
        return get_response({'podcast_content': latest_digest['podcast_content']})
    else:
        return get_response({'error': 'No podcast content available'}), 404


@flask_app.route('/get_podcast_audio', methods=['GET'])
@auth_required
@optin_required
def get_podcast_audio():
    try:
        return send_file("podcast.mp3", mimetype="audio/mpeg", as_attachment=True)
    except FileNotFoundError:
        return jsonify({'error': 'Podcast audio not found'}), 404


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    flask_app.run(debug=debug_mode)
