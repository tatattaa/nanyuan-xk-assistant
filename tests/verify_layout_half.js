// 前端布局验收：抢课清单 + 蹲课 以全局宽度五五开（横跨整行）。真实 8720，只读。
const http = require('http');
const BASE = 'http://127.0.0.1:8720';
const PORT = 9666;
const cdp = (u) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: u, timeout: 5000 }, (r) => {
    let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => res(JSON.parse(d)));
  }).on('error', rej);
});

(async () => {
  const pass = []; const fail = [];
  const check = (name, ok, extra) => {
    (ok ? pass : fail).push(name);
    console.log((ok ? '  ✓ ' : '  ✗ ') + name + (extra ? '  ' + extra : ''));
  };

  const targets = await cdp('/json/list');
  const t = targets.find((x) => x.type === 'page' && /zzxkyzb/i.test(x.url || ''));
  if (!t) { console.log('✗ 没找到选课页（9666）'); process.exit(1); }

  const ws0 = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws0.onopen = res; ws0.onerror = () => rej(new Error('ws0')); });
  let id0 = 0;
  const send0 = (m, p) => new Promise((res) => {
    const mid = ++id0; const on = (e) => { const x = JSON.parse(e.data); if (x.id === mid) { ws0.removeEventListener('message', on); res(x); } };
    ws0.addEventListener('message', on); ws0.send(JSON.stringify({ id: mid, method: m, params: p }));
  });
  await send0('Target.createTarget', { url: BASE + '/' });

  let ui = null;
  for (let i = 0; i < 20 && !ui; i++) {
    await new Promise((r) => setTimeout(r, 500));
    ui = (await cdp('/json/list')).find((x) => x.type === 'page' && x.url === BASE + '/');
  }
  if (!ui) { console.log('✗ 没等到 UI 页'); process.exit(1); }

  const ws = new WebSocket(ui.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws')); });
  let id = 0;
  const send = (m, p) => new Promise((res) => {
    const mid = ++id; const on = (e) => { const x = JSON.parse(e.data); if (x.id === mid) { ws.removeEventListener('message', on); res(x); } };
    ws.addEventListener('message', on); ws.send(JSON.stringify({ id: mid, method: m, params: p }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) throw new Error('异常：' + JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    return r.result.result.value;
  };
  await send('Runtime.enable');
  await new Promise((r) => setTimeout(r, 1500));

  const geo = await ev(`(() => {
    const byText = (txt) => [...document.querySelectorAll('.card')]
      .find((c) => c.querySelector('header') && c.querySelector('header').textContent.includes(txt));
    const plan = byText('抢课清单');
    const wait = byText('蹲课');
    const flt = byText('筛选课程');
    const search = byText('搜索课程');
    const cols = document.querySelector('.cols');
    if (!plan || !wait || !flt || !search || !cols) return { err: '没找全' };
    const pr = plan.getBoundingClientRect();
    const wr = wait.getBoundingClientRect();
    const cr = cols.getBoundingClientRect();
    const fr = flt.getBoundingClientRect();
    const sr = search.getBoundingClientRect();
    return {
      planW: pr.width, waitW: pr.width, colsW: cr.width,
      fltW: fr.width, searchW: sr.width,
      // 五五（全局）：抢课清单 ≈ 蹲课 ≈ 全局宽的一半
      sameW: Math.abs(pr.width - wr.width) < 2,
      eachHalfOfGlobal: Math.abs(pr.width - cr.width / 2) < cr.width * 0.03,
      // 蹲课在抢课清单右侧、同一行
      waitRight: wr.left > pr.right - 2,
      sameRow: Math.abs(pr.top - wr.top) < 2,
      // 筛选/搜索仍在上面那一行（抢课清单在其下方）
      planBelow: pr.top > fr.bottom - 2 && pr.top > sr.bottom - 2,
    };
  })()`);
  if (geo.err) { console.log('✗ ' + geo.err); process.exit(1); }
  check('抢课清单与蹲课同宽（五五）', geo.sameW === true,
        '抢课 ' + geo.planW.toFixed(1) + ' vs 蹲课 ' + geo.waitW.toFixed(1));
  check('各占全局宽度一半', geo.eachHalfOfGlobal === true,
        '卡片宽 ' + geo.planW.toFixed(1) + ' ≈ 全局宽 ' + geo.colsW.toFixed(1) + '/2=' + (geo.colsW / 2).toFixed(1));
  check('蹲课在抢课清单右侧', geo.waitRight === true);
  check('同一行并排', geo.sameRow === true);
  check('筛选/搜索在上方一行（未动）', geo.planBelow === true,
        '筛选宽 ' + geo.fltW.toFixed(1) + ' / 搜索宽 ' + geo.searchW.toFixed(1));

  console.log('\n── 结果 ──');
  console.log('通过 ' + pass.length + ' / ' + (pass.length + fail.length));
  if (fail.length) { console.log('失败：\n  ' + fail.join('\n  ')); process.exit(1); }
  console.log('✅ 全部通过');
  process.exit(0);
})().catch((e) => { console.log('异常：' + (e && e.stack ? e.stack.split('\n').slice(0, 5).join('\n') : e)); process.exit(1); });
