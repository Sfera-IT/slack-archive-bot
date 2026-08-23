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
import hashlib
import hmac
import logging
import secrets
import tempfile
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)
from sentence_transformers import SentenceTransformer
import numpy as np
import openai
from datetime import timedelta
import re
from functools import wraps
from flask import g
import csv
from io import StringIO
import openai
from pydub import AudioSegment
import io
from openai import OpenAI
from ai_agent import generate_text_response
from archive_search import OPTED_OUT_TEXT, build_archive_url, is_valid_slack_timestamp
from pathlib import Path
from flask import send_file
from privacy import purge_archived_user_data
from runtime_config import resolve_database_path

load_dotenv()

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

DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")
APP_VERSION = os.getenv("APP_VERSION", "2.2.0")
APP_REVISION = os.getenv("APP_REVISION", "unknown")
OAUTH_STATE_TTL_SECONDS = 600
MAX_OAUTH_STATES = 2000
MAX_QUERY_CHARS = 500
MAX_QUERY_TERMS = 20
MAX_CHAT_MESSAGE_CHARS = 4000
MAX_CHAT_ITEMS = 100
MAX_CHAT_ITEM_CHARS = 4000
MAX_CHAT_PROMPT_CHARS = 100_000
WEB_AI_LOCK_TTL_SECONDS = 7200
SLACK_CHANNEL_ID_RE = re.compile(r'^[A-Z][A-Z0-9]{1,31}$')
SLACK_TEAM_ID_RE = re.compile(r'^T[A-Z0-9]{8,15}$')
SLACK_USER_ID_RE = re.compile(r'^U[A-Z0-9]{8,31}$')
EMBEDDING_MODEL_NAME = os.getenv(
    'EMBEDDING_MODEL_NAME',
    'sentence-transformers/paraphrase-MiniLM-L6-v2',
)
EMBEDDING_MODEL_REVISION = os.getenv(
    'EMBEDDING_MODEL_REVISION',
    'c9a2bfebc254878aee8c3aca9e6844d5bbb102d1',
)
PODCAST_AUDIO_PATH = os.getenv('PODCAST_AUDIO_PATH', 'podcast.mp3')
_embedding_model = None
_embedding_model_lock = threading.Lock()

def auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        headers = get_slack_headers()
        g.headers = headers
        user_info = verify_token_and_get_user(headers) if headers else None
        if not user_info:
            return get_response({'error': 'Authentication required'}), 401
        
        g.user_id = user_info['user_id']
        g.username = get_username(g.user_id)
        
        conn = get_db_connection()
        g.opted_out = conn.execute('SELECT * FROM optout WHERE user = ?', (g.user_id,)).fetchone() is not None
        g.opted_out_ai = conn.execute('SELECT * FROM optout_ai WHERE user = ?', (g.user_id,)).fetchone() is not None
        conn.close()
        
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user_id not in ADMIN_USERS:
            return get_response({'error': 'Administrator access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def optin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user_id in ADMIN_USERS:
            return f(*args, **kwargs)
        if check_optout(g.user_id):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


flask_app = Flask(__name__)
flask_app.secret_key = os.getenv('SECRET_KEY')
flask_app.config['PREFERRED_URL_SCHEME'] = 'https'
flask_app.config['MAX_CONTENT_LENGTH'] = 512 * 1024
flask_app.config['SESSION_COOKIE_HTTPONLY'] = True
flask_app.config['SESSION_COOKIE_SECURE'] = True
flask_app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Attenzione, sono i dati dell'applicazione slack-archive-gui e non slack-archive-bot
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
OAUTH_SCOPE = os.getenv('OAUTH_SCOPE')
EXPECTED_TEAM_ID = os.getenv('EXPECTED_TEAM_ID')
CLIENT_URL = os.getenv('CLIENT_URL', 'https://sferaarchive-client.vercel.app/')
OAUTH_REDIRECT_URI = os.getenv(
    'OAUTH_REDIRECT_URI',
    'https://slack-archive.sferait.org/oauth_callback',
)
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
SLACK_SIGNING_SECRET = os.getenv('SLACK_SIGNING_SECRET')


def _configured_client_origin():
    parsed = urlsplit(CLIENT_URL)
    if not _is_secure_application_url(parsed):
        return None
    return f'{parsed.scheme}://{parsed.netloc}'


def _is_secure_application_url(parsed):
    if parsed.username or parsed.password or not parsed.netloc:
        return False
    if parsed.scheme == 'https':
        return True
    return parsed.scheme == 'http' and (parsed.hostname or '').lower() in {
        'localhost',
        '127.0.0.1',
        '::1',
    }


def _validate_runtime_configuration():
    """Fail at startup instead of discovering broken auth on the first request."""
    required = {
        'SECRET_KEY': flask_app.secret_key,
        'CLIENT_ID': CLIENT_ID,
        'CLIENT_SECRET': CLIENT_SECRET,
        'OAUTH_SCOPE': OAUTH_SCOPE,
        'EXPECTED_TEAM_ID': EXPECTED_TEAM_ID,
        'SLACK_BOT_TOKEN': SLACK_BOT_TOKEN,
        'SLACK_SIGNING_SECRET': SLACK_SIGNING_SECRET,
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise RuntimeError(
            'Missing required runtime configuration: ' + ', '.join(missing)
        )
    if len(str(flask_app.secret_key)) < 32:
        raise RuntimeError('SECRET_KEY must contain at least 32 characters')
    if not SLACK_TEAM_ID_RE.fullmatch(str(EXPECTED_TEAM_ID)):
        raise RuntimeError('EXPECTED_TEAM_ID is not a valid Slack team ID')
    if _configured_client_origin() is None:
        raise RuntimeError('CLIENT_URL must use HTTPS (HTTP is allowed only locally)')
    if not _is_secure_application_url(urlsplit(OAUTH_REDIRECT_URI)):
        raise RuntimeError(
            'OAUTH_REDIRECT_URI must use HTTPS (HTTP is allowed only locally)'
        )
    configured_bot_user = os.getenv('SLACK_BOT_USER_ID')
    if configured_bot_user and not SLACK_USER_ID_RE.fullmatch(configured_bot_user):
        raise RuntimeError('SLACK_BOT_USER_ID is not a valid Slack user ID')
    resolve_database_path()


_validate_runtime_configuration()


def _validated_return_to(value):
    """Return an allowlisted frontend URL, never an arbitrary redirect target."""
    client_origin = _configured_client_origin()
    fallback = CLIENT_URL if client_origin else '/'
    if not value:
        return fallback

    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return fallback
    if not parsed.scheme and not parsed.netloc:
        if not value.startswith('/') or value.startswith('//'):
            return fallback
        return f'{client_origin}{value}' if client_origin else fallback
    if parsed.scheme not in {'http', 'https'}:
        return fallback
    candidate_origin = f'{parsed.scheme}://{parsed.netloc}'
    return value if client_origin and candidate_origin == client_origin else fallback


def _frontend_redirect_with_token(return_to, token):
    parsed = urlsplit(_validated_return_to(return_to))
    return urlunsplit(parsed._replace(fragment=urlencode({'token': token})))

# default handler for slack events, through archivebot.py
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


def _health_payload(*, require_identity):
    try:
        conn = get_db_connection()
        try:
            conn.execute('SELECT 1').fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {
            'status': 'unhealthy',
            'version': APP_VERSION,
            'revision': APP_REVISION,
        }, 503
    identity_ready = bool(getattr(app, '_bot_identity_verified', False))
    status = 'ready' if identity_ready else 'degraded'
    code = 200 if identity_ready or not require_identity else 503
    return {
        'status': status,
        'version': APP_VERSION,
        'revision': APP_REVISION,
    }, code


@flask_app.route('/healthz', methods=['GET'])
def healthz():
    payload, status = _health_payload(require_identity=False)
    response = get_response(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@flask_app.route('/readyz', methods=['GET'])
def readyz():
    payload, status = _health_payload(require_identity=True)
    response = get_response(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@flask_app.after_request
def apply_security_headers(response):
    allowed_origin = _configured_client_origin()
    request_origin = request.headers.get('Origin')
    if allowed_origin:
        response.headers['Vary'] = 'Origin'
    if allowed_origin and request_origin == allowed_origin:
        response.headers['Access-Control-Allow-Origin'] = allowed_origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    if request.endpoint in {'login', 'oauth_callback'}:
        response.headers['Cache-Control'] = 'no-store'
    elif getattr(g, 'user_id', None):
        response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
    return response


def get_response(data):
    response = jsonify(data)
    return response


def log_and_return_error(e: Exception, status_code: int = 500):
    """Log exception with error ID and return generic error response."""
    error_id = uuid.uuid4().hex[:8]
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    logger.error(
        '[ERROR:%s] %s - %s',
        error_id,
        timestamp,
        type(e).__name__,
    )
    return get_response({'error': f'Internal error [{error_id}] at {timestamp}'}), status_code


def get_db_connection():
    db_path = resolve_database_path()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _oauth_state_digest(state):
    # OAuth state values are high-entropy nonces, not passwords. Key the
    # persisted digest as defense in depth so a leaked state table cannot be
    # used to validate captured values without the application secret.
    return hmac.new(
        str(flask_app.secret_key).encode('utf-8'),
        state.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _store_oauth_state(state, return_to):
    conn = get_db_connection()
    now = int(time.time())
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                return_to TEXT NOT NULL
            )
        ''')
        conn.execute(
            'DELETE FROM oauth_states WHERE created_at <= ?',
            (now - OAUTH_STATE_TTL_SECONDS,),
        )
        excess = conn.execute('SELECT COUNT(*) FROM oauth_states').fetchone()[0]
        excess = max(0, excess - MAX_OAUTH_STATES + 1)
        if excess:
            conn.execute(
                '''
                DELETE FROM oauth_states
                WHERE state_hash IN (
                    SELECT state_hash FROM oauth_states
                    ORDER BY created_at ASC, state_hash ASC
                    LIMIT ?
                )
                ''',
                (excess,),
            )
        conn.execute(
            'INSERT INTO oauth_states(state_hash, created_at, return_to) VALUES (?, ?, ?)',
            (_oauth_state_digest(state), now, return_to),
        )
        conn.commit()
    finally:
        conn.close()


def _consume_oauth_state(state):
    """Atomically consume a server-side state nonce and return its redirect."""
    conn = get_db_connection()
    now = int(time.time())
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'DELETE FROM oauth_states WHERE created_at <= ?',
            (now - OAUTH_STATE_TTL_SECONDS,),
        )
        state_hash = _oauth_state_digest(state)
        row = conn.execute(
            'SELECT return_to FROM oauth_states WHERE state_hash = ?',
            (state_hash,),
        ).fetchone()
        if row:
            conn.execute('DELETE FROM oauth_states WHERE state_hash = ?', (state_hash,))
        conn.commit()
        return row['return_to'] if row else None
    finally:
        conn.close()


def can_access_channel(conn, channel_id, user_id):
    row = conn.execute(
        '''
        SELECT 1
        FROM channels
        WHERE id = ? AND (
            COALESCE(channels.is_private, 1) = 0
            OR EXISTS (
                SELECT 1 FROM members visibility_member
                WHERE visibility_member.channel = channels.id
                  AND visibility_member.user = ?
            )
        )
        LIMIT 1
        ''',
        (channel_id, user_id),
    ).fetchone()
    return row is not None


def _bounded_query_arg(name, *, maximum=MAX_QUERY_CHARS):
    value = request.args.get(name, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{name} exceeds the maximum length')
    return value


def _parse_iso_timestamp(value, name):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError) as exception:
        raise ValueError(f'{name} is not a valid ISO-8601 timestamp') from exception


def _validated_chat_items(value, name):
    if not isinstance(value, list) or len(value) > MAX_CHAT_ITEMS:
        raise ValueError(f'{name} must be a bounded list')
    validated = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f'{name} contains an invalid item')
        user_name = item.get('user_name', '')
        message = item.get('message', '')
        if not isinstance(user_name, str) or not isinstance(message, str):
            raise ValueError(f'{name} contains an invalid item')
        if len(user_name) > 200 or len(message) > MAX_CHAT_ITEM_CHARS:
            raise ValueError(f'{name} contains an oversized item')
        validated.append({'user_name': user_name, 'message': message})
    return validated


def _validated_context_refs(value):
    if not isinstance(value, list) or len(value) > MAX_CHAT_ITEMS:
        raise ValueError('context_refs must be a bounded list')
    validated = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('context_refs contains an invalid item')
        channel = item.get('channel')
        timestamp = item.get('timestamp')
        if (
            not isinstance(channel, str)
            or not SLACK_CHANNEL_ID_RE.fullmatch(channel)
            or not isinstance(timestamp, str)
            or not is_valid_slack_timestamp(timestamp)
        ):
            raise ValueError('context_refs contains an invalid item')
        key = (channel, timestamp)
        if key not in seen:
            seen.add(key)
            validated.append({'channel': channel, 'timestamp': timestamp})
    return validated


def _load_ai_context_from_refs(refs, user_id):
    """Resolve archive context server-side with ACL and both privacy controls."""
    conn = get_db_connection()
    rows = []
    try:
        for ref in refs:
            row = conn.execute(
                '''
                SELECT users.name AS user_name, messages.message
                FROM messages
                JOIN users ON users.id = messages.user
                JOIN channels ON channels.id = messages.channel
                WHERE messages.channel = ?
                  AND messages.timestamp = ?
                  AND messages.user NOT IN (SELECT user FROM optout)
                  AND messages.user NOT IN (SELECT user FROM optout_ai)
                  AND (
                      COALESCE(channels.is_private, 1) = 0
                      OR EXISTS (
                          SELECT 1 FROM members visibility_member
                          WHERE visibility_member.channel = channels.id
                            AND visibility_member.user = ?
                      )
                  )
                LIMIT 1
                ''',
                (ref['channel'], ref['timestamp'], user_id),
            ).fetchone()
            if row:
                rows.append(dict(row))
    finally:
        conn.close()
    return rows


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        with _embedding_model_lock:
            if _embedding_model is None:
                _embedding_model = SentenceTransformer(
                    EMBEDDING_MODEL_NAME,
                    revision=EMBEDDING_MODEL_REVISION,
                )
    return _embedding_model


def _ensure_web_ai_tables(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS web_ai_requests (
            user_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            requested_at INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_web_ai_requests_lookup
        ON web_ai_requests(user_id, operation, requested_at)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS web_ai_locks (
            job_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )
    ''')
    conn.commit()


def _consume_web_ai_quota(user_id, operation, *, minute_limit, hour_limit):
    now = int(time.time())
    conn = get_db_connection()
    try:
        _ensure_web_ai_tables(conn)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM web_ai_requests WHERE requested_at <= ?', (now - 3600,))
        minute_rows = conn.execute(
            '''SELECT COUNT(*), MIN(requested_at) FROM web_ai_requests
               WHERE user_id = ? AND operation = ? AND requested_at > ?''',
            (user_id, operation, now - 60),
        ).fetchone()
        hour_rows = conn.execute(
            '''SELECT COUNT(*), MIN(requested_at) FROM web_ai_requests
               WHERE user_id = ? AND operation = ? AND requested_at > ?''',
            (user_id, operation, now - 3600),
        ).fetchone()
        retry_after = 0
        if minute_rows[0] >= minute_limit:
            retry_after = max(retry_after, int(minute_rows[1]) + 60 - now + 1)
        if hour_rows[0] >= hour_limit:
            retry_after = max(retry_after, int(hour_rows[1]) + 3600 - now + 1)
        if retry_after:
            conn.rollback()
            return False, retry_after
        conn.execute(
            'INSERT INTO web_ai_requests(user_id, operation, requested_at) VALUES (?, ?, ?)',
            (user_id, operation, now),
        )
        conn.commit()
        return True, 0
    finally:
        conn.close()


def web_ai_rate_limited(operation, *, minute_limit=2, hour_limit=10):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                allowed, retry_after = _consume_web_ai_quota(
                    g.user_id,
                    operation,
                    minute_limit=minute_limit,
                    hour_limit=hour_limit,
                )
            except sqlite3.Error as exception:
                return log_and_return_error(exception, 503)
            if not allowed:
                response = get_response({'error': 'Rate limit exceeded'})
                response.headers['Retry-After'] = str(retry_after)
                return response, 429
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def _claim_web_ai_lock(job_name):
    now = int(time.time())
    owner = uuid.uuid4().hex
    conn = get_db_connection()
    try:
        _ensure_web_ai_tables(conn)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM web_ai_locks WHERE expires_at <= ?', (now,))
        try:
            conn.execute(
                'INSERT INTO web_ai_locks(job_name, owner, expires_at) VALUES (?, ?, ?)',
                (job_name, owner, now + WEB_AI_LOCK_TTL_SECONDS),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        conn.commit()
        return owner
    finally:
        conn.close()


def _release_web_ai_lock(job_name, owner):
    conn = get_db_connection()
    try:
        conn.execute(
            'DELETE FROM web_ai_locks WHERE job_name = ? AND owner = ?',
            (job_name, owner),
        )
        conn.commit()
    finally:
        conn.close()


def singleflight_web_ai_job(job_name):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                owner = _claim_web_ai_lock(job_name)
            except sqlite3.Error as exception:
                return log_and_return_error(exception, 503)
            if owner is None:
                return get_response({'error': 'A matching AI job is already running'}), 409
            try:
                return f(*args, **kwargs)
            finally:
                try:
                    _release_web_ai_lock(job_name, owner)
                except sqlite3.Error as exception:
                    logger.error(
                        'Failed to release web AI lock %s: %s',
                        job_name,
                        type(exception).__name__,
                    )
        return decorated_function
    return decorator


@flask_app.route('/login')
def login():
    state = secrets.token_urlsafe(32)
    return_to = _validated_return_to(request.args.get('return_to'))
    try:
        _store_oauth_state(state, return_to)
    except sqlite3.Error as exception:
        return log_and_return_error(exception, 503)
    session['oauth_request'] = {
        'state': state,
        'created_at': int(time.time()),
    }
    slack_auth_url = 'https://slack.com/oauth/v2/authorize?' + urlencode({
        'client_id': CLIENT_ID or '',
        'scope': OAUTH_SCOPE or '',
        'user_scope': 'identity.basic',
        'redirect_uri': OAUTH_REDIRECT_URI,
        'state': state,
    })
    return redirect(slack_auth_url)


@flask_app.route('/oauth_callback')
def oauth_callback():
    code = request.args.get('code')
    supplied_state = request.args.get('state', '')
    oauth_request = session.pop('oauth_request', None)
    if not code or not isinstance(oauth_request, dict):
        return 'Authorization failed.', 400

    expected_state = str(oauth_request.get('state') or '')
    created_at = oauth_request.get('created_at')
    try:
        state_age = int(time.time()) - int(created_at)
        state_is_fresh = 0 <= state_age <= OAUTH_STATE_TTL_SECONDS
    except (TypeError, ValueError):
        state_is_fresh = False
    if (
        not expected_state
        or not supplied_state
        or not state_is_fresh
        or not hmac.compare_digest(expected_state, supplied_state)
    ):
        if expected_state:
            try:
                _consume_oauth_state(expected_state)
            except sqlite3.Error:
                logger.error('Failed to invalidate rejected OAuth state')
        return 'Authorization failed.', 400

    try:
        return_to = _consume_oauth_state(supplied_state)
    except sqlite3.Error as exception:
        return log_and_return_error(exception, 503)
    if not return_to:
        return 'Authorization failed.', 400

    if not EXPECTED_TEAM_ID:
        logger.error('OAuth rejected: EXPECTED_TEAM_ID is not configured')
        return 'OAuth configuration error.', 503

    try:
        response = requests.post('https://slack.com/api/oauth.v2.access', data={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'code': code,
            'redirect_uri': OAUTH_REDIRECT_URI,
        }, timeout=10)
        response.raise_for_status()
        response_data = response.json()
    except (requests.RequestException, ValueError) as exception:
        logger.warning('Slack OAuth exchange failed: %s', type(exception).__name__)
        return 'Failed to authenticate with Slack.', 502

    if not response_data.get('ok'):
        return 'Failed to authenticate with Slack.', 400

    team_id = str((response_data.get('team') or {}).get('id') or '')
    user_id = str((response_data.get('authed_user') or {}).get('id') or '')
    if team_id != EXPECTED_TEAM_ID or not user_id:
        logger.warning('Slack OAuth rejected for unexpected workspace or missing user')
        return 'Failed to authenticate with Slack.', 403

    exp_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    jwt_token = jwt.encode(
        {'user_id': user_id, 'exp': exp_time},
        flask_app.secret_key,
        algorithm='HS256',
    )
    response = redirect(
        _frontend_redirect_with_token(return_to, jwt_token)
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


def get_slack_headers():
    # get headers from the request
    headers = request.headers
    if 'Authorization' in headers:
        return {'Authorization': headers['Authorization']}
    return None


@flask_app.route('/emoji', methods=['GET'])
@auth_required
def get_emoji():
    if not SLACK_BOT_TOKEN:
        logger.warning('Custom Slack emoji unavailable: bot token is not configured')
        return get_response({'ok': True, 'emoji': {}, 'degraded': True})
    try:
        response = requests.get(
            'https://slack.com/api/emoji.list',
            headers={'Authorization': 'Bearer ' + SLACK_BOT_TOKEN},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exception:
        logger.warning(
            'Custom Slack emoji lookup unavailable type=%s',
            type(exception).__name__,
        )
        return get_response({'ok': True, 'emoji': {}, 'degraded': True})

    if not data.get('ok'):
        logger.warning(
            'Custom Slack emoji lookup unavailable error=%s',
            str(data.get('error') or 'unknown')[:80],
        )
        return get_response({'ok': True, 'emoji': {}, 'degraded': True})
    
    return get_response(data)


def verify_token_and_get_user(headers):
    if not headers:
        return False
    authorization = str(headers.get('Authorization') or '')
    scheme, separator, token = authorization.partition(' ')
    if not separator or scheme.lower() != 'bearer' or not token.strip():
        return False

    try:
        decoded = jwt.decode(
            token.strip(),
            flask_app.secret_key,
            algorithms=['HS256'],
            options={'require': ['user_id', 'exp'], 'verify_exp': True},
        )
        if set(decoded) != {'user_id', 'exp'}:
            return False
        user_id = str(decoded['user_id'])
        # check if user_id exists in the database
        conn = get_db_connection()
        try:
            user = conn.execute('SELECT id FROM users WHERE id = ?', (user_id,)).fetchone()
        finally:
            conn.close()
        if not user:
            return False
        return {'user_id': user_id}
    except (jwt.PyJWTError, KeyError, TypeError):
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
        purge_archived_user_data(conn, user)
        conn.commit()

        try:
            Path(PODCAST_AUDIO_PATH).unlink(missing_ok=True)
        except OSError:
            logger.warning('Could not remove invalidated podcast audio')

        try:
            notify_admins(
                "L'utente <@" + user + "> ha scelto di non essere più archiviato."
            )
        except Exception:
            logger.warning('Could not notify administrators about archive opt-out')

    except Exception as e:
        if conn:
            conn.rollback()
        return log_and_return_error(e)
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
    try:
        users = conn.execute('''
            SELECT id, name, avatar, real_name, display_name, is_deleted
            FROM users
            WHERE id NOT IN (SELECT user FROM optout)
        ''').fetchall()
    finally:
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
    try:
        try:
            offset = int(request.args.get('offset', 0))
            limit = int(request.args.get('limit', 20))
        except (TypeError, ValueError):
            return get_response({'error': 'Invalid pagination'}), 400
        if offset < 0 or limit < 1 or limit > 100:
            return get_response({'error': 'Invalid pagination'}), 400
        if not can_access_channel(conn, channel_id, g.user_id):
            return get_response({'error': 'Channel not found'}), 404

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
            ORDER BY CAST(m.timestamp AS REAL) DESC
            LIMIT ? OFFSET ?
        ''', (channel_id, limit, offset)).fetchall()
        return get_response([dict(msg) for msg in messages])
    finally:
        conn.close()


def _fetch_thread(conn, channel_id, thread_ts):
    return conn.execute('''
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
        WHERE messages.channel = ?
          AND (messages.timestamp = ? OR messages.thread_ts = ?)
          AND messages.user NOT IN (SELECT user FROM optout)
        ORDER BY CAST(messages.timestamp AS REAL) ASC
    ''', (channel_id, thread_ts, thread_ts)).fetchall()


@flask_app.route('/thread/<channel_id>/<thread_ts>', methods=['GET'])
@auth_required
@optin_required
def get_thread_exact(channel_id, thread_ts):
    conn = get_db_connection()
    try:
        if not can_access_channel(conn, channel_id, g.user_id):
            return get_response({'error': 'Thread not found'}), 404
        thread = _fetch_thread(conn, channel_id, thread_ts)
        if not thread:
            return get_response({'error': 'Thread not found'}), 404
        return get_response([dict(row) for row in thread])
    finally:
        conn.close()


@flask_app.route('/thread/<message_id>', methods=['GET'])
@auth_required
@optin_required
def get_thread(message_id):
    conn = get_db_connection()
    try:
        channels = conn.execute(
            '''
            SELECT DISTINCT messages.channel
            FROM messages
            JOIN channels ON channels.id = messages.channel
            WHERE (messages.timestamp = ? OR messages.thread_ts = ?)
              AND (
                  COALESCE(channels.is_private, 1) = 0
                  OR EXISTS (
                      SELECT 1 FROM members visibility_member
                      WHERE visibility_member.channel = channels.id
                        AND visibility_member.user = ?
                  )
              )
            ''',
            (message_id, message_id, g.user_id),
        ).fetchall()
        if not channels:
            return get_response({'error': 'Thread not found'}), 404
        if len(channels) != 1:
            return get_response({
                'error': 'Ambiguous thread; use /thread/<channel>/<thread_ts>'
            }), 409
        thread = _fetch_thread(conn, channels[0]['channel'], message_id)
        return get_response([dict(row) for row in thread])
    finally:
        conn.close()


@flask_app.route('/searchV2', methods=['GET'])
@auth_required
@optin_required
def search_messages_V2():
    try:
        query = _bounded_query_arg('query')
        user_name = _bounded_query_arg('user_name', maximum=200)
        channel_name = _bounded_query_arg('channel_name', maximum=200)
        start_timestamp = _parse_iso_timestamp(
            _bounded_query_arg('start_time', maximum=64), 'start_time'
        )
        end_timestamp = _parse_iso_timestamp(
            _bounded_query_arg('end_time', maximum=64), 'end_time'
        )
    except ValueError:
        return get_response({'error': 'Invalid search parameters'}), 400

    conn = get_db_connection()
    try:
        sql = '''
        SELECT DISTINCT
            messages.message,
            messages.user,
            messages.channel,
            messages.timestamp,
            messages.permalink,
            messages.thread_ts,
            users.name as user_name,
            channels.name as channel_name
        FROM messages
        JOIN users ON messages.user = users.id
        JOIN channels ON messages.channel = channels.id
        WHERE (
            COALESCE(channels.is_private, 1) = 0
            OR EXISTS (
                SELECT 1 FROM members visibility_member
                WHERE visibility_member.channel = channels.id
                  AND visibility_member.user = ?
            )
        )
          AND messages.user NOT IN (SELECT user FROM optout)
        '''
        params = [g.user_id]

        if query:
            if query.startswith('"') and query.endswith('"'):
                query = query[1:-1]
                sql += ' AND messages.message LIKE ?'
                params.append('%' + query + '%')
            else:
                terms = query.split()
                if len(terms) > MAX_QUERY_TERMS:
                    return get_response({'error': 'Too many search terms'}), 400
                for term in terms:
                    sql += ' AND messages.message LIKE ?'
                    params.append('%' + term + '%')

        if user_name:
            sql += ' AND users.name LIKE ?'
            params.append('%' + user_name + '%')
        if channel_name:
            sql += ' AND channels.name LIKE ?'
            params.append('%' + channel_name + '%')
        if start_timestamp is not None:
            sql += ' AND CAST(messages.timestamp AS FLOAT) >= ?'
            params.append(start_timestamp)
        if end_timestamp is not None:
            sql += ' AND CAST(messages.timestamp AS FLOAT) <= ?'
            params.append(end_timestamp)

        sql += ' ORDER BY CAST(messages.timestamp AS REAL) DESC LIMIT 2000'
        messages = conn.execute(sql, params).fetchall()
        return get_response([dict(row) for row in messages])
    finally:
        conn.close()


@flask_app.route('/searchEmbeddings', methods=['GET'])
@auth_required
@optin_required
@web_ai_rate_limited('search_embeddings', minute_limit=5, hour_limit=30)
def search_messages_embeddings():
    try:
        query = _bounded_query_arg('query')
        user_name = _bounded_query_arg('user_name', maximum=200)
        channel_name = _bounded_query_arg('channel_name', maximum=200)
        start_timestamp = _parse_iso_timestamp(
            _bounded_query_arg('start_time', maximum=64), 'start_time'
        )
        end_timestamp = _parse_iso_timestamp(
            _bounded_query_arg('end_time', maximum=64), 'end_time'
        )
    except ValueError:
        return get_response({'error': 'Invalid search parameters'}), 400
    if not query.strip():
        return get_response({'error': 'No query provided'}), 400

    conn = get_db_connection()
    try:
        sql = '''
        SELECT DISTINCT
            messages.message,
            messages.user,
            messages.channel,
            messages.timestamp,
            messages.permalink,
            messages.thread_ts,
            messages.embeddings,
            users.name as user_name,
            channels.name as channel_name
        FROM messages
        JOIN users ON messages.user = users.id
        JOIN channels ON messages.channel = channels.id
        WHERE messages.embeddings IS NOT NULL
          AND (
              COALESCE(channels.is_private, 1) = 0
              OR EXISTS (
                  SELECT 1 FROM members visibility_member
                  WHERE visibility_member.channel = channels.id
                    AND visibility_member.user = ?
              )
          )
          AND messages.user NOT IN (SELECT user FROM optout)
          AND messages.user NOT IN (SELECT user FROM optout_ai)
          AND messages.user != 'USLACKBOT'
          AND messages.message != ?
        '''
        params = [g.user_id, OPTED_OUT_TEXT]

        if user_name:
            sql += ' AND users.name LIKE ?'
            params.append('%' + user_name + '%')
        if channel_name:
            sql += ' AND channels.name LIKE ?'
            params.append('%' + channel_name + '%')
        if start_timestamp is not None:
            sql += ' AND CAST(messages.timestamp AS FLOAT) >= ?'
            params.append(start_timestamp)
        if end_timestamp is not None:
            sql += ' AND CAST(messages.timestamp AS FLOAT) <= ?'
            params.append(end_timestamp)
        sql += ' ORDER BY CAST(messages.timestamp AS REAL) DESC LIMIT 5000'
        messages = [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    model = _get_embedding_model()
    query_embedding = model.encode(query)
    distances = []
    for row in messages:
        embedding_blob = row['embeddings']
        if not isinstance(embedding_blob, (bytes, bytearray, memoryview)):
            continue
        item_size = np.dtype(np.float32).itemsize
        if len(embedding_blob) == 0 or len(embedding_blob) % item_size:
            continue
        embedding = np.frombuffer(embedding_blob, dtype=np.float32)
        if embedding.size == 0 or not np.all(np.isfinite(embedding)):
            continue
        denominator = np.linalg.norm(query_embedding) * np.linalg.norm(embedding)
        if not denominator or query_embedding.shape != embedding.shape:
            continue
        distance = np.dot(query_embedding, embedding) / denominator
        row['distance'] = distance
        row.pop('embeddings')
        distances.append(row)

    distances.sort(key=lambda x: x['distance'], reverse=True)
    distances = distances[:100]
    for d in distances:
        d['distance'] = str(d['distance'])

    return get_response(distances)

def generate_podcast_audio(podcast_content):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    max_length = 4000  # Lasciamo un po' di margine
    segments = [podcast_content[i:i+max_length] for i in range(0, len(podcast_content), max_length)]
    output_path = Path(PODCAST_AUDIO_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='archivebot-tts-') as temp_directory:
        audio_segments = []
        for index, segment in enumerate(segments):
            response = client.audio.speech.create(
                model="tts-1",
                voice="alloy",
                input=segment
            )
            temp_file = Path(temp_directory) / f"segment-{index}.mp3"
            response.stream_to_file(temp_file)
            audio_segments.append(AudioSegment.from_mp3(temp_file))

        combined_audio = sum(audio_segments)
        temporary_output = Path(temp_directory) / 'podcast.mp3'
        combined_audio.export(temporary_output, format="mp3")
        os.replace(temporary_output, output_path)

# Aggiungi questa funzione per generare il contenuto del podcast
def generate_podcast_content(formatted_messages):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    return generate_text_response(
        client,
        model=DEFAULT_OPENAI_MODEL,
        instructions="Sei un membro della Community Sfera IT che crea contenuti per podcast basati sulle conversazioni della community. Il tuo compito è creare un riassunto scorrevole e coinvolgente, adatto all'ascolto, come se stessi parlando con altri membri della community.",
        input_text=f"""
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
            """,
        max_output_tokens=8192,
        reasoning_effort="low",
    )


@flask_app.route('/generate_digest', methods=['POST'])
@auth_required
@optin_required
@admin_required
@web_ai_rate_limited('generate_digest', minute_limit=1, hour_limit=2)
@singleflight_web_ai_job('generate_digest')
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

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        conn.close()
        return get_response({'error': 'Invalid JSON body'}), 400
    force_generate = data.get('force_generate', False)
    send_to_channel = data.get('send_to_channel', False)
    if not isinstance(force_generate, bool) or not isinstance(send_to_channel, bool):
        conn.close()
        return get_response({'error': 'Invalid digest options'}), 400
    if existing_digest and not force_generate and not send_to_channel:
        conn.close()
        return get_response({
            'status': 'success', 
            'digest': existing_digest['digest'],
            'period': existing_digest['period']
        })
    
    # If no existing digest, continue with the original logic to generate a new one
    messages = conn.execute('''
    SELECT 
        message,
        users.name as username,
        channels.id as channel_id,
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
        COALESCE(channels.is_private, 1) = 0
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
            slack_link = (
                'https://slack-archive.sferait.org/getlink?'
                + urlencode({'timestamp': current_thread})
            )
            archive_link = build_archive_url(
                message['channel_id'],
                current_thread,
                current_thread,
                base_url=CLIENT_URL,
            )
            formatted_messages += (
                f"\nThread started at "
                f"{datetime.datetime.fromtimestamp(float(current_thread)).strftime('%Y-%m-%d %H:%M:%S')} "
                f"with timestamp {current_thread}. "
                f"Slack: {slack_link}. SferaArchive: {archive_link}:\n"
            )

        # Format the message
        timestamp = datetime.datetime.fromtimestamp(float(message['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
        formatted_messages += f"[{timestamp}] {message['username']}: {message['message']}\n"

    max_chars = 256000  # Approximate character limit (128000 tokens * 2 chars per token)
    if len(formatted_messages) > max_chars:
        formatted_messages = formatted_messages[:max_chars] + "...\n(truncated due to length)"
    
    
    # Generate summary using OpenAI
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    summary = generate_text_response(
        client,
        model=DEFAULT_OPENAI_MODEL,
        instructions="Sei un assistente che riassume le conversazioni di un workspace di Slack. Fornirai riassunti molto dettagliati, usando almeno 3000 parole, e sempre in italiano.",
        input_text=f"""
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
                - Per ogni conversazione citata inserisci entrambi i link forniti nel tracciato: `Slack` e `SferaArchive`. Il link SferaArchive è quello durevole anche quando Slack non conserva più il messaggio.
                - Evita commenti rispetto alla vivacita o varietà del gruppo, rimani sempre fattuale, parla dei fatti e delle conversazioni avvenute, non giudicarne il contenuto. 
                - È importante che il digest raccolga tutte le conversazioni delle ultime ore e non ne escluda nessuna.
                - Ricorda che il nome dell'utente che ha inviato il post o ha avviato la conversazione è sempre PRIMA del messaggio, non dopo

                {formatted_messages}""",
        max_output_tokens=16384,
        reasoning_effort="medium",
    )

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
            return log_and_return_error(e)

    return get_response({'status': 'success', 'digest': summary, 'period': period})


@flask_app.route('/digest_details', methods=['POST'])
@auth_required
@optin_required
@admin_required
@web_ai_rate_limited('digest_details', minute_limit=2, hour_limit=10)
def digest_details():
    user = g.user_id

    # Get the query from the POST request
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return get_response({'error': 'Invalid JSON body'}), 400
    query = data.get('query')
    if not isinstance(query, str) or not query.strip():
        return get_response({'error': 'No query provided'}), 400
    if len(query) > MAX_CHAT_MESSAGE_CHARS:
        return get_response({'error': 'Query exceeds the maximum length'}), 400

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
    details = generate_text_response(
        client,
        model=DEFAULT_OPENAI_MODEL,
        instructions="Sei un assistente che fornisce dettagli sulle conversazioni di un workspace Slack in base a specifiche richieste.",
        input_text=f"""Dati i seguenti post originali, fornisci dettagli specifici in risposta alla query dell'utente.
            Usa i post originali per fornire informazioni precise e dettagliate.

            Post originali:
            {posts}

            Query dell'utente: {query}

            Fornisci una risposta dettagliata, in italiano e in formato markdown.""",
        max_output_tokens=4096,
        reasoning_effort="medium",
    )

    # Salva i dettagli generati nel database
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO digest_details (user_id, query, details, timestamp, digest_timestamp)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (user, query, details, digest_timestamp))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception('Failed to persist digest details metadata')
    finally:
        if cursor:
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
            cursor.execute('UPDATE messages SET embeddings = NULL WHERE user = ?', (user,))
            cursor.execute('DELETE FROM digest_details')
            cursor.execute('DELETE FROM digests')
            ret = True
        
        conn.commit()

        if ret:
            try:
                Path(PODCAST_AUDIO_PATH).unlink(missing_ok=True)
            except OSError:
                logger.warning('Could not remove AI-opt-out-invalidated podcast audio')

    except Exception as e:
        if conn:
            conn.rollback()
        return log_and_return_error(e)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return get_response({'user_id': user, 'opted_out_ai': ret})


@flask_app.route('/getlink', methods=['GET'])
def get_link():
    timestamp = str(request.args.get('timestamp') or '').strip()
    if not is_valid_slack_timestamp(timestamp):
        return jsonify({'error': 'Invalid timestamp'}), 400

    conn = get_db_connection()
    try:
        message = conn.execute(
            '''
            SELECT messages.permalink
            FROM messages
            JOIN channels ON channels.id = messages.channel
            WHERE (messages.timestamp = ? OR messages.thread_ts = ?)
              AND messages.permalink != ''
              AND COALESCE(channels.is_private, 1) = 0
            ORDER BY CASE WHEN messages.timestamp = ? THEN 0 ELSE 1 END,
                     CAST(messages.timestamp AS REAL) ASC
            LIMIT 1
            ''',
            (timestamp, timestamp, timestamp),
        ).fetchone()
        if not message or not message['permalink']:
            return jsonify({'error': 'Message not found'}), 404

        permalink = urlsplit(message['permalink'])
        hostname = (permalink.hostname or '').lower()
        if (
            permalink.scheme != 'https'
            or not hostname
            or (hostname != 'slack.com' and not hostname.endswith('.slack.com'))
        ):
            logger.warning('Rejected invalid archived Slack permalink')
            return jsonify({'error': 'Message not found'}), 404

        response = redirect(message['permalink'])
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception as e:
        return log_and_return_error(e)
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


def _spreadsheet_safe_cell(value):
    """Neutralize user-controlled spreadsheet formulas in CSV exports."""
    if not isinstance(value, str):
        return value
    inspected = value.lstrip(" \t\r\n")
    if inspected.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@flask_app.route('/chat', methods=['POST'])
@auth_required
@optin_required
@web_ai_rate_limited('chat', minute_limit=2, hour_limit=10)
def chat():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400
    message = data.get('message')
    if not isinstance(message, str) or not message.strip():
        return jsonify({'error': 'No message provided'}), 400
    if len(message) > MAX_CHAT_MESSAGE_CHARS:
        return jsonify({'error': 'Message exceeds the maximum length'}), 400
    if data.get('context') not in (None, []):
        return jsonify({'error': 'Raw archive context is not accepted'}), 400
    try:
        context_refs = _validated_context_refs(data.get('context_refs', []))
        conversation = _validated_chat_items(
            data.get('conversation', []), 'conversation'
        )
    except ValueError:
        return jsonify({'error': 'Invalid chat context'}), 400

    context = _load_ai_context_from_refs(context_refs, g.user_id)

    # Prepare context for OpenAI
    context_text = "\n".join([f"{msg['user_name']}: {msg['message']}" for msg in context])
    conversation_text = "\n".join([f"{msg['user_name']}: {msg['message']}" for msg in conversation])
    prompt = f"Context:\n{context_text}\n\nConversation:\n{conversation_text}\n\nUser: {message}\nAI:"
    if len(prompt) > MAX_CHAT_PROMPT_CHARS:
        return jsonify({'error': 'Combined prompt exceeds the maximum length'}), 400

    # Call OpenAI
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    ai_response = generate_text_response(
        client,
        model=DEFAULT_OPENAI_MODEL,
        instructions="Sei un assistente che risponde alle domande relative alle conversazioni di un workspace di Slack. Ti verranno passate delle conversazioni e una serie di domande a cui dovrai rispondere con precisione.",
        input_text=prompt,
        max_output_tokens=4096,
        reasoning_effort="medium",
    )
    conversation.append({
        'user_name': 'AI',
        'message': ai_response,
        'timestamp': datetime.datetime.utcnow().timestamp()
    })

    return jsonify({'status': 'success', 'conversation': conversation})



@flask_app.route('/stats', methods=['GET'])
@auth_required
@optin_required
@admin_required
def get_stats():
    # Get the time period from the request, default to 30 days
    days = request.args.get('days', 30, type=int)
    if days is None or days < 1 or days > 365:
        return get_response({'error': 'days must be between 1 and 365'}), 400

    conn = get_db_connection()

    # update the users table
    update_users(conn, conn.cursor())

    stats = {}
    # Build all aggregates from the same audience-safe snapshot. Being an app
    # administrator does not imply membership in every private Slack channel.
    conn.execute(
        '''
        CREATE TEMP TABLE visible_stats_messages AS
        SELECT messages.*
        FROM messages
        JOIN channels ON channels.id = messages.channel
        WHERE messages.user NOT IN (SELECT user FROM optout)
          AND (
              COALESCE(channels.is_private, 1) = 0
              OR EXISTS (
                  SELECT 1 FROM members visibility_member
                  WHERE visibility_member.channel = channels.id
                    AND visibility_member.user = ?
              )
          )
        ''',
        (g.user_id,),
    )

    # 1. User activity ranking (excluding deleted users)
    user_activity = conn.execute('''
        SELECT users.name, COUNT(*) as post_count
        FROM visible_stats_messages AS messages
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
        FROM visible_stats_messages AS messages
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
        FROM visible_stats_messages AS messages
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
        FROM visible_stats_messages AS messages
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
        FROM visible_stats_messages AS messages
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
            channels.id AS channel_id,
            channels.name AS channel,
            thread_root.message AS thread_start,
            datetime(thread_root.timestamp, 'unixepoch') AS thread_date,
            COUNT(replies.timestamp) AS reply_count,
            thread_root.timestamp AS thread_ts
        FROM visible_stats_messages replies
        JOIN visible_stats_messages thread_root
          ON thread_root.channel = replies.channel
         AND thread_root.timestamp = replies.thread_ts
        JOIN users ON thread_root.user = users.id
        JOIN channels ON thread_root.channel = channels.id
        WHERE replies.thread_ts IS NOT NULL
          AND datetime(thread_root.timestamp, 'unixepoch') > datetime('now', ?)
          AND thread_root.user NOT IN (SELECT user FROM optout)
          AND replies.user NOT IN (SELECT user FROM optout)
        GROUP BY thread_root.channel, thread_root.timestamp
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
            FROM visible_stats_messages AS messages
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
        FROM visible_stats_messages AS messages
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
        LEFT JOIN visible_stats_messages AS messages ON users.id = messages.user
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

    # Posts and replies by channel
    posts_replies_by_channel = conn.execute('''
        SELECT 
            channels.name as channel_name,
            COUNT(CASE WHEN messages.thread_ts = messages.timestamp THEN 1 END) as post_count,
            COUNT(CASE WHEN messages.thread_ts != messages.timestamp THEN 1 END) as reply_count,
            COUNT(*) as total_messages
        FROM visible_stats_messages AS messages
        JOIN channels ON messages.channel = channels.id
        WHERE datetime(messages.timestamp, 'unixepoch') > datetime('now', ?)
        GROUP BY channels.id, channels.name
        ORDER BY total_messages DESC
    ''', (f'-{days} days',)).fetchall()
    stats['posts_replies_by_channel'] = [dict(row) for row in posts_replies_by_channel]

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
        LEFT JOIN messages
          ON users.id = messages.user
         AND messages.user NOT IN (SELECT user FROM optout)
         AND EXISTS (
             SELECT 1 FROM channels visibility_channel
             WHERE visibility_channel.id = messages.channel
               AND (
                   COALESCE(visibility_channel.is_private, 1) = 0
                   OR EXISTS (
                       SELECT 1 FROM members visibility_member
                       WHERE visibility_member.channel = visibility_channel.id
                         AND visibility_member.user = ?
                   )
               )
         )
        WHERE users.id NOT IN (SELECT user FROM optout)
        GROUP BY users.id
        ORDER BY users.name
    ''', (g.user_id,)).fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'ID', 'Real Name', 'Display Name', 'Email', 'Is Deleted', 'Days Since Last Activity', 'Total Posts'])
    for user in users:
        writer.writerow(_spreadsheet_safe_cell(value) for value in user)

    output.seek(0)
    return get_response({
        'csv': output.getvalue(),
        'filename': 'users.csv'
    })


@flask_app.route('/get_podcast_content', methods=['GET'])
@auth_required
@optin_required
@admin_required
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
@admin_required
def get_podcast_audio():
    try:
        return send_file(PODCAST_AUDIO_PATH, mimetype="audio/mpeg", as_attachment=True)
    except FileNotFoundError:
        return jsonify({'error': 'Podcast audio not found'}), 404


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    flask_app.run(debug=debug_mode)
