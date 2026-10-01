// 无头浏览器验收：把「在教务网站点了退出登录 → 顶栏自动改口」这件事验到底。
//
// 为什么非要有这个脚本：用户报的 bug 就是**界面**上的一句话
// （「教务都退出登录了，顶部固定栏还显示已登录」）。前端的服务端自检只能证明
// 接口语义，证明不了「页面上那枚胶囊最后真的变了」—— 那必须在真浏览器里跑。
//
// 场景（三步，全自动）：
//   ① 迷你假教务处于「已登录」→ 打开页面 → 断言胶囊是「已登录」；
//   ② 控制口把假教务切成「已退出」（302 回登录页，与真实教务同形态）；
//   ③ **什么都不点**，只等前端自己的定期探活（45s）→ 断言胶囊变成「登录已失效」。
//
// 用法：
//   XK_BASE=http://127.0.0.1:8721 XK_FAKE=http://127.0.0.1:8799 node tests/verify_session_pill.js
const http = require('http');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9787);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const FAKE = (process.env.XK_FAKE || 'http://127.0.0.1:8799').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_verify_pill_profile');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
function check(name, cond, detail = '') {
  console.log((cond ? '   ✓ ' : '   ✗ ') + name + (cond ? '' : '   ' + detail));
  if (!cond) fails.push(name);
}

function urlReq(u, method = 'GET') {
  return new Promise((res) => {
    const uo = new URL(u);
    const r = http.request(
      { host: uo.hostname, port: uo.port, path: uo.pathname + uo.search, method, timeout: 20000 },
      (x) => {
        let d = '';
        x.on('data', (c) => (d += c));
        x.on('end', () => res({ code: x.statusCode, body: d }));
      },
    );
    r.on('error', (e) => res({ code: 0, body: String(e.code || e) }));
    r.on('timeout', () => { r.destroy(); res({ code: 0, body: 'timeout' }); });
    r.end();
  });
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

// 读一枚胶囊的可见文字（并带上 class，用来确认颜色档位）
const PILL_JS = (id) => `(() => { const e = document.getElementById('${id}');
  return e ? (e.textContent.trim() + '|' + e.className) : 'MISSING'; })()`;

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
  console.log('='.repeat(62));
  try {
    // ---- ① 假教务先处于「已登录」 ----
    console.log('\n① 假教务处于「已登录」');
    await urlReq(`${FAKE}/__login`);
    const m0 = await urlReq(`${FAKE}/__mode`);
    check('假教务就绪（模式 = ok）', m0.body.trim() === 'ok', m0.body.slice(0, 40));

    const st0 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    check('界面已建立会话', st0.has_session === true, JSON.stringify(st0).slice(0, 120));

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
      return r.result && r.result.result ? r.result.result.value : undefined;
    };

    // 等页面把状态栏画出来
    let sess1 = '';
    for (let i = 0; i < 60; i++) {
      sess1 = await ev(PILL_JS('pillSession'));
      if (sess1 && sess1 !== 'MISSING' && !sess1.startsWith('未登录')) break;
      await sleep(500);
    }
    console.log(`   #pillSession = ${sess1}`);
    check('⭐ 打开页面时胶囊是「已登录」（且带 ok 色档）',
          sess1.startsWith('已登录') && sess1.includes('ok'), sess1);
    check('页面没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));
    check('控制台没有 error 级输出', consoleErrs.length === 0, consoleErrs.slice(0, 2).join(' | '));

    const open1 = await ev(PILL_JS('pillOpen'));
    console.log(`   #pillOpen    = ${open1}`);

    // ---- ② 模拟「用户去教务网站点了退出登录」 ----
    console.log('\n② 把假教务切成「已退出」（302 → 登录页，与真实教务同形态）');
    await urlReq(`${FAKE}/__logout`);
    check('假教务已切到退出态', (await urlReq(`${FAKE}/__mode`)).body.trim() === 'logged_out');
    console.log('   现在**什么都不点**，等前端自己的定期探活（每 45s 一次）…');

    // ---- ③ 只等，不做任何操作 ----
    let sess2 = sess1;
    const t0 = Date.now();
    while ((Date.now() - t0) / 1000 < 80) {
      sess2 = await ev(PILL_JS('pillSession'));
      if (sess2.includes('失效')) break;
      await sleep(1000);
    }
    const waited = ((Date.now() - t0) / 1000).toFixed(1);
    console.log(`   （等了 ${waited} 秒）#pillSession = ${sess2}`);

    check(`⭐ 胶囊自动变成「登录已失效」（等了 ${waited}s，全程没点任何东西）`,
          sess2.startsWith('登录已失效'), sess2);
    check('  且带 err 色档（红）', sess2.includes('err'), sess2);

    const open2 = await ev(PILL_JS('pillOpen'));
    console.log(`   #pillOpen    = ${open2}`);
    check('⭐ 同时「选课已开放/未到选课期」改口成「状态未知」'
          + '（不能一边说登录失效、一边说状态正常）',
          open2.startsWith('状态未知'), open2);

    const st2 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    check('服务端也记为 expired', st2.session_state === 'expired', st2.session_state);
    check('  且仍保留凭据对象（has_session=True，用于告诉用户用的是哪份凭据）',
          st2.has_session === true);
    check('  且记下了失效原因', !!st2.session_invalid_msg, st2.session_invalid_msg);

    const logs = await ev(`document.getElementById('log').textContent`);
    check('⭐ 日志里明确告诉用户发生了什么',
          typeof logs === 'string' && logs.includes('登录态已失效'), String(logs).slice(0, 200));

    check('全程没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));
    ws.close();
  } catch (e) {
    check('脚本自身执行', false, String(e && e.stack || e).slice(0, 300));
  } finally {
    try { chrome.kill(); } catch (_) {}
  }

  console.log('\n' + '='.repeat(62));
  if (fails.length) {
    console.log(`✗ ${fails.length} 条未通过：`);
    fails.forEach((f) => console.log('   -', f));
    process.exit(1);
  }
  console.log('✓ 全部通过');
})();
