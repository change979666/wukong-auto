import os
import sys
from typing import Any, Dict

import yaml


def _runtime_base_dir() -> str:
    # /**
    #  * 获取运行基目录：
    #  * 1) 优先 exe 同目录（便于打包后外置配置）
    #  * 2) 其次 PyInstaller 临时目录
    #  * 3) 最后回退到项目根目录
    #  * @returns str 绝对路径
    #  */
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir:
            return exe_dir
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return meipass
    return os.path.dirname(os.path.dirname(__file__))


def _resolve_path(base_dir: str, path_value: str) -> str:
    # /**
    #  * 将配置中的相对路径解析为绝对路径
    #  * @param base_dir 解析基目录
    #  * @param path_value 配置路径值
    #  * @returns str 绝对路径或原始值
    #  */
    if not path_value:
        return path_value
    if os.path.isabs(path_value):
        return path_value

    # 打包运行时，优先把相对资源路径落到 exe 同级目录，方便用户直接替换文件
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        exe_candidate = os.path.normpath(os.path.join(exe_dir, path_value))
        internal_candidate = os.path.normpath(os.path.join(base_dir, path_value))

        # 若 exe 目录已有资源，优先用 exe 目录
        if os.path.exists(exe_candidate):
            return exe_candidate
        # 对于 img/cookies 这类用户可维护资源，也默认走 exe 目录
        norm = path_value.replace("\\", "/").lower()
        if norm.startswith("./img") or norm.startswith("img/") or norm.endswith("cookies.json"):
            return exe_candidate
        return internal_candidate

    return os.path.normpath(os.path.join(base_dir, path_value))


def load_config() -> Dict[str, Any]:
    # /**
    #  * 加载全局配置文件
    #  * @returns dict 配置字典
    #  */
    base_dir = _runtime_base_dir()
    config_path = os.path.join(base_dir, "config.yaml")
    if not os.path.exists(config_path):
        # 源码运行时回退到项目根目录
        fallback_dir = os.path.dirname(os.path.dirname(__file__))
        config_path = os.path.join(fallback_dir, "config.yaml")
        base_dir = fallback_dir

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 统一解析资源相对路径，避免打包后 cwd 变化导致找不到文件
    web_cfg = cfg.get("web", {})
    input_cfg = cfg.get("input", {})
    if isinstance(web_cfg, dict):
        web_cfg["cookie_path"] = _resolve_path(base_dir, str(web_cfg.get("cookie_path", "")))
    if isinstance(input_cfg, dict):
        input_cfg["input_box_img"] = _resolve_path(base_dir, str(input_cfg.get("input_box_img", "")))
        input_cfg["submit_btn_img"] = _resolve_path(base_dir, str(input_cfg.get("submit_btn_img", "")))
        input_cfg["success_img"] = _resolve_path(base_dir, str(input_cfg.get("success_img", "")))
        input_cfg["failed_img"] = _resolve_path(base_dir, str(input_cfg.get("failed_img", "")))

        # 新增：更稳的兜底方案配置（不给则使用默认值）
        input_cfg.setdefault("uia_enable", True)
        input_cfg.setdefault("coord_enable", True)
        input_cfg.setdefault("tab_count", 8)
        # 窗口相对坐标（0~1），默认留空，推荐通过“定位窗口(校准)”生成
        input_cfg.setdefault("input_box_rel", None)
        input_cfg.setdefault("submit_btn_rel", None)
        # 屏幕绝对坐标（像素），用于手动点位兜底
        input_cfg.setdefault("input_box_abs", None)
        input_cfg.setdefault("submit_btn_abs", None)
    return cfg


def save_config(config: Dict[str, Any]) -> None:
    # /**
    #  * 保存全局配置文件
    #  * @param config 配置字典
    #  */
    base_dir = _runtime_base_dir()
    config_path = os.path.join(base_dir, "config.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

