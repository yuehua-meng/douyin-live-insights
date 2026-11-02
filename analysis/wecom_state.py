"""Minimal bot health and an explicit, one-time group binding; no chat history."""
import hmac
import re
import time


def ensure_table(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS wecom_bot_state(
            bot_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL DEFAULT '',
            connected INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL,
            last_error TEXT NOT NULL DEFAULT '')''')


def set_health(store, bot_id, connected, error='', now=None):
    ensure_table(store)
    with store.db(True) as db:
        db.execute('''INSERT INTO wecom_bot_state(bot_id,connected,updated,last_error) VALUES(?,?,?,?)
            ON CONFLICT(bot_id) DO UPDATE SET connected=excluded.connected,
            updated=excluded.updated,last_error=excluded.last_error''',
            (bot_id, int(connected), time.time() if now is None else now, error))


def bot_status(settings, store, now=None):
    ensure_table(store)
    now = time.time() if now is None else now
    with store.db() as db:
        row = db.execute('SELECT * FROM wecom_bot_state WHERE bot_id=?', (settings.wecom_bot_id,)).fetchone()
    return dict(bot_configured=bool(settings.wecom_bot_id and settings.wecom_bot_secret),
                bot_connected=bool(row and row['connected'] and now-row['updated'] < 90),
                group_bound=bool(row and row['chat_id']),
                bot_error=row['last_error'] if row else '')


def bound_chat(settings, store):
    ensure_table(store)
    with store.db() as db:
        row = db.execute('SELECT chat_id FROM wecom_bot_state WHERE bot_id=?', (settings.wecom_bot_id,)).fetchone()
    return row['chat_id'] if row else ''


def binding_command_matches(content, code):
    if not isinstance(content,str) or not code or len(content)>1000:
        return False
    # Mobile mentions may touch the command without a space. Copied commands
    # may include quotes, punctuation or invisible separators. Keep the nonce
    # exact and reject unrelated prose after it.
    normalized=re.sub(r'[\u200b-\u200f\ufeff]', '', content).strip()
    match=re.search(r'绑定日报\s*[:：]?\s*([A-Za-z0-9_-]+)',normalized)
    if not match:
        # A one-time pairing code sent directly after a real mention is enough;
        # this avoids asking mobile users to copy a longer command repeatedly.
        match=re.search(r'(?<![A-Za-z0-9_-])('+re.escape(code)+r')(?![A-Za-z0-9_-])',
                        normalized,re.IGNORECASE)
    if not match or not hmac.compare_digest(match[1].upper(),code.upper()):
        return False
    prefix=normalized[:match.start()].strip().strip('「『“\"\'')
    suffix=normalized[match.end():].strip().strip('」』”\"\'。.!！')
    return (not prefix or prefix.startswith('@')) and not suffix


def bind_group(settings, store, body):
    """Never bind private chats, arbitrary greetings, or replace an existing group."""
    if (not settings.wecom_bind_code or body.get('aibotid') != settings.wecom_bot_id
            or body.get('chattype') != 'group' or body.get('msgtype') != 'text'):
        return False
    chat = body.get('chatid')
    text = body.get('text') or {}
    if not isinstance(chat, str) or not chat or len(chat) > 256 or not isinstance(text, dict):
        return False
    content = text.get('content')
    if not isinstance(content, str) or len(content) > 1000:
        return False
    if not binding_command_matches(content,settings.wecom_bind_code):
        return False
    ensure_table(store)
    with store.db(True) as db:
        db.execute('INSERT OR IGNORE INTO wecom_bot_state(bot_id,updated) VALUES(?,?)',
                   (settings.wecom_bot_id,time.time()))
        return bool(db.execute("UPDATE wecom_bot_state SET chat_id=? WHERE bot_id=? AND chat_id=''",
                               (chat,settings.wecom_bot_id)).rowcount)
