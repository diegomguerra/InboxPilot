import sqlite3
import json
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = "automation.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_conn()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            folder TEXT,
            from_addr TEXT,
            subject TEXT,
            date TEXT,
            body_hash TEXT,
            body_text TEXT,
            status TEXT DEFAULT 'new',
            category TEXT,
            priority TEXT,
            created_ts TEXT,
            updated_ts TEXT,
            session_id TEXT
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN body_text TEXT")
    except:
        pass
    
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
    except:
        pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            key TEXT PRIMARY KEY,
            draft_text TEXT,
            created_ts TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT,
            provider TEXT,
            msg_id TEXT,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            meta_json TEXT,
            ts TEXT NOT NULL,
            session_id TEXT
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE actions ADD COLUMN session_id TEXT")
    except:
        pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            provider TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            meta TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            providers TEXT,
            folders TEXT,
            range_filter TEXT,
            from_date TEXT,
            to_date TEXT,
            status TEXT DEFAULT 'open'
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN from_date TEXT")
    except:
        pass
    
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN to_date TEXT")
    except:
        pass
    
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN date_mode TEXT")
    except:
        pass
    
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN rolling_days INTEGER")
    except:
        pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            key TEXT NOT NULL,
            provider TEXT,
            message_id TEXT,
            date TEXT,
            classification TEXT,
            subject TEXT,
            sender TEXT,
            UNIQUE(session_id, key)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key TEXT PRIMARY KEY,
            provider TEXT,
            model TEXT,
            action TEXT,
            email_key TEXT,
            prompt_hash TEXT,
            response_json TEXT,
            created_at TEXT,
            ttl_seconds INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT,
            scope TEXT,
            state_json TEXT,
            updated_at TEXT,
            PRIMARY KEY (session_id, scope)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            action TEXT,
            email_key TEXT,
            input_chars INTEGER,
            output_tokens INTEGER,
            cached INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS action_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            key TEXT NOT NULL,
            action TEXT NOT NULL,
            body TEXT,
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            result TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_jobs (
            job_id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            session_id TEXT,
            job_type TEXT NOT NULL,
            payload_json TEXT,
            status TEXT DEFAULT 'queued',
            attempts INTEGER DEFAULT 0,
            next_run_at INTEGER DEFAULT 0,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at INTEGER,
            updated_at INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_user_limits (
            user_id TEXT PRIMARY KEY,
            window_start INTEGER,
            window_count INTEGER,
            last_call_at INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ui_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            session_id TEXT,
            providers TEXT,
            folders TEXT,
            filters TEXT,
            created_at TEXT,
            message_keys TEXT,
            payload_json TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ui_snapshots_session ON ui_snapshots(session_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ui_snapshots_created ON ui_snapshots(created_at)")

    conn.commit()
    conn.close()


def snapshot_save(snapshot_id: str, session_id: str, providers: List[str], folders: List[str],
                  filters: dict, message_keys: List[str], payload: List[dict]) -> str:
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO ui_snapshots (snapshot_id, session_id, providers, folders, filters, created_at, message_keys, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snapshot_id, session_id,
        json.dumps(providers), json.dumps(folders), json.dumps(filters),
        now, json.dumps(message_keys), json.dumps(payload)
    ))
    conn.commit()
    conn.close()
    return snapshot_id


def snapshot_get_latest(session_id: str = None) -> Optional[Dict]:
    conn = _get_conn()
    if session_id:
        row = conn.execute(
            "SELECT * FROM ui_snapshots WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM ui_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["message_keys"] = json.loads(d.get("message_keys", "[]"))
    d["payload_json"] = json.loads(d.get("payload_json", "[]"))
    d["providers"] = json.loads(d.get("providers", "[]"))
    d["folders"] = json.loads(d.get("folders", "[]"))
    d["filters"] = json.loads(d.get("filters", "{}"))
    return d


def snapshot_cleanup(keep: int = 10):
    conn = _get_conn()
    conn.execute("""
        DELETE FROM ui_snapshots WHERE snapshot_id NOT IN (
            SELECT snapshot_id FROM ui_snapshots ORDER BY created_at DESC LIMIT ?
        )
    """, (keep,))
    conn.commit()
    conn.close()


def job_create(job_id: str, user_id: str, session_id: str, job_type: str, payload: dict) -> Dict:
    conn = _get_conn()
    now = int(datetime.utcnow().timestamp())
    conn.execute("""
        INSERT INTO llm_jobs (job_id, user_id, session_id, job_type, payload_json, status, attempts, next_run_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'queued', 0, 0, ?, ?)
    """, (job_id, user_id, session_id, job_type, json.dumps(payload), now, now))
    conn.commit()
    conn.close()
    return {"job_id": job_id, "status": "queued", "job_type": job_type}


def job_get(job_id: str) -> Optional[Dict]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_jobs WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("payload_json"):
        try:
            d["payload"] = json.loads(d["payload_json"])
        except:
            d["payload"] = {}
    if d.get("result_json"):
        try:
            d["result"] = json.loads(d["result_json"])
        except:
            d["result"] = {}
    return d


def job_claim_next() -> Optional[Dict]:
    conn = _get_conn()
    now = int(datetime.utcnow().timestamp())
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE llm_jobs SET status = 'processing', updated_at = ?
            WHERE job_id = (
                SELECT job_id FROM llm_jobs
                WHERE status IN ('queued', 'retry_wait') AND next_run_at <= ?
                ORDER BY created_at ASC LIMIT 1
            ) AND status IN ('queued', 'retry_wait')
        """, (now, now))
        if cursor.rowcount == 0:
            conn.rollback()
            conn.close()
            return None
        cursor.execute("""
            SELECT * FROM llm_jobs WHERE status = 'processing'
            ORDER BY updated_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return None
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("payload_json"):
        try:
            d["payload"] = json.loads(d["payload_json"])
        except:
            d["payload"] = {}
    return d


def job_update(job_id: str, status: str, result: dict = None, error_code: str = None, error_message: str = None, next_run_at: int = 0):
    conn = _get_conn()
    now = int(datetime.utcnow().timestamp())
    inc_attempts = 1 if status in ("retry_wait", "error") else 0
    conn.execute("""
        UPDATE llm_jobs SET status = ?, result_json = ?, error_code = ?, error_message = ?,
        attempts = attempts + ?, next_run_at = ?, updated_at = ?
        WHERE job_id = ?
    """, (status, json.dumps(result) if result else None, error_code, error_message, inc_attempts, next_run_at, now, job_id))
    conn.commit()
    conn.close()


def job_queue_stats() -> Dict:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) as cnt FROM llm_jobs GROUP BY status")
    rows = cursor.fetchall()
    cursor.execute("SELECT error_code, error_message FROM llm_jobs WHERE status = 'error' ORDER BY updated_at DESC LIMIT 1")
    last_err = cursor.fetchone()
    conn.close()
    stats = {row["status"]: row["cnt"] for row in rows}
    return {
        "queued": stats.get("queued", 0) + stats.get("retry_wait", 0),
        "processing": stats.get("processing", 0),
        "done": stats.get("done", 0),
        "error": stats.get("error", 0),
        "last_error_code": dict(last_err)["error_code"] if last_err else None,
        "last_error_message": dict(last_err)["error_message"] if last_err else None,
    }


def rate_limit_check(user_id: str, max_rpm: int, min_interval_s: int) -> Dict:
    conn = _get_conn()
    cursor = conn.cursor()
    now = int(datetime.utcnow().timestamp())
    cursor.execute("SELECT * FROM llm_user_limits WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if not row:
        cursor.execute("""
            INSERT INTO llm_user_limits (user_id, window_start, window_count, last_call_at)
            VALUES (?, ?, 1, ?)
        """, (user_id, now, now))
        conn.commit()
        conn.close()
        return {"ok": True}

    d = dict(row)
    window_start = d["window_start"] or 0
    window_count = d["window_count"] or 0
    last_call = d["last_call_at"] or 0

    if now - window_start > 60:
        window_start = now
        window_count = 0

    if window_count >= max_rpm:
        conn.close()
        return {"ok": False, "reason": "rpm", "retry_after": 60 - (now - window_start)}

    if min_interval_s > 0 and last_call > 0 and (now - last_call) < min_interval_s:
        conn.close()
        return {"ok": False, "reason": "cooldown", "retry_after": min_interval_s - (now - last_call)}

    cursor.execute("""
        UPDATE llm_user_limits SET window_start = ?, window_count = ?, last_call_at = ?
        WHERE user_id = ?
    """, (window_start, window_count + 1, now, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def rate_limit_status(user_id: str = "default") -> Dict:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_user_limits WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"user_id": user_id, "window_count": 0, "last_call_at": 0}
    return dict(row)


def llm_cache_get(cache_key: str) -> Optional[Dict]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_cache WHERE cache_key = ?", (cache_key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    row_dict = dict(row)
    from datetime import datetime
    created = datetime.fromisoformat(row_dict["created_at"])
    ttl = row_dict.get("ttl_seconds", 604800)
    elapsed = (datetime.utcnow() - created).total_seconds()
    if elapsed > ttl:
        conn2 = _get_conn()
        conn2.execute("DELETE FROM llm_cache WHERE cache_key = ?", (cache_key,))
        conn2.commit()
        conn2.close()
        return None
    return row_dict


def llm_cache_set(cache_key: str, provider: str, model: str, action: str,
                  email_key: str, prompt_hash: str, response_json: str, ttl_seconds: int):
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO llm_cache (cache_key, provider, model, action, email_key, prompt_hash, response_json, created_at, ttl_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (cache_key, provider, model, action, email_key, prompt_hash, response_json, now, ttl_seconds))
    conn.commit()
    conn.close()


def llm_log_insert(session_id: str, action: str, email_key: str,
                   input_chars: int, output_tokens: int, cached: int):
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO llm_logs (session_id, action, email_key, input_chars, output_tokens, cached, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (session_id, action, email_key, input_chars, output_tokens, cached, now))
    conn.commit()
    conn.close()


def make_key(provider: str, msg_id: str) -> str:
    return f"{provider}:{msg_id}"


def body_hash(body: str) -> str:
    return hashlib.md5(body.encode('utf-8', errors='ignore')).hexdigest()[:16]


def upsert_message(
    key: str,
    provider: str,
    msg_id: str,
    folder: str = None,
    from_addr: str = None,
    subject: str = None,
    date: str = None,
    body: str = None,
    status: str = "new",
    category: str = None,
    priority: str = None
) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    
    cursor.execute("SELECT key, status FROM messages WHERE key = ?", (key,))
    existing = cursor.fetchone()
    
    if existing:
        if existing["status"] in ("sent", "deleted"):
            conn.close()
            return False
        
        cursor.execute("""
            UPDATE messages SET
                folder = COALESCE(?, folder),
                from_addr = COALESCE(?, from_addr),
                subject = COALESCE(?, subject),
                date = COALESCE(?, date),
                body_hash = COALESCE(?, body_hash),
                body_text = COALESCE(?, body_text),
                status = COALESCE(?, status),
                category = COALESCE(?, category),
                priority = COALESCE(?, priority),
                updated_ts = ?
            WHERE key = ?
        """, (folder, from_addr, subject, date, body_hash(body) if body else None,
              body, status, category, priority, now, key))
    else:
        cursor.execute("""
            INSERT INTO messages (key, provider, msg_id, folder, from_addr, subject, date, body_hash, body_text, status, category, priority, created_ts, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (key, provider, msg_id, folder, from_addr, subject, date, 
              body_hash(body) if body else None, body, status, category, priority, now, now))
    
    conn.commit()
    conn.close()
    return True


def get_message(key: str) -> Optional[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_status(key: str, status: str) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("UPDATE messages SET status = ?, updated_ts = ? WHERE key = ?", (status, now, key))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def set_draft(key: str, text: str) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT OR REPLACE INTO drafts (key, draft_text, created_ts)
        VALUES (?, ?, ?)
    """, (key, text, now))
    conn.commit()
    conn.close()
    return True


def get_draft(key: str) -> Optional[str]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT draft_text FROM drafts WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["draft_text"] if row else None


def log_action(
    key: str,
    provider: str,
    msg_id: str,
    action: str,
    status: str,
    reason: str = "",
    meta: dict = None
) -> int:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO actions (key, provider, msg_id, action, status, reason, meta_json, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (key, provider, msg_id, action, status, reason, json.dumps(meta or {}), now))
    action_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return action_id


def list_logs(limit: int = 100) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_pending_deletes(pending_hours: int = 6) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=pending_hours)).isoformat()
    cursor.execute("""
        SELECT * FROM messages 
        WHERE status = 'pending_delete' AND updated_ts < ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_messages_by_status(status: str, limit: int = 100) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages WHERE status = ? ORDER BY created_ts DESC LIMIT ?", (status, limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_recent_messages(limit: int = 10) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages ORDER BY created_ts DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_session(session_id: str, providers: List[str], folders: List[str], date_mode: str, rolling_days: int = None, from_date: str = None, to_date: str = None) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN date_mode TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN rolling_days INTEGER")
    except:
        pass
    try:
        cursor.execute("""
            INSERT INTO sessions (id, created_at, providers, folders, range_filter, date_mode, rolling_days, from_date, to_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (session_id, now, json.dumps(providers), json.dumps(folders), date_mode, date_mode, rolling_days, from_date, to_date))
        conn.commit()
        result = True
    except:
        result = False
    conn.close()
    return result


def get_session(session_id: str) -> Optional[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_session() -> Optional[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sessions WHERE status = 'open' ORDER BY created_at DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def close_session(session_id: str) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def add_session_item(session_id: str, key: str, provider: str, message_id: str, date: str, classification: str, subject: str, sender: str) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO session_items (session_id, key, provider, message_id, date, classification, subject, sender)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, key, provider, message_id, date, classification, subject, sender))
        conn.commit()
        result = True
    except:
        result = False
    conn.close()
    return result


def get_session_items(session_id: str, limit: int = 100, provider: str = None, folder: str = None, classification: str = None) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    
    query = "SELECT si.*, m.folder, m.body_text, m.status FROM session_items si LEFT JOIN messages m ON si.key = m.key WHERE si.session_id = ?"
    params = [session_id]
    
    if provider:
        query += " AND si.provider = ?"
        params.append(provider)
    if classification:
        query += " AND si.classification = ?"
        params.append(classification)
    
    query += " ORDER BY si.date DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_session_item_count(session_id: str) -> Dict[str, int]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT provider, COUNT(*) as count FROM session_items WHERE session_id = ? GROUP BY provider", (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return {row["provider"]: row["count"] for row in rows}


def add_queued_action(key: str, action: str, session_id: str = None, body: str = None) -> int:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    
    parts = key.split(":", 1)
    provider = parts[0] if len(parts) > 1 else ""
    msg_id = parts[1] if len(parts) > 1 else key
    
    meta = {"body": body} if body else {}
    
    cursor.execute("""
        INSERT INTO actions (key, provider, msg_id, action, status, reason, meta_json, ts, session_id)
        VALUES (?, ?, ?, ?, 'queued', '', ?, ?, ?)
    """, (key, provider, msg_id, action, json.dumps(meta), now, session_id))
    action_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return action_id


def get_queued_actions(session_id: str = None, limit: int = 200) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    
    if session_id:
        cursor.execute("SELECT * FROM actions WHERE status = 'queued' AND session_id = ? ORDER BY ts LIMIT ?", (session_id, limit))
    else:
        cursor.execute("SELECT * FROM actions WHERE status = 'queued' ORDER BY ts LIMIT ?", (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_action_status(action_id: int, status: str, reason: str = "") -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE actions SET status = ?, reason = ? WHERE id = ?", (status, reason, action_id))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def link_message_to_session(key: str, session_id: str) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET session_id = ? WHERE key = ?", (session_id, key))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def add_chat_message(session_id: str, role: str, content: str) -> int:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO chat_messages (session_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
    """, (session_id, role, content, now))
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def get_chat_history(session_id: str, limit: int = 20) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM chat_messages
        WHERE session_id = ?
        ORDER BY id DESC LIMIT ?
    """, (session_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))


def clear_chat_history(session_id: str):
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()


def aq_add(session_id: str, key: str, action: str, body: str = None) -> int:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("SELECT id FROM action_queue WHERE session_id = ? AND key = ? AND action = ? AND status = 'queued'",
                   (session_id, key, action))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return existing["id"]
    cursor.execute("""
        INSERT INTO action_queue (session_id, key, action, body, created_at, status)
        VALUES (?, ?, ?, ?, ?, 'queued')
    """, (session_id, key, action, body, now))
    aq_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return aq_id


def aq_list(session_id: str) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT aq.*, m.subject, m.from_addr, m.provider
        FROM action_queue aq
        LEFT JOIN messages m ON aq.key = m.key
        WHERE aq.session_id = ? AND aq.status = 'queued'
        ORDER BY aq.id
    """, (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def aq_remove(aq_id: int, session_id: str = None) -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    if session_id:
        cursor.execute("DELETE FROM action_queue WHERE id = ? AND session_id = ? AND status = 'queued'", (aq_id, session_id))
    else:
        cursor.execute("DELETE FROM action_queue WHERE id = ? AND status = 'queued'", (aq_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def aq_get_queued(session_id: str) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM action_queue
        WHERE session_id = ? AND status = 'queued'
        ORDER BY id
    """, (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def aq_update_status(aq_id: int, status: str, result: str = "") -> bool:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE action_queue SET status = ?, result = ? WHERE id = ?", (status, result, aq_id))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0
