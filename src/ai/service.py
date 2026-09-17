"""AI 服务模块 - 调用各种 AI 模型获取答案。

支持：豆包AI / Gemini AI / 自定义 OpenAI 兼容 Provider。
"""

import base64
import configparser
import json
import logging
import os
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple, cast

import requests
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)

# 压缩模型报错
_ERROR_LOG_LIMIT = 120
_HTML_MARKERS = ("<html", "<!doctype", "<script", "just a moment", "challenge")


def _compact_error(value: object, limit: int = _ERROR_LOG_LIMIT) -> str:
    """把异常/错误文本压成适合日志的一行短消息，在截断时标注原始长度"""
    text = str(value).strip()
    compact = re.sub(r"\s+", " ", text)
    lowered = compact.lower()
    if any(marker in lowered for marker in _HTML_MARKERS):
        return "返回 HTML 页面（疑似被网关或 Cloudflare 拦截）"
    if len(compact) > limit:
        return f"{compact[:limit]}...(已截断，原始 {len(text)} 字符)"
    return compact

# 提示词，模型统一返回 JSON 
PROMPT_ANSWER = (
    "请分析这张图片中的习题，并返回题目类型和正确的答案选项json，格式为："
    '{"type":"题目类型","answers":"正确的答案选项"} 。'
    "如果是单选题（single），请返回题目类型和正确的答案选项，如："
    '{"type":"single","answers":"A"} ；'
    "如果是多选题（multi），请返回题目类型和正确的答案选项数组，如："
    '{"type":"multi","answers":["A","B","D"]} ；'
    "如果是填空题（fill）或主观题（sub），请返回题目类型，如："
    '{"type":"fill"} ；'
    "如果你没有视觉模块，无法读取我上传的图片，请直接返回："
    '{"type":"unknown"}'
    "回答仅包含json，禁止使用代码块包裹，禁止使用反引号，回复不要包含任何额外信息。"
)

# 测试模型视觉
TEST_PROMPT = "用十六个字以内描述该图片"
TEST_IMAGE_PATH = "test_pic.png"


NO_THINKING_EXTRA_BODY = {"enable_thinking": False}


@dataclass(frozen=True)
class _MultiAIEndpoint:
    name: str
    base_url: str
    api_key: str
    model: str
    description: str = ""


class AIService:
    """AI 答题服务，支持单模型及 INI 配置的多模型并行投票。"""

    ANSWER_MAX_TOKENS = 128

    def __init__(self, config: "Config"):  # type: ignore
        self.config = config
        self._request_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ai-request"
        )
        self._last_cleanup = 0.0
        self._closed = False

        # 手动截断与进度：GUI 线程只递增序号，请求侧在入口快照基线后自行比对。
        # 用递增序号而不是 Event，是为了避免两题之间空窗期的一次点击残留下来，
        # 把下一题瞬间截断（Event 需要清理，清理就有竞态）。
        self._multi_lock = threading.Lock()
        self._truncate_seq = 0
        self._multi_run_id = 0
        self._multi_status: dict = {
            "active": False,
            "run_id": 0,
            "total": 0,
            "received": 0,
            "valid": 0,
            "started": 0.0,
            "truncated": False,
        }

    # ==================== 公开方法 ====================

    def get_answer(self, image_url: str, cookies: Optional[dict] = None) -> str:
        """
        根据配置的模型调用 AI 并返回答案。

        cookies：可选，带鉴权下载题目图片（雨课堂题图可能需要登录态）。
        图片一律先下载再以 base64 传输，避免 URL 直传失败后产生第二次调用。
        """
        prompt = PROMPT_ANSWER
        model = self.config.get("ai_model", "豆包AI")
        if self._closed:
            return "AI调用失败：服务已经关闭。"
        if model == "多AI作答":
            return self._get_multi_answer(image_url=image_url, cookies=cookies)

        try:
            image_bytes, _ = self._download_and_save(image_url, cookies)
        except Exception as e:
            logger.error(f"下载图片失败：{e}")
            return f"图片下载失败（调用失败）：{e}"

        if model == "Gemini AI":
            return self._ask_gemini(image_bytes, prompt)

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return self._ask(image_url, image_bytes, prompt, test_image_base64=b64)

    def _download_and_save(
        self,
        image_url: str,
        cookies: Optional[dict] = None,
        timeout: float = 10,
    ) -> Tuple[bytes, str]:
        """下载图片并保存到 data/YYYY-MM-DD/HH-MM-SS.png。返回 (bytes, 路径)。

        cookies：可选，用于带鉴权下载（雨课堂题图可能需要登录态）。
        """
        response = requests.get(image_url, timeout=timeout, cookies=cookies)
        response.raise_for_status()
        image_bytes = response.content

        now = datetime.now()
        data_dir = os.path.join("data", now.strftime("%Y-%m-%d"))
        os.makedirs(data_dir, exist_ok=True)
        filepath = os.path.join(data_dir, f"{now.strftime('%H-%M-%S-%f')}.png")

        with open(filepath, "wb") as f:
            f.write(image_bytes)

        logger.info(f"题目截图已保存：{filepath}")
        # 定时清理旧截图，避免每次下载都全盘遍历
        now_ts = time.time()
        if now_ts - self._last_cleanup > 3600:
            self._last_cleanup = now_ts
            self._cleanup_old_screenshots(max_age_days=7)
        return image_bytes, filepath

    def _cleanup_old_screenshots(self, max_age_days: int = 7) -> None:
        """清理 data/ 下超过 max_age_days 天的题目截图，避免无限堆积。"""
        data_root = "data"
        if not os.path.isdir(data_root):
            return
        cutoff = time.time() - max_age_days * 86400
        try:
            for day_dir in os.listdir(data_root):
                d = os.path.join(data_root, day_dir)
                if not os.path.isdir(d):
                    continue
                for fn in os.listdir(d):
                    fp = os.path.join(d, fn)
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            os.remove(fp)
                    except OSError:
                        pass
                try:
                    if not os.listdir(d):
                        os.rmdir(d)
                except OSError:
                    pass
        except OSError:
            pass

    def test_vision(self) -> str:
        """使用 test_pic.png 测试当前选中的 AI 模型视觉能力。"""
        if not os.path.exists(TEST_IMAGE_PATH):
            return f"测试图片 {TEST_IMAGE_PATH} 不存在，请放入项目根目录。"

        with open(TEST_IMAGE_PATH, "rb") as f:
            image_bytes = f.read()

        test_image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        if self.config.get("ai_model", "豆包AI") == "多AI作答":
            return self._test_multi_vision(
                test_image_base64,
                self._guess_mime(image_bytes),
            )
        return self._ask(None, image_bytes, TEST_PROMPT, test_image_base64=test_image_base64)

    def answer_from_image(self, image_b64: str) -> str:
        """用截图 base64 直接获取答案（图片 URL 不可用时的兜底方案）。"""
        if self.config.get("ai_model", "豆包AI") == "多AI作答":
            return self._get_multi_answer(image_b64=image_b64)
        prompt = PROMPT_ANSWER
        try:
            return self._ask(None, None, prompt, test_image_base64=image_b64)
        except Exception as e:
            logger.error(f"截图答题失败：{e}")
            return f"答题失败（调用失败）：{e}"

    def shutdown(self) -> None:
        """关闭线程池，释放资源。Bot 停止时调用。"""
        if self._closed:
            return
        self._closed = True
        self._request_executor.shutdown(wait=False, cancel_futures=True)
        logger.debug("AI 服务线程池已关闭。")

    # ==================== 手动截断 / 进度 ====================

    def request_truncate(self) -> None:
        """请求提前结束当前多AI等待：不再等剩余模型，用已返回的答案投票。

        线程安全，可由 GUI 线程直接调用。只递增序号，不做别的判断；
        正在等待的请求会自行发现序号变化并立即收尾。若此刻没有请求在收集，
        这次点击不会残留成下一题的误截断（请求在入口快照基线，只认基线之后的递增）。
        """
        with self._multi_lock:
            self._truncate_seq += 1

    def multi_progress(self) -> dict:
        """返回多AI作答的只读进度快照，供 GUI 轮询显示与判断能否截断。"""
        with self._multi_lock:
            snapshot = dict(self._multi_status)
        elapsed = 0.0
        if snapshot["active"] and snapshot["started"]:
            elapsed = max(0.0, time.monotonic() - snapshot["started"])
        snapshot["elapsed"] = elapsed
        return snapshot

    def _begin_multi_run(self, total: int) -> tuple[int, int]:
        """登记一轮多AI收集，返回 (本轮 id, 截断序号基线)。"""
        with self._multi_lock:
            self._multi_run_id += 1
            self._multi_status = {
                "active": True,
                "run_id": self._multi_run_id,
                "total": total,
                "received": 0,
                "valid": 0,
                "started": time.monotonic(),
                "truncated": False,
            }
            return self._multi_run_id, self._truncate_seq

    def _update_multi_progress(self, run_id: int, received: int, valid: int) -> None:
        """更新本轮计数。迟到的工人线程拿着旧 run_id，不会污染新一轮的显示。"""
        with self._multi_lock:
            if self._multi_status["run_id"] != run_id:
                return
            self._multi_status["received"] = received
            self._multi_status["valid"] = valid

    def _finish_multi_run(
        self,
        run_id: int,
        received: int,
        valid: int,
        truncated: bool,
    ) -> None:
        """收尾本轮：置为不活跃，保留最终计数供 GUI 显示最后一帧。"""
        with self._multi_lock:
            if self._multi_status["run_id"] != run_id:
                return
            self._multi_status["active"] = False
            self._multi_status["received"] = received
            self._multi_status["valid"] = valid
            self._multi_status["truncated"] = truncated

    def submit_answer(
        self,
        *,
        image_url: Optional[str] = None,
        image_b64: Optional[str] = None,
        cookies: Optional[dict] = None,
    ) -> Future[str]:
        """异步提交答题请求；调用方负责在页面线程中处理返回结果。"""
        if self._closed:
            raise RuntimeError("AI 服务已经关闭")
        if self.config.get("ai_model", "豆包AI") == "多AI作答":
            if image_url:
                return self._start_daemon_request(self.get_answer, image_url, cookies)
            if image_b64:
                return self._start_daemon_request(self.answer_from_image, image_b64)
            raise ValueError("答题请求缺少图片数据")
        if image_url:
            return self._request_executor.submit(self.get_answer, image_url, cookies)
        if image_b64:
            return self._request_executor.submit(self.answer_from_image, image_b64)
        raise ValueError("答题请求缺少图片数据")

    @staticmethod
    def _start_daemon_request(
        function: Callable[..., str],
        *args,
    ) -> Future[str]:
        """直接启动协调线程，避免多 AI 请求进入线程池队列。"""
        future: Future[str] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)

        threading.Thread(target=run, name="multi-ai-coordinator", daemon=True).start()
        return future


    # ==================== 内部路由 ====================

    @staticmethod
    def _guess_mime(raw: bytes) -> str:
        """根据文件头推断图片 MIME（截图可能是 JPEG / WEBP 等非 PNG 格式）。"""
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw[:4] == b"GIF8":
            return "image/gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        return "image/png"  # 兜底

    def _ask(
        self,
        image_url: Optional[str],
        image_bytes: Optional[bytes],
        prompt: str,
        test_image_base64: Optional[str] = None,
    ) -> str:
        """统一路由到对应 Provider。"""
        model = self.config.get("ai_model", "豆包AI")

        if model == "豆包AI":
            return self._ask_doubao(image_url, image_bytes, prompt, test_image_base64)
        elif model == "Gemini AI":
            return self._ask_gemini(image_bytes, prompt, test_image_base64)
        elif model == "自定义":
            return self._ask_custom(image_url, prompt, test_image_base64)
        elif model == "多AI作答":
            if not test_image_base64:
                return "多AI调用失败：无图片数据。"
            mime_type = self._guess_mime(image_bytes) if image_bytes else "image/png"
            return self._ask_multi(test_image_base64, prompt, mime_type)
        else:
            return f"未知的 AI 模型：{model}"

    # ==================== 多 AI ====================

    def _multi_ai_timeout(self) -> float:
        try:
            timeout = float(self.config.get("multi_ai_timeout", 20))
        except (TypeError, ValueError):
            timeout = 20.0
        return max(1.0, min(300.0, timeout))

    def _multi_ai_config_path(self) -> Path:
        value = str(
            self.config.get("multi_ai_config_path", "model_visible.ini")
        ).strip() or "model_visible.ini"
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path.resolve()

    def _load_multi_ai_endpoints(self) -> list[_MultiAIEndpoint]:
        path = self._multi_ai_config_path()
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                parser.read_file(file)
        except (OSError, configparser.Error) as exc:
            logger.error("多AI配置文件读取失败：%s", _compact_error(exc))
            return []

        endpoints: list[_MultiAIEndpoint] = []
        for section in parser.sections():
            base_url = parser.get(section, "base_url", fallback="").strip()
            api_key = parser.get(section, "key", fallback="").strip()
            model = parser.get(section, "model", fallback="").strip()
            if not base_url or not api_key or not model:
                logger.warning("多AI配置 [%s] 缺少 base_url/key/model，已跳过。", section)
                continue
            endpoints.append(
                _MultiAIEndpoint(
                    name=section,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    description=parser.get(section, "description", fallback="").strip(),
                )
            )
        return endpoints

    def _run_multi_requests(
        self,
        image_b64: str,
        prompt: str,
        mime_type: str = "image/png",
        max_wait: Optional[float] = None,
    ) -> tuple[list[_MultiAIEndpoint], list[tuple[str, str]], int, bool]:
        """同时请求全部模型，返回「截止前收到的结果」「未完成数量」及是否被手动截断。"""
        endpoints = self._load_multi_ai_endpoints()
        if not endpoints:
            return [], [], 0, False

        configured_timeout = self._multi_ai_timeout()
        timeout = (
            configured_timeout
            if max_wait is None
            else max(0.05, min(configured_timeout, max_wait))
        )
        deadline = time.monotonic() + timeout
        run_id, baseline = self._begin_multi_run(len(endpoints))
        lock = threading.Lock()
        all_done = threading.Event()
        results: list[tuple[str, str]] = []
        state = {"remaining": len(endpoints), "accepting": True, "received": 0, "valid": 0}

        def worker(endpoint: _MultiAIEndpoint) -> None:
            started = time.monotonic()
            try:
                remaining = max(0.1, deadline - time.monotonic())
                result = self._ask_multi_endpoint(
                    endpoint,
                    image_b64,
                    prompt,
                    remaining,
                    mime_type,
                )
            except Exception as exc:
                logger.warning("多AI模型 [%s] 请求失败：%s", endpoint.name, exc)
            else:
                text = result.strip()
                accepted = False
                vote: Optional[tuple[str, ...]] = None
                with lock:
                    if state["accepting"] and time.monotonic() <= deadline:
                        results.append((endpoint.name, text))
                        state["received"] += 1
                        vote = self._canonical_vote(text)
                        if vote is not None:
                            state["valid"] += 1
                        accepted = True
                    received, valid = state["received"], state["valid"]
                # 到达即上屏：用户要看着「已有几个答案」才知道何时值得手动截断，
                # 不能等全部收完再一次性打印。
                if accepted:
                    cost = time.monotonic() - started
                    if vote is None:
                        logger.info(
                            "多AI模型 [%s] 已返回（%.1fs），但内容无法解析为有效答案。",
                            endpoint.name,
                            cost,
                        )
                    else:
                        logger.info(
                            "多AI模型 [%s] 已返回（%.1fs）：%s",
                            endpoint.name,
                            cost,
                            self._vote_to_answer(vote),
                        )
                self._update_multi_progress(run_id, received, valid)
            finally:
                with lock:
                    state["remaining"] -= 1
                    done = state["remaining"] == 0
                if done:
                    all_done.set()

        logger.info(
            "多AI作答：同时请求 %d 个模型，最大等待 %.1f 秒。",
            len(endpoints),
            timeout,
        )
        for endpoint in endpoints:
            threading.Thread(
                target=worker,
                args=(endpoint,),
                name=f"multi-ai-{endpoint.name}",
                daemon=True,
            ).start()

        # 等待期间每 50ms 瞄一眼是否被手动截断；截断即收尾，用已收到的答案投票。
        truncated = False
        while True:
            remaining_wait = deadline - time.monotonic()
            if remaining_wait <= 0 or all_done.wait(min(0.05, remaining_wait)):
                break
            with self._multi_lock:
                if self._truncate_seq > baseline:
                    truncated = True
                    break

        with lock:
            state["accepting"] = False
            accepted = list(results)
            pending = state["remaining"]
            final_received = state["received"]
            final_valid = state["valid"]
        if truncated:
            logger.warning(
                "已手动截断：不再等待剩余 %d 个模型，改用已返回的 %d 个有效答案投票。",
                pending,
                final_valid,
            )
        elif pending:
            logger.warning("多AI等待时间已到，忽略 %d 个迟到模型。", pending)
        self._finish_multi_run(run_id, final_received, final_valid, truncated)
        return endpoints, accepted, pending, truncated

    def _ask_multi_endpoint(
        self,
        endpoint: _MultiAIEndpoint,
        image_b64: str,
        prompt: str,
        timeout: float,
        mime_type: str = "image/png",
    ) -> str:
        """向一个 OpenAI 兼容模型发送一次请求，不做 SDK 自动重试。"""
        client = OpenAI(
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
            max_retries=0,
            timeout=timeout,
        )
        try:
            response = client.chat.completions.create(
                model=endpoint.model,
                messages=cast(list[ChatCompletionMessageParam], [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]),
                timeout=timeout,
                max_tokens=self.ANSWER_MAX_TOKENS,
                extra_body=NO_THINKING_EXTRA_BODY,
            )
            return (response.choices[0].message.content or "").strip()
        finally:
            client.close()

    @staticmethod
    def _failed_multi_result(text: str) -> bool:
        if not text or not text.strip():
            return True
        lowered = text.lower()
        return any(marker in lowered for marker in (
            "调用失败",
            "答题失败",
            "下载失败",
            "无法获取",
            "未设置",
            "error",
            "timeout",
            "insufficient balance",
        ))

    @staticmethod
    def _vote_letters(value, *, allow_compact: bool = False) -> tuple[str, ...]:
        if isinstance(value, list):
            text = ",".join(str(item) for item in value)
        else:
            text = str(value or "")
        compact = text.strip().upper()
        if allow_compact and re.fullmatch(r"[A-G]{1,7}", compact):
            return tuple(sorted(set(compact)))
        match = re.fullmatch(
            r"\s*(?:(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*)?"
            r"([A-G](?:\s*[,，、/\s]\s*[A-G])*)\s*[。.]?\s*",
            text.upper(),
        )
        if not match:
            return ()
        return tuple(sorted(set(re.findall(r"[A-G]", match.group(1)))))

    @staticmethod
    def _extract_answer_json(value: str) -> Optional[dict]:
        """从代码块或思维链文本中提取包含答题字段的首个 JSON 对象。"""
        decoder = json.JSONDecoder()
        fallback = None
        for match in re.finditer(r"\{", value):
            try:
                candidate, _ = decoder.raw_decode(value[match.start() :])
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            if "type" in candidate or "answers" in candidate:
                return candidate
            if fallback is None:
                fallback = candidate
        return fallback

    @classmethod
    def _canonical_vote(cls, text: str) -> Optional[tuple[str, ...]]:
        """把不同模型的等价答案归一化为可计票键。"""
        if not text or not text.strip():
            return None
        value = text.strip()
        value = re.sub(r"^```(?:json)?", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"```$", "", value).strip()

        data = cls._extract_answer_json(value)

        if data is None:
            if cls._failed_multi_result(value):
                return None
            letters = cls._vote_letters(value)
            if letters:
                return ("choice", *letters)
            judgment = value.upper().rstrip("。.")
            if judgment in ("对", "正确", "T", "TRUE", "是"):
                return ("judgment", "true")
            if judgment in ("错", "错误", "F", "FALSE", "否"):
                return ("judgment", "false")
            return None

        qtype = str(data.get("type", "")).strip().lower()
        if qtype == "unknown":
            return None
        raw_answer = data.get("answers", "")
        letters = cls._vote_letters(raw_answer, allow_compact=True)
        if letters:
            return ("choice", *letters)

        judgment = str(raw_answer).strip().upper().rstrip("。.")
        if judgment in ("对", "正确", "T", "TRUE", "是"):
            return ("judgment", "true")
        if judgment in ("错", "错误", "F", "FALSE", "否"):
            return ("judgment", "false")
        if qtype in ("fill", "sub"):
            return ("type", qtype)
        return None

    @staticmethod
    def _vote_to_answer(vote: tuple[str, ...]) -> str:
        kind = vote[0]
        if kind == "choice":
            letters = list(vote[1:])
            payload = {
                "type": "single" if len(letters) == 1 else "multi",
                "answers": letters[0] if len(letters) == 1 else letters,
            }
        elif kind == "judgment":
            payload = {
                "type": "single",
                "answers": "对" if vote[1] == "true" else "错",
            }
        else:
            payload = {"type": vote[1]}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _ask_multi(
        self,
        image_b64: str,
        prompt: str,
        mime_type: str = "image/png",
        max_wait: Optional[float] = None,
    ) -> str:
        endpoints, raw_results, _, truncated = self._run_multi_requests(
            image_b64,
            prompt,
            mime_type,
            max_wait,
        )
        if not endpoints:
            return "多AI调用失败：配置文件中没有可用模型。"

        # 每个模型的到达情况已在收集阶段实时上屏，这里只负责计票，不再重复打印。
        votes: list[tuple[str, ...]] = []
        for name, result in raw_results:
            vote = self._canonical_vote(result)
            if vote is None:
                continue
            votes.append(vote)

        if not votes:
            if truncated:
                return "多AI调用失败：已手动截断，但暂无有效答案。"
            return "多AI调用失败：截止时间内没有有效答案。"

        counts = Counter(votes)
        highest = max(counts.values())
        candidates = [vote for vote, count in counts.items() if count == highest]
        winner = random.choice(candidates)
        answer = self._vote_to_answer(winner)
        logger.info(
            "多AI投票完成：有效 %d/%d，最高 %d 票，采用 %s",
            len(votes),
            len(endpoints),
            highest,
            answer,
        )
        return answer

    def _get_multi_answer(
        self,
        *,
        image_url: Optional[str] = None,
        image_b64: Optional[str] = None,
        cookies: Optional[dict] = None,
    ) -> str:
        """在一个总截止时间内准备题图、并行请求并完成投票。"""
        timeout = self._multi_ai_timeout()
        deadline = time.monotonic() + timeout
        mime_type = "image/png"

        if image_url:
            try:
                image_bytes, _ = self._download_and_save(
                    image_url,
                    cookies,
                    timeout=min(10.0, timeout),
                )
            except Exception as exc:
                logger.error("下载图片失败：%s", exc)
                return f"多AI调用失败：图片下载失败：{exc}"
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
            mime_type = self._guess_mime(image_bytes)

        if not image_b64:
            return "多AI调用失败：无图片数据。"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "多AI调用失败：准备题图已超过最大等待时间。"
        return self._ask_multi(
            image_b64,
            PROMPT_ANSWER,
            mime_type,
            max_wait=remaining,
        )

    def _test_multi_vision(self, image_b64: str, mime_type: str = "image/png") -> str:
        endpoints, raw_results, _, _ = self._run_multi_requests(
            image_b64,
            TEST_PROMPT,
            mime_type,
        )
        if not endpoints:
            return "多AI测试失败：配置文件中没有可用模型。"
        valid = [
            f"[{name}] {result}"
            for name, result in raw_results
            if not self._failed_multi_result(result)
        ]
        return "\n".join(valid) if valid else "多AI测试失败：截止时间内没有有效返回。"

    # ==================== 豆包 AI ====================

    def _ask_doubao(
        self,
        image_url: Optional[str],
        image_bytes: Optional[bytes],
        prompt: str,
        test_image_base64: Optional[str] = None,
    ) -> str:
        api_key = self.config.get("doubao_api_key", "")
        if not api_key:
            return "豆包AI调用失败：未设置 API Key，无法获取答案。"

        if test_image_base64:
            image_url = f"data:image/png;base64,{test_image_base64}"

        try:
            client = OpenAI(
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key=api_key,
                max_retries=0,
            )
            response = client.chat.completions.create(
                model="doubao-seed-1-6-250615",
                messages=cast(list[ChatCompletionMessageParam], [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]),
                timeout=15,
                max_tokens=self.ANSWER_MAX_TOKENS,
                extra_body=NO_THINKING_EXTRA_BODY,
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except Exception as e:
            logger.error(f"调用豆包AI API 失败：{e}")
            return f"豆包AI调用失败：{e}"

    # ==================== Gemini AI ====================

    def _ask_gemini(
        self,
        image_bytes: Optional[bytes],
        prompt: str,
        test_image_base64: Optional[str] = None,
    ) -> str:
        api_key = self.config.get("gemini_api_key", "")
        if not api_key:
            return "Gemini AI调用失败：未设置 API Key，无法获取答案。"

        if test_image_base64:
            b64_data = test_image_base64
        elif image_bytes:
            b64_data = base64.b64encode(image_bytes).decode("utf-8")
        else:
            return "Gemini AI调用失败：无图片数据。"

        api_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={api_key}"
        )
        raw = base64.b64decode(b64_data)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": self._guess_mime(raw), "data": b64_data}},
                    ]
                }
            ],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

        try:
            response = requests.post(api_url, json=payload, timeout=15)
            response.raise_for_status()
            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                reason = (
                    result.get("promptFeedback", {}).get("finishReason")
                    or "无返回内容"
                )
                logger.error(f"Gemini 未返回候选内容（{reason}）。")
                return f"Gemini AI调用失败：{reason}"
            candidate = candidates[0]
            return (
                candidate.get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
        except Exception as e:
            logger.error("调用Gemini AI API 失败：%s", _compact_error(e))
            return f"Gemini AI调用失败：{_compact_error(e)}"

    # ==================== 自定义 Provider ====================

    def _ask_custom(
        self,
        image_url: Optional[str],
        prompt: str,
        test_image_base64: Optional[str] = None,
    ) -> str:
        base_url = self.config.get("custom_ai_base_url", "").strip()
        api_key = self.config.get("custom_ai_api_key", "").strip()
        model_id = self.config.get("custom_ai_model", "").strip()

        if not base_url or not api_key:
            return "自定义 AI 调用失败：未设置 Base URL 或 API Key，无法获取答案。"
        if not model_id:
            return "自定义 AI 调用失败：未设置 Model ID，无法获取答案。"

        if test_image_base64:
            image_url = f"data:image/png;base64,{test_image_base64}"

        try:
            client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
            )
            response = client.chat.completions.create(
                model=model_id,
                messages=cast(list[ChatCompletionMessageParam], [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]),
                timeout=15,
                max_tokens=self.ANSWER_MAX_TOKENS,
                extra_body=NO_THINKING_EXTRA_BODY,
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except Exception as e:
            logger.error(f"调用自定义 AI API 失败：{e}")
            return f"自定义 AI 调用失败：{e}"
