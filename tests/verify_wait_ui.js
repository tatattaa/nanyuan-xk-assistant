// 无头浏览器验收：蹲课功能（2026-10-01）。
//
// 需求来源（用户）：
//   「自己把蹲课功能做好有以下要求
//     蹲课采用抢课清单的轮流发送模式
//     每条申请请求间隔分为1s和0.8s两档，让用户自行选择
//     蹲课不按次数按时间，让用户自行选择蹲课开始和结束时间，沿用拨轮设计」
//
// 关键点（都必须在浏览器里验）：
//   ① 蹲课卡 UI：清单、间隔档（1s/0.8s）、开始/结束两套拨轮（沿用抢课拨轮）。
//   ② 间隔档：两档可切，选中的档要反映到回显与提交。
//   ③ 拨轮沿用：开始（立即/今天/明天）+ 结束（今天/明天），滚动/点击都精确到秒。
//   ④ 结束必填、且晚于开始 → 否则「开始蹲课」禁用。
//   ⑤ 提交后：/api/wait/plan 拿到 round_robin + interval_ms + start_at + deadline_at。
//   ⑥ 「+ 蹲」按钮：从搜索结果（教学班行）加进蹲课清单。
//
// 前置（本脚本自己铺数据，不碰 8720）：
//   XK_STATE_DIR=<临时目录> python serve.py --port 8721 --mock
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_wait_ui.js
//
// ⚠️ 本脚本会 POST /api/wait/plan + /api/wait/start，仍建议打隔离实例（8721）。
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9801);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_wait_ui_profile');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fails = [];
function check(name, cond, detail = '') {
  console.log((cond ? '   ✓ ' : '   ✗ ') + name + (cond ? '' : '   ' + detail));
  if (!cond) fails.push(name);
}

function urlReq(u, method = 'GET', body = null) {
  return new Promise((res) => {
    const uo = new URL(u);
    const r = http.request(
      { host: uo.hostname, port: uo.port, path: uo.pathname + uo.search, method, timeout: 20000,
        headers: body ? { 'Content-Type': 'application/json' } : {} },
      (x) => { let d = ''; x.on('data', (c) => (d += c)); x.on('end', () => res({ code: x.statusCode, body: d })); },
    );
    r.on('error', (e) => res({ code: 0, body: String(e.code || e) }));
    r.on('timeout', () => { r.destroy(); res({ code: 0, body: 'timeout' }); });
    if (body !== null) r.write(body);
    r.end();
  });
}
const jpost = (p, o) => urlReq(`${BASE}${p}`, 'POST', JSON.stringify(o));
const state = async () => JSON.parse((await urlReq(`${BASE}/api/state`)).body);

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--remote-debugging-address=127.0.0.1', '--remote-allow-origins=*',
  `--user-data-dir=${PROFILE}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--proxy-server=direct://', '--proxy-bypass-list=*',
  '--window-size=1420,1400', 'about:blank',
], { stdio: 'ignore' });

const json = (u) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: DEBUG_PORT, path: u, timeout: 5000 }, (r) => {
    let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => res(JSON.parse(d)));
  }).on('error', rej);
});

async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
  let id = 0;
  const errors = [];
  ws.addEventListener('message', (raw) => {
    let m = null;
    try { m = JSON.parse(raw.data); } catch (_) { return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      errors.push((d.exception && (d.exception.description || d.exception.value)) || d.text || 'unknown');
    }
  });
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    const on = (ev) => { const m = JSON.parse(ev.data); if (m.id === mid) { ws.removeEventListener('message', on); res(m); } };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ws, send, errors };
}

const pad2 = (n) => String(n).padStart(2, '0');

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log('='.repeat(64));
  try {
    check('前置：打的是隔离实例（别把用户的 8720 当试验田）',
      !/:8720(\/|$)/.test(BASE), BASE);

    let ver = null;
    for (let i = 0; i < 40; i++) {
      try { ver = await json('/json/version'); break; } catch (e) { await sleep(300); }
    }
    if (!ver) throw new Error('chrome 没起来');
    const tabs = await json('/json/list');
    const t = tabs.find((x) => x.type === 'page');
    const { send, errors } = await connect(t.webSocketDebuggerUrl);
    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      const ex = r.result && r.result.exceptionDetails;
      if (ex) {
        const msg = (ex.exception && (ex.exception.description || ex.exception.value))
          || ex.text || JSON.stringify(ex);
        throw new Error('页面内表达式抛异常：' + String(msg).split('\n').slice(0, 4).join(' | '));
      }
      return r.result.result.value;
    };
    await send('Runtime.enable');
    await send('Page.enable');

    await send('Page.navigate', { url: BASE + '/' });
    await sleep(2500);
    let ready = false;
    for (let i = 0; i < 40; i++) {
      ready = await ev(`typeof S !== 'undefined' && typeof initWaitWheel === 'function'`);
      if (ready) break;
      await sleep(300);
    }
    check('前置：页面加载完成，蹲课初始化函数存在', ready);

    // ================= ① 蹲课卡 UI 骨架 =================
    console.log('\n① 蹲课卡 UI 骨架');
    const ui = await ev(`(() => {
      const has = (id) => !!document.getElementById(id);
      return {
        list: has('waitList'), count: has('waitCount'), clear: has('btnClearWait'),
        ivSeg: has('waitIntervalSeg'), ivBtns: [...document.querySelectorAll('#waitIntervalSeg button')]
          .map((b) => b.dataset.ms),
        startSeg: has('waitStartSeg'), endSeg: has('waitEndSeg'),
        startWheel: has('waitStartWheel'), endWheel: has('waitEndWheel'),
        sh: has('waitSH'), sm: has('waitSM'), ss: has('waitSS'),
        eh: has('waitEH'), em: has('waitEM'), es: has('waitES'),
        echo: has('waitEcho'), btnStart: has('btnStartWait'), btnStop: has('btnStopWait'),
      };
    })()`);
    check('   蹲课清单列表 + 计数 + 清空按钮都在', ui.list && ui.count && ui.clear);
    check('   间隔档两档：1s(1000) / 0.8s(800)', ui.ivSeg && ui.ivBtns.join() === '1000,800',
      ui.ivBtns.join());
    check('   开始/结束分段 + 两套拨轮（6 个 wcol）都在',
      ui.startSeg && ui.endSeg && ui.startWheel && ui.endWheel
      && ui.sh && ui.sm && ui.ss && ui.eh && ui.em && ui.es);
    check('   回显 + 开始/停止按钮都在', ui.echo && ui.btnStart && ui.btnStop);

    // ================= ② 间隔档：两档可切 =================
    console.log('\n② 请求间隔两档（1s / 0.8s）');
    const iv0 = await ev(`(() => {
      const seg = document.getElementById('waitIntervalSeg');
      return { v: S.waitInterval,
               on: [...seg.querySelectorAll('button')].filter((b) => b.classList.contains('on'))
                 .map((b) => b.dataset.ms) };
    })()`);
    check('   默认间隔 1000ms（1s），且 1s 档高亮', iv0.v === 1000 && iv0.on.join() === '1000',
      `v=${iv0.v} on=${iv0.on}`);
    const iv1 = await ev(`(() => {
      document.querySelector('#waitIntervalSeg button[data-ms="800"]').click();
      return S.waitInterval;
    })()`);
    check('   点 0.8s → S.waitInterval 变 800', iv1 === 800, String(iv1));

    // ================= ③ 开始/结束拨轮沿用 =================
    console.log('\n③ 开始/结束两套拨轮（沿用抢课拨轮）');
    // 拨轮结构：时 0-23 / 分 0-59 / 秒 0-59
    const wheelStruct = await ev(`(() => {
      const col = (id) => document.getElementById(id);
      const n = (id) => col(id).querySelectorAll('.wi[data-v]').length;
      return {
        sh: n('waitSH'), sm: n('waitSM'), ss: n('waitSS'),
        eh: n('waitEH'), em: n('waitEM'), es: n('waitES'),
      };
    })()`);
    check('   开始拨轮：时 24 / 分 60 / 秒 60', wheelStruct.sh === 24 && wheelStruct.sm === 60 && wheelStruct.ss === 60,
      JSON.stringify(wheelStruct));
    check('   结束拨轮：时 24 / 分 60 / 秒 60', wheelStruct.eh === 24 && wheelStruct.em === 60 && wheelStruct.es === 60);

    // 结束必须晚于开始。把开始设为「今天 + 未来某刻」，结束设为「更早」→ 应禁用
    console.log('\n④ 结束必填且晚于开始');
    const badOrder = await ev(`(() => {
      // 开始 = 今天 20:00:00
      document.querySelector('#waitStartSeg button[data-day="0"]').click();
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('waitSH', 20); set('waitSM', 0); set('waitSS', 0);
      // 结束 = 今天 08:00:00（早于开始）
      document.querySelector('#waitEndSeg button[data-day="0"]').click();
      set('waitEH', 8); set('waitEM', 0); set('waitES', 0);
      return true;
    })()`);
    await sleep(400);
    const badRes = await ev(`(() => {
      const echo = document.getElementById('waitEcho');
      return { cls: echo.className, txt: echo.textContent.replace(/\\s+/g,' ').trim(),
               disabled: document.getElementById('btnStartWait').disabled };
    })()`);
    check('⭐⭐ 结束早于开始 → 回显告警 + 「开始蹲课」禁用',
      /\bwarn\b/.test(badRes.cls) && badRes.disabled === true, `${badRes.cls} disabled=${badRes.disabled}`);

    // 结束晚于开始 → 按钮可用
    const goodOrder = await ev(`(() => {
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('waitEH', 22); set('waitEM', 30); set('waitES', 0);
      return true;
    })()`);
    await sleep(400);
    const goodRes = await ev(`(() => {
      const echo = document.getElementById('waitEcho');
      return { cls: echo.className, disabled: document.getElementById('btnStartWait').disabled };
    })()`);
    check('   结束晚于开始 → 回显 info + 按钮可用（清单为空时仍禁用）',
      /\binfo\b/.test(goodRes.cls), `cls=${goodRes.cls}`);
    // 清单为空 → 即便时间合法，按钮也禁用
    check('   清单为空 → 「开始蹲课」仍禁用（没有要蹲的课）', goodRes.disabled === true);

    // ================= ⑤ 从搜索结果「+ 蹲」加入清单 =================
    console.log('\n⑤ 「+ 蹲」按钮：从教学班加进蹲课清单');
    // 先验证 addToWait / isInWait / removeFromWait 这条「+ 蹲」点击链路的核心函数
    // （与 addToPlan 对称，直接在页面上下文驱动，等价于点教学班行的「+ 蹲」）。
    const addRes = await ev(`(async () => {
      const course = { kch_id: 'WT1', kcmc: '蹲课测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      const jxb = { do_id: 'DO_WT1', jxb_id: 'JXB_WT1', xf: '2.0', teacher_text: '测老师',
                    sksj_text: '周一 3-5节', slots: [] };
      addToWait(course, jxb);
      await syncWait();
      return { n: S.wait.length, inWait: isInWait(jxb, 'WT1') };
    })()`);
    check('⭐⭐ 「+ 蹲」链路：addToWait 把课加进 S.wait，isInWait 判为已蹲',
      addRes.n === 1 && addRes.inWait === true, JSON.stringify(addRes));

    // 再次 addToWait **同一个班**（do_id 相同）→ 判重拒绝（仍 1 项）
    const dup = await ev(`(async () => {
      const course = { kch_id: 'WT1', kcmc: '蹲课测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      const jxb2 = { do_id: 'DO_WT1', jxb_id: 'JXB_WT1', xf: '2.0', teacher_text: '测老师',
                     sksj_text: '周一 3-5节', slots: [] };
      addToWait(course, jxb2);
      await syncWait();
      return S.wait.length;
    })()`);
    check('   同一个班重复「+ 蹲」→ 判重拒绝（仍 1 项）', dup === 1, String(dup));

    // ⭐ 同课**不同班**可以共存（2026-10-01 用户要求：蹲课允许同课不同班）
    const multi = await ev(`(async () => {
      const course = { kch_id: 'WT1', kcmc: '蹲课测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      addToWait(course, { do_id: 'DO_WT1b', jxb_id: 'JXB_WT1b', xf: '2.0', teacher_text: '测老师2',
                          sksj_text: '周一 5-6节', slots: [] });
      await syncWait();
      return S.wait.length;
    })()`);
    check('   同课不同班 → 共存（共 2 项）', multi === 2, String(multi));

    // removeFromWait 按 do_id 精确移除（同课其他班保留）
    const del = await ev(`(async () => {
      removeFromWait('DO_WT1', '蹲课测试课');   // 删 DO_WT1，保留 DO_WT1b
      await syncWait();
      return { n: S.wait.length, dos: S.wait.map((p) => p.do_id) };
    })()`);
    check('   按 do_id 精确移除 → 同课另一班保留（剩 1 项 DO_WT1b）',
      del.n === 1 && del.dos.join() === 'DO_WT1b', JSON.stringify(del));

    // 直接向后端铺蹲课清单（绕过搜索 UI，验证清单渲染）。
    const items = [
      { kch_id: 'W1', kcmc: '蹲课演示甲', jsxx: '张老师', xf: '2.0', slots: [] },
      { kch_id: 'W2', kcmc: '蹲课演示乙', jsxx: '李老师', xf: '3.0', slots: [] },
    ];
    // 需要一个未来的开始 + 更晚的结束
    const now = Date.now();
    const start = new Date(now + 3600 * 1000);   // +1h
    const end = new Date(now + 7200 * 1000);     // +2h
    const fmt = (d) => `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())} `
      + `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    const rw = await jpost('/api/wait/plan', {
      items, interval_ms: 800, start_at: fmt(start), deadline_at: fmt(end),
    });
    check('   后端 /api/wait/plan 铺清单成功', rw.code === 200, `${rw.code} ${rw.body.slice(0,120)}`);
    const wb = JSON.parse(rw.body);
    check('   返回 wait_rev + interval_ms + start_at + deadline_at',
      typeof wb.wait_rev === 'number' && wb.interval_ms === 800
      && typeof wb.start_at === 'number' && typeof wb.deadline_at === 'number',
      JSON.stringify({ rev: wb.wait_rev, iv: wb.interval_ms, s: wb.start_at, d: wb.deadline_at }));

    // 触发前端 hydrate（重新拉 state），清单应渲染 2 项
    await ev(`refreshState()`);
    await sleep(500);
    const listRes = await ev(`(() => {
      const box = document.getElementById('waitList');
      const items = [...box.querySelectorAll('.item')];
      return { n: items.length, count: document.getElementById('waitCount').textContent,
               titles: items.map((it) => (it.querySelector('.title') || {}).textContent) };
    })()`);
    check('⭐⭐ 蹲课清单渲染 2 项，标题正确',
      listRes.n === 2 && listRes.titles.join('|') === '蹲课演示甲|蹲课演示乙',
      JSON.stringify(listRes));

    // 间隔档 + 时间从后端回填到前端 S
    const hy = await ev(`(() => ({
      iv: S.waitInterval, startAt: S.waitStartAt, running: S.waitRunning,
    }))()`);
    check('   间隔档回填 800（跟后端一致）', hy.iv === 800, String(hy.iv));
    check('   开始时刻回填（unix 秒非空）', typeof hy.startAt === 'number' && hy.startAt > 0, String(hy.startAt));

    // ================= ⑥ 点「开始蹲课」→ 后端跑起来 =================
    console.log('\n⑥ 点「开始蹲课」（立即开始）');

    // ⭐ 关键：按钮 `btnStartWait` 的 onclick 会**自己** `waitBody()` → POST /api/wait/plan
    //    → POST /api/wait/start → subscribe()。所以这一节要验的是「点按钮这条完整链路」，
    //    不能再用脚本直连 http 铺清单（那会绕过前端 S.waitRev 的同步，按钮再 POST 时
    //    版本对不上 → 409 → 走不到 start）。正确做法：全走前端 UI。
    await jpost('/api/wait/stop', {});
    await jpost('/api/wait/clear', {});
    await ev(`(async () => {
      // 清空前端本地清单 + 对齐后端（clear 只清了后端，前端要 hydrate 才同步）
      S.wait = [];
      await refreshState();
      // 通过前端 addToWait 加一门课（走 UI 链路，S.waitRev 会同步）
      const course = { kch_id: 'WT1', kcmc: '蹲课测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      addToWait(course, { do_id: 'DO_WT1', jxb_id: 'JXB_WT1', xf: '2.0',
                          teacher_text: '测老师', sksj_text: '周一 3-5节', slots: [] });
      // 显式选 0.8s 档（hydrate 会用后端值覆盖 S.waitInterval，这里钉回 800 再同步）
      document.querySelector('#waitIntervalSeg button[data-ms="800"]').click();
      await syncWait();
      // 拨轮：开始 = 立即（保持 'now'），结束 = 今天 23:59:59
      const segNow = document.querySelector('#waitStartSeg button[data-day="now"]');
      if (segNow) segNow.click();
      document.querySelector('#waitEndSeg button[data-day="0"]').click();
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('waitEH', 23); set('waitEM', 59); set('waitES', 59);
      syncWaitAt();
      return { n: S.wait.length, rev: S.waitRev, startDisabled: document.getElementById('btnStartWait').disabled };
    })()`);
    await sleep(500);

    // 点按钮 —— 走完整「POST 清单 + start + subscribe」链路
    await ev(`document.getElementById('btnStartWait').click()`);
    await sleep(2000);
    const st1 = await state();
    check('⭐⭐ 后端 wait_running = true（蹲课任务真的启动了）', st1.wait_running === true);
    check('   后端 wait_retry_mode = round_robin（固定轮流发送）', st1.wait_retry_mode === 'round_robin');
    check('   后端 wait_interval_ms = 800（用户选的那档）', st1.wait_interval_ms === 800);

    // ⭐ 本轮修复：开始蹲课后「日志持续输出」+「停止按钮不立即变暗」
    //   （曾因 btnStartWait 没 subscribe() + FULL 被当终态失败 → 无日志且秒停）
    await sleep(3000);
    const logState = await ev(`(() => {
      const box = document.getElementById('log');
      return {
        text: box ? box.innerText : '',
        stopDisabled: document.getElementById('btnStopWait').disabled,
        frontRunning: S.waitRunning,
      };
    })()`);
    check('⭐⭐ 开始后日志面板有内容（subscribe 已接上，不再空白）',
      logState.text && logState.text.trim().length > 0, JSON.stringify(logState.text).slice(0, 120));
    check('⭐⭐ 日志里有「继续蹲退课名额」或 attempt/retry 字样（蹲课在持续发请求）',
      /继续蹲退课名额|retry|attempt|蹲课/.test(logState.text), logState.text.slice(0, 160));
    check('⭐⭐ 停止按钮**没有**立即变暗（任务还在蹲，不是秒停）',
      logState.stopDisabled === false, 'stopDisabled=' + logState.stopDisabled);
    check('   前端 S.waitRunning 仍为 true（与后端一致）', logState.frontRunning === true);

    await jpost('/api/wait/stop', {});
    await sleep(800);

    check('全程没有 JS 未捕获异常', errors.length === 0, errors.slice(0, 3).join(' | '));
  } catch (e) {
    console.log('   ✗ 异常：' + (e && e.message ? e.message : e));
    fails.push('异常：' + (e && e.message ? e.message : e));
  } finally {
    try { await jpost('/api/wait/stop', {}); } catch (_) {}
    chrome.kill();
    await sleep(400);
    console.log('='.repeat(64));
    console.log(fails.length ? `===== 失败 ${fails.length} 条 =====` : '===== 全部通过 =====');
    for (const f of fails) console.log('   ✗ ' + f);
    process.exit(fails.length ? 1 : 0);
  }
})();
