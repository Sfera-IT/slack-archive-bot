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
    conn.close()
    if not headers or not user:
        return redirect(url_for('login'))
    if status:
        return get_response({'user_id': user, 'username': username, 'opted_out': True})
    return get_response({'user_id': user, 'username': username, 'opted_out': False})


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
        SELECT messages.*, users.name as user_name 
        FROM messages 
        JOIN users ON messages.user = users.id 
        WHERE channel = ? 
        AND (thread_ts IS NULL OR thread_ts = timestamp)
        AND user NOT IN (SELECT user FROM optout)
        ORDER BY timestamp DESC 
        LIMIT ? OFFSET ?''', 
        (channel_id, limit, offset)).fetchall()
    conn.close()
    
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
        SELECT messages.*, users.name as user_name 
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
        SELECT messages.*, users.name as user_name 
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
    SELECT DISTINCT messages.*, users.name as user_name, channels.name as channel_name
    FROM messages
    JOIN users ON messages.user = users.id
    JOIN channels ON messages.channel = channels.id
    LEFT JOIN members ON messages.channel = members.channel
    WHERE 1=1
    '''
    params = []

    # Add conditions based on provided parameters
    if query:
        sql += ' AND messages.message LIKE ?'
        params.append('%' + query + '%')

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

if __name__ == '__main__':
    flask_app.run(debug=True)