// 无头浏览器验收：落盘快照一定要**用户主动点**才进内存，而且点了真的能看课。
//
// 背景（用户 2026-09-30 要求）：落盘的数据平时不用调用。以前服务一启动就自动
// 把上次的清单塞回内存 —— 用户不知道它什么时候冒出来（可能是上一学期的、
// 可能是上轮已经抢完的），界面上突然多一份「不是现在我攒的」清单，
// 随手点「开始抢课」就拿着旧目标去打教务了。
// 现在改成：磁盘那份只当素材，**未开放期**给一条「上次保存的数据 …… [加载查看]」，
// 用户点了才进内存、才能看到课程。
//
// 场景（三步）：
//   ① 打开页面 → 清单区是空的，但出现「上次保存的数据：N 项」，界面没有任何异常；
//   ② 断言这一刻**内存里确实没有清单**（不是后台偷偷载入了）；
//   ③ 点「加载查看」→ 清单里真的出现了那几门课，那条提示随之消失。
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_snapshot_bar.js
const http = require('http');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9788);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_verify_snapshot_profile');

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
  console.log('='.repeat(62));
  try {
    const st = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    console.log('   前置：snapshot =', JSON.stringify(st.snapshot));
    console.log('   前置：内存 items =', (st.items || []).length);
    check('前置：磁盘上有快照，而内存里没有清单（服务刚起、没自动恢复）',
      (st.snapshot || {}).count >= 1 && (st.items || []).length === 0,
      JSON.stringify({ snap: st.snapshot, items: st.items }).slice(0, 200));

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

    // ---- ① 打开页面：清单空，但「上次保存的数据」条出现了 ----
    console.log('\n① 打开页面（启动不自动载入清单）');
    let bar = '';
    for (let i = 0; i < 40; i++) {
      bar = await ev(`(() => { const e = document.getElementById('snapshotBar');
        return (e && e.style.display !== 'none') ? e.textContent.replace(/\\s+/g, ' ').trim() : ''; })()`);
      const items = await ev(`document.querySelectorAll('#planList .planitem, #planList > *').length`);
      if (bar && items >= 0) break;
      await sleep(500);
    }
    console.log(`   #snapshotBar = ${bar}`);
    check('⭐ 清单区出现了「上次保存的数据」提示（不是自动载入）', bar.includes('上次保存的数据'), bar);
    check('  提示里写明有几项', /(\d+)\s*项/.test(bar), bar);
    check('  提示里有「加载查看」按钮',
      await ev(`!!document.getElementById('btnLoadSnapshot')`));

    const planCount = await ev(`document.getElementById('planCount').textContent`);
    console.log(`   #planCount = ${planCount}`);
    check('⭐ 清单本身仍然是空的（没有被偷偷载入）', String(planCount).startsWith('0'), String(planCount));

    const st2 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    check('⭐ 后端也确认内存里没有清单（不是前端藏起来了）',
      (st2.items || []).length === 0, JSON.stringify(st2.items).slice(0, 120));
    check('页面没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));
    check('控制台没有 error 级输出', consoleErrs.length === 0, consoleErrs.slice(0, 2).join(' | '));

    // ---- ② 点「加载查看」 ----
    console.log('\n② 点「加载查看」');
    await ev(`document.getElementById('btnLoadSnapshot').click()`);
    let cnt = '';
    for (let i = 0; i < 40; i++) {
      cnt = await ev(`document.getElementById('planCount').textContent`);
      const bn = await ev(`!!document.getElementById('btnLoadSnapshot')`);
      if (!String(cnt).startsWith('0') && !bn) break;
      await sleep(400);
    }
    console.log(`   #planCount = ${cnt}`);
    const planText = await ev(`document.getElementById('planList').textContent.replace(/\\s+/g, ' ').trim()`);
    console.log(`   #planList  = ${planText.slice(0, 120)}`);

    check('⭐ 加载后清单里真的出现了课程', !String(cnt).startsWith('0'), String(cnt));
    check('  课程名渲染到了清单里', planText.length > 0, planText.slice(0, 80));
    check('⭐ 加载后那条「上次保存的数据」提示消失（内存已有清单，磁盘那份就是它自己）',
      !(await ev(`!!document.getElementById('btnLoadSnapshot')`)));
    check('「开始抢课」按钮解禁（清单非空 → 可以抢）',
      (await ev(`!document.getElementById('btnStart').disabled`)) === true);

    const st3 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    check('⭐ 后端状态同步（清单进内存了）', (st3.items || []).length >= 1,
      JSON.stringify(st3.items).slice(0, 120));
    check('  且版本号相对加载前变大（前端靠它触发重新载入）',
      typeof st3.plan_rev === 'number' && st3.plan_rev > (st.plan_rev || 0),
      `${st.plan_rev} → ${st3.plan_rev}`);
    check('加载过程没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));

    const logs = await ev(`document.getElementById('log').textContent`);
    check('日志说明了「已加载上次保存的数据」（用户得知道刚才发生了什么）',
      typeof logs === 'string' && logs.includes('已加载上次保存的数据'),
      String(logs).slice(-200));

    ws.close();
  } catch (e) {
    check('脚本自身执行', false, String((e && e.stack) || e).slice(0, 300));
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
