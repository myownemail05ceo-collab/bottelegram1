"""
Database layer — multi-tenant subscription platform.

Schema:
  channels   — tenant (cada dono de canal registrado)
  plans      — planos de assinatura por canal
  subscribers — assinantes por canal/plano
"""
import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", "/tmp/subscriptions.db")

_conn_pool = {}


async def _get_conn():
    """Conexão persistente por processo."""
    pid = os.getpid()
    if pid not in _conn_pool:
        _conn_pool[pid] = await aiosqlite.connect(DB_PATH)
        _conn_pool[pid].row_factory = aiosqlite.Row
    return _conn_pool[pid]


async def init_db():
    db = await _get_conn()
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        channel_id TEXT PRIMARY KEY,       -- ID Telegram do canal (ex: -100xxxx)
        channel_username TEXT,             -- @canal
        channel_title TEXT,               -- nome do canal
        owner_id INTEGER,                  -- user_id do dono
        owner_username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1        -- 1 = ativo, 0 = desativado
    );

    CREATE TABLE IF NOT EXISTS plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        name TEXT NOT NULL,               -- nome do plano (ex: "Mensal")
        description TEXT,
        price INTEGER NOT NULL,           -- em centavos (ex: 1990 = R$19,90)
        interval TEXT NOT NULL DEFAULT 'month',  -- month, year, week
        stripe_price_id TEXT,             -- price_XXXX
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
    );

    CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        plan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        invite_link TEXT,
        status TEXT DEFAULT 'active',      -- active, expired, cancelled
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        UNIQUE(channel_id, user_id),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id),
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
    );

    -- Índice pra busca rápida por tenant
    CREATE INDEX IF NOT EXISTS idx_subscribers_channel ON subscribers(channel_id);
    CREATE INDEX IF NOT EXISTS idx_subscribers_user ON subscribers(user_id);
    CREATE INDEX IF NOT EXISTS idx_plans_channel ON plans(channel_id);
    CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_id);
    """)
    await db.commit()


# ─── Channels ────────────────────────────────────────────────────────

async def add_channel(channel_id, owner_id, username=None, title=None, owner_username=None):
    db = await _get_conn()
    await db.execute(
        "INSERT OR IGNORE INTO channels (channel_id, owner_id, channel_username, channel_title, owner_username) VALUES (?, ?, ?, ?, ?)",
        (str(channel_id), owner_id, username, title, owner_username),
    )
    await db.commit()


async def get_channel(channel_id):
    db = await _get_conn()
    async with db.execute("SELECT * FROM channels WHERE channel_id = ?", (str(channel_id),)) as cur:
        return await cur.fetchone()


async def get_channel_by_username(username):
    db = await _get_conn()
    async with db.execute("SELECT * FROM channels WHERE channel_username = ? COLLATE NOCASE", (username,)) as cur:
        return await cur.fetchone()


async def get_channels_by_owner(owner_id):
    db = await _get_conn()
    async with db.execute("SELECT * FROM channels WHERE owner_id = ? AND is_active = 1", (owner_id,)) as cur:
        return await cur.fetchall()


async def deactivate_channel(channel_id):
    db = await _get_conn()
    await db.execute("UPDATE channels SET is_active = 0 WHERE channel_id = ?", (str(channel_id),))
    await db.commit()


async def list_all_channels():
    db = await _get_conn()
    async with db.execute("SELECT * FROM channels ORDER BY created_at DESC") as cur:
        return await cur.fetchall()


# ─── Plans ───────────────────────────────────────────────────────────

async def add_plan(channel_id, name, price, interval="month", description=None):
    db = await _get_conn()
    cursor = await db.execute(
        "INSERT INTO plans (channel_id, name, description, price, interval) VALUES (?, ?, ?, ?, ?)",
        (str(channel_id), name, description, price, interval),
    )
    await db.commit()
    return cursor.lastrowid


async def update_plan_stripe_id(plan_id, stripe_price_id):
    db = await _get_conn()
    await db.execute(
        "UPDATE plans SET stripe_price_id = ? WHERE plan_id = ?",
        (stripe_price_id, plan_id),
    )
    await db.commit()


async def get_plan(plan_id):
    db = await _get_conn()
    async with db.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)) as cur:
        return await cur.fetchone()


async def get_plans_by_channel(channel_id, active_only=True):
    db = await _get_conn()
    if active_only:
        async with db.execute("SELECT * FROM plans WHERE channel_id = ? AND is_active = 1 ORDER BY price", (str(channel_id),)) as cur:
            return await cur.fetchall()
    else:
        async with db.execute("SELECT * FROM plans WHERE channel_id = ? ORDER BY price", (str(channel_id),)) as cur:
            return await cur.fetchall()


async def deactivate_plan(plan_id):
    db = await _get_conn()
    await db.execute("UPDATE plans SET is_active = 0 WHERE plan_id = ?", (plan_id,))
    await db.commit()


# ─── Subscribers ─────────────────────────────────────────────────────

async def add_subscriber(channel_id, plan_id, user_id, username, customer_id, sub_id, invite_link, expires_at):
    db = await _get_conn()
    await db.execute(
        """INSERT OR REPLACE INTO subscribers
        (channel_id, plan_id, user_id, username, stripe_customer_id,
         stripe_subscription_id, invite_link, status, started_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (str(channel_id), plan_id, user_id, username, customer_id,
         sub_id, invite_link or "", datetime.now(), expires_at),
    )
    await db.commit()


async def update_subscription_status(channel_id, user_id, status, expires_at=None):
    db = await _get_conn()
    if expires_at:
        await db.execute(
            "UPDATE subscribers SET status = ?, expires_at = ? WHERE channel_id = ? AND user_id = ?",
            (status, expires_at, str(channel_id), user_id),
        )
    else:
        await db.execute(
            "UPDATE subscribers SET status = ? WHERE channel_id = ? AND user_id = ?",
            (status, str(channel_id), user_id),
        )
    await db.commit()


async def get_subscriber(channel_id, user_id):
    db = await _get_conn()
    async with db.execute(
        "SELECT * FROM subscribers WHERE channel_id = ? AND user_id = ?",
        (str(channel_id), user_id),
    ) as cur:
        return await cur.fetchone()


async def get_subscriber_by_stripe_sub(sub_id):
    db = await _get_conn()
    async with db.execute(
        "SELECT * FROM subscribers WHERE stripe_subscription_id = ?", (sub_id,)
    ) as cur:
        return await cur.fetchone()


async def get_expired_subscriptions():
    db = await _get_conn()
    async with db.execute(
        "SELECT * FROM subscribers WHERE status = 'active' AND expires_at < ?",
        (datetime.now(),),
    ) as cur:
        return await cur.fetchall()


async def remove_subscriber(channel_id, user_id):
    db = await _get_conn()
    await db.execute(
        "DELETE FROM subscribers WHERE channel_id = ? AND user_id = ?",
        (str(channel_id), user_id),
    )
    await db.commit()


async def count_active_subscribers(channel_id):
    db = await _get_conn()
    async with db.execute(
        "SELECT COUNT(*) FROM subscribers WHERE channel_id = ? AND status = 'active'",
        (str(channel_id),),
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row else 0


async def list_channel_subscribers(channel_id, limit=50):
    db = await _get_conn()
    async with db.execute(
        "SELECT user_id, username, status, expires_at, started_at FROM subscribers WHERE channel_id = ? AND status = 'active' ORDER BY started_at DESC LIMIT ?",
        (str(channel_id), limit),
    ) as cur:
        return await cur.fetchall()


async def list_all_subscribers_stats():
    """Estatísticas agregadas por canal."""
    db = await _get_conn()
    async with db.execute("""
        SELECT s.channel_id, c.channel_title, c.owner_id, c.owner_username,
               COUNT(s.id) as total,
               SUM(CASE WHEN s.status = 'active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN s.status = 'expired' THEN 1 ELSE 0 END) as expired
        FROM subscribers s
        JOIN channels c ON s.channel_id = c.channel_id
        GROUP BY s.channel_id
        ORDER BY active DESC
    """) as cur:
        return await cur.fetchall()


async def execute(sql, params=None):
    """Utility para comandos SQL diretos (ex: UPDATE channels)."""
    db = await _get_conn()
    if params:
        await db.execute(sql, params)
    else:
        await db.execute(sql)
    await db.commit()
