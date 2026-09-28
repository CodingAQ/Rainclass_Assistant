"""课程检测与进入课堂子系统。

以 Mixin 形式承载「发现/进入/退出课堂」相关逻辑，由 src.bot.Bot 继承使用。
Mixin 依赖宿主（Bot）在运行期提供的属性与方法：

  config / browser / stop_event / log / _debug_dump / _debug_save_tabs /
  _run_classroom_loop / _abandon_pending_answer / _answer_future /
  _ended_lesson_ids / _waiting_for_class_logged / _home_refreshed_at /
  _last_tab_limit_warn

这些成员的初始化与实现都留在 Bot 中，Mixin 只负责课程发现与进出课堂的流程。
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from src.browser import DEFAULT_SERVER, YUKETANG_SERVERS

logger = logging.getLogger(__name__)

# 重试间隔（秒）
RETRY_DELAY = 10

# 点击课程后，课堂页可能需要等待后端创建并完成首屏渲染。
# 60 秒：等待期间不产生新调用；超时后关闭本轮新开的标签页。
CLASSROOM_OPEN_TIMEOUT = 60

# 首页 DOM 刷新间隔（秒）
HOME_REFRESH_INTERVAL = 600

# 课程标签页数上限
MAX_ENTRY_TABS = 5

# 标签页超限警告的节流间隔（秒）。
TAB_LIMIT_WARN_INTERVAL = 300

# 雨课堂下课后会把该消息永久保留在课堂时间线中。
CLASS_ENDED_SELECTOR = '//div[@title="下课啦！" and contains(@class, "timeline__msg")]'


class ClassroomEntryMixin:
    """课程检测、进入课堂与下课收敛（由 Bot 继承）。"""

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
                self._home_refreshed_at = time.monotonic()
            elif time.monotonic() - self._home_refreshed_at >= HOME_REFRESH_INTERVAL:
                # 定时刷新首页：已渲染的 DOM 不会随下课自动更新（见常量注释）。
                # 刷新失败不阻塞本轮，下一轮会重试。
                if self.browser.refresh(self.browser.page):
                    self._home_refreshed_at = time.monotonic()

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

                # 标签页保险丝：点击课程条会真实打开新标签页，数量异常时
                # 先停止进入并告警，防止泄漏进一步扩大。
                if len(self.browser.pages) >= MAX_ENTRY_TABS:
                    self._warn_tab_limit()
                    return

                pages_before_click = self._snapshot_pages()
                if not self._click_active_class(page):
                    # click() 半途抛异常但页面可能已被打开，同样要做 diff 清理。
                    self._close_pages_opened_after(pages_before_click)
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

                # 首页不是课堂；关闭本轮新开的标签页后交给下一轮重新发现，
                # 不能在首页死循环，更不能让点击产生的标签页累积。
                self._close_pages_opened_after(pages_before_click)
                self._log_waiting_for_class_once()
                return

            except PlaywrightTimeout:
                self._log_waiting_for_class_once()
                return
            except Exception as e:
                self.log(f"课程检测发生错误：{e}，{RETRY_DELAY} 秒后重试...")
                self.stop_event.wait(RETRY_DELAY)
                continue

    def _server_host(self) -> str:
        """所配雨课堂服务器的主机名（小写），用于首页/课堂判定。"""
        name = self.config.get("yuketang_server", DEFAULT_SERVER)
        return urlsplit(
            YUKETANG_SERVERS.get(name, YUKETANG_SERVERS[DEFAULT_SERVER])
        ).netloc.lower()

    def _is_home_page(self, page: Page) -> bool:
        """是否处于所配服务器的首页（根路径或 v2/web 应用入口）。"""
        try:
            parts = urlsplit(page.url.strip())
            host = parts.netloc.lower()
            path = parts.path.lower().rstrip("/")
        except Exception:
            return False
        if host != self._server_host():
            return False
        return path in ("", "/v2/web", "/v2/web/index")

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
        """点击状态徽标为「听」的在课条目；横幅折叠时先点摘要条展开。

        2026-09 实测（changjiang）：横幅展开面板条目为 `.onlesson .lessonlist`，
        在课条目的 `.status` 徽标文案为「听」，考试条目为「考试」（绝不能点，
        否则会跳去考试页）；旧版前端变体的 `.jump_lesson__bar` 保留为兜底。
        """
        if self._click_live_lesson(page):
            return True

        # 展开面板（.onlessonlist）未渲染说明横幅处于折叠态：点摘要条展开后再找。
        # 面板已展开却找不到「听」条目 = 没有在课课程，直接返回，避免再把面板收起。
        try:
            if page.locator(".onlesson .onlessonlist").count() > 0:
                return False
        except Exception:
            return False

        summary = page.locator(".onlesson > .tipbar")
        if not self._click_first_visible(summary):
            return False
        self.stop_event.wait(0.5)
        return self._click_live_lesson(page)

    def _click_live_lesson(self, page: Page) -> bool:
        """在横幅面板中点击「听」状态条目；找不到时退回旧前端课程条。"""
        items = page.locator(".onlesson .lessonlist")
        try:
            count = items.count()
        except Exception:
            count = 0
        for index in range(count):
            item = items.nth(index)
            if not self._is_live_lesson_item(item):
                continue
            try:
                if not item.is_visible():
                    continue
                item.click(timeout=5_000)
                return True
            except Exception:
                continue
        # 旧版前端兜底：条目类名不同（.jump_lesson__bar，2026-09 之前的变体）。
        return self._click_first_visible(page.locator(".onlesson .jump_lesson__bar"))

    @staticmethod
    def _is_live_lesson_item(item) -> bool:
        """条目是否为在课课程：状态徽标「听」，或旧样式的音频播放图标。"""
        try:
            status = item.locator(".status")
            if status.count() > 0 and (status.first.inner_text() or "").strip() == "听":
                return True
        except Exception:
            pass
        try:
            if item.locator("i[class*='icon--yinpinbofang']").count() > 0:
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _click_first_visible(locator) -> bool:
        """点击 locator 中第一个可见元素。"""
        item = ClassroomEntryMixin._first_visible(locator)
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

    def _is_classroom_page(self, page: Page) -> bool:
        """保守识别实时课堂，明确排除首页和普通课程页。"""
        try:
            if page.is_closed():
                return False
            url = page.url.lower()
        except Exception:
            return False

        # /v2/web/* 是 Web 应用页面（首页/课程页/考试页等），永远不是实时课堂；
        # 所配服务器的根路径同理
        try:
            parts = urlsplit(url)
        except Exception:
            parts = None
        if parts is not None and parts.path.startswith("/v2/web"):
            return False
        if (
            parts is not None
            and parts.netloc.lower() == self._server_host()
            and parts.path in ("", "/")
        ):
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
                if ClassroomEntryMixin._first_visible(page.locator(selector)) is not None:
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
        # 根本手段：立即刷新主页，让课程条马上反映"课已结束"的服务器真相，
        # 否则上课期间渲染的首页 DOM 会残留课程条，主循环会反复点击它。
        self._refresh_home_after_class()
        return True

    def _snapshot_pages(self) -> list:
        """点击前快照现有标签页，用于之后识别本轮新开的页面。"""
        try:
            return list(self.browser.pages)
        except Exception:
            return []

    def _close_pages_opened_after(self, before: list) -> None:
        """关闭快照之后新打开且未被识别为课堂的标签页（防泄漏）。"""
        try:
            current = list(self.browser.pages)
        except Exception:
            return
        before_ids = {id(page) for page in before}
        opened = [page for page in current if id(page) not in before_ids]
        closed = 0
        for candidate in opened:
            try:
                if candidate.is_closed():
                    continue
                candidate.close()
                closed += 1
            except Exception as exc:
                logger.warning("关闭本轮新开标签页失败：%s", exc)
        if closed:
            self.log(f"未识别出课堂，已关闭本轮新打开的 {closed} 个标签页。")

    def _warn_tab_limit(self) -> None:
        """标签页达到上限时的节流警告；只警告不自动关闭。"""
        now = time.monotonic()
        if now - self._last_tab_limit_warn < TAB_LIMIT_WARN_INTERVAL:
            return
        self._last_tab_limit_warn = now
        self.log(
            f"标签页数量已达 {len(self.browser.pages)} 个"
            f"（上限 {MAX_ENTRY_TABS}），跳过进入课程以防泄漏，请检查浏览器。"
        )

    def _refresh_home_after_class(self) -> None:
        """下课关闭课堂标签页后立即刷新主页。

        没有存活的首页标签页时跳过：下一轮检测会新建页面并导航，
        天然是新鲜 DOM。
        """
        home_page = None
        try:
            for candidate in reversed(self.browser.pages):
                if not candidate.is_closed() and self._is_home_page(candidate):
                    home_page = candidate
                    break
        except Exception:
            home_page = None
        if home_page is None:
            return
        try:
            self.browser.use_page(home_page)
        except Exception:
            pass
        if self.browser.refresh(home_page):
            self._home_refreshed_at = time.monotonic()
