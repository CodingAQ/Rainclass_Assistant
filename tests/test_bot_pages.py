import unittest
import os
import time
from concurrent.futures import Future
from typing import Any, cast
from unittest.mock import mock_open, patch

from src.bot import Bot
from src.browser import BrowserManager


class FakeItem:
    def __init__(
        self,
        visible=True,
        on_click=None,
        disabled=False,
        click_error=None,
        text="",
        selectors=None,
        attributes=None,
    ):
        self.visible = visible
        self.on_click = on_click
        self.disabled = disabled
        self.click_error = click_error
        self.text = text
        self.clicked = False
        self.click_count = 0
        self.selectors = selectors or {}
        self.attributes = attributes or {}

    def is_visible(self):
        return self.visible

    def click(self, timeout=None):
        self.click_count += 1
        if self.click_error:
            raise self.click_error
        self.clicked = True
        if self.on_click:
            self.on_click()

    def is_disabled(self):
        return self.disabled

    def inner_text(self):
        return self.text

    def locator(self, selector):
        return FakeLocator(self.selectors.get(selector, []))

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.selectors.get(f"text:{text}", []))

    def get_attribute(self, name):
        return self.attributes.get(name)

    def screenshot(self, type="png"):
        return b"image"


class FakeLocator:
    def __init__(self, items=None):
        self.items = items or []

    @property
    def first(self):
        return self.items[0] if self.items else FakeItem(visible=False)

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakePage:
    def __init__(self, url, selectors=None, closed=False, evaluated=None):
        self.url = url
        self.selectors = selectors or {}
        self.closed = closed
        self.front = False
        self.evaluated = evaluated
        self.content_calls = 0

    def is_closed(self):
        return self.closed

    def bring_to_front(self):
        self.front = True

    def locator(self, selector):
        return FakeLocator(self.selectors.get(selector, []))

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.selectors.get(f"text:{text}", []))

    def evaluate(self, script):
        return self.evaluated

    def screenshot(self, type="png"):
        return b"image"

    def content(self):
        self.content_calls += 1
        return "<html>exercise</html>"

    def wait_for_selector(self, selector, timeout=None):
        return None

    def wait_for_timeout(self, timeout):
        return None


class FakeBrowser:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.current = self.pages[0] if self.pages else None
        self.navigate_calls = 0

    @property
    def page(self):
        if self.current is None:
            raise RuntimeError("no page")
        return self.current

    def ensure_running(self):
        return True

    def navigate_to_class(self):
        self.navigate_calls += 1
        return True

    def use_page(self, page):
        self.current = page
        page.bring_to_front()
        return page


class FakeConfig:
    def get(self, key, default=None):
        return default


class FakeStopEvent:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout=None) -> bool:
        return False


class StoppingEvent(FakeStopEvent):
    def __init__(self):
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout=None) -> bool:
        self.stopped = True
        return True


class FakeAI:
    def __init__(self, answer='{"type":"single","answers":"A"}', complete=True):
        self.answer = answer
        self.complete = complete
        self.answer_calls = 0
        self.last_future = None

    def submit_answer(self, **kwargs):
        self.answer_calls += 1
        future = Future()
        if self.complete:
            future.set_result(self.answer)
        self.last_future = future
        return future

    def shutdown(self):
        return None


def make_bot(pages=None, ai=None):
    return Bot(
        FakeConfig(),
        FakeBrowser(pages),
        ai or FakeAI(),
        object(),
        cast(Any, FakeStopEvent()),
    )


def as_page(page):
    return cast(Any, page)


class ClassroomPageTests(unittest.TestCase):
    def test_classroom_loop_logs_only_new_ppt_and_exercise_urls(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123?source=5"
        )
        routes = [
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/1",
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/1",
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/2",
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
        ]

        class RouteSequenceEvent(FakeStopEvent):
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout=None):
                if routes:
                    page.url = routes.pop(0)
                    return False
                self.stopped = True
                return True

        bot = make_bot([page])
        bot.stop_event = cast(Any, RouteSequenceEvent())

        with (
            patch.object(bot, "log") as log,
            patch.object(bot, "_is_classroom_page", return_value=True),
            patch.object(bot, "_open_new_quiz", return_value=False),
            patch.object(bot, "_check_and_sign_in"),
            patch.object(bot, "_answer"),
        ):
            bot._run_classroom_loop(as_page(page))

        messages = [entry.args[0] for entry in log.call_args_list]
        self.assertEqual(
            messages,
            [
                "我去上课啦！",
                "进入新的 PPT 页：https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/1",
                "进入新的 PPT 页：https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/2",
                "进入新的习题页：https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            ],
        )

    def test_classroom_wait_pumps_playwright_page_events(self):
        home = FakePage("https://changjiang.yuketang.cn/v2/web/index")
        classroom = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/21",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        browser = FakeBrowser([home])
        bot = make_bot()
        bot.browser = cast(Any, browser)

        def publish_popup(timeout):
            if classroom not in browser.pages:
                browser.pages.append(classroom)

        home.wait_for_timeout = publish_popup

        self.assertIs(bot._wait_for_classroom_page(1), classroom)

    def test_homepage_is_rescanned_before_clicking_course(self):
        home = FakePage("https://changjiang.yuketang.cn/v2/web/index")
        classroom = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/21",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        browser = FakeBrowser([home])
        bot = make_bot()
        bot.browser = cast(Any, browser)

        def finish_pending_open(selector, timeout=None):
            browser.pages.append(classroom)

        home.wait_for_selector = finish_pending_open
        with (
            patch.object(bot, "_click_active_class") as click_course,
            patch.object(bot, "_run_classroom_loop") as classroom_loop,
        ):
            bot._get_into_class()

        self.assertEqual(browser.navigate_calls, 0)
        click_course.assert_not_called()
        classroom_loop.assert_called_once_with(classroom)

    def test_homepage_is_never_a_classroom(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/v2/web/index",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        self.assertFalse(make_bot()._is_classroom_page(as_page(page)))

    def test_plain_course_page_is_not_a_live_classroom(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/v2/web/course/123",
            {'[class*="submit-btn"]': [FakeItem()]},
        )
        self.assertFalse(make_bot()._is_classroom_page(as_page(page)))

    def test_timeline_is_strong_classroom_evidence(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/pro/abc",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        self.assertTrue(make_bot()._is_classroom_page(as_page(page)))

    def test_hidden_timeline_is_not_classroom_evidence(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/pro/abc",
            {'[class*="timeline__"]': [FakeItem(visible=False)]},
        )
        self.assertFalse(make_bot()._is_classroom_page(as_page(page)))

    def test_url_and_slide_together_identify_classroom(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/123",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        self.assertTrue(make_bot()._is_classroom_page(as_page(page)))

    def test_finder_prefers_newest_valid_page(self):
        old = FakePage(
            "https://changjiang.yuketang.cn/lesson/old",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        new = FakePage(
            "https://changjiang.yuketang.cn/lesson/new",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        bot = make_bot([old, new])

        self.assertIs(bot._find_classroom_in_pages(announce=False), new)
        self.assertIs(bot.browser.current, new)

    def test_click_active_class_expands_summary_then_clicks_course(self):
        page = FakePage("https://changjiang.yuketang.cn/v2/web/index")
        course = FakeItem()

        def expand():
            page.selectors[".onlesson .jump_lesson__bar"] = [course]

        page.selectors[".onlesson > .tipbar"] = [FakeItem(on_click=expand)]
        bot = make_bot()

        self.assertTrue(bot._click_active_class(as_page(page)))
        self.assertTrue(course.clicked)

    def test_summary_without_concrete_course_fails_safely(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/v2/web/index",
            {".onlesson > .tipbar": [FakeItem()]},
        )
        self.assertFalse(make_bot()._click_active_class(as_page(page)))

    def test_new_quiz_prompt_is_clicked(self):
        prompt = FakeItem()
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/123",
            {"text:你有新的课堂习题": [prompt]},
        )

        self.assertTrue(make_bot()._open_new_quiz(as_page(page)))
        self.assertTrue(prompt.clicked)

    def test_only_newly_opened_classroom_page_is_followed(self):
        old = FakePage(
            "https://changjiang.yuketang.cn/lesson/old",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        new = FakePage(
            "https://changjiang.yuketang.cn/lesson/new",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        bot = make_bot([old, new])
        existing = (old,)

        self.assertIs(bot._find_new_classroom_page(existing), new)

    def test_existing_shell_page_is_not_treated_as_new_after_route_change(self):
        ppt = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/6",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        shell = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123?source=5"
        )
        bot = make_bot([ppt, shell])
        existing = tuple(bot.browser.pages)

        shell.url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/7"
        shell.selectors['section[class*="slide__cmp"]'] = [FakeItem()]

        self.assertIsNone(bot._find_new_classroom_page(existing))

    def test_finder_prefers_ppt_page_over_newer_shell_page(self):
        ppt = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/6",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        shell = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123?source=5",
            {'[class*="timeline__"]': [FakeItem()]},
        )
        bot = make_bot([ppt, shell])

        self.assertIs(bot._find_classroom_in_pages(announce=False), ppt)


class BrowserManagerPageTests(unittest.TestCase):
    def test_debug_port_is_local_only(self):
        browser = BrowserManager(debug_port=9222)
        self.assertIn("--remote-debugging-address=127.0.0.1", browser._launch_args())
        self.assertIn("--remote-debugging-port=9222", browser._launch_args())

    def test_debug_port_can_be_read_from_environment(self):
        with patch.dict(os.environ, {"RAINCLASS_DEBUG_PORT": "9333"}):
            browser = BrowserManager()
        self.assertIn("--remote-debugging-port=9333", browser._launch_args())

    def test_use_page_updates_current_page(self):
        page = FakePage("https://changjiang.yuketang.cn/lesson/123")
        browser = BrowserManager()

        self.assertIs(browser.use_page(as_page(page)), page)
        self.assertIs(browser.page, page)
        self.assertTrue(page.front)

    def test_use_page_rejects_closed_page(self):
        browser = BrowserManager()
        with self.assertRaises(RuntimeError):
            browser.use_page(as_page(FakePage("about:blank", closed=True)))


class QuestionStateTests(unittest.TestCase):
    def test_strict_option_parser_accepts_only_answer_shaped_text(self):
        self.assertEqual(Bot._parse_options("A,B,C"), ["A", "B", "C"])
        self.assertEqual(Bot._parse_options("答案为 A。"), ["A"])
        self.assertEqual(Bot._parse_options("CAFE"), [])
        self.assertEqual(Bot._parse_options("Error code: 429"), [])

    def test_judgment_parser_requires_exact_answer(self):
        self.assertIs(Bot._parse_judgment("正确"), True)
        self.assertIs(Bot._parse_judgment("答案：F"), False)
        self.assertIsNone(Bot._parse_judgment("THE ANSWER IS TRUE"))

    def test_failed_question_is_not_retried_but_completed_is_not(self):
        bot = make_bot()
        self.assertTrue(bot._begin_question("q1"))
        bot._finish_question("q1", False)
        self.assertFalse(bot._begin_question("q1"))

    def test_question_id_uses_visible_question_content_and_classroom_scope(self):
        page_a = FakePage(
            "https://changjiang.yuketang.cn/lesson/1?class=10",
            evaluated={"id": "q1", "text": "题目 A", "options": ["A:甲"], "images": []},
        )
        page_b = FakePage(
            "https://changjiang.yuketang.cn/lesson/1?class=11",
            evaluated={"id": "q1", "text": "题目 A", "options": ["A:甲"], "images": []},
        )
        bot = make_bot()

        first = bot._question_id(as_page(page_a), None)
        self.assertEqual(first, bot._question_id(as_page(page_a), None))
        self.assertNotEqual(first, bot._question_id(as_page(page_b), None))

    def test_signed_image_query_does_not_change_question_id(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "", "text": "题目", "options": [], "images": []},
        )
        bot = make_bot()
        first = bot._question_id(as_page(page), "https://img/a.png?sign=one")
        second = bot._question_id(as_page(page), "https://img/a.png?sign=two")
        self.assertEqual(first, second)

    def test_stable_question_id_ignores_dynamic_text(self):
        first_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q1", "text": "倒计时 10", "options": ["A:甲"], "images": []},
        )
        second_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q1", "text": "倒计时 9", "options": ["A:甲"], "images": []},
        )
        bot = make_bot()

        self.assertEqual(
            bot._question_id(as_page(first_page), None),
            bot._question_id(as_page(second_page), None),
        )

    def test_failed_submission_is_not_retried(self):
        # AI 请求成本高；同一道题失败后不再自动发起重复请求。
        bot = make_bot()
        self.assertTrue(bot._begin_question("q1"))
        bot._finish_question("q1", False)
        self.assertFalse(bot._begin_question("q1"))


class AnswerActionTests(unittest.TestCase):
    def test_exercise_html_is_saved_once_per_continuous_visit(self):
        exercise = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise"
        )
        ppt = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/8"
        )
        bot = make_bot()

        with (
            patch("src.bot.os.makedirs"),
            patch("builtins.open", mock_open()),
        ):
            bot._answer(as_page(exercise))
            bot._answer(as_page(exercise))
            bot._answer(as_page(ppt))
            bot._answer(as_page(exercise))

        self.assertEqual(exercise.content_calls, 2)

    def test_plain_ppt_does_not_call_answer_api(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/7",
            {'section[class*="slide__cmp"]': [FakeItem()]},
        )
        bot = make_bot()

        bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_homepage_does_not_call_answer_api(self):
        page = FakePage("https://changjiang.yuketang.cn/v2/web/index")
        bot = make_bot()

        bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_exercise_with_visible_options_calls_answer_api_once(self):
        option = FakeItem()
        submit = FakeItem(text="提交答案")
        slide = FakeItem(
            selectors={
                "p[data-option]": [option],
                '[class*="submit-btn"]': [submit],
            }
        )
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {
                'section[class*="slide__cmp"]': [slide],
                '[class*="submit-btn"]:has-text("提交答案")': [submit],
            },
            evaluated={"id": "q1", "text": "题目", "options": ["A:甲"], "images": []},
        )
        bot = make_bot()

        with patch.object(bot, "_save_exercise_html"):
            bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 1)

    def test_exercise_without_clickable_options_does_not_call_answer_api(self):
        question = FakeItem()
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {
                "[data-question-id]": [question],
                '[class*="submit-btn"]': [FakeItem(text="提交答案")],
            },
            evaluated={"id": "q2", "text": "请作答", "options": [], "images": []},
        )
        bot = make_bot()

        with patch.object(bot, "_save_exercise_html"):
            bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_exercise_with_only_hidden_options_does_not_call_answer_api(self):
        hidden_option = FakeItem(visible=False)
        submit = FakeItem(text="提交答案")
        slide = FakeItem(selectors={"p[data-option]": [hidden_option]})
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/2",
            {
                'section[class*="slide__cmp"]': [slide],
                '[class*="submit-btn"]:has-text("提交答案")': [submit],
            },
            evaluated={"id": "q2", "text": "题目", "options": [], "images": []},
        )
        bot = make_bot()

        with patch.object(bot, "_save_exercise_html"):
            bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_quiz_like_ppt_does_not_call_answer_api(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/ppt/8",
            {
                "p[data-option]": [FakeItem()],
                '[class*="submit-btn"]': [FakeItem(text="提交答案")],
            },
        )
        bot = make_bot()

        bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_completed_exercise_without_submit_button_does_not_call_answer_api(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {"p[data-option]": [FakeItem()]},
        )
        bot = make_bot()

        with patch.object(bot, "_save_exercise_html"):
            bot._answer(as_page(page))
        self.assertEqual(bot.ai.answer_calls, 0)

    def test_ai_result_survives_dynamic_dom_changes_on_same_exercise(self):
        option = FakeItem()
        submit = FakeItem()
        scope = FakeItem(
            text="多选题",
            selectors={
                'p[data-option="B"]': [option],
                '[class*="submit-btn"]': [submit],
                '[class*="submit-btn"]:has-text("提交答案")': [submit],
            },
        )
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {
                'section[class*="slide__cmp"]': [scope],
                '[class*="submit-btn"]:has-text("提交答案")': [submit],
            },
            evaluated={"id": "initial", "text": "题目加载中", "options": [], "images": []},
        )
        ai = FakeAI(answer='{"type":"multi","answers":["B"]}', complete=False)
        bot = make_bot(ai=ai)

        with patch.object(bot, "_capture_question_image", return_value=None):
            bot._handle_quiz(as_page(page))
            page.evaluated = {
                "id": "rendered-later",
                "text": "完整题干",
                "options": ["B:正确答案"],
                "images": [],
            }
            ai.last_future.set_result('{"type":"multi","answers":["B"]}')
            with patch.object(bot, "_submit_answer", return_value=True):
                bot._answer(as_page(page))

        self.assertTrue(option.clicked)
        initial_id = bot._question_id(
            as_page(FakePage(page.url, evaluated={"id": "initial"})), None
        )
        self.assertEqual(bot._question_states[initial_id][0], "completed")

    def test_multi_letters_click_all_options(self):
        # 统一 JSON 提示词后不再区分单选/多选：多字母按逐一点击处理。
        option_a = FakeItem()
        option_b = FakeItem()
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            {
                'p[data-option="A"]': [option_a],
                'p[data-option="B"]': [option_b],
            },
        )
        self.assertTrue(make_bot()._click_options(as_page(page), ["A", "B"]))
        self.assertTrue(option_a.clicked)
        self.assertTrue(option_b.clicked)

    def test_missing_multi_choice_option_prevents_all_clicks(self):
        option_a = FakeItem()
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            {'p[data-option="A"]': [option_a]},
        )
        self.assertFalse(make_bot()._click_options(as_page(page), ["A", "B"]))
        self.assertEqual(option_a.click_count, 0)

    def test_partial_multi_choice_failure_rolls_back_prior_click(self):
        option_a = FakeItem()
        option_b = FakeItem(click_error=RuntimeError("blocked"))
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            {
                'p[data-option="A"]': [option_a],
                'p[data-option="B"]': [option_b],
            },
        )
        self.assertFalse(make_bot()._click_options(as_page(page), ["A", "B"]))
        self.assertEqual(option_a.click_count, 2)

    def test_submit_confirmed_by_button_disappearing(self):
        # 准则：提交成功 = 提交按钮消失（不依赖任何文本证据）。
        def remove_button():
            page.selectors['[class*="submit-btn"]:has-text("提交答案")'] = []

        submit = FakeItem(on_click=remove_button)
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {'[class*="submit-btn"]:has-text("提交答案")': [submit]},
        )
        self.assertTrue(make_bot()._submit_answer(as_page(page)))

    def test_submit_button_still_present_means_unanswered(self):
        # 准则：点击提交后按钮仍在 = 未作答，返回 False 以便重试。
        submit = FakeItem()
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise",
            {'[class*="submit-btn"]:has-text("提交答案")': [submit]},
        )
        bot = make_bot()
        bot.stop_event = cast(Any, StoppingEvent())
        page.wait_for_timeout = lambda timeout: (_ for _ in ()).throw(
            RuntimeError("interrupted")
        )
        self.assertFalse(bot._submit_answer(as_page(page)))

    def test_judgment_f_selects_false_option(self):
        # 统一 JSON 提示词后判断题走文案映射兜底（AI 返回对/错类文案时）。
        true_option = FakeItem(text="正确")
        false_option = FakeItem(text="错误")
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            {"p[data-option]": [true_option, false_option]},
        )

        self.assertTrue(make_bot()._click_judgment(as_page(page), "F"))
        self.assertFalse(true_option.clicked)
        self.assertTrue(false_option.clicked)

    def test_actions_are_scoped_to_current_question_container(self):
        stale_option = FakeItem()
        current_option = FakeItem()
        current = FakeItem(
            selectors={"p[data-option=\"B\"]": [current_option]},
        )
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            {
                'section[class*="slide__cmp"]': [current],
                'p[data-option="B"]': [stale_option],
            },
        )

        self.assertTrue(make_bot()._click_options(as_page(page), ["B"]))
        self.assertTrue(current_option.clicked)
        self.assertFalse(stale_option.clicked)

    def test_click_failure_is_not_submitted_or_completed(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q1", "text": "题目", "options": ["A:甲"], "images": []},
        )
        bot = make_bot()
        bot.ai.answer = '{"type":"single","answers":"A"}'
        with (
            patch.object(bot, "_capture_question_image", return_value=None),
            patch.object(bot, "_click_options", return_value=False),
            patch.object(bot, "_submit_answer") as submit,
        ):
            bot._handle_quiz(as_page(page))

        submit.assert_not_called()
        question_id = bot._question_id(as_page(page), None)
        self.assertEqual(bot._question_states[question_id][0], "failed")

    def test_subjective_question_is_handled_once(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q1", "text": "请简答", "options": [], "images": []},
        )
        bot = make_bot()
        bot.ai.answer = '{"type":"sub"}'
        with (
            patch.object(bot, "_capture_question_image", return_value=None),
        ):
            bot._handle_quiz(as_page(page))
            bot._handle_quiz(as_page(page))

        self.assertEqual(bot.ai.answer_calls, 1)
        question_id = bot._question_id(as_page(page), None)
        self.assertEqual(bot._question_states[question_id][0], "completed")

    def test_unknown_visual_result_does_not_click_or_submit(self):
        page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1",
            evaluated={"id": "q1", "text": "题目", "options": ["A:甲"], "images": []},
        )
        bot = make_bot()
        bot.ai.answer = '{"type":"unknown","answers":"A"}'

        with (
            patch.object(bot, "_capture_question_image", return_value=None),
            patch.object(bot, "_click_options") as click,
            patch.object(bot, "_submit_answer") as submit,
        ):
            bot._handle_quiz(as_page(page))

        click.assert_not_called()
        submit.assert_not_called()
        question_id = bot._question_id(as_page(page), None)
        self.assertEqual(bot._question_states[question_id][0], "completed")

    def test_new_question_cancels_stale_ai_request(self):
        ai = FakeAI(complete=False)
        first_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q1", "text": "题目一", "options": ["A:甲"], "images": []},
        )
        second_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/1",
            evaluated={"id": "q2", "text": "题目二", "options": ["A:乙"], "images": []},
        )
        bot = make_bot(ai=ai)

        with patch.object(bot, "_capture_question_image", return_value=None):
            bot._handle_quiz(as_page(first_page))
            first_future = ai.last_future
            bot._handle_quiz(as_page(second_page))

        first_id = bot._question_id(as_page(first_page), None)
        second_id = bot._question_id(as_page(second_page), None)
        self.assertTrue(first_future.cancelled())
        self.assertEqual(bot._question_states[first_id][0], "failed")
        self.assertEqual(bot._question_states[second_id][0], "inflight")
        self.assertEqual(ai.answer_calls, 2)

    def test_ai_result_is_discarded_when_exercise_route_changes(self):
        ai = FakeAI(complete=False)
        first_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1",
            evaluated={"id": "q1", "text": "题目一", "options": ["A:甲"], "images": []},
        )
        second_page = FakePage(
            "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/2",
            evaluated={"id": "q2", "text": "题目二", "options": ["A:乙"], "images": []},
        )
        bot = make_bot(ai=ai)

        with patch.object(bot, "_capture_question_image", return_value=None):
            bot._handle_quiz(as_page(first_page))
            ai.last_future.set_result('{"type":"single","answers":"A"}')
            with (
                patch.object(bot, "_click_options") as click_options,
                patch.object(bot, "_submit_answer") as submit,
            ):
                bot._complete_pending_answer(as_page(second_page))

        click_options.assert_not_called()
        submit.assert_not_called()

    def test_parse_ai_answer_variants(self):
        parse = Bot._parse_ai_answer
        self.assertEqual(
            parse('{"type":"single","answers":"A"}'), ("single", ["A"], "A")
        )
        self.assertEqual(
            parse('{"type":"multi","answers":["A","B","D"]}'),
            ("multi", ["A", "B", "D"], ""),
        )
        self.assertEqual(parse('{"type":"fill"}'), ("fill", [], ""))
        self.assertEqual(parse('{"type":"sub"}'), ("sub", [], ""))
        self.assertEqual(parse('{"type":"unknown"}'), ("unknown", [], ""))
        self.assertEqual(
            parse('```json\n{"type":"single","answers":"C"}\n```'),
            ("single", ["C"], "C"),
        )
        # 判断题文案：无字母但 answers 是对/错类 → 交给文案映射
        self.assertEqual(parse('{"type":"single","answers":"对"}'), ("single", [], "对"))
        # 非法返回 → 解析失败
        self.assertEqual(parse("不是json"), (None, [], ""))
        self.assertEqual(parse('{"type":"single"}'), (None, [], ""))
        self.assertEqual(
            parse('{"type":"unknown","answers":"A"}'),
            ("unknown", [], ""),
        )


if __name__ == "__main__":
    unittest.main()
