# wukong-auto（悟空邀请码自动抢激活工具）

A Python/Playwright desktop automation project for monitoring the official DingTalk Wukong invitation page and assisting with invite-code activation, with configurable settings, logs, retries, and alerts.

钉钉悟空 AI 工作平台内测资格采用**整点限量发放邀请码**的模式。本工具实现邀请码全流程自动化：**监测 → 抢码 → 校验 → 自动激活**，最大化提升抢码成功率。

## 核心能力

1. **7×24 小时实时监测**悟空邀请码页面，整点时段自动切换高频轮询，第一时间捕获邀请码发放状态
2. 检测到有效邀请码时，自动触发官方复制按钮，完成邀请码剪贴板获取与有效性校验
3. 自动启动悟空客户端，定位邀请码输入界面，完成**邀请码自动输入与激活**
4. 全流程日志记录与实时告警（支持钉钉机器人 / Server酱），异常场景自动重试与容错

## 适用范围

- 适用平台：Windows、macOS 主流操作系统
- 适用场景：钉钉悟空官方邀请码页面监测、PC 客户端自动激活
- 合规说明：本工具仅模拟用户正常手动操作，不篡改官方源码、不破解接口、不恶意请求服务器，严格遵守平台用户协议

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制配置示例并修改为本机环境
copy config.example.yaml config.yaml

# 3. 运行
python main.py
```

## 配置说明

`config.yaml` 包含**本机绝对路径**（客户端路径、界面截图路径、Cookie 路径），**不要提交真实配置**：

- 参考 `config.example.yaml` 模板，填入：
  - `client.windows_path`：悟空客户端 exe 绝对路径
  - `input.*_img`：邀请码输入框 / 提交按钮 / 成功 / 失败 四张界面截图路径（可用 `img/` 目录下的参考图）
  - `polling`：常规轮询 1s、整点前 2 分钟进入高频轮询（80ms）、整点 30ms 轮询
- 首次运行需先通过浏览器登录悟空页面，登录态 Cookie 保存在 `cookies.json`（自动生成）

## 项目结构

```
├── main.py                  # 主程序入口
├── config.example.yaml      # 配置示例（真实 config.yaml 不提交）
├── modules/                 # 核心模块（页面监测、客户端控制、UI 自动化）
├── img/                     # 界面元素参考截图
├── 项目规划.md              # 完整项目文档
└── requirements.txt         # 依赖
```

## 许可证

MIT License
