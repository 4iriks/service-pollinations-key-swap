from db.database import get_db


# --- API Keys ---

async def get_all_keys() -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT k.*, v.remark as vless_remark, v.config_index as vless_config_index
           FROM api_keys k LEFT JOIN vless_configs v ON k.vless_id = v.id
           ORDER BY k.key_index"""
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_active_key(balance_threshold: float = 0.1, exclude_ids: set | None = None) -> dict | None:
    """Get the best active key with its bound VLESS config_index."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT k.*, v.config_index as vless_config_index
           FROM api_keys k LEFT JOIN vless_configs v ON k.vless_id = v.id
           WHERE k.is_active = 1
           AND (k.pollen_balance IS NULL OR k.pollen_balance >= ?)
           ORDER BY k.pollen_balance DESC""",
        (balance_threshold,),
    )
    rows = await cursor.fetchall()
    for row in rows:
        if exclude_ids and row["id"] in exclude_ids:
            continue
        return dict(row)
    return None


async def get_key_by_id(key_id: int) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def add_api_key(key: str, vless_id: int | None = None) -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COALESCE(MAX(key_index), -1) + 1 FROM api_keys")
    row = await cursor.fetchone()
    next_index = row[0]
    await db.execute(
        "INSERT OR IGNORE INTO api_keys (key, key_index, vless_id) VALUES (?, ?, ?)",
        (key, next_index, vless_id),
    )
    await db.commit()
    return next_index


async def delete_api_key(key_id: int):
    db = await get_db()
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()


async def update_key_balance(key_id: int, balance: float | None, next_reset_at: str | None = None):
    db = await get_db()
    await db.execute(
        """UPDATE api_keys SET pollen_balance = ?, next_reset_at = ?,
           balance_checked_at = datetime('now') WHERE id = ?""",
        (balance, next_reset_at, key_id),
    )
    await db.commit()


async def bind_key_to_vless(key_id: int, vless_id: int | None):
    db = await get_db()
    await db.execute("UPDATE api_keys SET vless_id = ? WHERE id = ?", (vless_id, key_id))
    await db.commit()


async def deactivate_key(key_id: int):
    db = await get_db()
    await db.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    await db.commit()


async def reactivate_keys_after_reset():
    """Re-activate all keys (called after pollen reset)."""
    db = await get_db()
    await db.execute("UPDATE api_keys SET is_active = 1")
    await db.commit()


async def get_keys_stats() -> dict:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM api_keys")
    total = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1")
    active = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT COALESCE(SUM(pollen_balance), 0) FROM api_keys WHERE pollen_balance IS NOT NULL"
    )
    total_balance = (await cursor.fetchone())[0]
    return {"total": total, "active": active, "total_balance": round(total_balance, 2)}


# --- VLESS Configs ---

async def get_all_vless() -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM vless_configs ORDER BY config_index")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_active_vless_urls() -> list[str]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT url FROM vless_configs WHERE is_active = 1 ORDER BY config_index"
    )
    rows = await cursor.fetchall()
    return [r["url"] for r in rows]


async def add_vless(url: str, remark: str = "") -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COALESCE(MAX(config_index), -1) + 1 FROM vless_configs")
    row = await cursor.fetchone()
    next_index = row[0]
    await db.execute(
        "INSERT OR IGNORE INTO vless_configs (url, remark, config_index) VALUES (?, ?, ?)",
        (url, remark, next_index),
    )
    await db.commit()
    return next_index


async def delete_vless(vless_id: int):
    db = await get_db()
    # Unbind any keys from this vless
    await db.execute("UPDATE api_keys SET vless_id = NULL WHERE vless_id = ?", (vless_id,))
    await db.execute("DELETE FROM vless_configs WHERE id = ?", (vless_id,))
    await db.commit()


async def get_vless_stats() -> dict:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM vless_configs")
    total = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM vless_configs WHERE is_active = 1")
    active = (await cursor.fetchone())[0]
    return {"total": total, "active": active}


# --- Service Tokens ---

async def get_all_tokens() -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM service_tokens ORDER BY id")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_token_by_value(token: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM service_tokens WHERE token = ? AND is_active = 1", (token,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def create_token(token: str, name: str = "") -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO service_tokens (token, name) VALUES (?, ?)", (token, name),
    )
    await db.commit()
    return cursor.lastrowid


async def revoke_token(token_id: int):
    db = await get_db()
    await db.execute("UPDATE service_tokens SET is_active = 0 WHERE id = ?", (token_id,))
    await db.commit()


async def delete_token(token_id: int):
    db = await get_db()
    await db.execute("DELETE FROM service_tokens WHERE id = ?", (token_id,))
    await db.commit()


# --- Request Log ---

async def add_request_log(
    path: str, method: str, status: str,
    key_id: int | None = None, token_id: int | None = None,
):
    db = await get_db()
    await db.execute(
        "INSERT INTO request_log (path, method, status, key_id, token_id) VALUES (?, ?, ?, ?, ?)",
        (path, method, status, key_id, token_id),
    )
    await db.commit()


async def get_stats() -> dict:
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM request_log WHERE created_at >= date('now')"
    )
    today = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM request_log")
    total = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT COUNT(*) FROM request_log WHERE status = 'ok' AND created_at >= date('now')"
    )
    success_today = (await cursor.fetchone())[0]
    return {"today": today, "total": total, "success_today": success_today}


async def get_token_stats(token_id: int) -> dict:
    """Stats for a specific token."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM request_log WHERE token_id = ? AND created_at >= date('now')",
        (token_id,),
    )
    today = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT COUNT(*) FROM request_log WHERE token_id = ?",
        (token_id,),
    )
    total = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT COUNT(*) FROM request_log WHERE token_id = ? AND status = 'ok' AND created_at >= date('now')",
        (token_id,),
    )
    success_today = (await cursor.fetchone())[0]
    return {"today": today, "total": total, "success_today": success_today}


async def get_all_tokens_stats() -> list[dict]:
    """Get stats per token for display."""
    tokens = await get_all_tokens()
    result = []
    for t in tokens:
        stats = await get_token_stats(t["id"])
        result.append({**t, **stats})
    return result
