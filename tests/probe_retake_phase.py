"""退改选（退补选）阶段探测：与抢课（初选）阶段做对比。

⚠️ 全程**只读**：不 submit、不 cancel，一个写请求都不发。
   （铁律 #2 先预检后提交；铁律 #9 凭据不落盘。）

对比维度：
  ① 阶段标志        —— iskxk / _open / 页面提示语
  ② Index 页隐藏域  —— 数量 + 关键字段有无（选课上下文 / 退课资格开关）
  ③ 课程 Tab        —— 数量、加密串长度
  ④ 学分要求        —— zxfs / xkzgxf / 学年学期
  ⑤ 已选列表 + 退课资格 —— 逐门跑 core.drop.drop_state，看到底能不能退
  ⑥ 课程查询        —— 接口是否仍可用
  ⑦ 菜单            —— index_initMenu.html 里有没有「退改选」入口（gnmkdm 码）

用法：
    python tests/probe_retake_phase.py                       # 走 CDP 9666 取 Cookie
    python tests/probe_retake_phase.py --cookie "JSESSIONID=..; route=.."
    python tests/probe_retake_phase.py --dump captures/2026-09-30-retake   # 存原始响应
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.client import ZfClient, parse_hidden_inputs, parse_tabs  # noqa: E402
from core.config import DEFAULT_SCHOOL, PATH_INDEX_MENU, PATH_XK_INDEX  # noqa: E402
from core.credential import Credential  # noqa: E402
from core.drop import drop_state  # noqa: E402
from core.errors import FailureKind, XKError  # noqa: E402

# 抢课（初选）期实测锚点：这些字段当时**存在**（见 REAL-SCHOOL-PARAMS.md / OPEN-PERIOD-API.md）
BASELINE_PRESENT = [
    "xkkz_id", "xkxnm", "xkxqm", "xklc", "xklcmc", "zxfs", "xkzgxf",
    "xkxnmc", "xkxqmc", "iskxk", "sfktk", "tktjrs", "txbsfrl",
    "tkzgcs_jb", "tkzgcs_qt", "tkdxyzms", "sfxkbj", "zckz",
    "firstXkkzId", "firstKklxdm", "rwlx", "xkly", "njdm_id", "zyh_id",
]
# 退课资格相关的页面级开关（Index 页那份才算数，Display 会抹成空串）
DROP_PAGE_FIELDS = [
    "xxdm", "sfktk", "tktjrs", "txbsfrl", "tkzgcs_jb", "tkzgcs_qt", "tkdxyzms",
    "sfxkbj", "zckz", "bdzcbj", "bhbcyxkjxb", "isInxksj", "sfyxsksjct",
]


def _hr(t: str) -> None:
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


def _dump(dirpath: Path, name: str, text: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_text(text or "", encoding="utf-8")
    print(f"   ↳ 已存 {dirpath / name}（{len(text or '')} 字节）")


def main() -> int:
    argv = sys.argv[1:]
    cookie = ""
    dump_dir: Path | None = None
    if "--cookie" in argv:
        i = argv.index("--cookie")
        cookie = argv[i + 1] if i + 1 < len(argv) else ""
    if "--dump" in argv:
        i = argv.index("--dump")
        dump_dir = Path(argv[i + 1]) if i + 1 < len(argv) else None

    if not cookie:
        from tests.e2e_live import fetch_cookies as _fc  # type: ignore

        cookie = _fc()

    names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
    print(f"Cookie {len(names)} 条：{', '.join(names)}")
    if "JSESSIONID" not in names:
        print("⚠️ 缺 JSESSIONID —— 登录态大概率无效")

    client = ZfClient(Credential(cookie_header=cookie, source="cdp"))

    # ---- ① 阶段标志 ----
    _hr("① 阶段标志")
    try:
        store = client.init()
    except XKError as e:
        print(f"✗ init 抛异常（本该优雅返回）：{e.kind}")
        if e.raw:
            print(f"  原文：{e.raw[:400]}")
        return 1

    keys = [k for k in store if not k.startswith("_")]
    print(f"init 抽到 {len(keys)} 个字段")
    print(f"  is_open (client) = {client.is_open}      <-- 抢课期曾 = True")
    print(f"  iskxk            = {store.get('iskxk')!r}   <-- 抢课期 = '1'")
    print(f"  _open            = {store.get('_open')!r}")

    # 单独再 GET 一次 Index，拿原始 HTML 做字段盘点 + 证据留存
    idx_url = DEFAULT_SCHOOL.url(PATH_XK_INDEX, with_gnmkdm=True, with_layout=True)
    raw_idx = ""
    try:
        r = client.http.get(idx_url, expect_states=(FailureKind.NOT_OPEN,))
        raw_idx = r.text
        print(f"\nIndex 页原始 HTML：{len(raw_idx)} 字节，HTTP {r.status}")
        h = parse_hidden_inputs(raw_idx)
        print(f"Index 页隐藏域：{len(h)} 个   <-- 抢课期 = 225")
        tabs_raw = parse_tabs(raw_idx)
        print(f"Index 页 Tab 锚点：{len(tabs_raw)} 个   <-- 抢课期 = 6")
        present = [f for f in BASELINE_PRESENT if f in h or f in store]
        missing = [f for f in BASELINE_PRESENT if f not in h and f not in store]
        print(f"\n抢课期锚点字段：命中 {len(present)}/{len(BASELINE_PRESENT)}")
        print(f"  ✅ 仍在：{', '.join(present) or '（无）'}")
        print(f"  ❌ 消失：{', '.join(missing) or '（无）'}")
        if dump_dir:
            _dump(dump_dir, "index_retake.html", raw_idx)
            (dump_dir / "index_retake_hidden.json").write_text(
                json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception as e:  # noqa: BLE001
        print(f"✗ 二次 GET Index 失败：{type(e).__name__}: {e}")

    # 页面提示语
    if raw_idx:
        print("\n页面提示语命中：")
        hit = False
        for kw in (
            "不属于选课阶段", "不在选课时间", "退改选", "补选", "退课",
            "选课已结束", "未开放", "已结束", "退选",
        ):
            if kw in raw_idx:
                print(f"   🔎 {kw!r}")
                hit = True
        if not hit:
            print("   （无已知提示词）")

    # ---- ② 退课资格页面级开关 ----
    _hr("② 退课资格页面级开关（取 Index 页那份，Display 会抹空）")
    print(f"{'字段':<16}{'index_store':<20}{'store(合并后)':<20}{'BASELINE抢课期':<16}")
    for f in DROP_PAGE_FIELDS:
        iv = client.index_store.get(f, "—")
        sv = store.get(f, "—")
        print(f"{f:<16}{str(iv):<20}{str(sv):<20}")

    # ---- ③ Tab ----
    _hr("③ 课程 Tab")
    print(f"共 {len(client.tabs)} 个   <-- 抢课期 = 6")
    for t in client.tabs:
        print(f"  [{t.kklxdm}] {t.name:26s} xkkz_id={t.xkkz_id[:16]}… xh={len(t.xkkz_xh)}位")

    # ---- ④ 学分 ----
    _hr("④ 学分要求")
    c = client.credit
    print(f"found={c.found}  学年/学期={c.year!r}/{c.term!r}")
    print(f"最低={c.min_credit}  最高={c.max_credit}  已选={c.used_credit}")
    print(f"轮次时间={c.time_text!r}  round_name={c.round_name!r}")

    # ---- ⑤ 已选列表 + 退课资格 ----
    _hr("⑤ 已选列表 + 退课资格（core.drop.drop_state 逐门判定）")
    try:
        rows = client.query_selected()
    except Exception as e:  # noqa: BLE001
        rows = []
        print(f"✗ 查已选失败：{type(e).__name__}: {e}")
    print(f"已选 {len(rows)} 门   <-- 抢课期 = 12")
    page = client.index_store
    n_can = 0
    for r in rows:
        ds = drop_state(r, page)
        n_can += 1 if ds.allowed else 0
        print(f"\n  {r.get('kch_id','?')}  {r.get('kcmc','')}  [{r.get('jxbmc','')}]")
        print(f"     {'🟥 可退' if ds.allowed else '🔵 不可退'}  {ds.text}")
        print(f"     checks: sfktk={ds.checks.get('sfktk')} zntgpk={ds.checks.get('zntgpk')} "
              f"yxzrs={ds.checks.get('yxzrs')} tktjrs={ds.checks.get('tktjrs')} "
              f"isInxksj={ds.checks.get('isInxksj')} sfxkbj={ds.checks.get('sfxkbj')} "
              f"zckz={ds.checks.get('zckz')} bdzcbj={ds.checks.get('bdzcbj')}")
    print(f"\n汇总：可退 {n_can} / 共 {len(rows)} 门")
    if rows and dump_dir:
        (dump_dir / "selected_retake.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"   ↳ 已存 {dump_dir / 'selected_retake.json'}")

    # ---- ⑥ 课程查询 ----
    _hr("⑥ 课程查询（接口是否仍可用）")
    if not client.tabs:
        print("（无 Tab，跳过）")
    else:
        for t in client.tabs:
            try:
                cr, meta = client.query_courses("", tab=t, page=1, page_size=5)
                print(f"  [{t.kklxdm}] {t.name} → {len(cr)} 条")
                for x in cr[:2]:
                    print(f"      {x.get('kch_id','?')} {x.get('kcmc','')} 已选={x.get('yxzrs','?')}")
            except XKError as e:
                print(f"  [{t.kklxdm}] {t.name} → ✗ {e.kind.value}: {str(e)[:80]}")
            except Exception as e:  # noqa: BLE001
                print(f"  [{t.kklxdm}] {t.name} → ✗ {type(e).__name__}: {str(e)[:80]}")

    # ---- ⑦ 菜单 ----
    _hr("⑦ 菜单（找「退改选」入口）")
    menu_url = DEFAULT_SCHOOL.url(PATH_INDEX_MENU, with_gnmkdm=False)
    try:
        rm = client.http.get(menu_url)
        html = rm.text
        print(f"菜单页 {len(html)} 字节，HTTP {rm.status}")
        import re as _re

        codes = sorted(set(_re.findall(r"gnmkdm=([A-Za-z0-9]+)", html)))
        print(f"菜单里出现的 gnmkdm 码（{len(codes)} 个）：{', '.join(codes)}")
        print(f"  选课码 N253512 在菜单里：{'是' if 'N253512' in codes else '否'}")
        for kw in ("退改选", "退补选", "退课", "补选", "自主选课", "学生退课"):
            if kw in html:
                print(f"  🔎 菜单含关键词：{kw!r}")
        if dump_dir:
            _dump(dump_dir, "menu.html", html)
    except Exception as e:  # noqa: BLE001
        print(f"✗ 取菜单失败：{type(e).__name__}: {e}")

    _hr("探测完成（全程只读，未发任何写请求）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
