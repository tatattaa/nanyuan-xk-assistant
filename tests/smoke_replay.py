"""离线回放自检：不打教务，用抓包 fixture 重建完整查询链路并逐一对账。

断言「回放出来的数据」与抓包当天的 summary.json 完全一致：
  - Tab 数量 / 名称 / 加密串长度
  - 每个 Tab 课程列表总行数（全分页）
  - 每门课的教学班数量（含 do_jxb_id 令牌）
  - 冲突预检样本 flag
  - 已选课程门数

用法：python tests/smoke_replay.py [captures/2026-09-29-open]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ZfClient
from core.config import Credential
from core.errors import XKError
from core.replay import ReplaySession

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}{('：' + detail) if detail else ''}")
    else:
        FAIL += 1
        print(f"  ✗ {name}{('：' + detail) if detail else ''}")


def main() -> int:
    global PASS, FAIL
    cap = Path(sys.argv[1] if len(sys.argv) > 1 else "captures/2026-09-29-open")
    summary = json.loads((cap / "summary.json").read_text(encoding="utf-8"))
    print(f"回放目录：{cap}（抓包时间 {summary['captured_at']}，共 {summary['total_packets']} 包）")

    cred = Credential(cookie_header="JSESSIONID=replay; route=replay", source="replay")
    client = ZfClient(cred)
    client.http.close()
    client.http = ReplaySession(cap)

    try:
        # ---- init ----
        client.init()
        st = summary["tabs"]
        check("init：选课开放", client.is_open)
        check("init：Tab 数", len(client.tabs) == len(st), f"{len(client.tabs)}/{len(st)}")
        for i, t in enumerate(client.tabs):
            want = st[i]
            check(
                f"init：Tab{i} [{want['kklxdm']}/{want['name']}]",
                t.kklxdm == want["kklxdm"] and t.name == want["name"]
                and len(t.xkkz_xh) == want["xkkz_xh_len"] and t.xkkz_id == want["xkkz_id"],
            )

        # ---- 逐 Tab 全链路 ----
        for ti, tab in enumerate(client.tabs):
            want = st[ti]
            client.switch_tab(tab)

            total = 0
            for page in range(1, want["pages"] + 1):
                rows, _ = client.query_courses("", tab=tab, page=page)
                total += len(rows)
            check(f"Tab{ti} 课程行数", total == want["course_rows"],
                  f"{total}/{want['course_rows']}")

            # 用回放出的课程行重建 kch→cxbj/fxbj 映射（与抓包脚本同一取法）
            kch_map: dict[str, dict] = {}
            for page in range(1, want["pages"] + 1):
                rows, _ = client.query_courses("", tab=tab, page=page)
                for r in rows:
                    k = str(r.get("kch_id") or "")
                    if k and k not in kch_map:
                        kch_map[k] = r
            check(f"Tab{ti} 去重课程数", len(kch_map) == want["unique_courses"],
                  f"{len(kch_map)}/{want['unique_courses']}")

            bad = []
            for kch, row in kch_map.items():
                jxbs = client.query_classes(
                    kch, tab=tab,
                    cxbj=str(row.get("cxbj", "0") or "0"),
                    fxbj=str(row.get("fxbj", "0") or "0"),
                )
                w = want["classes"].get(kch, {})
                if len(jxbs) != w.get("jxb_count"):
                    bad.append(f"{kch} {len(jxbs)}!={w.get('jxb_count')}")
                elif [j.jxb_id for j in jxbs] != w.get("jxb_ids"):
                    bad.append(f"{kch} jxb_id 序列不一致")
            check(f"Tab{ti} 教学班逐课对账（{len(kch_map)} 门）", not bad, "; ".join(bad[:3]))

        # ---- 冲突预检样本 ----
        for cs in summary["conflict_samples"]:
            tab = client.tabs[cs["tab"]]
            client.switch_tab(tab)
            kch_map2: dict[str, dict] = {}
            for page in range(1, st[cs["tab"]]["pages"] + 1):
                rows, _ = client.query_courses("", tab=tab, page=page)
                for r in rows:
                    k = str(r.get("kch_id") or "")
                    if k and k not in kch_map2:
                        kch_map2[k] = r
            row = kch_map2[cs["kch_id"]]
            jxbs = client.query_classes(
                cs["kch_id"], tab=tab,
                cxbj=str(row.get("cxbj", "0") or "0"),
                fxbj=str(row.get("fxbj", "0") or "0"),
            )
            pc = client.precheck_conflict(cs["kch_id"], jxbs[0].do_id)
            check(f"冲突预检 Tab{cs['tab']} {cs['kch_id'][:8]}…",
                  str(pc.get("flag")) == str(cs["flag"]), f"flag={pc.get('flag')}")

        # ---- 已选 ----
        sel = client.query_selected()
        check("已选课程门数", len(sel) == summary["selected_count"],
              f"{len(sel)}/{summary['selected_count']}")
    except XKError as e:
        print(f"\n✗ 回放中断：{e}")
        if e.raw:
            print(f"  细节：{e.raw[:300]}")
        FAIL += 1
    finally:
        client.close()

    print()
    print(f"=== 回放自检：{PASS} 通过 / {FAIL} 失败"
          f"（命中 {client.http.hits}，未命中 {client.http.misses}）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
