import logging
import os
import subprocess
import time
from typing import Optional, Tuple, Union

import win32con
import win32gui
import win32process
import win32api

from modules.config import load_config

config = load_config()
logger = logging.getLogger("wukong.client_control")


class ClientControl:
    # /**
    #  * 悟空客户端控制模块
    #  * 负责启动客户端并将窗口置前
    #  */

    def __init__(self, client_path: Optional[str] = None) -> None:
        # 实例化时刷新配置，避免运行中修改配置后仍使用旧值
        global config
        config = load_config()
        self._process: Optional[subprocess.Popen] = None
        self._client_path_override = (client_path or "").strip()

    def detect_window(self) -> Tuple[bool, Optional[int], str]:
        # /**
        #  * 检测悟空窗口是否已运行
        #  * @returns (bool, hwnd, title) 是否存在、窗口句柄、窗口标题
        #  */
        hwnd = self._find_wukong_hwnd()
        if not hwnd:
            return (False, None, "")
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            return (True, hwnd, title)
        except Exception:  # noqa: BLE001
            return (True, hwnd, "")

    def _get_process_exe_by_hwnd(self, hwnd: int) -> str:
        # /**
        #  * 通过窗口句柄获取进程可执行文件路径（用于避免把浏览器标签页误认为客户端）
        #  * @returns str exe 路径（失败则空串）
        #  */
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return ""
            h_process = win32api.OpenProcess(0x0410, False, pid)  # QUERY_LIMITED_INFORMATION|VM_READ
            try:
                exe = win32process.GetModuleFileNameEx(h_process, 0)  # type: ignore[arg-type]
                return exe or ""
            finally:
                win32api.CloseHandle(h_process)
        except Exception:  # noqa: BLE001
            return ""

    def launch_client(self) -> bool:
        # /**
        #  * 启动悟空客户端（仅实现 Windows）
        #  * @returns bool 是否启动成功
        #  */
        exe_path = self._client_path_override or config["client"]["windows_path"]
        if not os.path.exists(exe_path):
            logger.error("悟空客户端路径不存在: %s", exe_path)
            return False
        try:
            self._process = subprocess.Popen(exe_path)  # noqa: S603
            logger.info("已启动悟空客户端进程")
            time.sleep(config["client"]["launch_timeout"])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("启动悟空客户端失败: %s", exc, exc_info=True)
            return False

    def launch_multiple(self, count: int = 2) -> int:
        # /**
        #  * 尝试多开客户端
        #  * @param count 启动次数
        #  * @returns int 实际启动成功次数
        #  */
        success = 0
        for _ in range(max(1, count)):
            if self.launch_client():
                success += 1
                time.sleep(0.3)
        return success

    def _find_wukong_hwnd(self) -> Optional[int]:
        # /**
        #  * 查找悟空窗口句柄（优先用进程路径匹配，避免误判浏览器窗口）
        #  */
        target_hwnd: Optional[int] = None
        target_area: int = -1
        cfg_path = self._client_path_override or (config.get("client", {}).get("windows_path") or "").strip()
        cfg_path_norm = os.path.normcase(os.path.normpath(cfg_path)) if cfg_path else ""

        def _enum_handler(hwnd: int, _param: object) -> None:
            nonlocal target_hwnd, target_area
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = max(0, right - left)
                height = max(0, bottom - top)
            except Exception:  # noqa: BLE001
                return
            # 过滤掉托盘/提示/悬浮等小窗口，避免点到错误句柄（常见 16x16）
            if width < 300 or height < 300:
                return
            area = width * height
            exe = self._get_process_exe_by_hwnd(hwnd)
            exe_norm = os.path.normcase(os.path.normpath(exe)) if exe else ""

            # 1) 如果配置了客户端路径：严格按 exe 路径匹配
            if cfg_path_norm and exe_norm:
                if exe_norm == cfg_path_norm:
                    if area > target_area:
                        target_hwnd = hwnd
                        target_area = area
                return

            # 2) 未配置或取不到 exe：兜底按标题，但排除常见浏览器进程
            if exe_norm and (exe_norm.endswith("\\chrome.exe") or exe_norm.endswith("\\msedge.exe")):
                return
            if ("悟空" in title) or ("Wukong" in title) or ("钉钉空" in title) or ("com.dingtalk.real" in title.lower()):
                if area > target_area:
                    target_hwnd = hwnd
                    target_area = area

        win32gui.EnumWindows(_enum_handler, None)
        return target_hwnd

    def active_window(self, wait_after_focus: bool = True) -> bool:
        # /**
        #  * 将悟空客户端窗口置为前台
        #  * @returns bool 是否置前成功
        #  */
        hwnd = self._find_wukong_hwnd()
        if not hwnd:
            # 兜底：如果前台窗口标题符合悟空特征，也允许直接使用
            try:
                fg = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(fg) or ""
                if ("悟空" in title) or ("Wukong" in title) or ("com.dingtalk.real" in title.lower()):
                    hwnd = fg
            except Exception:  # noqa: BLE001
                hwnd = None
        if not hwnd:
            logger.error("未找到悟空客户端窗口")
            return False

        def _try_focus_once(target_hwnd: int) -> bool:
            try:
                win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
                win32gui.BringWindowToTop(target_hwnd)
                win32gui.SetForegroundWindow(target_hwnd)
                fg_now = win32gui.GetForegroundWindow()
                return int(fg_now) == int(target_hwnd)
            except Exception:
                return False

        try:
            # 先直接尝试一次
            if _try_focus_once(hwnd):
                logger.info("悟空客户端窗口已置前")
                if wait_after_focus:
                    time.sleep(config["client"]["page_load_wait"])
                return True

            # 再尝试线程输入附加 + TOPMOST 闪切兜底
            fg = win32gui.GetForegroundWindow()
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            current_tid = win32api.GetCurrentThreadId()

            attached_fg = False
            attached_cur = False
            try:
                if fg_tid and target_tid and fg_tid != target_tid:
                    win32process.AttachThreadInput(fg_tid, target_tid, True)
                    attached_fg = True
                if current_tid and target_tid and current_tid != target_tid:
                    win32process.AttachThreadInput(current_tid, target_tid, True)
                    attached_cur = True

                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )

                for _ in range(3):
                    if _try_focus_once(hwnd):
                        logger.info("悟空客户端窗口已置前")
                        if wait_after_focus:
                            time.sleep(config["client"]["page_load_wait"])
                        return True
                    time.sleep(0.1)
            finally:
                try:
                    if attached_fg and fg_tid and target_tid:
                        win32process.AttachThreadInput(fg_tid, target_tid, False)
                except Exception:
                    pass
                try:
                    if attached_cur and current_tid and target_tid:
                        win32process.AttachThreadInput(current_tid, target_tid, False)
                except Exception:
                    pass

            logger.error("激活悟空窗口失败: 多次尝试后仍无法置前")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("激活悟空窗口失败: %s", exc, exc_info=True)
            return False

    def get_window_rect(self, hwnd: Union[int, None] = None) -> Optional[Tuple[int, int, int, int]]:
        # /**
        #  * 获取悟空窗口位置与尺寸
        #  * @param hwnd 可选：指定窗口句柄
        #  * @returns (left, top, right, bottom) 或 None
        #  */
        try:
            target = hwnd or self._find_wukong_hwnd()
            if not target:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(target)
            return (left, top, right, bottom)
        except Exception:  # noqa: BLE001
            return None

