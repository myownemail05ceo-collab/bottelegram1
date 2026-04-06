import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriptions.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            channel_invite_link TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
        """)
        await db.commit()


async def add_subscriber(user_id, username, customer_id, sub_id, invite_link, expires_at):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO subscribers
            (user_id, username, stripe_customer_id, stripe_subscription_id,
             channel_invite_link, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (user_id, username, customer_id, sub_id, invite_link, datetime.now(), expires_at),
        )
        await db.commit()


async def update_subscription_status(user_id, status, expires_at=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if expires_at:
            await db.execute(
                "UPDATE subscribers SET status = ?, expires_at = ? WHERE user_id = ?",
                (status, expires_at, user_id),
            )
        else:
            await db.execute(
                "UPDATE subscribers SET status = ? WHERE user_id = ?",
                (status, user_id),
            )
        await db.commit()


async def get_subscriber(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM subscribers WHERE user_id = ?", (user_id,)
        ) as cursor:
            return await cursor.fetchone()


async def get_subscriber_by_stripe_sub(subscription_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM subscribers WHERE stripe_subscription_id = ?",
            (subscription_id,),
        ) as cursor:
            return await cursor.fetchone()


async def get_all_active_subscribers():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM subscribers WHERE status = 'active'") as cursor:
            return await cursor.fetchall()


async def get_expired_subscriptions():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM subscribers WHERE status = 'active' AND expires_at < ?",
            (datetime.now(),),
        ) as cursor:
            return await cursor.fetchall()


async def remove_subscriber(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_all_subscribers_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM subscribers") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def count_active_subscribers():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM subscribers WHERE status = 'active'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def list_all_subscribers():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, status, expires_at, created_at FROM subscribers ORDER BY created_at DESC") as cursor:
            return await cursor.fetchall()
