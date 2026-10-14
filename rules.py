"""Чистая логика решений бота: без сети и без aiogram, поэтому тестируется отдельно (test_rules.py).

Здесь нет ни одного вызова Telegram API. Всё, что может что-то удалить, проходит через
plan_for_companion() (узкое условие) и SafetyGate (лимиты и автопауза).
"""

from __future__ import annotations

import html
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Msg:
    """Упрощённое сообщение: только то, что нужно для решения."""

    chat_id: int
    message_id: int
    from_id: int | None
    from_is_bot: bool
    text: str | None
    display_name: str = ""
    video_file_id: str | None = None  # видео у сообщения загрузчика
    reply_to: Msg | None = None  # только один уровень вложенности


@dataclass(frozen=True)
class Plan:
    """Что нужно сделать: заменить два сообщения одним."""

    chat_id: int
    link_message_id: int  # сообщение пользователя со ссылкой
    companion_message_id: int  # ответ бота-загрузчика на эту ссылку
    sender_id: int
    sender_name: str
    url: str
    video_file_id: str  # видео, которое пойдёт в итоговое сообщение


def canonical_tiktok_url(text: str | None) -> str | None:
    """Вернуть очищенную ссылку TikTok, если сообщение состоит ТОЛЬКО из неё.

    Сообщения с посторонним текстом не трогаем: иначе пользователь потеряет свои слова.
    Хост проверяется по hostname, а не по подстроке, поэтому tiktok.com.evil.com и
    https://tiktok.com@evil.com отсекаются.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped.split()) != 1:
        return None
    parts = urlsplit(stripped)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return None
    if parts.path in ("", "/"):
        return None
    # query и fragment (трекинг вроде ?is_from_webapp=1) отбрасываем
    return f"https://{host}{parts.path}"


def plan_for_companion(
    msg: Msg,
    companion_bot_ids: frozenset[int],
    allowed_chats: frozenset[int],
) -> Plan | None:
    """Вернуть план замены, если сообщение — ответ загрузчика на ссылку TikTok живого пользователя.

    Все условия обязательны. Любое несоответствие — None, и бот ничего не делает.
    """
    if msg.chat_id not in allowed_chats:
        return None
    if msg.from_id is None or msg.from_id not in companion_bot_ids or not msg.from_is_bot:
        return None
    if not msg.video_file_id:
        return None  # без видео объединять нечего

    link = msg.reply_to
    if link is None or link.chat_id != msg.chat_id:
        return None
    if link.from_id is None or link.from_is_bot:
        return None  # удаляем только сообщения людей

    url = canonical_tiktok_url(link.text)
    if url is None:
        return None

    return Plan(
        chat_id=msg.chat_id,
        link_message_id=link.message_id,
        companion_message_id=msg.message_id,
        sender_id=link.from_id,
        sender_name=link.display_name,
        url=url,
        video_file_id=msg.video_file_id,
    )


def render(plan: Plan) -> str:
    """Текст итогового сообщения (HTML-разметка)."""
    name = html.escape(plan.sender_name.strip() or "Пользователь")
    url = html.escape(plan.url, quote=True)
    return f'👤 <a href="tg://user?id={plan.sender_id}">{name}</a>\n🔗 {url}'


class SeenSet:
    """Ограниченный набор уже обработанных ссылок: защита от повторной обработки."""

    def __init__(self, limit: int = 5000) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[int, int], None] = OrderedDict()

    def add(self, key: tuple[int, int]) -> bool:
        """True, если ключ добавлен впервые."""
        if key in self._items:
            return False
        self._items[key] = None
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return True

    def discard(self, key: tuple[int, int]) -> None:
        self._items.pop(key, None)


class SafetyGate:
    """Предохранитель: лимиты и автопауза, чтобы сбой или злоупотребление не стали массовым удалением.

    - не больше per_minute замен в минуту (лишние пропускаются, без паузы);
    - не больше per_hour замен в час (при превышении — автопауза: это аномалия);
    - max_failures ошибок подряд (удаление/отправка) — автопауза.
    Пауза снимается только вручную (resume).
    """

    def __init__(
        self,
        per_minute: int = 10,
        per_hour: int = 60,
        max_failures: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._max_failures = max_failures
        self._clock = clock
        self._actions: deque[float] = deque()
        self._failures = 0
        self.pause_reason: str | None = None

    @property
    def paused(self) -> bool:
        return self.pause_reason is not None

    def check(self) -> str | None:
        """None — действие разрешено. Иначе — причина отказа."""
        if self.pause_reason is not None:
            return f"пауза: {self.pause_reason}"
        now = self._clock()
        while self._actions and now - self._actions[0] > 3600:
            self._actions.popleft()
        if sum(1 for t in self._actions if now - t <= 60) >= self._per_minute:
            return "лимит в минуту"
        if len(self._actions) >= self._per_hour:
            self.pause("превышен часовой лимит замен, возможен сбой или злоупотребление")
            return f"пауза: {self.pause_reason}"
        return None

    def record_action(self) -> None:
        self._actions.append(self._clock())

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> bool:
        """True, если бот только что встал на паузу из-за ошибок."""
        self._failures += 1
        if self._failures >= self._max_failures and self.pause_reason is None:
            self.pause(f"{self._failures} ошибок подряд")
            return True
        return False

    def pause(self, reason: str) -> None:
        self.pause_reason = reason

    def resume(self) -> None:
        self.pause_reason = None
        self._failures = 0
