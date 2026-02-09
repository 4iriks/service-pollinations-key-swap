import aiosqlite

from config import settings

_connection: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _connection
    if _connection is None:
        _connection = await aiosqlite.connect(settings.db_path)
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA journal_mode=WAL")
    return _connection


async def init_db():
    db = await get_db()

    # API keys — each bound to a vless_config
    await db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            key_index INTEGER NOT NULL DEFAULT 0,
            vless_id INTEGER DEFAULT NULL,
            is_active INTEGER DEFAULT 1,
            pollen_balance REAL DEFAULT NULL,
            next_reset_at TEXT DEFAULT NULL,
            balance_checked_at TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (vless_id) REFERENCES vless_configs(id) ON DELETE SET NULL
        )
    """)

    # VLESS proxy configs
    await db.execute("""
        CREATE TABLE IF NOT EXISTS vless_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            remark TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            config_index INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Service tokens for Bearer auth
    await db.execute("""
        CREATE TABLE IF NOT EXISTS service_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Legacy services table (kept for migration compatibility)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            source_ip TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Request log
    await db.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id INTEGER DEFAULT NULL,
            path TEXT DEFAULT '',
            method TEXT DEFAULT '',
            status TEXT DEFAULT '',
            key_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (token_id) REFERENCES service_tokens(id),
            FOREIGN KEY (key_id) REFERENCES api_keys(id)
        )
    """)

    await db.commit()


async def close_db():
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
