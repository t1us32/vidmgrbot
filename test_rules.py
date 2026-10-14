import unittest

import rules
from rules import Msg

COMPANION = 4242
HUMAN = 7
CHAT = -1001
COMPANION_IDS = frozenset({COMPANION})
CHATS = frozenset({CHAT})
TIKTOK = "https://www.tiktok.com/@ashleyfinds1987/video/7696305172955401503?is_from_webapp=1&sender_device=pc"


def link_msg(text=TIKTOK, from_id=HUMAN, is_bot=False, chat=CHAT, mid=100):
    return Msg(chat_id=chat, message_id=mid, from_id=from_id, from_is_bot=is_bot,
               text=text, display_name="Никита")


def companion_msg(reply_to, from_id=COMPANION, is_bot=True, chat=CHAT, mid=101, video="FID"):
    return Msg(chat_id=chat, message_id=mid, from_id=from_id, from_is_bot=is_bot,
               text=None, video_file_id=video, reply_to=reply_to)


class CanonicalUrlTests(unittest.TestCase):
    def test_strips_tracking_query(self):
        self.assertEqual(
            rules.canonical_tiktok_url(TIKTOK),
            "https://www.tiktok.com/@ashleyfinds1987/video/7696305172955401503",
        )

    def test_short_link(self):
        self.assertEqual(rules.canonical_tiktok_url("https://vm.tiktok.com/ZMabc/"),
                         "https://vm.tiktok.com/ZMabc/")

    def test_rejects_lookalike_hosts(self):
        self.assertIsNone(rules.canonical_tiktok_url("https://tiktok.com.evil.com/@a/video/1"))
        self.assertIsNone(rules.canonical_tiktok_url("https://tiktok.com@evil.com/@a/video/1"))
        self.assertIsNone(rules.canonical_tiktok_url("https://evil.com/?https://tiktok.com/x"))

    def test_rejects_text_with_extra_words(self):
        self.assertIsNone(rules.canonical_tiktok_url("смотрите " + TIKTOK))

    def test_rejects_empty_and_schemeless(self):
        self.assertIsNone(rules.canonical_tiktok_url(None))
        self.assertIsNone(rules.canonical_tiktok_url(""))
        self.assertIsNone(rules.canonical_tiktok_url("tiktok.com/@a/video/1"))
        self.assertIsNone(rules.canonical_tiktok_url("ftp://tiktok.com/@a/video/1"))


class PlanTests(unittest.TestCase):
    def plan(self, companion):
        return rules.plan_for_companion(companion, COMPANION_IDS, CHATS)

    def test_happy_path(self):
        plan = self.plan(companion_msg(link_msg()))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.link_message_id, 100)
        self.assertEqual(plan.companion_message_id, 101)
        self.assertEqual(plan.sender_id, HUMAN)
        self.assertEqual(plan.video_file_id, "FID")

    def test_no_video_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(), video=None)))

    def test_wrong_bot_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(), from_id=999)))

    def test_not_a_bot_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(), is_bot=False)))

    def test_not_a_reply_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(None)))

    def test_disallowed_chat_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(chat=-5), chat=-5)))

    def test_never_delete_bot_link_message(self):
        # если ссылку прислал бот, его сообщение не трогаем
        self.assertIsNone(self.plan(companion_msg(link_msg(is_bot=True))))

    def test_link_message_with_text_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(text="глянь " + TIKTOK))))

    def test_reply_in_other_chat_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(chat=CHAT + 1), chat=CHAT)))

    def test_link_without_sender_is_ignored(self):
        self.assertIsNone(self.plan(companion_msg(link_msg(from_id=None))))


class RenderTests(unittest.TestCase):
    def test_render_escapes_and_mentions(self):
        plan = rules.Plan(CHAT, 100, 101, HUMAN, "<b>Ник</b>", "https://www.tiktok.com/@a/video/1", "FID")
        text = rules.render(plan)
        self.assertIn('tg://user?id=7', text)
        self.assertIn("&lt;b&gt;Ник&lt;/b&gt;", text)
        self.assertIn("https://www.tiktok.com/@a/video/1", text)


class SeenSetTests(unittest.TestCase):
    def test_add_once(self):
        s = rules.SeenSet()
        self.assertTrue(s.add((1, 2)))
        self.assertFalse(s.add((1, 2)))
        s.discard((1, 2))
        self.assertTrue(s.add((1, 2)))


class SafetyGateTests(unittest.TestCase):
    def setUp(self):
        self.t = [0.0]
        self.gate = rules.SafetyGate(per_minute=2, per_hour=5, max_failures=3, clock=lambda: self.t[0])

    def test_per_minute_limit_then_recovers(self):
        for _ in range(2):
            self.assertIsNone(self.gate.check())
            self.gate.record_action()
        self.assertEqual(self.gate.check(), "лимит в минуту")
        self.assertFalse(self.gate.paused)  # лимит минуты не ставит на паузу
        self.t[0] = 61
        self.assertIsNone(self.gate.check())

    def test_hourly_limit_pauses(self):
        for i in range(5):
            self.t[0] = i * 61
            self.assertIsNone(self.gate.check())
            self.gate.record_action()
        self.t[0] = 5 * 61
        self.assertIsNotNone(self.gate.check())
        self.assertTrue(self.gate.paused)

    def test_consecutive_failures_pause(self):
        self.assertFalse(self.gate.record_failure())
        self.assertFalse(self.gate.record_failure())
        self.assertTrue(self.gate.record_failure())
        self.assertTrue(self.gate.paused)
        self.assertIsNotNone(self.gate.check())

    def test_success_resets_failures(self):
        self.gate.record_failure()
        self.gate.record_failure()
        self.gate.record_success()
        self.assertFalse(self.gate.record_failure())
        self.assertFalse(self.gate.paused)

    def test_resume_clears_pause(self):
        self.gate.pause("тест")
        self.gate.resume()
        self.assertIsNone(self.gate.check())


if __name__ == "__main__":
    unittest.main()
