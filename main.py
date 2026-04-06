#!/usr/bin/env python3
"""
Subscription Bot — canal pago via assinatura Stripe (mensal)

Deploy: Render (polling mode + webhook para pagamentos)
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

import stripe
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    CHANNEL_ID,
    PRICE_ID,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    WEBHOOK_URL,
    WEBHOOK_PORT,
)
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

stripe.api_key = STRIPE_SECRET_KEY
CHANNEL_ID_INT = int(CHANNEL_ID)

# ─── Estado compartilhado para webhook → polling bridge ──────────────
# Como o bot usa polling e o webhook roda separado, usamos uma fila
# para comunicar pagamento confirmado → envio de link pro usuário
pending_confirmed_users = asyncio.Queue()


# ─── Helpers ─────────────────────────────────────────────────────────

async def ban_and_unban(user_id: int):
    """Remove usuário do canal. ban + unban para permitir re-entrada futura."""
    try:
        await bot.ban_chat_member(CHANNEL_ID_INT, user_id)
        await bot.unban_chat_member(CHANNEL_ID_INT, user_id)
    except Exception as e:
        logger.warning(f"Falha ao banir usuário {user_id}: {e}")


async def send_invite_link(user_id: int):
    """Cria link de convite único e envia pro usuário."""
    try:
        invite = await bot.create_chat_invite_link(
            CHANNEL_ID_INT,
            member_limit=1,
            name=f"sub_{user_id}_{int(datetime.now().timestamp())}",
        )
        await bot.send_message(
            user_id,
            f"\U0001f389 Pagamento confirmado! Clique abaixo para entrar no canal:\n\n{invite.invite_link}",
        )
        return invite.invite_link
    except Exception as e:
        logger.error(f"Falha ao criar link para {user_id}: {e}")
        # Manda mesmo assim com instruções
        await bot.send_message(
            user_id,
            "\U0001f389 Pagamento confirmado!\nEntre no canal manualmente. Se tiver problemas, mande /suporte",
        )
        return None


# ─── Commands ────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    sub = await db.get_subscriber(uid)

    if sub and sub[5] == "active":
        expires = datetime.fromisoformat(sub[7]) if sub[7] else None
        text = (
            "\U0001f44b Bem-vindo(a) de volta!\n"
            f"Sua assinatura está ativa até {expires.strftime('%d/%m/%Y') if expires else 'Nunca'}."
        )
        return await message.answer(text)

    text = (
        "\U0001f512 Este canal é exclusivo para assinantes.\n\n"
        "Com a assinatura mensal, você tem acesso completo ao conteúdo.\n\n"
        "Quer assinar?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4b3 Assinar agora", callback_data="subscribe")]
    ])
    await message.answer(text, reply_markup=kb)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    await _handle_subscribe(message)


async def _handle_subscribe(target: types.Message | types.CallbackQuery):
    uid = target.from_user.id
    username = target.from_user.username or target.from_user.first_name

    sub = await db.get_subscriber(uid)
    if sub and sub[5] == "active":
        expires = datetime.fromisoformat(sub[7]) if sub[7] else None
        msg = f"\u2705 Sua assinatura está ativa até {expires.strftime('%d/%m/%Y')}."
        if isinstance(target, types.CallbackQuery):
            return await target.answer(msg, show_alert=False)
        return await target.answer(msg)

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            success_url=f"https://t.me/{(await bot.get_me()).username}?start=success",
            cancel_url=f"https://t.me/{(await bot.get_me()).username}?start=cancel",
            client_reference_id=str(uid),
            metadata={"user_id": str(uid)},
            subscription_data={
                "metadata": {"user_id": str(uid)},
            },
        )
    except Exception as e:
        logger.error(f"Erro Stripe checkout ({uid}): {e}")
        msg = "Erro ao gerar link de pagamento. Tente novamente ou mande /suporte."
        if isinstance(target, types.CallbackQuery):
            return await target.answer(msg, show_alert=True)
        return await target.answer(msg)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pagar com cartão \U0001f512", url=session.url)]
    ])

    text = (
        f"@{username}, seu link de pagamento:\n\n"
        f"Assinatura mensal para acesso ao canal.\n"
        f"Após o pagamento, o link de acesso é enviado automaticamente."
    )
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@dp.callback_query(lambda c: c.data == "subscribe")
async def cb_subscribe(callback: types.CallbackQuery):
    await _handle_subscribe(callback)


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    uid = message.from_user.id
    sub = await db.get_subscriber(uid)
    if not sub:
        return await message.answer("\u2753 Nenhuma assinatura encontrada.")
    status, expires = sub[5], sub[7]
    exp_str = datetime.fromisoformat(expires).strftime("%d/%m/%Y %H:%M") if expires else "?"
    await message.answer(f"Status: {status}\nExpira em: {exp_str}")


@dp.message(Command("suporte"))
async def cmd_suporte(message: types.Message):
    await message.answer(
        "\U0001f4ac Tivemos um problema? Envie uma mensagem descrevendo a situação "
        "e iremos resolver o mais rápido possível."
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    total = await db.count_active_subscribers()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4cb Ver assinantes", callback_data="admin_list")]
    ])
    await message.answer(
        f"\U0001f4ca Painel Admin\nAssinantes ativos: {total}",
        reply_markup=kb,
    )


@dp.callback_query(lambda c: c.data == "admin_list")
async def cb_admin_list(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Sem permissão", show_alert=True)
    rows = await db.list_all_subscribers()
    if not rows:
        return await callback.answer("Nenhum assinante registrado", show_alert=True)

    lines = []
    for r in rows:
        uid, uname, status, expires, created = r
        exp = datetime.fromisoformat(expires).strftime("%d/%m") if expires else "?"
        lines.append(f"{uid} | @{uname or '?'} | {status} | exp: {exp} | desde: {created[:10]}")
    text = "\n".join(lines)

    # Se muito longo, manda como arquivo
    if len(text) > 4000:
        buf = f"Assinantes:\n\n{text}"
        await bot.send_document(
            callback.from_user.id,
            document=aiohttp.formdata.BytesIOField(
                filename="subscribers.txt",
                value=buf.encode(),
                content_type="text/plain",
            ),
        )
        await callback.answer("Lista enviada como arquivo", show_alert=False)
    else:
        await callback.answer(text, show_alert=True)


@dp.message(Command("success"))
async def cmd_success(message: types.Message):
    await message.answer(
        "\U0001f389 Se seu pagamento foi confirmado, "
        "o link de acesso já foi enviado. Confira o chat!"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    await message.answer(
        "\U0001f5d1\ufe0f Pagamento cancelado. Quando quiser, é só clicar em /subscribe novamente."
    )


# ─── Webhook Stripe ─────────────────────────────────────────────────

async def stripe_webhook_handler(request: web.Request) -> web.Response:
    body = await request.text()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            body, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return web.Response(status=400, text="Payload inválido")
    except stripe.error.SignatureVerificationError:
        return web.Response(status=400, text="Assinatura inválida")

    etype = event["type"]
    obj = event["data"]["object"]
    logger.info(f"Webhook recebido: {etype}")

    if etype == "checkout.session.completed":
        user_id = int(
            obj.get("client_reference_id")
            or obj.get("metadata", {}).get("user_id", 0)
        )
        customer_id = obj.get("customer") or ""
        sub_id = obj.get("subscription") or ""

        if not user_id:
            logger.warning("checkout.session.completed sem user_id")
            return web.Response(status=200)

        # Pega data de expiração da assinatura
        try:
            sub_obj = stripe.Subscription.retrieve(sub_id)
            period_end = sub_obj["current_period_end"]
            expires_at = datetime.fromtimestamp(period_end)
        except Exception as e:
            logger.error(f"Falha ao buscar sub {sub_id}: {e}")
            expires_at = datetime.now() + timedelta(days=30)

        invite_link = await send_invite_link(user_id)

        await db.add_subscriber(
            user_id=user_id,
            username="",
            customer_id=customer_id,
            sub_id=sub_id,
            invite_link=invite_link or "",
            expires_at=expires_at,
        )
        logger.info(f"Assinatura criada: user={user_id}, expires={expires_at}")

    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id")
        sub = await db.get_subscriber_by_stripe_sub(sub_id)
        if sub:
            uid = sub[0]
            await db.update_subscription_status(uid, "cancelled")
            await ban_and_unban(uid)
            await bot.send_message(uid, "\u274c Sua assinatura foi cancelada e o acesso ao canal foi removido.")
            logger.info(f"Assinatura cancelada: user={uid}")

    elif etype == "customer.subscription.updated":
        sub_id = obj.get("id")
        user_id = obj.get("metadata", {}).get("user_id")
        status = obj.get("status", "")

        if user_id and status == "active":
            period_end = obj.get("current_period_end")
            expires_at = datetime.fromtimestamp(period_end) if period_end else None
            await db.update_subscription_status(int(user_id), "active", expires_at)
        elif user_id and status in ("past_due", "unpaid"):
            await db.update_subscription_status(int(user_id), status)

    return web.Response(status=200)


# ─── aiohttp server (webhook + health) ────────────────────────────────

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook", stripe_webhook_handler)

    async def health(request):
        return web.json_response({"status": "ok"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    return app


# ─── Background jobs ─────────────────────────────────────────────────

async def cleanup_expired():
    expired = await db.get_expired_subscriptions()
    for row in expired:
        uid = row[0]
        username = row[1] or str(uid)
        await db.update_subscription_status(uid, "expired")
        await ban_and_unban(uid)
        try:
            await bot.send_message(
                uid,
                f"\u23f0 Sua assinatura expirou. O acesso ao canal foi removido.\n"
                f"Para renovar, envie /subscribe.",
            )
        except Exception:
            pass
        logger.info(f"Limpeza: {username} ({uid}) removido por expiração")


# ─── Startup / Shutdown ──────────────────────────────────────────────

async def on_startup():
    await db.init_db()
    logger.info("Banco inicializado")
    scheduler.start()
    scheduler.add_job(cleanup_expired, "interval", hours=6)
    # aiohttp no mesmo loop
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = WEBHOOK_PORT or int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server rodando na porta {port}")


async def on_shutdown():
    scheduler.shutdown()
    await bot.session.close()


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    logger.info("Bot iniciado (polling mode)")
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
