// 无头浏览器验收：「派发方式」（serial / 轮流发送）选择器。
//
// 需求（用户 2026-10-01）：
//   「同时做 1+2+3 和做轮流发送两种模式让用户选择，也就是第一项发送后就轮二项发送，
//     以此类推这样就能确保在不能及时得到系统回应时，每个课程申请都能在短时间内
//     提交一遍给系统，但这要做好后续系统回应信息与对应课程匹配，确定是抢课成功还是失败」
//
// 为什么非要浏览器跑：这是**用户可见的选择器**，服务端自检证明不了
// 「它渲染出来没有、切换后有没有真的提交给后端、刷新后回填对不对」。
//
// 场景：
//   ① 选择器在「3 抢课清单」卡片里，默认串行，旁边有「什么时候该选哪个」的说明；
//   ② 切到「轮流发送」→ 说明文字跟着变，且**真的提交给了后端**（/api/state.retry_mode）；
//   ③ 刷新页面 → 下拉回填成「轮流发送」（证明它是落盘的，不是页面内存里的临时值）；
//   ④ 定时开抢后（运行中）→ 下拉被**禁用**（Plan 已经在跑，改了不生效 →
//      「显示的值必须等于正在执行的值」）；
//   ⑤ 停止 → 下拉恢复可用；
//   ⑥ ⭐ 清单为空时**不许**被误判成「后端版本较旧」（真实环境实测复现过的 bug：
//      空清单 → 后端曾下发 null → 前端 `typeof null === 'object'` → 误判老后端 → 禁用）；
//   ⑦ 「后端版本较旧」时的降级文案（页面比后端新：静态文件按需读盘先生效）；
//   ⑧ 全程没有 JS 异常。
//
// 前置：
//   · 界面实例在跑（本脚本自己建清单与启动，不碰 8720）：
//       XK_STATE_DIR=<临时目录> python serve.py --port 8721 --mock
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_dispatch_mode.js
const http = require('http');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9791);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_verify_dispatch_profile');

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

const jpost = (p, obj) => urlReq(`${BASE}${p}`, 'POST', JSON.stringify(obj),
  { 'Content-Type': 'application/json' });
const state = async () => JSON.parse((await urlReq(`${BASE}/api/state`)).body);

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

// 一次取全：选择器的位置/取值/可编辑性 + 说明文字。
const PROBE = `(() => {
  const sel = document.getElementById('optRetryMode');
  if (!sel) return { missing: true };
  const row = sel.closest('.row');
  const card = sel.closest('section.card');
  const hdr = card ? card.querySelector('header') : null;
  const hint = document.getElementById('modeHint');
  return {
    value: sel.value,
    options: Array.from(sel.options).map((o) => o.value),
    optionText: Array.from(sel.options).map((o) => o.textContent.trim()),
    disabled: !!sel.disabled,
    visible: sel.getBoundingClientRect().width > 0,
    inPlanCard: !!(card && /抢课清单/.test(hdr ? hdr.textContent : '')),
    sameRowAsStopFirst: !!(row && row.querySelector('#optStopFirst')),
    hint: hint ? hint.textContent.replace(/\\s+/g, ' ').trim() : '',
  };
})()`;

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log('='.repeat(62));
  try {
    // 前置：把清单铺好（两门课，定时开抢留到 ④ 用），并确认会话在
    const st0 = await state();
    console.log(`   服务端视角：session_state=${st0.session_state} inited=${st0.inited}`
      + ` 教务目标=${st0.source}`);
    check('前置：实例有会话（否则前端拿不到状态）', !!st0.has_session, JSON.stringify(st0.source));

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
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push(JSON.stringify(m.params.exceptionDetails).slice(0, 300));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push('console.error: ' + JSON.stringify(m.params.args).slice(0, 300));
      }
    });

    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
      return r.result && r.result.result ? r.result.result.value : undefined;
    };
    // 等**页面初始化完成**，而不是「HTML 解析到那个元素」。
    // ⚠️ 这两件事差着一次脚本执行：<select> 在 HTML 一解析出来就存在（还能点），
    //    而 app.js 是随后才执行的 —— 若在它执行前就探测，会得到
    //    「说明文字是空的、change 事件没人听」这种**假失败**（实测踩过）。
    //    判据取「说明文字已渲染」：只有 app.js 跑完了它才非空。
    async function load() {
      await send('Page.navigate', { url: `${BASE}/` });
      for (let i = 0; i < 60; i++) {
        const ready = await ev(`(() => {
          const h = document.getElementById('modeHint');
          const s = document.getElementById('optRetryMode');
          return !!(h && h.textContent.trim() && s
                    && typeof S !== 'undefined' && Array.isArray(S.plan));
        })()`);
        if (ready) return true;
        await sleep(300);
      }
      return false;
    }

    // 后端先写一份「两门课 + 串行」的清单（页面首次载入会把它读进来）
    const r0 = await jpost('/api/plan', {
      items: [
        { kch_id: 'VR1', kcmc: '验收甲课', do_id: 'doVR1', max_attempts: 3, interval_ms: 200 },
        { kch_id: 'VR2', kcmc: '验收乙课', do_id: 'doVR2', max_attempts: 3, interval_ms: 200 },
      ],
      retry_mode: 'serial',
    });
    check('前置：清单写入成功', r0.code === 200, `${r0.code} ${r0.body.slice(0, 160)}`);
    check('前置：页面加载完成', await load());

    // ---- ① 位置与默认值 ----
    console.log('\n① 选择器就在「3 抢课清单」里，默认串行');
    let info = await ev(PROBE);
    console.log(`   #optRetryMode = ${info.value} / [${info.optionText.join(' | ')}]`
      + ` disabled=${info.disabled}`);
    check('⭐ 选择器渲染出来了', !!(info && info.visible && !info.missing), JSON.stringify(info));
    check('⭐ 它在「3 抢课清单」卡片里、与「抢到一门即停」同一行',
      !!(info.inPlanCard && info.sameRowAsStopFirst), JSON.stringify(info));
    check('⭐ 两个选项就是「串行」与「轮流发送」',
      JSON.stringify(info.options) === '["serial","round_robin"]'
      && /串行/.test(info.optionText[0]) && /轮流发送/.test(info.optionText[1]),
      JSON.stringify(info.optionText));
    check('⭐ 默认是串行（保持旧行为，用户不动它就不会变）',
      info.value === 'serial', info.value);
    check('   旁边有「什么时候该选哪个」的说明（不是只给术语）',
      /串行/.test(info.hint) && /火力集中/.test(info.hint), info.hint.slice(0, 160));
    check('   运行前选择器可编辑', info.disabled === false, String(info.disabled));

    // ---- ② 切到轮流发送 → 说明变 + 真的提交给后端 ----
    console.log('\n② 切到「轮流发送」');
    await ev(`(() => { const s = document.getElementById('optRetryMode');
      s.value = 'round_robin';
      s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
    await sleep(1200);
    info = await ev(PROBE);
    const st2 = await state();
    console.log(`   说明: ${info.hint.slice(0, 110)}…`);
    console.log(`   服务端 retry_mode = ${st2.retry_mode}（plan_rev=${st2.plan_rev}）`);
    check('⭐ 说明文字跟着切到「轮流发送」的解释',
      /轮流发送/.test(info.hint) && /不等上一发回应/.test(info.hint), info.hint.slice(0, 200));
    check('⭐⭐ 切换**真的提交给了后端**（不是只改界面）', st2.retry_mode === 'round_robin',
      String(st2.retry_mode));
    check('   提交触发了清单版本号递增（乐观锁照常生效）',
      st2.plan_rev > (st0.plan_rev || 0), `${st0.plan_rev} → ${st2.plan_rev}`);

    // ---- ③ 刷新页面 → 回填 ----
    console.log('\n③ 刷新页面后回填成「轮流发送」（它是落盘的，不是页面内存值）');
    await load();
    for (let i = 0; i < 20; i++) {
      info = await ev(PROBE);
      if (info.value === 'round_robin') break;
      await sleep(300);
    }
    check('⭐ 刷新后页面显示的就是服务端那份模式（否则会出现「界面说轮流、实际在串行」）',
      info.value === 'round_robin', info.value);

    // ---- ④ 定时开抢（运行中）→ 禁用 ----
    console.log('\n④ 定时开抢中：选择器被禁用（Plan 已在跑，改了不生效）');
    const r4 = await jpost('/api/plan', {
      items: [
        { kch_id: 'VR1', kcmc: '验收甲课', do_id: 'doVR1', max_attempts: 3, interval_ms: 200 },
        { kch_id: 'VR2', kcmc: '验收乙课', do_id: 'doVR2', max_attempts: 3, interval_ms: 200 },
      ],
      retry_mode: 'round_robin',
      start_at: '+300',
    });
    console.log(`   POST /api/plan(+300) → ${r4.code}`);
    const r5 = await jpost('/api/start', {});
    console.log(`   POST /api/start → ${r5.code}`);
    check('前置：任务已启动', r5.code === 200, `${r5.code} ${r5.body.slice(0, 160)}`);
    let st4 = await state();
    for (let i = 0; i < 20 && !st4.running; i++) { await sleep(250); st4 = await state(); }
    check('⭐ 服务端确认正在跑（定时开抢已进入预热/倒计时）', st4.running === true,
      JSON.stringify({ running: st4.running }));
    for (let i = 0; i < 20; i++) {
      info = await ev(PROBE);
      if (info.disabled) break;
      await sleep(500);
    }
    check('⭐⭐ 运行中下拉被禁用 —— 界面显示的值永远等于正在执行的值（不许撒谎）',
      info.disabled === true, JSON.stringify(info));

    // ---- ⑤ 停止 → 恢复 ----
    console.log('\n⑤ 停止后选择器恢复可用');
    await jpost('/api/stop', {});
    for (let i = 0; i < 40; i++) {
      info = await ev(PROBE);
      if (info.disabled === false) break;
      await sleep(500);
    }
    check('⭐ 停止后可以改派发方式了', info.disabled === false, JSON.stringify(info));

    // ---- ⑥ 空清单也不能被误判成「后端版本较旧」（真实环境暴露的 bug） ----
    // 背景：`/api/state.retry_mode` 曾经在**清单为空**时返回 null，而前端拿
    // `typeof s.retry_mode === 'string'` 当「后端支不支持这个开关」的探针 ——
    // `typeof null === 'object'`，于是「新后端 + 空清单」（刚启动、还没加载落盘
    // 清单，是最常见的初始状态）被误判成老后端：下拉被禁用、拨回串行，
    // 还劝用户去重启一个本来就够新的服务。
    // 2026-10-01 在「新后端 + 真实教务 + 空清单」的隔离实例上实测复现。
    // 这条用例钉的是：清单空不空，和「后端支不支持」是两件事。
    console.log('\n⑥ 清单为空时不许被误判成「后端版本较旧」');
    await jpost('/api/plan', { items: [], retry_mode: 'serial' });
    const st6 = await state();
    console.log(`   空清单下 state.retry_mode = ${JSON.stringify(st6.retry_mode)}`
      + `  typeof=${typeof st6.retry_mode}  items=${(st6.items || []).length}`);
    check('⭐ 空清单时后端仍下发字符串 retry_mode（清单空 ≠ 后端旧）',
      typeof st6.retry_mode === 'string' && st6.retry_mode !== '',
      `typeof=${typeof st6.retry_mode} value=${JSON.stringify(st6.retry_mode)}`);
    await load();
    for (let i = 0; i < 20; i++) {
      info = await ev(PROBE);
      if (info.hint) break;
      await sleep(300);
    }
    console.log(`   空清单下：disabled=${info.disabled}  modeSupported=${await ev('S.modeSupported')}`);
    check('⭐ 空清单下选择器仍可用（新后端不许被当成老后端）',
      info.disabled === false, JSON.stringify(info));
    check('   说明文字不是「版本较旧」的降级文案',
      !/版本较旧/.test(info.hint), info.hint.slice(0, 140));
    // 还原成前面那份两门课的清单，别把状态留给后面的用例去猜
    await jpost('/api/plan', {
      items: [
        { kch_id: 'VR1', kcmc: '验收甲课', do_id: 'doVR1', max_attempts: 3, interval_ms: 200 },
        { kch_id: 'VR2', kcmc: '验收乙课', do_id: 'doVR2', max_attempts: 3, interval_ms: 200 },
      ],
      retry_mode: 'serial',
    });

    // ---- ⑦ 无 JS 异常 ----
    console.log('\n⑦ 「后端版本较旧」时的降级文案（页面比后端新：静态文件按需读盘先生效）');
    const degraded = await ev(`(() => {
      const keep = S.modeSupported;
      S.modeSupported = false;
      renderModeHint();
      const txt = document.getElementById('modeHint').textContent;
      S.modeSupported = keep;
      renderModeHint();
      return txt;
    })()`);
    console.log(`   降级文案: ${String(degraded).slice(0, 90)}…`);
    check('⭐ 后端不认这个开关时会如实说明并让人重启（不许装作能用）',
      /重启/.test(String(degraded)) && /串行/.test(String(degraded)), String(degraded).slice(0, 160));
    check('   恢复后可正常渲染（不残留在降级文案上）',
      /串行|轮流发送/.test(info.hint), info.hint.slice(0, 60));

    console.log('\n⑧ JS 异常');
    check('全程没有 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

    ws.close();
  } catch (e) {
    check('脚本未抛异常', false, String(e && e.message || e));
  } finally {
    chrome.kill();
    console.log('\n' + '='.repeat(62));
    if (fails.length) {
      console.log(`✗ ${fails.length} 条未通过：`);
      fails.forEach((f) => console.log('   - ' + f));
      process.exit(1);
    }
    console.log('✓ 派发方式选择器 浏览器验收全部通过');
    process.exit(0);
  }
})();
