from flask import Flask, jsonify, request, redirect, url_for, session
import sqlite3
import os
import requests
from dotenv import load_dotenv
import jwt
from slack_bolt.adapter.flask import SlackRequestHandler
from archivebot import app
handler = SlackRequestHandler(app)

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
    conn = sqlite3.connect('/data/slack.sqlite')
    conn.row_factory = sqlite3.Row
    return conn

@flask_app.route('/login')
def login():
    slack_auth_url = (
        f"https://slack.com/oauth/v2/authorize?client_id={CLIENT_ID}"
        f"&scope={OAUTH_SCOPE}&user_scope=identity.basic"
        f"&redirect_uri={url_for('oauth_callback', _external=True, _scheme='https')}"
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

    # create a jwt token
    jwt_token = jwt.encode({'user_id': response_data['authed_user']['id']}, flask_app.secret_key, algorithm='HS256')
    
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

def verify_token_and_get_user(headers):
    token = headers['Authorization']
    # remove Bearer
    token = token.split('Bearer ')[1]

    try:
        decoded = jwt.decode(token, flask_app.secret_key, algorithms=['HS256'])
        user_id = decoded['user_id']
        # check if user_id exists in the database
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        if not user:
            return False
        else:
            return decoded['user_id']
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False
    return True

@flask_app.route('/channels', methods=['OPTIONS'])
def get_channels_options():
    return get_response({})

@flask_app.route('/channels', methods=['GET'])
def get_channels():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)
    if not headers or not user:
        return redirect(url_for('login'))
    conn = get_db_connection()
    channels = conn.execute('SELECT * FROM channels WHERE is_private = 0 ORDER BY name').fetchall()
    conn.close()
    return get_response([dict(ix) for ix in channels])

@flask_app.route('/messages/<channel_id>', methods=['GET'])
def get_messages(channel_id):
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)
    if not headers or not user:
        return redirect(url_for('login'))

    conn = get_db_connection()
    offset = request.args.get('offset', 0)
    limit = request.args.get('limit', 20)
    messages = conn.execute('''
        SELECT messages.*, users.name as user_name 
        FROM messages 
        JOIN users ON messages.user = users.id 
        WHERE channel = ? 
        AND thread_ts is NULL
        ORDER BY timestamp DESC 
        LIMIT ? OFFSET ?''', 
        (channel_id, limit, offset)).fetchall()
    conn.close()
    
    return get_response([dict(ix) for ix in messages])

@flask_app.route('/thread/<message_id>', methods=['GET'])
def get_thread(message_id):
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)
    if not headers or not user:
        return redirect(url_for('login'))

    conn = get_db_connection()
    thread = conn.execute('''
        SELECT messages.*, users.name as user_name 
        FROM messages 
        JOIN users ON messages.user = users.id 
        WHERE messages.timestamp = ?
        OR messages.thread_ts = ?''', 
        (message_id, message_id)).fetchall()
    conn.close()
    return get_response([dict(ix) for ix in thread])

@flask_app.route('/search', methods=['GET'])
def search_messages():
    headers = get_slack_headers()
    user = verify_token_and_get_user(headers)
    if not headers or not user:
        return redirect(url_for('login'))

    query = request.args.get('query', '')
    conn = get_db_connection()
    messages = conn.execute('''
        SELECT messages.*, users.name as user_name 
        FROM messages 
        JOIN users ON messages.user = users.id 
        WHERE message LIKE ?''', 
        ('%' + query + '%',)).fetchall()
    conn.close()
    return get_response([dict(ix) for ix in messages])

if __name__ == '__main__':
    flask_app.run(debug=True)