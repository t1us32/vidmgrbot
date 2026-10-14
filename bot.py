"""Telegram-бот: заменяет пару «ссылка TikTok + ответ бота-загрузчика» одним сообщением.

Цепочка, на которую бот реагирует:
  1. Человек пишет в группе ссылку на TikTok (и только ссылку).
  2. Бот-загрузчик (например USaver) отвечает на неё своим сообщением.
  3. Бот публикует одно сообщение «отправитель + ссылка», и только потом удаляет оба исходных.

Все решения принимает rules.py. Здесь только Telegram-обвязка.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, Message
from dotenv import load_dotenv

import rules

log = logging.getLogger("tiktok-consolidator")


@dataclass(frozen=True)
class Config:
    token: str
    allowed_chats: frozenset[int]
    companion_bot_ids: frozenset[int]
    admin_ids: frozenset[int]
    dry_run: bool
    max_message_age: int = 3600  # старше часа не обрабатываем


def _id_set(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


def load_config() -> Config:
    load_dotenv()
    cfg = Config(
        token=os.getenv("BOT_TOKEN", "").strip(),
        allowed_chats=_id_set(os.getenv("ALLOWED_CHAT_IDS", "")),
        companion_bot_ids=_id_set(os.getenv("COMPANION_BOT_IDS", "")),
        admin_ids=_id_set(os.getenv("ADMIN_IDS", "")),
        # по умолчанию безопасный режим: ничего не удаляет, только пишет в лог
        dry_run=os.getenv("DRY_RUN", "1") == "1",
    )
    if not cfg.token:
        raise SystemExit("BOT_TOKEN не задан")
    if not cfg.allowed_chats:
        raise SystemExit("ALLOWED_CHAT_IDS пуст: бот не будет работать ни в одном чате")
    if not cfg.companion_bot_ids:
        if not cfg.dry_run:
            raise SystemExit("COMPANION_BOT_IDS пуст: некого считать загрузчиком")
        # в DRY_RUN можно стартовать без него, чтобы узнать id загрузчика по логу
        log.warning("COMPANION_BOT_IDS пуст: в DRY_RUN бот только логирует ботов в группе")
    if not cfg.admin_ids:
        raise SystemExit("ADMIN_IDS пуст: некому управлять паузой")
    return cfg


def to_msg(m: Message) -> rules.Msg:
    """Превратить aiogram-сообщение в упрощённое для rules.py (один уровень reply)."""
    return rules.Msg(
        chat_id=m.chat.id,
        message_id=m.message_id,
        from_id=m.from_user.id if m.from_user else None,
        from_is_bot=bool(m.from_user and m.from_user.is_bot),
        text=m.text,
        display_name=m.from_user.full_name if m.from_user else "",
        video_file_id=_video_id(m),
        reply_to=_flat(m.reply_to_message) if m.reply_to_message else None,
    )


def _video_id(m: Message) -> str | None:
    """file_id видео в любом из видов: обычное видео, GIF, кружок или файл video/*."""
    if m.video:
        return m.video.file_id
    if m.animation:
        return m.animation.file_id
    if m.video_note:
        return m.video_note.file_id
    if m.document and (m.document.mime_type or "").startswith("video/"):
        return m.document.file_id
    return None


def _flat(m: Message) -> rules.Msg:
    return rules.Msg(
        chat_id=m.chat.id,
        message_id=m.message_id,
        from_id=m.from_user.id if m.from_user else None,
        from_is_bot=bool(m.from_user and m.from_user.is_bot),
        text=m.text,
        display_name=m.from_user.full_name if m.from_user else "",
    )


def build_dispatcher(cfg: Config, gate: rules.SafetyGate, seen: rules.SeenSet) -> Dispatcher:
    dp = Dispatcher()
    lock = asyncio.Lock()  # две параллельные реакции на одну ссылку недопустимы

    def is_admin(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in cfg.admin_ids

    async def notify_admins(bot: Bot, text: str) -> None:
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except TelegramAPIError as e:
                log.error("Не удалось написать админу %s: %s", admin_id, e)

    @dp.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        if not is_admin(message):
            return
        gate.pause("вручную админом")
        log.warning("Пауза включена админом %s", message.from_user.id)
        await message.reply("Пауза включена. Бот ничего не удаляет.")

    @dp.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        if not is_admin(message):
            return
        gate.resume()
        log.warning("Бот возобновлён админом %s", message.from_user.id)
        await message.reply("Бот возобновлён.")

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not is_admin(message):
            return
        state = f"пауза ({gate.pause_reason})" if gate.paused else "работает"
        mode = "DRY_RUN (только лог, без удалений)" if cfg.dry_run else "боевой режим"
        await message.reply(f"Состояние: {state}\nРежим: {mode}")

    @dp.message(Command("chatid"))
    async def cmd_chatid(message: Message) -> None:
        if not is_admin(message):
            return
        await message.reply(f"chat_id: {message.chat.id}")

    @dp.message(F.chat.type.in_({"group", "supergroup"}))
    async def on_group_message(message: Message, bot: Bot) -> None:
        # свои сообщения (в том числе с нашей ссылкой) никогда не обрабатываем: это защита от петли
        if message.from_user and message.from_user.id == bot.id:
            return

        msg = to_msg(message)

        if msg.from_is_bot and msg.from_id not in cfg.companion_bot_ids:
            # подсказка для настройки: id новых ботов в группе видно в логе
            log.info(
                "Сообщение от бота id=%s (@%s) в чате %s; в COMPANION_BOT_IDS его нет",
                msg.from_id,
                message.from_user.username,
                msg.chat_id,
            )

        age = (datetime.now(timezone.utc) - message.date).total_seconds()
        if age > cfg.max_message_age:
            return

        if msg.from_id in cfg.companion_bot_ids:
            log.info(
                "Ответ загрузчика %s: тип=%s, reply=%s, видео=%s",
                msg.message_id,
                message.content_type,
                msg.reply_to is not None,
                msg.video_file_id is not None,
            )

        plan = rules.plan_for_companion(msg, cfg.companion_bot_ids, cfg.allowed_chats)
        if plan is None:
            return

        key = (plan.chat_id, plan.link_message_id)
        async with lock:
            reason = gate.check()
            if reason:
                log.warning("Пропуск (%s): чат %s, %s", reason, plan.chat_id, plan.url)
                return
            if not seen.add(key):
                return

            if cfg.dry_run:
                seen.discard(key)  # в DRY_RUN ничего не фиксируем
                log.info(
                    "[DRY_RUN] заменил бы сообщения %s и %s в чате %s на ответ для %s: %s",
                    plan.link_message_id,
                    plan.companion_message_id,
                    plan.chat_id,
                    plan.sender_id,
                    plan.url,
                )
                return

            # Порядок важен: сначала публикуем новое, и только потом удаляем старое.
            # Если копия не отправилась, исходные сообщения остаются нетронутыми.
            # copyMessage переотправляет видео на стороне Telegram без скачивания, с новой подписью.
            try:
                await bot.copy_message(
                    chat_id=plan.chat_id,
                    from_chat_id=plan.chat_id,
                    message_id=plan.companion_message_id,
                    caption=rules.render(plan),
                    disable_notification=True,
                )
            except TelegramAPIError as e:
                seen.discard(key)
                log.error("Не отправилось новое сообщение, исходные не трогаю: %s", e)
                if gate.record_failure():
                    await notify_admins(bot, f"⛔ Бот на паузе: {gate.pause_reason}. Журнал: actions.log")
                return

            # удаляем ровно два известных id, без диапазонов
            for mid in (plan.companion_message_id, plan.link_message_id):
                try:
                    await bot.delete_message(plan.chat_id, mid)
                    gate.record_success()
                    log.info("Удалено сообщение %s в чате %s", mid, plan.chat_id)
                except TelegramAPIError as e:
                    log.error("Не удалось удалить сообщение %s: %s", mid, e)
                    if gate.record_failure():
                        await notify_admins(bot, f"⛔ Бот на паузе: {gate.pause_reason}. Журнал: actions.log")
                        return

            gate.record_action()
            log.info("Заменено: чат %s, отправитель %s, %s", plan.chat_id, plan.sender_id, plan.url)

    return dp


def _can_delete(member) -> bool:
    if isinstance(member, ChatMemberOwner):
        return True
    return isinstance(member, ChatMemberAdministrator) and bool(member.can_delete_messages)


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("actions.log", encoding="utf-8")],
    )

    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()

    # Предполётные проверки: без них бот либо молчит, либо работает наполовину
    if not me.can_read_all_group_messages:
        raise SystemExit("В BotFather отключите privacy mode: /setprivacy -> Disable, затем заново добавьте бота в группу")
    for chat_id in cfg.allowed_chats:
        member = await bot.get_chat_member(chat_id, me.id)
        if not _can_delete(member):
            raise SystemExit(f"В чате {chat_id} боту нужны права администратора с правом удалять сообщения")

    gate = rules.SafetyGate()
    seen = rules.SeenSet()
    dp = build_dispatcher(cfg, gate, seen)

    await bot.delete_webhook(drop_pending_updates=True)  # не обрабатываем старую очередь после перезапуска
    log.info("Запущен @%s, dry_run=%s, чатов=%d", me.username, cfg.dry_run, len(cfg.allowed_chats))
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
