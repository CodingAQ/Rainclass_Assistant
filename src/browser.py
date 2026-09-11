"""浏览器管理模块 - 基于 Playwright 封装。

替代原来的 Selenium/ChromeDriver 方案。Playwright 自带 Chromium，
首次运行 `playwright install chromium` 后即可使用，无需系统安装 Chrome 或匹配驱动。
"""

import logging
import os
import subprocess
import sys
import time
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright

logger = logging.getLogger(__name__)

# 默认会话状态文件（存储 cookies + localStorage）
DEFAULT_STATE_FILE = "browser_state.json"

# 雨课堂各服务器主页。域名以官方帮助页为准（荷塘雨课堂 = pro.yuketang.cn）：
# https://www.yuketang.cn/help?detail=508
YUKETANG_SERVERS: dict[str, str] = {
    "雨课堂": "https://www.yuketang.cn",
    "荷塘雨课堂": "https://pro.yuketang.cn",
    "长江雨课堂": "https://changjiang.yuketang.cn",
    "黄河雨课堂": "https://huanghe.yuketang.cn",
}
DEFAULT_SERVER = "长江雨课堂"

# 旧常量：默认服务器域名（实际导航地址由 BrowserManager.base_url 决定）
YUKETANG_URL = YUKETANG_SERVERS[DEFAULT_SERVER]


# 模块级缓存：Playwright 浏览器安装检测只做一次，避免每次 start() 都 launch 验证
_browsers_checked = False


def _ensure_playwright_browsers() -> None:
    """确保 Playwright 浏览器已安装（仅首次调用时检测）。"""
    global _browsers_checked
    if _browsers_checked:
        return

    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch()
            browser.close()
        finally:
            pw.stop()
    except Exception:
        logger.info("Playwright 浏览器未安装，正在自动安装...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"无法安装 Playwright 浏览器：{result.stderr}\n"
                "请手动执行：playwright install chromium"
            )
        logger.info("Playwright 浏览器安装完成。")
    _browsers_checked = True


class BrowserManager:
    """Playwright 浏览器生命周期管理。

    使用方式：
        bm = BrowserManager(headless=False)
        bm.start()
        page = bm.page
        # ... 操作 page ...
        bm.stop()
    """

    def __init__(
        self,
        headless: bool = False,
        state_file: str = DEFAULT_STATE_FILE,
        debug_port: Optional[int] = None,
        base_url: Optional[str] = None,
    ):
        self._headless = headless
        self._state_file = state_file
        self._debug_port = self._resolve_debug_port(debug_port)
        # 导航主页地址：按所配雨课堂服务器决定（见 YUKETANG_SERVERS）
        self.base_url = (base_url or YUKETANG_URL).rstrip("/")
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    @staticmethod
    def _resolve_debug_port(debug_port: Optional[int]) -> Optional[int]:
        """解析仅供本机调试使用的 CDP 端口。"""
        value = debug_port
        if value is None:
            raw = os.environ.get("RAINCLASS_DEBUG_PORT", "").strip()
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError("RAINCLASS_DEBUG_PORT 必须是有效端口号。") from exc
        if not 1024 <= value <= 65535:
            raise ValueError("调试端口必须在 1024 到 65535 之间。")
        return value

    def _launch_args(self) -> list[str]:
        args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if self._debug_port is not None:
            args.extend([
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self._debug_port}",
            ])
        return args

    # ---- 属性 ----

    @property
    def page(self) -> Page:
        """获取当前页面。需先调用 start()。"""
        if not self._page:
            raise RuntimeError("浏览器未启动，请先调用 start()。")
        return self._page

    @property
    def is_running(self) -> bool:
        """浏览器是否正在运行。"""
        return self._browser is not None and self._browser.is_connected()

    @property
    def has_session(self) -> bool:
        """是否存在已保存的会话状态。"""
        return os.path.exists(self._state_file)

    # ---- 启动 / 停止 ----

    def start(self) -> bool:
        """启动浏览器。返回 True 表示成功。"""
        if self.is_running:
            logger.info("浏览器已在运行中，跳过启动。")
            return True

        try:
            _ensure_playwright_browsers()
            self._playwright = sync_playwright().start()

            # 如果有保存的会话状态，则恢复
            storage_state = self._state_file if self.has_session else None

            self._browser = self._playwright.chromium.launch(
                headless=self._headless,
                args=self._launch_args(),
            )
            try:
                self._context = self._browser.new_context(
                    storage_state=storage_state,
                    viewport={"width": 1280, "height": 720},
                )
            except Exception:
                if not storage_state:
                    raise
                logger.warning("登录会话文件已损坏，将以未登录状态启动，请重新获取 Cookies。")
                self._context = self._browser.new_context(
                    viewport={"width": 1280, "height": 720},
                )
            self._page = self._context.new_page()
            logger.info("Playwright 浏览器启动成功。")
            if self._debug_port is not None:
                logger.info(
                    "浏览器调试端口已开启：http://127.0.0.1:%s",
                    self._debug_port,
                )
            return True
        except Exception as e:
            logger.error(f"启动浏览器失败：{e}")
            self.stop()
            return False

    def stop(self) -> None:
        """关闭浏览器。"""
        try:
            if self._context:
                self._context.close()
                self._context = None
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
                self._browser = None
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
        except Exception:
            pass
        self._page = None
        logger.info("浏览器已关闭。")

    def ensure_running(self) -> bool:
        """检查浏览器是否存活，必要时重启。"""
        if not self.is_running:
            logger.info("浏览器已断开，正在重新启动...")
            return self.start()
        try:
            if self._page is None or self._page.is_closed():
                if self._context is None:
                    return False
                # 当前课堂页被关闭后，优先复用仍存在的首页或其他标签页，
                # 避免额外新建 about:blank 并产生重复首页标签。
                for candidate in reversed(self._context.pages):
                    try:
                        if candidate.is_closed():
                            continue
                        self.use_page(candidate)
                        break
                    except Exception:
                        continue
                else:
                    self._page = self._context.new_page()
        except Exception:
            return False
        return True

    # ---- 会话 / Cookie ----

    def get_cookies(self, timeout_ms: int = 120_000, stop_event=None) -> bool:
        """
        打开浏览器让用户手动登录雨课堂，登录成功后保存会话状态。

        此方法会临时以非无头模式打开浏览器（覆盖全局 headless 设置）。
        """
        logger.info("正在打开浏览器以获取登录 Cookies...")

        # 以可见模式临时启动
        try:
            _ensure_playwright_browsers()
            pw = sync_playwright().start()
        except Exception as e:
            logger.error(f"启动 Playwright 失败：{e}")
            return False

        browser = None
        context = None
        try:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()
            page.goto(self.base_url)

            # 轮询登录标记，使应用关闭时可以取消等待。
            logger.info("等待用户完成登录（最长 120 秒）...")
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    logger.info("获取 Cookies 已取消。")
                    return False
                if page.locator("#tab-student").count() > 0:
                    break
                page.wait_for_timeout(250)
            else:
                logger.error("等待登录超时。")
                return False

            # 保存会话状态
            context.storage_state(path=self._state_file)
            logger.info(f"会话状态已保存到 {self._state_file}。")

            return True
        except Exception as e:
            logger.error(f"获取 Cookies 失败：{e}")
            return False
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                pw.stop()
            except Exception:
                pass

    def save_session(self) -> None:
        """保存当前会话状态到文件。"""
        if self._context and self.is_running:
            self._context.storage_state(path=self._state_file)
            logger.info("会话状态已保存。")

    # ---- 导航 ----

    def navigate_to_class(self) -> bool:
        """导航到雨课堂。storage_state 在 start() 时已加载 cookie，无需刷新。
        返回 True 表示成功进入。
        """
        if not self.ensure_running():
            return False
        if self._context is None:
            return False

        try:
            if self._page is None or self._page.is_closed():
                self._page = self._context.new_page()
        except Exception:
            self._page = self._context.new_page()

        try:
            self.page.goto(self.base_url, timeout=30_000)
            self.page.wait_for_selector("body", timeout=20_000)
            logger.info("已成功导航到雨课堂。")
            return True
        except Exception as e:
            logger.error(f"导航到雨课堂失败：{e}")
            return False

    def refresh(self, page: Optional[Page] = None) -> bool:
        """刷新指定标签页；未指定时刷新当前由管理器聚焦的标签页。"""
        if not self.ensure_running():
            return False
        try:
            target = page if page is not None else self.page
            if target.is_closed():
                raise RuntimeError("无法刷新已关闭的标签页。")
            target.reload(timeout=30_000)
            logger.info("标签页已刷新：%s", target.url[:120])
            return True
        except Exception as e:
            logger.error("刷新标签页失败：%s", e)
            return False

    def is_logged_in(self) -> bool:
        """快速检查当前页面是否已登录（tab-student 元素存在）。"""
        try:
            self.page.wait_for_selector("#tab-student", timeout=3_000)
            return True
        except Exception:
            return False

    def check_connectivity(self) -> bool:
        """快速检查网络能否连通雨课堂。"""
        try:
            import requests
            requests.get(self.base_url, timeout=5)
            return True
        except Exception:
            return False

    def switch_to_latest_page(self) -> Page:
        """切换到最新打开的标签页并返回。"""
        if self._context and len(self._context.pages) > 1:
            self.use_page(self._context.pages[-1])
            logger.info(f"已切换到新标签页 (共 {len(self._context.pages)} 个标签页)。")
        return self.page

    def use_page(self, page: Page) -> Page:
        """把已验证的标签页设为后续浏览器操作的当前页。"""
        if page.is_closed():
            raise RuntimeError("无法切换到已关闭的标签页。")
        self._page = page
        page.bring_to_front()
        return page

    @property
    def pages(self) -> list:
        """返回所有打开的标签页列表。"""
        if self._context:
            return self._context.pages
        return []

    def get_cookies_dict(self) -> dict:
        """返回当前上下文的 cookies（name->value），用于带鉴权下载题目图片。"""
        if self._context and self.is_running:
            try:
                return {
                    c.get("name", ""): c.get("value", "")
                    for c in self._context.cookies()
                    if c.get("name")
                }
            except Exception:
                return {}
        return {}
