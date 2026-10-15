import logging
import os
import time
from typing import Literal, Optional

import pyautogui
import pyperclip

from modules.config import load_config

config = load_config()
logger = logging.getLogger("wukong.auto_input")


class AutoInput:
    # /**
    #  * 自动输入与激活模块
    #  * 负责邀请码粘贴、点击激活与结果识别
    #  */

    def __init__(self, invite_code: Optional[str]) -> None:
        # 每次实例化都重新加载一次配置，确保“选项/校准”保存后立即生效
        global config
        config = load_config()
        self.invite_code = invite_code

    def _uia_input_and_submit(self) -> bool:
        # /**
        #  * 方案A：Windows UIA 控件自动化（不依赖截图/分辨率/缩放）
        #  * 需要窗口已在前台
        #  * @returns bool 是否执行成功
        #  */
        if not config.get("input", {}).get("uia_enable", True):
            return False
        try:
            from pywinauto import Application  # type: ignore
            from pywinauto.findwindows import ElementNotFoundError  # type: ignore
        except Exception:
            return False

        try:
            from modules.client_control import ClientControl

            cc = ClientControl(client_path=config.get("client", {}).get("windows_path", ""))
            (running, hwnd, _title) = cc.detect_window()
            if (not running) or (not hwnd):
                return False

            app = Application(backend="uia").connect(handle=hwnd)
            win = app.window(handle=hwnd)

            # 1) 找输入框（尽量宽松）
            edits = win.descendants(control_type="Edit")
            if not edits:
                return False
            edit = edits[0]
            try:
                edit.set_edit_text(self.invite_code or "")
            except Exception:
                # 兜底：点一下再发 Ctrl+V
                edit.click_input()
                pyperclip.copy(self.invite_code or "")
                pyautogui.hotkey("ctrl", "v")

            # 2) 找提交按钮（常见文案兜底）
            for name in ["立即体验", "激活", "提交", "确认", "确定"]:
                try:
                    btn = win.child_window(title=name, control_type="Button")
                    if btn.exists(timeout=0.2):
                        btn.click_input()
                        return True
                except ElementNotFoundError:
                    continue
                except Exception:
                    continue

            # 3) 按回车兜底提交
            pyautogui.press("enter")
            return True
        except Exception:
            return False

    def _coord_click(self, rel: Optional[object]) -> bool:
        # /**
        #  * 方案B：窗口相对坐标点击（需要先“定位窗口(校准)”生成坐标）
        #  * @param rel [xRel,yRel]，0~1
        #  * @returns bool 是否点击成功
        #  */
        if not config.get("input", {}).get("coord_enable", True):
            return False
        if not isinstance(rel, (list, tuple)) or len(rel) != 2:
            return False
        try:
            x_rel = float(rel[0])
            y_rel = float(rel[1])
        except Exception:
            return False

    def _abs_click(self, point: Optional[object], tag: str = "point") -> bool:
        # /**
        #  * 方案C：屏幕绝对坐标点击（手动点位兜底）
        #  * @param point [x,y]
        #  * @returns bool 是否点击成功
        #  */
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        try:
            x = int(float(point[0]))
            y = int(float(point[1]))
            if x <= 0 or y <= 0:
                return False
            pyautogui.click(x, y)
            time.sleep(0.05)
            logger.info("绝对坐标点击%s: (%d, %d)", tag, x, y)
            return True
        except Exception:
            return False
        if x_rel <= 0 or y_rel <= 0:
            return False
        def _is_valid_rect(rect_obj: Optional[tuple]) -> bool:
            if not rect_obj:
                return False
            try:
                l, t, r, b = rect_obj
                return (r - l) >= 300 and (b - t) >= 300
            except Exception:
                return False

        try:
            from modules.client_control import ClientControl

            cc = ClientControl(client_path=config.get("client", {}).get("windows_path", ""))
            (running, hwnd, _title) = cc.detect_window()
            rect = None
            # 优先使用当前前台悟空窗口，避免多窗口时点到错误窗口
            try:
                import win32gui

                fg = win32gui.GetForegroundWindow()
                fg_title = (win32gui.GetWindowText(fg) or "").lower()
                if fg and (("悟空" in fg_title) or ("wukong" in fg_title) or ("com.dingtalk.real" in fg_title)):
                    rect = cc.get_window_rect(fg)
            except Exception:  # noqa: BLE001
                rect = None

            if (not _is_valid_rect(rect)) and running and hwnd:
                rect = cc.get_window_rect(hwnd)
            if not _is_valid_rect(rect):
                logger.warning("坐标点击失败：未获取到有效主窗口矩形 rect=%s", rect)
                return False
            left, top, right, bottom = rect
            w = max(1, right - left)
            h = max(1, bottom - top)
            x = int(left + w * x_rel)
            y = int(top + h * y_rel)
            # 增加微小偏移重试，降低边框/阴影/布局抖动导致的失准
            for dx, dy in [(0, 0), (0, -6), (0, 6), (-6, 0), (6, 0)]:
                pyautogui.click(x + dx, y + dy)
                time.sleep(0.03)
            logger.info("坐标点击输入点: rel=(%.4f, %.4f), abs=(%d, %d), rect=%s", x_rel, y_rel, x, y, rect)
            time.sleep(0.05)
            return True
        except Exception:
            return False

    def _focus_input_box(self) -> bool:
        # /**
        #  * 通过图像识别聚焦输入框
        #  * @returns bool 是否定位成功
        #  */
        img_path = config["input"]["input_box_img"]
        if not os.path.exists(img_path):
            logger.error("输入框截图文件不存在: %s（请创建 img 目录并放入 input_box.png）", img_path)
            return False
        try:
            location = None
            # 多档置信度 + 灰度识别，提升跨机器/缩放场景的匹配成功率
            for conf in [config["input"]["confidence"], 0.75, 0.65, 0.55]:
                try:
                    location = pyautogui.locateCenterOnScreen(
                        img_path,
                        confidence=float(conf),
                        grayscale=True,
                    )
                except Exception:  # noqa: BLE001
                    location = None
                if location is not None:
                    break
            if location is None:
                logger.warning("未识别到输入框截图: %s", img_path)
                return False
            pyautogui.click(location.x, location.y)
            time.sleep(min(0.2, float(config["input"]["action_interval"])))
            return True
        except pyautogui.ImageNotFoundException:
            logger.warning("未匹配到输入框截图（将走 Tab 兜底）: %s", img_path)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("定位输入框失败: %s", exc, exc_info=True)
            return False

    def _tab_fallback_focus(self, tab_count: int = 8) -> bool:
        # /**
        #  * 兜底方案：不依赖截图，尝试 Tab 切换聚焦到输入框
        #  * @param tab_count 最大 Tab 次数
        #  * @returns bool 是否执行了兜底聚焦（不保证一定命中输入框，但可用于“测试链路”）
        #  */
        try:
            w, h = pyautogui.size()
            # 先在窗口中部点一下，确保有焦点
            pyautogui.click(int(w * 0.65), int(h * 0.6))
            time.sleep(0.2)
            for _ in range(tab_count):
                pyautogui.press("tab")
                time.sleep(0.08)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Tab 兜底聚焦失败: %s", exc, exc_info=True)
            return False

    def input_invite_code(self) -> bool:
        # /**
        #  * 将邀请码粘贴到输入框
        #  * @returns bool 是否输入成功
        #  */
        if not self.invite_code:
            logger.error("暂无邀请码，无法输入")
            return False
        focused = self._focus_input_box()
        if not focused:
            logger.warning("将尝试 Tab 兜底定位输入框")
            if not self._tab_fallback_focus():
                return False
        try:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            pyautogui.press("backspace")
            time.sleep(0.05)
            pyperclip.copy(self.invite_code)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(config["input"]["action_interval"])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("输入邀请码失败: %s", exc, exc_info=True)
            return False

    def fast_input_and_submit(self) -> bool:
        # /**
        #  * 极速输入提交流程：先尝试直接粘贴+回车，失败再走图像识别流程
        #  * @returns bool 是否完成提交动作
        #  */
        if not self.invite_code:
            logger.error("暂无邀请码，无法输入")
            return False

        # 先尝试方案A：UIA 控件自动化（最稳）
        if self._uia_input_and_submit():
            return True

        # 再尝试方案B：手动绝对坐标（当前机器下最直接）
        abs_input = config.get("input", {}).get("input_box_abs")
        abs_submit = config.get("input", {}).get("submit_btn_abs")
        if self._abs_click(abs_input, "输入框"):
            try:
                pyperclip.copy(self.invite_code)
                pyautogui.hotkey("ctrl", "a")
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
                if self._abs_click(abs_submit, "提交按钮"):
                    return True
                pyautogui.press("enter")
                return True
            except Exception:
                pass

        # 再尝试方案C：窗口相对坐标（稳定、速度快）
        rel_input = config.get("input", {}).get("input_box_rel")
        rel_submit = config.get("input", {}).get("submit_btn_rel")
        if self._coord_click(rel_input):
            try:
                pyperclip.copy(self.invite_code)
                pyautogui.hotkey("ctrl", "a")
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
                if self._coord_click(rel_submit):
                    return True
                pyautogui.press("enter")
                return True
            except Exception:
                pass

        try:
            # 先尝试快速聚焦输入区域，减少“没反应”的概率
            if not self._focus_input_box():
                self._tab_fallback_focus(tab_count=int(config.get("input", {}).get("tab_count", 8)))
            pyperclip.copy(self.invite_code)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "v")
            pyautogui.press("enter")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("极速提交流程失败，将回退到标准流程: %s", exc)

        if not self.input_invite_code():
            return False
        return self.click_submit_btn()

    def click_submit_btn(self) -> bool:
        # /**
        #  * 点击激活按钮
        #  * @returns bool 是否点击成功
        #  */
        img_path = config["input"]["submit_btn_img"]
        if not os.path.exists(img_path):
            logger.warning("激活按钮截图不存在: %s，将使用回车兜底提交", img_path)
            pyautogui.press("enter")
            return True
        try:
            location = None
            for conf in [config["input"]["confidence"], 0.75, 0.65, 0.55]:
                try:
                    location = pyautogui.locateCenterOnScreen(
                        img_path,
                        confidence=float(conf),
                        grayscale=True,
                    )
                except Exception:  # noqa: BLE001
                    location = None
                if location is not None:
                    break
            if location is None:
                logger.warning("未识别到激活按钮截图: %s", img_path)
                pyautogui.press("enter")
                return True
            pyautogui.click(location.x, location.y)
            time.sleep(min(0.2, float(config["input"]["action_interval"])))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("点击激活按钮失败: %s", exc, exc_info=True)
            return False

    def check_activate_result(self) -> Literal["success", "failed", "unknown"]:
        # /**
        #  * 通过截图识别激活结果
        #  * @returns str 激活结果：success/failed/unknown
        #  */
        success_img = config["input"]["success_img"]
        failed_img = config["input"]["failed_img"]

        def _input_box_visible() -> Optional[bool]:
            # /**
            #  * 检查输入框是否仍可见（用于无结果图时的启发式判定）
            #  * @returns Optional[bool] True=可见，False=不可见，None=无法判断
            #  */
            input_img = config["input"]["input_box_img"]
            if not os.path.exists(input_img):
                return None
            try:
                for conf in [config["input"]["confidence"], 0.75, 0.65, 0.55]:
                    try:
                        loc = pyautogui.locateOnScreen(
                            input_img,
                            confidence=float(conf),
                            grayscale=True,
                        )
                    except Exception:  # noqa: BLE001
                        loc = None
                    if loc is not None:
                        return True
                return False
            except Exception:  # noqa: BLE001
                return None

        try:
            # 优先使用 success/failed 图（如果提供）
            if os.path.exists(success_img) and os.path.exists(failed_img):
                if pyautogui.locateOnScreen(
                    success_img,
                    confidence=config["input"]["confidence"],
                ):
                    logger.info("检测到激活成功提示")
                    return "success"
                if pyautogui.locateOnScreen(
                    failed_img,
                    confidence=config["input"]["confidence"],
                ):
                    logger.warning("检测到激活失败提示")
                    return "failed"

            # 无结果图模式：按输入框可见性启发式判断
            visible = _input_box_visible()
            if visible is True:
                logger.warning("启发式判定：输入框仍可见，视为未成功")
                return "failed"
            if visible is False:
                logger.info("启发式判定：输入框不可见，视为成功")
                return "success"

            logger.warning("未检测到明确的激活结果")
            return "unknown"
        except Exception as exc:  # noqa: BLE001
            logger.error("检测激活结果失败: %s", exc, exc_info=True)
            return "unknown"

