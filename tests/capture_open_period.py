"""选课开放期全量抓包：把所有只读接口的「请求参数 + 原始响应」整体落盘。

目的：系统关闭后仍能离线重现这批包（配合 core/replay.py 回放）。

抓什么（全部只读，零副作用，不发任何提交/退课请求）：
  1. Index 页（init 步骤 1）
  2. 每个课程类别 Tab 的 Display 页（切 Tab 上下文）
  3. 每个 Tab 的课程列表**全部分页**（kspage/jspage 与页面 JS 一致）
  4. 每门课的教学班查询（含 do_jxb_id 令牌与真实容量）
  5. 已选课程
  6. 时间冲突预检样本（每个 Tab 一门课，预检零副作用）
  7. Index 页引用的选课相关 JS（页面逻辑留档）

安全约定：
  - 凭据不落盘：manifest 只记 method/url/body/状态，**不记任何请求头**（含 Cookie）
  - body 里的 xkkz_xh / do_jxb_id 是**会话令牌**（关系统后必然失效），
    保留它们只为离线回放时与原代码路径逐字节对齐，不是凭据。

用法：
    python tests/capture_open_period.py [--out captures/2026-09-29-open] [--port 9666]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ZfClient
from core.config import Credential, DEFAULT_SCHOOL
from core.errors import XKError
from core.http import HttpSession, RawResponse

logger = logging.getLogger("xk.capture")

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 记录器
# ---------------------------------------------------------------------------


class Recorder:
    """把每次 HTTP 交换落盘：body 存 raw/，元数据追加 manifest.jsonl。"""

    def __init__(self, out_dir: Path):
        self.dir = out_dir
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = out_dir / "manifest.jsonl"
        self.seq = 0
        self.n_ok = 0
        self.n_err = 0

    def record(
        self,
        label: str,
        method: str,
        url: str,
        body: dict | None,
        resp: RawResponse | None,
        error: str | None = None,
    ) -> None:
        self.seq += 1
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:80]
        fname = f"{self.seq:04d}_{safe_label}.txt"
        text = resp.text if resp is not None else ""
        (self.raw_dir / fname).write_text(text, encoding="utf-8", errors="replace")
        entry = {
            "seq": self.seq,
            "label": label,
            "ts": datetime.now(CST).isoformat(timespec="milliseconds"),
            "method": method,
            "url": url,
            "body": body,  # POST 表单（含会话令牌，非凭据）；GET 为 None
            "status": resp.status if resp else None,
            "rtt_ms": round(resp.rtt * 1000, 1) if resp else None,
            "server_date": resp.server_date if resp else None,
            "bytes": len(text.encode("utf-8", errors="replace")),
            "sha1": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest(),
            "file": f"raw/{fname}",
            "error": error,
        }
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if error:
            self.n_err += 1
        else:
            self.n_ok += 1


class RecordingHttpSession(HttpSession):
    """在 HttpSession 上加一层落盘，不改任何请求行为。"""

    def __init__(self, *args, recorder: Recorder, labeler, **kw):
        super().__init__(*args, **kw)
        self._rec = recorder
        self._labeler = labeler  # callable() -> str，取当前请求的业务标签

    def _do(self, method, url, data=None, referer=None, allow_redirects=True):
        label = self._labeler() if self._labeler else "http"
        try:
            resp = super()._do(
                method, url, data=data, referer=referer, allow_redirects=allow_redirects
            )
        except XKError as e:
            # 异常响应同样有价值（铁律 #8），raw 里是原文
            self._rec.record(
                label, method, url, data,
                RawResponse(status=0, text=e.raw or "", url=url),
                error=str(e)[:300],
            )
            raise
        self._rec.record(label, method, url, data, resp)
        return resp


# ---------------------------------------------------------------------------
# 抓包主流程
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="captures/2026-09-29-open")
    ap.add_argument("--port", type=int, default=9666)
    ap.add_argument("--sleep", type=float, default=0.25, help="请求间隔（秒），礼貌限速")
    ap.add_argument("--max-pages", type=int, default=60, help="单 Tab 课程列表分页上限（保险丝）")
    ap.add_argument("--no-classes", action="store_true", help="只抓课程列表，不逐课抓教学班")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out_dir)
    label = ["http"]  # 当前业务标签（可变闭包）

    def set_label(s: str) -> None:
        label[0] = s

    print(f"输出目录：{out_dir.resolve()}")
    print(f"① CDP(:{args.port}) 取 Cookie …")
    import tests.e2e_live as live

    live.CDP_PORT = args.port  # e2e_live 端口写死为 9666，这里按参数覆盖
    raw = live.fetch_cookies()
    names = [p.split("=", 1)[0].strip() for p in raw.split(";") if "=" in p]
    print(f"   Cookie {len(names)} 条：{', '.join(names)}（值不落盘）")

    cred = Credential(cookie_header=raw, source="cdp")
    client = ZfClient(cred)
    client.http.close()
    client.http = RecordingHttpSession(cred, recorder=rec, labeler=lambda: label[0])

    summary = {
        "captured_at": datetime.now(CST).isoformat(timespec="seconds"),
        "school": DEFAULT_SCHOOL.base_url,
        "cookie_names": names,
        "tabs": [],
        "selected_count": None,
        "conflict_samples": [],
        "js_files": [],
        "errors": [],
    }

    try:
        # ---- ② init：Index + 第一个 Tab 的 Display ----
        set_label("index")
        client.init()
        print(f"② init 完成：隐藏域 {len(client.store)}，Tab {len(client.tabs)} 个，开放={client.is_open}")
        if not client.is_open or not client.tabs:
            print("⚠️ 当前不在选课开放期或未解析到 Tab，抓包中止（已存 Index 页）")
            _write_summary(out_dir, summary, rec)
            return 1

        # Index 页引用的选课 JS（页面逻辑留档，关系统后也取不到）
        index_html = (rec.raw_dir / "0001_index.txt").read_text(encoding="utf-8", errors="replace") \
            if (rec.raw_dir / "0001_index.txt").exists() else ""
        _capture_js(client, index_html, rec, set_label, summary, args.sleep)

        # ---- ③ 逐 Tab：Display → 课程列表全分页 → 每门课的教学班 ----
        for ti, tab in enumerate(client.tabs):
            tname = f"{tab.kklxdm}_{tab.name}".replace("/", "_")
            try:
                set_label(f"display_tab{ti}_{tname}")
                client.switch_tab(tab)
                time.sleep(args.sleep)

                # 课程列表：翻到短页为止
                all_rows: list[dict] = []
                page = 1
                while page <= args.max_pages:
                    set_label(f"courses_tab{ti}_{tname}_p{page}")
                    rows, _meta = client.query_courses("", tab=tab, page=page)
                    all_rows.extend(rows)
                    if len(rows) < 10:  # DEFAULT_PAGE_STEP
                        break
                    page += 1
                    time.sleep(args.sleep)

                kch_map: dict[str, dict] = {}
                for r in all_rows:
                    k = str(r.get("kch_id") or "")
                    if k and k not in kch_map:
                        kch_map[k] = r
                print(f"③ Tab{ti} [{tab.kklxdm}/{tab.name}]：课程行 {len(all_rows)}，"
                      f"去重课程 {len(kch_map)}，分页 {page}")
                tab_info = {
                    "index": ti, "kklxdm": tab.kklxdm, "name": tab.name,
                    "xkkz_id": tab.xkkz_id, "njdm_id": tab.njdm_id, "zyh_id": tab.zyh_id,
                    "xkkz_xh_len": len(tab.xkkz_xh),
                    "course_rows": len(all_rows), "unique_courses": len(kch_map),
                    "pages": page, "classes": {},
                }

                # 每门课的教学班（do_jxb_id + 真容量）
                conflict_done = False
                if not args.no_classes:
                    for ci, (kch, row) in enumerate(kch_map.items()):
                        set_label(f"classes_tab{ti}_{kch}")
                        try:
                            jxbs = client.query_classes(
                                kch, tab=tab,
                                cxbj=str(row.get("cxbj", "0") or "0"),
                                fxbj=str(row.get("fxbj", "0") or "0"),
                            )
                            tab_info["classes"][kch] = {
                                "kcmc": row.get("kcmc", ""),
                                "jxb_count": len(jxbs),
                                "jxb_ids": [j.jxb_id for j in jxbs],
                            }
                            # 冲突预检样本：每个 Tab 第一门课的第一个班（零副作用）
                            if jxbs and not conflict_done:
                                set_label(f"conflict_tab{ti}_{kch}")
                                pc = client.precheck_conflict(kch, jxbs[0].do_id)
                                summary["conflict_samples"].append(
                                    {"tab": ti, "kch_id": kch, "flag": pc.get("flag"), "msg": pc.get("msg")}
                                )
                                conflict_done = True
                                time.sleep(args.sleep)
                        except XKError as e:
                            tab_info["classes"][kch] = {"error": str(e)[:200]}
                            summary["errors"].append(f"classes {kch}: {str(e)[:120]}")
                        time.sleep(args.sleep)
                        if (ci + 1) % 25 == 0:
                            print(f"   …教学班进度 {ci + 1}/{len(kch_map)}（已抓 {rec.seq} 包）")
                summary["tabs"].append(tab_info)
            except XKError as e:
                summary["errors"].append(f"tab{ti} {tname}: {str(e)[:150]}")
                print(f"③ Tab{ti} [{tab.kklxdm}/{tab.name}] 失败：{e}（继续下一个 Tab）")
                continue

        # ---- ④ 已选课程 ----
        try:
            set_label("selected")
            sel = client.query_selected()
            summary["selected_count"] = len(sel)
            print(f"④ 已选课程：{len(sel)} 门")
        except XKError as e:
            summary["errors"].append(f"selected: {str(e)[:150]}")
            print(f"④ 查已选失败：{e}")

    finally:
        client.close()

    _write_summary(out_dir, summary, rec)
    print()
    print(f"=== 抓包完成：{rec.seq} 个包（成功 {rec.n_ok} / 异常 {rec.n_err}）→ {out_dir.resolve()}")
    return 0


def _capture_js(client, index_html: str, rec: Recorder, set_label, summary: dict, sleep_s: float) -> None:
    """把 Index 页引用的选课相关 JS 抓下来（页面逻辑留档）。"""
    import re

    srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', index_html or "", re.I)
    # Index 页只直接引用 zzxkYzb.js；同目录的模块 JS（页面 .load() 动态加载的）
    # 按已知命名规律补齐，抓不到会在 manifest 里留异常记录，不影响主流程
    extra_names = ("zzxkYzbZy.js", "zzxkYzbChoosedZy.js")
    for src in list(srcs):
        if "zzxkYzb.js" in src:
            base = src.rsplit("/", 1)[0]
            srcs.extend(f"{base}/{n}" for n in extra_names)
            break
    seen = set()
    for src in srcs:
        if "zzxk" not in src and "xk" not in src.lower():
            continue
        if src in seen:
            continue
        seen.add(src)
        url = src if src.startswith("http") else DEFAULT_SCHOOL.base_url.rstrip("/") + "/" + src.lstrip("/")
        name = src.rsplit("/", 1)[-1].split("?")[0] or "script.js"
        set_label(f"js_{name}")
        try:
            client.http.get(url)
            summary["js_files"].append(name)
        except XKError as e:
            summary["errors"].append(f"js {name}: {str(e)[:120]}")
        time.sleep(sleep_s)
    if summary["js_files"]:
        print(f"   JS 留档 {len(summary['js_files'])} 个：{', '.join(summary['js_files'])}")


def _write_summary(out_dir: Path, summary: dict, rec: Recorder) -> None:
    summary["total_packets"] = rec.seq
    summary["ok"] = rec.n_ok
    summary["err"] = rec.n_err
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
