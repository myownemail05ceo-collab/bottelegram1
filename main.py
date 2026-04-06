#!/usr/bin/env python3
"""
Subscription Bot — Multi-tenant SaaS Platform

Um bot único para múltiplos canais venderem acesso via Stripe.
O dono do bot recebe todos os pagamentos (modelo plataforma).

┌─ Fluxos ──────────────────────────────────────────────────────┐
│ /register → Dono registra seu canal e configura planos        │
│ /subscribe → Usuário paga e ganha acesso ao canal             │
│ /status → Checa status da assinatura                          │
│ /manage → Painel do dono do canal (adicionar/remover planos)  │
│ /admin → Super-admin (dono do bot) vê todos os tenants        │
└───────────────────────────────────────────────────────────────┘
"""
import asyncio
import json
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
    PLATFORM_FEE_PERCENT,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    WEBHOOK_PORT,
)
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_bot_token() -> str:
    """Resolve BOT_TOKEN com fallbacks pro Railway."""
    import os
    # Railway injeta via ENV direto — leia ANTES de dotenv
    token = os.environ.get("BOT_TOKEN", "")
    # Limpa espaços/newlines que podem vir acidentalmente
    token = token.strip()
    logger.info(f"[DEBUG] BOT_TOKEN len={len(token)}, prefix={token[:5] + '...' if len(token) >= 5 else '(vazio)'}")
    if not token or token == "PLACEHOLDER" or len(token) < 20:
        logger.warning("BOT_TOKEN ausente ou inválido nos envs — bot não iniciará até o token ser definido")
        return ""
    return token


_bot_token = _resolve_bot_token()
bot = Bot(token=_bot_token) if _bot_token else None
dp = Dispatcher()
scheduler = AsyncIOScheduler()

stripe.api_key = STRIPE_SECRET_KEY

# Fila para comunicação webhook → bot
pending_confirmed_users = asyncio.Queue()


# ─── Helpers ─────────────────────────────────────────────────────────

async def generate_stripe_link(user_id: int, channel_id: str, plan_id: int) -> str | None:
    """Cria checkout session do Stripe para o plano do canal."""
    plan = await db.get_plan(plan_id)
    channel = await db.get_channel(channel_id)
    if not plan or not channel:
        return None

    bot_info = await bot.get_me()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            success_url=f"https://t.me/{bot_info.username}?start=paid_{channel_id}_{plan_id}",
            cancel_url=f"https://t.me/{bot_info.username}?start=cancel_{channel_id}",
            client_reference_id=json.dumps({
                "user_id": user_id,
                "channel_id": channel_id,
                "plan_id": plan_id,
            }),
            metadata={
                "user_id": str(user_id),
                "channel_id": str(channel_id),
                "plan_id": str(plan_id),
            },
            subscription_data={
                "metadata": {
                    "channel_id": str(channel_id),
                    "plan_id": str(plan_id),
                    "user_id": str(user_id),
                },
            },
        )
        return session.url
    except Exception as e:
        logger.error(f"Erro Stripe checkout (user={user_id}, plan={plan_id}): {e}")
        return None


async def send_invite_link(user_id: int, channel_id: str) -> str | None:
    """Cria link de convite único pro canal."""
    try:
        invite = await bot.create_chat_invite_link(
            int(channel_id),
            member_limit=1,
            name=f"sub_{user_id}_{int(datetime.now().timestamp())}",
        )
        await bot.send_message(
            user_id,
            f"\U0001f389 Pagamento confirmado! Clique abaixo para entrar no canal:\n\n{invite.invite_link}",
        )
        return invite.invite_link
    except Exception as e:
        logger.error(f"Falha ao criar link para {user_id} no canal {channel_id}: {e}")
        await bot.send_message(
            user_id,
            "\U0001f389 Pagamento confirmado!\nEntre no canal manualmente. Se tiver problemas, mande /suporte",
        )
        return None


async def ban_and_unban(user_id: int, channel_id: str):
    """Remove usuário do canal."""
    try:
        await bot.ban_chat_member(int(channel_id), user_id)
        await bot.unban_chat_member(int(channel_id), user_id)
    except Exception as e:
        logger.warning(f"Falha ao banir {user_id} do canal {channel_id}: {e}")


def fmt_price(cents: int) -> str:
    """Formata centavos para R$."""
    return f"R$ {cents / 100:.2f}"


# ─── Commands: Usuário Final ─────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Handler inicial inteligente: detecta se veio de link de plano."""
    text = message.text or ""
    uid = message.from_user.id

    # Parse deep links: /start paid_CID_PID, /start cancel_CID, /start register
    parts = text.split()
    if len(parts) > 1:
        arg = parts[1]
        if arg.startswith("paid_"):
            # Usuário voltou do pagamento
            _, cid, pid = arg.split("_", 2)
            await message.answer(
                "\U0001f389 Se seu pagamento foi confirmado, verifique seu chat — "
                "o link de acesso será enviado em instantes!"
            )
            return

        if arg.startswith("cancel_"):
            await message.answer("\U0001f5d1\ufe0f Pagamento cancelado. Quando quiser, clique em /subscribe novamente.")
            return

    # Canal detectado via deep link padrão: /start_<channel_id>
    if len(parts) > 1 and parts[1].startswith("cid_"):
        channel_id = parts[1].replace("cid_", "")
        return await _show_channel_plans(message, channel_id)

    # Default: mostra catálogo de todos os canais ativos
    all_channels = await db.list_all_channels()
    active_channels = [ch for ch in all_channels if ch["is_active"]]

    if active_channels:
        # Mostra os canais disponíveis
        kb_rows = []
        for ch in active_channels[:15]:  # Limita a 15 pra caber no inline keyboard
            plan_count = len(await db.get_plans_by_channel(ch["channel_id"]))
            subscriber_count = await db.count_active_subscribers(ch["channel_id"])
            label = f"{ch['channel_title'] or ch['channel_id']}"
            sub_info = f" ({subscriber_count} sub{'s' if subscriber_count != 1 else ''})"
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"\U0001f512 {label}{sub_info}",
                    callback_data=f"cat_{ch['channel_id']}"
                )
            ])

        text = (
            f"\U0001f916 *Alfe — Assinaturas*\n\n"
            f"Bem-vindo(a)! Escolha um canal abaixo para ver os planos disponíveis:\n\n"
            f"_📊 {len(active_channels)} canal(is) disponíve(is)_"
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    else:
        text = (
            "\U0001f916 Olá! Eu gerencio assinaturas para canais pagos no Telegram.\n\n"
            "\U0001f527 Quer vender acesso ao seu canal? Digite /register para começar!\n\n"
            "Os canais cadastrados aparecerão aqui em breve."
        )
        await message.answer(text)


async def _show_channel_plans(target: types.Message | types.CallbackQuery, channel_id: str):
    """Mostra os planos disponíveis de um canal."""
    if isinstance(target, types.CallbackQuery):
        uid = target.from_user.id
        msg = target.message
        reply_func = msg.edit_text
    else:
        uid = target.from_user.id
        msg = target
        reply_func = msg.answer
    channel = await db.get_channel(channel_id)

    if not channel or not channel["is_active"]:
        return await reply_func("\U0001f4e2 Este canal não está mais disponível.")

    plans = await db.get_plans_by_channel(channel_id)
    if not plans:
        return await reply_func(f"\u26a0\ufe0f O canal *{channel['channel_title']}* ainda não configurou planos de assinatura.")

    # Checa se já é assinante
    sub = await db.get_subscriber(channel_id, uid)
    if sub and sub["status"] == "active":
        expires = datetime.fromisoformat(sub["expires_at"]) if sub["expires_at"] else None
        return await reply_func(
            f"\u2705 Você já é assinante do *{channel['channel_title']}*!\n"
            f"Expira em: {expires.strftime('%d/%m/%Y') if expires else 'Nunca'}"
        )

    header = f"\U0001f512 *{channel['channel_title']}*\n"
    if channel["channel_username"]:
        header += f"@{channel['channel_username']}\n"
    header += "\nEscolha seu plano de acesso:"

    kb_rows = []
    for plan in plans:
        plan_name = f"{plan['name']} — {fmt_price(plan['price'])}/{plan['interval']}"
        if plan["description"]:
            plan_name += f"\n_{plan['description']}_"
        kb_rows.append([
            InlineKeyboardButton(
                text=plan_name,
                callback_data=f"buy_{channel_id}_{plan['plan_id']}",
            )
        ])

    await reply_func(header, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")


@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def cb_buy(callback: types.CallbackQuery):
    """Usuário clicou num plano para comprar."""
    parts = callback.data.split("_")
    if len(parts) < 3:
        return await callback.answer("Link inválido", show_alert=True)

    channel_id = parts[1]
    plan_id = int(parts[2])
    uid = callback.from_user.id

    # Checa se já assinou
    sub = await db.get_subscriber(channel_id, uid)
    if sub and sub["status"] == "active":
        expires = datetime.fromisoformat(sub["expires_at"]) if sub["expires_at"] else None
        return await callback.answer(
            f"Você já é assinante! Expira: {expires.strftime('%d/%m/%Y')}",
            show_alert=True,
        )

    # Gera link de pagamento
    payment_url = await generate_stripe_link(uid, channel_id, plan_id)
    if not payment_url:
        return await callback.answer("Erro ao gerar link de pagamento. Tente novamente ou mande /suporte.", show_alert=True)

    plan = await db.get_plan(plan_id)
    channel = await db.get_channel(channel_id)

    text = (
        f"\U0001f4b3 *{channel['channel_title']}* — {plan['name']}\n\n"
        f"Valor: {fmt_price(plan['price'])}\n\n"
        "Clique abaixo para pagar com cartão. "
        "Após confirmação, o link de acesso será enviado automaticamente."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pagar com cartão \U0001f512", url=payment_url)]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@dp.message(Command("catalogo"))
async def cmd_catalogo(message: types.Message):
    """Mostra o catálogo completo de canais ativos."""
    all_channels = await db.list_all_channels()
    active_channels = [ch for ch in all_channels if ch["is_active"]]

    if not active_channels:
        return await message.answer("\U0001f4e2 Nenhum canal disponível no momento.")

    kb_rows = []
    for ch in active_channels[:20]:
        sub_count = await db.count_active_subscribers(ch["channel_id"])
        label = f"{ch['channel_title'] or ch['channel_id']}"
        kb_rows.append([
            InlineKeyboardButton(
                text=f"\U0001f512 {label} ({sub_count})",
                callback_data=f"cat_{ch['channel_id']}"
            )
        ])

    text = f"\U0001f916 *Catálogo de Canais*\n\nEscolha um canal para ver os planos:\n_📊 {len(active_channels)} canal(is) ativo(s)_"
    await message.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def cb_catalog(callback: types.CallbackQuery):
    """Usuário clicou num canal no catálogo."""
    channel_id = callback.data.replace("cat_", "")
    await _show_channel_plans(callback.message, channel_id)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    await message.answer(
        "\U0001f512 Para assinar, use o link enviado pelo dono do canal.\n\n"
        "Se você criou o canal e quer configurar planos, digite /register.\n"
        "Se precisa de ajuda, digite /suporte."
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Mostra status de assinatura do usuário."""
    uid = message.from_user.id
    channels = await db.get_channels_by_owner(uid)

    # Checa se é assinante de algum canal
    # Lista todos os canais ativos e checa assinatura
    all_channels = await db.list_all_channels()
    subs = []
    for ch in all_channels:
        sub = await db.get_subscriber(ch["channel_id"], uid)
        if sub and sub["status"] == "active":
            subs.append((ch, sub))

    if not subs:
        return await message.answer(
            "\u2753 Nenhuma assinatura ativa encontrada.\n\n"
            "Use os links dos canais que deseja assinar."
        )

    text = "\U0001f4cb *Suas assinaturas ativas:*\n\n"
    for ch, sub in subs:
        expires = datetime.fromisoformat(sub["expires_at"]) if sub["expires_at"] else None
        text += f"\U0001f3d7\ufe0f *{ch['channel_title']}*\n"
        text += f"Expira em: {expires.strftime('%d/%m/%Y') if expires else 'Never'}\n\n"

    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("suporte"))
async def cmd_suporte(message: types.Message):
    await message.answer(
        "\U0001f4ac Tivemos um problema? Descreva a situação aqui e "
        "a equipe do canal vai resolver o mais rápido possível."
    )


# ─── Commands: Dono do Canal ─────────────────────────────────────────

@dp.message(Command("register"))
async def cmd_register(message: types.Message):
    """Inicia o fluxo de registro de um novo canal."""
    uid = message.from_user.id
    text = (
        "\U0001f527 *Registrar seu canal*\n\n"
        "Para configurar a venda de acesso ao seu canal, preciso de 2 informações:\n\n"
        "1\ufe0f\u20e3 O ID do canal (ex: -1001234567890)\n"
        "   - Para descobrir: adicione @userinfobot ao canal e veja o ID, ou configure o bot como admin e digite /channel\n\n"
        "2\ufe0f\u20e3 O @ do canal (opcional)\n\n"
        "Responda com o ID do canal para começar."
    )
    await message.answer(text, parse_mode="Markdown")

    # Cria sessão de registro
    await _set_user_state(message.from_user.id, "register_channel_id")


@dp.message(Command("channel"))
async def cmd_channel(message: types.Message):
    """Detecta o ID do canal onde o bot está."""
    uid = message.from_user.id

    # Se mensagem é do admin, mostra os canais dele
    channels = await db.get_channels_by_owner(uid)
    if channels:
        text = "\U0001f4cb *Seus canais registrados:*\n\n"
        for ch in channels:
            text += f"\U0001f3d7\ufe0f {ch['channel_title'] or ch['channel_id']}\n"
            plans = await db.get_plans_by_channel(ch["channel_id"])
            text += f"📊 {len(plans)} plano(s) configurado(s)\n\n"
        text += "\U0001f527 Para registrar outro canal digite /register"
        return await message.answer(text, parse_mode="Markdown")

    await message.answer(
        "\u2753 Você não tem canais registrados ainda.\n"
        "Digite /register para começar!"
    )


@dp.message(Command("manage"))
async def cmd_manage(message: types.Message):
    """Painel de gerenciamento do dono do canal."""
    uid = message.from_user.id
    channels = await db.get_channels_by_owner(uid)

    if not channels:
        return await message.answer(
            "\u2753 Você não tem canais registrados. Digite /register."
        )

    kb_rows = []
    for ch in channels:
        plans = await db.get_plans_by_channel(ch["channel_id"])
        subs = await db.count_active_subscribers(ch["channel_id"])
        label = f"{ch['channel_title'] or ch['channel_id']} — {len(plans)} plano(s), {subs} assinantes"
        kb_rows.append([
            InlineKeyboardButton(text=f"\U0001f527 {label}", callback_data=f"manage_{ch['channel_id']}")
        ])

    await message.answer("\U0001f4cb *Selecione o canal para gerenciar:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")


@dp.message(Command("newplan"))
async def cmd_newplan(message: types.Message):
    """Cria um novo plano para um canal."""
    uid = message.from_user.id
    channels = await db.get_channels_by_owner(uid)
    if not channels:
        return await message.answer("\u2753 Registre um canal primeiro com /register.")

    if len(channels) == 1:
        await _set_user_state(message.from_user.id, "newplan_select")
        return await _prompt_newplan(message, channels[0]["channel_id"])

    # Multi-canal: mostra lista
    kb_rows = []
    for ch in channels:
        kb_rows.append([
            InlineKeyboardButton(text=ch["channel_title"] or ch["channel_id"], callback_data=f"newplan_{ch['channel_id']}")
        ])
    await _set_user_state(message.from_user.id, "newplan_select_channel")
    await message.answer("Para qual canal quer criar o plano?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(lambda c: c.data.startswith("newplan_"))
async def cb_newplan_channel(callback: types.CallbackQuery):
    channel_id = callback.data.replace("newplan_", "")
    await _set_user_state(callback.from_user.id, "newplan_create")
    await _set_user_data(callback.from_user.id, "plan_channel", channel_id)
    await _prompt_newplan(callback.message, channel_id)


async def _prompt_newplan(msg: types.Message | types.CallbackQuery, channel_id: str):
    plan_count = len(await db.get_plans_by_channel(channel_id))
    text = (
        f"\U0001f4b0 *Criar novo plano*\n\n"
        f"Planos existentes: {plan_count}\n\n"
        "Envie as informações no formato:\n"
        "`Nome | Preço | Período`\n\n"
        "Exemplos:\n"
        "`Mensal | 1990 | month`\n"
        "`Trimestral | 4990 | month`\n"
        "`Anual | 19900 | year`\n\n"
        "(Preço em centavos: R$19,90 = 1990)"
    )
    if isinstance(msg, types.CallbackQuery):
        await msg.message.edit_text(text, parse_mode="Markdown")
    else:
        await msg.answer(text, parse_mode="Markdown")


@dp.message(Command("link"))
async def cmd_link(message: types.Message):
    """Gera link de assinatura para compartilhar."""
    uid = message.from_user.id
    channels = await db.get_channels_by_owner(uid)
    if not channels:
        return await message.answer("\u2753 Registre um canal primeiro com /register.")

    kb_rows = []
    for ch in channels:
        kb_rows.append([
            InlineKeyboardButton(
                text=f"\U0001f517 {ch['channel_title'] or ch['channel_id']}",
                callback_data=f"genlink_{ch['channel_id']}"
            )
        ])
    await message.answer("Para qual canal gerar o link?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(lambda c: c.data.startswith("genlink_"))
async def cb_genlink(callback: types.CallbackQuery):
    uid = callback.from_user.id
    channel_id = callback.data.replace("genlink_", "")
    ch = await db.get_channel(channel_id)

    if ch["owner_id"] != uid:
        return await callback.answer("Sem permissão", show_alert=True)

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=cid_{channel_id}"
    await callback.answer("Link copiado!", show_alert=False)
    await callback.message.reply(f"\U0001f517 *Link de assinatura:*\n\n`{link}`\n\nCompartilhe esse link para que as pessoas vejam os planos do canal.", parse_mode="Markdown")


# ─── Commands: Super Admin ───────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Painel de super-admin do bot (dono da plataforma)."""
    if message.from_user.id not in ADMIN_IDS:
        return

    all_channels = await db.list_all_channels()
    stats = await db.list_all_subscribers_stats()

    total_channels = len(all_channels)
    total_subs = sum(s["total"] for s in stats) if stats else 0
    active_subs = sum(s["active"] for s in stats) if stats else 0

    text = (
        f"\U0001f4ca *Painel Admin da Plataforma*\n\n"
        f"\U0001f3d7\ufe0f Canais registrados: {total_channels}\n"
        f"\U0001f465 Total de assinantes: {total_subs}\n"
        f"\u2705 Assinantes ativos: {active_subs}\n"
        f"\U0001f4b0 Taxa da plataforma: {PLATFORM_FEE_PERCENT}%\n"
    )

    kb_rows = []
    for ch in all_channels[:10]:
        label = f"{ch['channel_title'] or ch['channel_id']} (owner: {ch['owner_id']})"
        kb_rows.append([
            InlineKeyboardButton(text=f"\U0001f527 {label}", callback_data=f"admin_channel_{ch['channel_id']}")
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(lambda c: c.data.startswith("admin_channel_"))
async def cb_admin_channel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Sem permissão", show_alert=True)

    channel_id = callback.data.replace("admin_channel_", "")
    ch = await db.get_channel(channel_id)
    plans = await db.get_plans_by_channel(channel_id, active_only=False)
    subs = await db.count_active_subscribers(channel_id)

    text = (
        f"\U0001f3d7\ufe0f *Canal: {ch['channel_title'] or ch['channel_id']}*\n"
        f"ID: `{ch['channel_id']}`\n"
        f"Dono: {ch['owner_id']} (@{ch['owner_username'] or '?'})\n"
        f"Assinantes ativos: {subs}\n"
        f"Ativo: {'Sim' if ch['is_active'] else 'Não'}\n\n"
    )

    if plans:
        text += "\U0001f4b0 *Planos:*\n"
        for p in plans:
            status = "\u2705" if p["is_active"] else "\u274c"
            text += f"{status} {p['name']} — {fmt_price(p['price'])}/{p['interval']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\U0001f4c4 Listar assinantes", callback_data=f"admin_list_{channel_id}"),
            InlineKeyboardButton(text="\U0001f6ab Desativar" if ch['is_active'] else "\u2705 Ativar", callback_data=f"admin_toggle_{channel_id}"),
        ]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(lambda c: c.data.startswith("admin_toggle_"))
async def cb_admin_toggle(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Sem permissão", show_alert=True)

    channel_id = callback.data.replace("admin_toggle_", "")
    ch = await db.get_channel(channel_id)

    new_status = 0 if ch["is_active"] else 1
    await db.execute(
        f"UPDATE channels SET is_active = {new_status} WHERE channel_id = ?",
        (str(channel_id),),
    )
    await callback.answer("Status alterado!", show_alert=False)


@dp.callback_query(lambda c: c.data.startswith("admin_list_"))
async def cb_admin_list(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Sem permissão", show_alert=True)

    channel_id = callback.data.replace("admin_list_", "")
    rows = await db.list_channel_subscribers(channel_id)

    if not rows:
        return await callback.answer("Nenhum assinante ativo", show_alert=True)

    lines = []
    for r in rows:
        uid, uname, status, expires, started = r
        exp = datetime.fromisoformat(expires).strftime("%d/%m") if expires else "?"
        lines.append(f"{uid} | @{uname or '?'} | {status} | exp: {exp}")

    text = "\n".join(lines[:30])
    if len(text) > 4000:
        buf = f"Assinantes do canal {channel_id}:\n\n{text}"
        await callback.message.answer_document(
            document=aiohttp.payload.BytesPayload(
                buf.encode(),
                filename="subscribers.txt",
                content_type="text/plain",
            ),
        )
        await callback.answer("Lista enviada como arquivo", show_alert=False)
    else:
        await callback.answer(text, show_alert=True)


# ─── User State Management (memória simples) ─────────────────────────

_user_states = {}
_user_data = {}


async def _get_user_state(uid: int) -> str:
    return _user_states.get(uid, "")


async def _set_user_state(uid: int, state: str):
    _user_states[uid] = state


async def _get_user_data(uid: int) -> dict:
    return _user_data.get(uid, {})


async def _set_user_data(uid: int, key: str, value):
    if uid not in _user_data:
        _user_data[uid] = {}
    _user_data[uid][key] = value


# ─── Message Handler — estado do usuário ─────────────────────────────

@dp.message()
async def handle_state_messages(message: types.Message):
    """Processa mensagens de texto conforme o estado do usuário."""
    uid = message.from_user.id
    state = await _get_user_state(uid)
    text = message.text.strip()

    if state == "register_channel_id":
        # Usuário mandou o ID do canal
        channel_id = text.strip()
        if not channel_id.lstrip('-').isdigit():
            return await message.answer("\u274c ID inválido. Envie um número (ex: -1001234567890)")

        await db.add_channel(channel_id, uid)
        channel = await db.get_channel(channel_id)
        await _set_user_state(uid, "register_channel_name")

        # Tenta pegar info do canal
        try:
            info = await bot.get_chat(int(channel_id))
            await db.execute(
                "UPDATE channels SET channel_username = ?, channel_title = ? WHERE channel_id = ?",
                (info.username, info.title, str(channel_id)),
            )
            await message.answer(
                f"\u2705 Canal *{channel['channel_title'] or info.title}* registrado!\n\n"
                f"Agora crie planos com /newplan"
            )
        except Exception as e:
            logger.warning(f"Falha ao buscar info do canal {channel_id}: {e}")
            await message.answer(
                f"\u2705 Canal *{channel_id}* registrado!\n\n"
                f"Agora crie planos com /newplan"
            )

    elif state == "newplan_select_channel":
        # Usuário escolheu canal, não precisa processar aqui (callback lida)
        pass

    elif state == "newplan_create":
        # Cria plano: "Nome | Preço | Período"
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            return await message.answer(
                "\u274c Formato inválido. Use: `Nome | Preço | Período`\n"
                "Ex: `Mensal | 1990 | month`"
            )

        name = parts[0]
        try:
            price = int(parts[1])
        except ValueError:
            return await message.answer("\u274c Preço deve ser um número em centavos (ex: 1990 para R$19,90)")

        interval = parts[2] if len(parts) > 2 else "month"
        if interval not in ("month", "year", "week", "day"):
            return await message.answer("\u274c Período deve ser: month, year, week, ou day")

        channel_id = _user_data.get(uid, {}).get("plan_channel")
        if not channel_id:
            # Se single-canal, pega o único
            channels = await db.get_channels_by_owner(uid)
            if len(channels) == 1:
                channel_id = channels[0]["channel_id"]
            else:
                return await message.answer("\u274c Canal não identificado. Use /newplan para começar.")

        plan_id = await db.add_plan(channel_id, name, price, interval)

        # Cria o produto/preço no Stripe automaticamente
        ch = await db.get_channel(channel_id)
        try:
            product = stripe.Product.create(name=f"{ch['channel_title'] or channel_id} — {name}")
            stripe_price = stripe.Price.create(
                unit_amount=price,
                currency="brl",
                recurring={"interval": interval},
                product=product.id,
            )
            await db.update_plan_stripe_id(plan_id, stripe_price.id)
            logger.info(f"Plano {name} criado no Stripe: {stripe_price.id}")
            await message.answer(
                f"\u2705 *Plano criado!*\n\n"
                f"Nome: {name}\n"
                f"Preço: {fmt_price(price)}/{interval}\n"
                f"Stripe ID: `{stripe_price.id}`\n\n"
                f"Use /link para gerar o link de assinatura."
            )
        except Exception as e:
            logger.error(f"Erro ao criar plano no Stripe: {e}")
            await message.answer(
                f"\u2705 Plano *{name}* criado no banco!\n\n"
                f"\u274c Erro ao criar no Stripe: {e}\n"
                f"Corrija o erro e use /newplan novamente."
            )

        await _set_user_state(uid, "")
        await _set_user_data(uid, "plan_channel", None)

    else:
        # Sem estado — ignora
        pass


# ─── Callback: Gerenciar Canal ───────────────────────────────────────

@dp.callback_query(lambda c: c.data.startswith("manage_"))
async def cb_manage(callback: types.CallbackQuery):
    channel_id = callback.data.replace("manage_", "")
    ch = await db.get_channel(channel_id)

    if ch["owner_id"] != callback.from_user.id:
        return await callback.answer("Sem permissão", show_alert=True)

    plans = await db.get_plans_by_channel(channel_id)
    subs = await db.count_active_subscribers(channel_id)

    text = (
        f"\U0001f527 *{ch['channel_title'] or ch['channel_id']}*\n\n"
        f"\U0001f4b0 Planos: {len(plans)}\n"
        f"\U0001f465 Assinantes ativos: {subs}\n\n"
        f"Use /newplan para criar planos"
    )

    kb_rows = []
    for p in plans:
        kb_rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {fmt_price(p['price'])}/{p['interval']}",
            callback_data=f"plan_detail_{p['plan_id']}"
        )])

    if kb_rows:
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    else:
        kb = None

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ─── Webhook Stripe ─────────────────────────────────────────────────

async def stripe_webhook_handler(request: web.Request) -> web.Response:
    body = await request.text()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(body, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return web.Response(status=400, text="Payload inválido")
    except stripe.error.SignatureVerificationError:
        return web.Response(status=400, text="Assinatura inválida")

    etype = event["type"]
    obj = event["data"]["object"]
    logger.info(f"Webhook recebido: {etype}")

    if etype == "checkout.session.completed":
        metadata = obj.get("metadata", {})
        client_ref = obj.get("client_reference_id")

        # Parse channel_id e plan_id
        if client_ref:
            try:
                ref = json.loads(client_ref)
                user_id = ref.get("user_id")
                channel_id = str(ref.get("channel_id", ""))
                plan_id = ref.get("plan_id")
            except (json.JSONDecodeError, TypeError):
                user_id = int(obj.get("client_reference_id", 0) if str(obj.get("client_reference_id", "0")).isdigit() else metadata.get("user_id", 0))
                channel_id = str(metadata.get("channel_id", ""))
                plan_id = int(metadata.get("plan_id", 0))
        else:
            user_id = int(metadata.get("user_id", 0))
            channel_id = str(metadata.get("channel_id", ""))
            plan_id = int(metadata.get("plan_id", 0))

        customer_id = obj.get("customer") or ""
        sub_id = obj.get("subscription") or ""

        if not user_id:
            logger.warning("checkout.session.completed sem user_id")
            return web.Response(status=200)

        plan = await db.get_plan(plan_id) if plan_id else None

        # Pega data de expiração
        try:
            sub_obj = stripe.Subscription.retrieve(sub_id)
            period_end = sub_obj["current_period_end"]
            expires_at = datetime.fromtimestamp(period_end)
        except Exception as e:
            logger.error(f"Falha ao buscar sub {sub_id}: {e}")
            expires_at = datetime.now() + timedelta(days=30)

        # Envia link de acesso
        invite_link = await send_invite_link(user_id, channel_id)

        await db.add_subscriber(
            channel_id=channel_id,
            plan_id=plan_id or 0,
            user_id=user_id,
            username="",
            customer_id=customer_id,
            sub_id=sub_id,
            invite_link=invite_link or "",
            expires_at=expires_at,
        )
        logger.info(f"Assinatura criada: user={user_id}, channel={channel_id}, plan={plan_id}, expires={expires_at}")

    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id")
        sub = await db.get_subscriber_by_stripe_sub(sub_id)
        if sub:
            uid = sub["user_id"]
            cid = sub["channel_id"]
            await db.update_subscription_status(cid, uid, "cancelled")
            await ban_and_unban(uid, cid)
            try:
                await bot.send_message(uid, "\u274c Sua assinatura foi cancelada e o acesso ao canal foi removido.")
            except Exception:
                pass
            logger.info(f"Assinatura cancelada: user={uid}, channel={cid}")

    elif etype == "customer.subscription.updated":
        sub_id = obj.get("id")
        sub = await db.get_subscriber_by_stripe_sub(sub_id)
        if sub:
            uid = sub["user_id"]
            cid = sub["channel_id"]
            status = obj.get("status", "")

            if status == "active":
                period_end = obj.get("current_period_end")
                expires_at = datetime.fromtimestamp(period_end) if period_end else None
                await db.update_subscription_status(cid, uid, "active", expires_at)
            elif status in ("past_due", "unpaid"):
                await db.update_subscription_status(cid, uid, status)

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
        uid = row["user_id"]
        cid = row["channel_id"]
        channel = await db.get_channel(cid)
        await db.update_subscription_status(cid, uid, "expired")
        await ban_and_unban(uid, cid)
        try:
            await bot.send_message(
                uid,
                f"\u23f0 Sua assinatura no *{channel['channel_title'] or channel['channel_id']}* expirou.\n"
                f"O acesso ao canal foi removido.\nPara renovar, use o link original.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        logger.info(f"Limpeza: user={uid}, channel={cid}")


# ─── Startup / Shutdown ──────────────────────────────────────────────

async def on_startup():
    await db.init_db()
    logger.info("Banco inicializado")

    if not bot:
        logger.error("=" * 60)
        logger.error("BOT_TOKEN não está definido! O bot não vai responder.")
        logger.error("Verifique a variável de ambiente BOT_TOKEN no Railway.")
        logger.error("")
        logger.error("Todas as variáveis disponíveis no ambiente:")
        for key in sorted(os.environ.keys()):
            val = os.environ[key]
            display = val[:5] + "..." if len(val) > 5 else val
            logger.error(f"  {key}='{display}'")
        logger.error("=" * 60)
        return

    # Pega o username do bot
    bot_info = await bot.get_me()
    import config
    config.BOT_USERNAME = bot_info.username
    logger.info(f"Bot logado como @{bot_info.username}")

    scheduler.start()
    scheduler.add_job(cleanup_expired, "interval", hours=6)

    # aiohttp (webhook do Stripe)
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server rodando na porta {port}")


async def on_shutdown():
    scheduler.shutdown()
    if bot:
        await bot.session.close()


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    if bot:
        logger.info("Bot multi-tenant iniciado (polling mode)")
        dp.run_polling(bot)
    else:
        logger.info("Servidor webhook rodando sem polling (BOT_TOKEN ausente)")
        # Roda só o webhook server
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(on_startup())
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            loop.run_until_complete(on_shutdown())


if __name__ == "__main__":
    main()
