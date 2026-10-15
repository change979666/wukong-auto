import json
import logging
import os
import re
import sys
import time
from glob import glob
from typing import Any, Dict, Optional, Tuple

import pyperclip
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from modules.config import load_config

config = load_config()
logger = logging.getLogger("wukong.web_monitor")


class WebMonitor:
    # /**
    #  * 网页监测与自动复制模块
    #  * 负责浏览器初始化、状态轮询与邀请码复制
    #  */

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.invite_code: Optional[str] = None
        self.last_status: Dict[str, Any] = {}
        self._last_reload_ts: float = 0.0

    def init_browser(self) -> bool:
        # /**
        #  * 初始化内置浏览器（Playwright 自带内核，不依赖系统浏览器）
        #  * @returns bool 是否初始化成功
        #  */
        try:
            self._playwright = sync_playwright().start()
            browser_type_name = config["web"]["browser_type"]
            browser_type = getattr(self._playwright, browser_type_name)
            launch_args: Dict[str, Any] = {
                "headless": config["web"]["headless"],
            }

            def _pick_playwright_executable() -> Optional[str]:
                # /**
                #  * 选择可用的 Playwright 浏览器可执行文件（兼容 headless_shell / chrome）
                #  */
                candidates = []
                if getattr(sys, "frozen", False):
                    exe_dir = os.path.dirname(sys.executable)
                    candidates.extend(
                        [
                            os.path.join(
                                exe_dir,
                                "_internal",
                                "playwright",
                                "driver",
                                "package",
                                ".local-browsers",
                                "chromium_headless_shell-1161",
                                "chrome-win",
                                "headless_shell.exe",
                            ),
                            os.path.join(
                                exe_dir,
                                "_internal",
                                "playwright",
                                "driver",
                                "package",
                                ".local-browsers",
                                "chromium-1112",
                                "chrome-win",
                                "chrome.exe",
                            ),
                            os.path.join(exe_dir, "pw-browsers", "chromium-1112", "chrome-win", "chrome.exe"),
                            os.path.join(exe_dir, "_internal", "pw-browsers", "chromium-1112", "chrome-win", "chrome.exe"),
                        ],
                    )

                local_app = os.environ.get("LOCALAPPDATA", "")
                if local_app:
                    # 兼容不同版本号目录（如 chromium_headless_shell-1161 / chromium-1112）
                    candidates.extend(
                        sorted(
                            glob(
                                os.path.join(
                                    local_app,
                                    "ms-playwright",
                                    "chromium_headless_shell-*",
                                    "chrome-win",
                                    "headless_shell.exe",
                                ),
                            ),
                            reverse=True,
                        ),
                    )
                    candidates.extend(
                        sorted(
                            glob(
                                os.path.join(
                                    local_app,
                                    "ms-playwright",
                                    "chromium-*",
                                    "chrome-win",
                                    "chrome.exe",
                                ),
                            ),
                            reverse=True,
                        ),
                    )

                for p in candidates:
                    if p and os.path.exists(p):
                        return p
                return None

            # 打包版优先使用可定位到的浏览器内核，避免 .local-browsers 在传输中丢失导致启动失败
            if getattr(sys, "frozen", False):
                picked = _pick_playwright_executable()
                if picked:
                    launch_args["executable_path"] = picked
                    logger.info("使用可用浏览器内核启动 Chromium: %s", picked)
                else:
                    logger.warning("未找到可用 Playwright 内核，将使用默认查找逻辑")

            self._browser = browser_type.launch(**launch_args)
            self._context = self._browser.new_context()

            cookie_path = config["web"]["cookie_path"]
            if os.path.exists(cookie_path):
                with open(cookie_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self._context.add_cookies(cookies)

            self._page = self._context.new_page()
            url = config["web"]["url"]
            # 某些网络环境下 networkidle 容易超时，先快开页面，再尝试更稳等待
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                self._page.goto(url, wait_until="load", timeout=30000)
            logger.info("浏览器初始化完成，页面加载成功")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("浏览器初始化失败: %s", exc, exc_info=True)
            return False

    def get_invite_status(self) -> Dict[str, Any]:
        # /**
        #  * 解析邀请码区域的状态（基于页面 DOM，而不是仅靠固定文案）
        #  * @returns dict 结构化状态：state/status_text/header/next_release/copy_btn_enabled...
        #  */
        if not self._page:
            return {
                "ok": False,
                "error": "page_not_ready",
            }

        def _safe_text(selector: str) -> Optional[str]:
            try:
                loc = self._page.locator(selector)
                if loc.count() <= 0:
                    return None
                txt = loc.first.inner_text().strip()
                return txt or None
            except Exception:  # noqa: BLE001
                return None

        def _safe_attr(selector: str, name: str) -> Optional[str]:
            try:
                loc = self._page.locator(selector)
                if loc.count() <= 0:
                    return None
                val = loc.first.get_attribute(name)
                return val or None
            except Exception:  # noqa: BLE001
                return None

        def _safe_bool_tuple() -> Tuple[bool, bool, bool]:
            # returns: (exists, enabled, visible)
            try:
                for selector in ["button#inviteCopyBtn", "button#inviteCopyBtnNew", "button.invite-copy-btn"]:
                    btn = self._page.locator(selector)
                    if btn.count() <= 0:
                        continue
                    exists = True
                    enabled = bool(btn.first.is_enabled())
                    visible = bool(btn.first.is_visible())
                    return (exists, enabled, visible)
                # 新版页面：复制区域本身是可点击 div，而非 button
                for selector in [
                    "div.pill.invite-pill[role='button']",
                    "div.pill[role='button'][data-copy-enabled]",
                ]:
                    pill = self._page.locator(selector)
                    if pill.count() <= 0:
                        continue
                    exists = True
                    visible = bool(pill.first.is_visible())
                    copy_enabled_attr = pill.first.get_attribute("data-copy-enabled")
                    enabled = str(copy_enabled_attr).lower() != "false"
                    return (exists, enabled, visible)
                return (False, False, False)
            except Exception:  # noqa: BLE001
                return (False, False, False)

        invite_state = (
            _safe_attr("div#hero-invite-card", "data-invite-state")
            or _safe_attr("div.pill.invite-pill", "data-invite-state")
            or _safe_attr("div[role='button'][data-invite-state]", "data-invite-state")
        )
        header = _safe_text("#inviteCardHeader") or _safe_text("#invite-label")
        status_text = _safe_text("#inviteStatusText")
        code_value_text = _safe_text("#inviteCodeValue")
        if not code_value_text:
            code_value_text = _safe_text("#invite-code")
        (btn_exists, btn_enabled, btn_visible) = _safe_bool_tuple()

        # 从“下次发放时间：16:00”里抽取时间段，方便 GUI 显示
        next_release: Optional[str] = None
        for release_text in [code_value_text, status_text, header]:
            if release_text:
                m = re.search(r"(\d{1,2}:\d{2})", release_text)
                if m:
                    next_release = m.group(1)
                    break

        # 补充一些通用兜底文案识别
        has_used_up_text = (
            (status_text == "已用完")
            or (header and "已用完" in header)
            or (header and "exhausted" in header.lower())
            or (code_value_text and "已用完" in code_value_text)
        )
        has_login_hint = bool(self._page.locator("text=登录").count() > 0)  # 非强依赖，仅提示

        status: Dict[str, Any] = {
            "ok": True,
            "invite_state": invite_state,  # e.g. exhausted / available / ...
            "header": header,
            "status_text": status_text,
            "code_value_text": code_value_text,
            "next_release": next_release,
            "copy_btn": {
                "exists": btn_exists,
                "enabled": btn_enabled,
                "visible": btn_visible,
            },
            "hints": {
                "used_up": has_used_up_text,
                "maybe_need_login": has_login_hint,
            },
        }

        self.last_status = status
        return status

    def refresh_page_if_needed(self, poll_interval_sec: float) -> bool:
        # /**
        #  * 按轮询节奏周期性刷新页面，避免一直读取旧 DOM 状态
        #  * @param poll_interval_sec 当前轮询间隔（秒）
        #  * @returns bool 本次是否执行了刷新
        #  */
        if not self._page:
            return False
        try:
            now = time.time()
            # 30ms/80ms 这类高频场景不可能每次都 reload，这里做节流
            min_reload_interval = max(0.2, min(1.0, poll_interval_sec * 2))
            if (now - self._last_reload_ts) < min_reload_interval:
                return False

            self._page.reload(wait_until="domcontentloaded", timeout=8000)
            self._last_reload_ts = now
            logger.info("页面已刷新，继续检测邀请码状态")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("页面刷新失败，将继续沿用当前页面: %s", exc)
            return False

    def check_btn_status(self) -> bool:
        # /**
        #  * 双重校验邀请码按钮状态
        #  * @returns bool 是否存在可用邀请码
        #  */
        if not self._page:
            return False
        try:
            status = self.get_invite_status()
            if status.get("ok") is True:
                copy_btn = status.get("copy_btn", {})
                invite_state = status.get("invite_state")
                code_text = (status.get("code_value_text") or "").strip()
                header_text = (status.get("header") or "").strip()
                status_text = (status.get("status_text") or "").strip()
                hints = status.get("hints", {}) or {}

                def _looks_like_real_code(text: str) -> bool:
                    if not text:
                        return False
                    s = text.strip()
                    if not s:
                        return False
                    # 明确排除“无可用码”相关文案
                    bad_tokens = [
                        "下次发放时间",
                        "next release",
                        "已用完",
                        "exhausted",
                        "请稍后",
                        "loading",
                        "邀请码：",
                        "当前邀请码：",
                        "current invite",
                        "today's code",
                        "邀请码",
                        "领取",
                        "可用",
                        "复制",
                        "刷新",
                        "活动",
                        "token",
                    ]
                    low = s.lower()
                    for token in bad_tokens:
                        if token in low:
                            return False
                    # 过滤时间格式
                    if re.search(r"\d{1,2}:\d{2}", s):
                        return False
                    # 真实邀请码通常不会有空格
                    if " " in s:
                        return False
                    # 过滤异常短文本/超长文本
                    if len(s) < 4 or len(s) > 24:
                        return False
                    # 仅允许中文、英文、数字（避免把句子文案当邀请码）
                    if re.search(r"[^0-9A-Za-z\u4e00-\u9fff]", s):
                        return False
                    return True

                # 任何“已用完”迹象都直接判定不可用，避免误触发激活流程
                if bool(hints.get("used_up")):
                    return False
                merged_hint_text = f"{header_text} {status_text}".lower()
                if ("已用完" in merged_hint_text) or ("exhausted" in merged_hint_text):
                    return False

                # 强保守策略：仅当 state 明确 available + 按钮可见可点 + 邀请码文本可信时才触发
                if invite_state == "available":
                    if (
                        bool(copy_btn.get("enabled"))
                        and bool(copy_btn.get("visible"))
                        and _looks_like_real_code(code_text)
                    ):
                        return True
                    return False

                # state 缺失/非 available 时一律不触发自动激活，避免误判
                return False

            # 旧兜底逻辑（避免页面改版后完全失效）
            copy_btn_loc = self._page.locator("button#inviteCopyBtn")
            if copy_btn_loc.count() > 0:
                is_btn_enabled = bool(copy_btn_loc.first.is_enabled())
                is_available = bool(self._page.locator("text=可使用").count() > 0)
                is_taken = bool(self._page.locator("text=此邀请码已被领取完").count() > 0)
                return bool(is_btn_enabled and is_available and not is_taken)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("按钮状态检测失败: %s", exc, exc_info=True)
            return False

    def click_copy_btn(self) -> bool:
        # /**
        #  * 点击复制按钮并从剪贴板读取邀请码
        #  * @returns bool 是否复制成功
        #  */
        if not self._page:
            return False

        def _is_valid_code(text: str) -> bool:
            # /**
            #  * 邀请码基础校验：排除测试文案/空白/中文提示文案
            #  * @param text 候选邀请码
            #  * @returns bool 是否可作为真实邀请码
            #  */
            if not text:
                return False
            normalized = text.strip()
            if not normalized:
                return False
            if normalized == "胡歌最帅":
                return False
            if "下次发放时间" in normalized or "Next release" in normalized:
                return False
            if " " in normalized:
                return False
            return True

        try:
            # 极速路径：如果上一轮状态里已有可信邀请码，直接使用，不再依赖复制按钮/剪贴板
            try:
                st = self.last_status or {}
                st_state = str(st.get("invite_state") or "").strip().lower()
                st_code = str(st.get("code_value_text") or "").strip()
                if st_state == "available" and _is_valid_code(st_code):
                    self.invite_code = st_code
                    try:
                        pyperclip.copy(st_code)
                    except Exception:
                        pass
                    logger.info("已直接使用页面邀请码文本（极速模式）: %s", st_code)
                    return True
            except Exception:
                pass

            prev_clip = pyperclip.paste().strip()
            clicked = False
            for selector in [
                "button#inviteCopyBtn",
                "button#inviteCopyBtnNew",
                "button.invite-copy-btn",
                "div.pill.invite-pill[role='button']",
                "div.pill[role='button'][data-copy-enabled]",
            ]:
                btn = self._page.locator(selector)
                if btn.count() <= 0:
                    continue
                try:
                    # 极短超时点击，保证整体流程控制在秒级
                    btn.first.click(timeout=200)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                logger.info("未找到可点击复制按钮，将直接尝试页面文本提取")
            time.sleep(0.2)
            code = pyperclip.paste().strip()
            # 仅当剪贴板变化且文本校验通过时才视为复制成功
            if code != prev_clip and _is_valid_code(code):
                self.invite_code = code
                logger.info("邀请码复制成功: %s", code)
                return True

            # 无头浏览器下可能无法写入系统剪贴板，兜底从页面文本提取邀请码（全部短超时）
            raw_text = ""
            # 优先复用最近一次状态解析结果，避免元素瞬时消失导致等待
            try:
                st = self.last_status or {}
                raw_text = str(st.get("code_value_text") or "").strip()
            except Exception:  # noqa: BLE001
                raw_text = ""
            try:
                if not raw_text:
                    loc = self._page.locator("#inviteCodeValue")
                    if loc.count() > 0:
                        raw_text = (loc.first.inner_text(timeout=800) or "").strip()
            except Exception:  # noqa: BLE001
                raw_text = ""
            if not raw_text:
                try:
                    loc = self._page.locator("#invite-code")
                    if loc.count() > 0:
                        raw_text = (loc.first.inner_text(timeout=800) or "").strip()
                except Exception:  # noqa: BLE001
                    raw_text = ""

            fallback_code = ""
            if raw_text:
                normalized = raw_text.replace("：", ":").strip()
                if _is_valid_code(normalized):
                    fallback_code = normalized

            if fallback_code:
                self.invite_code = fallback_code
                pyperclip.copy(fallback_code)
                logger.info("剪贴板为空，已使用页面文本兜底提取邀请码: %s", fallback_code)
                return True

            logger.warning(
                "复制失败：剪贴板未更新或内容无效（当前=%s, 之前=%s）",
                code,
                prev_clip,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("点击复制按钮失败: %s", exc, exc_info=True)
            return False

    def close(self) -> None:
        # /**
        #  * 关闭浏览器并释放资源
        #  */
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            # 关闭阶段异常不再抛出
            pass

