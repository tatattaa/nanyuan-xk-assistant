// 验收：版本徽章 + 页脚（系统版本 / 免责声明）。
//
// 打**隔离实例**（默认 8721），全程只 GET 页面，不碰清单、不碰会话。
// 判据都落在「真实渲染结果」上：文字内容 + 几何关系（在卡片下方 / 跨整行 / 不被裁）。
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_version_footer.js
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9805);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_ver_footer_profile');
const VERSION = 'v1.1.0';
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
  `--window-size=${process.env.XK_WIDTH || 1420},1400`, 'about:blank',
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

(async () => {
  console.log(`目标实例：${BASE}`);
  console.log('='.repeat(64));
  try {
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
    await sleep(2600);

    // ---------------- 登录页 ----------------
    const login = await ev(`(() => {
      const gate = document.getElementById('loginGate');
      const card = gate.querySelector('.login-card');
      const f = gate.querySelector('[data-footer]');
      const chip = gate.querySelector('h2 [data-ver]');
      const h2 = gate.querySelector('h2');
      const c = card.getBoundingClientRect(), fr = f.getBoundingClientRect();
      const cr = chip.getBoundingClientRect();
      return {
        gateShown: getComputedStyle(gate).display !== 'none',
        title: h2.textContent.trim(),
        chipText: chip ? chip.textContent.trim() : null,
        chipVisible: cr.width > 0 && cr.height > 0,
        chipOnTitleLine: Math.abs((cr.top + cr.height / 2) - (h2.getBoundingClientRect().top + h2.getBoundingClientRect().height / 2)) < 14,
        footerText: f.textContent.trim(),
        footerBelowCard: fr.top >= c.bottom - 2,
        footerVisible: fr.width > 0 && fr.height > 0,
        footerOverflow: f.scrollWidth - f.clientWidth,
      };
    })()`);

    console.log('-- 登录页 --');
    check('登录门可见（前置）', login.gateShown);
    check(`标题含版本号「${VERSION}」`, login.title.includes(VERSION), `实际：「${login.title}」`);
    check('登录卡副标题仍是校名占位（没被版本号挤掉）', login.title.startsWith('南苑抢课助手'));
    check('版本徽章已渲染且有尺寸', login.chipVisible && login.chipText === VERSION, `chip=${JSON.stringify(login.chipText)}`);
    check('版本徽章与标题同一行', login.chipOnTitleLine);
    check('登录页有页脚', login.footerVisible);
    check('页脚在登录卡**下方**（页面最底部）', login.footerBelowCard);
    check('页脚含版本号', login.footerText.includes(VERSION), login.footerText.slice(0, 60));
    check('页脚含教务系统版本', login.footerText.includes('正方教务系统 V-9.0'));
    check('页脚含免责声明', login.footerText.includes('免责声明'), login.footerText.slice(0, 60));
    check('页脚未被裁切（无横向溢出）', login.footerOverflow <= 1, `overflow=${login.footerOverflow}`);

    // ---------------- 主页面 ----------------
    // ⚠️ 强制显示 + 测量**必须在同一次 Runtime.evaluate 里**完成：
    //    init() 里有 3s 轮询会调 renderGate()，一见「无会话」就把主体重新隐藏；
    //    跨 await 去量，量到的是 display:none 的全 0 几何 —— 断言会退化成
    //    「0 >= -2 恒真」「0 <= 52 恒真」这种永远通过／永远失败的假结果。
    //    （只改本次无头页面的 style，不动任何数据。）
    const main = await ev(`(() => {
      document.getElementById('loginGate').style.display = 'none';
      document.getElementById('topbar').style.display = '';
      const wrap = document.getElementById('mainApp');
      wrap.style.display = '';
      const kids = [...wrap.children];
      const f = wrap.querySelector(':scope > [data-footer]');
      const h1 = document.querySelector('#topbar h1');
      const chip = h1.querySelector('[data-ver]');
      const prev = kids[kids.indexOf(f) - 1];
      const cs = getComputedStyle(wrap);
      const wr = wrap.getBoundingClientRect(), fr = f.getBoundingClientRect();
      const tb = document.getElementById('topbar').getBoundingClientRect();
      return {
        wrapWidth: Math.round(wr.width),
        topbarHeight: Math.round(tb.height),
        chipWidth: chip ? Math.round(chip.getBoundingClientRect().width) : 0,
        h1Height: Math.round(h1.getBoundingClientRect().height),
        isLastChild: kids[kids.length - 1] === f,
        title: h1.textContent.trim(),
        chipText: chip ? chip.textContent.trim() : null,
        footerText: f.textContent.trim(),
        footerBelowPrev: fr.top >= prev.getBoundingClientRect().bottom - 2,
        footerWidth: Math.round(fr.width),
        wrapInnerWidth: Math.round(wr.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
        footerOverflow: f.scrollWidth - f.clientWidth,
        docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        footerCount: document.querySelectorAll('[data-footer]').length,
        chipCount: document.querySelectorAll('[data-ver]').length,
      };
    })()`);

    console.log('-- 主页面 --');
    // 前置：几何断言必须先确认「真的渲染出来了」，否则下面的宽高断言全是废话
    check('前置：主体已渲染（宽度 > 400px）', main.wrapWidth > 400, `宽度 ${main.wrapWidth}px`);
    check('前置：顶栏已渲染（高度 > 20px）', main.topbarHeight > 20, `高度 ${main.topbarHeight}px`);
    check('顶栏标题含版本号', main.title.includes(VERSION), `实际：「${main.title}」`);
    check('顶栏版本徽章已渲染', main.chipWidth > 0 && main.chipText === VERSION,
      `chip=${JSON.stringify(main.chipText)} width=${main.chipWidth}`);
    check('顶栏没被徽章撑高（h1 ≤ 26px）', main.h1Height <= 26, `实际 ${main.h1Height}px`);
    check('页脚是主体**最后一个**子元素（页面最底部）', main.isLastChild);
    check('页脚在最后一张卡片**下方**', main.footerBelowPrev);
    check('页脚跨整行（宽度 == 主体内容宽度）',
      Math.abs(main.footerWidth - main.wrapInnerWidth) <= 2,
      `footer=${main.footerWidth} wrapInner=${main.wrapInnerWidth}`);
    check('页脚含版本 + 教务版本 + 免责声明',
      main.footerText.includes(VERSION)
      && main.footerText.includes('正方教务系统 V-9.0')
      && main.footerText.includes('免责声明'));
    check('页脚未被裁切', main.footerOverflow <= 1, `overflow=${main.footerOverflow}`);
    check('整页无横向溢出', main.docOverflow <= 1, `overflow=${main.docOverflow}`);
    check('页脚共 2 处（登录页 + 主页面）', main.footerCount === 2, `实际 ${main.footerCount}`);
    check('版本徽章共 2 处（登录页 + 顶栏）', main.chipCount === 2, `实际 ${main.chipCount}`);

    console.log('-- 运行期 --');
    const exs = errors.filter((e) => !/favicon|net::ERR_CONNECTION|Failed to fetch|404/i.test(e));
    check('页面无 JS 异常', exs.length === 0, exs.slice(0, 2).join(' | '));

    console.log('='.repeat(64));
    console.log(fails.length === 0 ? '===== 全部通过 =====' : `===== 失败 ${fails.length} 条 =====`);
    fails.forEach((f) => console.log('   ✗ ' + f));
  } catch (e) {
    console.log('执行出错：' + e.message);
  } finally {
    try { chrome.kill(); } catch (_) { /* ignore */ }
  }
  process.exit(fails.length === 0 ? 0 : 1);
})();
