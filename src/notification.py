"""通知模块 - 微信推送通知。"""

import logging
import requests
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)


class NotificationService:
    """通过 xxtui 平台发送微信通知。"""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
        self._closed = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def send(self, title: str, content: str) -> bool:
        """发送微信通知，返回是否成功。"""
        if not self.api_key:
            logger.warning("未设置微信提醒 API Key，通知功能已禁用。")
            return False

        api_url = f"https://www.xxtui.com/xxtui/{self.api_key}"
        headers = {"Content-Type": "application/json"}
        data = {
            "from": "课堂机器人",
            "title": title,
            "content": content,
            "channel": "WX_MP",
        }

        logger.info("正在向 xxtui 发送通知。")
        try:
            response = requests.post(api_url, headers=headers, json=data, timeout=5)
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 0:
                logger.info("微信通知发送成功。")
                return True
            else:
                logger.warning(
                    f"微信通知发送失败，错误码：{result.get('code')}，信息：{result.get('message')}"
                )
                return False
        except (requests.RequestException, ValueError, TypeError) as e:
            logger.error(f"发送微信通知时发生错误：{e}")
            return False

    def send_async(self, title: str, content: str) -> Optional[Future[bool]]:
        """后台发送通知；通知拥塞或失败不会阻塞课堂处理。"""
        if self._closed:
            return None
        return self._executor.submit(self.send, title, content)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
