// 无头浏览器验收：「实时日志」不许在第二轮 / 服务重启后变成空白。
//
// 需求来源（用户 2026-10-01）：
//   「串行模式实时日志里怎么不显示了」
//
// 根因**不是串行**，是事件序号：
//   ① `RUNTIME.start()` 里曾有一句 `self._seq = 0` —— 每点一次「开始」序号都从 1
//      重来，而浏览器的游标 `S.seq` 还停在上一次的最后一号。SSE 那边
//      `seq > since` 从此永不成立 → **第二轮日志整段不显示**（第一轮正常，
//      所以很难联想到序号）。
//   ② 服务重启同理：`_seq` 是**进程内**的，而前端游标能跨进程活下来。
//
// 修法（三处，各管一头）：
//   · 后端 `RUNTIME.start()`：只清 `_events`（面板只显示本轮），**不清 `_seq`**；
//   · 后端入口 `RUNTIME.clamp_since()`：陈旧游标（`since > seq`）夹回 0 → 重放；
//     ⚠️ 必须夹在 `/api/events` 与 `_sse` 的**入口**，不能只在 `events_since()` 里
//     （调用方会 `last = max(last, ev.seq)` 把那个大数一直带着走）；
//   · 前端「开始」处理器：清空日志面板后 `S.seq = 0` 再订阅（本轮从头显示）。
//     ⚠️ 只在「开始」归零；SSE 断线重连**不许**归零，否则会重放出重复行。
//
// 为什么非要浏览器跑：这是**用户看得见的那块面板**。接口层断言「事件取得到」
// 证明不了「面板上真的出现字」——它们之间还隔着 handleEvent → logLine 一层。
//
// 前置：界面实例在跑，且**有活会话**（要 POST /api/start）。
//   XK_STATE_DIR=<临时目录> $PY serve.py --real --port 8722 &
//   <注入登录态：POST /api/session/cdp {"port":9666} 或 POST /api/session>
//
// 用法：XK_BASE=http://127.0.0.1:8722 node tests/verify_log_cursor.js
//
// ⚠️ 本脚本会 POST /api/plan + /api/start，**但一律用定时 `+300` 秒**：只走预热相，
//    绝不进入开火相，所以不会往真教务发任何提交。跑完会 POST /api/stop。
// ⚠️ 仍建议打**隔离实例**（8722 / 8721），别打用户正在用的 8720。
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9798);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_log_cursor_profile');
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
  '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
  '--proxy-server=direct://', '--proxy-bypass-list=*',
  '--window-size=1420,1100', 'about:blank',
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
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    const on = (ev) => { const m = JSON.parse(ev.data); if (m.id === mid) { ws.removeEventListener('message', on); res(m); } };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ws, send };
}

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log('='.repeat(62));
  try {
    const st0 = await state();
    console.log(`   服务端视角：session_state=${st0.session_state} inited=${st0.inited}`
      + ` 教务目标=${st0.source} seq=${st0.seq}`);
    check('前置：实例有活会话（否则 /api/start 会 400）', !!st0.has_session,
      JSON.stringify({ has_session: st0.has_session }));
    if (!st0.has_session) throw new Error('没有会话，先注入登录态再跑');

    let ver = null;
    for (let i = 0; i < 40; i++) {
      try { ver = await json('/json/version'); break; } catch (e) { await sleep(300); }
    }
    if (!ver) throw new Error('chrome 没起来');
    const tabs = await json('/json/list');
    const t = tabs.find((x) => x.type === 'page');
    const { ws, send } = await connect(t.webSocketDebuggerUrl);
    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      const ex = r.result && r.result.exceptionDetails;
      if (ex) throw new Error(JSON.stringify(ex));
      return r.result.result.value;
    };
    await send('Page.enable');

    // ---- 先让服务端「已经跑过一轮」：进程里的 seq 不再是 0 ----
    // ⚠️ 定时 +300 秒 → 只预热、不开火，绝不往真教务发提交。
    const r0 = await jpost('/api/plan', {
      items: [{ kch_id: 'VRLOG', kcmc: '日志探针课', do_id: 'doVRLOG',
                max_attempts: 3, interval_ms: 200 }],
      retry_mode: 'serial', start_at: '+300',
    });
    const r1 = await jpost('/api/start', {});
    check('前置：任务已启动（定时，不会开火）', r0.code === 200 && r1.code === 200,
      `${r0.code}/${r1.code} ${r1.body.slice(0, 120)}`);
    await sleep(3000);
    await jpost('/api/stop', {});
    await sleep(600);
    let st = await state();
    const seqRound1 = st.seq;
    console.log(`   第 1 轮跑完：服务端 seq = ${seqRound1}`);
    check('⭐ 序号真的推进了（否则后面测不出「游标超车」）', seqRound1 > 0, `seq=${seqRound1}`);

    // ---- 打开页面 ----
    await send('Page.navigate', { url: BASE + '/' });
    await sleep(2500);
    let ready = false;
    for (let i = 0; i < 40; i++) {
      ready = await ev(`typeof S !== 'undefined' && Array.isArray(S.plan) && S.plan.length > 0`);
      if (ready) break;
      await sleep(300);
    }
    check('前置：页面加载完成且已读到清单', ready);

    // ---- ① 陈旧游标直接打 SSE：服务端必须夹回 0 并重放 ----
    console.log('\n① 陈旧游标（模拟「上一个服务进程留下的 since」）');
    const A = await ev(`new Promise((res) => {
      let n = 0, seq = 0;
      const es = new EventSource('/api/events/stream?since=999999');
      es.onmessage = (m) => {
        try { const e = JSON.parse(m.data); n++; seq = Math.max(seq, e.seq || 0); } catch (_) {}
      };
      setTimeout(() => { es.close(); res({ messages: n, lastSeq: seq }); }, 2500);
    })`);
    console.log(`   SSE(since=999999) 收到 ${A.messages} 条，最大序号 ${A.lastSeq}`);
    check('⭐ 后端夹掉陈旧游标：since 超车时仍然推事件（不再永远空白）',
      A.messages > 0, `messages=${A.messages}`);
    check('   推的还是真实序号（没被伪造）',
      A.lastSeq > 0 && A.lastSeq <= seqRound1, `lastSeq=${A.lastSeq} seq1=${seqRound1}`);

    // ---- ② 复刻用户操作：游标是「旧的」，然后点「开始」 ----
    console.log('\n② 带着陈旧游标点「开始」→ 实时日志必须出字');
    await ev(`(() => {
      S.seq = 999999;                                    // 上一次服务进程留下的游标
      document.getElementById('log').innerHTML = '';
      document.getElementById('startAt').value = '+300';  // 定时：只预热，不开火
    })()`);
    const before = await ev(`({ seq: S.seq, lines: document.getElementById('log').children.length })`);
    await ev(`document.getElementById('btnStart').click()`);
    await sleep(4000);
    let B = await ev(`({ lines: document.getElementById('log').children.length, seq: S.seq,
                           txt: document.getElementById('log').textContent.replace(/\\s+/g,' ') })`);
    st = await state();
    console.log(`   点击前：S.seq=${before.seq}，日志 ${before.lines} 行`);
    console.log(`   点击后：日志 ${B.lines} 行，S.seq=${B.seq}，服务端 seq=${st.seq}`);
    console.log(`   首行：${B.txt.slice(0, 80)}…`);
    check('⭐⭐ 实时日志**不再是空的**（这就是用户报的现象）', B.lines > 0, `lines=${B.lines}`);
    // ⚠️ 别断言「派发方式」那行 —— 它只在清单**多于一项**时才打（见 runner._dispatch）。
    check('⭐ 显示的是**本轮**内容，不是上一轮倒灌',
      /开始执行计划/.test(B.txt) && /重抓选课上下文|上下文就绪/.test(B.txt), B.txt.slice(0, 120));
    check('   游标归零后正常推进，追上服务端序号',
      B.seq === st.seq && B.seq > 0, `S.seq=${B.seq} srv=${st.seq}`);

    // ---- ③ 再点一次「开始」：第二轮同样必须出字（`_seq` 不许归零） ----
    // ⚠️ 必须先停下来：运行中 `btnStart` 是 disabled 的，直接 click 等于什么都没做
    //    （第一版就这么写的，于是拿到「34 → 34」的假失败）。
    console.log('\n③ 停掉后紧接第二轮（同一进程内再点一次「开始」）');
    await jpost('/api/stop', {});
    for (let i = 0; i < 40; i++) {
      if (await ev(`!document.getElementById('btnStart').disabled`)) break;
      await sleep(400);
    }
    const stStop = await state();
    const seqBefore2 = stStop.seq;
    check('前置：已停止，按钮恢复可点', stStop.running === false, `running=${stStop.running}`);
    await ev(`document.getElementById('btnStart').click()`);
    await sleep(4500);
    B = await ev(`({ lines: document.getElementById('log').children.length, seq: S.seq,
                       txt: document.getElementById('log').textContent.replace(/\\s+/g,' ') })`);
    st = await state();
    console.log(`   日志 ${B.lines} 行，S.seq=${B.seq}，服务端 seq=${st.seq}（上一轮末 ${seqBefore2}）`);
    check('⭐⭐ 第二轮日志照样出字（`_seq` 没有归零）', B.lines > 0, `lines=${B.lines}`);
    check('⭐ 服务端序号跨轮**单调递增**（归零过就会掉回去）',
      st.seq > seqBefore2, `${seqBefore2} → ${st.seq}`);
    check('   面板被清过并重新从第一条开始（没有两轮混在一起）',
      B.lines > 0 && B.lines <= st.seq, `lines=${B.lines} seq=${st.seq}`);
    // 缓冲里只留本轮（`start()` 清过 `_events`），但序号**不归零** ——
    // 所以「since=0 取回的事件」应当**全部**大于上一轮末的序号。
    // 归零过的话，这里会出现 1、2、3… 这种掉回去的小序号。
    const buf = JSON.parse((await urlReq(`${BASE}/api/events?since=0`)).body).events || [];
    console.log(`   事件缓冲 ${buf.length} 条，序号范围 `
      + `${buf.length ? buf[0].seq : '-'} … ${buf.length ? buf[buf.length - 1].seq : '-'}`);
    check('⭐ 事件缓冲只留本轮，且序号全部大于上一轮末（序号全程单调）',
      buf.length > 0 && buf.every((e) => e.seq > seqBefore2),
      `min=${buf.length ? Math.min(...buf.map((e) => e.seq)) : '-'} 上一轮末=${seqBefore2}`);

    await jpost('/api/stop', {});

    await jpost('/api/stop', {});
    await sleep(300);
    ws.close();
  } catch (e) {
    check('脚本未抛异常', false, String((e && e.message) || e));
  } finally {
    chrome.kill();
    console.log('\n' + '='.repeat(62));
    if (fails.length) {
      console.log(`✗ ${fails.length} 条未通过：`);
      fails.forEach((f) => console.log('   - ' + f));
      process.exit(1);
    }
    console.log('✓ 实时日志游标 浏览器验收全部通过');
    process.exit(0);
  }
})();
