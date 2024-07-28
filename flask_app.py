from flask import Flask, jsonify, request, redirect, url_for, session
import sqlite3
import os
import requests
from dotenv import load_dotenv
import jwt
from slack_bolt.adapter.flask import SlackRequestHandler
from archivebot import app
handler = SlackRequestHandler(app)
import datetime
from sentence_transformers import SentenceTransformer
import numpy as np
import openai
from datetime import timedelta


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

# disabled slack token validation and replaced with jwt token validation
# def verify_token(headers):
#     response = requests.get('https://slack.com/api/auth.test', headers=headers)
#     data = response.json()

#     if not data.get('ok'):
#         return False
    
#     # Verifica se il token è valido per il workspace e l'app specificati
#     if data.get('team_id') != EXPECTED_TEAM_ID:
#         return False
    
#     return True

@flask_app.route('/emoji', methods=['GET'])
def get_emoji():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    slack_token = verify_token_and_get_user(headers)['slack_token']

    if not headers or not user:
        return redirect(url_for('login'))
    
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})
    
    response = requests.get('https://slack.com/api/emoji.list', headers={'Authorization': 'Bearer ' + slack_token})
    print(response)
    print(response.json())
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
def whoami():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    username = get_username(user)
    conn = get_db_connection()
    status = conn.execute('SELECT * FROM optout WHERE user = ?', (user,)).fetchone()
    opted_out_ai = conn.execute('SELECT * FROM optout_ai WHERE user = ?', (user,)).fetchone()
    if opted_out_ai:
        opted_out_ai = True
    else:
        opted_out_ai = False

    conn.close()
    if not headers or not user:
        return redirect(url_for('login'))
    if status:
        return get_response({'user_id': user, 'username': username, 'opted_out': True, 'opted_out_ai': opted_out_ai})
    return get_response({'user_id': user, 'username': username, 'opted_out': False, 'opted_out_ai': opted_out_ai})


def notify_users(users, text):
    for user in users:
        response = app.client.chat_postMessage(
            channel=user,
            text=text
        )

@flask_app.route('/optout', methods=['GET'])
def optout():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO optout (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)', (user,))
        cursor.execute('UPDATE messages SET message = "User opted out of archiving. This message has been deleted", user = "USLACKBOT", permalink = "" WHERE user = ?', (user,))
        conn.commit()

        notify_users(
            [
                'U011PQ7RHRT',
                'U011MV24J2W',
                'U0129HFHRJ4',
                'U011N8WRRD0',
                'U011Z26G449',
                'U011CKQ7D71',
                'U011KE4BF0W',
                'U011PN35BHT'
                ],
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
def get_channels():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    conn = get_db_connection()
    channels = conn.execute('SELECT * FROM channels WHERE is_private = 0 ORDER BY name').fetchall()
    conn.close()
    return get_response([dict(ix) for ix in channels])

@flask_app.route('/users', methods=['GET'])
def get_users():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})
    
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
def get_messages(channel_id):
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})
    
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
def get_thread(message_id):
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

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

@flask_app.route('/search', methods=['GET'])
def search_messages():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

    query = request.args.get('query', '')
    conn = get_db_connection()
    messages = conn.execute('''
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
        WHERE (message LIKE ? OR users.name LIKE ?)
        AND user NOT IN (SELECT user FROM optout)
        ORDER BY timestamp DESC
        ''', 
        ('%' + query + '%','%' + query + '%',)).fetchall()
    conn.close()
    return get_response([dict(ix) for ix in messages])



@flask_app.route('/searchV2', methods=['GET'])
def search_messages_V2():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

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

    sql += ' ORDER BY messages.timestamp DESC'

    messages = conn.execute(sql, params).fetchall()
    conn.close()

    return get_response([dict(ix) for ix in messages])


@flask_app.route('/searchEmbeddings', methods=['GET'])
def search_messages_embeddings():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

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

@flask_app.route('/generate_digest', methods=['POST'])
def generate_digest():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

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
            formatted_messages += f"\nThread started at {datetime.datetime.fromtimestamp(float(current_thread)).strftime('%Y-%m-%d %H:%M:%S')}:\n"

        # Format the message
        timestamp = datetime.datetime.fromtimestamp(float(message['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
        formatted_messages += f"[{timestamp}] {message['username']}: {message['message']}\n"

    max_chars = 256000  # Approximate character limit (128000 tokens * 2 chars per token)
    if len(formatted_messages) > max_chars:
        formatted_messages = formatted_messages[:max_chars] + "...\n(truncated due to length)"
    
    
    # Generate summary using OpenAI
    openai.api_key = os.getenv('OPENAI_API_KEY')
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Sei un assistente che riassume le conversazioni di un workspace di Slack. Fornirai riassunti molto dettagliati, usando almeno 2000 parole, e sempre in italiano."},
            {"role": "user", "content": f"""In allegato ti invio il tracciato delle ultime 24 ore di un workspace Slack. 
                L'estrazione contiene tutti i messaggi inviati sul workspace, suddivisi in canali e thread. 
                Sono inclusi anche i thread più vecchi di 24 ore se hanno ricevuto una risposta nelle ultime 24 ore. 
                Il tuo compito è creare un digest discorsivo ma abbastanza dettagliato da fornire agli utenti. 
                Racconta cosa è successo su ogni canale in maniera descrittiva, ma enfatizza le conversazioni più coinvolgenti e partecipate se ci sono state, gli argomenti trattati, fornendo un buon numero di dettagli, 
                inclusi i nomi dei partecipanti alle varie conversazioni, evidenziati. (Attenzione: il nome è sempre prima del messaggio, non dopo)
                La risposta deve essere in formato markdown.
                Inserisci sempre un link alle conversazioni più coinvolgenti, il link è nel formato [link](https://slack-archive.sferait.org/getlink?timestamp=MESSAGE_TIMESTAMP).
                PRIMA del riassunto, inserisci una sezione in cui fai un preambolo dicendo quali sono stati i canali più attivi, quali i thread più discussi, e quali sono stati gli argomenti più trattati.
                Evita commenti rispetto alla vivacita o varietà del gruppo, nei preamboli e conclusioni parla dei fatti e delle conversazioni avvenute, non giudicarne il contenuto. 
                {formatted_messages}"""}
        ],
       max_tokens=4096,
       request_timeout=300
    )
    
    summary = response.choices[0].message.content

    # Calculate the period
    end_date = datetime.datetime.utcnow()
    start_date = end_date - timedelta(days=1)
    period = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"

    # Insert the digest into the database
    conn.execute('''
    INSERT INTO digests (timestamp, period, digest, posts)
    VALUES (?, ?, ?, ?)
    ''', (datetime.datetime.utcnow().isoformat(), period, summary, formatted_messages))
    conn.commit()
    conn.close()

    # If send_to_channel is set, send the digest to the channel
    if send_to_channel:
        try:
            message = f"*Digest for {period}*\n\n{summary} \n\n Puoi trovare maggiori informazioni ed eseguire opt-out dalle funzioni AI qui: https://sferaarchive-client.vercel.app/"
            response = app.client.chat_postMessage(
                channel='C011CK2HYP9',
                text=message,
                parse="full"
            )
            if not response['ok']:
                return get_response({'status': 'error', 'message': 'Failed to send digest to channel'})
        except Exception as e:
            return get_response({'status': 'error', 'message': f'Error sending digest to channel: {str(e)}'})

    return get_response({'status': 'success', 'digest': summary, 'period': period})


@flask_app.route('/digest_details', methods=['POST'])
def digest_details():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))
    if check_optout(user):
        return get_response({'error': 'User opted out of archiving'})

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
    openai.api_key = os.getenv('OPENAI_API_KEY')
    response = openai.ChatCompletion.create(
        model="gpt-4o", # gpt-4o non mini per essere più precisi
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
        request_timeout=300
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
def optout_ai():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)['user_id']
    if not headers or not user:
        return redirect(url_for('login'))

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

    return get_response({'user_id': user, 'opted_out': ret})

@flask_app.route('/getlink', methods=['GET'])
def get_link():
    timestamp = request.args.get('timestamp')
    if not timestamp:
        return jsonify({'error': 'No timestamp provided'}), 400

    conn = get_db_connection()
    try:
        message = conn.execute('SELECT permalink FROM messages WHERE timestamp LIKE ?', ('%'+timestamp+'%',)).fetchone()
        if message and message['permalink']:
            return redirect(message['permalink'])
        else:
            return jsonify({'error': 'Message not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    flask_app.run(debug=debug_mode)