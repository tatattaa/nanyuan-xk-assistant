"""真实环境选课实测（⚠️ --go 会真实占课位）。

用途：验证 `submit()` 的成功分支 —— 直接打 `zzxkyzbjk_xkBcZyZzxkYzb.html`。

用法：
    # 1) 只扫描候选（只读，零副作用）——推荐先跑这个
    python tests/e2e_grab.py

    # 2) 真实提交「第一个未满且无时间冲突」的教学班
    python tests/e2e_grab.py --go

    # 3) 指定课程
    python tests/e2e_grab.py --go --kch <课程id>

    # 4) 退课（不可逆！）
    python tests/e2e_grab.py --cancel <课程id> --do-id <do_jxb_id>

凭据经 CDP 取自已登录的浏览器，只在内存传递（铁律 #9）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ZfClient
from core.config import (
    CT_FLAG_BOTH,
    CT_FLAG_CONFLICT,
    CT_FLAG_CROSS_CAMPUS,
    CT_FLAG_MANUAL,
    CT_FLAG_OK,
    Credential,
)
from core.errors import KIND_LABEL, XKError
from tests.e2e_live import fetch_cookies

CT_LABEL = {
    CT_FLAG_OK: "无冲突",
    CT_FLAG_CONFLICT: "时间冲突",
    CT_FLAG_CROSS_CAMPUS: "同半天跨校区",
    CT_FLAG_BOTH: "时间冲突+跨校区",
    CT_FLAG_MANUAL: "需申请教务处理",
}

MAX_COURSES_PER_TAB = 25   # 每个 Tab 最多细查多少门课（控制请求量）
MAX_CANDIDATES = 3         # 攒够几个「未满且无冲突」就停


def arg(name: str, default: str = "") -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default


def scan(client: ZfClient, only_kch: str = "", only_tab: str = "") -> list[dict]:
    """遍历全部 Tab，找出「未满」的教学班，并对每个做零副作用的时间冲突预检。"""
    client.init()
    print(f"选课开放 = {client.is_open}，课程类别 {len(client.tabs)} 个\n")

    cands: list[dict] = []
    for t in client.tabs:
        if only_tab and t.kklxdm != only_tab:
            continue
        rows, _ = client.query_courses(tab=t, page=1, page_size=200)
        courses: dict[str, dict] = {}
        for r in rows:
            kch = r.get("kch_id") or ""
            if kch and kch not in courses:
                courses[kch] = r
        keys = list(courses)
        if only_kch:
            keys = [k for k in keys if k == only_kch]
        print(f"[{t.kklxdm}] {t.name}：{len(rows)} 个教学班 / {len(courses)} 门课"
              + (f"（筛出 {len(keys)} 门）" if only_kch else ""))

        max_courses = MAX_COURSES_PER_TAB if "--allow-full" not in sys.argv else 60
        for kch in keys[:max_courses]:
            row = courses[kch]
            try:
                classes = client.query_classes(
                    kch, tab=t, cxbj=row.get("cxbj", "0"), fxbj=row.get("fxbj", "0")
                )
            except XKError as e:
                print(f"    ! {row.get('kcmc')} 查教学班失败：{e}")
                continue
            free = [j for j in classes if not j.is_full]
            print(f"    · {row.get('kcmc','')[:24]:26s} {len(classes):3d} 班 / {len(free):3d} 未满")
            do_precheck_full = "--allow-full" in sys.argv
            for j in classes:
                if j.is_full and not do_precheck_full:
                    # 满员的不做冲突预检（省请求）；标记为满，仅在 --allow-full 时使用
                    cands.append({
                        "tab": t, "row": row, "kch_id": kch, "jxb": j,
                        "ct_flag": "", "ct_msg": "",
                    })
                    continue
                try:
                    ct = client.precheck_conflict(kch, j.do_id)
                except XKError as e:
                    ct = {"flag": "", "msg": str(e)}
                flag = str(ct.get("flag", ""))
                cands.append({
                    "tab": t, "row": row, "kch_id": kch, "jxb": j,
                    "ct_flag": flag, "ct_msg": str(ct.get("msg") or ""),
                })
                if flag == CT_FLAG_OK:
                    print(f"        ✔ 可用  {j.sksj[:40]}  {j.yxzrs}/{j.jxbrl}")
            cap = MAX_CANDIDATES if "--allow-full" not in sys.argv else 200
            if sum(1 for c in cands if c["ct_flag"] == CT_FLAG_OK) >= cap:
                print("    （已攒够候选，提前结束本 Tab 扫描）")
                break
    return cands


def full_sweep(client: ZfClient) -> int:
    """扫全部 Tab，对「已满且预检无冲突」的教学班逐个尝试提交，直到拿到满员分支。

    这是验证 `flag` 满员语义的唯一办法 —— 满员班提交**必然失败**，零副作用。
    （注意：若某班预检报冲突，服务端会先返回冲突，测不到满员分支，所以要逐个试。）
    """
    client.init()
    tried = 0
    for t in client.tabs:
        rows, _ = client.query_courses(tab=t, page=1, page_size=200)
        courses: dict[str, dict] = {}
        for r in rows:
            k = r.get("kch_id") or ""
            if k and k not in courses:
                courses[k] = r
        for kch, row in list(courses.items())[:60]:
            try:
                classes = client.query_classes(
                    kch, tab=t, cxbj=row.get("cxbj", "0"), fxbj=row.get("fxbj", "0")
                )
            except XKError:
                continue
            for j in classes:
                if not j.is_full:
                    continue
                try:
                    ct = client.precheck_conflict(kch, j.do_id)
                except XKError:
                    continue
                if str(ct.get("flag", "")) != CT_FLAG_OK:
                    continue
                tried += 1
                print(f"\n[{t.kklxdm}/{t.name}] {row.get('kcmc')}  "
                      f"{j.sksj[:40]}  {j.yxzrs}/{j.jxbrl}（满，预检无冲突）")
                client.switch_tab(t)
                res = client.submit(
                    kch, j.do_id, kcmc=row.get("kcmc", ""), kklxdm=t.kklxdm,
                    xkbj=str(row.get("xxkbj", "0")), cxbj=str(row.get("cxbj", "0")),
                )
                print(f"   → success={res['success']} flag={res['flag']!r} "
                      f"msg={res['msg']!r} kind={res.get('kind')}")
                if res["success"]:
                    print("   ⚠️ 竟然选上了（服务端容量计数有滞后）—— 建议退课")
                    return 3
                if str(res["flag"]) == "-1":
                    print("   ✅ 拿到满员分支原始返回")
                    return 0
                if tried >= 12:
                    print("\n试了 12 个仍未拿到满员分支")
                    return 2
    print(f"\n试过 {tried} 个满员班，均未返回 flag=-1")
    return 2


def main() -> int:
    go = "--go" in sys.argv
    only_kch = arg("--kch")
    cancel_kch = arg("--cancel")

    cred = Credential(cookie_header=fetch_cookies(), source="cdp")
    client = ZfClient(cred)
    try:
        if "--full-sweep" in sys.argv:
            return full_sweep(client)

        # ---------- 退课分支 ----------
        # --cancel 支持「课程名关键字」或 kch_id：从已选列表里反查 do_jxb_id
        if cancel_kch:
            client.init()
            sel = client.query_selected()
            hit = [
                r for r in sel
                if str(r.get("kch_id")) == cancel_kch
                or cancel_kch in str(r.get("kcmc", ""))
                or cancel_kch in str(r.get("kch", ""))
            ]
            if not hit:
                print(f"已选列表里没有匹配「{cancel_kch}」的课程")
                for r in sel:
                    print(f"    {r.get('kcmc')}  kch_id={r.get('kch_id')}")
                return 1
            row = hit[0]
            kch_id = str(row.get("kch_id"))
            do_id = str(row.get("do_jxb_id") or "")
            print(f"命中已选课程：{row.get('kcmc')}  {row.get('jxbmc','')}")
            print(f"  kch_id   = {kch_id}")
            print(f"  do_jxb_id= {do_id[:28]}…（{len(do_id)} 位）")
            if not do_id:
                print("!! 该行没有 do_jxb_id，无法退课")
                return 1
            print(f"\n⚠️ 正在退课……")
            r = client.cancel(kch_id, do_id)
            print(f"   {'✅' if r['success'] else '❌'} {r['msg']}（返回码 {r['code']!r}）")
            sel2 = client.query_selected()
            print(f"   退课后已选：{len(sel)} → {len(sel2)} 门")
            return 0 if r["success"] else 1

        # ---------- 扫描 ----------
        cands = scan(client, only_kch, arg("--tab"))

        # ---------- 汇总 ----------
        n_free = sum(1 for c in cands if not c["jxb"].is_full)
        n_free_ok = sum(1 for c in cands if not c["jxb"].is_full and c["ct_flag"] == CT_FLAG_OK)
        n_ok = sum(1 for c in cands if c["ct_flag"] == CT_FLAG_OK)
        print(f"\n{'='*70}")
        print(f"教学班共 {len(cands)} 个：未满 {n_free} / 已满 {len(cands)-n_free}"
              f"；其中「未满且无时间冲突」{n_free_ok} 个（预检 flag=1 共 {n_ok} 个）")
        for i, c in enumerate(cands[:12]):
            lab = "满" if c["jxb"].is_full else CT_LABEL.get(c["ct_flag"], f"flag={c['ct_flag']}")
            print(f"  [{i}] {'✔' if c['ct_flag'] == CT_FLAG_OK else ' '} "
                  f"{c['tab'].name[:10]:12s} {str(c['row'].get('kcmc',''))[:22]:24s} "
                  f"{c['jxb'].yxzrs}/{c['jxb'].jxbrl}  {lab}")

        good = [c for c in cands if c["ct_flag"] == CT_FLAG_OK]
        if "--allow-full" in sys.argv:
            # 允许选「已满」的班（用于验证满员分支：提交必然失败，零副作用）
            # 但只在「预检无冲突」时才纳入 —— 否则服务端会先报冲突，测不到满员分支
            good = good + [c for c in cands if c["jxb"].is_full and c["ct_flag"] == CT_FLAG_OK]
        print("\n可提交候选（含 kch_id，可配合 --kch 精确指定）：")
        for i, c in enumerate(good):
            tag = "满" if c["jxb"].is_full else "可"
            print(f"  [{i}] {tag} {str(c['row'].get('kcmc',''))[:20]:22s} "
                  f"{c['jxb'].yxzrs}/{c['jxb'].jxbrl}  kch_id={c['kch_id']}")
        if not good:
            print("\n没有「未满且无冲突」的候选，无法验证成功分支。")
            return 0

        if not go:
            print("\n（只读模式；加 --go 才会真实提交）")
            return 0

        # ---------- 真实提交 ----------
        pick = int(arg("--pick", "0") or 0)
        c = good[pick] if 0 <= pick < len(good) else good[0]
        t, row, j, kch = c["tab"], c["row"], c["jxb"], c["kch_id"]
        print(f"\n{'='*70}")
        print(f"⚠️ 真实提交：{row.get('kcmc')}  [{t.kklxdm}/{t.name}]")
        print(f"   教学班 {j.sksj[:60]}")
        print(f"   人数 {j.yxzrs}/{j.jxbrl}   教师 {j.jsxx[:50]}")
        print(f"   do_id {j.do_id[:24]}…")

        client.switch_tab(t)
        res = client.submit(
            kch, j.do_id,
            kcmc=row.get("kcmc", ""),
            kklxdm=t.kklxdm,
            xkbj=str(row.get("xxkbj", "0")),
            cxbj=str(row.get("cxbj", "0")),
            qz="0",
        )
        print(f"\n结果：success={res['success']} flag={res['flag']!r} msg={res['msg']!r}")
        if res.get("kind"):
            print(f"      语义 = {KIND_LABEL.get(res['kind'], res['kind'])}")

        sel = client.query_selected()
        print(f"\n提交后已选课程：{len(sel)} 门")
        for s in sel:
            if str(s.get("kch_id")) == kch:
                print(f"   ✔ 其中包含本次目标：{s.get('kcmc')} {s.get('jxbmc','')}")
        return 0 if res["success"] else 2

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
