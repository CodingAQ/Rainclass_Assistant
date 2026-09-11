import logging
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from tkinter import messagebox

import customtkinter as ctk

from src.ai import AIService
from src.bot import Bot, COOKIE_VALID_DAYS
from src.browser import DEFAULT_SERVER, YUKETANG_SERVERS, BrowserManager
from src.config import Config
from src.instance_lock import InstanceLock
from src.notification import NotificationService

# ==================== 全局配置 ====================

APP_TITLE = "雨课堂自动助手"
APP_WIDTH = 900
APP_HEIGHT = 700
APP_MIN_W = 600
APP_MIN_H = 500

# 日志格式化器
LOG_FORMAT = logging.Formatter(
    "%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)

# 队列
_log_queue: queue.Queue = queue.Queue()
_gui_log_queue: queue.Queue = queue.Queue()
_log_listener: QueueListener | None = None


class _GuiLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _gui_log_queue.put(self.format(record))


class _MaxLogLengthFilter(logging.Filter):
    """日志长度闸门：超长记录截断后再落盘/上屏。

    模型报错、SDK 调试日志可能携带整页 HTML 或 base64 数据（曾出现
    单条 315KB 的日志），会在 GUI 控制窗格刷屏并撑爆日志文件。
    正常业务日志远低于该上限，不受影响。
    """

    def __init__(self, limit: int = 1000) -> None:
        super().__init__()
        self.limit = limit

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if len(message) > self.limit:
            record.msg = (
                f"{message[:self.limit]}...(日志过长已截断，原始 {len(message)} 字符)"
            )
            record.args = None
        return True


def _setup_logging() -> None:
    global _log_listener
    if _log_listener is not None:
        _stop_logging()
    root_logger = logging.getLogger()
    # 通过环境变量 RAINCLASS_DEBUG=1 开启 DEBUG 级别日志
    root_logger.setLevel(logging.DEBUG if os.environ.get("RAINCLASS_DEBUG") else logging.INFO)
    root_logger.handlers.clear()

    # SDK 的 DEBUG 日志会包含完整请求体（包括截图 base64 和临时图片 URL）。
    # 应用调试模式只保留自身日志，避免日志膨胀和敏感数据泄露。
    for noisy_logger in ("openai", "httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    qh = QueueHandler(_log_queue)
    root_logger.addHandler(qh)

    length_gate = _MaxLogLengthFilter()
    fh = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(LOG_FORMAT)
    fh.addFilter(length_gate)

    gh = _GuiLogHandler()
    gh.setFormatter(LOG_FORMAT)
    gh.addFilter(_MaxLogLengthFilter(500))

    _log_listener = QueueListener(_log_queue, fh, gh)
    _log_listener.start()


def _stop_logging() -> None:
    global _log_listener
    if _log_listener is not None:
        listener = _log_listener
        listener.stop()
        for handler in listener.handlers:
            handler.close()
        _log_listener = None


# ==================== 主应用 ====================


class App(ctk.CTk):
    """CustomTkinter 主窗口。"""

    def __init__(self) -> None:
        super().__init__()

        # ---- 窗口 ----
        self.title(APP_TITLE)
        self.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        self.minsize(APP_MIN_W, APP_MIN_H)

        # 默认暗色主题
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        # ---- 核心模块 ----
        self.config = Config()
        self.browser: BrowserManager | None = None
        self.notification = NotificationService(self.config.get("xxtui_api_key", ""))

        # ---- 状态 ----
        self.is_running = False
        self.stop_event: threading.Event | None = None
        self._bot_thread: threading.Thread | None = None
        self._stopping = False
        self._closing = False
        self._destroying = False
        self._cookie_thread: threading.Thread | None = None
        self._cookie_stop_event = threading.Event()
        self._save_job: str | None = None  # 实时保存的防抖 after 任务 id
        self._last_save_error_log = 0.0  # 保存失败日志的节流
        self._active_server = self.config.get("yuketang_server", DEFAULT_SERVER)

        # ---- 构建 UI ----
        self._setup_ui()
        self._start_log_pump()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log("应用已启动。")
        self._log("请先在主页右上角获取登录 Cookies。")
        self._update_cookie_info()

    # ==================== 日志 ====================

    def _log(self, message: str) -> None:
        logging.getLogger("app").info(message)

    def _start_log_pump(self) -> None:
        def pump() -> None:
            try:
                while True:
                    msg = _gui_log_queue.get_nowait()
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
            except queue.Empty:
                pass
            self.after(100, pump)

        self.after(100, pump)

    # ==================== UI 构建 ====================

    def _setup_ui(self) -> None:
        # 顶部主题切换栏
        self._setup_topbar()

        # 标签页
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(expand=True, fill="both", padx=10, pady=(0, 10))
        self.tabview.add("主页")
        self.tabview.add("设置")

        self._setup_home_tab()
        self._setup_settings_tab()

    def _setup_topbar(self) -> None:
        bar = ctk.CTkFrame(self, height=36, corner_radius=0)
        bar.pack(fill="x", padx=0, pady=0)

        ctk.CTkLabel(bar, text=APP_TITLE, font=ctk.CTkFont(size=13, weight="bold")).pack(
            side="left", padx=12, pady=4
        )

        self.theme_btn = ctk.CTkButton(
            bar,
            text="☀",
            width=32,
            height=28,
            corner_radius=6,
            fg_color="transparent",
            hover_color=("#E0E0E0", "#3A3A3A"),
            command=self._toggle_theme,
        )
        self.theme_btn.pack(side="right", padx=10, pady=4)

    def _toggle_theme(self) -> None:
        current = ctk.get_appearance_mode()
        new = "Light" if current == "Dark" else "Dark"
        ctk.set_appearance_mode(new)
        self.theme_btn.configure(text="☀" if new == "Dark" else "🌙")
        # tk.Canvas 不受 CustomTkinter 主题管理，切换后需手动刷新背景色
        try:
            self.status_canvas.configure(bg=self._canvas_bg())
        except (AttributeError, tk.TclError):
            pass

    # ==================== 主页 ====================

    def _setup_home_tab(self) -> None:
        parent = self.tabview.tab("主页")

        # 状态栏
        status_bar = ctk.CTkFrame(parent, corner_radius=8)
        status_bar.pack(fill="x", padx=10, pady=(10, 6))

        # 状态指示灯
        self.status_canvas = tk.Canvas(
            status_bar, width=18, height=18, bg=self._canvas_bg(), highlightthickness=0
        )
        self.status_canvas.pack(side="left", padx=(12, 6), pady=10)
        self.status_dot = self.status_canvas.create_oval(
            2, 2, 16, 16, fill="#EF4444", outline=""
        )

        self.status_label = ctk.CTkLabel(
            status_bar, text="已停止", font=ctk.CTkFont(size=16, weight="bold")
        )
        self.status_label.pack(side="left", padx=(0, 20), pady=10)

        self.toggle_btn = ctk.CTkButton(
            status_bar,
            text="▶  启动自动答题",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=36,
            corner_radius=8,
            fg_color="#43A047",
            hover_color="#388E3C",
            command=self._toggle_bot,
        )
        self.toggle_btn.pack(side="left", pady=8)

        self.cookies_btn = ctk.CTkButton(
            status_bar,
            text="获取登录Cookie",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=36,
            corner_radius=8,
            command=self._run_get_cookies,
        )
        self.cookies_btn.pack(side="right", padx=(0, 12), pady=8)

        self.cookie_status = ctk.CTkLabel(
            status_bar,
            text="Cookies: 未获取",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self.cookie_status.pack(side="right", padx=(0, 10), pady=10)

        # 日志区
        self.log_text = ctk.CTkTextbox(
            parent, wrap="word", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.log_text.configure(state="disabled")
        self.log_text.pack(expand=True, fill="both", padx=10, pady=(4, 10))

    def _canvas_bg(self) -> str:
        mode = ctk.get_appearance_mode()
        return "#212121" if mode == "Dark" else "#EBEBEB"

    # ==================== 设置页 ====================

    def _setup_settings_tab(self) -> None:
        parent = self.tabview.tab("设置")

        # 可滚动容器
        scroll = ctk.CTkScrollableFrame(parent, label_text="")
        scroll.pack(expand=True, fill="both", padx=10, pady=10)

        # ---- 雨课堂服务器 ----
        self._section_label(scroll, "🏫 雨课堂服务器")
        self.yuketang_server_var = self._combo_row(
            scroll,
            "服务器",
            list(YUKETANG_SERVERS),
            self.config.get("yuketang_server", DEFAULT_SERVER),
        )
        self.yuketang_server_var.trace_add("write", self._on_server_changed)

        # ---- 时间设置 ----
        self._section_label(scroll, "⏰ 时间设置")
        self.start_time_var, _ = self._entry_row(scroll, "每日开始时间", self.config.get("start_time", "07:00"))
        self.end_time_var, _ = self._entry_row(scroll, "每日结束时间", self.config.get("end_time", "22:00"))

        # ---- 浏览器 ----
        self._section_label(scroll, "🌐 浏览器设置")
        self.headless_var = self._checkbox_row(
            scroll, "静默运行（无界面）", self.config.get("headless_mode", False)
        )
        self.debug_var = self._checkbox_row(
            scroll, "调试模式（保存页面截图和 HTML）", self.config.get("debug_mode", False)
        )

        # ---- AI 模型 ----
        self._section_label(scroll, "🤖 AI 模型")

        # 模型选择 + 测试按钮 同行
        self._model_row = ctk.CTkFrame(scroll, fg_color="transparent")
        self._model_row.pack(fill="x", pady=3)
        ctk.CTkLabel(self._model_row, text="模型选择", width=130, anchor="w").pack(side="left", padx=(0, 8))
        self.ai_model_var = tk.StringVar(value=self.config.get("ai_model", "豆包AI"))
        model_combo = ctk.CTkOptionMenu(
            self._model_row, values=["豆包AI", "Gemini AI", "自定义", "多AI作答"],
            variable=self.ai_model_var,
        )
        model_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.test_ai_btn = ctk.CTkButton(
            self._model_row, text="🧪 测试", width=70, height=28, corner_radius=6,
            fg_color="#555555", hover_color="#444444",
            command=self._test_ai_model,
        )
        self.test_ai_btn.pack(side="left")

        # 豆包 / Gemini Key（非自定义时显示）
        self.doubao_key_var, self._doubao_row = self._entry_row(
            scroll, "豆包 API Key", self.config.get("doubao_api_key", ""), show="*"
        )
        self.gemini_key_var, self._gemini_row = self._entry_row(
            scroll, "Gemini API Key", self.config.get("gemini_api_key", ""), show="*"
        )

        # 自定义 Provider（仅选"自定义"时显示）
        self.custom_base_url_var, self._custom_url_row = self._entry_row(
            scroll, "自定义 Base URL", self.config.get("custom_ai_base_url", "")
        )
        self.custom_api_key_var, self._custom_key_row = self._entry_row(
            scroll, "自定义 API Key", self.config.get("custom_ai_api_key", ""), show="*"
        )
        self.custom_model_var, self._custom_model_row = self._entry_row(
            scroll, "自定义 Model ID", self.config.get("custom_ai_model", "")
        )
        self.multi_ai_path_var, self._multi_ai_path_row = self._entry_row(
            scroll,
            "多AI配置文件",
            self.config.get("multi_ai_config_path", "model_visible.ini"),
        )
        self.multi_ai_timeout_var, self._multi_ai_timeout_row = self._entry_row(
            scroll,
            "多AI最大等待（秒）",
            str(self.config.get("multi_ai_timeout", 20)),
        )

        self._on_model_change()
        self.ai_model_var.trace_add("write", lambda *_: self._on_model_change())

        # ---- 时间间隔 ----
        self._section_label(scroll, "⏱ 时间间隔")
        self.submit_delay_var, _ = self._entry_row(scroll, "提交前等待（秒）", str(self.config.get("submit_delay", 1)))
        self.check_interval_var, _ = self._entry_row(scroll, "课程检查间隔（秒）", str(self.config.get("check_interval", 60)))
        self.quiz_refresh_interval_var, _ = self._entry_row(
            scroll, "答题刷新间隔（秒）", str(self.config.get("quiz_refresh_interval", 1))
        )

        # ---- 通知 ----
        self._section_label(scroll, "🔔 微信通知")
        self.xxtui_key_var, _ = self._entry_row(scroll, "xxtui API Key", self.config.get("xxtui_api_key", ""), show="*")

        # ---- 实时保存 ----
        # 任一设置变化后自动写盘（防抖见 _on_setting_changed），
        # 不再需要「保存设置」按钮。
        for var in (
            self.yuketang_server_var,
            self.start_time_var,
            self.end_time_var,
            self.headless_var,
            self.debug_var,
            self.ai_model_var,
            self.doubao_key_var,
            self.gemini_key_var,
            self.custom_base_url_var,
            self.custom_api_key_var,
            self.custom_model_var,
            self.multi_ai_path_var,
            self.multi_ai_timeout_var,
            self.submit_delay_var,
            self.check_interval_var,
            self.quiz_refresh_interval_var,
            self.xxtui_key_var,
        ):
            var.trace_add("write", self._on_setting_changed)

    # ---- 设置行构建器 ----

    def _section_label(self, parent: ctk.CTkFrame, text: str) -> None:
        lbl = ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(size=13, weight="bold")
        )
        lbl.pack(anchor="w", pady=(14, 2))

    def _entry_row(self, parent: ctk.CTkFrame, label: str, default: str, show: str = "") -> tuple[tk.StringVar, ctk.CTkFrame]:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=130, anchor="w").pack(side="left", padx=(0, 8))
        var = tk.StringVar(value=default)
        entry = ctk.CTkEntry(row, textvariable=var)
        if show:
            entry.configure(show=show)
        entry.pack(side="left", fill="x", expand=True)
        return var, row

    def _checkbox_row(self, parent: ctk.CTkFrame, label: str, default: bool) -> tk.BooleanVar:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        var = tk.BooleanVar(value=default)
        cb = ctk.CTkCheckBox(row, text=label, variable=var)
        cb.pack(side="left", padx=(0, 8))
        return var

    def _combo_row(self, parent: ctk.CTkFrame, label: str, values: list[str], default: str) -> tk.StringVar:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=130, anchor="w").pack(side="left", padx=(0, 8))
        var = tk.StringVar(value=default)
        ctk.CTkOptionMenu(row, values=values, variable=var).pack(side="left", fill="x", expand=True)
        return var

    # ==================== 模型切换 ====================

    def _on_model_change(self) -> None:
        """根据选中的模型显示/隐藏对应的配置字段。"""
        model = self.ai_model_var.get()
        is_custom = model == "自定义"
        is_multi = model == "多AI作答"

        # 预设字段（豆包/Gemini key）——显示在模型行之后
        if is_custom or is_multi:
            self._doubao_row.pack_forget()
            self._gemini_row.pack_forget()
        else:
            self._doubao_row.pack(after=self._model_row, fill="x", pady=3)
            self._gemini_row.pack(after=self._doubao_row, fill="x", pady=3)

        # 自定义字段——显示在模型行之后
        if is_custom:
            self._custom_url_row.pack(after=self._model_row, fill="x", pady=3)
            self._custom_key_row.pack(after=self._custom_url_row, fill="x", pady=3)
            self._custom_model_row.pack(after=self._custom_key_row, fill="x", pady=3)
        else:
            self._custom_url_row.pack_forget()
            self._custom_key_row.pack_forget()
            self._custom_model_row.pack_forget()

        if is_multi:
            self._multi_ai_path_row.pack(after=self._model_row, fill="x", pady=3)
            self._multi_ai_timeout_row.pack(after=self._multi_ai_path_row, fill="x", pady=3)
        else:
            self._multi_ai_path_row.pack_forget()
            self._multi_ai_timeout_row.pack_forget()

    # ==================== AI 测试 ====================

    def _test_ai_model(self) -> None:
        """使用 test_pic.png 测试当前选中的 AI 模型视觉能力。"""
        self.test_ai_btn.configure(text="⏳", state="disabled")
        self._log("正在测试 AI 模型视觉能力...")

        # 构建临时 AI 服务（使用 UI 当前值）
        temp_config = Config()
        temp_config.update_from_dict(self._collect_settings())
        ai = AIService(temp_config)

        def _run_test() -> None:
            try:
                result = ai.test_vision()
            except Exception as e:
                result = f"测试异常：{e}"
            finally:
                ai.shutdown()
            try:
                self.after(0, lambda: self._show_test_result(result))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=_run_test, daemon=True).start()

    def _show_test_result(self, result: str) -> None:
        self.test_ai_btn.configure(text="🧪 测试", state="normal")
        self._log(f"AI 测试结果：{result}")

        dialog = ctk.CTkToplevel(self)
        dialog.title("AI 模型测试结果")
        dialog.geometry("420x200")
        dialog.transient(self)
        dialog.grab_set()

        model_name = self.ai_model_var.get()
        ctk.CTkLabel(
            dialog, text=f"模型：{model_name}", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(pady=(16, 4))

        result_box = ctk.CTkTextbox(dialog, wrap="word", height=80)
        result_box.insert("1.0", result if result else "（空响应）")
        result_box.configure(state="disabled")
        result_box.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        ctk.CTkButton(dialog, text="关闭", command=dialog.destroy, width=80).pack(pady=(0, 12))

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 420) // 2
        y = self.winfo_y() + (self.winfo_height() - 200) // 2
        dialog.geometry(f"+{x}+{y}")

    # ==================== Cookie 管理 ====================

    def _run_get_cookies(self) -> None:
        if self._cookie_thread and self._cookie_thread.is_alive():
            self._log("登录 Cookies 正在获取中，请完成当前登录窗口。")
            return
        self.cookies_btn.configure(state="disabled", text="⏳ 等待登录")
        self._cookie_stop_event.clear()

        def run() -> None:
            try:
                self._get_cookies()
            finally:
                try:
                    self.after(0, self._finish_cookie_task)
                except (RuntimeError, tk.TclError):
                    pass

        self._cookie_thread = threading.Thread(target=run, daemon=True)
        self._cookie_thread.start()

    def _finish_cookie_task(self) -> None:
        self._cookie_thread = None
        if not self._closing:
            self.cookies_btn.configure(state="normal", text="获取登录Cookie")
        else:
            self._wait_for_shutdown()

    def _get_cookies(self) -> None:
        self._log("正在打开浏览器以获取登录 Cookies...")
        server = self.config.get("yuketang_server", DEFAULT_SERVER)
        temp_bm = BrowserManager(
            headless=False,
            base_url=YUKETANG_SERVERS.get(server, YUKETANG_SERVERS[DEFAULT_SERVER]),
        )
        if temp_bm.get_cookies(
            timeout_ms=120_000, stop_event=self._cookie_stop_event
        ):
            self.config.set("last_cookie_update_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._log("Cookies 已成功保存。")
            # 仅在后台线程中更新配置字段并写盘；不读取任何 UI 变量，
            # 避免在非主线程触碰 tkinter 的 Tcl 解释器（C1）。
            if not self.config.persist():
                self._log("Cookies 已保存，但更新时间写入配置失败。")
            try:
                self.after(0, self._update_cookie_info)
            except (RuntimeError, tk.TclError):
                pass
        else:
            self._log("获取 Cookies 超时或失败，请重新尝试。")

    def _update_cookie_info(self) -> None:
        state_file = "browser_state.json"
        if not os.path.exists(state_file):
            self.cookie_status.configure(text="Cookies: 未获取", text_color="#E6A817")
            return

        last_update = self.config.get("last_cookie_update_time", "")
        if not last_update:
            self.cookie_status.configure(text="Cookies: 未知", text_color="gray")
            return

        try:
            last_dt = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
            remaining = COOKIE_VALID_DAYS - (datetime.now() - last_dt).days
            if remaining > 0:
                self.cookie_status.configure(
                    text=f"Cookies: 约 {remaining:.1f} 天", text_color="#22C55E"
                )
            else:
                self.cookie_status.configure(text="Cookies: 已过期", text_color="#EF4444")
        except ValueError:
            self.cookie_status.configure(text="Cookies: 未知", text_color="gray")

    # ==================== 设置保存 ====================

    def _collect_settings(self) -> dict:
        return {
            "yuketang_server": self.yuketang_server_var.get(),
            "start_time": self.start_time_var.get(),
            "end_time": self.end_time_var.get(),
            "headless_mode": self.headless_var.get(),
            "debug_mode": self.debug_var.get(),
            "ai_model": self.ai_model_var.get(),
            "doubao_api_key": self.doubao_key_var.get(),
            "gemini_api_key": self.gemini_key_var.get(),
            "custom_ai_base_url": self.custom_base_url_var.get(),
            "custom_ai_api_key": self.custom_api_key_var.get(),
            "custom_ai_model": self.custom_model_var.get(),
            "multi_ai_config_path": self.multi_ai_path_var.get(),
            "multi_ai_timeout": int(self.multi_ai_timeout_var.get()),
            "submit_delay": int(self.submit_delay_var.get()),
            "check_interval": int(self.check_interval_var.get()),
            "quiz_refresh_interval": int(self.quiz_refresh_interval_var.get()),
            "xxtui_api_key": self.xxtui_key_var.get(),
        }

    def _on_server_changed(self, *_args) -> None:
        """切换雨课堂服务器时提示重新登录（各服务器登录态按域名隔离）。"""
        name = self.yuketang_server_var.get()
        if name == self._active_server:
            return
        self._active_server = name
        self._log(
            f"雨课堂服务器已切换为「{name}」。各服务器登录态相互独立，"
            "请重新获取登录 Cookies。"
        )

    def _on_setting_changed(self, *_args) -> None:
        """任一设置变化后防抖 500ms 再写盘，避免逐键触发。"""
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, self._live_save)

    def _live_save(self) -> None:
        """实时保存当前设置；失败只记节流日志，不弹窗打断输入。"""
        self._save_job = None
        try:
            settings = self._collect_settings()
        except ValueError:
            # 数字字段处于无效中间态（如清空输入中）：不保存也不打扰，
            # 待输入完成后下一次变更会正常落盘。
            return

        errors = self.config.save(settings)
        if errors:
            now = time.monotonic()
            if now - self._last_save_error_log > 10:
                self._last_save_error_log = now
                self._log("设置保存失败：" + "；".join(errors))
            return

        self.notification.api_key = self.config.get("xxtui_api_key", "")

    # ==================== Bot 控制 ====================

    def _toggle_bot(self) -> None:
        if self.is_running:
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self) -> None:
        """启动自动答题（浏览器由 bot 线程自行管理）。"""
        if self._bot_thread and self._bot_thread.is_alive():
            self._log("上一次自动答题仍在退出中，请稍候。")
            return

        headless = self.config.get("headless_mode", False)
        server = self.config.get("yuketang_server", DEFAULT_SERVER)
        self.browser = BrowserManager(
            headless=headless,
            base_url=YUKETANG_SERVERS.get(server, YUKETANG_SERVERS[DEFAULT_SERVER]),
        )
        self.stop_event = threading.Event()
        ai_service = AIService(self.config)

        self.is_running = True
        self._stopping = False
        self._set_running_ui(True)
        self._log("开始检查课程...")

        bot = Bot(
            config=self.config,
            browser=self.browser,
            ai_service=ai_service,
            notification=self.notification,
            stop_event=self.stop_event,
        )

        thread: threading.Thread

        def run() -> None:
            try:
                bot.run()
            finally:
                try:
                    self.after(0, lambda: self._on_bot_finished(thread))
                except (RuntimeError, tk.TclError):
                    pass

        thread = threading.Thread(target=run, daemon=True)
        self._bot_thread = thread
        thread.start()

    def _stop_bot(self) -> None:
        """停止自动答题（bot 线程自行关闭浏览器）。"""
        if self._stopping:
            return
        self.is_running = False
        self._stopping = True
        if self.stop_event is not None:
            self.stop_event.set()
        self._set_stopping_ui()
        self._log("正在停止自动答题...")

        if not self._bot_thread or not self._bot_thread.is_alive():
            self._on_bot_finished(self._bot_thread)

    def _on_bot_finished(self, thread: threading.Thread | None) -> None:
        if thread is not self._bot_thread:
            return
        self._bot_thread = None
        self.browser = None
        self.stop_event = None
        self.is_running = False
        self._stopping = False
        self._set_running_ui(False)
        self._log("自动答题已停止。")
        if self._closing:
            self._wait_for_shutdown()

    def _set_stopping_ui(self) -> None:
        self.status_canvas.itemconfig(self.status_dot, fill="#E6A817")
        self.status_label.configure(text="正在停止")
        self.toggle_btn.configure(text="⏳  正在停止", state="disabled")

    def _set_running_ui(self, running: bool) -> None:
        if running:
            self.status_canvas.itemconfig(self.status_dot, fill="#43A047")
            self.status_label.configure(text="运行中")
            self.toggle_btn.configure(
                text="⏹  停止自动答题", fg_color="#E53935", hover_color="#C62828",
                state="normal",
            )
        else:
            self.status_canvas.itemconfig(self.status_dot, fill="#BDBDBD")
            self.status_label.configure(text="已停止")
            self.toggle_btn.configure(
                text="▶  启动自动答题", fg_color="#43A047", hover_color="#388E3C",
                state="normal",
            )

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        # 关窗前把防抖中尚未落盘的设置改动立即保存，避免丢失最后一次修改。
        if self._save_job is not None:
            self.after_cancel(self._save_job)
            self._save_job = None
            self._live_save()
        self._cookie_stop_event.set()
        if self._bot_thread and self._bot_thread.is_alive():
            self.is_running = False
            self._stopping = True
            if self.stop_event is not None:
                self.stop_event.set()
            self._set_stopping_ui()
            self._log("正在关闭应用并等待自动答题退出...")
        self._wait_for_shutdown()

    def _wait_for_shutdown(self) -> None:
        bot_alive = bool(self._bot_thread and self._bot_thread.is_alive())
        cookie_alive = bool(self._cookie_thread and self._cookie_thread.is_alive())
        if bot_alive or cookie_alive:
            self.after(100, self._wait_for_shutdown)
            return
        self._finish_close()

    def _finish_close(self) -> None:
        if self._destroying:
            return
        self._destroying = True
        self.notification.shutdown()
        _stop_logging()
        self.destroy()


# ==================== 入口 ====================


def run_app(*, auto_start: bool = False) -> bool:
    """启动唯一应用实例；普通模式与 Debug 模式共用同一把锁。"""
    instance_lock = InstanceLock()
    if not instance_lock.acquire():
        dialog = tk.Tk()
        dialog.withdraw()
        messagebox.showwarning(
            "程序已在运行",
            "检测到另一个雨课堂助手实例。请先关闭旧实例，避免重复调用 AI。",
            parent=dialog,
        )
        dialog.destroy()
        return False

    try:
        _setup_logging()
        app = App()
        if auto_start:
            app.after(250, app._start_bot)
        app.mainloop()
        return True
    finally:
        _stop_logging()
        instance_lock.release()


if __name__ == "__main__":
    run_app()
