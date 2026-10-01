# 南苑抢课助手

一个针对**正方教务系统**的选课/抢课工具：Python 核心 + 本地 Web 界面，全程不落盘凭据，支持预检冲突、定时开抢、盲交重试等自动化能力。

> ⚠️ 本工具默认内置**广州南方学院**的教务参数。其他学校（同样是正方教务）可通过一份配置文件接入，见下文「接入你的学校」。

## 功能

- 账号密码登录（密码仅内存走一遍，不落盘、不上传）
- 抢课清单管理：预检时间冲突、满员改盲交、令牌自动刷新
- 定时开抢：对齐服务器时钟，T0 只发一个请求抢占先机
- 课表可视化：已抢 / 已选 / 待选三类清晰标注
- 学业情况查询、退课（按教务规则判断是否可退）
- 后台静默运行（无黑窗）+ 托盘图标 + 日志轮转

## 快速开始

### 方式一：直接跑 exe（Windows，无需 Python）

1. 下载 `南苑抢课助手.exe`；
2. 双击运行（无黑窗，托盘图标提供「打开助手 / 停止服务」）；
3. 浏览器自动打开 `http://127.0.0.1:8720`，输入学号密码登录即可。

日志写在 exe 同目录的 `logs/` 下（可用环境变量 `XK_LOG_DIR` 改位置）。

### 方式二：源码运行（需 Python 3.10+）

```bash
# 1. 安装依赖（建议用 venv）
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 2. 启动（真实教务）
python serve.py --real

# 3. 浏览器打开 http://127.0.0.1:8720
```

启动参数：

| 参数 | 说明 |
|---|---|
| `--real` | 连接真实教务（默认） |
| `--mock` | 连接本地模拟教务（开发用，需 captures/ 抓包存档） |
| `--port 9000` | 换端口 |
| `--lan` | 同时监听局域网（手机可访问，会自动生成访问口令） |
| `--access-key 123456` | 手动指定访问口令 |

## 接入你的学校

本工具基于**正方教务**通用接口，但不同学校的域名、功能码、课表作息可能不同。接入新学校只需两步：

1. 复制 `schools/广州南方学院.json` 为 `schools/你的学校.json`，改 `base_url`、`gnmkdm_xk` 等字段；
2. 启动时指定该配置：

```bash
XK_SCHOOL_PROFILE=schools/你的学校.json python serve.py
```

完整字段说明与「如何抓包获取自己学校的参数」见 **[docs/接入新学校.md](docs/接入新学校.md)**。

## 打包成 exe

```bash
pip install pyinstaller pystray pillow
python gen_icon.py            # 生成图标（首次）
pyinstaller build.spec --noconfirm
# 产物在 dist/南苑抢课助手.exe
```

## 项目结构

```
ui/        前端（纯 HTML/CSS/JS，零构建）
engine/    引擎层（抢课清单、时序、令牌刷新）
core/      核心层（教务接口、配置、课表、冲突判定）
schools/   学校配置文件
tests/     离线自检（smoke_*）+ 真实环境验收（e2e_live）
```

三层架构 `ui → engine → core`，依赖只能自上而下。

## 免责声明

本工具仅供学习与个人方便使用。请遵守所在学校关于选课的规章制度，勿用于扰乱选课秩序、抢占他人资源等不当用途。使用本工具产生的任何后果由使用者自行承担。

## 许可证

[MIT](LICENSE)
