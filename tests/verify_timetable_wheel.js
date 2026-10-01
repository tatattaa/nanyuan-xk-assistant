// 无头浏览器验收：「课表同一时段并列」+「开抢时刻拨轮」。
//
// 需求来源（用户 2026-10-01）：
//   「把课程表同一时间段的课程从折叠做回并列
//     抢课的开抢时刻做出手动拨轮的形式，精确到秒」
//
// 两个改动的关键点，都必须**在浏览器里**才能验：
//
// ① 课表并列：这是 CSS 布局 + 表格 rowspan 的相互作用。
//    上一版是「只显示 1 门 + 折叠 +N」，这次改成**全部显示、纵向均分**。
//    ⚠️ 最大的坑：均分若用 `display:flex` 加在 `<td>` 上，**整表 rowspan 会失效**
//       （实测 rowspan=2/3 都变 70px）。所以均分必须交给 td 内部那层 `.slot-stack`。
//       本脚本专门量 rowspan=3 与 rowspan=2 的高度比 —— 不是 1.5 就说明又踩回去了。
//
// ② 拨轮：要证明「**界面上显示的时刻 = 真正提交给后端的时刻**」。
//    所以最后一节不是看 DOM，而是点「开始抢课」后去 `/api/state.schedule.start_at`
//    核对绝对时间戳。
//
// 前置（本脚本自己铺清单，不碰 8720）：
//   XK_STATE_DIR=<临时目录> python serve.py --port 8721 --mock
//
// 用法：XK_BASE=http://127.0.0.1:8721 node tests/verify_timetable_wheel.js
//
// ⚠️ 本脚本会 POST /api/plan + /api/start，但**一律用未来的时刻**（只预热、不开火），
//    跑完 POST /api/stop。仍建议打隔离实例（8721），别打用户正在用的 8720。
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9799);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_timetable_wheel_profile');
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
  const errors = [];                       // 页面里的未捕获异常，最后统一断言
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

const pad2 = (n) => String(n).padStart(2, '0');

// 周二 3-5 与 4-5 三个时段两两相交 → 必须并成**一个** rowspan=3 的格子（3 门课都在里面）。
// 周四 8-9 独立一个 → rowspan=2，用来跟 rowspan=3 比高度（验 rowspan 没被 flex 弄坏）。
const SLOT = (weekday, start, end, weeks) => ({ weekday, start, end, weeks });
const PLAN_ITEMS = [
  { kch_id: 'TWA', kcmc: '演示课甲', jsxx: '张老师', slots: [SLOT(2, 3, 5, [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17])] },
  { kch_id: 'TWB', kcmc: '演示课乙', jsxx: '李老师', slots: [SLOT(2, 3, 5, [1, 2, 3, 4, 5])] },
  { kch_id: 'TWC', kcmc: '演示课丙', jsxx: '王老师', slots: [SLOT(2, 4, 5, [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17])] },
  { kch_id: 'TWD', kcmc: '演示课丁', jsxx: '赵老师', slots: [SLOT(4, 8, 9, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17])] },
  // 压力用例：**最矮的格子**（单行 = 58−8 = 50px）+ 一个会换行的长名字。
  // 这一格放不下「名字 + 两行副信息」→ 必须触发压缩档位（并成一行 + 缩字号）。
  // 就是它把「按固定阈值猜」那种写法的毛病逼出来的：50px 的块，阈值判 46 会说"放得下"，
  // 结果是副信息被 overflow:hidden 从中间裁断。
  { kch_id: 'TWE', kcmc: '演示课戊超长课程名称测试', jsxx: '钱老师',
    slots: [SLOT(1, 1, 1, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17])] },
];

// ⭐ 压力用例：复刻**真实课表**里那种「周六下午堆了一堆周次各不相同的课」。
//
// ⚠️ 2026-10-01 起的分块结果（合并判据由「相邻也并」改为「**只并相交**」之后）：
//      · 8-10 ×5 + 10-11 ×3 → 相交（共用第 10 节）→ 并成**一个 rowspan=4 的格子**
//        （4×58−8 = 224px，8 门课每块只剩 ~28px）
//      · 12-13 ×1          → 与 8-11 只是节次号相邻、并不相交 → **独立成格**
//    改动前 9 门会被并进同一个 rowspan=6 的格子（每块 36px）。
//    现在虽然少一门，但**每块更矮**（28px < 36px），"挤"的压力反而更足。
//
// 这一格正是把「挤不下就 display:none 掉副信息」那种写法的毛病逼出来的：
// 用户看到的是 8 个只剩课程名的方块，**节次和周次全没了**（用户 2026-10-01 的原话）。
const WEEKS_ALL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17];
const SAT_SPEC = [[8, 10, [8]], [8, 10, [7]], [8, 10, [10]], [8, 10, [14]], [8, 10, [2]],
                  [10, 11, [9]], [10, 11, [16]], [10, 11, [3]], [12, 13, WEEKS_ALL]];
SAT_SPEC.forEach(([s, e, w], i) => {
  PLAN_ITEMS.push({
    kch_id: 'TSAT' + i, kcmc: '周六课' + (i + 1),
    // 地点故意写长一些：dense 档从**尾部**省略，先被牺牲的必须是地点、不能是节次
    jsxx: '老师' + (i + 1) + ' · 教研楼2号201机房 / 8-305',
    slots: [SLOT(6, s, e, w)],
  });
});

(async () => {
  console.log(`界面实例：${BASE}`);
  console.log('='.repeat(64));
  try {
    const st0 = await state();
    console.log(`   服务端视角：source=${st0.source} session_state=${st0.session_state}`);
    check('前置：打的是隔离实例（别把用户的 8720 当试验田）',
      !/:8720(\/|$)/.test(BASE), BASE);

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
        // ⚠️ 早先这里直接 JSON.stringify(ex) —— 会把**整段表达式**打出来，真正的错误消息
        //    被挤到末尾、一截断就看不见（曾因此把一个「模板串里写了反引号」的语法错误
        //    误判成「表达式里的语法问题」，白查半天）。异常自身的描述必须排在最前面。
        const msg = (ex.exception && (ex.exception.description || ex.exception.value))
          || ex.text || JSON.stringify(ex);
        throw new Error('页面内表达式抛异常：' + String(msg).split('\n').slice(0, 4).join(' | '));
      }
      return r.result.result.value;
    };
    await send('Runtime.enable');
    await send('Page.enable');

    // ---- 铺一份**带时段**的清单：时段要能在课表上画出「同一时段多门课」----
    const r0 = await jpost('/api/plan', { items: PLAN_ITEMS, retry_mode: 'serial', start_at: '' });
    check('前置：清单已铺好（5 项，含 3 项周二同一时段）', r0.code === 200,
      `${r0.code} ${r0.body.slice(0, 160)}`);

    await send('Page.navigate', { url: BASE + '/' });
    await sleep(2500);
    let ready = false;
    for (let i = 0; i < 40; i++) {
      ready = await ev(`typeof S !== 'undefined' && Array.isArray(S.plan) && S.plan.length === 14`);
      if (ready) break;
      await sleep(300);
    }
    check('前置：页面加载完成且读到 14 项清单', ready);
    check('前置：清单项的时段（slots）确实带到了前端',
      await ev(`S.plan.every((p) => (p.slots || []).length > 0)`));

    // ================= ① 课表：信息显示全、装不下就拉长课表 =================
    // 用户 2026-10-01 最新口径：
    //   「让信息显示全，不要省略的，位置不够可以适当拉长课表」
    // → 不再有 dense/micro/nano 压缩，也不许出现 text-overflow: ellipsis。
    console.log('\n① 课表：信息完整显示（不省略），装不下就把课表拉长');

    const info = await ev(`(() => {
      const wrap = document.getElementById('timetable');
      const trs = [...wrap.querySelectorAll('tbody > tr')];
      const rowH = trs.map((t) => Math.round(t.getBoundingClientRect().height));
      const cells = [...wrap.querySelectorAll('td.tt-cell')];
      // ⭐ 关键换算：一个 td 从第 j 节开始、跨 span 行。
      // 它「应该」有多高 = 它跨的那些行的高度之和 —— 这是 rowspan 生效的本质判据。
      // （上一版用「rowspan=3 高度 ≈ 1.5 × rowspan=2」间接验证；但既然现在允许拉长，
      //   两个格子可能落在被拉长的行组里，比例就不再成立，改成直接核对行高之和。）
      const cellInfo = (td) => {
        const span = Number(td.getAttribute('rowspan'));
        const j = trs.indexOf(td.parentElement) + 1;
        const slotVar = td.style.getPropertyValue('--slot-h');
        const minH = span * 58 - 8;
        const stack = td.querySelector(':scope > .slot-stack');
        const slots = stack ? [...stack.querySelectorAll('.slot')] : [];
        // ⭐ 「按节次分区」：格内的区（.zone）。每个区只占 it 自己那一段节次。
        const zones = stack ? [...stack.querySelectorAll(':scope > .zone')].map((z) => ({
          ua: Number(z.dataset.ua),
          ub: Number(z.dataset.ub),
          sep: z.classList.contains('sep'),
          h: Math.round(z.getBoundingClientRect().height),
          top: Math.round(z.getBoundingClientRect().top - stack.getBoundingClientRect().top),
          // 相对 **td 顶边** 的位置 —— 拿它和「行网格」对账（虚线该落在哪一行）
          topTd: Math.round(z.getBoundingClientRect().top - td.getBoundingClientRect().top),
          n: z.querySelectorAll('.slot').length,
          // 虚线是否真的画出来了（::before 的 content 非 none 且有 border-top）
          dash: (() => {
            const cs = getComputedStyle(z, '::before');
            return cs.content !== 'none' && parseFloat(cs.borderTopWidth) > 0
                   && cs.borderTopStyle === 'dashed';
          })(),
        })) : [];
        return {
          span: span,
          j: j,
          slotVar: slotVar,
          minH: minH,
          grown: Math.round(parseFloat(slotVar)) > minH,
          tdH: Math.round(td.getBoundingClientRect().height),
          rowSum: rowH.slice(j - 1, j - 1 + span).reduce((a, b) => a + b, 0),
          stackH: stack ? Math.round(stack.getBoundingClientRect().height) : null,
          stackOver: stack ? (stack.scrollHeight - stack.clientHeight) : null,
          zones: zones,
          zoneSum: zones.reduce((a, z) => a + z.h, 0),
          slotH: slots.map((s) => Math.round(s.getBoundingClientRect().height)),
          names: slots.map((s) => (s.querySelector('.nm') || {}).textContent || ''),
        };
      };
      // 逐块量：用户要的是「一个字都不许丢」，所以三条硬检查 ——
      //   ① 没有被 display:none 掉的信息
      //   ② 没有 ellipsis / nowrap（任何"按宽度省略"的机制）
      //   ③ 内容既没纵向溢出、也没横向溢出（scroll <= client）
      // ⚠️ scrollWidth/scrollHeight 才是"有没有被裁"的判据：ellipsis 只改观感，
      //    textContent 照样是完整的，只看文字内容根本验不出省略号。
      const every = [...wrap.querySelectorAll('.slot-stack .slot')].map((s) => {
        const nm = s.querySelector('.nm');
        const meta = s.querySelector('.meta');
        const mcs = meta ? getComputedStyle(meta) : null;
        return {
          h: Math.round(s.getBoundingClientRect().height),
          name: (nm ? nm.textContent : '').slice(0, 18),
          metaText: meta ? meta.textContent.replace(/\\s+/g, ' ').trim() : '',
          metaHidden: !meta || (mcs ? mcs.display === 'none' : false),
          metaClipX: meta ? (meta.scrollWidth - meta.clientWidth) : null,
          metaClipY: meta ? (meta.scrollHeight - meta.clientHeight) : null,
          nameClipX: nm ? (nm.scrollWidth - nm.clientWidth) : null,
          nameClipY: nm ? (nm.scrollHeight - nm.clientHeight) : null,
          ellipsis: [s, meta, nm].some((n) => n && getComputedStyle(n).textOverflow === 'ellipsis'),
          nowrap: [meta, nm].some((n) => n && getComputedStyle(n).whiteSpace === 'nowrap'),
          overflowY: s.scrollHeight - s.clientHeight,
          denseCls: /\\b(dense|micro|nano)\\b/.test(String(s.className)),
        };
      });
      return {
        cellCount: cells.length,
        rowH: rowH,
        cells: cells.map(cellInfo),
        slots: every,
        // 旧折叠方案的残留物：必须一个都没有
        folded: wrap.querySelectorAll('.slot-folded').length,
        more: wrap.querySelectorAll('.slot-more').length,
      };
    })()`);

    const r3 = info.cells.find((c) => c.span === 3 && c.slotH.length === 3);
    const grownCells = info.cells.filter((c) => c.grown);
    console.log(`   课表格子 ${info.cellCount} 个；节次行高 ${JSON.stringify(info.rowH)}`);
    console.log(`   被拉长的格子：${grownCells.length
      ? grownCells.map((c) => `${c.span} 跨、${c.slotH.length} 门课（${c.minH}→${parseFloat(c.slotVar)}px）`).join('；')
      : '（无）'}`);
    console.log(`   装得下的那格（周二 3-5 节、3 门课）每块高 ${JSON.stringify(r3 && r3.slotH)}`);

    check('⭐ 同一时段的多门课**全部显示**（不再是只显示 1 门）',
      !!r3 && r3.slotH.length === 3, `格内 ${r3 && r3.slotH.length} 门`);
    // 课程名前可能带一个「⚠ 」前缀（与已选/清单内其他待选真撞时间时 chipHtml 会加），
    // 所以比较前先剥掉它 —— 前缀本身是另一个特性，这里只验"三门课都画出来了"。
    const bareNames = (r3 ? r3.names : []).map((s) => s.replace(/^⚠\s*/, ''));
    check('   三门课的课程名都画出来了（没被裁掉）',
      bareNames.join('|') === '演示课甲|演示课乙|演示课丙', bareNames.join('|'));

    // ⭐⭐ 取代旧版「rowspan=3 ≈ 1.5 × rowspan=2」的比例断言。
    // 现在允许拉长，两个跨行格可能落在被拉长的行组里，比例不再稳定；
    // 但「格子高度 == 它跨的那些节次行高之和」是**永恒成立**的表格不变量 ——
    // 一旦 td 被 flex 弄坏、rowspan 失效，这条立刻挂。
    const badSpan = info.cells.filter((c) => Math.abs(c.tdH - c.rowSum) > 2);
    check('⭐⭐ rowspan 生效：每个跨行格子的高度 == 它所跨那些节次行的高之和',
      badSpan.length === 0,
      JSON.stringify(badSpan.map((c) => ({ span: c.span, tdH: c.tdH, rowSum: c.rowSum }))));
    // 色块至少要填满自己那段节次（td 高 = 跨度×58 是这层的下界）
    const shortCell = info.cells.filter((c) => c.tdH < c.span * 58 - 2);
    check('⭐ 每一格都不低于自己的节次跨度（span×58，色块填满节次）',
      shortCell.length === 0,
      JSON.stringify(shortCell.map((c) => ({ span: c.span, tdH: c.tdH }))));
    check('⭐ 旧折叠方案已彻底移除（+N 角标 / 折叠容器都不存在）',
      info.folded === 0 && info.more === 0, `folded=${info.folded} more=${info.more}`);

    // 装得下的格子仍然严格均分（用户明确选的口径，不该因为这次改动而变）
    const hs = (r3 || { slotH: [] }).slotH;
    check('⭐ 装得下的格子仍**纵向均分**：最高块与最矮块差 ≤ 3px',
      hs.length > 0 && Math.max(...hs) - Math.min(...hs) <= 3, JSON.stringify(hs));
    check('   该格 --slot-h 仍是 span×58−8（没被无谓地拉长）',
      !!r3 && !r3.grown && Math.abs(parseFloat(r3.slotVar) - 166) < 0.5, `${r3 && r3.slotVar}`);
    check('   三块叠起来没超出格子（stack 没溢出）',
      !!r3 && r3.stackOver === 0, `${r3 && r3.stackOver}`);

    // ================= ⭐⭐ 正题：信息必须显示全 =================
    console.log('\n①b 信息必须显示全（不省略、不裁切）');

    const hidden = info.slots.filter((s) => s.metaHidden);
    check('⭐⭐ 全表没有任何一块被 display:none 掉副信息',
      hidden.length === 0,
      `${hidden.length} 块：${JSON.stringify(hidden.slice(0, 3).map((s) => s.name))}`);

    const clipX = info.slots.filter((s) => (s.metaClipX || 0) > 1 || (s.nameClipX || 0) > 1);
    check('⭐⭐ 全表没有任何一块被**省略号/横向截断**吃掉字符',
      clipX.length === 0,
      JSON.stringify(clipX.slice(0, 3).map((s) => ({ h: s.h, mx: s.metaClipX, nx: s.nameClipX }))));

    const clipY = info.slots.filter((s) => (s.metaClipY || 0) > 1 || (s.nameClipY || 0) > 1
      || s.overflowY > 1);
    check('⭐⭐ 全表没有任何一块被**纵向裁切**（拉长以后必须谁都装得下）',
      clipY.length === 0,
      JSON.stringify(clipY.slice(0, 3).map((s) => ({ h: s.h, my: s.metaClipY, ny: s.nameClipY, o: s.overflowY }))));

    const anyEllipsis = info.slots.filter((s) => s.ellipsis || s.nowrap);
    check('⭐⭐ 全表没有任何一块用 ellipsis / nowrap（压缩档位已彻底退役）',
      anyEllipsis.length === 0,
      JSON.stringify(anyEllipsis.slice(0, 3).map((s) => ({ h: s.h, e: s.ellipsis, n: s.nowrap }))));
    check('   压缩档位的 class（dense/micro/nano）一个都不剩',
      info.slots.every((s) => !s.denseCls));

    // 副信息里必须真的带「节」和「周」（不是渲染了个空壳）
    const noJie = info.slots.filter((s) => !/节/.test(s.metaText));
    check('⭐⭐ 每一块的副信息里确实含「节次」',
      noJie.length === 0, `${noJie.length} 块：${JSON.stringify(noJie.slice(0, 3).map((s) => s.metaText))}`);

    // ================= ⭐⭐ 装不下 → 真的把课表拉长了 =================
    console.log('\n①c 装不下 → 拉长课表（而不是省略）');

    // ⚠️ 2026-10-01：`mergeDayBlocks` 的合并判据从「相邻也并」改成「**只并相交**」，
    //    于是周六 12-13 节（与 8-11 只是节次号相邻，11 节 17:35 下课、12 节 18:45 上课，
    //    中间隔着晚饭）**独立成格**，不再被并进 8-13 的同一个大格 ——
    //    压力格里因此是 **8 门**课而不是 9 门。
    //    8 门挤 4 行（4×58−8 = 224px，每块 ~28px）比原来 9 门挤 6 行（每块 36px）**更挤**，
    //    压力足够，所以只调这里的数字，不动用例数据。
    const big = info.cells.find((c) => c.slotH.length >= 8);
    check('⭐ 存在「8 门课挤一格」的压力用例',
      !!big, `最大格的块数=${Math.max(...info.cells.map((c) => c.slotH.length))}`);
    check('⭐⭐ 这一格确实被**拉长**了（不是把内容压扁/省略）',
      !!big && big.grown, big ? `--slot-h=${big.slotVar}（原网格 ${big.minH}px）` : '');
    check('⭐⭐ 拉长后的格高 ≥ 各块内容需要的高度之和',
      !!big && parseFloat(big.slotVar) >= big.slotH.reduce((a, b) => a + b, 0) - 1,
      big ? `slotVar=${parseFloat(big.slotVar)} ΣslotH=${big.slotH.reduce((a, b) => a + b, 0)}` : '');
    check('   该格每块都有可读高度（≥ 34px，不再是被压扁的三十几像素）',
      !!big && Math.min(...big.slotH) >= 34, big ? JSON.stringify(big.slotH) : '');
    check('   该格 stack 没溢出（拉长到位了）',
      !!big && big.stackOver === 0, big ? String(big.stackOver) : '');

    // ================= ⭐⭐ 按节次分区（用户 2026-10-01 新增要求） =================
    // 用户原话：「8-10节有很多课，导致他都排到11节的位置了，这种情况你可以单独对
    //           8-10节做好延伸」「他们共有第10节就在当天第十节位置加条虚线隔离」
    //          「课程底色色块做好完全填充对应的节次」
    console.log('\n①d 按节次分区（8-10 的课只能待在 8-10 里）');

    // 1) 格内按节次分了区，且每块只归它自己那一段
    const zoned = info.cells.filter((c) => c.zones.length > 1);
    check('⭐ 压力格子内确实按节次分成了多个区（不是一个格子 8 等分）',
      !!big && big.zones.length >= 2,
      big ? `区数=${big.zones.length}，各区块数 ${JSON.stringify(big.zones.map((z) => z.n))}` : '');

    check('⭐⭐ 8-10 节的 5 门课**全在第一个区里**（不再被摊到 11 节）',
      !!big && big.zones[0].n === 5, big ? `第一区块数=${big.zones[0].n}` : '');
    check('⭐⭐ 10-11 节的 3 门课在第二个区（8-10 与 10-11 共用第 10 节 → 并成一格两区）',
      !!big && big.zones.length === 2 && big.zones[1].n === 3,
      big ? JSON.stringify(big.zones.map((z) => z.n)) : '');
    // ⭐⭐ 2026-10-01 新增（对应上面那次的判据修正）：
    //    「相邻也并」改成「只并相交」之后，与 8-11 不相交的 12-13 **必须**独立成格。
    //    这条同时钉住「不能因为怕撞坑①就把相邻段也并进来」——
    //    那正是把 4-5 的课拖成 4-11 巨块、色块被拉长的根因。
    check('⭐⭐ 与 8-11 **不相交**的 12-13 节独立成格（压力格只跨 4 行，不再并成跨 6 行的大格）',
      !!big && big.span === 4, `span=${big && big.span}`);

    // 2) 区高 ≥ 它对应节次在原网格里的高度 → 「完全填充对应的节次」
    if (big) {
      const slack = big.tdH - big.stackH;      // td 比 stack 多出的内边距 + 边框
      const bad = big.zones.filter((z) => {
        // 该区的节次跨度 L 换算成原网格高：整格 span×58−slack，按 L 比例分
        const L = z.ub - z.ua;
        const grid = (58 - slack / big.span) * L;
        return z.h < grid - 1.5;
      });
      check('⭐ 每个区都不低于它对应节次在原网格里的高度（完全填充对应节次）',
        bad.length === 0, JSON.stringify(bad.map((z) => ({ ua: z.ua, ub: z.ub, h: z.h }))));
      check('⭐ 区高之和 == stack 高（区之间没有缝、也没溢出）',
        Math.abs(big.zoneSum - big.stackH) <= 1, `Σ区高=${big.zoneSum} stackH=${big.stackH}`);
      // 区内每块加起来要填满这个区（⚠️ 区内的 gap 也要算进去，否则永远差十几像素）
      const gapSum = big.zones.reduce((a, z) => a + Math.max(0, z.n - 1) * 2, 0);   // .zone 的 gap = 2px
      const sumSlot = big.slotH.reduce((a, b) => a + b, 0);
      check('   区内各块叠起来填满该区（Σ块高 + 区内间隙 == Σ区高）',
        Math.abs(sumSlot + gapSum - big.zoneSum) <= big.zones.length * 2,
        `Σ块高=${sumSlot} 间隙=${gapSum} Σ区高=${big.zoneSum}`);

      // 3) 虚线：只有「真的共用了节次」的相邻区才画
      const sepz = big.zones.filter((z) => z.sep);
      check('⭐⭐ 共用第 10 节的两区之间画了**虚线**（边界在第 10 节中间）',
        sepz.length >= 1 && sepz.every((z) => z.dash),
        `sep=${JSON.stringify(big.zones.map((z) => z.sep))} dash=${JSON.stringify(big.zones.map((z) => z.dash))}`);
      // ⚠️ 2026-10-01：合并判据改成「只并相交」之后，**格内的区必然两两相交**
      //    （两个不相交的区不可能落在同一格里 —— 它们各自成格了），
      //    所以原来那条「首尾相接的两区之间不画虚线（10-11 与 12-13）」已经构造不出来：
      //    它的前提正是被修掉的「相邻也并」。换成等价且**可达**的断言 ——
      //    除首区外每个区都该带虚线（都真的与上一区共用了节次）。
      check('⭐⭐ 格内每个非首区都带虚线（块内各区必然相交，不存在"首尾相接"的边界）',
        big.zones.slice(1).every((z) => z.sep === true),
        `sep=${JSON.stringify(big.zones.map((z) => z.sep))}`);
      // ⚠️ 「虚线」标在区的**顶边**（虚线挂给下面那一区）
      console.log(`   各区：` + big.zones.map((z) => `${z.sep ? '···虚线··· ' : ''}` +
        `[${z.ua}~${z.ub}) ${z.h}px ${z.n}门`).join(' | '));
    } else {
      check('⭐⭐ 共用第 10 节的两区之间画了**虚线**', false, '没找到压力用例');
      check('⭐⭐ 格内每个非首区都带虚线', false, '没找到压力用例');
    }
    // 全表：所有 sep 的区都必须真画了虚线，没 sep 的都不许有
    const allZones = info.cells.flatMap((c) => c.zones);
    check('   全表一致性：sep ⇔ 虚线',
      allZones.every((z) => z.sep === z.dash),
      JSON.stringify(allZones.filter((z) => z.sep !== z.dash).map((z) => ({ ua: z.ua, ub: z.ub }))));
    check('   单区的格子不画任何虚线',
      allZones.filter((z) => !z.sep).every((z) => !z.dash));
    console.log(`   全表 ${allZones.length} 个区，其中 ${allZones.filter((z) => z.sep).length} 个带虚线；` +
      `多区格子 ${zoned.length} 个`);

    // ⭐⭐ 行高口径（2026-10-01 第四版）：按「节次速率 × 覆盖长度」加权分摊 →
    //    **格内各行本来就不该等高**：第 8、9 节那两行承载 5 门课，就该比只待 1 门课的
    //    第 12、13 节高一截。真正该钉的是：**虚线要落在它该在的那一节行的正中**
    //    （用户口径：共用的第 10 节由相邻两组**平分**）。
    //    ⚠️ 旧的「该格跨的那几行行高一致」断言已被本版推翻 —— 别再钉它。
    if (big) {
      const dashes = big.zones.filter((z) => z.sep);
      const badB = dashes.map((z) => {
        // ⚠️ 虚线挂给**下面**那一区（.zone.sep::before 是 border-top）→ 用 z.ua 而非 z.ub
        const k = Math.floor(z.ua);                                  // 边界落在本格第 k 行（0 基）
        const before = info.rowH.slice(big.j - 1, big.j - 1 + k).reduce((a, b) => a + b, 0);
        const rowH = info.rowH[big.j - 1 + k] || 0;
        const want = before + rowH / 2;                              // 期望：该行正中
        return { ua: z.ua, rowH: rowH, want: Math.round(want), got: z.topTd,
                 err: Math.abs(z.topTd - want) };
      }).filter((x) => x.err > x.rowH * 0.3);
      check('⭐⭐ 虚线落在**它该在的那一节行的正中**（共用的第 10 节两组平分）',
        dashes.length >= 1 && badB.length === 0,
        dashes.length ? JSON.stringify(badB)
                      : `sep 区一个都没有（zones=${JSON.stringify(big.zones.map((z) => z.sep))}）`);
      const rows = info.rowH.slice(big.j - 1, big.j - 1 + big.span);
      console.log(`   该格跨的 ${big.span} 行行高 ${JSON.stringify(rows)}；虚线位置 ` +
        JSON.stringify(dashes.map((z) => ({ ua: z.ua, got: z.topTd }))));
    } else {
      check('⭐⭐ 虚线落在它该在的那一节行的正中', false, '没找到压力用例');
    }

    console.log(`   全表 ${info.slots.length} 块，高度 ${JSON.stringify(info.slots.map((s) => s.h))}`);

    // ================= ② 拨轮 =================
    console.log('\n② 开抢时刻：手动拨轮（时/分/秒，精确到秒）');

    const w0 = await ev(`(() => {
      const box = document.getElementById('startAt');
      const seg = [...document.querySelectorAll('#segDay button')];
      const cols = ['wH', 'wM', 'wS'].map((id) => {
        const el = document.getElementById(id);
        return { id, total: el.children.length, value: el.querySelectorAll('.wi[data-v]').length };
      });
      return {
        type: box.type, value: box.value,
        segLabels: seg.map((b) => b.textContent),
        segOn: seg.filter((b) => b.classList.contains('on')).map((b) => b.textContent),
        cols,
        off: document.getElementById('wheel').classList.contains('off'),
      };
    })()`);
    console.log(`   分段 ${JSON.stringify(w0.segLabels)}，当前「${w0.segOn.join()}」；` +
      `拨轮列 ${JSON.stringify(w0.cols.map((c) => c.value))}`);
    check('   原来那个文本框换成了**提交用的隐藏字段**',
      w0.type === 'hidden', `type=${w0.type}`);
    check('   默认「立即开始」→ 提交串为空（就是原来的"留空"语义）',
      w0.value === '' && w0.segOn.join() === '立即开始', `${w0.value} / ${w0.segOn}`);
    check('   三个拨轮：时 0-23、分 0-59、秒 0-59（逐秒可选）',
      w0.cols[0].value === 24 && w0.cols[1].value === 60 && w0.cols[2].value === 60,
      JSON.stringify(w0.cols.map((c) => c.value)));
    check('   「立即开始」时拨轮变灰、不参与（但仍看得见上次拨的数）', w0.off === true);

    // ⭐ 「单格高度」是三处共用的数：JS 的 WHEEL_H（要做滚动除法）、CSS 的 --wheel-h
    //   （数字格高 + 行高）、以及中间那个选中框的高度。差一个像素就会出现
    //   「选中的格和中间框对不齐」这种没法解释的偏移，所以直接钉住三处相等。
    const wg = await ev(`(() => {
      const col = document.getElementById('wH');
      const cs = getComputedStyle(col.querySelector('.wi'));
      const box = getComputedStyle(document.getElementById('wheel'), '::before');
      const root = getComputedStyle(document.documentElement);
      return { wi: cs.height, line: cs.lineHeight, box: box.height,
               v: root.getPropertyValue('--wheel-h').trim(), js: WHEEL_H };
    })()`);
    console.log(`   单格高：JS ${wg.js}px / --wheel-h ${wg.v} / 数字格 ${wg.wi}（行高 ${wg.line}）/ 选中框 ${wg.box}`);
    check('⭐ 单格高度三处一致（JS WHEEL_H == CSS --wheel-h == 数字格 == 选中框）',
      wg.v === wg.js + 'px' && wg.wi === wg.js + 'px' && wg.line === wg.js + 'px'
        && wg.box === wg.js + 'px',
      JSON.stringify(wg));

    // 滚动拨轮（模拟用户滚轮/拖动）—— 这才是真正被验证的交互路径
    await ev(`(() => {
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('wH', 14); set('wM', 30); set('wS', 7);
    })()`);
    await sleep(300);
    const afterScroll = await ev(`(() => {
      const on = ['wH','wM','wS'].map((id) => {
        const c = document.querySelector('#' + id + ' > .wi.on');
        return c ? c.textContent : null;
      });
      return { on, box: document.getElementById('startAt').value };
    })()`);
    console.log(`   拨到 ${afterScroll.on.join(':')}；「立即开始」下提交串仍为 "${afterScroll.box}"`);
    check('   拨轮真的滚到了 14:30:07（高亮的那格就是选中的）',
      afterScroll.on.join(':') === '14:30:07', afterScroll.on.join(':'));
    check('   仍是「立即开始」→ 拨轮不写进提交串（避免误定时）', afterScroll.box === '');

    // ⭐⭐ 2026-10-01 新增：滚轮**一个档位走一格** + **左键点某一格精确选它**。
    //
    // 起因（用户反馈）：原来靠原生滚动 + scroll-snap，滚轮的 delta 有多大就走多远。
    // Windows 鼠标一个档位 deltaY = 100px，而单格只有 28px → 一滑跳 3~4 格，根本拨不准。
    //
    // ⚠️ 上面那段断言是直接 `el.scrollTop = v * WHEEL_H`（**绕过了滚轮**），
    //    所以「一滑跳好几格」它根本验不出来 —— 这一节必须派发**真的 wheel 事件**。
    // ⚠️ 拨轮在「立即开始」下是 .off（故意不响应滚轮），先切「明天」把它激活。
    await ev(`document.querySelector('#segDay button[data-day="1"]').click()`);
    await sleep(150);

    const wh = await ev(`(() => {
      // 派发真事件。cancelable 必须 true，否则 dispatchEvent 恒返回 true，
      // 就验不出我们到底有没有 preventDefault（＝页面会不会跟着一起滚）。
      const fire = (id, dy, mode) => document.getElementById(id).dispatchEvent(
        new WheelEvent('wheel', { deltaY: dy, deltaMode: mode || 0, bubbles: true, cancelable: true }));
      const vals = () => [WHEELS.wH.v, WHEELS.wM.v, WHEELS.wS.v];
      const reset = () => { setWheel(WHEELS.wH, 14); setWheel(WHEELS.wM, 30); setWheel(WHEELS.wS, 7); };
      const o = {};

      reset();
      o.base = vals();
      // (1) 一个鼠标档位 = deltaY 100px ≈ 3.6 格 → 只准走 **1** 格
      o.wheelPrevented = !fire('wH', 100);
      o.oneNotch = vals();
      // (2) 反向一档 → 退回原处（累加器不能留零头，否则会多退一格）
      fire('wH', -100);
      o.backNotch = vals();
      // (3) 一次给三个档位的量 → 仍然只走 1 格（"每个事件最多一格"）
      fire('wH', 300);
      o.bigOnce = vals();
      // (4) 触控板：一次只发几像素、但极密 → 累加够一格才动
      reset();
      for (let i = 0; i < 5; i++) fire('wH', 5);   // 5×5/28 = 0.89 格 → 不许动
      o.tp5 = vals();
      fire('wH', 5);                               // 第 6 次累计 1.07 格 → 正好走 1 格
      o.tp6 = vals();
      // (5) ↑/↓ 方向键也是一格（三种输入方式里最精准的那档）。收尾停在 15 时。
      reset();
      document.getElementById('wH').dispatchEvent(new KeyboardEvent('keydown',
        { key: 'ArrowDown', bubbles: true, cancelable: true }));
      o.arrow = vals();
      return o;
    })()`);
    console.log(`   滚轮一档：${wh.base.join(':')} → ${wh.oneNotch.join(':')}` +
      `（反向 ${wh.backNotch.join(':')}）；一次给三档 → ${wh.bigOnce.join(':')}`);
    check('⭐⭐ 鼠标滚轮**一个档位只走一格**（deltaY=100 不再一滑跳 3~4 格）',
      wh.oneNotch[0] === wh.base[0] + 1 && wh.oneNotch.join() === (wh.base[0] + 1) + ',30,7',
      `${wh.base.join(':')} → ${wh.oneNotch.join(':')}`);
    check('   滚轮被我们接管了（preventDefault）→ 页面不会跟着一起滚',
      wh.wheelPrevented === true);
    check('   反向一档退回原处（累加器没留零头）',
      wh.backNotch.join() === wh.base.join(), wh.backNotch.join(':'));
    check('   单个 wheel 事件最多只走一格（给三档的量也只走 1 格）',
      wh.bigOnce[0] === wh.base[0] + 1, wh.bigOnce.join(':'));
    check('⭐ 触控板式微移会累加：5 次 ×5px 不动，第 6 次才走一格',
      wh.tp5[0] === 14 && wh.tp6[0] === 15, `${wh.tp5.join(':')} → ${wh.tp6.join(':')}`);
    check('   ↓ 方向键走一格', wh.arrow[0] === 15, wh.arrow.join(':'));
    // ⚠️ `#startAt` 不是 setWheel 同步写的：setWheel 只改滚动位置 + 高亮，
    //    提交串要等**异步的 scroll 事件**回来才重算。所以在同一个同步块里读它是旧值
    //    （踩过：这里直接读，读到 14:30:07，白怀疑了半天同步逻辑）。
    await sleep(250);
    const whBox = await ev(`document.getElementById('startAt').value`);
    check('   拨轮动过之后提交串跟着同步（界面显示 15:30:07 = 交给后端的）',
      whBox.endsWith('15:30:07'), whBox);

    // 左键点某一格 → 精确选它（不用先滚过去、也不用担心吸歪半格）
    const wc = await ev(`(() => {
      const col = document.getElementById('wM');
      const before = WHEELS.wM.v;
      col.querySelector('.wi[data-v="42"]').click();
      const after = WHEELS.wM.v;
      const onNow = (col.querySelector('.wi.on') || {}).textContent;
      // 上下两块"垫片"不是数字（没有 data-v）→ 点了什么都不该发生
      col.firstElementChild.click();
      col.lastElementChild.click();
      return { before, after, onNow, afterGap: WHEELS.wM.v };
    })()`);
    await sleep(250);
    const wcBox = await ev(`document.getElementById('startAt').value`);
    console.log(`   点「42」：${wc.before} → ${wc.after}（高亮 ${wc.onNow}），提交串 ${wcBox}`);
    check('⭐⭐ 左键点哪一格就选中哪一格（点 42 分 → 就是 42 分）',
      wc.before === 30 && wc.after === 42 && wc.onNow === '42',
      `${wc.before} → ${wc.after}，高亮 ${wc.onNow}`);
    check('   点选后提交串同步（时=15 是上一步方向键留下的，只该变分）',
      wcBox.endsWith('15:42:07'), wcBox);
    check('   点上下垫片（不是数字）无效', wc.afterGap === 42, String(wc.afterGap));

    // 「立即开始」时拨轮是灰的 → 不该抢滚轮、也不该给"可点"的光标
    const wo = await ev(`(() => {
      document.querySelector('#segDay button[data-day="now"]').click();
      const col = document.getElementById('wM');
      const off = document.getElementById('wheel').classList.contains('off');
      const before = WHEELS.wM.v;
      const notPrevented = col.dispatchEvent(new WheelEvent('wheel',
        { deltaY: 100, deltaMode: 0, bubbles: true, cancelable: true }));
      const afterWheel = WHEELS.wM.v;
      col.querySelector('.wi[data-v="5"]').click();
      const afterClick = WHEELS.wM.v;
      const cursor = getComputedStyle(col.querySelector('.wi[data-v="5"]')).cursor;
      // 收尾：切回「明天」并复原成 14:30:07（后面的用例还要接着用）
      document.querySelector('#segDay button[data-day="1"]').click();
      setWheel(WHEELS.wH, 14); setWheel(WHEELS.wM, 30); setWheel(WHEELS.wS, 7);
      return { off, before, notPrevented, afterWheel, afterClick, cursor };
    })()`);
    await sleep(250);
    const woBox = await ev(`document.getElementById('startAt').value`);
    console.log(`   灰掉时：off=${wo.off}，滚轮被拦=${!wo.notPrevented}，光标 ${wo.cursor}；` +
      `复原 → ${woBox}`);
    check('   「立即开始」时拨轮灰掉 → 滚轮不被它吃掉（页面能正常滚）',
      wo.off === true && wo.notPrevented === true, `off=${wo.off} 被拦=${!wo.notPrevented}`);
    check('   灰掉时滚轮/点击都不改值，也不给"可点"光标（免得骗人）',
      wo.afterWheel === wo.before && wo.afterClick === wo.before && wo.cursor !== 'pointer',
      `${wo.before} → ${wo.afterWheel} / ${wo.afterClick}，cursor=${wo.cursor}`);
    check('   已复原成 14:30:07（后续用例接着验）', /14:30:07$/.test(woBox), woBox);

    // 切到「明天」→ 应当拼出明天的完整日期
    await ev(`document.querySelector('#segDay button[data-day="1"]').click()`);
    await sleep(200);
    const tomorrow = await ev(`(() => {
      const v = document.getElementById('startAt').value;
      const d = new Date(); d.setDate(d.getDate() + 1);
      const want = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0')
                 + '-' + String(d.getDate()).padStart(2,'0');
      const echo = document.getElementById('startEcho');
      return { v, want, echoShown: echo.style.display !== 'none',
               echoCls: echo.className, echoTxt: echo.textContent.replace(/\\s+/g,' ').trim() };
    })()`);
    console.log(`   明天 → "${tomorrow.v}"；回显「${tomorrow.echoTxt}」`);
    check('⭐ 切「明天」拼出**完整日期**（不是 "14:30:07" 那种今天简写）',
      tomorrow.v === `${tomorrow.want} 14:30:07`, `${tomorrow.v} (期望 ${tomorrow.want} 14:30:07)`);
    check('   拨轮的即时回显出来了，并说明"还有多久"',
      tomorrow.echoShown && /将在/.test(tomorrow.echoTxt) && /还有/.test(tomorrow.echoTxt),
      tomorrow.echoTxt);

    // 拨到一个**已经过去**的时刻 → 必须告警 + 禁用「开始」；不许偷偷顺延/静默开火
    const past = await ev(`(() => {
      document.querySelector('#segDay button[data-day="0"]').click();
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('wH', 0); set('wM', 0); set('wS', 0);
      return true;
    })()`);
    await sleep(400);
    const pastRes = await ev(`(() => {
      const echo = document.getElementById('startEcho');
      return { v: document.getElementById('startAt').value, cls: echo.className,
               txt: echo.textContent.replace(/\\s+/g,' ').trim(),
               disabled: document.getElementById('btnStart').disabled };
    })()`);
    console.log(`   拨到过去（"${pastRes.v}"）→ ${pastRes.cls}；开始按钮 disabled=${pastRes.disabled}`);
    check('⭐⭐ 拨到**已经过去**的时刻会被拦下（不偷偷开火、也不偷偷顺延到明天）',
      /已经过去/.test(pastRes.txt) && /\bwarn\b/.test(pastRes.cls) && pastRes.disabled === true,
      `${pastRes.cls} disabled=${pastRes.disabled} ${pastRes.txt}`);

    // 拨到**未来**（今天 +150 秒）→ 按钮恢复可用
    const target = await ev(`(() => {
      const d = new Date(Date.now() + 150000);
      const set = (id, v) => { document.getElementById(id).scrollTop = v * WHEEL_H; };
      set('wH', d.getHours()); set('wM', d.getMinutes()); set('wS', d.getSeconds());
      return { h: d.getHours(), m: d.getMinutes(), s: d.getSeconds(), ts: d.getTime() };
    })()`);
    await sleep(500);
    const futRes = await ev(`(() => {
      const echo = document.getElementById('startEcho');
      return { v: document.getElementById('startAt').value, cls: echo.className,
               disabled: document.getElementById('btnStart').disabled,
               // 从页面上读回"这一刻"的绝对时间戳，稍后跟后端对账
               ts: new Date(document.getElementById('startAt').value.replace(/-/g,'/')).getTime() };
    })()`);
    console.log(`   拨到未来 → "${futRes.v}"（${futRes.cls}），开始按钮 disabled=${futRes.disabled}`);
    check('   拨到未来 → 按钮可用、回显是正常提示（info）',
      futRes.disabled === false && /\binfo\b/.test(futRes.cls), `${futRes.cls} disabled=${futRes.disabled}`);
    check('   拨轮显示的 h/m/s 与目标一致（拨轮没算错位）',
      futRes.v.endsWith(`${pad2(target.h)}:${pad2(target.m)}:${pad2(target.s)}`),
      `${futRes.v} vs ${pad2(target.h)}:${pad2(target.m)}:${pad2(target.s)}`);

    // ================= ③ 真的点「开始抢课」：显示值必须等于后端执行值 =================
    console.log('\n③ 点「开始抢课」：界面拨的那一刻 = 后端真的那一刻');

    await ev(`document.getElementById('btnStart').click()`);
    await sleep(2500);
    const st1 = await state();
    const srvStart = st1.schedule && st1.schedule.start_at;
    console.log(`   界面 ${futRes.v}（${futRes.ts}） → 后端 start_at=${srvStart}`);
    check('⭐⭐ 后端拿到的开抢时刻与拨轮显示的**同一秒**（误差 ≤ 2s，秒级对齐）',
      !!srvStart && Math.abs(srvStart * 1000 - futRes.ts) <= 2000,
      `srv=${srvStart} local=${futRes.ts / 1000}`);

    await jpost('/api/stop', {});
    await sleep(800);

    check('全程没有 JS 未捕获异常', errors.length === 0, errors.slice(0, 3).join(' | '));
  } catch (e) {
    console.log('   ✗ 异常：' + (e && e.message ? e.message : e));
    fails.push('异常：' + (e && e.message ? e.message : e));
  } finally {
    try { await jpost('/api/stop', {}); } catch (_) {}
    chrome.kill();
    await sleep(400);
    console.log('='.repeat(64));
    console.log(fails.length ? `===== 失败 ${fails.length} 条 =====` : '===== 全部通过 =====');
    for (const f of fails) console.log('   ✗ ' + f);
    process.exit(fails.length ? 1 : 0);
  }
})();
