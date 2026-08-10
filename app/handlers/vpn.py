"""Команды /vpn* — управление VPN-сервером (mieru) прямо из чата.

Смысл в том, чтобы не ходить на VPS руками. Особенно ради двух вещей:
выдать доступ новому человеку (ссылка + QR приходят сразу в чат) и сменить
адрес сервера, когда текущий IP заблокируют — подписки перегенерируются
разом, и клиенты подтянут новый адрес сами.
"""

import io
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.formatting import esc, human_bytes
from app.services import vpn
from app.services.errors import ServiceError

router = Router(name="vpn")

# Ожидающие подтверждения смены адреса: chat_id -> новый хост.
# В callback_data не влезает (лимит 64 байта), поэтому держим здесь.
_pending_server: dict[int, str] = {}


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "ни разу не заходил"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 90:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    return f"{int(delta // 86400)} дн назад"


def _qr_png(data: str) -> bytes:
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="png", scale=8, border=4, dark="#000000", light="#FFFFFF"
    )
    return buf.getvalue()


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="vpn:users"),
                InlineKeyboardButton(text="♻️ Обновить", callback_data="vpn:refresh"),
            ]
        ]
    )


async def _status_text() -> str:
    st = await vpn.get_status()
    head = "🔐 <b>VPN</b> (mieru): " + ("✅ работает" if st.running else "❌ не запущен")
    lines = [
        head,
        "",
        f"Сервер: <code>{esc(st.host)}</code>",
        f"Порты: <code>{esc(st.port_range)}</code> — "
        f"{st.tcp_sockets} TCP, {st.udp_sockets} UDP",
        f"Соединений сейчас: <b>{st.connections}</b>",
        f"Пользователей: <b>{st.users}</b> (заходили: {st.active_users})",
        f"Трафик: ↓ {human_bytes(st.total_down)} · ↑ {human_bytes(st.total_up)}",
    ]
    if st.sub_port:
        mark = "✅" if st.sub_serving else "❌"
        lines.append(f"Раздача подписок (порт {st.sub_port}): {mark}")
    lines.append(f"\n<i>обновлено {datetime.now():%H:%M:%S}</i>")
    return "\n".join(lines)


async def _users_text() -> str:
    users = await vpn.list_users()
    if not users:
        return "🔐 <b>VPN</b>: пользователей нет. Завести: /vpn_add имя"
    out = ["👥 <b>Пользователи VPN</b>", ""]
    for u in users:
        out.append(
            f"👤 <b>{esc(u.name)}</b>\n"
            f"   ↓ {human_bytes(u.down)} · ↑ {human_bytes(u.up)} · {_ago(u.last_active)}"
        )
    out.append("\nСсылка и QR: /vpn_link имя\nОтозвать: /vpn_del имя")
    return "\n".join(out)


async def _send_access(message: Message, name: str, link: str, sub: str) -> None:
    """Шлёт человеку всё, что нужно для подключения: подписку, ссылку и QR."""
    text = [f"🔑 <b>Доступ для «{esc(name)}»</b>", ""]
    if sub:
        text += [
            "<b>Подписка</b> — давать нужно её. Отдаётся в формате Clash,",
            "поэтому работает на любой версии клиента, включая ту,",
            "что лежит в App Store. И при переезде на новый сервер",
            "адрес подтянется сам.",
            "",
            "В Karing: <b>Добавление подписки</b> → вставить ссылку",
            "или отсканировать QR ниже.",
            f"<code>{esc(sub)}</code>",
            "",
        ]
    if link:
        text += [
            "<b>Прямая ссылка</b> — запасной вариант. Её разбирают не все",
            "версии клиентов, и при переезде придётся выдавать заново:",
            f"<code>{esc(link)}</code>",
        ]
    await message.answer("\n".join(text))
    if sub or link:
        payload = sub or link
        await message.answer_photo(
            BufferedInputFile(_qr_png(payload), filename=f"vpn-{name}.png"),
            caption=(
                f"QR с {'подпиской' if sub else 'ссылкой'} для «{esc(name)}»"
            ),
        )


@router.message(Command("vpn"))
async def cmd_vpn(message: Message) -> None:
    try:
        text = await _status_text()
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    await message.answer(text, reply_markup=_menu())


@router.message(Command("vpn_users"))
async def cmd_vpn_users(message: Message) -> None:
    try:
        await message.answer(await _users_text())
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")


@router.message(Command("vpn_add"))
async def cmd_vpn_add(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer(
            "Кому выдаём? Например: <code>/vpn_add petya</code>\n"
            "Имя — латиница, цифры, <code>_</code> и <code>-</code>, до 32 символов."
        )
        return
    waiting = await message.answer(f"🔑 Завожу «{esc(name)}»…")
    try:
        link, sub = await vpn.add_user(name)
    except ServiceError as e:
        await waiting.edit_text(f"⚠️ {esc(e.user_message)}")
        return
    await waiting.delete()
    await _send_access(message, name, link, sub)


@router.message(Command("vpn_link"))
async def cmd_vpn_link(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("Чью ссылку показать? Например: <code>/vpn_link alex</code>")
        return
    try:
        link = await vpn.get_link(name)
        sub = await vpn.get_sub(name)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    await _send_access(message, name, link, sub)


@router.message(Command("vpn_del"))
async def cmd_vpn_del(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("Кого отозвать? Например: <code>/vpn_del petya</code>")
        return
    await message.answer(
        f"Отозвать доступ у «<b>{esc(name)}</b>»?\n"
        "Его ссылка и подписка перестанут работать сразу. Остальных не затронет.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗑 Отозвать", callback_data=f"vpn:delok:{name}"),
                    InlineKeyboardButton(text="Отмена", callback_data="vpn:cancel"),
                ]
            ]
        ),
    )


@router.message(Command("vpn_server"))
async def cmd_vpn_server(message: Message, command: CommandObject) -> None:
    host = (command.args or "").strip()
    if not host:
        await message.answer(
            "На какой адрес переезжаем? Например: <code>/vpn_server 1.2.3.4</code>\n"
            "Перегенерирую все подписки — обходить людей вручную не придётся."
        )
        return
    _pending_server[message.chat.id] = host
    await message.answer(
        f"Сменить адрес сервера на <code>{esc(host)}</code>?\n\n"
        "Все подписки перепишутся на новый адрес. У кого прямая ссылка "
        "вместо подписки — тем придётся выдать заново.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚚 Переехать", callback_data="vpn:srvok"),
                    InlineKeyboardButton(text="Отмена", callback_data="vpn:cancel"),
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("vpn:"))
async def cb_vpn(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None
    msg = callback.message
    if msg is None:
        await callback.answer()
        return

    if action == "cancel":
        _pending_server.pop(msg.chat.id, None)
        await callback.answer("Отменено.")
        try:
            await msg.edit_text("Отменено.")
        except TelegramBadRequest:
            pass
        return

    await callback.answer("Выполняю…")
    try:
        if action == "refresh":
            try:
                await msg.edit_text(await _status_text(), reply_markup=_menu())
            except TelegramBadRequest:
                # "message is not modified" — ничего не поменялось, это не ошибка
                pass
            return
        if action == "users":
            await msg.answer(await _users_text())
            return
        if action == "delok" and arg:
            left = await vpn.delete_user(arg)
            await msg.answer(
                f"🗑 Доступ «{esc(arg)}» отозван. Осталось пользователей: {left}."
            )
            return
        if action == "srvok":
            host = _pending_server.pop(msg.chat.id, None)
            if not host:
                await msg.answer("Не помню, куда переезжали — повторите /vpn_server.")
                return
            old, count = await vpn.set_server(host)
            await msg.answer(
                f"🚚 Адрес сервера: <code>{esc(old)}</code> → <code>{esc(host)}</code>\n"
                f"Перегенерировано подписок: <b>{count}</b>.\n\n"
                "Клиенты подтянут новый адрес при следующем обновлении подписки."
            )
            return
    except ServiceError as e:
        await msg.answer(f"⚠️ {esc(e.user_message)}")
