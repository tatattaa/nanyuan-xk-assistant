// 只读探针：拿**真实数据**核对课表「按节次分区 + 虚线隔离 + 完全填充节次」。
//
// 与 `verify_timetable_wheel.js` 的分工：
//   · wheel 那条**自带清单**（铺压力用例）→ 打**隔离实例** 8721；
//   · 本条**不铺任何数据**，只看目标实例上**现成**的课表 → 用来对**用户的真实数据**复测。
//
// ⚠️ 全程只 GET，不 POST /api/plan、不 DELETE /api/session；跑完还会把清单前后对比一遍，
//    确认一个字节都没被动过（`🛡 只读`）。所以它可以安全地打 8720。
//
// 用法：XK_BASE=http://127.0.0.1:8720 node tests/verify_timetable_live.js
//
// 判据（都是「几何关系」，不是「元素在不在」）：
//   ① td 高 == 它跨的那些节次行的高之和    ② 零省略 / 零裁切 / 副信息含「节次」
//   ③ Σ区高 == stack 高，sep ⇔ 虚线        ④ **每条虚线落在它该在的那一节行的正中**
//   ⑤ 首区只覆盖到共享节次的正中（8-10 的课不越界到 11 节）
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9803);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8720').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_timetable_live_profile');
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
  console.log(`真实实例（只读）：${BASE}`);
  console.log('='.repeat(64));
  try {
    const st0 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    const plan0 = JSON.stringify(st0.items);
    console.log(`   服务端视角：source=${st0.source} session_state=${st0.session_state} ` +
      `清单 ${(st0.items || []).length} 项 / 已选 ${(st0.selected || []).length} 门 / ` +
      `retry_mode=${st0.retry_mode}`);

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
    await sleep(2500);
    let ready = false;
    for (let i = 0; i < 50; i++) {
      ready = await ev(`(() => {
        const w = document.getElementById('timetable');
        return !!w && !w.querySelector('.empty') && w.querySelectorAll('td.tt-cell').length > 0;
      })()`);
      if (ready) break;
      await sleep(300);
    }
    check('前置：课表已渲染（拿到真实数据）', ready);

    const info = await ev(`(() => {
      const wrap = document.getElementById('timetable');
      const trs = [...wrap.querySelectorAll('tbody > tr')];
      const rowH = trs.map((t) => Math.round(t.getBoundingClientRect().height));
      const cells = [...wrap.querySelectorAll('td.tt-cell')].map((td) => {
        const stack = td.querySelector(':scope > .slot-stack');
        const j = trs.indexOf(td.parentElement) + 1;
        const span = Number(td.getAttribute('rowspan'));
        const zones = [...stack.querySelectorAll(':scope > .zone')].map((z) => ({
          ua: Number(z.dataset.ua), ub: Number(z.dataset.ub),
          sep: z.classList.contains('sep'),
          h: Math.round(z.getBoundingClientRect().height),
          topTd: Math.round(z.getBoundingClientRect().top - td.getBoundingClientRect().top),
          n: z.querySelectorAll('.slot').length,
          dash: (() => {
            const cs = getComputedStyle(z, '::before');
            return cs.content !== 'none' && parseFloat(cs.borderTopWidth) > 0
                   && cs.borderTopStyle === 'dashed';
          })(),
          names: [...z.querySelectorAll('.slot .nm')].map((n) => n.textContent),
        }));
        return { j, span, rowspan: span, tdH: Math.round(td.getBoundingClientRect().height),
                 slotVar: parseFloat(td.style.getPropertyValue('--slot-h')) || 0,
                 stackH: Math.round(stack.getBoundingClientRect().height),
                 stackOver: stack.scrollHeight - stack.clientHeight,
                 zones, zoneSum: zones.reduce((a, z) => a + z.h, 0),
                 slotH: [...stack.querySelectorAll('.slot')].map((s) => Math.round(s.getBoundingClientRect().height)) };
      });
      const slots = [...wrap.querySelectorAll('.slot-stack .slot')].map((s) => {
        const nm = s.querySelector('.nm'), meta = s.querySelector('.meta');
        return { name: (nm ? nm.textContent : '').slice(0, 16),
                 meta: meta ? meta.textContent.replace(/\\s+/g, ' ').trim() : '',
                 metaHidden: !meta || getComputedStyle(meta).display === 'none',
                 clipX: meta ? meta.scrollWidth - meta.clientWidth : 0,
                 clipY: meta ? meta.scrollHeight - meta.clientHeight : 0,
                 overY: s.scrollHeight - s.clientHeight,
                 ellipsis: [s, meta, nm].some((n) => n && getComputedStyle(n).textOverflow === 'ellipsis') };
      });
      return { rowH, cells, slots, tableH: Math.round(wrap.getBoundingClientRect().height) };
    })()`);

    console.log(`\n课表总高 ${info.tableH}px；节次行高 ${JSON.stringify(info.rowH)}`);
    console.log(`格子 ${info.cells.length} 个，块 ${info.slots.length} 个`);

    // ① rowspan：td 高 == 它跨的那些行之和
    const badRow = info.cells.filter((c) => {
      const sum = info.rowH.slice(c.j - 1, c.j - 1 + c.span).reduce((a, b) => a + b, 0);
      return Math.abs(c.tdH - sum) > 2;
    });
    check('⭐ rowspan 生效：每个跨行格子的高度 == 它所跨那些节次行的高之和',
      badRow.length === 0, JSON.stringify(badRow.map((c) => ({ j: c.j, span: c.span, tdH: c.tdH }))));

    // ② 信息不丢
    const hidden = info.slots.filter((s) => s.metaHidden);
    check('⭐⭐ 没有被 display:none 掉副信息的块', hidden.length === 0,
      `${hidden.length} 块：${JSON.stringify(hidden.slice(0, 3).map((s) => s.name))}`);
    const clipped = info.slots.filter((s) => s.clipX > 0 || s.clipY > 0 || s.overY > 0);
    check('⭐⭐ 没有横向/纵向被裁切的块', clipped.length === 0,
      `${clipped.length} 块：${JSON.stringify(clipped.slice(0, 3).map((s) => ({ n: s.name, x: s.clipX, y: s.clipY, o: s.overY })))}`);
    const ell = info.slots.filter((s) => s.ellipsis);
    check('⭐⭐ 没有 ellipsis 的块', ell.length === 0, `${ell.length} 块`);
    const noJie = info.slots.filter((s) => !/节/.test(s.meta));
    check('⭐⭐ 每块的副信息里都含「节次」', noJie.length === 0,
      `${noJie.length} 块：${JSON.stringify(noJie.slice(0, 3).map((s) => ({ n: s.name, m: s.meta })))}`);

    // ③ 分区 + 虚线 + 填充
    const zoned = info.cells.filter((c) => c.zones.length > 1);
    console.log(`\n多区格子 ${zoned.length} 个：`);
    for (const c of zoned) {
      const rows = info.rowH.slice(c.j - 1, c.j - 1 + c.span);
      console.log(`   第 ${c.j} 节起跨 ${c.span} 行  td=${c.tdH}px stack=${c.stackH}px  行高 ${JSON.stringify(rows)}`);
      console.log('      ' + c.zones.map((z) => `${z.sep ? '···虚线··· ' : ''}[${z.ua}~${z.ub}) ${z.h}px ${z.n}门 |`).join(' '));
      console.log('      ' + c.zones.map((z) => z.names.slice(0, 2).join('/')).join('  ||  '));
    }

    const noSepGap = info.cells.filter((c) => Math.abs(c.zoneSum - c.stackH) > 1);
    check('⭐ 每个多区格：Σ区高 == stack 高（无缝无溢出）', noSepGap.length === 0,
      JSON.stringify(noSepGap.map((c) => ({ j: c.j, zoneSum: c.zoneSum, stackH: c.stackH }))));
    const over = info.cells.filter((c) => c.stackOver > 0);
    check('⭐ 没有格子溢出', over.length === 0, JSON.stringify(over.map((c) => ({ j: c.j, o: c.stackOver }))));

    const allZones = info.cells.flatMap((c) => c.zones);
    check('⭐ 全表一致性：sep ⇔ 虚线', allZones.every((z) => z.sep === z.dash),
      JSON.stringify(allZones.filter((z) => z.sep !== z.dash).map((z) => ({ ua: z.ua, ub: z.ub }))));
    console.log(`全表 ${allZones.length} 个区，其中 ${allZones.filter((z) => z.sep).length} 个带虚线`);

    // ④ 虚线必须落在「它该在的那一节行的正中」
    const bounds = [];
    for (const c of info.cells) {
      for (const z of c.zones) {
        if (!z.sep) continue;
        const k = Math.floor(z.ua);
        const before = info.rowH.slice(c.j - 1, c.j - 1 + k).reduce((a, b) => a + b, 0);
        const rowH = info.rowH[c.j - 1 + k] || 0;
        bounds.push({ j: c.j, k: k, rowH, want: Math.round(before + rowH / 2),
                      got: z.topTd, err: Math.abs(z.topTd - (before + rowH / 2)), tol: rowH * 0.3 });
      }
    }
    console.log('虚线对账：' + JSON.stringify(bounds.map((b) => ({ j: b.j, 第几行: b.k, want: b.want, got: b.got, err: Math.round(b.err) }))));
    check('⭐⭐ 每条虚线都落在它该在的那一节行的正中（±30% 行高）',
      bounds.length > 0 && bounds.every((b) => b.err <= b.tol),
      JSON.stringify(bounds.filter((b) => b.err > b.tol)));

    // ⑤ 「8-10 的课不再排到 11 节」：区边界 u 必须把 8-10 组关在 [?,2.5) 内
    const satLike = info.cells.find((c) => c.zones.length >= 3 && c.zones[0].n >= 3);
    if (satLike) {
      const z0 = satLike.zones[0];
      // ub 必须是「半整数」（= 某个节次的正中）且不能到本格末尾
      // ⚠️ 别写成 (ub*2)%2 —— 2.5*2=5，5%2=1，会把正确的 2.5 判成错
      check('⭐⭐ 首区只覆盖到共享节次的正中（8-10 的课不越界到 11 节）',
        Number.isFinite(z0.ub) && Number.isInteger(Math.round(z0.ub * 2)) && z0.ub * 2 % 2 === 1
        && z0.ub < satLike.span,
        `首区 ub=${z0.ub} span=${satLike.span}`);
    }

    check('全程没有 JS 未捕获异常', errors.length === 0, JSON.stringify(errors.slice(0, 2)));

    const st1 = JSON.parse((await urlReq(`${BASE}/api/state`)).body);
    check('🛡 只读：跑完清单原封不动（没被探针动过）',
      JSON.stringify(st1.items) === plan0, `${JSON.parse(plan0).length} → ${(st1.items || []).length} 项`);

    console.log('\n' + '='.repeat(64));
    console.log(fails.length ? `===== 失败 ${fails.length} 条 =====` : '===== 全部通过 =====');
    fails.forEach((f) => console.log('   ✗ ' + f));
  } catch (e) {
    console.log('\n探针异常：' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n') : e));
    fails.push('exception');
  } finally {
    try { chrome.kill(); } catch (_) {}
    await sleep(300);
    process.exit(fails.length ? 1 : 0);
  }
})();
