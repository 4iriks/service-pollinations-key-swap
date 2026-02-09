"""Telegram бот — админ-панель: ключи, VLESS, токены, статистика."""

import logging
import secrets

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import settings
from db import models
from services.pollinations import check_key_balance, validate_key
from services.vless import parse_vless_url, is_xray_running, restart_xray

logger = logging.getLogger(__name__)

router = Router()


class AddKey(StatesGroup):
    waiting_key = State()
    waiting_vless_bind = State()


class AddVless(StatesGroup):
    waiting_url = State()


class CreateToken(StatesGroup):
    waiting_name = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="🔑 Ключи", callback_data="keys")],
        [InlineKeyboardButton(text="🌐 VLESS", callback_data="vless")],
        [InlineKeyboardButton(text="🔐 Токены", callback_data="tokens")],
    ])


# --- /start ---

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Service Pollinations Key Swap</b>\n\nВыберите раздел:",
        reply_markup=main_menu_kb(),
    )


# --- Статистика ---

@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    keys_stats = await models.get_keys_stats()
    vless_stats = await models.get_vless_stats()
    req_stats = await models.get_stats()

    text = (
        "📊 <b>Общая статистика</b>\n\n"
        f"🔑 Ключей: {keys_stats['active']}/{keys_stats['total']}\n"
        f"💰 Общий баланс: {keys_stats['total_balance']} pollen\n"
        f"🌐 VLESS: {vless_stats['active']}/{vless_stats['total']}\n"
        f"⚡ XRAY: {'✅ работает' if is_xray_running() else '❌ не запущен'}\n\n"
        f"📈 Запросов сегодня: {req_stats['success_today']}/{req_stats['today']}\n"
        f"📈 Всего запросов: {req_stats['total']}"
    )

    # Статистика по токенам
    tokens_stats = await models.get_all_tokens_stats()
    if tokens_stats:
        text += "\n\n<b>По токенам:</b>"
        for t in tokens_stats:
            status = "✅" if t["is_active"] else "❌"
            name = t["name"] or f"token-{t['id']}"
            text += f"\n  {status} {name}: {t['success_today']}/{t['today']} сегодня, {t['total']} всего"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# --- Ключи ---

@router.callback_query(F.data == "keys")
async def cb_keys(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    keys = await models.get_all_keys()

    if not keys:
        text = "🔑 <b>Ключи</b>\n\nНет ключей."
    else:
        lines = ["🔑 <b>Ключи</b>\n"]
        for k in keys:
            status = "✅" if k["is_active"] else "❌"
            balance = f"{k['pollen_balance']:.2f}" if k["pollen_balance"] is not None else "?"
            masked = k["key"][:8] + "..." + k["key"][-4:]
            vless_info = f" → {k['vless_remark']}" if k.get("vless_remark") else " (без VLESS)"
            lines.append(f"{status} <code>{masked}</code> — {balance} p{vless_info}")
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(text="➕ Добавить ключ", callback_data="key_add")],
        [InlineKeyboardButton(text="🔄 Обновить балансы", callback_data="key_refresh_all")],
    ]
    for k in keys:
        masked = k["key"][:8] + "..."
        row = [
            InlineKeyboardButton(text=f"🔗 {masked}", callback_data=f"key_bind_{k['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"key_del_{k['id']}"),
        ]
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "key_add")
async def cb_key_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AddKey.waiting_key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="keys")],
    ])
    await callback.message.edit_text("🔑 Отправьте API ключ Pollinations:", reply_markup=kb)
    await callback.answer()


@router.message(AddKey.waiting_key)
async def on_key_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    key = message.text.strip()
    await message.answer("⏳ Проверяю ключ...")

    socks_port = 10801 if is_xray_running() else None
    valid = await validate_key(key, socks_port)
    if not valid:
        await message.answer("❌ Ключ невалиден. Попробуйте /start")
        await state.clear()
        return

    # Сохраняем ключ в state для привязки к VLESS
    await state.update_data(new_key=key)

    # Предлагаем привязать к VLESS
    vless_configs = await models.get_all_vless()
    if vless_configs:
        buttons = []
        for v in vless_configs:
            remark = v["remark"] or f"config-{v['config_index']}"
            buttons.append([InlineKeyboardButton(
                text=f"🌐 {remark}",
                callback_data=f"key_bind_vless_{v['id']}",
            )])
        buttons.append([InlineKeyboardButton(text="⏭ Без привязки", callback_data="key_bind_vless_none")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await state.set_state(AddKey.waiting_vless_bind)
        await message.answer("✅ Ключ валиден! Привяжите к VLESS конфигу:", reply_markup=kb)
    else:
        # Нет VLESS — сохраняем без привязки
        await _save_new_key(message, state, key, None, socks_port)


@router.callback_query(F.data.startswith("key_bind_vless_"), AddKey.waiting_vless_bind)
async def cb_key_bind_vless(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    key = data.get("new_key", "")

    vless_id_str = callback.data.replace("key_bind_vless_", "")
    vless_id = None if vless_id_str == "none" else int(vless_id_str)

    socks_port = 10801 if is_xray_running() else None
    await _save_new_key(callback.message, state, key, vless_id, socks_port, edit=True)
    await callback.answer()


async def _save_new_key(message, state, key, vless_id, socks_port, edit=False):
    """Сохраняет ключ и обновляет баланс."""
    idx = await models.add_api_key(key, vless_id)

    result = await check_key_balance(key, socks_port)
    if result.get("balance") is not None:
        keys = await models.get_all_keys()
        for k in keys:
            if k["key"] == key:
                await models.update_key_balance(k["id"], result["balance"], result.get("next_reset_at"))
                break

    await state.clear()
    masked = key[:8] + "..." + key[-4:]
    balance = result.get("balance", "?")
    text = f"✅ Ключ добавлен: <code>{masked}</code>\nБаланс: {balance} pollen"
    if vless_id:
        text += "\n🔗 Привязан к VLESS"

    if edit:
        await message.edit_text(text, reply_markup=main_menu_kb())
    else:
        await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "key_refresh_all")
async def cb_key_refresh_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("⏳ Обновляю балансы...")

    keys = await models.get_all_keys()
    for k in keys:
        vless_idx = k.get("vless_config_index")
        socks_port = (10801 + vless_idx) if vless_idx is not None and is_xray_running() else None

        result = await check_key_balance(k["key"], socks_port)
        balance = result.get("balance")
        next_reset = result.get("next_reset_at")
        await models.update_key_balance(k["id"], balance, next_reset)

        if balance is not None and balance < settings.balance_threshold:
            await models.deactivate_key(k["id"])

    await cb_keys(callback)


@router.callback_query(F.data.startswith("key_bind_"))
async def cb_key_bind(callback: CallbackQuery):
    """Привязать существующий ключ к VLESS."""
    if not is_admin(callback.from_user.id):
        return
    # key_bind_{key_id} — но не key_bind_vless_
    if "vless" in callback.data:
        return

    key_id = int(callback.data.split("_")[2])
    vless_configs = await models.get_all_vless()

    if not vless_configs:
        await callback.answer("Нет VLESS конфигов для привязки")
        return

    buttons = []
    for v in vless_configs:
        remark = v["remark"] or f"config-{v['config_index']}"
        buttons.append([InlineKeyboardButton(
            text=f"🌐 {remark}",
            callback_data=f"key_setv_{key_id}_{v['id']}",
        )])
    buttons.append([InlineKeyboardButton(text="🚫 Отвязать", callback_data=f"key_setv_{key_id}_none")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="keys")])

    await callback.message.edit_text(
        f"🔗 Привязка ключа #{key_id} к VLESS:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key_setv_"))
async def cb_key_set_vless(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    key_id = int(parts[2])
    vless_id = None if parts[3] == "none" else int(parts[3])

    await models.bind_key_to_vless(key_id, vless_id)
    await callback.answer("✅ Привязка обновлена")
    await cb_keys(callback)


@router.callback_query(F.data.startswith("key_del_"))
async def cb_key_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    key_id = int(callback.data.split("_")[2])
    await models.delete_api_key(key_id)
    await callback.answer("🗑 Ключ удалён")
    await cb_keys(callback)


# --- VLESS ---

@router.callback_query(F.data == "vless")
async def cb_vless(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    configs = await models.get_all_vless()

    if not configs:
        text = "🌐 <b>VLESS конфиги</b>\n\nНет конфигов."
    else:
        lines = ["🌐 <b>VLESS конфиги</b>\n"]
        for c in configs:
            status = "✅" if c["is_active"] else "❌"
            remark = c["remark"] or f"config-{c['config_index']}"
            lines.append(f"{status} {remark} (idx: {c['config_index']})")
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(text="➕ Добавить VLESS", callback_data="vless_add")],
    ]
    for c in configs:
        remark = c["remark"] or f"config-{c['config_index']}"
        buttons.append([
            InlineKeyboardButton(text=f"🗑 {remark}", callback_data=f"vless_del_{c['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "vless_add")
async def cb_vless_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AddVless.waiting_url)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="vless")],
    ])
    await callback.message.edit_text("🌐 Отправьте VLESS URL:", reply_markup=kb)
    await callback.answer()


@router.message(AddVless.waiting_url)
async def on_vless_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    url = message.text.strip()

    parsed = parse_vless_url(url)
    if not parsed:
        await message.answer("❌ Невалидный VLESS URL. /start для меню.")
        await state.clear()
        return

    remark = parsed.get("remark", "")
    await models.add_vless(url, remark)
    await state.clear()

    # Перезапускаем XRAY чтобы подхватить новый конфиг
    urls = await models.get_active_vless_urls()
    ok = await restart_xray(urls)
    xray_status = "✅ XRAY перезапущен" if ok else "⚠️ Не удалось перезапустить XRAY"

    await message.answer(
        f"✅ VLESS добавлен: {remark or 'без имени'}\n{xray_status}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("vless_del_"))
async def cb_vless_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    vless_id = int(callback.data.split("_")[2])
    await models.delete_vless(vless_id)

    # Перезапускаем XRAY без удалённого конфига
    urls = await models.get_active_vless_urls()
    if urls:
        await restart_xray(urls)
    else:
        from services.vless import stop_xray
        await stop_xray()

    await callback.answer("🗑 VLESS удалён, XRAY обновлён")
    await cb_vless(callback)


# --- Токены ---

@router.callback_query(F.data == "tokens")
async def cb_tokens(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    tokens = await models.get_all_tokens_stats()

    if not tokens:
        text = "🔐 <b>Токены доступа</b>\n\nНет токенов."
    else:
        lines = ["🔐 <b>Токены доступа</b>\n"]
        for t in tokens:
            status = "✅" if t["is_active"] else "❌"
            name = t["name"] or f"token-{t['id']}"
            masked = t["token"][:8] + "..." + t["token"][-4:]
            lines.append(
                f"{status} <b>{name}</b> — <code>{masked}</code>\n"
                f"    Сегодня: {t['success_today']}/{t['today']} | Всего: {t['total']}"
            )
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(text="➕ Создать токен", callback_data="token_create")],
    ]
    for t in tokens:
        name = t["name"] or f"token-{t['id']}"
        row = []
        if t["is_active"]:
            row.append(InlineKeyboardButton(text=f"🚫 {name}", callback_data=f"token_revoke_{t['id']}"))
        row.append(InlineKeyboardButton(text="🗑", callback_data=f"token_del_{t['id']}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "token_create")
async def cb_token_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(CreateToken.waiting_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="tokens")],
    ])
    await callback.message.edit_text("🔐 Введите название для токена (имя сервиса):", reply_markup=kb)
    await callback.answer()


@router.message(CreateToken.waiting_name)
async def on_token_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    token_value = secrets.token_urlsafe(32)
    await models.create_token(token_value, name)
    await state.clear()

    await message.answer(
        f"✅ Токен создан для <b>{name}</b>\n\n"
        f"<code>{token_value}</code>\n\n"
        "⚠️ Сохраните токен — он больше не будет показан полностью!\n"
        "Использование: <code>Authorization: Bearer {token}</code>",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("token_revoke_"))
async def cb_token_revoke(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    token_id = int(callback.data.split("_")[2])
    await models.revoke_token(token_id)
    await callback.answer("🚫 Токен отозван")
    await cb_tokens(callback)


@router.callback_query(F.data.startswith("token_del_"))
async def cb_token_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    token_id = int(callback.data.split("_")[2])
    await models.delete_token(token_id)
    await callback.answer("🗑 Токен удалён")
    await cb_tokens(callback)


# --- Назад в меню ---

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "🛠 <b>Service Pollinations Key Swap</b>\n\nВыберите раздел:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()
