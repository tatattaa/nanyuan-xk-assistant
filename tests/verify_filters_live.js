// 前端筛选器验收：真实 8720，只读。
// 验证：① 筛选卡在左列、结果卡在右列 ② chips 渲染正确 ③ 点「周一」chip 后搜索，
//       结果数应 = 后端 sksj=1 的行数 ④ 重置恢复全量。
// ⚠️ 全程只 GET（/api/courses 是只读查询），不碰 /api/plan、不 DELETE session。
const http = require('http');
const BASE = 'http://127.0.0.1:8720';
const json = (u) => new Promise((res, rej) => {
  http.get(BASE + u, (r) => {
    let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => {
      try { res(JSON.parse(d)); } catch (e) { rej(new Error('非 JSON: ' + d.slice(0, 120))); }
    });
  }).on('error', rej);
});
const getHtml = (u) => new Promise((res, rej) => {
  http.get(BASE + u, (r) => {
    let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => res(d));
  }).on('error', rej);
});

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

  // 后端基准：sksj=1 应有多少教学班（公共选修课 Tab 3）
  const baseAll = await json('/api/courses?tab_index=3&size=200');
  const baseMon = await json('/api/courses?tab_index=3&size=200&sksj=1');
  console.log('后端基准：全量 ' + baseAll.count + ' 行，周一 ' + baseMon.count + ' 行');

  // 打开 CDP 页面
  const targets = await cdp('/json/list');
  let t = targets.find((x) => x.type === 'page' && /zzxkyzb/i.test(x.url || ''));
  if (!t) { console.log('✗ 没有选课页'); process.exit(1); }
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws')); });
  let id = 0;
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    const on = (ev) => { const m = JSON.parse(ev.data); if (m.id === mid) { ws.removeEventListener('message', on); res(m); } };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) throw new Error('页面异常：' + JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    return r.result.result.value;
  };
  await send('Runtime.enable');

  // 打开我们自己的 UI 页（新 tab）
  await send('Page.enable');
  await send('Target.createTarget', { url: BASE + '/' });
  // 找到新建的 UI 页
  let uiPage = null;
  for (let i = 0; i < 20 && !uiPage; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const ts = await cdp('/json/list');
    uiPage = ts.find((x) => x.type === 'page' && x.url === BASE + '/');
  }
  if (!uiPage) { console.log('✗ 没等到 UI 页'); process.exit(1); }

  const ws2 = new WebSocket(uiPage.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws2.onopen = res; ws2.onerror = () => rej(new Error('ws2')); });
  let id2 = 0;
  const send2 = (method, params) => new Promise((res) => {
    const mid = ++id2;
    const on = (e) => { const m = JSON.parse(e.data); if (m.id === mid) { ws2.removeEventListener('message', on); res(m); } };
    ws2.addEventListener('message', on);
    ws2.send(JSON.stringify({ id: mid, method, params }));
  });
  const ev2 = async (expr) => {
    const r = await send2('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) throw new Error('UI 异常：' + JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    return r.result.result.value;
  };
  await send2('Runtime.enable');

  // 等 UI 加载 + 会话建立
  await new Promise((r) => setTimeout(r, 1500));

  // ① 布局：筛选卡在左列；搜索框与结果列表在同一张右列卡里
  const layout = await ev2(`(() => {
    const flt = document.getElementById('fSksj');
    const kw = document.getElementById('kw');
    const res = document.getElementById('courseList');
    const fltCard = flt ? flt.closest('.card') : null;
    const kwCard = kw ? kw.closest('.card') : null;
    const resCard = res ? res.closest('.card') : null;
    const left = fltCard && fltCard.classList.contains('col-left');
    const right = kwCard && kwCard.classList.contains('col-right');
    const sameCard = kwCard === resCard;   // 搜索框与结果同一张卡
    // 3:7 列比：读 cols 的 grid-template-columns
    const cols = document.querySelector('.cols');
    const cs = cols ? getComputedStyle(cols).gridTemplateColumns : '';
    return { left, right, sameCard, cs };
  })()`);
  check('筛选卡在左列', layout.left === true);
  check('搜索卡在右列', layout.right === true);
  check('搜索框与结果列表同一张卡', layout.sameCard === true);
  // getComputedStyle 会把 3fr/7fr 解析成像素，所以按像素比判（3:7 ≈ 0.4286，容差 ±0.02）
  {
    const m = String(layout.cs).match(/([\d.]+)px\s+([\d.]+)px/);
    const ratio = m ? (+m[1]) / (+m[2]) : -1;
    check('两列 3:7', ratio > 0.40 && ratio < 0.46, 'px=' + layout.cs + ' 比=' + ratio.toFixed(4));
  }

  // ② chips 渲染
  const chips = await ev2(`(() => {
    const w = document.getElementById('fSksj');
    const j = document.getElementById('fSkjc');
    return { week: w ? w.querySelectorAll('.chip').length : 0, jie: j ? j.querySelectorAll('.chip').length : 0,
             w1: w && w.querySelector('.chip') ? w.querySelector('.chip').textContent : '' };
  })()`);
  check('星期 7 个 chip', chips.week === 7, '实 ' + chips.week);
  check('节次 15 个 chip', chips.jie === 15, '实 ' + chips.jie);
  check('首个 chip = 周一', chips.w1 === '周一', '实 ' + chips.w1);

  // ③ 点「周一」chip → 点搜索 → 结果数应 = 后端周一数
  await ev2(`(() => {
    const w = document.getElementById('fSksj');
    const chip = w.querySelector('.chip');   // 周一
    chip.classList.add('on');
    // 切到公共选修课（tab_index=3）
    const sel = document.getElementById('tabSel');
    const opt = [...sel.options].find((o) => o.value === '3');
    if (opt) sel.value = opt.value;
    return true;
  })()`);
  // 让 tab 变更生效（onchange 可能异步切 tab），再点搜索
  await new Promise((r) => setTimeout(r, 800));
  await ev2(`document.getElementById('btnSearch').click()`);
  await new Promise((r) => setTimeout(r, 2500));
  const resMon = await ev2(`(() => {
    const cnt = document.getElementById('courseCount');
    const title = document.getElementById('courseResultTitle');
    return { cnt: cnt ? cnt.textContent : '', title: title ? title.textContent : '' };
  })()`);
  console.log('  周一搜索结果：', JSON.stringify(resMon));
  check('结果标题带筛选说明', /周一|星期/.test(resMon.title), resMon.title);
  check('结果计数 = 后端周一数', resMon.cnt.indexOf(String(baseMon.count)) >= 0,
        '前端「' + resMon.cnt + '」 vs 后端 ' + baseMon.count);

  // ④ 重置 → 应恢复全量
  await ev2(`document.getElementById('btnFilterReset').click()`);
  await ev2(`document.getElementById('btnSearch').click()`);
  await new Promise((r) => setTimeout(r, 2500));
  const resReset = await ev2(`(() => {
    const cnt = document.getElementById('courseCount');
    return cnt ? cnt.textContent : '';
  })()`);
  check('重置后恢复全量', resReset.indexOf(String(baseAll.count)) >= 0,
        '前端「' + resReset + '」 vs 后端 ' + baseAll.count);

  // ⑤ 只看时间冲突（下拉）→ 结果数 = 后端 sksjct=1
  const baseCtf = await json('/api/courses?tab_index=3&size=200&sksjct=1');
  await ev2(`(() => {
    const sel = document.getElementById('fSksjct');
    sel.value = '1';
    sel.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await ev2(`document.getElementById('btnSearch').click()`);
  await new Promise((r) => setTimeout(r, 2500));
  const resCtf = await ev2(`document.getElementById('courseCount').textContent`);
  check('只看冲突结果数正确', resCtf.indexOf(String(baseCtf.count)) >= 0,
        '前端「' + resCtf + '」 vs 后端 ' + baseCtf.count);

  console.log('\n── 结果 ──');
  console.log('通过 ' + pass.length + ' / ' + (pass.length + fail.length));
  if (fail.length) { console.log('失败：\n  ' + fail.join('\n  ')); process.exit(1); }
  console.log('✅ 全部通过');
  process.exit(0);
})().catch((e) => { console.log('异常：' + (e && e.stack ? e.stack.split('\n').slice(0, 5).join('\n') : e)); process.exit(1); });
