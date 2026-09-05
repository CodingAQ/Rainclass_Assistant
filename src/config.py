"""配置管理模块 - 负责 config.json 的读写和校验。"""

import json
import logging
import os
import re
import tempfile
import threading
from typing import Any


CONFIG_FILE = "config.json"
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """轻量 .env 加载器：把 KEY=VALUE 注入 os.environ（不覆盖已存在的值）。

    不引入 python-dotenv 依赖；仅支持简单 KEY=VALUE 行、# 注释与引号包裹的值。
    """
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


# 模块加载时自动加载 .env（仅一次）
_load_dotenv()

# 默认配置
DEFAULTS: dict[str, Any] = {
    "start_time": "07:00",
    "end_time": "22:00",
    "headless_mode": False,
    "ai_model": "豆包AI",
    "doubao_api_key": "",
    "gemini_api_key": "",
    "custom_ai_base_url": "",
    "custom_ai_api_key": "",
    "custom_ai_model": "",
    "multi_ai_config_path": "model_visible.ini",
    "multi_ai_timeout": 20,
    "submit_delay": 1,
    "check_interval": 60,
    "quiz_refresh_interval": 1,
    "xxtui_api_key": "",
    "last_cookie_warn_date": "",
    "last_cookie_update_time": "",
    "debug_mode": False,  # 调试模式：保存 HTML/截图/tab 列表到 debug/
}

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class Config:
    """配置管理器，提供类型安全的配置读写。

    所有对 _data 的读写都经过 self._lock 保护，支持 bot 线程与 UI 线程并发访问。
    """

    # 敏感字段优先从环境变量读取，避免密钥明文落盘到 config.json
    _ENV_KEYS = {
        "doubao_api_key": "DOUBAO_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "custom_ai_api_key": "CUSTOM_AI_API_KEY",
        "xxtui_api_key": "XXTUI_API_KEY",
    }

    def __init__(self, config_file: str = CONFIG_FILE):
        self.config_file = config_file
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """从文件加载配置，不存在或损坏时返回默认值。"""
        if not os.path.exists(self.config_file):
            return dict(DEFAULTS)

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"配置文件无法读取，已使用默认配置：{e}")
            return dict(DEFAULTS)

        # 合并默认值，确保新字段存在
        merged = dict(DEFAULTS)
        merged.update(data)
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        env_name = self._ENV_KEYS.get(key)
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update_from_dict(self, values: dict[str, Any]) -> None:
        """批量更新配置。"""
        with self._lock:
            self._data.update(values)

    def save(self, settings: dict[str, Any]) -> list[str]:
        """保存配置到文件，返回校验错误列表（空列表表示成功）。"""
        errors = self._validate(settings)
        if errors:
            return errors

        with self._lock:
            previous = dict(self._data)
            self._data.update(settings)
            try:
                self._write_file()
            except OSError as e:
                self._data = previous
                return [f"配置文件写入失败：{e}"]
        return []

    def _validate(self, settings: dict[str, Any]) -> list[str]:
        """校验配置项，返回错误消息列表。"""
        errors: list[str] = []

        # 时间格式校验
        for key in ("start_time", "end_time"):
            val = settings.get(key, "")
            if isinstance(val, str) and val and not TIME_RE.match(val):
                errors.append(f"{key} 格式错误，应为 HH:MM（如 07:00）")

        # 时间窗口：允许跨午夜（如 22:00 - 06:00）。bot 侧按 start > end 解释为
        # 跨午夜窗口，因此这里不再强制 start <= end，仅做格式校验（上面已完成）。

        # AI 模型合法性
        ai_model = settings.get("ai_model")
        if ai_model not in ("豆包AI", "Gemini AI", "自定义", "多AI作答"):
            errors.append("AI 模型必须是：豆包AI / Gemini AI / 自定义 / 多AI作答")
        if ai_model == "多AI作答" and not str(
            settings.get("multi_ai_config_path", "")
        ).strip():
            errors.append("多AI配置文件不能为空")

        # 布尔字段类型
        for key in ("headless_mode", "debug_mode"):
            if not isinstance(settings.get(key), bool):
                errors.append(f"{key} 必须是布尔值（true/false）")

        # 数值范围校验
        int_fields = {
            "submit_delay": (0, 300),
            "check_interval": (5, 3600),
            "quiz_refresh_interval": (1, 300),
            "multi_ai_timeout": (1, 300),
        }
        for key, (lo, hi) in int_fields.items():
            val = settings.get(key)
            try:
                val = int(val)
            except (ValueError, TypeError):
                errors.append(f"{key} 必须是数字")
                continue
            if not (lo <= val <= hi):
                errors.append(f"{key} 范围应为 {lo}~{hi}")

        return errors

    def _write_file(self) -> None:
        """通过同目录临时文件原子替换配置（调用方须持有 _lock）。"""
        target = os.path.abspath(self.config_file)
        directory = os.path.dirname(target)
        os.makedirs(directory, exist_ok=True)
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=directory,
                prefix=".rainclass-config-",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = f.name
                json.dump(self._data, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target)
        except OSError:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def persist(self) -> bool:
        """将当前内存状态写入文件，返回是否成功。"""
        with self._lock:
            try:
                self._write_file()
                return True
            except OSError as e:
                logger.error(f"配置文件写入失败：{e}")
                return False

    def reload(self) -> None:
        """重新加载配置文件。"""
        with self._lock:
            self._data = self._load()
