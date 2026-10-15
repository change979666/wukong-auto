import logging
from typing import Any, Dict, Optional

import requests

from modules.config import load_config

config = load_config()
logger = logging.getLogger("wukong.alert")


class Alert:
    # /**
    #  * 日志与告警模块
    #  * 通过钉钉或 Server酱推送关键事件
    #  */

    def __init__(self) -> None:
        self._enabled: bool = bool(config.get("alert", {}).get("enable", False))

    def _post_json(self, url: str, payload: Dict[str, Any]) -> None:
        try:
            requests.post(url, json=payload, timeout=5)  # noqa: S113
        except Exception as exc:  # noqa: BLE001
            logger.error("告警发送失败: %s", exc, exc_info=True)

    def send_alert(self, title: str, message: str) -> None:
        # /**
        #  * 发送告警消息
        #  * @param title 标题
        #  * @param message 详情
        #  */
        if not self._enabled:
            return
        alert_cfg: Dict[str, Any] = config.get("alert", {})
        dingtalk_url: Optional[str] = alert_cfg.get("dingtalk_webhook")
        serverchan_key: Optional[str] = alert_cfg.get("serverchan_key")

        if dingtalk_url:
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": f"**{title}**\n\n{message}"},
            }
            self._post_json(dingtalk_url, payload)

        if serverchan_key:
            url = f"https://sctapi.ftqq.com/{serverchan_key}.send"
            data = {"title": title, "desp": message}
            try:
                requests.post(url, data=data, timeout=5)  # noqa: S113
            except Exception as exc:  # noqa: BLE001
                logger.error("Server酱告警发送失败: %s", exc, exc_info=True)

