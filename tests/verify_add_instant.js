// 无头浏览器验收：加入清单 / 蹲课后，教学班行按钮**秒显**翻转（2026-10-01 修复）。
//
// 问题来源（用户反馈）：
//   「现在加入选课清单会卡一下再显示已加入」
//   「加入蹲课名单则有时候显示已蹲有时候不显示」
//
// 根因：
//   · addToPlan 只 fire-and-forget 调 commitPlan，教学班行的「✓ 已加入」要等
//     commitPlan → syncPlan(POST) → refreshPlanConflicts(POST) → refreshConflictsForExpanded(POST)
//     三趟网络串行跑完才重画 → 卡一下。
//   · addToWait 干脆没重画教学班列表，「✓ 已蹲」只靠 3 秒轮询 → 时有时无。
// 修法：新增 rerenderExpanded()（纯本地重画按钮态，不打网络），在 add/remove 后**同步**调用。
//
// 本脚本验证：调 addToPlan / addToWait 后，**同一同步 tick 内**（不等任何 await），
// 展开的教学班行按钮文案已经翻转成「✓ 已加入」/「✓ 已蹲」。
//
// 前置：XK_STATE_DIR=off python serve.py --port 8721 --mock
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_add_instant.js
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9802);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_add_instant_profile');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fails = [];
function check(name, cond, detail = '') {
  console.log((cond ? '   ✓ ' : '   ✗ ') + name + (cond ? '' : '   ' + detail));
  if (!cond) fails.push(name);
}

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
    let m = null; try { m = JSON.parse(raw.data); } catch (_) { return; }
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

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log('='.repeat(64));
  try {
    check('前置：打的是隔离实例', !/:8720(\/|$)/.test(BASE), BASE);
    let ver = null;
    for (let i = 0; i < 40; i++) { try { ver = await json('/json/version'); break; } catch (e) { await sleep(300); } }
    if (!ver) throw new Error('chrome 没起来');
    const tabs = await json('/json/list');
    const t = tabs.find((x) => x.type === 'page');
    const { send, errors } = await connect(t.webSocketDebuggerUrl);
    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      const ex = r.result && r.result.exceptionDetails;
      if (ex) throw new Error('页面异常：' + ((ex.exception && (ex.exception.description || ex.exception.value)) || ex.text));
      return r.result.result.value;
    };
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Page.navigate', { url: BASE + '/' });
    await sleep(2500);
    for (let i = 0; i < 40; i++) { if (await ev(`typeof S !== 'undefined'`)) break; await sleep(300); }
    check('前置：页面加载完成', await ev(`typeof S !== 'undefined' && typeof rerenderExpanded === 'function'`));

    // 造一个「展开的教学班列表」环境：直接往 S.expanded 塞一个 box，DOM 上放一个容器。
    // 然后调 addToPlan / addToWait，检查按钮文案是否**同一同步 tick**内翻转。
    console.log('\n① addToPlan 后「✓ 已加入」秒显（不等网络）');
    const r1 = await ev(`(() => {
      // 铺一个展开的教学班容器 + S.expanded 条目
      const host = document.getElementById('courseList');
      host.innerHTML = '<div id="cls-0"></div>';
      const box = document.getElementById('cls-0');
      const items = [
        { do_id: 'DO_1', jxb_id: 'JB_1', xf: '2.0', teacher_text: '张老师',
          sksj_text: '周一 3-5节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] },
      ];
      S.expanded.set('cls-0', { i: 0, kch: 'KCH_1', items, box });
      // 先画一次（未加入态：应是「+ 加入」/「+ 蹲」）
      renderClassRows(S.expanded.get('cls-0'));
      const before = box.querySelector('button[data-k]').textContent;
      const beforeW = box.querySelector('button[data-wk]').textContent;
      // 造课程对象，同步调 addToPlan（注意：它内部 fire-and-forget commitPlan，不 await）
      const course = { kch_id: 'KCH_1', kcmc: '测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      addToPlan(course, items[0]);
      // ⚠️ 关键：addToPlan 是同步的（commitPlan 是 fire-and-forget），
      // 所以这一行之后 rerenderExpanded 应该已经同步跑完，按钮文案应立即翻转。
      const after = box.querySelector('button[data-k]').textContent;
      return { before, beforeW, after };
    })()`);
    check('   加入前按钮是「+ 加入」', r1.before === '+ 加入', r1.before);
    check('⭐⭐ 调 addToPlan 后**同一同步 tick**内按钮变「✓ 已加入」（不再卡）',
      r1.after === '✓ 已加入', r1.after);

    console.log('\n② addToWait 后「✓ 已蹲」秒显（不再靠 3 秒轮询）');
    const r2 = await ev(`(() => {
      const box = document.getElementById('cls-0');
      const before = box.querySelector('button[data-wk]').textContent;
      const course = { kch_id: 'KCH_1', kcmc: '测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      const jxb = { do_id: 'DO_1', jxb_id: 'JB_1', xf: '2.0', teacher_text: '张老师',
                    sksj_text: '周一 3-5节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] };
      addToWait(course, jxb);
      const after = box.querySelector('button[data-wk]').textContent;
      return { before, after };
    })()`);
    check('   加入前按钮是「+ 蹲」', r2.before === '+ 蹲', r2.before);
    check('⭐⭐ 调 addToWait 后**同一同步 tick**内按钮变「✓ 已蹲」（不再时有时无）',
      r2.after === '✓ 已蹲', r2.after);

    console.log('\n③ removeFromPlan / removeFromWait 后按钮秒显翻回');
    const r3 = await ev(`(() => {
      const box = document.getElementById('cls-0');
      removeFromPlan('KCH_1', '测试课');
      const afterPlan = box.querySelector('button[data-k]').textContent;
      // 蹲课允许同课不同班 → 移除要传 do_id（精确删这一个班）
      removeFromWait('DO_1', '测试课');
      const afterWait = box.querySelector('button[data-wk]').textContent;
      return { afterPlan, afterWait };
    })()`);
    check('   移除后「✓ 已加入」同步翻回「+ 加入」', r3.afterPlan === '+ 加入', r3.afterPlan);
    check('   移除后「✓ 已蹲」同步翻回「+ 蹲」', r3.afterWait === '+ 蹲', r3.afterWait);

    console.log('\n④ 同课不同班都能加进蹲课清单（不再只押一个班）');
    const r4 = await ev(`(() => {
      const course = { kch_id: 'KCH_1', kcmc: '测试课', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      // 同课两个不同教学班
      addToWait(course, { do_id: 'DO_A', jxb_id: 'JB_A', xf: '2.0', teacher_text: '张老师',
                         sksj_text: '周一 3-5节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] });
      addToWait(course, { do_id: 'DO_B', jxb_id: 'JB_B', xf: '2.0', teacher_text: '李老师',
                         sksj_text: '周一 5-6节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] });
      // 同班重复加 → 应拒绝（仍 2 项）
      addToWait(course, { do_id: 'DO_A', jxb_id: 'JB_A', xf: '2.0', teacher_text: '张老师',
                         sksj_text: '周一 3-5节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] });
      return { n: S.wait.length, dos: S.wait.map((p) => p.do_id) };
    })()`);
    check('⭐⭐ 同课两个不同班都加进去了（共 2 项）', r4.n === 2 && r4.dos.join() === 'DO_A,DO_B',
      JSON.stringify(r4));

    console.log('\n⑤ 清空蹲课清单后「✓ 已蹲」翻回「+ 蹲」');
    const r5 = await ev(`(async () => {
      // 展开一个教学班容器，先加一个班让它显示「✓ 已蹲」
      const host = document.getElementById('courseList');
      host.innerHTML = '<div id="cls-1"></div>';
      const box = document.getElementById('cls-1');
      const items = [
        { do_id: 'DO_C', jxb_id: 'JB_C', xf: '2.0', teacher_text: '王老师',
          sksj_text: '周二 1-2节', is_full: false, yxzrs: 0, jxbrl: 50, slots: [] },
      ];
      S.expanded.set('cls-1', { i: 1, kch: 'KCH_2', items, box });
      renderClassRows(S.expanded.get('cls-1'));
      const course = { kch_id: 'KCH_2', kcmc: '测试课2', kklxdm: '01', xf: '2.0', cxbj: '0', fxbj: '0' };
      addToWait(course, items[0]);
      const afterAdd = box.querySelector('button[data-wk]').textContent;
      // 点「清空蹲课清单」按钮（btnClearWait.onclick 是 async，但先同步清 S.wait + rerenderExpanded）
      document.getElementById('btnClearWait').click();
      const afterClear = box.querySelector('button[data-wk]').textContent;
      return { afterAdd, afterClear };
    })()`);
    check('   清空前该班按钮是「✓ 已蹲」', r5.afterAdd === '✓ 已蹲', r5.afterAdd);
    check('⭐⭐ 点清空后按钮**同步**翻回「+ 蹲」', r5.afterClear === '+ 蹲', r5.afterClear);

    check('全程没有 JS 未捕获异常', errors.length === 0, errors.slice(0, 3).join(' | '));
  } catch (e) {
    console.log('   ✗ 异常：' + (e && e.message ? e.message : e));
    fails.push('异常：' + (e && e.message ? e.message : e));
  } finally {
    chrome.kill();
    await sleep(400);
    console.log('='.repeat(64));
    console.log(fails.length ? `===== 失败 ${fails.length} 条 =====` : '===== 全部通过 =====');
    for (const f of fails) console.log('   ✗ ' + f);
    process.exit(fails.length ? 1 : 0);
  }
})();
