import json
import os
import sqlite3
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

TOKEN_PATH = Path("token_cache.json")
GMAIL_TOKEN_BACKUP_PATH = Path("gmail_token_backup.json")
DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_PATH = Path("automation.db")

_pg_oauth_table_initialized = False


def _get_pg_conn():
    if not DATABASE_URL:
        return None
    import psycopg2
    return psycopg2.connect(DATABASE_URL)

def _init_kv_table():
    conn = _get_pg_conn()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS key_value_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"KV table init error: {e}")


def _init_pg_oauth_table():
    global _pg_oauth_table_initialized
    if _pg_oauth_table_initialized:
        return
    conn = _get_pg_conn()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider TEXT PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                scope TEXT,
                token_type TEXT DEFAULT 'Bearer',
                expiry_ts BIGINT,
                client_id TEXT,
                client_secret TEXT,
                needs_reauth BOOLEAN DEFAULT FALSE,
                last_refresh_error TEXT,
                updated_at BIGINT
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        _pg_oauth_table_initialized = True
    except Exception as e:
        logger.error(f"PG oauth_tokens init error: {e}")


def get_auth_token(provider: str) -> Tuple[Optional[str], str]:
    key = f"{provider}_token"

    conn = _get_pg_conn()
    if conn:
        try:
            _init_kv_table()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM key_value_store WHERE key = %s", (key,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row and row[0]:
                logger.debug(f"Token for {provider} loaded from PostgreSQL")
                return row[0], "postgresql"
        except Exception as e:
            logger.error(f"PostgreSQL get error for {provider}: {e}")

    try:
        if TOKEN_PATH.exists():
            cache = json.loads(TOKEN_PATH.read_text())
            if key in cache and cache[key]:
                logger.debug(f"Token for {provider} loaded from file fallback")
                return cache[key], "file"
    except Exception as e:
        logger.error(f"File fallback get error for {provider}: {e}")

    return None, "none"


def set_auth_token(provider: str, token_json: str) -> bool:
    key = f"{provider}_token"
    pg_ok = False
    file_ok = False

    conn = _get_pg_conn()
    if conn:
        try:
            _init_kv_table()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO key_value_store (key, value, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            """, (key, token_json))
            conn.commit()
            cursor.close()
            conn.close()
            pg_ok = True
            logger.info(f"Token for {provider} saved to PostgreSQL")
        except Exception as e:
            logger.error(f"PostgreSQL set error for {provider}: {e}")

    try:
        cache = {}
        if TOKEN_PATH.exists():
            cache = json.loads(TOKEN_PATH.read_text())
        cache[key] = token_json
        TOKEN_PATH.write_text(json.dumps(cache))
        file_ok = True
        logger.info(f"Token for {provider} saved to file fallback")
    except Exception as e:
        logger.error(f"File fallback set error for {provider}: {e}")

    return pg_ok or file_ok


def get_storage_info() -> dict:
    pg_available = False
    conn = _get_pg_conn()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
            pg_available = True
        except:
            pass

    return {
        "postgresql_available": pg_available,
        "postgresql_url_set": bool(DATABASE_URL),
        "file_path": str(TOKEN_PATH),
        "file_exists": TOKEN_PATH.exists()
    }

def load_cache() -> dict:
    if TOKEN_PATH.exists():
        return json.loads(TOKEN_PATH.read_text())
    return {}

def save_cache(cache: dict) -> None:
    TOKEN_PATH.write_text(json.dumps(cache))

def get_item(key: str, default=None):
    conn = _get_pg_conn()
    if conn:
        try:
            _init_kv_table()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM key_value_store WHERE key = %s", (key,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                return row[0]
            return default
        except Exception as e:
            print(f"DB get error: {e}")
    return load_cache().get(key, default)

def set_item(key: str, value):
    conn = _get_pg_conn()
    if conn:
        try:
            _init_kv_table()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO key_value_store (key, value, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"DB set error: {e}")
    cache = load_cache()
    cache[key] = value
    save_cache(cache)


# =========================
# Gmail Token Storage — PostgreSQL primary, SQLite + file fallback
# =========================

TOKEN_FIELDS = [
    "access_token", "refresh_token", "scope", "token_type",
    "expiry_ts", "client_id", "client_secret", "needs_reauth",
    "last_refresh_error", "updated_at"
]


def _get_sqlite_conn():
    return sqlite3.connect(str(SQLITE_PATH))


def _init_gmail_tokens_table():
    conn = _get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gmail_tokens (
            email TEXT PRIMARY KEY DEFAULT 'default',
            access_token TEXT,
            refresh_token TEXT,
            scope TEXT,
            token_type TEXT DEFAULT 'Bearer',
            expiry_ts INTEGER,
            client_id TEXT,
            client_secret TEXT,
            needs_reauth INTEGER DEFAULT 0,
            last_refresh_error TEXT,
            updated_at INTEGER
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _save_gmail_token_backup(token_data: dict):
    try:
        backup = {k: v for k, v in token_data.items() if k != "storage_source"}
        GMAIL_TOKEN_BACKUP_PATH.write_text(json.dumps(backup, indent=2))
    except Exception as e:
        logger.error(f"Failed to save Gmail token backup: {e}")


def _load_gmail_token_backup() -> Optional[dict]:
    try:
        if GMAIL_TOKEN_BACKUP_PATH.exists():
            data = json.loads(GMAIL_TOKEN_BACKUP_PATH.read_text())
            if data.get("refresh_token"):
                return data
    except Exception as e:
        logger.error(f"Failed to load Gmail token backup: {e}")
    return None


def _row_to_token(row, source: str) -> dict:
    return {
        "access_token": row[0],
        "refresh_token": row[1],
        "scope": row[2],
        "token_type": row[3] or "Bearer",
        "expiry_ts": row[4],
        "client_id": row[5],
        "client_secret": row[6],
        "needs_reauth": bool(row[7]),
        "last_refresh_error": row[8],
        "updated_at": row[9],
        "storage_source": source,
    }


def _get_token_from_pg() -> Optional[dict]:
    conn = _get_pg_conn()
    if not conn:
        return None
    try:
        _init_pg_oauth_table()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT access_token, refresh_token, scope, token_type,
                   expiry_ts, client_id, client_secret, needs_reauth,
                   last_refresh_error, updated_at
            FROM oauth_tokens WHERE provider = 'gmail'
        """)
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and row[1]:
            return _row_to_token(row, "postgresql")
    except Exception as e:
        logger.error(f"PG get gmail token error: {e}")
    return None


def _save_token_to_pg(token_data: dict) -> bool:
    conn = _get_pg_conn()
    if not conn:
        return False
    try:
        _init_pg_oauth_table()
        cursor = conn.cursor()

        cursor.execute("SELECT refresh_token FROM oauth_tokens WHERE provider = 'gmail'")
        existing = cursor.fetchone()

        now = int(time.time())

        if existing:
            updates = []
            values = []

            for field in ["access_token", "scope", "token_type", "expiry_ts",
                          "client_id", "client_secret", "last_refresh_error"]:
                val = token_data.get(field)
                if val is not None:
                    updates.append(f"{field} = %s")
                    values.append(val)

            rt = token_data.get("refresh_token")
            if rt:
                updates.append("refresh_token = %s")
                values.append(rt)

            na = token_data.get("needs_reauth")
            if na is not None:
                updates.append("needs_reauth = %s")
                values.append(bool(na))
                if na is False:
                    updates.append("last_refresh_error = %s")
                    values.append(None)

            updates.append("updated_at = %s")
            values.append(now)

            if updates:
                sql = f"UPDATE oauth_tokens SET {', '.join(updates)} WHERE provider = 'gmail'"
                cursor.execute(sql, values)
        else:
            cursor.execute("""
                INSERT INTO oauth_tokens (
                    provider, access_token, refresh_token, scope, token_type,
                    expiry_ts, client_id, client_secret, needs_reauth,
                    last_refresh_error, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                'gmail',
                token_data.get("access_token"),
                token_data.get("refresh_token"),
                token_data.get("scope"),
                token_data.get("token_type", "Bearer"),
                token_data.get("expiry_ts"),
                token_data.get("client_id"),
                token_data.get("client_secret"),
                bool(token_data.get("needs_reauth", False)),
                token_data.get("last_refresh_error"),
                now,
            ))

        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Gmail token saved to PostgreSQL")
        return True
    except Exception as e:
        logger.error(f"PG save gmail token error: {e}")
        return False


def _get_token_from_sqlite() -> Optional[dict]:
    try:
        _init_gmail_tokens_table()
        conn = _get_sqlite_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT access_token, refresh_token, scope, token_type,
                   expiry_ts, client_id, client_secret, needs_reauth,
                   last_refresh_error, updated_at
            FROM gmail_tokens WHERE email = 'default'
        """)
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and row[1]:
            return _row_to_token(row, "sqlite")
    except Exception:
        pass
    return None


def _save_token_to_sqlite(token_data: dict) -> bool:
    try:
        _init_gmail_tokens_table()
        conn = _get_sqlite_conn()
        cursor = conn.cursor()
        now = int(time.time())

        cursor.execute("SELECT refresh_token FROM gmail_tokens WHERE email = 'default'")
        existing = cursor.fetchone()

        if existing:
            updates = []
            values = []

            for field in ["access_token", "scope", "expiry_ts",
                          "client_id", "client_secret", "last_refresh_error"]:
                val = token_data.get(field)
                if val is not None:
                    updates.append(f"{field} = ?")
                    values.append(val)

            rt = token_data.get("refresh_token")
            if rt:
                updates.append("refresh_token = ?")
                values.append(rt)

            na = token_data.get("needs_reauth")
            if na is not None:
                updates.append("needs_reauth = ?")
                values.append(1 if na else 0)
                if na is False:
                    updates.append("last_refresh_error = ?")
                    values.append(None)

            updates.append("updated_at = ?")
            values.append(now)

            sql = f"UPDATE gmail_tokens SET {', '.join(updates)} WHERE email = 'default'"
            cursor.execute(sql, values)
        else:
            cursor.execute("""
                INSERT INTO gmail_tokens (
                    email, access_token, refresh_token, scope, expiry_ts,
                    client_id, client_secret, needs_reauth, last_refresh_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'default',
                token_data.get("access_token"),
                token_data.get("refresh_token"),
                token_data.get("scope"),
                token_data.get("expiry_ts"),
                token_data.get("client_id"),
                token_data.get("client_secret"),
                1 if token_data.get("needs_reauth") else 0,
                token_data.get("last_refresh_error"),
                now,
            ))

        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"SQLite save gmail token error: {e}")
        return False


def get_gmail_token() -> Optional[dict]:
    """
    Get Gmail token with priority: PostgreSQL > SQLite > backup file > legacy KV.
    If found in a lower layer, re-hydrates upper layers.
    """
    pg_token = _get_token_from_pg()
    if pg_token and pg_token.get("refresh_token"):
        _save_token_to_sqlite(pg_token)
        _save_gmail_token_backup(pg_token)
        return pg_token

    sqlite_token = _get_token_from_sqlite()
    if sqlite_token and sqlite_token.get("refresh_token"):
        logger.info("Gmail token found in SQLite, re-hydrating PostgreSQL")
        _save_token_to_pg(sqlite_token)
        _save_gmail_token_backup(sqlite_token)
        sqlite_token["storage_source"] = "sqlite_rehydrated"
        return sqlite_token

    backup = _load_gmail_token_backup()
    if backup and backup.get("refresh_token"):
        logger.info("Restoring Gmail token from backup file")
        _save_token_to_pg(backup)
        _save_token_to_sqlite(backup)
        backup["storage_source"] = "restored_from_backup"
        return backup

    legacy_json, legacy_source = get_auth_token("gmail")
    if legacy_json:
        try:
            legacy_data = json.loads(legacy_json)
            if legacy_data.get("refresh_token"):
                logger.info(f"Migrating Gmail token from legacy {legacy_source}")
                scopes = legacy_data.get("scopes", [])
                scope_str = ' '.join(scopes) if isinstance(scopes, list) else str(scopes)
                migrated = {
                    "access_token": legacy_data.get("token"),
                    "refresh_token": legacy_data.get("refresh_token"),
                    "scope": scope_str,
                    "expiry_ts": legacy_data.get("expiry_ts"),
                    "client_id": legacy_data.get("client_id"),
                    "client_secret": legacy_data.get("client_secret"),
                    "needs_reauth": legacy_data.get("needs_reauth", False),
                }
                _save_token_to_pg(migrated)
                _save_token_to_sqlite(migrated)
                _save_gmail_token_backup(migrated)
                migrated["storage_source"] = f"migrated_from_{legacy_source}"
                return migrated
        except Exception as e:
            logger.error(f"Error migrating legacy Gmail token: {e}")

    return None


def set_gmail_token(
    access_token: str = None,
    refresh_token: str = None,
    scope: str = None,
    expiry_ts: int = None,
    client_id: str = None,
    client_secret: str = None,
    needs_reauth: bool = None,
    last_refresh_error: str = None,
    preserve_refresh_token: bool = True
) -> bool:
    """
    Save Gmail token to ALL storage layers (PostgreSQL + SQLite + file backup).
    Never overwrites refresh_token with None when preserve_refresh_token=True.
    """
    token_data = {}

    if access_token is not None:
        token_data["access_token"] = access_token
    if scope is not None:
        token_data["scope"] = scope
    if expiry_ts is not None:
        token_data["expiry_ts"] = expiry_ts
    if client_id is not None:
        token_data["client_id"] = client_id
    if client_secret is not None:
        token_data["client_secret"] = client_secret
    if needs_reauth is not None:
        token_data["needs_reauth"] = needs_reauth
    if last_refresh_error is not None:
        token_data["last_refresh_error"] = last_refresh_error

    if refresh_token:
        token_data["refresh_token"] = refresh_token
    elif not preserve_refresh_token:
        token_data["refresh_token"] = None

    pg_ok = _save_token_to_pg(token_data)
    sqlite_ok = _save_token_to_sqlite(token_data)

    fresh = _get_token_from_pg() or _get_token_from_sqlite()
    if fresh:
        _save_gmail_token_backup(fresh)

    ok = pg_ok or sqlite_ok
    if ok:
        logger.info(f"Gmail token saved (pg={pg_ok}, sqlite={sqlite_ok})")
    else:
        logger.error("Gmail token save failed on all layers")
    return ok


def clear_gmail_refresh_error() -> bool:
    return set_gmail_token(needs_reauth=False, last_refresh_error=None)


def restore_gmail_token_on_boot():
    """Called at server startup to ensure token is available."""
    token = get_gmail_token()
    if token and token.get("refresh_token"):
        logger.info(f"Boot: Gmail token restored (source={token.get('storage_source', 'unknown')}), has_refresh_token=true")
        return True
    else:
        logger.warning("Boot: No Gmail token found in any storage layer")
        return False
