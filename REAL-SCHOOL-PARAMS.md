# 真实教务参数快照（切回真实环境必读）

> **用途**：本地模拟教务（`--mock`）与真实教务之间来回切换时，这份文档是**唯一权威对照**。
> 代码万一被改坏，照着它就能恢复。
>
> **权威定义处**：`core/config.py` 的 `DEFAULT_SCHOOL`（第 185-233 行）+ 各路径常量（第 14-32 行）。
> 本文件只是快照，**改参数请改 `core/config.py`，再回来同步这份**。
>
> **最后核验**：2026-09-30

---

## 1. 学校基本信息

| 项 | 值 |
|---|---|
| 学校名 | 广州南方学院 |
| 教务系统 | 正方教务（zfsoft） |
| 基址 `base_url` | `https://jwxt.nfu.edu.cn/jwglxt/` |
| 公网可达 | ✅ 直连，**不需要 WebVPN** |
| 功能码 `gnmkdm` | `N253512`（自主选课） |
| `layout` | **必需** `layout=default`（不带时索引页隐藏域从 23 个掉到 1 个） |

## 2. Cookie

| 项 | 值 | 说明 |
|---|---|---|
| 主 Cookie | `JSESSIONID` | **HttpOnly** → `document.cookie` 读不到，必须用 CDP `Network.getCookies` |
| 附加 Cookie | `route` | 负载均衡路由，必须与 JSESSIONID 一起带 |
| Cookie 头示例 | `JSESSIONID=xxx; route=yyy` | 整个字符串可直接放进请求头 |

**铁律 #9：凭据不落盘**，只在内存持有。

## 3. 接口路径（全部为 `base_url` 下的相对路径）

| 用途 | 路径常量 | 路径值 |
|---|---|---|
| 登录页 | `PATH_LOGIN_PAGE` | `xtgl/login_slogin.html` |
| 公钥 | `PATH_PUBLIC_KEY` | `xtgl/login_getPublicKey.html` |
| 首页菜单 | `PATH_INDEX_MENU` | `xtgl/index_initMenu.html` |
| **选课入口（Index）** | `PATH_XK_INDEX` | `xsxk/zzxkyzb_cxZzxkYzbIndex.html` |
| **切 Tab 重载（Display）** | `PATH_XK_DISPLAY` | `xsxk/zzxkyzb_cxZzxkYzbDisplay.html` |
| 课程列表 | `PATH_COURSE_LIST` | `xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html` |
| 教学班详情 | `PATH_CLASS_INFO` | `xsxk/zzxkyzbjk_cxJxbWithKchZzxkYzb.html` |
| **提交选课** | `PATH_SUBMIT` | `xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html` |
| **已选课程** | `PATH_SELECTED` | `xsxk/zzxkyzb_cxZzxkYzbChoosedDisplay.html` |
| 冲突预检 | `PATH_CONFLICT` | `xsxk/zzxkyzb_cxCtKcZyZzxkYzb.html` |
| **退课** | `PATH_CANCEL` | `xsxk/zzxkyzb_tuikBcZzxkYzb.html` |

URL 拼接规则（`core/config.py::SchoolProfile.url()`）：
- `with_gnmkdm=True` → 自动附加 `?gnmkdm=N253512`
- `with_layout=True` → 再附加 `&layout=default`

## 4. 动态商品字段数量（只做「有没有漏抓」的粗校验）

| 集合 | 数量 | 常量名 |
|---|---|---|
| 提交选课字段 | **19** | `SUBMIT_FIELDS` |
| 课程查询字段 | **45** | `QUERY_FIELDS` |
| 教学班查询字段 | **46** | `CLASS_FIELDS` |
| Display 第二步字段 | **29** | `DISPLAY_FIELDS` |

⚠️ **这些字段全部动态抓取，一个都不能写死**（铁律 #1）。
真实环境实测：Index 页可抽到 **131 个隐藏域 / 6 个课程 Tab**。

## 5. 作息表（节次 → 时间）

⚠️ 编号 **1-15 连续**，但**相邻节次之间的间隔并不均匀** —— 午休夹在 5 和 6 之间
（5 节 12:00 下课、6 节 12:50 才上，中间 50 分钟）。所以只能查表，
**绝不能按节次递增去算时间**。

| 节 | 时间 | 节 | 时间 | 节 | 时间 |
|---|---|---|---|---|---|
| 1 | 08:00-08:40 | 6 | 12:50-13:30 | 11 | 16:55-17:35 |
| 2 | 08:50-09:30 | 7 | 13:40-14:20 | 12 | 18:45-19:25 |
| 3 | 09:45-10:25 | 8 | 14:30-15:10 | 13 | 19:30-20:10 |
| 4 | 10:35-11:15 | 9 | 15:15-15:55 | 14 | 20:15-20:55 |
| 5 | 11:20-12:00 | 10 | 16:10-16:50 | 15 | 21:05-21:45 |

🔴 **2026-10-01 修正**：本表原先缺 6、7 节，被错记成「我校没有这两节」，
连前端都专门做了"缺号行压扁成细线"的处理。但教务数据里**真的**排了
「星期一第6-9节{7周}」—— 那两节课画不出来，整表行号也跟着错位。
**表里缺号只代表"不知道该节的上课时间"，绝不能推出"学校没有这一节"。**

## 6. 真实环境的关键实测结论

- **`iskxk` 是「是否选课期」的权威标志**：`iskxk=1` 开放期 / `iskxk=0` 关闭期。
- **关闭期的表现**：Index 页 HTTP 200 + 「当前不属于选课阶段」，`iskxk=0`；
  隐藏域只剩 21 个通用项，`xkkz_xd` / `xkxnm` / `zxfs` / `xkzgxf` 全部消失。
- **`do_jxb_id` 是一次性令牌**：每次查询重新下发 → 退课/提交都必须
  「同一次查询内部完成」；`jxb_id`（稳定）用来认班，`do_id`（易变）只用于当次提交。
- **`xkkz_xh` 加密串**从 Tab 锚点正则取（`#xkkz_xh` 隐藏域是空的）。
- **切 Tab 必须重载 Display**（`rwlx/xkly/bklx_id` 随 Tab 变）。
- **`kklxdm` 会重复**，唯一定位用**下标**。
- **学分只能读隐藏域**：`zxfs`=已选、`xkzgxf`=最高；页面 `<font id="yxxfs">` 在原始 HTTP 响应里**恒为 0**。
- **`xkly` 语义随接口变**：`PartDisplay` 随 Tab（0/1）、`ChoosedDisplay` 恒为 0（见 `core/client.py::query_selected`）。
- **退课资格**：`sfxkbj=0` → 我校一律不可退（`isktk` 五条件见 `core/drop.py`）。
- **时序**：RTT 新连接 ~410ms / 复用 ~250ms；`Date` 头精度 1s；**T0 只发 1 个请求**是胜负手。

---

## 7. 切换开关速查

| 想要 | 命令 | 打向 |
|---|---|---|
| **真实教务**（推荐带 `--real` 兜底） | `python serve.py --real` | 🟢 `https://jwxt.nfu.edu.cn/jwglxt/` |
| 真实教务（干净新进程也可以） | `python serve.py` | 🟢 同上 |
| 本地模拟教务 | `python serve.py --mock` | 🟡 `http://127.0.0.1:<随机端口>/jwglxt/` |
| 指定抓包目录的模拟 | `python serve.py --mock captures/2026-09-29-open` | 🟡 同上 |

**为什么建议永远带 `--real`**：`XK_SCHOOL_URL` 是**进程级环境变量**。
如果它残留在你当前的 shell 里（手工 `export` 过、或某些启动器继承了它），
那么**不加 `--mock` 启动也会悄悄打到本地假教务**。
`--real` 会主动清掉它；不带 `--real` 时程序也会**检测到并大字告警**。

**启动后认准这一行**（`serve.py` 打印）：
```
教务目标:  🟢 真实教务  广州南方学院  https://jwxt.nfu.edu.cn/jwglxt/
教务目标:  🟡 本地模拟教务（抓包回放，不碰真实教务）
```

## 8. 真实环境验收命令

```bash
PY=C:/Users/32768/.workbuddy/binaries/python/envs/default/Scripts/python.exe

# 端到端（需 9666 端口已登录的抓包浏览器实例）
$PY tests/e2e_live.py

# 关闭期只读探测
$PY tests/probe_closed_phase.py

# 离线回归（不碰教务）
$PY tests/smoke_core.py && $PY tests/smoke_schedule.py && $PY tests/smoke_e2e.py
```

⚠️ **抓包浏览器实例若被销毁 → 登录态失效**，重跑 e2e/live 前需重开
`--remote-debugging-port=9666` 并重新登录。
