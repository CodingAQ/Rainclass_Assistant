"""核心 Bot 逻辑 - 自动签到、答题、课程检测。

替代原 main.py 中 AutoClassBotApp 的业务逻辑部分，
使用 Playwright 替代 Selenium。
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from concurrent.futures import Future
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# 重试间隔（秒）
RETRY_DELAY = 10

# 点击课程后，课堂页可能需要等待后端创建并完成首屏渲染。
CLASSROOM_OPEN_TIMEOUT = 20

# 雨课堂下课后会把该消息永久保留在课堂时间线中。
CLASS_ENDED_SELECTOR = '//div[@title="下课啦！" and contains(@class, "timeline__msg")]'

# Cookie 有效期（天）—— 与 main.py 中的显示共用，抽成单一常量避免两处硬编码
COOKIE_VALID_DAYS = 14

# 有效的选择题选项
VALID_OPTIONS = ["A", "B", "C", "D", "E", "F"]


class Bot:
    """课堂自动化 Bot。在独立线程中运行主循环。"""

    def __init__(
        self,
        config: "Config",           # type: ignore
        browser: "BrowserManager",  # type: ignore
        ai_service: "AIService",    # type: ignore
        notification: "NotificationService",  # type: ignore
        stop_event: threading.Event,
    ):
        self.config = config
        self.browser = browser
        self.ai = ai_service
        self.notification = notification
        self.stop_event = stop_event
        self._question_states: dict[str, tuple[str, float, int]] = {}
        self._last_notify_time = 0.0
        self._signed_in = False
        self._last_classroom_url = ""
        self._last_sign_in_notify = 0.0
        self._answer_future: Optional[Future[str]] = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        self._last_unidentified_log = 0.0  # 「无法识别题目」日志节流
        self._exercise_html_saved = False
        self._ended_lesson_ids: set[str] = set()
        self._waiting_for_class_logged = False

    def log(self, message: str) -> None:
        """统一日志输出（只 emit 一次）。

        直接走 logging 根 logger 的 QueueHandler：
        QueueListener 会把同一条记录同时派发给文件处理器(RotatingFileHandler)
        与 GUI 处理器(_GuiLogHandler)，因此文件与界面都会各收到一次。
        切勿再二次 emit（例如同时走其他打印通道），否则同一条日志会打印两遍。
        """
        logger.info(message)

    def _int_setting(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _notify(self, title: str, content: str) -> None:
        """通知是非关键旁路；支持异步实现且绝不阻断答题。"""
        try:
            send_async = getattr(self.notification, "send_async", None)
            if callable(send_async):
                send_async(title, content)
            else:
                self.notification.send(title, content)
        except Exception as e:
            self.log(f"通知发送失败：{e}")

    # ==================== 时间窗口 ====================

    @staticmethod
    def _parse_hhmm(value: str):
        """解析 'HH:MM' 为 (时, 分) 元组，失败返回 None。"""
        try:
            h, m = value.split(":")
            return int(h), int(m)
        except Exception:
            return None

    @staticmethod
    def _in_time_window(current: str, start: str, end: str) -> bool:
        """判断 current(HH:MM) 是否落在 [start, end] 内，支持跨午夜窗口。

        - start <= end：普通窗口，要求 start <= current <= end。
        - start > end（如 22:00 - 06:00）：跨午夜，要求 current >= start 或 current <= end。
        """
        ct = Bot._parse_hhmm(current)
        st = Bot._parse_hhmm(start)
        et = Bot._parse_hhmm(end)
        if ct is None or st is None or et is None:
            return False
        cur = ct[0] * 60 + ct[1]
        s = st[0] * 60 + st[1]
        e = et[0] * 60 + et[1]
        if s <= e:
            return s <= cur <= e
        return cur >= s or cur <= e

    # ==================== 主循环 ====================

    def run(self) -> None:
        """Bot 主循环（在后台线程中执行）。

        浏览器创建/导航/关闭全在本线程内完成，避免 Playwright 跨线程报错。
        """
        try:
            # 浏览器创建、使用和关闭都留在 Bot 线程内。
            if not self.browser.start():
                self.log("浏览器启动失败，无法开始自动答题。")
                return
            self.log("浏览器已就绪。")

            if not self.browser.has_session:
                self.log("⚠ 未检测到登录会话（browser_state.json），请先在设置中「获取登录 Cookies」。")
            elif self.browser.navigate_to_class() and self.browser.is_logged_in():
                self.log("登录会话有效。")
            else:
                self.log("⚠ 登录会话可能已过期，请重新「获取登录 Cookies」后再启动。")

            while not self.stop_event.is_set():
                check_interval = self._int_setting("check_interval", 60, 5, 3600)
                current_time = time.strftime("%H:%M", time.localtime())
                start_time = self.config.get("start_time", "07:00")
                end_time = self.config.get("end_time", "22:00")

                self._check_cookie_warning()

                if self._in_time_window(current_time, start_time, end_time):
                    # 具体检查日志由 _get_into_class 输出，避免每轮重复打印。
                    self._get_into_class()
                else:
                    self.log(
                        f"当前时间不在检查时间段内 ({start_time} - {end_time})，跳过检查。"
                    )

                # 每轮统一等待，无论是否在时间窗口内都按 check_interval 节奏休眠，
                # 避免忙等空转吃满 CPU（原代码此处被错误缩进到 while 之外）。
                self.stop_event.wait(check_interval)

        except Exception:
            self.log(f"发生意外错误：{traceback.format_exc()}")
        finally:
            self.browser.stop()
            self.ai.shutdown()
            self.log("浏览器已关闭。")

    # ==================== 课程检测 ====================

    def _log_waiting_for_class_once(self) -> None:
        """每个等待课程阶段只输出一次状态，后台检测照常继续。"""
        if self._waiting_for_class_logged:
            return
        self._waiting_for_class_logged = True
        self.log("未找到可进入的课程")
        self.log("等待课程中……")

    def _get_into_class(self) -> None:
        """进入正在进行的课程——优先复用已有课堂标签页，没有才重新导航。"""
        while not self.stop_event.is_set():
            # 1. 先找已有课堂标签页
            classroom_page = self._find_existing_classroom()
            if classroom_page:
                self.log("检测到已有课堂标签页，直接进入。")
                self._run_classroom_loop(classroom_page)
                return

            # 2. 没有 → 导航到主页面找课
            if not self.browser.ensure_running():
                self.log(f"浏览器不可用，{RETRY_DELAY} 秒后重试...")
                self.stop_event.wait(RETRY_DELAY)
                continue

            # run() 启动时已经为登录校验导航过首页。避免紧接着重复刷新，
            # 否则第一次课程检测可能仍拿着刷新前的页面状态。
            try:
                on_home_page = self._is_home_page(self.browser.page)
            except Exception:
                on_home_page = False
            if not on_home_page:
                if not self.browser.navigate_to_class():
                    self.stop_event.wait(RETRY_DELAY)
                    continue

            page = self.browser.page
            self._debug_dump(page, "main-page")

            onlesson_selector = ".onlesson"

            try:
                page.wait_for_selector(onlesson_selector, timeout=10_000)

                # 首页加载期间，之前的进入请求可能已经创建并加载了课堂页。
                # 必须在再次点击课程前刷新标签页扫描结果，避免重复进入。
                classroom_page = self._find_classroom_in_pages(announce=False)
                if classroom_page:
                    self.log("课程列表加载期间检测到课堂页，取消重复进入。")
                    self._run_classroom_loop(classroom_page)
                    return

                if not self._click_active_class(page):
                    self._log_waiting_for_class_once()
                    return

                # 新标签页和同页跳转都轮询验证，不等待经常无法达到的 networkidle。
                classroom_page = self._wait_for_classroom_page(CLASSROOM_OPEN_TIMEOUT)
                self._debug_save_tabs()

                # 诊断：列出所有标签页
                try:
                    pages = self.browser.pages
                    self.log(f"当前共 {len(pages)} 个标签页：")
                    for i, p in enumerate(pages):
                        try:
                            self.log(f"  [{i}] {p.url[:120]}")
                        except Exception:
                            self.log(f"  [{i}] （无法读取 URL）")
                except Exception:
                    pass

                if classroom_page:
                    self.log(f"匹配到课堂页：{classroom_page.url[:100]}")
                    self._debug_dump(classroom_page, "classroom")
                    self._run_classroom_loop(classroom_page)
                    return

                # 首页不是课堂；失败后交给下一轮重新发现，不能在首页死循环。
                self._log_waiting_for_class_once()
                return

            except PlaywrightTimeout:
                self._log_waiting_for_class_once()
                return
            except Exception as e:
                self.log(f"课程检测发生错误：{e}，{RETRY_DELAY} 秒后重试...")
                self.stop_event.wait(RETRY_DELAY)
                continue

    @staticmethod
    def _is_home_page(page: Page) -> bool:
        try:
            url = page.url.lower().rstrip("/")
        except Exception:
            return False
        return url == "https://changjiang.yuketang.cn" or "/v2/web/index" in url

    def _wait_for_classroom_page(self, timeout: float) -> Optional[Page]:
        """等待课堂页，并持续处理 Playwright 的新页面事件。"""
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            classroom_page = self._find_classroom_in_pages(announce=False)
            if classroom_page:
                return classroom_page
            try:
                # context.pages 是事件驱动的本地快照；threading.Event.wait()
                # 不会泵送 Playwright 消息，新标签页会一直不可见到下一次 API 调用。
                self.browser.page.wait_for_timeout(100)
            except Exception:
                if self.stop_event.wait(0.1):
                    break
        return None

    # ------- 标签页查找 -------

    def _find_existing_classroom(self) -> Optional[Page]:
        """检查已有标签页中是否有课堂（优先 URL 匹配，其次 DOM 匹配）。"""
        return self._find_classroom_in_pages()

    def _find_classroom_in_pages(self, announce: bool = True) -> Optional[Page]:
        """选择通过强校验的课堂页，优先使用明确的 PPT 页面。"""
        candidates = []
        for index, page in enumerate(self.browser.pages):
            try:
                lesson_id = self._lesson_id_from_url(page.url)
                if lesson_id and lesson_id in self._ended_lesson_ids:
                    continue
                if self._is_classroom_page(page):
                    is_ppt = bool(re.search(r"/ppt/\d+(?:[/?#]|$)", page.url.lower()))
                    candidates.append((is_ppt, index, page))
            except Exception:
                continue

        if not candidates:
            return None
        page = max(candidates, key=lambda item: (item[0], item[1]))[2]
        self.browser.use_page(page)
        if announce:
            self.log(f"匹配到课堂页：{page.url[:100]}")
            self._debug_dump(page, "classroom")
        return page

    def _click_active_class(self, page: Page) -> bool:
        """点击具体的在课课程；多课程时先展开汇总栏。"""
        courses = page.locator(".onlesson .jump_lesson__bar")
        if self._click_first_visible(courses):
            return True

        summary = page.locator(".onlesson > .tipbar")
        if not self._click_first_visible(summary):
            return False
        self.stop_event.wait(0.5)
        return self._click_first_visible(page.locator(".onlesson .jump_lesson__bar"))

    @staticmethod
    def _click_first_visible(locator) -> bool:
        """点击 locator 中第一个可见元素。"""
        item = Bot._first_visible(locator)
        if item is None:
            return False
        try:
            item.click(timeout=5_000)
            return True
        except Exception:
            return False

    @staticmethod
    def _first_visible(locator):
        try:
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            return None
        return None

    @staticmethod
    def _last_visible(locator):
        """返回 locator 中最后一个可见元素，适配保留历史 slide 的页面。"""
        try:
            for index in range(locator.count() - 1, -1, -1):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                try:
                    in_viewport = item.evaluate(
                        """el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && r.bottom > 0 &&
                                r.right > 0 && r.top < window.innerHeight &&
                                r.left < window.innerWidth;
                        }"""
                    )
                    if not in_viewport:
                        continue
                except Exception:
                    pass
                return item
        except Exception:
            return None
        return None

    def _question_scope(self, page: Page):
        """定位当前可见题目容器；未知页面结构时保守回退到页面。"""
        for selector in (
            'section[class*="slide__cmp"]',
            '[data-question-id]',
            '[data-problem-id]',
        ):
            try:
                item = self._last_visible(page.locator(selector))
                if item is not None:
                    return item
            except Exception:
                continue
        return page

    def _is_classroom_page(self, page: Page) -> bool:
        """保守识别实时课堂，明确排除首页和普通课程页。"""
        try:
            if page.is_closed():
                return False
            url = page.url.lower()
        except Exception:
            return False

        if "/v2/web/index" in url or url.rstrip("/") == "https://changjiang.yuketang.cn":
            return False

        has_timeline = self._has_any(page, [
            '[class*="timeline__"]',
        ])
        has_quiz_prompt = self._has_any(page, [
            'text=你有新的课堂习题',
        ])
        if has_timeline or has_quiz_prompt:
            return True

        url_hint = any(key in url for key in ("/lesson/", "/pro/lesson", "classroom"))
        has_classroom_content = self._has_any(page, [
            'section[class*="slide__cmp"]',
            '[class*="submit-btn"]',
        ])
        return url_hint and has_classroom_content

    @staticmethod
    def _has_any(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                if Bot._first_visible(page.locator(selector)) is not None:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _lesson_id_from_url(url: str) -> str:
        """从雨课堂课堂 URL 中提取 lesson ID。"""
        try:
            path = urlsplit(url).path.lower()
        except Exception:
            return ""
        match = re.search(r"/lesson/(?:fullscreen/v\d+/)?(\d+)(?:/|$)", path)
        return match.group(1) if match else ""

    def _pages_for_lesson(self, lesson_id: str, current_page: Page) -> list[Page]:
        """返回同一 lesson 的全部页面；无法提取 ID 时仅处理当前页。"""
        if not lesson_id:
            return [current_page]

        pages: list[Page] = []
        try:
            candidates = tuple(self.browser.pages)
        except Exception:
            candidates = ()
        for candidate in candidates:
            try:
                if self._lesson_id_from_url(candidate.url) == lesson_id:
                    pages.append(candidate)
            except Exception:
                continue
        if not any(candidate is current_page for candidate in pages):
            pages.append(current_page)
        return pages

    @staticmethod
    def _has_class_ended_signal(page: Page) -> bool:
        try:
            return page.locator(CLASS_ENDED_SELECTOR).count() > 0
        except Exception:
            return False

    def _handle_class_ended(self, current_page: Page) -> bool:
        """检测同 lesson 的下课消息，并让该课程在本进程内永久收敛。"""
        try:
            lesson_id = self._lesson_id_from_url(current_page.url)
        except Exception:
            lesson_id = ""
        lesson_pages = self._pages_for_lesson(lesson_id, current_page)
        if not any(self._has_class_ended_signal(page) for page in lesson_pages):
            return False

        # 必须先标记再关闭。即使某个 close() 失败，标签页发现流程也不会
        # 再次把这个已经结束的 lesson 当作正在进行的课堂。
        if lesson_id:
            self._ended_lesson_ids.add(lesson_id)
        if self._answer_future is not None:
            self._abandon_pending_answer("检测到下课")

        self.log("检测到下课啦！自动答题已停止。")
        closed = 0
        failed = 0
        for page in lesson_pages:
            try:
                page.close()
                closed += 1
            except Exception as exc:
                failed += 1
                logger.warning("关闭已结束课堂标签页失败：%s", exc)

        lesson_label = lesson_id or "当前课堂"
        self.log(f"已清理课程 {lesson_label} 的 {closed} 个标签页。")
        if failed:
            self.log(f"另有 {failed} 个课堂标签页关闭失败，后续将忽略这些页面。")
        return True

    def _run_classroom_loop(self, page: Page) -> None:
        """在课堂页面内循环签到/答题直到下课。"""
        self._waiting_for_class_logged = False
        self.browser.use_page(page)
        self._last_classroom_url = ""
        self.log("我去上课啦！")

        invalid_checks = 0

        while not self.stop_event.is_set():
            quiz_interval = self._int_setting("quiz_refresh_interval", 1, 1, 300)
            try:
                # 当前页可能是没有 timeline 的 exercise；必须扫描同 lesson 的
                # PPT/入口页，才能及时收到老师结束课堂的消息。
                if self._handle_class_ended(page):
                    return

                if page.is_closed():
                    self.log("课堂标签页已关闭，返回课程发现流程。")
                    return

                if not self._is_classroom_page(page):
                    replacement = self._find_classroom_in_pages(announce=False)
                    if replacement is None:
                        invalid_checks += 1
                        if invalid_checks >= 3:
                            self.log("课堂页面已失效，返回课程发现流程。")
                            return
                        self.stop_event.wait(1)
                        continue
                    page = replacement
                    self.browser.use_page(page)
                invalid_checks = 0

                # 新题可能在提示点击后同页展示，也可能打开新标签页。
                pages_before_prompt = tuple(self.browser.pages)
                clicked_prompt = self._open_new_quiz(page)
                new_page = None
                if clicked_prompt:
                    deadline = time.monotonic() + 3
                    while not self.stop_event.is_set() and time.monotonic() < deadline:
                        new_page = self._find_new_classroom_page(pages_before_prompt)
                        if new_page is not None:
                            break
                        self.stop_event.wait(0.1)
                if new_page is not None:
                    page = new_page
                    self.browser.use_page(page)
                    self.log(f"已跟随到新的课堂标签页：{page.url[:100]}")

                cur = page.url
                if cur and cur != self._last_classroom_url:
                    self._last_classroom_url = cur
                    self._signed_in = False
                    path = urlsplit(cur).path.lower()
                    if "exercise" in path:
                        self.log(f"进入新的习题页：{cur[:100]}")
                    elif re.search(r"/ppt(?:/|$)", path):
                        self.log(f"进入新的 PPT 页：{cur[:100]}")

                self._check_and_sign_in(page)
                self._answer(page)
            except Exception as e:
                self.log(f"课堂页面处理失败：{e}，稍后重试。")
                if self.stop_event.wait(min(quiz_interval, RETRY_DELAY)):
                    return
                continue

            self.stop_event.wait(quiz_interval)

    def _find_new_classroom_page(self, existing_pages: tuple[Page, ...]) -> Optional[Page]:
        """返回一次明确操作后真正新建并已加载为课堂的标签页。"""
        existing_ids = {id(page) for page in existing_pages}
        for candidate in reversed(self.browser.pages):
            if id(candidate) in existing_ids:
                continue
            try:
                lesson_id = self._lesson_id_from_url(candidate.url)
            except Exception:
                lesson_id = ""
            if lesson_id and lesson_id in self._ended_lesson_ids:
                continue
            if self._is_classroom_page(candidate):
                return candidate
        return None

    def _open_new_quiz(self, page: Page) -> bool:
        """消费雨课堂的新题提示，让页面切到最新题目。"""
        prompt = page.get_by_text("你有新的课堂习题", exact=False)
        if self._click_first_visible(prompt):
            self.log("发现新的课堂习题，已点击提示。")
            return True
        return False

    # ==================== 签到 ====================

    def _check_and_sign_in(self, page: Page) -> None:
        """检测并执行签到——同一课堂只签一次。

        精确匹配文本恰为「签到」的可点击元素，避免误匹配「已签到 / 签到记录」等
        静态文案；只有找不到签到按钮属正常情况（静默返回），真正的失败会记日志。
        """
        if self._signed_in:
            return
        sign_in_btn = page.get_by_text("签到", exact=True)
        # 先即时检查是否有可见签到按钮，避免无签到时固定等待 5 秒。
        try:
            if sign_in_btn.first.count() == 0 or not sign_in_btn.first.is_visible():
                return
        except Exception:
            return  # 无签到按钮属正常情况

        self.log("检测到签到。")
        now = time.time()
        if now - self._last_sign_in_notify > 60:
            try:
                self._notify("自动签到提醒", "课程有签到任务，正在自动签到。")
                self._last_sign_in_notify = now
            except Exception as e:
                self.log(f"签到通知发送失败：{e}")

        try:
            sign_in_btn.first.click()
            self._signed_in = True
            self.log("已成功自动签到。")
        except Exception as e:
            self.log(f"签到点击失败：{e}")

    # ==================== 答题 ====================

    def _answer(self, page: Page) -> None:
        """
        可答判定 → AI 答题 → 点击选项 → 提交 → 微信通知。

        不判断题型：exercise URL 是题目页的权威信号，提交按钮存在表示
        题目仍可作答，两者同时满足即发起 AI 请求（统一 JSON 提示词）。
        主观/填空题由 AI 返回的 type 识别，仅记录不点击。
        """
        if not self._is_exercise_page(page):
            self._exercise_html_saved = False
            if self._answer_future is not None:
                self._abandon_pending_answer("答题页面已关闭")
            return

        if not self._exercise_html_saved:
            self._exercise_html_saved = True
            self._save_exercise_html(page)

        if not self._has_submit_button(page):
            if self._answer_future is not None:
                self._finish_pending_as_completed("提交答案按钮已消失")
            return

        # AI 请求期间只核对廉价且稳定的路由和提交按钮状态。题目 DOM 会在
        # 渐进渲染时变化，不能用重新计算的内容哈希判断是否切题。
        if self._answer_future is not None:
            if self._answer_future.done():
                self._complete_pending_answer(page)
            return

        # 当前点击逻辑只支持 data-option 客观题；没有可操作选项时不调用 AI。
        if not self._has_answerable_options(page):
            return

        # --- 触发：exercise 页 + 提交按钮 + 可见选项同时满足才发起 ---
        self._debug_dump(page, "quiz-dom")
        self._handle_quiz(page)

    @staticmethod
    def _is_exercise_page(page: Page) -> bool:
        """URL 路径中包含 exercise 时才认为当前处于题目页。"""
        try:
            return "exercise" in urlsplit(page.url).path.lower()
        except Exception:
            return False

    def _save_exercise_html(self, page: Page) -> None:
        """普通和调试模式都为每次 exercise 页面保存一份 HTML。"""
        now = datetime.now()
        data_dir = os.path.join("data", now.strftime("%Y-%m-%d"))
        path = os.path.join(
            data_dir,
            f"{now.strftime('%H-%M-%S-%f')}-exercise.html",
        )
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as file:
                file.write(page.content())
            self.log(f"题目 HTML 已保存：{path}")
        except Exception as e:
            self.log(f"题目 HTML 保存失败：{e}")

    def _handle_quiz(self, page: Page) -> None:
        """发起当前题目的异步 AI 请求（统一 JSON 提示词，不判断题型）。"""
        image_url = self._capture_question_image(page)
        question_id = self._question_id(page, image_url)
        if not question_id:
            now = time.time()
            if now - self._last_unidentified_log > 30:
                self._last_unidentified_log = now
                self.log("无法稳定识别当前题目，跳过自动作答（30 秒内不再提示）。")
            return

        if self._answer_future is not None:
            if question_id != self._answer_question_id:
                self._abandon_pending_answer("检测到新的题目")
            elif self._answer_future.done():
                self._complete_pending_answer(page)
                return
            else:
                return
        if not self._begin_question(question_id):
            return

        self.log("检测到可作答题目，开始获取 AI 答案。")
        try:
            if time.time() - self._last_notify_time > 30:
                self._notify(
                    "自动答题提醒",
                    "课程检测到新题目，正在获取 AI 答案...",
                )
                self._last_notify_time = time.time()

            if image_url:
                future = self.ai.submit_answer(
                    image_url=image_url,
                    cookies=self.browser.get_cookies_dict(),
                )
            else:
                scope = self._question_scope(page)
                screenshot_b64 = base64.b64encode(
                    scope.screenshot(type="png")
                ).decode("utf-8")
                future = self.ai.submit_answer(image_b64=screenshot_b64)
        except Exception as e:
            self.log(f"AI 答题请求启动失败：{e}")
            self._finish_question(question_id, False)
            return

        self._answer_future = future
        self._answer_question_id = question_id
        self._answer_exercise_path = self._exercise_path(page)

        # 测试桩或缓存结果可能立即完成。
        if future.done():
            self._complete_pending_answer(page)

    def _complete_pending_answer(self, page: Page) -> None:
        future = self._answer_future
        question_id = self._answer_question_id
        self._answer_future = None
        self._answer_question_id = ""
        if future is None or not question_id:
            return

        succeeded = False
        try:
            answer_text = future.result()
            self.log(f"AI 返回：{answer_text}")
            if self._is_failed_answer(answer_text):
                self.log("AI 未返回有效答案，本题不再自动重试。")
                return
            qtype, letters, raw_answer = self._parse_ai_answer(answer_text)
            if qtype is None:
                self.log("AI 返回格式不符合约定，本题不再自动重试。")
                return
            if qtype == "unknown":
                self.log("AI 模型没有可用的视觉能力，已跳过本题。")
                succeeded = True
                return
            if qtype in ("fill", "sub"):
                self.log("AI 判定为主观/填空题，不自动作答，本题结束。")
                succeeded = True
                return

            if not self._is_exercise_page(page):
                self.log("AI 返回前答题页面已关闭，已丢弃旧答案。")
                return
            if self._exercise_path(page) != self._answer_exercise_path:
                self.log("AI 返回前题目路由已经变化，已丢弃旧答案。")
                return
            if not self._has_submit_button(page):
                self.log("AI 返回时提交答案按钮已消失，本题不再重复作答。")
                succeeded = True
                return

            if letters:
                clicked = self._click_options(page, letters)
            else:
                # 无字母：可能是判断题文案（对/错/正确/错误等），按选项文案映射
                clicked = self._click_judgment(page, raw_answer)
                if not clicked:
                    self.log("AI 未能提供有效选项，本题不再自动重试。")
                    self._notify_answer_failure(answer_text)
                    return
            if not clicked:
                return
            # 点击选项后确认选择已生效（按钮进入 can 态），否则提交会命中
            # 灰色按钮而不生效，还会误判为已作答。
            if not self._wait_submit_ready(page):
                self.log("选项点击未生效（提交按钮未进入可提交状态），本题不再自动重试。")
                return
            submit_delay = self._int_setting("submit_delay", 1, 0, 300)
            if submit_delay > 0 and self.stop_event.wait(submit_delay):
                return
            succeeded = self._submit_answer(page)
        except Exception as e:
            self.log(f"AI 答题结果处理失败：{e}")
        finally:
            self._finish_question(question_id, succeeded)

    def _finish_pending_as_completed(self, reason: str) -> None:
        """提交按钮消失时结束尚未完成的 AI 请求。"""
        future = self._answer_future
        question_id = self._answer_question_id
        self._answer_future = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        if future is not None:
            future.cancel()
        if question_id:
            self.log(f"{reason}，本题已结束处理。")
            self._finish_question(question_id, True)

    def _abandon_pending_answer(self, reason: str) -> None:
        future = self._answer_future
        question_id = self._answer_question_id
        self._answer_future = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        if future is not None:
            future.cancel()
        if question_id:
            self.log(f"{reason}，已丢弃旧题 AI 请求。")
            self._finish_question(question_id, False)

    def _begin_question(self, question_id: str) -> bool:
        """进入题目处理状态；同一道题只允许发起一次 AI 请求。"""
        state, _, attempts = self._question_states.get(
            question_id, ("pending", 0.0, 0)
        )
        if state in ("inflight", "completed", "failed"):
            return False
        self._set_question_state(question_id, "inflight", attempts + 1)
        return True

    def _finish_question(self, question_id: str, succeeded: bool) -> None:
        attempts = self._question_states.get(question_id, ("", 0.0, 1))[2]
        state = "completed" if succeeded else "failed"
        self._set_question_state(question_id, state, attempts)
        if succeeded:
            self.log("当前题目已确认处理完成。")
        else:
            self.log("当前题目处理失败，本轮不再自动重试。")

    @staticmethod
    def _exercise_path(page: Page) -> str:
        """返回题目路由，用于校验异步答案仍属于原题页面。"""
        try:
            return urlsplit(page.url).path.lower()
        except Exception:
            return ""

    def _set_question_state(self, question_id: str, state: str, attempts: int) -> None:
        # 仅保留最近 200 道题；需要跨进程历史时再持久化。
        self._question_states.pop(question_id, None)
        self._question_states[question_id] = (state, time.time(), attempts)
        while len(self._question_states) > 200:
            self._question_states.pop(next(iter(self._question_states)))

    @staticmethod
    def _parse_ai_answer(text: str) -> tuple[Optional[str], list[str], str]:
        """解析统一提示词的 JSON 返回。

        约定格式（见 AIService.PROMPT_ANSWER）：
          {"type":"single","answers":"A"}
          {"type":"multi","answers":["A","B","D"]}
          {"type":"fill"} / {"type":"sub"} / {"type":"unknown"}
        返回 (题型, 选项字母列表, answers 原始字符串)；解析失败返回 (None, [], "")。
        客观题（single/multi）解析不出字母视为失败，交给上层重试。
        """
        if not text:
            return None, [], ""
        s = text.strip()
        # 容错：剥掉被提示词禁止但仍可能出现的代码块围栏
        s = re.sub(r"^```(?:json)?", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"```$", "", s).strip()
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            return None, [], ""
        try:
            data = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return None, [], ""
        if not isinstance(data, dict):
            return None, [], ""
        qtype = str(data.get("type", "")).strip().lower()
        if qtype not in ("single", "multi", "fill", "sub", "unknown"):
            return None, [], ""
        if qtype == "unknown":
            return "unknown", [], ""
        raw = data.get("answers", "")
        letters: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                letters.extend(Bot._parse_options(str(item)))
        elif isinstance(raw, str):
            letters = Bot._parse_options(raw)
        if (
            qtype in ("single", "multi")
            and not letters
            and not (isinstance(raw, str) and Bot._parse_judgment(raw) is not None)
        ):
            # 客观题无字母且 answers 也不是判断题文案（对/错等）→ 解析失败重试
            return None, [], ""
        return qtype, letters, raw if isinstance(raw, str) else ""

    @staticmethod
    def _parse_options(text: str) -> list[str]:
        """严格解析 A-F，拒绝解释文本中的英文单词和错误码。"""
        if not text:
            return []
        match = re.fullmatch(
            r"\s*(?:(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*)?"
            r"([A-F](?:\s*[,，、/\s]\s*[A-F])*)\s*[。.]?\s*",
            text.upper(),
        )
        if not match:
            return []
        letters = re.findall(r"[A-F]", match.group(1))
        seen: set[str] = set()
        result: list[str] = []
        for letter in letters:
            if letter not in seen:
                seen.add(letter)
                result.append(letter)
        return result

    def _question_id(self, page: Page, image_url: Optional[str]) -> str:
        """优先使用稳定题目 ID，否则退化到题干、选项和图片摘要。"""
        try:
            data = page.evaluate(
                """() => {
                    const visible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            style.opacity !== '0' && rect.width > 0 && rect.height > 0 &&
                            rect.bottom > 0 && rect.right > 0 &&
                            rect.top < window.innerHeight && rect.left < window.innerWidth;
                    };
                    const roots = [...document.querySelectorAll(
                        '[data-question-id], [data-problem-id], [data-slide-id], section[class*="slide__cmp"], [class*="question"]'
                    )];
                    const visibleRoots = roots.filter(visible);
                    const idNames = ['data-question-id', 'data-problem-id', 'data-slide-id', 'data-id', 'id'];
                    const root = [...visibleRoots].reverse().find(el =>
                        idNames.some(name => el.getAttribute(name))
                    ) || visibleRoots[visibleRoots.length - 1];
                    if (!root) return null;
                    const id = idNames
                        .map(name => root.getAttribute(name)).find(Boolean) || '';
                    const text = (root.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 4000);
                    const options = [...root.querySelectorAll('[data-option]')]
                        .filter(visible)
                        .map(el => `${el.getAttribute('data-option') || ''}:${(el.innerText || '').replace(/\\s+/g, ' ').trim()}`);
                    const images = [...root.querySelectorAll('img')]
                        .filter(visible).map(img => img.currentSrc || img.src || '');
                    return {id, text, options, images};
                }"""
            )
        except Exception:
            data = None

        identity: list[str] = []
        if isinstance(data, dict):
            stable_id = str(data.get("id", "")).strip()
            if stable_id:
                identity = ["id", stable_id]
            else:
                options = "|".join(map(str, data.get("options", []) or []))
                images = "|".join(
                    self._stable_image_url(str(url))
                    for url in data.get("images", []) or []
                )
                text = str(data.get("text", "")).strip()
                identity = ["content", text, options, images]
        if not any(identity) and image_url:
            identity = ["image", self._stable_image_url(image_url)]
        if not any(identity):
            return ""

        try:
            page_url = page.url
        except Exception:
            page_url = ""
        scope = self._classroom_scope(page_url)
        digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()
        return f"{scope}:{digest}"

    @staticmethod
    def _stable_image_url(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path

    @staticmethod
    def _classroom_scope(url: str) -> str:
        parsed = urlsplit(url)
        scope = f"{parsed.netloc}{parsed.path}".rstrip("/")
        stable_tokens = ("class", "lesson", "course", "session", "room", "live")
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if any(token in key.lower() for token in stable_tokens)
        ]
        return f"{scope}?{urlencode(sorted(query))}" if query else scope

    @staticmethod
    def _is_failed_answer(answer: str) -> bool:
        if not answer or not answer.strip():
            return True
        lowered = answer.lower()
        return any(marker in lowered for marker in (
            "调用失败", "答题失败", "下载失败", "无法获取", "未设置", "error"
        ))

    # ------- 子方法 -------

    def _has_submit_button(self, page: Page) -> bool:
        """检查 exercise 页面中是否仍存在可见的提交答案按钮。"""
        return self._find_submit_button(page) is not None

    def _has_answerable_options(self, page: Page) -> bool:
        """当前题目至少有一个本程序能够点击的可见客观题选项。"""
        try:
            options = self._question_scope(page).locator("p[data-option]")
            return self._first_visible(options) is not None
        except Exception:
            return False

    def _find_submit_button(self, page: Page):
        """返回可见提交按钮；按钮可能位于题目卡片外的页面操作栏。

        真实 DOM（2026-09-04 快照验证）：
        <div class="slide__shape submit-btn [can]">提交答案 <div class="tips">…</div></div>
        是 div 而非 button，故不能用 button 标签选择器作主匹配。
        """
        selectors = [
            'div.slide__shape.submit-btn',
            '[class*="submit-btn"]:has-text("提交答案")',
            'button:has-text("提交答案")',
            '[class*="submit-btn"]',
        ]
        for sel in selectors:
            try:
                button = self._first_visible(page.locator(sel))
                if button is not None:
                    return button
            except Exception:
                continue
        return None

    def _submit_ready(self, page: Page) -> bool:
        """提交按钮是否处于可提交态（class 含 can，即已选中至少一个选项）。

        真实 DOM 中按钮初始为灰色（无 can），选中选项后追加 can 类（蓝色）；
        点击灰色按钮不会生效，因此提交前须确认该状态。
        class 不可读或不存在时视为未知 DOM 变体，按旧行为放行，不阻塞提交。
        """
        button = self._find_submit_button(page)
        if button is None:
            return False
        try:
            class_attr = button.get_attribute("class")
        except Exception:
            return True
        if not class_attr:
            return True
        return "can" in class_attr.split()

    def _wait_submit_ready(self, page: Page, timeout: float = 2.5) -> bool:
        """轮询等待提交按钮进入可提交态（点击选项后 class 渲染可能略有延迟）。"""
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if self._submit_ready(page):
                return True
            try:
                page.wait_for_timeout(100)
            except Exception:
                if self.stop_event.wait(0.1):
                    break
        return False

    def _capture_question_image(self, page: Page) -> Optional[str]:
        """获取题目图片的 URL。即时检查可见性和 src，避免无图时固定等待。"""
        try:
            scope = self._question_scope(page)
            image = self._first_visible(scope.locator('img[class*="cover"]'))
            if image is None:
                return None
            src = image.get_attribute("src")
            return src if src else None
        except Exception:
            return None

    def _click_options(self, page: Page, letters: list[str]) -> bool:
        """点击 AI 返回的选项字母列表（不区分单选/多选，逐一点击）。

        - 字母来自统一 JSON 提示词的 answers 字段（已由 _parse_ai_answer 解析）。
        - 多字母点击中途失败时回滚已点击项，避免残留半选状态。
        - 全部失败时改微信通知报警，不默认选 A（避免误作答）。
        """
        scope = self._question_scope(page)
        if not letters:
            self.log("AI 未返回有效选项字母。")
            self._notify_answer_failure("")
            return False

        items = []
        for option in letters:
            item = self._first_visible(scope.locator(f'p[data-option="{option}"]'))
            if item is None:
                self.log(f"未找到可见选项 {option}。")
                self._notify_answer_failure(", ".join(letters))
                return False
            items.append((option, item))

        clicked = []
        for option, item in items:
            try:
                item.click(timeout=5_000)
                clicked.append(item)
            except Exception:
                self.log(f"未能点击选项 {option}。")
                if len(items) > 1:
                    for previous in reversed(clicked):
                        try:
                            previous.click(timeout=2_000)
                        except Exception:
                            pass
                self._notify_answer_failure(", ".join(letters))
                return False
        return True

    def _notify_answer_failure(self, predicted_str: str) -> None:
        try:
            self._notify(
                "自动答题失败",
                f"AI 返回：{predicted_str}，无法自动作答，请手动处理。",
            )
        except Exception as e:
            self.log(f"答题失败通知发送失败：{e}")

    def _click_judgment(self, page: Page, predicted_str: str) -> bool:
        """判断题作答：将 AI 返回的 对/错/正确/错误/T/F/是/否 映射到页面上的选项。

        雨课堂判断题通常是「单选题」形态，选项 data-option 为 A/B，文案为 对/错
        （或 正确/错误）。这里按页面实际文案匹配，而不是假设 A=对、B=错。
        仅当选项文案较短（<=4 字）时才视为判断题选项，避免误命中普通选择题长文本。
        """
        judgment = self._parse_judgment(predicted_str)
        if judgment is None:
            return False

        try:
            opts = self._question_scope(page).locator('p[data-option]')
            count = opts.count()
            if count == 0:
                return False
            for i in range(count):
                opt = opts.nth(i)
                try:
                    text = (opt.inner_text() or "").strip()
                except Exception:
                    continue
                if len(text) > 4:
                    continue  # 长文本不是判断题选项（如 "A. 关于对错的叙述"）
                option_value = self._parse_judgment(text)
                if option_value is judgment:
                    try:
                        opt.click(timeout=5_000)
                        self.log(f"判断题作答：{text}")
                        return True
                    except Exception:
                        self.log(f"未能点击选项 {text}。")
        except Exception as e:
            self.log(f"判断题解析失败：{e}")
        return False

    @staticmethod
    def _parse_judgment(text: str) -> Optional[bool]:
        normalized = re.sub(
            r"^(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*",
            "",
            text.strip().upper(),
        ).rstrip("。.")
        if normalized in ("对", "正确", "T", "TRUE", "是"):
            return True
        if normalized in ("错", "错误", "F", "FALSE", "否"):
            return False
        return None

    def _submit_answer(self, page: Page) -> bool:
        """提交答案。仅以提交按钮状态判断结果（准则：按钮存在 = 未作答）。

        True  = 已确认提交：点击后按钮消失，或已离开 exercise 页面。
        False = 仍未作答：未找到按钮、点击失败，或点击后按钮仍然存在。
        返回 False 的题在本轮运行中不会再次调用 AI。
        """
        try:
            submit_btn = self._find_submit_button(page)
            if submit_btn is None:
                self.log("未找到可见的提交答案按钮。")
                return False
            submit_btn.click(timeout=5_000)
        except Exception as e:
            self.log(f"无法提交答案：{e}")
            return False

        deadline = time.monotonic() + 10
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if not self._is_exercise_page(page) or not self._has_submit_button(page):
                self.log("已提交答案。")
                return True
            try:
                page.wait_for_timeout(100)
            except Exception:
                if self.stop_event.wait(0.1):
                    break

        self.log("已点击提交，但提交按钮仍存在，视为未作答，本题不再自动重试。")
        return False

    # ==================== Cookie 过期提醒 ====================

    def _check_cookie_warning(self) -> None:
        """检查 cookie 是否即将过期并发送微信提醒。"""
        last_update = self.config.get("last_cookie_update_time", "")
        if not last_update or not self.browser.has_session:
            return

        try:
            last_dt = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
            days_passed = (datetime.now() - last_dt).days
            remaining = COOKIE_VALID_DAYS - days_passed

            now = datetime.now()
            last_warn_date = self.config.get("last_cookie_warn_date", "")

            # 有效期不足 3 天且今天尚未提醒过即提醒（不再依赖恰好 8 点整）
            if (
                remaining <= 3
                and now.strftime("%Y-%m-%d") != last_warn_date
            ):
                self.log(f"Cookies 有效期不足 3 天，正在发送微信提醒。")
                try:
                    self._notify(
                        "长江雨课堂助手：Cookies 即将过期",
                        f"您的登录 Cookies 还剩约 {remaining:.1f} 天过期。请尽快更新 Cookies。",
                    )
                except Exception as notify_err:
                    self.log(f"Cookie 过期通知发送失败：{notify_err}")
                self.config.set("last_cookie_warn_date", now.strftime("%Y-%m-%d"))
                self.config.persist()
        except Exception as e:
            self.log(f"Cookie 过期检查异常：{e}")

    # ==================== 调试模式 ====================

    def _debug_dump(self, page: Page, label: str) -> None:
        """调试模式：保存页面 HTML + 截图到 debug/。"""
        if not self.config.get("debug_mode", False):
            return
        os.makedirs("debug", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        try:
            path = f"debug/{ts}_{label}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(page.content())
            self.log(f"[DEBUG] HTML: {path}")
        except Exception as e:
            self.log(f"[DEBUG] HTML 失败: {e}")
        try:
            path = f"debug/{ts}_{label}.png"
            page.screenshot(path=path, full_page=False)
            self.log(f"[DEBUG] 截图: {path}")
        except Exception as e:
            self.log(f"[DEBUG] 截图失败: {e}")

    def _debug_save_tabs(self) -> None:
        """调试模式：保存所有标签页 URL 到文件。"""
        if not self.config.get("debug_mode", False):
            return
        os.makedirs("debug", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = f"debug/{ts}_tabs.txt"
        try:
            pages = self.browser.pages
            lines = [f"共 {len(pages)} 个标签页：\n"]
            for i, p in enumerate(pages):
                try:
                    lines.append(f"[{i}] closed={p.is_closed()} {p.url}\n")
                except Exception:
                    lines.append(f"[{i}] 无法读取\n")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            self.log(f"[DEBUG] 标签页: {path}")
        except Exception as e:
            self.log(f"[DEBUG] 标签页失败: {e}")
