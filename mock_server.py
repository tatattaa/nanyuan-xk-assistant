"""本地模拟教务系统：把抓包存档变成一个**真能跑的 HTTP 服务器**。

让项目（UI / CLI / 引擎）像连真教务一样连它，离线重现开放期数据：

    python mock_server.py --capture captures/2026-09-29-open2 --port 8790

配合项目使用（推荐）：直接 `python serve.py --mock` —— 它会自动起本服务器
并把会话指过来，不需要浏览器、不需要 Cookie。

匹配规则（三级，响应头 X-Mock-Match 可见命中的哪一级）：
  1. exact   ：method + 路径 + 排序query + 完整表单逐字节一致
  2. relaxed ：忽略分页字段（kspage/jspage），取分页位置最近的一包
               （UI 默认 page_size=20 而抓包按页面 JS 的 10 抓，必然落到这级）
  3. nofilter：再忽略关键词字段（filter_list[*]）—— 模拟「搜索没生效」，
               返回该 Tab 第一页，比直接 404 友好
  写接口（提交/退课，未抓包）→ 200 + {"flag":"0","msg":"模拟教务：写操作不支持回放"}
  时间冲突预检（未抓包）    → 200 + {"flag":"1"} 按「无冲突」放行（否则会被读成冲突）
  其余未录取 → 404 + JSON 说明（client 会按「加密串错误/未知」语义抛错）

只监听 127.0.0.1。凭据不校验（任何 Cookie 都放行），因为它本来就只服务于离线测试。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("xk.mock")

_VOLATILE_PAGE = {"kspage", "jspage"}
_PREFIX_FILTER = "filter_list["

# 写接口路径关键词（未抓包，返回可读的失败而不是 404）
_WRITE_PATH_HINTS = ("xkBcZy", "tuikBc")

#: 只读、但抓包时没路过 → 给一个**诚实的桩响应**，而不是 404。
#:
#: ⚠️ 为什么要单独列出来，不能让它走 404：`precheck_conflict`（时间冲突预检）
#: 的判定口径是「`flag` 不是 `1` 就算冲突」（真实教务：1=无冲突 / 2~5=冲突）。
#: 而 404 的桩响应 body 是 `{"flag":"0","msg":"未抓包"}` —— `flag="0"` 会被
#: 顺理成章地读成「冲突」，于是**模拟模式下每一门课都被跳过**，
#: 日志清一色「时间冲突，跳过该项」，根本演示不了抢课流程。
#: 抓包全程是只读的，预检本来也没被单独录（它是页面上的点击动作）。
_PREPACK_READONLY = {
    "cxCtKcZy": json.dumps(
        {"flag": "1", "msg": "模拟教务：时间冲突预检未抓包，按「无冲突」放行"},
        ensure_ascii=False,
    ),
    # 教学班查询（cxJxbWithKch）：对未抓包的课程，返回一个**可提交**的桩教学班。
    # 否则蹲课/抢课在 mock 下会因「查不到教学班」直接 plan_done，根本走不到提交，
    # 也就复现不了「满员持续重试」这条。桩里 do_jxb_id 非空（client 靠它判有效行），
    # 提交时 mock 的写接口会回「满员」，正好接上 FULL 分支。
    "cxJxbWithKch": json.dumps(
        [
            {
                "jxb_id": "MOCK_JXB", "do_jxb_id": "MOCK_DO_ID",
                "kcmc": "", "jsxx": "模拟老师", "sksj": "星期三第3-4节",
                "jxdd": "", "jxbrl": "60", "yxzrs": "60", "jxms": "",
                "kkxymc": "", "xf": "2.0", "dsfrl": "0", "sjsfsj": "",
            }
        ],
        ensure_ascii=False,
    ),
}


def _norm_query(q: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(parse_qsl(q, keep_blank_values=True)))


def _body_key(body: dict[str, str], drop_page: bool = False, drop_filter: bool = False) -> tuple:
    items = []
    for k, v in body.items():
        if drop_page and k in _VOLATILE_PAGE:
            continue
        if drop_filter and k.startswith(_PREFIX_FILTER):
            continue
        items.append((k, v))
    return tuple(sorted(items))


def _content_type(text: str) -> str:
    t = text.lstrip()
    if t.startswith("{") or t.startswith("["):
        return "application/json; charset=utf-8"
    return "text/html; charset=utf-8"


class CaptureStore:
    """抓包存档 → 三张查找表。"""

    def __init__(self, capture_dir: str | Path):
        self.dir = Path(capture_dir)
        self.exact: dict[tuple, dict] = {}
        self.relaxed: dict[tuple, list[dict]] = {}
        self.nofilter: dict[tuple, list[dict]] = {}
        self.paths: set[str] = set()
        self.n = 0
        self._load()

    def _load(self) -> None:
        manifest = self.dir / "manifest.jsonl"
        if not manifest.exists():
            raise FileNotFoundError(f"抓包清单不存在：{manifest}")
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("error"):
                continue
            f = self.dir / e["file"]
            text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
            u = urlsplit(e["url"])
            body = {str(k): str(v) for k, v in (e.get("body") or {}).items()}
            entry = {
                "method": e["method"].upper(),
                "path": u.path,
                "query": _norm_query(u.query),
                "body": body,
                "status": e.get("status") or 200,
                "text": text,
                "label": e["label"],
            }
            self.paths.add(u.path)
            # 1) exact
            self.exact[(entry["method"], u.path, entry["query"], _body_key(body))] = entry
            # 2) relaxed（去分页字段）；记录 kspage 便于就近取
            entry["_ks"] = int(body.get("kspage") or 0) if str(body.get("kspage") or "").isdigit() else 0
            rk = (entry["method"], u.path, entry["query"], _body_key(body, drop_page=True))
            self.relaxed.setdefault(rk, []).append(entry)
            # 3) nofilter（再去关键词字段）
            nk = (entry["method"], u.path, entry["query"],
                  _body_key(body, drop_page=True, drop_filter=True))
            self.nofilter.setdefault(nk, []).append(entry)
            self.n += 1
        logger.info("mock 装载 %d 个包（%s）", self.n, self.dir)

    # -- 三级匹配 ------------------------------------------------------------

    def match(self, method: str, raw_path: str, body: dict[str, str]):
        u = urlsplit(raw_path)
        q = _norm_query(u.query)
        m = method.upper()
        k = (m, u.path, q, _body_key(body))
        if k in self.exact:
            return self.exact[k], "exact"
        rk = (m, u.path, q, _body_key(body, drop_page=True))
        cands = self.relaxed.get(rk)
        if cands:
            want_ks = int(body.get("kspage") or 0) if str(body.get("kspage") or "").isdigit() else 0
            return min(cands, key=lambda c: abs(c["_ks"] - want_ks)), "relaxed"
        nk = (m, u.path, q, _body_key(body, drop_page=True, drop_filter=True))
        cands = self.nofilter.get(nk)
        if cands:
            return cands[0], "nofilter"
        return None, "miss"


class _Handler(BaseHTTPRequestHandler):
    store: CaptureStore = None  # 由 start_mock 注入
    hits = {"exact": 0, "relaxed": 0, "nofilter": 0, "miss": 0, "write": 0, "stub": 0}

    def log_message(self, fmt, *args):  # 静音默认访问日志（用我们自己的）
        pass

    def _reply(self, status: int, text: str, tier: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", _content_type(text))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Mock-Match", tier)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        body = dict(parse_qsl(raw, keep_blank_values=True))

        path = urlsplit(self.path).path
        if any(h in path for h in _WRITE_PATH_HINTS):
            self.hits["write"] += 1
            # 提交（xkBcZy）→ 回「满员」：flag="-1" + "辅教学班标志,教学班id,已选人数,本轮已选"
            #   这样抢课/蹲课在 mock 下能走到真实的 FULL 分支（而非笼统的「未知错误」），
            #   蹲课也正好能复现「满员持续重试」这条（2026-10-01 修的 bug 的回归场景）。
            # 退课（tuikBc）→ 保持「写操作不支持」的可读失败。
            if "xkBcZy" in path:
                logger.info("[write ] %s %s（回满员：已选 46 人）", method, path)
                self._reply(200, json.dumps({
                    "flag": "-1", "msg": "0,J1,46,0",
                }, ensure_ascii=False), "write")
            else:
                logger.info("[write ] %s %s（写操作不支持回放）", method, path)
                self._reply(200, json.dumps({
                    "flag": "0", "msg": "模拟教务：写操作未抓包，不支持回放（抓包全程只读）",
                }, ensure_ascii=False), "write")
            return

        entry, tier = self.store.match(method, self.path, body)
        if entry:
            self.hits[tier] += 1
            logger.info("[%-7s] %s %s → %s", tier, method, path, entry["label"])
            self._reply(entry["status"], entry["text"], tier)
            return

        # 只读但没路过 → 给桩响应，别落进 404（否则会被上层误读成业务结果）
        for hint, stub in _PREPACK_READONLY.items():
            if hint in path:
                self.hits["stub"] += 1
                logger.info("[stub   ] %s %s（未抓包，给桩响应）", method, path)
                self._reply(200, stub, "stub")
                return

        self.hits["miss"] += 1
        logger.warning("[miss   ] %s %s", method, path)
        self._reply(404, json.dumps({
            "flag": "0",
            "msg": "模拟教务：该请求未抓包",
            "hint": "已知路径见 mock_server 启动日志或 captures README",
        }, ensure_ascii=False), "miss")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start_mock(capture_dir: str | Path, host: str = "127.0.0.1", port: int = 0):
    """起模拟教务服务器（后台线程）。返回 (httpd, 实际端口)。"""
    store = CaptureStore(capture_dir)

    class Handler(_Handler):
        pass

    Handler.store = store
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="mock-jwxt", daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模拟教务系统（抓包回放）")
    ap.add_argument("--capture", default="captures/2026-09-29-open2")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    httpd, port = start_mock(a.capture, port=a.port)
    base = f"http://127.0.0.1:{port}/jwglxt/"
    print(f"模拟教务已启动：{base}（存档 {a.capture}）")
    print("接入方式：")
    print(f"  · UI：python serve.py --mock {a.capture}   （全自动，推荐）")
    print(f"  · 手动：XK_SCHOOL_URL={base} 后正常启动项目")
    print("Ctrl+C 停止")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
