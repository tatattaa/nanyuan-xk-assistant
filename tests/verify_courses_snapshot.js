// 无头浏览器验收：「📂 上次数据」按钮 —— **位置**与**显示时机**。
//
// 需求（用户 2026-09-30，两条）：
//   ① 按钮要放在「2 搜索课程」**标题栏的右侧**（不是搜索输入框那一行里）；
//   ② 这个按钮**只在未开放状态显示** —— 开放期「搜索」是实时的，离线入口只会碍眼。
//
// 为什么非要浏览器跑：这两条都是纯粹的**界面**约定（位置、显隐），
// 服务端自检只能证明接口语义，证明不了「它此刻到底在不在页面上、在哪儿」。
//
// 场景（全自动，靠迷你假教务的选课期开关驱动）：
//   ① 未开放期 → 按钮出现，且确实在「2 搜索课程」标题栏里、贴右边缘；
//   ② 点它 → 课程列表里真出现上次搜到的课，并明确标注「这是上次保存下来的」；
//   ③ 切到开放期 → 前端自己的轮询把状态刷新后，按钮**消失**；
//   ④ 再切回未开放期 → 按钮**回来**（证明③不是「一次性隐藏」）；
//   ⑤ 清掉会话（状态未知）→ 按钮**也是隐藏的**（「未知」不算「未开放」）。
//
// 前置（缺一不可，脚本会自己检查前两条）：
//   · 迷你假教务在跑：            python tests/mini_fake_jw.py 8799
//   · 界面实例指向它、且**磁盘上有课程快照**（state 里要有 courses.json）：
//       XK_STATE_DIR=<临时目录> XK_SCHOOL_URL=http://127.0.0.1:8799/jwglxt/ \
//         python serve.py --port 8723
//   · 会话由本脚本自己建（POST /api/session，假教务不校验 Cookie）
//
// 用法：XK_BASE=http://127.0.0.1:8723 XK_FAKE=http://127.0.0.1:8799 node tests/verify_courses_snapshot.js
const http = require('http');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9789);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8723').replace(/\/$/, '');
const FAKE = (process.env.XK_FAKE || 'http://127.0.0.1:8799').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_verify_courses_profile');

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

// 假教务的控制口：切换「选课期」，并**同步**让界面实例知道（打一次探活）。
// ⚠️ 界面实例只在 init / 探活时才知道教务开没开放（`sess.is_open` 是个缓存），
//    所以这里必须真的打一次 `/api/session/check`；之后前端 3 秒轮询就会刷新界面。
async function setPeriod(closed) {
  const r = await urlReq(`${FAKE}${closed ? '/__close' : '/__open'}`);
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

// 按钮的「位置」与「显隐」一次取全 —— 断言全部基于这份快照，避免多次求值之间状态漂移。
const PROBE = `(() => {
  const b = document.getElementById('btnCoursesSnap');
  if (!b) return { missing: true };
  const hdr = b.parentElement;
  const card = hdr ? hdr.parentElement : null;
  const title = hdr ? hdr.textContent.replace(/\\s+/g, ' ').trim() : '';
  const br = b.getBoundingClientRect();
  const hr = hdr ? hdr.getBoundingClientRect() : { right: 0 };
  return {
    shown: b.style.display !== 'none' && br.width > 0,
    text: b.textContent.trim(),
    title_attr: b.title,
    parentTag: hdr ? hdr.tagName : '',
    inHeaderOfCard: !!(hdr && hdr.tagName === 'HEADER'
                       && card && card.tagName === 'SECTION' && card.classList.contains('card')),
    headerText: title,
    isLastInHeader: hdr ? hdr.lastElementChild === b : false,
    sharesRowWithSearch: !!(hdr && hdr.querySelector && hdr.querySelector('#btnSearch')),
    rightGap: Math.round(hr.right - br.right),
    isPrimary: b.classList.contains('primary'),
  };
})()`;

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log(`迷你假教务：${FAKE}`);
  console.log('='.repeat(62));
  try {
    // ---- 前置 ----
    const cs0 = JSON.parse((await urlReq(`${BASE}/api/state`)).body).courses_snapshot || {};
    if (!cs0.exists) {
      check('前置：磁盘上有课程快照（courses.json）', false,
        '这个实例的落盘目录里没有 courses.json —— 换个 XK_STATE_DIR 或先搜一次课');
    }
    // 假教务切到「未开放期」，并建一个会话（假教务不校验 Cookie 内容）
    console.log(`   假教务选课期：${await setPeriod(true)}`);
    const sr = await urlReq(`${BASE}/api/session`, 'POST',
      JSON.stringify({ cookie: 'JSESSIONID=fake; route=fake' }),
      { 'Content-Type': 'application/json' });
    check('前置：会话已建立', sr.code === 200, `${sr.code} ${sr.body.slice(0, 160)}`);

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
    // 等到「按钮的可见性」达到预期（前端 3 秒轮询一次状态）
    async function waitShown(want, secs = 12) {
      let info = null;
      for (let i = 0; i < secs * 2; i++) {
        info = await ev(PROBE);
        if (info && info.shown === want) return info;
        await sleep(500);
      }
      return info;
    }

    const st0 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    console.log(`   服务端视角：session_state=${st0.session_state} inited=${st0.inited} is_open=${st0.is_open}`);

    // ---- ① 未开放期 → 按钮在「2 搜索课程」标题栏右侧 ----
    console.log('\n① 未开放期：「📂 上次数据」在「2 搜索课程」标题栏右侧');
    const info = await waitShown(true);
    console.log(`   #btnCoursesSnap = ${info && info.text}   (parent=${info && info.parentTag}`
      + `, headerText=「${info && info.headerText}」, 距右边缘 ${info && info.rightGap}px)`);
    check('⭐ 按钮出现了（未开放期）', !!(info && info.shown), JSON.stringify(info));
    check('⭐ 它的父节点是 <header>（不是搜索输入框那一行）',
      !!(info && info.inHeaderOfCard), JSON.stringify(info));
    check('⭐ 而且就是「2 搜索课程」这张卡片的标题栏',
      !!(info && /2\s*搜索课程/.test(info.headerText || '')), info && info.headerText);
    check('⭐ 在标题栏里排**最后**（即标题文字右侧）',
      !!(info && info.isLastInHeader), JSON.stringify(info));
    check('⭐ 贴住标题栏右边缘（≤24px = 16px 内边距 + 余量）',
      !!(info && info.rightGap >= 0 && info.rightGap <= 24), info && String(info.rightGap));
    check('  确认已**不在**搜索按钮那一行（旧位置）',
      !!(info && !info.sharesRowWithSearch), JSON.stringify(info));
    check('文案带门数，一眼看得出能拿到多少',
      /(\d+)/.test((info && info.text) || ''), info && info.text);
    check('悬停说明列出类别与门数（点之前就知道有没有自己要的那一类）',
      /板块课|主修课程/.test((info && info.title_attr) || '')
      && /个教学班/.test((info && info.title_attr) || ''),
      (info && info.title_attr || '').replace(/\n/g, ' | ').slice(0, 160));
    check('未开放期 → 露面即主操作色（这时「搜索」用不了，它才是唯一能看课的路子）',
      !!(info && info.isPrimary), JSON.stringify(info));

    const before = await ev(`document.querySelectorAll('#courseList .item').length`);
    check('点之前课程列表是空的（没有偷偷加载）', before === 0, String(before));

    // ---- ② 点它 → 离线课程列表出来 ----
    console.log('\n② 点「📂 上次数据」');
    await ev(`document.getElementById('btnCoursesSnap').click()`);
    let n = 0;
    for (let i = 0; i < 40; i++) {
      n = await ev(`document.querySelectorAll('#courseList .item').length`);
      if (n > 0) break;
      await sleep(400);
    }
    const firstNames = await ev(`Array.from(document.querySelectorAll('#courseList .title'))
      .slice(0, 3).map(e => e.textContent.trim()).join('、')`);
    const hint = await ev(`(() => { const e = document.getElementById('courseHint');
      return (e && e.style.display !== 'none') ? e.textContent.replace(/\\s+/g, ' ').trim() : ''; })()`);
    const logs = await ev(`document.getElementById('log').textContent`);

    console.log(`   #courseList 条目数 = ${n}；前几门：${firstNames}`);
    console.log(`   #courseHint = ${hint.slice(0, 160)}`);

    check('⭐ 课程列表里真的出现了上次搜到的课', n > 0, String(n));
    // ⚠️ 门数**不等于**教学班数 —— 列表是按「课程」聚合的，一个课号下几十个教学班只算一门。
    //    所以别断言「门数 ≥ 10」那种随环境漂移的数，改成两条**恒真**的口径：
    //    ① 日志里的「共 N 个教学班」必须等于落盘那一桶的 N（证明加载的是同一份数据）；
    //    ② 聚合后门数 ≤ 教学班数。
    const newest = (cs0.buckets || []).slice()
      .sort((a, b) => (b.saved_at || 0) - (a.saved_at || 0))[0] || {};
    check(`  加载的正是最近保存的那一桶（${newest.kklxmc} / ${newest.count} 个教学班）`,
      new RegExp(`共 ${newest.count} 个教学班`).test(String(logs)), String(logs).slice(-180));
    check('  列表确实按「教学班 → 课程」聚合过（门数 ≤ 教学班数）',
      n <= Number(newest.count || 0), `门数 ${n} / 教学班 ${newest.count}`);
    check('⭐ 明确标注「这是上次保存下来的」（不能让人误以为是实时数据）',
      hint.includes('上次搜索保存下来的'), hint.slice(0, 120));
    check('⭐ 且说清了「选班」现在能不能用（教学班数据没落盘）',
      hint.includes('选班'), hint.slice(0, 200));
    check('日志说明了来源与保存时间',
      typeof logs === 'string' && logs.includes('已加载上次搜索的课程数据')
      && /保存于/.test(logs), String(logs).slice(-200));
    // ⚠️ 「已自动切到…」这句必须与现实一致：课程类别下拉为空时根本切不动
    const tabOpts = await ev(`document.getElementById('tabSel').options.length`);
    check('"已自动切到"这句话没有撒谎（下拉为空时不许说已切）',
      tabOpts > 1 || !String(logs).includes('已自动切到'),
      `下拉 ${tabOpts} 个选项 / ${String(logs).slice(-120)}`);

    // ---- ③ 切到开放期 → 按钮消失 ----
    console.log('\n③ 教务开放了 → 按钮应当**消失**（开放期搜索是实时的）');
    console.log(`   假教务选课期：${await setPeriod(false)}`);
    const info3 = await waitShown(false, 15);
    console.log(`   按钮 shown = ${info3 && info3.shown}`);
    check('⭐⭐ 开放期按钮消失（这就是用户要的「只在未开放状态显示」）',
      !!(info3 && info3.shown === false), JSON.stringify(info3));
    check('  且是**隐藏**不是被移出 DOM（切回未开放期要能原样回来）',
      !!(info3 && !info3.missing), JSON.stringify(info3));

    // ---- ④ 再切回未开放期 → 按钮回来 ----
    console.log('\n④ 又回到未开放期 → 按钮应当**回来**');
    console.log(`   假教务选课期：${await setPeriod(true)}`);
    const info4 = await waitShown(true, 15);
    check('⭐ 按钮回来了（位置不变，仍在标题栏右侧）',
      !!(info4 && info4.shown && info4.isLastInHeader && info4.inHeaderOfCard),
      JSON.stringify(info4));

    // ---- ⑤ 清掉会话（状态未知）→ 也是隐藏 ----
    console.log('\n⑤ 清掉会话（「未开放」未知）→ 按钮也是隐藏的');
    await urlReq(`${BASE}/api/session`, 'DELETE');
    const info5 = await waitShown(false, 15);
    const st5 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    console.log(`   服务端视角：session_state=${st5.session_state} inited=${st5.inited}`);
    check('⭐ 会话没了 → 按钮隐藏（「未知」不算「未开放」，不猜）',
      !!(info5 && info5.shown === false), JSON.stringify(info5));

    check('全程没有未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));
    check('控制台没有 error 级输出', consoleErrs.length === 0, consoleErrs.slice(0, 2).join(' | '));

    ws.close();
  } catch (e) {
    check('脚本自身执行', false, String((e && e.stack) || e).slice(0, 300));
  } finally {
    try { chrome.kill(); } catch (_) {}
    try { await setPeriod(true); } catch (_) {}   // 把假教务留在「未开放」这个默认档
  }

  console.log('\n' + '='.repeat(62));
  if (fails.length) {
    console.log(`✗ ${fails.length} 条未通过：`);
    fails.forEach((f) => console.log('   -', f));
    process.exit(1);
  }
  console.log('✓ 全部通过');
})();
