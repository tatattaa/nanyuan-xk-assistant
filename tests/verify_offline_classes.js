// 无头浏览器验收：**落盘数据的两件事**（2026-10-12 用户报的 bug + 新增需求）。
//
// ① bug：看落盘课程时**切换不了课程类别**
//    未开放期课程类别下拉是空的（Tab 只在开放期的 Index 页出现 → `S.tabs` 为空
//    → `currentTabIndex()` 恒为 -1），于是「📂 上次数据」永远只能显示最近搜过的那一类，
//    别的桶看得见（悬停里有）却点不到。
//    修法 = 课程列表上方加一条「快照类别」条（`#snapCats`），每个桶一个按钮。
//    🔴 刻意**不**把快照类别塞进那个「课程类别」下拉：那个下拉的 value 是 S.tabs 的
//       下标，塞进去 value 就对不上 S.tabs，「搜索 / 加入清单」会拿错类别 ——
//       正是本项目最忌讳的「界面显示值 ≠ 执行值」。
//
// ② 新增：教学班数据落盘 → 未开放期也能「选班」
//    展开教学班时先打教务；打不通（未开放期会被本地闸门挡下）就回落到
//    `/api/classes/snapshot/load`，并**明确标注这是离线数据**。
//    加进清单时：
//      · `do_id` 留空（那是每次查询重新下发的一次性令牌，离线手里的是废的）；
//      · `tab_index` / `kklxdm` **必须取自快照桶** —— 两个「板块课」的 kklxdm 都是 06，
//        拿 -1 或只靠 kklxdm，开抢时会把「大英一」的课解析到「大学体育」板块去。
//
// 前置（缺一不可，脚本会自己检查前两条）：
//   · 迷你假教务在跑：            python tests/mini_fake_jw.py 8799
//   · 界面实例指向它、且磁盘上有 courses.json **与 classes.json**：
//       XK_STATE_DIR=<临时目录> XK_SCHOOL_URL=http://127.0.0.1:8799/jwglxt/ \
//         python serve.py --port 8723
//   · 会话由本脚本自己建（POST /api/session，假教务不校验 Cookie）
//
// 用法：XK_BASE=http://127.0.0.1:8723 XK_FAKE=http://127.0.0.1:8799 \
//         node tests/verify_offline_classes.js
const http = require('http');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9791);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8723').replace(/\/$/, '');
const FAKE = (process.env.XK_FAKE || 'http://127.0.0.1:8799').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_verify_offline_classes_profile');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
function check(name, cond, detail = '') {
  console.log((cond ? '   ✓ ' : '   ✗ ') + name + (cond ? '' : '   ' + detail));
  if (!cond) fails.push(name);
}

function urlReq(u, method = 'GET', body = null, headers = {}) {
  return new Promise((res) => {
    const uo = new URL(u);
    const r = http.request(
      {
        host: uo.hostname, port: uo.port, path: uo.pathname + uo.search,
        method, timeout: 20000, headers,
      },
      (x) => {
        let d = '';
        x.on('data', (c) => (d += c));
        x.on('end', () => res({ code: x.statusCode, body: d }));
      },
    );
    r.on('error', (e) => res({ code: 0, body: String(e.code || e) }));
    r.on('timeout', () => { r.destroy(); res({ code: 0, body: 'timeout' }); });
    if (body !== null) r.write(body);
    r.end();
  });
}

// 假教务切「未开放期」后**必须打一次探活**：界面实例的 `sess.is_open` 是个缓存，
// 不给它一次联网核实的机会，前端 3 秒轮询也只会一直显示旧值。
async function setClosed() {
  const r = await urlReq(`${FAKE}/__close`);
  await urlReq(`${BASE}/api/session/check`);
  return r.body.trim();
}

async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error('ws error'));
  });
  let id = 0;
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id === mid) { ws.removeEventListener('message', onMsg); res(m); }
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ws, send };
}

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${DEBUG_PORT}`,
  '--remote-debugging-address=127.0.0.1',
  '--remote-allow-origins=*',
  `--user-data-dir=${PROFILE}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
  '--proxy-server=direct://', '--proxy-bypass-list=*',
  '--window-size=1420,1100',
  'about:blank',
], { stdio: 'ignore' });

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log(`迷你假教务：${FAKE}`);
  console.log('='.repeat(64));
  try {
    // ---- 前置 ----
    let st = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    const cs0 = st.courses_snapshot || {};
    const ks0 = st.classes_snapshot || {};
    if (!cs0.exists) {
      check('前置：磁盘上有课程快照（courses.json）', false,
        '这个实例的落盘目录里没有 courses.json —— 换个 XK_STATE_DIR 或先搜一次课');
    }
    check('前置：磁盘上有教学班快照（classes.json）', !!ks0.exists,
      '这个实例的落盘目录里没有 classes.json —— 先展开一次「选班」，或用测试夹具铺一份');
    if (!cs0.exists || !ks0.exists) throw new Error('前置不满足');

    console.log(`   假教务选课期：${await setClosed()}`);
    const sr = await urlReq(`${BASE}/api/session`, 'POST',
      JSON.stringify({ cookie: 'JSESSIONID=fake; route=fake' }),
      { 'Content-Type': 'application/json' });
    check('前置：会话已建立', sr.code === 200, `${sr.code} ${sr.body.slice(0, 160)}`);

    // ---- 从接口取「要验的那门课」与「它属于哪一桶」----
    // 不写死任何 id：夹具换了这套断言依然成立。
    const buckets = (cs0.buckets || []).slice()
      .sort((a, b) => (b.saved_at || 0) - (a.saved_at || 0));
    const cls0 = (ks0.courses || [])[0] || {};
    const homeBucket = buckets.find((b) => (b.kklxdm || '') === (cls0.kklxdm || '')
      && (b.kklxmc || '') === (cls0.kklxmc || ''))
      || buckets.find((b) => (b.key || '').split('|')[1] === (cls0.kklxmc || ''));
    check('前置：教学班夹具对应的课程类别确实在课程快照里（能切过去找到它）',
      !!homeBucket, JSON.stringify({ cls: cls0.kklxmc, buckets: buckets.map((b) => b.key) }));
    if (!homeBucket) throw new Error('夹具不自洽');
    console.log(`   要验的课：${cls0.kcmc}（kch_id=${cls0.kch_id}，${cls0.count} 个教学班）`
      + `\n   它属于：${homeBucket.kklxmc}（key=${homeBucket.key}，tab_index=${homeBucket.tab_index}）`
      + `\n   快照里共 ${buckets.length} 个类别桶`);

    let ver = null;
    for (let i = 0; i < 40; i++) {
      const r = await urlReq(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
      if (r.code === 200) { ver = JSON.parse(r.body); break; }
      await sleep(500);
    }
    if (!ver) throw new Error('调试端口没起来');

    const tab = JSON.parse((await urlReq(
      `http://127.0.0.1:${DEBUG_PORT}/json/new?about:blank`, 'PUT')).body);
    const { ws, send } = await connect(tab.webSocketDebuggerUrl);
    await send('Page.enable');
    await send('Runtime.enable');

    const errors = [];
    const consoleErrs = [];
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push(JSON.stringify(m.params.exceptionDetails).slice(0, 300));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        consoleErrs.push(JSON.stringify(m.params.args).slice(0, 300));
      }
    });

    await send('Page.navigate', { url: `${BASE}/` });
    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
      if (r.result && r.result.exceptionDetails) {
        return { __err: JSON.stringify(r.result.exceptionDetails).slice(0, 200) };
      }
      return r.result && r.result.result ? r.result.result.value : undefined;
    };

    // 等按钮露面（前端 3 秒轮询一次状态）
    let shown = false;
    for (let i = 0; i < 30; i++) {
      shown = await ev(`(() => { const b = document.getElementById('btnCoursesSnap');
        return !!b && b.style.display !== 'none' && b.getBoundingClientRect().width > 0; })()`);
      if (shown) break;
      await sleep(500);
    }
    check('前置：未开放期「📂 上次数据」按钮已露面', !!shown);

    // =====================================================================
    console.log('\n① 点「📂 上次数据」→ 加载落盘课程');
    // =====================================================================
    await ev(`document.getElementById('btnCoursesSnap').click()`);
    let n = 0;
    for (let i = 0; i < 40; i++) {
      n = await ev(`document.querySelectorAll('#courseList .item').length`);
      if (n > 0) break;
      await sleep(400);
    }
    check('课程列表出现了', n > 0, String(n));

    // =====================================================================
    console.log('\n② ⭐ BUG 修复：出现「快照类别」条，能切换课程类别');
    // =====================================================================
    const bar = await ev(`(() => {
      const e = document.getElementById('snapCats');
      if (!e) return { missing: true };
      const btns = Array.from(e.querySelectorAll('button[data-key]'));
      const r = e.getBoundingClientRect();
      return {
        shown: e.style.display !== 'none' && r.height > 0,
        count: btns.length,
        keys: btns.map((b) => b.dataset.key),
        labels: btns.map((b) => b.textContent.trim()),
        curIdx: btns.findIndex((b) => b.classList.contains('cur')),
        curDisabled: btns.some((b) => b.classList.contains('cur') && b.disabled),
        curIsPrimary: btns.some((b) => b.classList.contains('cur') && b.classList.contains('primary')),
      };
    })()`);
    console.log(`   #snapCats = ${JSON.stringify(bar)}`);
    check('⭐ 「快照类别」条出现了（这就是离线切类别的控件）',
      !!(bar && bar.shown), JSON.stringify(bar));
    check(`⭐ 每个课程类别桶都列出来了（${buckets.length} 个）`,
      !!(bar && bar.count === buckets.length), `${bar && bar.count} vs ${buckets.length}`);
    check('⭐ 列的正是快照里那些桶（key 逐个对上，不多不少）',
      !!(bar && buckets.every((b) => bar.keys.includes(b.key))),
      JSON.stringify(bar && bar.keys));
    check('⭐ 当前正在看的那一类被高亮/不可点（免得用户以为没生效）',
      !!(bar && bar.curIdx >= 0 && bar.curDisabled), JSON.stringify(bar));
    check('  文案带门数（一眼看出每类有多少）',
      !!(bar && bar.labels.every((t) => /（\d+）/.test(t))), JSON.stringify(bar && bar.labels));

    // 切到「教学班夹具所在的那一类」（若本来就是它，就切到另一个，再切回来）
    const curKey = (bar && bar.curIdx >= 0) ? bar.keys[bar.curIdx] : '';
    const targetKey = (curKey === homeBucket.key)
      ? (bar.keys.find((k) => k !== curKey) || curKey) : homeBucket.key;
    const target = buckets.find((b) => b.key === targetKey) || homeBucket;
    console.log(`   当前是「${curKey}」→ 点「${target.key}」`);

    const logsBefore = await ev(`document.getElementById('log').textContent`);
    await ev(`(() => { const b = document.querySelector('#snapCats button[data-key="'
      + ${JSON.stringify(targetKey)} + '"]'); return b ? (b.click(), true) : false; })()`);
    // ⚠️ 收敛条件必须与**真实日志文案**一致：点快照类别按钮走的是
    //   `loadCoursesSnapshot(key)` → 日志是「已加载上次搜索的课程数据：<类别名> 共 N 个教学班…」。
    //   早期这里写成匹配「类别「<名>」」（那是按钮 title 的措辞）→ 功能明明是好的却报红。
    //   这里同时接受「日志出现该类名」**或**「快照类别条的高亮已切到 target」——
    //   两者任何一个成立都足以证明「点了另一类，真的换了那一类」。
    let ok2 = false;
    let hint2 = '';
    let logHit = '';   // 记下到底哪条先成立，失败时好排查
    for (let i = 0; i < 40; i++) {
      const lg = await ev(`document.getElementById('log').textContent`);
      hint2 = await ev(`(() => { const e = document.getElementById('courseHint');
        return (e && e.style.display !== 'none') ? e.textContent.replace(/\\s+/g, ' ').trim() : ''; })()`);
      const cur = await ev(`(() => { const c = document.querySelector('#snapCats button.cur');
        return c ? (c.dataset.key || '') : ''; })()`);
      const byLog = !!lg && lg !== logsBefore
        && new RegExp(`已加载上次搜索的课程数据：${target.kklxmc}`).test(lg);
      const byCur = cur === targetKey;
      if (byLog || byCur) { ok2 = true; logHit = byLog ? '日志' : '高亮'; break; }
      await sleep(400);
    }
    const bar2 = await ev(`(() => {
      const e = document.getElementById('snapCats');
      const btns = Array.from(e.querySelectorAll('button[data-key]'));
      return { curKey: (btns.find((b) => b.classList.contains('cur')) || {}).dataset?.key || '' };
    })()`);
    const n2 = await ev(`document.querySelectorAll('#courseList .item').length`);
    const logs2 = await ev(`document.getElementById('log').textContent`);
    console.log(`   切换后：列表 ${n2} 门 / 高亮 = ${bar2 && bar2.curKey}；日志尾部 = ${String(logs2).slice(-110)}`);
    check('⭐⭐ 点了另一类 → 真的加载了那一类（日志/高亮佐证）',
      ok2, `target=${target.kklxmc}｜依据=${logHit || '无'}`);
    check('⭐ 高亮跟着换到刚点的那一类（不许「点了没反应」）',
      !!(bar2 && bar2.curKey === targetKey), JSON.stringify(bar2));
    // 断言「换的是同一份数据」：日志里的「共 N 个教学班」必须等于那一桶的 count
    check(`⭐ 加载的正是点的那一桶（${target.kklxmc} / ${target.count} 个教学班）`,
      n2 > 0 && new RegExp(`共 ${target.count} 个教学班`).test(String(logs2)),
      `门数 ${n2}；日志 ${String(logs2).slice(-140)}`);
    check('  提示条里写着「用快照类别条切换」（告诉用户这个控件是干嘛的）',
      String(hint2).includes('快照类别'), String(hint2).slice(0, 160));

    // =====================================================================
    console.log('\n③ ⭐ 新增：未开放期展开「选班」→ 自动回落到落盘的教学班');
    // =====================================================================
    // 先确保站在「夹具那门课所属的类别」上
    if (bar2 && bar2.curKey !== homeBucket.key) {
      await ev(`(() => { const b = document.querySelector('#snapCats button[data-key="'
        + ${JSON.stringify(homeBucket.key)} + '"]'); return b ? (b.click(), true) : false; })()`);
      for (let i = 0; i < 30; i++) {
        const k = await ev(`(() => { const e = document.getElementById('snapCats');
          const c = e.querySelector('button.cur'); return c ? c.dataset.key : ''; })()`);
        if (k === homeBucket.key) break;
        await sleep(400);
      }
    }
    // 找那门课在列表里的下标（用课程名匹配 —— DOM 上不暴露 kch_id）。
    // ⚠️ 要轮询等：切类别后列表是异步重画的，一次取可能还是上一类的列表。
    const FIND = `(() => {
      const want = ${JSON.stringify(cls0.kcmc)};
      const items = Array.from(document.querySelectorAll('#courseList .item[data-course]'));
      const hit = items.find((el) => {
        const t = el.querySelector('.title');
        return t && t.textContent.includes(want);
      });
      return hit ? parseInt(hit.dataset.course, 10) : -1;
    })()`;
    let idx = -1;
    for (let i = 0; i < 30; i++) {
      idx = await ev(FIND);
      if (idx >= 0) break;
      await sleep(400);
    }
    console.log(`   「${cls0.kcmc}」在列表里的下标 = ${idx}`);
    check('⭐ 夹具那门课出现在当前类别的列表里（前置自洽）', idx >= 0, String(idx));
    if (idx >= 0) {
      await ev(`(() => { const b = document.querySelector('button[data-act="expand"][data-i="'
        + ${idx} + '"]'); return b ? (b.click(), true) : false; })()`);
      let rows = 0;
      let offSeen = '';
      for (let i = 0; i < 40; i++) {
        rows = await ev(`document.querySelectorAll('#cls-${idx} .cls-row').length`);
        offSeen = await ev(`(() => { const e = document.getElementById('cls-${idx}');
          const t = e ? e.textContent : ''; return t.replace(/\\s+/g, ' ').trim().slice(0, 260); })()`);
        if (rows > 0) break;
        await sleep(400);
      }
      const logs3 = await ev(`document.getElementById('log').textContent`);
      console.log(`   展开出 ${rows} 行；头部：${String(offSeen).slice(0, 150)}`);
      check(`⭐ 离线展开出教学班（夹具里是 ${cls0.count} 个）`,
        rows === Number(cls0.count || 0), `${rows} vs ${cls0.count}`);
      check('⭐⭐ 明确标注「离线数据」（余量可能已经变了，不能让用户以为是实时）',
        String(offSeen).includes('离线数据'), String(offSeen).slice(0, 200));
      check('⭐ 说清了「提交令牌会在开始抢课时重新获取」（不撒谎：现在手里的令牌是废的）',
        String(offSeen).includes('开始抢课时'), String(offSeen).slice(0, 240));
      check('  日志交代了「教务查不到 → 改用上次保存的教学班数据」',
        String(logs3).includes('改用上次保存的教学班数据'), String(logs3).slice(-200));

      // =====================================================================
      console.log('\n④ ⭐ 离线加入清单：tab_index / kklxdm 必须取自快照桶');
      // =====================================================================
      await ev(`(() => { const b = document.querySelector('#cls-${idx} button[data-k="0"]');
        return b ? (b.click(), true) : false; })()`);
      let plan = null;
      for (let i = 0; i < 25; i++) {
        plan = await ev(`(S.plan || []).map((p) => ({kch_id: p.kch_id, jxb_id: p.jxb_id,
          do_id: p.do_id, kklxdm: p.kklxdm, tab_index: p.tab_index, kcmc: p.kcmc, slots: (p.slots||[]).length}))`);
        if (Array.isArray(plan) && plan.length) break;
        await sleep(400);
      }
      console.log(`   S.plan = ${JSON.stringify(plan)}`);
      check('⭐ 离线也能把教学班加进清单', Array.isArray(plan) && plan.length === 1, JSON.stringify(plan));
      if (Array.isArray(plan) && plan.length) {
        const p = plan[0];
        check('⭐⭐ `tab_index` 取自快照桶（不是 -1）—— 两个板块课 kklxdm 都是 06，'
          + '拿 -1 会在开抢时定位到错板块',
          p.tab_index === homeBucket.tab_index,
          `plan.tab_index=${p.tab_index} bucket.tab_index=${homeBucket.tab_index}`);
        check('⭐ `kklxdm` 取自快照桶', p.kklxdm === homeBucket.kklxdm,
          `plan=${p.kklxdm} bucket=${homeBucket.kklxdm}`);
        check('⭐⭐ `do_id` 留空（离线手里的一是一次性令牌、早废了，不该带）',
          !p.do_id, String(p.do_id));
        check('⭐ `jxb_id` 保留（稳定 id，开抢时靠它认回同一个班）',
          !!p.jxb_id, String(p.jxb_id));
        check('  时段带上了（课表画「待选」那一层要用）', p.slots > 0, String(p.slots));
      }
      const btnTxt = await ev(`(() => { const b = document.querySelector('#cls-${idx} button[data-k="0"]');
        return b ? b.textContent.trim() : ''; })()`);
      check('⭐ 按钮翻成「✓ 已加入」（判据走稳定的 jxb_id，do_id 为空也能认出来）',
        btnTxt.includes('已加入'), btnTxt);
      const logs4 = await ev(`document.getElementById('log').textContent`);
      check('  日志主动交代了「这是离线数据里加的」',
        String(logs4).includes('离线数据'), String(logs4).slice(-200));
    }

    check('全程没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));
    check('控制台没有 error 级输出', consoleErrs.length === 0, consoleErrs.slice(0, 2).join(' | '));

    ws.close();
  } catch (e) {
    check('脚本自身执行', false, String((e && e.stack) || e).slice(0, 300));
  } finally {
    try { chrome.kill(); } catch (_) {}
    try { await setClosed(); } catch (_) {}
  }

  console.log('\n' + '='.repeat(64));
  if (fails.length) {
    console.log(`✗ ${fails.length} 条未通过：`);
    fails.forEach((f) => console.log('   -', f));
    process.exit(1);
  }
  console.log('✓ 全部通过');
})();
