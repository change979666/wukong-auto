import logging
import os
import subprocess
import sys
import threading
import time
import ctypes
from glob import glob
from datetime import datetime
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from modules.alert import Alert
from modules.auto_input import AutoInput
from modules.client_control import ClientControl
from modules.config import load_config, save_config
from modules.web_monitor import WebMonitor

config = load_config()


class LogSignalBus(QObject):
    # /**
    #  * GUI 日志信号总线
    #  */
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    invite_status_signal = pyqtSignal(str)
    client_status_signal = pyqtSignal(str)
    polling_signal = pyqtSignal(str)


signal_bus = LogSignalBus()


def init_logger() -> logging.Logger:
    # /**
    #  * 初始化全局日志记录器
    #  */
    log_dir = "./logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y%m%d')}.log")

    logger = logging.getLogger("wukong")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    class GuiLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = self.format(record)
            signal_bus.log_signal.emit(msg)

    gui_handler = GuiLogHandler()
    gui_handler.setFormatter(formatter)
    logger.addHandler(gui_handler)

    return logger


logger = init_logger()


def get_poll_interval() -> float:
    # /**
    #  * 根据当前时间动态获取轮询间隔
    #  * @returns float 秒
    #  */
    now = datetime.now()
    minute = now.minute
    second = now.second
    before_minute = config["polling"]["before_hour_minute"]

    if minute >= (60 - before_minute) and minute < 60:
        return config["polling"]["before_hour_interval"] / 1000
    if minute == 0 and second <= 30:
        return config["polling"]["hour_interval"] / 1000
    return config["polling"]["normal_interval"]


class Worker(threading.Thread):
    # /**
    #  * 后台监控与激活线程
    #  */

    def __init__(self, client_path: str = "") -> None:
        super().__init__(daemon=True)
        self._stop_flag = threading.Event()
        self._running = False
        self._client_path = client_path.strip()

    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:  # noqa: D401
        # /**
        #  * 线程主循环：网页轮询 + 客户端激活
        #  */
        global logger
        web_monitor = WebMonitor()
        client_control = ClientControl(client_path=self._client_path)
        alert = Alert()
        max_retry = config["polling"]["max_retry"]
        activate_success = False
        last_tried_code = ""
        last_tried_ts = 0.0

        logger.info("===== 悟空邀请码自动抢激活工具启动 =====")
        signal_bus.status_signal.emit("运行中")
        signal_bus.invite_status_signal.emit("监控中，暂未发现可用邀请码")
        signal_bus.client_status_signal.emit("悟空客户端：未启动")

        if not web_monitor.init_browser():
            msg = "浏览器初始化失败，程序退出"
            logger.error(msg)
            alert.send_alert("程序启动失败", msg)
            signal_bus.status_signal.emit("浏览器初始化失败")
            return

        alert.send_alert("程序启动成功", "悟空自动抢码工具已启动，开始监测邀请码状态")

        try:
            while not self._stop_flag.is_set() and not activate_success:
                interval = get_poll_interval()
                signal_bus.polling_signal.emit(f"{int(interval * 1000)} ms/次")
                web_monitor.refresh_page_if_needed(interval)

                # 轮询时同步检测“悟空是否已在运行”（不必等抢到才检测）
                try:
                    (running, _hwnd, title) = client_control.detect_window()
                    if running:
                        signal_bus.client_status_signal.emit(
                            f"悟空客户端：已运行（{title or '已检测到窗口'}）",
                        )
                    else:
                        signal_bus.client_status_signal.emit("悟空客户端：未运行")
                except Exception:
                    signal_bus.client_status_signal.emit("悟空客户端：检测异常")

                # 每轮都解析一次“邀请码区域状态”，用于 GUI 展示
                try:
                    st = web_monitor.get_invite_status()
                    if st.get("ok") is True:
                        invite_state = st.get("invite_state") or "unknown"
                        status_text = st.get("status_text") or ""
                        header = st.get("header") or ""
                        next_release = st.get("next_release")
                        btn = st.get("copy_btn", {})
                        btn_enabled = "可点" if btn.get("enabled") else "不可点"
                        btn_visible = "可见" if btn.get("visible") else "隐藏/不可见"

                        extra = []
                        if next_release:
                            extra.append(f"下次发放：{next_release}")
                        if st.get("hints", {}).get("maybe_need_login"):
                            extra.append("可能需要登录")
                        extra_text = ("；" + "，".join(extra)) if extra else ""

                        signal_bus.invite_status_signal.emit(
                            f"state={invite_state} / {(status_text or header or '未知')}；复制按钮：{btn_enabled}+{btn_visible}{extra_text}",
                        )
                    else:
                        signal_bus.invite_status_signal.emit("当前状态：页面未就绪/解析失败")
                except Exception:
                    signal_bus.invite_status_signal.emit("当前状态：解析异常（将继续轮询）")

                if web_monitor.check_btn_status():
                    signal_bus.invite_status_signal.emit("当前状态：发现可用邀请码")
                    logger.info("检测到可用邀请码，启动自动激活流程")
                    alert.send_alert("检测到可用邀请码", "即将开始自动复制、激活流程")

                    copy_success = False
                    for i in range(max_retry):
                        if web_monitor.click_copy_btn():
                            copy_success = True
                            break
                        logger.warning("复制失败，重试第 %d 次", i + 1)
                        time.sleep(0.1)
                    if not copy_success:
                        alert.send_alert("复制失败", "邀请码复制多次失败，已返回轮询")
                        signal_bus.invite_status_signal.emit("当前状态：复制失败，返回轮询")
                        time.sleep(interval)
                        continue

                    # 避免同一个邀请码在短时间内重复触发激活
                    current_code = (web_monitor.invite_code or "").strip()
                    now_ts = time.time()
                    if current_code and current_code == last_tried_code and (now_ts - last_tried_ts) < 20:
                        logger.info("检测到重复邀请码，20秒内跳过重复激活: %s", current_code)
                        continue

                    # 抢到后：优先复用已运行的悟空客户端；没有则再启动
                    (running, _hwnd, _title) = client_control.detect_window()
                    if not running:
                        if not client_control.launch_client():
                            alert.send_alert("客户端启动失败", "悟空客户端启动失败，无法完成激活")
                            signal_bus.client_status_signal.emit("悟空客户端：启动失败")
                            time.sleep(interval)
                            continue
                        signal_bus.client_status_signal.emit("悟空客户端：已启动，尝试激活窗口")
                    else:
                        signal_bus.client_status_signal.emit("悟空客户端：已运行，尝试激活窗口")

                    if client_control.active_window(wait_after_focus=False):
                        signal_bus.client_status_signal.emit("悟空客户端：窗口已激活，等待输入框")
                    else:
                        signal_bus.client_status_signal.emit("悟空客户端：窗口激活失败")

                    auto_input = AutoInput(invite_code=web_monitor.invite_code)
                    signal_bus.client_status_signal.emit("悟空客户端：尝试识别输入框并输入邀请码")
                    if not auto_input.fast_input_and_submit():
                        alert.send_alert("输入失败", "邀请码输入失败，无法完成激活")
                        signal_bus.client_status_signal.emit("悟空客户端：未找到输入框或输入失败")
                        time.sleep(interval)
                        continue

                    result = auto_input.check_activate_result()
                    last_tried_code = (web_monitor.invite_code or "").strip()
                    last_tried_ts = time.time()
                    if result == "success":
                        activate_success = True
                        alert.send_alert(
                            "激活成功！",
                            f"邀请码【{auto_input.invite_code}】激活成功，工具已停止运行",
                        )
                        logger.info("===== 邀请码激活成功，程序正常结束 =====")
                        signal_bus.status_signal.emit("激活成功，已停止")
                        signal_bus.client_status_signal.emit("悟空客户端：邀请码已激活成功")
                    elif result == "failed":
                        alert.send_alert(
                            "激活失败",
                            f"邀请码【{auto_input.invite_code}】激活失败，已返回轮询",
                        )
                        logger.warning("激活失败，返回轮询状态")
                        signal_bus.client_status_signal.emit("悟空客户端：激活失败，返回轮询")
                    else:
                        alert.send_alert(
                            "激活结果未知",
                            "未检测到明确的激活结果，请手动查看",
                        )
                        logger.warning("激活结果未知，返回轮询状态")
                        signal_bus.client_status_signal.emit("悟空客户端：激活结果未知，返回轮询")
                else:
                    # 已由 get_invite_status() 提供更细状态，这里不再覆盖成“暂无可用”
                    pass

                time.sleep(interval)
        except Exception as exc:  # noqa: BLE001
            logger.error("主程序运行异常: %s", exc, exc_info=True)
            alert.send_alert("程序异常", f"主程序运行异常: {exc}，请检查程序状态")
            signal_bus.status_signal.emit("运行异常")
        finally:
            web_monitor.close()
            logger.info("===== 程序退出 =====")
            if not activate_success:
                signal_bus.status_signal.emit("已停止")


class MainWindow(QMainWindow):
    # /**
    #  * 主界面窗口：集成日志、状态与控制按钮
    #  */

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("悟空邀请码自动抢激活工具")
        self.resize(900, 600)
        self._worker: Optional[Worker] = None
        self._init_ui()
        signal_bus.log_signal.connect(self._append_log)
        signal_bus.status_signal.connect(self._set_status)
        signal_bus.invite_status_signal.connect(self._set_invite_status)
        signal_bus.client_status_signal.connect(self._set_client_status)
        signal_bus.polling_signal.connect(self._set_polling)

    class OptionsDialog(QDialog):
        # /**
        #  * 选项页：配置路径与轮询时间参数
        #  */

        def __init__(self, parent: "MainWindow") -> None:
            super().__init__(parent)
            self.setWindowTitle("选项设置")
            self.resize(560, 260)

            self.path_edit = QLineEdit()
            self.path_edit.setText(config.get("client", {}).get("windows_path", ""))

            self.normal_interval_edit = QLineEdit(str(config.get("polling", {}).get("normal_interval", 2)))
            self.before_minute_edit = QLineEdit(str(config.get("polling", {}).get("before_hour_minute", 5)))
            self.before_interval_edit = QLineEdit(str(config.get("polling", {}).get("before_hour_interval", 200)))
            self.hour_interval_edit = QLineEdit(str(config.get("polling", {}).get("hour_interval", 50)))

            form = QFormLayout()
            form.addRow("悟空路径", self.path_edit)
            form.addRow("常规轮询(秒)", self.normal_interval_edit)
            form.addRow("整点前分钟数", self.before_minute_edit)
            form.addRow("整点前轮询(ms)", self.before_interval_edit)
            form.addRow("整点轮询(ms)", self.hour_interval_edit)

            save_btn = QPushButton("保存")
            save_btn.clicked.connect(self._on_save_clicked)
            cancel_btn = QPushButton("取消")
            cancel_btn.clicked.connect(self.reject)

            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            btn_layout.addWidget(save_btn)
            btn_layout.addWidget(cancel_btn)

            root = QVBoxLayout()
            root.addLayout(form)
            root.addLayout(btn_layout)
            self.setLayout(root)

        def _on_save_clicked(self) -> None:
            try:
                config.setdefault("client", {})["windows_path"] = self.path_edit.text().strip()
                config.setdefault("polling", {})["normal_interval"] = float(self.normal_interval_edit.text().strip())
                config["polling"]["before_hour_minute"] = int(self.before_minute_edit.text().strip())
                config["polling"]["before_hour_interval"] = int(self.before_interval_edit.text().strip())
                config["polling"]["hour_interval"] = int(self.hour_interval_edit.text().strip())
                save_config(config)
                self.accept()
            except Exception:
                QMessageBox.warning(self, "提示", "参数格式不正确，请检查数字输入")

    class FirstRunGuideDialog(QDialog):
        # /**
        #  * 首次向导：指导用户截图并检查关键图片文件
        #  */

        def __init__(self, parent: "MainWindow") -> None:
            super().__init__(parent)
            self.setWindowTitle("首次向导")
            self.resize(680, 420)
            self._init_ui()

        def _img_dir(self) -> str:
            # /**
            #  * 获取 img 目录
            #  * @returns str img 绝对路径
            #  */
            # 打包模式优先使用 exe 同级 img 目录，避免写入 _internal
            if getattr(sys, "frozen", False):
                return os.path.join(os.path.dirname(sys.executable), "img")
            input_path = str(config.get("input", {}).get("input_box_img", ""))
            return os.path.dirname(input_path) if input_path else os.path.join(os.getcwd(), "img")

        def _required_images(self) -> list:
            # /**
            #  * 获取必需截图路径列表
            #  * @returns list 路径列表
            #  */
            input_cfg = config.get("input", {})
            return [
                str(input_cfg.get("input_box_img", "")),
                str(input_cfg.get("submit_btn_img", "")),
            ]

        def _init_ui(self) -> None:
            guide_text = (
                "【首次向导】\n"
                "1. 打开悟空客户端，进入“邀请码输入”页面，窗口大小尽量固定。\n"
                "2. 截图并保存到 img 目录，文件名必须一致：\n"
                "   - input_box.png（输入框）\n"
                "   - submit_btn.png（立即体验/激活按钮）\n"
                "3. 截图建议：100% 缩放、无遮挡、清晰、不裁切过紧。\n"
                "4. 保存后点“检查截图状态”，看到 PASS 即可开始测试。\n"
            )

            self.text = QPlainTextEdit()
            self.text.setReadOnly(True)
            self.text.setPlainText(guide_text)

            self.status = QLabel("状态：未检查")
            self.status.setStyleSheet("color: gray;")

            self.open_btn = QPushButton("打开 img 目录")
            self.open_btn.clicked.connect(self._open_img_dir)
            self.check_btn = QPushButton("检查截图状态")
            self.check_btn.clicked.connect(self._check_images)
            self.close_btn = QPushButton("关闭")
            self.close_btn.clicked.connect(self.accept)

            btn_layout = QHBoxLayout()
            btn_layout.addWidget(self.open_btn)
            btn_layout.addWidget(self.check_btn)
            btn_layout.addStretch()
            btn_layout.addWidget(self.close_btn)

            root = QVBoxLayout()
            root.addWidget(self.text)
            root.addWidget(self.status)
            root.addLayout(btn_layout)
            self.setLayout(root)

        def _open_img_dir(self) -> None:
            # /**
            #  * 打开 img 目录
            #  */
            img_dir = self._img_dir()
            os.makedirs(img_dir, exist_ok=True)
            try:
                os.startfile(img_dir)  # type: ignore[attr-defined]
                self.status.setText(f"状态：已打开目录 {img_dir}")
                self.status.setStyleSheet("color: green;")
            except Exception:
                try:
                    subprocess.Popen(["explorer", img_dir])  # noqa: S603
                    self.status.setText(f"状态：已打开目录 {img_dir}")
                    self.status.setStyleSheet("color: green;")
                except Exception as exc:  # noqa: BLE001
                    self.status.setText("状态：打开 img 目录失败，请检查权限")
                    self.status.setStyleSheet("color: red;")
                    logger.error("打开 img 目录失败: %s", exc, exc_info=True)

        def _check_images(self) -> None:
            # /**
            #  * 检查截图是否齐全
            #  */
            missing = []
            for p in self._required_images():
                if not p or not os.path.exists(p):
                    missing.append(p)

            if not missing:
                self.status.setText("状态：PASS，截图齐全，可以开始测试")
                self.status.setStyleSheet("color: green;")
                logger.info("首次向导检查通过：截图文件齐全")
            else:
                self.status.setText("状态：FAIL，缺少截图，请补齐后再测")
                self.status.setStyleSheet("color: red;")
                logger.warning("首次向导检查失败，缺失截图: %s", " ; ".join(missing))

    class CalibrateDialog(QDialog):
        # /**
        #  * 定位窗口(校准)：用鼠标当前位置生成输入框/按钮的窗口相对坐标
        #  */

        def __init__(self, parent: "MainWindow") -> None:
            super().__init__(parent)
            self.setWindowTitle("定位窗口(校准)")
            self.resize(720, 360)
            self._input_abs = config.get("input", {}).get("input_box_abs")
            self._submit_abs = config.get("input", {}).get("submit_btn_abs")
            self._countdown_timer: Optional[QTimer] = None
            self._countdown_seconds = 0
            self._countdown_target = ""
            self._init_ui()

        def _init_ui(self) -> None:
            tip = (
                "用法：\n"
                "1) 打开悟空客户端到“输入邀请码”页面。\n"
                "2) 点击“记录绝对点位-输入框”，在3秒内把鼠标移到输入框中心。\n"
                "3) 点击“记录绝对点位-提交按钮”，在3秒内把鼠标移到提交按钮中心。\n"
                "4) 点“保存”，会写入 config.yaml 的 input_box_abs / submit_btn_abs。\n"
                "说明：此模式仅使用手动绝对坐标，最直接、最可控。\n"
            )

            self.text = QPlainTextEdit()
            self.text.setReadOnly(True)
            self.text.setPlainText(tip)

            self.status = QLabel("状态：未取点")
            self.status.setStyleSheet("color: gray;")
            self.coord_preview = QLabel("")
            self.coord_preview.setStyleSheet("color: #444;")
            self._refresh_coord_preview()

            self.pick_abs_input_btn = QPushButton("记录绝对点位-输入框")
            self.pick_abs_input_btn.clicked.connect(self._pick_abs_input)
            self.pick_abs_submit_btn = QPushButton("记录绝对点位-提交按钮")
            self.pick_abs_submit_btn.clicked.connect(self._pick_abs_submit)
            self.save_btn = QPushButton("保存")
            self.save_btn.clicked.connect(self._save)
            self.close_btn = QPushButton("关闭")
            self.close_btn.clicked.connect(self.accept)

            btn_layout = QHBoxLayout()
            btn_layout.addWidget(self.pick_abs_input_btn)
            btn_layout.addWidget(self.pick_abs_submit_btn)
            btn_layout.addStretch()
            btn_layout.addWidget(self.save_btn)
            btn_layout.addWidget(self.close_btn)

            root = QVBoxLayout()
            root.addWidget(self.text)
            root.addWidget(self.status)
            root.addWidget(self.coord_preview)
            root.addLayout(btn_layout)
            self.setLayout(root)

        def _refresh_coord_preview(self) -> None:
            self.coord_preview.setText(
                f"当前坐标：input_abs={self._input_abs} submit_abs={self._submit_abs}",
            )

        def _get_mouse_point(self):
            try:
                import pyautogui

                x, y = pyautogui.position()
                return [int(x), int(y)]
            except Exception:
                return None


        def _start_countdown_capture(self, target: str) -> None:
            if self._countdown_timer and self._countdown_timer.isActive():
                self._countdown_timer.stop()
            self._countdown_target = target
            self._countdown_seconds = 3
            self.status.setStyleSheet("color: #b36b00;")
            self.status.setText(f"状态：{self._countdown_seconds} 秒后记录{target}坐标，请移动鼠标")
            self._countdown_timer = QTimer(self)
            self._countdown_timer.timeout.connect(self._on_countdown_tick)
            self._countdown_timer.start(1000)

        def _on_countdown_tick(self) -> None:
            self._countdown_seconds -= 1
            if self._countdown_seconds > 0:
                self.status.setText(f"状态：{self._countdown_seconds} 秒后记录{self._countdown_target}坐标，请移动鼠标")
                return

            if self._countdown_timer:
                self._countdown_timer.stop()
            abs_point = self._get_mouse_point()
            if not abs_point:
                self.status.setText("状态：记录绝对坐标失败")
                self.status.setStyleSheet("color: red;")
                return
            if self._countdown_target == "输入框":
                self._input_abs = abs_point
            else:
                self._submit_abs = abs_point
            self._refresh_coord_preview()
            self.status.setText(f"状态：已记录{self._countdown_target}绝对坐标 {abs_point}")
            self.status.setStyleSheet("color: blue;")

        def _pick_abs_input(self) -> None:
            self._start_countdown_capture("输入框")

        def _pick_abs_submit(self) -> None:
            self._start_countdown_capture("提交按钮")

        def _save(self) -> None:
            has_input = bool(self._input_abs)
            has_submit = bool(self._submit_abs)
            if (not has_input) or (not has_submit):
                QMessageBox.warning(self, "提示", "请先记录输入框与提交按钮的绝对坐标")
                return
            config.setdefault("input", {})["input_box_rel"] = None
            config.setdefault("input", {})["submit_btn_rel"] = None
            config.setdefault("input", {})["input_box_abs"] = self._input_abs
            config.setdefault("input", {})["submit_btn_abs"] = self._submit_abs
            save_config(config)
            QMessageBox.information(self, "成功", "已保存手动绝对坐标到 config.yaml")
            logger.info(
                "已保存手动绝对坐标: input_box_abs=%s submit_btn_abs=%s",
                self._input_abs,
                self._submit_abs,
            )

    def _init_ui(self) -> None:
        status_label = QLabel("运行状态：")
        self.status_value = QLabel("未运行")
        self.status_value.setStyleSheet("color: gray;")

        invite_label = QLabel("邀请码状态：")
        self.invite_status_value = QLabel("未开始监控")
        self.invite_status_value.setStyleSheet("color: gray;")

        polling_label = QLabel("刷新频率：")
        self.polling_value = QLabel("—")
        self.polling_value.setStyleSheet("color: gray;")

        client_label = QLabel("悟空客户端：")
        self.client_status_value = QLabel("未启动")
        self.client_status_value.setStyleSheet("color: gray;")

        self.options_btn = QPushButton("选项设置")
        self.options_btn.clicked.connect(self._on_options_clicked)

        self.start_btn = QPushButton("启动监控")
        self.start_btn.clicked.connect(self._on_start_clicked)
        self.stop_btn = QPushButton("停止监控")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop_clicked)

        self.test_btn = QPushButton("测试输入激活码")
        self.test_btn.clicked.connect(self._on_test_clicked)
        self.check_btn = QPushButton("环境自检")
        self.check_btn.clicked.connect(self._on_check_clicked)

        self.first_guide_btn = QPushButton("首次向导")
        self.first_guide_btn.clicked.connect(self._on_first_guide_clicked)
        self.calibrate_btn = QPushButton("定位窗口")
        self.calibrate_btn.clicked.connect(self._on_calibrate_clicked)

        top_layout = QHBoxLayout()
        left_layout = QVBoxLayout()
        left_layout.addWidget(status_label)
        left_layout.addWidget(self.status_value)
        left_layout.addWidget(invite_label)
        left_layout.addWidget(self.invite_status_value)
        left_layout.addWidget(polling_label)
        left_layout.addWidget(self.polling_value)
        left_layout.addWidget(client_label)
        left_layout.addWidget(self.client_status_value)

        top_layout.addLayout(left_layout)
        top_layout.addStretch()
        top_layout.addWidget(self.options_btn)
        top_layout.addWidget(self.check_btn)
        top_layout.addWidget(self.first_guide_btn)
        top_layout.addWidget(self.calibrate_btn)
        top_layout.addWidget(self.test_btn)
        top_layout.addWidget(self.start_btn)
        top_layout.addWidget(self.stop_btn)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.NoWrap)

        tip_label = QLabel(
            "说明：程序使用 Playwright 内置浏览器在后台无头运行，不会调用系统自带浏览器；\n"
            "请提前配置好 config.yaml、cookies.json 以及 img 目录下的截图。",
        )
        tip_label.setWordWrap(True)
        tip_label.setStyleSheet("color: #555555;")

        main_layout = QVBoxLayout()
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.log_view, stretch=1)
        main_layout.addWidget(tip_label)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def _append_log(self, text: str) -> None:
        self.log_view.append(text)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum(),
        )

    def _set_status(self, text: str) -> None:
        self.status_value.setText(text)
        if "运行" in text:
            self.status_value.setStyleSheet("color: green;")
        elif "成功" in text:
            self.status_value.setStyleSheet("color: blue;")
        elif "失败" in text or "异常" in text:
            self.status_value.setStyleSheet("color: red;")
        else:
            self.status_value.setStyleSheet("color: gray;")

    def _set_invite_status(self, text: str) -> None:
        self.invite_status_value.setText(text)

    def _set_client_status(self, text: str) -> None:
        self.client_status_value.setText(text)

    def _set_polling(self, text: str) -> None:
        self.polling_value.setText(text)

    def _on_start_clicked(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = Worker(client_path=config.get("client", {}).get("windows_path", ""))
        self._worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _on_stop_clicked(self) -> None:
        if self._worker:
            self._worker.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_status("已停止")

    def _on_test_clicked(self) -> None:
        # /**
        #  * 测试自动输入与提交（固定内容：胡歌最帅）
        #  * 逻辑：检测/启动悟空 -> 激活窗口 -> 定位输入框 -> 粘贴 -> 点击提交
        #  */
        def _run() -> None:
            try:
                signal_bus.client_status_signal.emit("测试：准备检测悟空窗口")
                cc = ClientControl(client_path=config.get("client", {}).get("windows_path", ""))
                (running, _hwnd, _title) = cc.detect_window()
                if not running:
                    signal_bus.client_status_signal.emit("测试：未检测到悟空，尝试启动")
                    if not cc.launch_client():
                        signal_bus.client_status_signal.emit("测试：启动失败")
                        return
                else:
                    signal_bus.client_status_signal.emit("测试：已检测到悟空，尝试激活窗口")

                if not cc.active_window(wait_after_focus=False):
                    signal_bus.client_status_signal.emit("测试：窗口激活失败，继续尝试前台窗口兜底")

                signal_bus.client_status_signal.emit("测试：窗口已激活，准备输入“胡歌最帅”")
                ai = AutoInput(invite_code="胡歌最帅")
                if not ai.fast_input_and_submit():
                    signal_bus.client_status_signal.emit("测试：未找到输入框/输入失败")
                    return
                signal_bus.client_status_signal.emit("测试：已提交（点击按钮/回车兜底）")
            except Exception:  # noqa: BLE001
                signal_bus.client_status_signal.emit("测试：执行异常")

        threading.Thread(target=_run, daemon=True).start()

    def _on_options_clicked(self) -> None:
        # /**
        #  * 打开选项页并保存设置
        #  */
        dialog = self.OptionsDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            QMessageBox.information(self, "成功", "设置已保存")
            signal_bus.client_status_signal.emit("悟空客户端：设置已更新")

    def _on_first_guide_clicked(self) -> None:
        # /**
        #  * 打开首次向导窗口
        #  */
        dialog = self.FirstRunGuideDialog(self)
        dialog.exec_()

    def _on_calibrate_clicked(self) -> None:
        # /**
        #  * 打开定位窗口(校准)窗口
        #  */
        dialog = self.CalibrateDialog(self)
        dialog.exec_()

    def _on_check_clicked(self) -> None:
        # /**
        #  * 环境自检：检查关键文件、路径与 Playwright 浏览器内核
        #  */
        def _check() -> None:
            try:
                base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()

                checks = []
                cfg_path = os.path.join(base_dir, "config.yaml")
                cfg_path_internal = os.path.join(base_dir, "_internal", "config.yaml")
                checks.append((f"config.yaml({cfg_path} 或 {cfg_path_internal})", os.path.exists(cfg_path) or os.path.exists(cfg_path_internal), True))

                cookie_path = config.get("web", {}).get("cookie_path", "")
                checks.append((f"[OPTIONAL] cookies.json({cookie_path})", os.path.exists(cookie_path), False))

                input_cfg = config.get("input", {})
                has_abs_input = isinstance(input_cfg.get("input_box_abs"), (list, tuple)) and len(input_cfg.get("input_box_abs")) == 2
                has_abs_submit = isinstance(input_cfg.get("submit_btn_abs"), (list, tuple)) and len(input_cfg.get("submit_btn_abs")) == 2

                for key in ["input_box_img", "submit_btn_img"]:
                    p = str(input_cfg.get(key, ""))
                    # 已配置手动绝对坐标时，截图不再是必需项
                    required = not (has_abs_input and has_abs_submit)
                    tag = "" if required else "[OPTIONAL] "
                    checks.append((f"{tag}{key}({p})", os.path.exists(p), required))

                # success/failed 图允许缺失，仅提示
                for key in ["success_img", "failed_img"]:
                    p = str(input_cfg.get(key, ""))
                    checks.append((f"[OPTIONAL] {key}({p})", os.path.exists(p), False))

                client_path = config.get("client", {}).get("windows_path", "")
                checks.append((f"悟空路径({client_path})", os.path.exists(client_path), True))

                # Playwright 内核检查（兼容不同版本目录名）
                local_app = os.environ.get("LOCALAPPDATA", "")
                browser_globs = [
                    os.path.join(base_dir, "_internal", "playwright", "driver", "package", ".local-browsers", "chromium-*", "chrome-win", "chrome.exe"),
                    os.path.join(base_dir, "_internal", "playwright", "driver", "package", ".local-browsers", "chromium_headless_shell-*", "chrome-win", "headless_shell.exe"),
                    os.path.join(base_dir, "pw-browsers", "chromium-*", "chrome-win", "chrome.exe"),
                    os.path.join(base_dir, "_internal", "pw-browsers", "chromium-*", "chrome-win", "chrome.exe"),
                    os.path.join(local_app, "ms-playwright", "chromium-*", "chrome-win", "chrome.exe") if local_app else "",
                    os.path.join(local_app, "ms-playwright", "chromium_headless_shell-*", "chrome-win", "headless_shell.exe") if local_app else "",
                ]
                found_browser = False
                found_path = ""
                for pattern in browser_globs:
                    if not pattern:
                        continue
                    matches = glob(pattern)
                    if matches:
                        found_browser = True
                        found_path = matches[0]
                        break
                checks.append((f"playwright-browser({found_path or '未发现可用内核'})", found_browser, True))

                fail_count = 0
                logger.info("===== 环境自检开始 =====")
                for item in checks:
                    if len(item) == 2:
                        (name, ok) = item
                        required = True
                    else:
                        (name, ok, required) = item
                    if ok:
                        logger.info("[PASS] %s", name)
                    else:
                        if required:
                            logger.warning("[FAIL] %s", name)
                            fail_count += 1
                        else:
                            logger.info("[INFO] %s 缺失（可选，不影响核心流程）", name)
                logger.info("===== 环境自检结束，失败项: %d =====", fail_count)

                if fail_count == 0:
                    signal_bus.client_status_signal.emit("环境自检：通过")
                else:
                    signal_bus.client_status_signal.emit(f"环境自检：有 {fail_count} 项失败")
            except Exception as exc:  # noqa: BLE001
                logger.error("环境自检异常: %s", exc, exc_info=True)
                signal_bus.client_status_signal.emit("环境自检：执行异常")

        threading.Thread(target=_check, daemon=True).start()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker:
            self._worker.stop()
        event.accept()


def main() -> None:
    # /**
    #  * GUI 入口函数
    #  */
    # Qt 要求：AA_EnableHighDpiScaling 必须在 QApplication 创建前设置
    # Windows 下启用进程 DPI 感知，避免坐标点击与窗口坐标出现缩放偏差
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

