"""对比两次抓包：确认覆盖无遗漏 + 找出数据差异。

用法：python tests/compare_captures.py captures/2026-09-29-open captures/2026-09-29-open2
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def load(d: Path):
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    manifest = [
        json.loads(l)
        for l in (d / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    return summary, manifest


def load_classes(d: Path, manifest: list[dict]) -> dict[str, dict]:
    """{jxb_id: {kch_id, yxzrs, jxbrl, kcmc}} —— 从教学班响应原文重建。"""
    out: dict[str, dict] = {}
    for e in manifest:
        if not e["label"].startswith("classes_") or e.get("error"):
            continue
        kch = e["label"].split("_", 2)[-1]
        try:
            rows = json.loads((d / e["file"]).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            jid = str(r.get("jxb_id") or "")
            if jid:
                out[jid] = {
                    "kch_id": kch,
                    "yxzrs": str(r.get("yxzrs", "")),
                    "jxbrl": str(r.get("jxbrl", "")),
                }
    return out


def main() -> int:
    d1, d2 = Path(sys.argv[1]), Path(sys.argv[2])
    s1, m1 = load(d1)
    s2, m2 = load(d2)
    print(f"对比：{d1}（{s1['captured_at']}） vs {d2}（{s2['captured_at']}）\n")

    # ---- ① 覆盖面对账：两次抓的「包类型 × 数量」必须一致或只多不少 ----
    def kind(label: str) -> str:
        return label.split("_")[0]

    c1, c2 = Counter(kind(e["label"]) for e in m1), Counter(kind(e["label"]) for e in m2)
    print("① 覆盖面对账（包类型：第一次 → 第二次）")
    coverage_ok = True
    for k in sorted(set(c1) | set(c2)):
        flag = ""
        if c2[k] < c1[k]:
            flag = "  ⚠️ 第二次抓少了！"
            coverage_ok = False
        print(f"   {k:10s} {c1[k]:3d} → {c2[k]:3d}{flag}")
    # 逐门课教学班覆盖
    kch1 = {t["index"]: set(t["classes"]) for t in s1["tabs"]}
    kch2 = {t["index"]: set(t["classes"]) for t in s2["tabs"]}
    for ti in sorted(set(kch1) | set(kch2)):
        missing = kch1.get(ti, set()) - kch2.get(ti, set())
        if missing:
            coverage_ok = False
            print(f"   ⚠️ Tab{ti} 第二次漏抓教学班：{sorted(missing)}")
    print(f"   → 覆盖面：{'无遗漏 ✓' if coverage_ok else '有遗漏 ✗'}\n")

    # ---- ② 结构对账：Tab / 课程 / 教学班数量 ----
    print("② 结构对账（两次是否同一份选课上下文）")
    struct_ok = True
    for i, (t1, t2) in enumerate(zip(s1["tabs"], s2["tabs"])):
        same = (
            t1["kklxdm"] == t2["kklxdm"] and t1["xkkz_id"] == t2["xkkz_id"]
            and t1["course_rows"] == t2["course_rows"]
            and t1["unique_courses"] == t2["unique_courses"]
        )
        struct_ok &= same
        print(f"   Tab{i} [{t2['kklxdm']}/{t2['name'][:14]}] 行 {t1['course_rows']}→{t2['course_rows']}，"
              f"课 {t1['unique_courses']}→{t2['unique_courses']}，"
              f"xkkz_id {'同' if t1['xkkz_id'] == t2['xkkz_id'] else '不同（换轮次？）'}")
    print(f"   → 结构：{'一致 ✓' if struct_ok else '有差异（见上）'}\n")

    # ---- ③ 教学班人数变动（抢课实况） ----
    j1, j2 = load_classes(d1, m1), load_classes(d2, m2)
    both = set(j1) & set(j2)
    changed = [
        (j2[j]["kch_id"], j, j1[j]["yxzrs"], j2[j]["yxzrs"], j2[j]["jxbrl"])
        for j in both if j1[j]["yxzrs"] != j2[j]["yxzrs"]
    ]
    print(f"③ 教学班人数变动：可比班 {len(both)} 个，人数有变 {len(changed)} 个")
    for kch, j, a, b, cap in changed[:15]:
        print(f"   {kch[:12]}… 班{j[:8]}… {a} → {b} / 容量{cap}")
    gone = set(j1) - set(j2)
    if gone:
        print(f"   ⚠️ 第二次消失的班 {len(gone)} 个（可能已满被下墙）：{[g[:8] for g in list(gone)[:5]]}")
    print()

    # ---- ④ 已选 / 令牌 / 其它 ----
    print("④ 其它关键数据")
    print(f"   已选课程：{s1['selected_count']} 门 → {s2['selected_count']} 门")
    xh1 = s1["tabs"][0]["xkkz_id"] == s2["tabs"][0]["xkkz_id"]
    print(f"   加密串长度：{s1['tabs'][0]['xkkz_xh_len']} → {s2['tabs'][0]['xkkz_xh_len']}（256=同一会话体系）")
    print(f"   JS 留档：{s1['js_files']} → {s2['js_files']}")
    print(f"   总包数：{s1['total_packets']} → {s2['total_packets']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
