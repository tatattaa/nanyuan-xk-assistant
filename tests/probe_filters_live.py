"""筛选参数实测：对同一 Tab 逐个条件打，比对行数/内容是否真的变化。

判据：教务**服务端真的筛了**才叫「功能可用」；若行数完全不变 = 教务静默忽略 → 方案作废。
多值条件用**重复参数**（`sksj=1&sksj=3`）传，不是逗号拼接。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8720"
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def courses(tab, size=200, **kw):
    """kw 的值可以是 str（单值）或 list（多值 → 重复参数）。"""
    pairs = [("tab_index", tab), ("size", size)]
    for k, v in kw.items():
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple)):
            pairs += [(k, str(x)) for x in v]
        else:
            pairs.append((k, str(v)))
    path = "/api/courses?" + urllib.parse.urlencode(pairs)
    t0 = time.time()
    try:
        with _op.open(BASE + path, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # noqa: F821
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            return None, j.get("detail") or {}, time.time() - t0
        except Exception:  # noqa: BLE001
            return None, {"raw": body[:200]}, time.time() - t0
    return d.get("rows") or [], d.get("meta") or {}, time.time() - t0


TAB = int(sys.argv[1]) if len(sys.argv) > 1 else 3

base_rows, base_meta, dt = courses(TAB)
print(f"Tab {TAB} = {base_meta.get('kklxmc')}（kklxdm={base_meta.get('kklxdm')}）")
print(f"★ 基线（无筛选）: {len(base_rows)} 行  {dt:.2f}s")
base_ids = set(r.get("jxb_id") for r in base_rows)
print()


def probe(title, **kw):
    rows, meta, dt = courses(TAB, **kw)
    if rows is None:
        print(f"  ✗ {title:<30} 教务报错: {str(meta.get('message') or meta)[:60]} / {str(meta.get('detail'))[:60]}")
        return None
    ids = set(r.get("jxb_id") for r in rows)
    same = ids == base_ids
    want_none = kw.pop("_expect_empty", False)
    tag = "🔴 未生效（=基线）" if same else f"🟢 生效（交集 {len(base_ids & ids)}）"
    print(f"  {'✓' if not same else '✗'} {title:<30} {len(rows):>4} 行  "
          f"echo={meta.get('filters')}  {tag}")
    return rows


print("── 单值 ──")
probe("sksj=1（周一）", sksj="1")
probe("sksj=3（周三）", sksj="3")
probe("skjc=8", skjc="8")
probe("skjc=1", skjc="1")
probe("yl=1（有余量）", yl="1")
probe("yl=0（无余量）", yl="0")
probe("cx=0（非重修）", cx="0")
probe("xf=2（2 学分）", xf="2")
print()
print("── 多值（重复参数）★ 本次重点 ──")
probe("sksj=[1,3]（周一+周三）", sksj=["1", "3"])
probe("sksj=[1,2,3,4,5]", sksj=["1", "2", "3", "4", "5"])
probe("skjc=[8,9,10]", skjc=["8", "9", "10"])
print()
print("── 新功能：只看时间冲突 ──")
probe("sksjct=1（与已选冲突）", sksjct="1")
probe("sksjct=0（不冲突）", sksjct="0")
print()
print("── 组合 ──")
probe("sksj=[1,3] + yl=1", sksj=["1", "3"], yl="1")
probe("sksjct=1 + yl=1", sksjct="1", yl="1")

# 一致性校验：两个互补条件之和应等于基线
a, _, _ = courses(TAB, yl="1")
b, _, _ = courses(TAB, yl="0")
c, _, _ = courses(TAB, sksjct="1")
e, _, _ = courses(TAB, sksjct="0")
print()
print("── 完备性校验 ──")
if a is not None and b is not None:
    print(f"   有无余量: {len(a)} + {len(b)} = {len(a) + len(b)}  vs 基线 {len(base_ids)}  "
          f"{'✅ 互补' if len(a) + len(b) == len(base_ids) else '⚠️ 不互补'}")
if c is not None and e is not None:
    print(f"   时间冲突: {len(c)} + {len(e)} = {len(c) + len(e)}  vs 基线 {len(base_ids)}  "
          f"{'✅ 互补' if len(c) + len(e) == len(base_ids) else '⚠️ 不互补'}")
