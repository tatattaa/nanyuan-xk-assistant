// 验收：使用说明书弹窗（分页翻页 + 图片 + 自动弹出记忆）。
//
// 打**隔离实例**（默认 8721），全程只 GET 页面与 /static/manual.html，不动清单/会话。
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_manual.js
//
// ⚠️ 本脚本需要 `ui/static/manual/cover.png` 存在（用来验证「图片正常渲染」这条路径）。
//    它**不存在**时自动从 assets/icon.png 临时复制一份，跑完删掉（不污染你的内容）。
const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9807);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_manual_profile');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 临时示例图：manual.html 第 1 页引用 /static/manual/cover.png
const COVER = path.join(ROOT, 'ui', 'static', 'manual', 'cover.png');
const COVER_TMP = !fs.existsSync(COVER);
if (COVER_TMP) {
  fs.mkdirSync(path.dirname(COVER), { recursive: true });
  fs.copyFileSync(path.join(ROOT, 'assets', 'icon.png'), COVER);
}

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

    // 前置 ①：内容接口可用 —— 2026-10-02 起正文来源是**结构化内容** `GET /api/manual`
    // ⚠️ 必须**先导航再 fetch**：相对路径在 about:blank 上会直接抛 "Failed to parse URL"（踩过）。
    const man = await ev(`fetch('/api/manual',{cache:'no-store'}).then(r => r.json()).then(d => ({
      source: d.source,
      writable: !!d.writable,
      pages: (d.manual && d.manual.pages) ? d.manual.pages.length : 0,
      title0: (d.manual && d.manual.pages && d.manual.pages[0]) ? (d.manual.pages[0].title || '') : '',
      title1: (d.manual && d.manual.pages && d.manual.pages[1]) ? (d.manual.pages[1].title || '') : '',
    }))`);
    check('前置：/api/manual 有结构化内容（来源 = 项目目录）',
      man.source === 'external' && man.pages >= 2, JSON.stringify(man));

    // 前置 ②：旧的 /static/manual.html 仍在（「一条结构化内容都没有」时的兜底）
    const legacyStatus = await ev(`fetch('/static/manual.html',{cache:'no-store'}).then(r => r.status)`);
    check('前置：旧版 /static/manual.html 仍可访问（200，兜底用）',
      String(legacyStatus) === '200', `实际 ${legacyStatus}`);

    console.log('-- 打开方式 --');
    // ⚠️ 无会话时 `#topbar` 是 display:none（renderGate 控制），量之前必须先显示；
    //    而且必须**在同一次求值里**显示+测量 —— 页面有 3s 轮询会把它重新隐藏。
    const btn = await ev(`(() => {
      document.getElementById('topbar').style.display = '';
      const b = document.getElementById('btnManual');
      const r = b.getBoundingClientRect();
      return { exists: !!b, text: b.textContent.trim(), visible: r.width > 0 && r.height > 0,
               inTopbar: b.closest('#topbar') === document.getElementById('topbar') };
    })()`);
    check('顶栏有「使用说明」按钮且可见', btn.exists && btn.visible, JSON.stringify(btn));
    check('按钮位于顶栏内', btn.inTopbar);
    check('按钮文案是「使用说明」', btn.text === '使用说明', btn.text);

    await ev(`document.getElementById('btnManual').click(); true`);
    await sleep(600);   // 等 fetch 回来
    const opened = await ev(`(() => ({
      shown: getComputedStyle(document.getElementById('manualModal')).display !== 'none',
      bodyLocked: document.body.classList.contains('modal-open'),
      pos: document.getElementById('manualPos').textContent.trim(),
      hasContent: !!document.querySelector('#manualBody h3'),
    }))()`);
    check('点按钮 → 弹窗打开', opened.shown);
    check('弹窗打开时锁定背景滚动（body.modal-open）', opened.bodyLocked);
    check('正文已加载（有 <h3> 小节）', opened.hasContent);
    check('页码初始为「1 / N」', /^1 \/ \d+$/.test(opened.pos), opened.pos);

    // 「编辑内容」按钮：内容可写时才可点（开发态 = 项目目录，必然可写）
    const edBtn = await ev(`(() => { const b = document.getElementById('btnManualEdit');
      return { exists: !!b, text: b.textContent.trim(), disabled: b.disabled }; })()`);
    check('弹窗里有「编辑内容」按钮', edBtn.exists && edBtn.text === '编辑内容', JSON.stringify(edBtn));
    check('内容可写 →「编辑内容」可点', edBtn.disabled === false, JSON.stringify(edBtn));

    console.log('-- 分页翻页 --');
    const p1 = await ev(`(() => {
      const first = document.querySelector('#manualBody h3');
      return {
        title: first ? first.textContent.trim() : '',
        pages: Number(document.getElementById('manualPos').textContent.split('/')[1].trim()),
        prevDisabled: document.getElementById('btnManualPrev').disabled,
        nextDisabled: document.getElementById('btnManualNext').disabled,
        prevShown: getComputedStyle(document.getElementById('btnManualPrev')).display !== 'none',
      };
    })()`);
    check(`页数 == /api/manual 里的页数（${man.pages}）`, p1.pages === man.pages,
      `弹窗 ${p1.pages} / 接口 ${man.pages}`);
    check(`第一页就是内容里的第 1 页（「${man.title0}」）`, p1.title === man.title0, `实际「${p1.title}」`);
    check('第 1 页「← 上一页」不可点', p1.prevDisabled);
    check('页数 > 1 时翻页按钮可见', p1.prevShown);

    await ev(`document.getElementById('btnManualNext').click(); true`);
    await sleep(200);
    const p2 = await ev(`(() => ({
      pos: document.getElementById('manualPos').textContent.trim(),
      title: (document.querySelector('#manualBody h3') || {}).textContent || '',
      scrollTop: document.getElementById('manualBody').scrollTop,
    }))()`);
    check('点「下一页」→ 2 / N', p2.pos.startsWith('2 /'), p2.pos);
    check('第 2 页内容换了（就是内容里的第 2 页）',
      p2.title.trim() === man.title1, `实际「${p2.title.trim()}」 / 期望「${man.title1}」`);
    check('翻页后回到页首（scrollTop 归零）', p2.scrollTop === 0, `scrollTop=${p2.scrollTop}`);

    await ev(`document.getElementById('btnManualPrev').click(); true`);
    await sleep(200);
    const back = await ev(`document.getElementById('manualPos').textContent.trim()`);
    check('点「← 上一页」→ 回到 1 / N', back.startsWith('1 /'), back);

    // 键盘 → （⚠️ 不能用 ↑↓ 翻页，那留给页内滚动）
    await ev(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})); true`);
    await sleep(200);
    const kb = await ev(`document.getElementById('manualPos').textContent.trim()`);
    check('键盘 → 翻到下一页', kb.startsWith('2 /'), kb);

    // 一路翻到最后一页 → 下一页按钮应禁用
    const last = await ev(`(() => {
      const n = Number(document.getElementById('manualPos').textContent.split('/')[1].trim());
      const b = document.getElementById('btnManualNext');
      for (let i = 0; i < n + 2; i++) b.click();
      return { pos: document.getElementById('manualPos').textContent.trim(), n,
               nextDisabled: b.disabled };
    })()`);
    check(`连点下一页停在最后一页（${last.n} / ${last.n}）`, last.pos === `${last.n} / ${last.n}`, last.pos);
    check('最后一页「下一页 →」不可点', last.nextDisabled);

    console.log('-- 图片（img 块的渲染与坏图兜底）--');
    // 内容改成结构化 JSON 之后，图片由 `{type:'img', url:'…'}` 这个块决定。
    // 所以直接测**渲染器本身**的两条路径（不依赖说明书里恰好有图）：
    //   ① 图存在 → <img> 正常渲染、等比缩放、不超宽
    //   ② 图不存在 → 换成 .note.warn「图片没找到 → 路径」，不留破图
    // ⚠️ 探针容器挂 `.modal-body` 这个 class，才能吃到 `.modal-body img` 的等比缩放规则。
    // ⚠️ error 事件是**异步**的：插入后立刻读会读到「还没发生」→ 必须 触发 → sleep → 读（踩过）。
    await ev(`(() => {
      const box = document.createElement('div');
      box.id = 'xk_probe_ok';
      box.className = 'modal-body';
      box.style.width = '600px';
      document.body.appendChild(box);
      box.appendChild(blockToEl({ type: 'img', url: '/static/manual/cover.png', cap: '示例图' }));
      bindManualImages(box);
      return true;
    })()`);
    await sleep(900);
    const imgs = await ev(`(() => {
      const box = document.getElementById('xk_probe_ok');
      const img = box.querySelector('img');
      if (!img) return { hasImg: false };
      const cs = box.getBoundingClientRect();
      const pad = parseFloat(getComputedStyle(box).paddingLeft) + parseFloat(getComputedStyle(box).paddingRight);
      return {
        hasImg: true,
        natW: img.naturalWidth,
        natH: img.naturalHeight,
        w: Math.round(img.getBoundingClientRect().width),
        h: Math.round(img.getBoundingClientRect().height),
        availW: Math.round(cs.width - pad),
        cap: (box.querySelector('.cap') || {}).textContent || '',
      };
    })()`);
    check('img 块能渲染出图片（naturalWidth > 0）', imgs.hasImg && imgs.natW > 0, JSON.stringify(imgs));
    check('图片等比缩放、宽度不超出可用宽度', imgs.hasImg && imgs.w <= imgs.availW + 1,
      `img=${imgs.w} 可用=${imgs.availW}`);
    check('图片没被压扁（高宽比与原图一致 ±2%）', imgs.hasImg
      && Math.abs(imgs.h / imgs.w - imgs.natH / imgs.natW) < 0.02, JSON.stringify(imgs));
    check('图注 .cap 渲染出来了', imgs.cap === '示例图', imgs.cap);

    await ev(`(() => {
      const box = document.createElement('div');
      box.id = 'xk_probe_bad';
      box.className = 'modal-body';
      document.body.appendChild(box);
      box.appendChild(blockToEl({ type: 'img', url: '/static/manual/__definitely_missing__.png' }));
      bindManualImages(box);
      return true;
    })()`);
    await sleep(900);
    const broken = await ev(`(() => {
      const box = document.getElementById('xk_probe_bad');
      const w = box.querySelector('.note.warn');
      return { warnCount: box.querySelectorAll('.note.warn').length,
               text: w ? w.textContent.trim() : '',
               imgLeft: box.querySelectorAll('img').length };
    })()`);
    check('图片路径写错时显示「图片没找到 → 路径」', broken.warnCount >= 1 && broken.text.includes('图片没找到'),
      JSON.stringify(broken));
    check('坏图已被替换掉（不残留 <img>）', broken.imgLeft === 0, `剩余 img=${broken.imgLeft}`);
    await ev(`document.getElementById('xk_probe_ok').remove();
              document.getElementById('xk_probe_bad').remove(); true`);

    console.log('-- 关闭方式 --');
    await ev(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); true`);
    await sleep(150);
    const escClosed = await ev(`getComputedStyle(document.getElementById('manualModal')).display === 'none'
      && !document.body.classList.contains('modal-open')`);
    check('Esc 关闭弹窗（并解除背景锁定）', escClosed);

    await ev(`document.getElementById('btnManual').click(); true`);
    await sleep(500);
    await ev(`(() => {
      const m = document.getElementById('manualModal');
      // 点在卡片内部的空白处：不应关闭
      m.querySelector('.modal-head').click();
      return true;
    })()`);
    await sleep(150);
    const stillOpen = await ev(`getComputedStyle(document.getElementById('manualModal')).display !== 'none'`);
    check('点卡片内部**不**关闭', stillOpen);

    await ev(`(() => {
      const m = document.getElementById('manualModal');
      m.dispatchEvent(new MouseEvent('click', { bubbles: true }));  // 目标就是遮罩本身
      return true;
    })()`);
    await sleep(150);
    const maskClosed = await ev(`getComputedStyle(document.getElementById('manualModal')).display === 'none'`);
    check('点遮罩空白处关闭', maskClosed);

    await ev(`document.getElementById('btnManual').click(); true`);
    await sleep(500);
    await ev(`document.getElementById('btnManualClose').click(); true`);
    await sleep(150);
    const btnClosed = await ev(`getComputedStyle(document.getElementById('manualModal')).display === 'none'`);
    check('点「关闭 ✕」关闭', btnClosed);

    console.log('-- 自动弹出记忆 --');
    const auto = await ev(`(() => {
      localStorage.removeItem('xk_manual_auto');
      document.getElementById('manualModal').style.display = 'none';
      maybeShowManual();                                  // 等价于「登录成功」那一刻的调用
      const shownDefault = getComputedStyle(document.getElementById('manualModal')).display !== 'none';
      const pos = document.getElementById('manualPos').textContent.trim();
      return { shownDefault, pos };
    })()`);
    check('没勾「不再弹出」时，登录后会弹出', auto.shownDefault);
    check('自动弹出从第 1 页开始', auto.pos.startsWith('1 /'), auto.pos);

    const noAuto = await ev(`(() => {
      const cb = document.getElementById('manualNoAuto');
      cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true }));
      const saved = localStorage.getItem('xk_manual_auto');
      document.getElementById('manualModal').style.display = 'none';
      maybeShowManual();
      const shown = getComputedStyle(document.getElementById('manualModal')).display !== 'none';
      return { saved, shown };
    })()`);
    check('勾选「不再自动弹出」写入 localStorage', noAuto.saved === '0', `实际 ${noAuto.saved}`);
    check('勾选后登录不再自动弹出', noAuto.shown === false);

    const reAuto = await ev(`(() => {
      const cb = document.getElementById('manualNoAuto');
      document.getElementById('manualModal').style.display = 'flex';   // 为了让 sync 生效
      cb.checked = false; cb.dispatchEvent(new Event('change', { bubbles: true }));
      const saved = localStorage.getItem('xk_manual_auto');
      syncManualCheckbox();
      const reflected = document.getElementById('manualNoAuto').checked;
      return { saved, reflected };
    })()`);
    check('取消勾选 → 恢复自动弹出', reAuto.saved === '1', `实际 ${reAuto.saved}`);
    check('勾选状态与 localStorage 双向同步（且用 localStorage 为准）', reAuto.reflected === false);

    console.log('-- 运行期 --');
    const exs = errors.filter((e) => !/favicon|net::ERR_CONNECTION|Failed to fetch|404/i.test(e));
    check('页面无 JS 异常', exs.length === 0, exs.slice(0, 2).join(' | '));

    console.log('='.repeat(64));
    console.log(fails.length === 0 ? '===== 全部通过 =====' : `===== 失败 ${fails.length} 条 =====`);
    fails.forEach((f) => console.log('   ✗ ' + f));
  } catch (e) {
    console.log('执行出错：' + e.message);
    fails.push('执行出错');
  } finally {
    try { chrome.kill(); } catch (_) { /* ignore */ }
    if (COVER_TMP) { try { fs.unlinkSync(COVER); } catch (_) { /* ignore */ } }
  }
  process.exit(fails.length === 0 ? 0 : 1);
})();
