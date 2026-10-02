// 验收：说明书「可视化编辑」弹窗（分块编辑 + 图片上传 + 保存/取消）。
//
// 打**隔离实例**（默认 8721）：XK_BASE=http://127.0.0.1:8721 node tests/verify_manual_edit.js
//
// ⚠️ 这个脚本**会写**：它真的去 PUT /api/manual（落到 `manual/manual.json`）。
//    所以开头先把当前内容读出来存着，`finally` 里原样 PUT 回去 —— 跑完你的内容一点没变。
//    上传的图片也会在 finally 里删掉（只删这一跑自己建的那个内容寻址文件名）。
//
// ⚠️ 编辑里用到 window.confirm（关掉未保存的改动 / 删页），CDP 里默认会**自动取消**，
//    等于「点了取消」。所以这里挂 Page.javascriptDialogOpening → 自动 accept。
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const os = require('os');

const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DEBUG_PORT = Number(process.env.XK_CDP_PORT || 9808);
const BASE = (process.env.XK_BASE || 'http://127.0.0.1:8721').replace(/\/$/, '');
const PROFILE = path.join(os.tmpdir(), 'xk_manual_edit_profile');
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
  const listeners = new Set();
  ws.addEventListener('message', (raw) => {
    let m = null;
    try { m = JSON.parse(raw.data); } catch (_) { return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      errors.push((d.exception && (d.exception.description || d.exception.value)) || d.text || 'unknown');
    }
    listeners.forEach((fn) => fn(m));
  });
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    const on = (ev) => { const m = JSON.parse(ev.data); if (m.id === mid) { ws.removeEventListener('message', on); res(m); } };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ws, send, errors, on: (fn) => listeners.add(fn) };
}

// 一次很小的合法 PNG（1×1 透明）—— 用来验证「上传图片」这条路
const TINY_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';

let uploadedName = '';
let backup = null;

(async () => {
  console.log(`目标实例：${BASE}`);
  console.log('='.repeat(64));
  let ev = null;
  try {
    let ver = null;
    for (let i = 0; i < 40; i++) {
      try { ver = await json('/json/version'); break; } catch (e) { await sleep(300); }
    }
    if (!ver) throw new Error('chrome 没起来');
    const tabs = await json('/json/list');
    const t = tabs.find((x) => x.type === 'page');
    const { send, errors, on } = await connect(t.webSocketDebuggerUrl);

    // ⚠️ 编辑里有 confirm：CDP 默认会把它当「取消」→ 必须自动接受，否则「删页」永远删不掉
    on((m) => {
      if (m.method === 'Page.javascriptDialogOpening') {
        send('Page.handleJavaScriptDialog', { accept: true });
      }
    });

    ev = async (expr) => {
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

    // ---------- 前置：先把现有内容备份（跑完原样还回去） ----------
    const pre = await ev(`fetch('/api/manual',{cache:'no-store'}).then(r => r.json()).then(d => ({
      source: d.source, writable: !!d.writable,
      pages: (d.manual && d.manual.pages) ? d.manual.pages.length : 0,
      doc: d.manual || null,
    }))`);
    check('前置：/api/manual 可用且内容可写', pre.source === 'external' && pre.writable && pre.pages >= 1,
      JSON.stringify({ source: pre.source, writable: pre.writable, pages: pre.pages }));
    backup = pre.doc;
    if (!backup) throw new Error('拿不到备份内容，不敢往下跑');

    // ---------- 打开编辑弹窗 ----------
    console.log('-- 打开编辑弹窗 --');
    await ev(`document.getElementById('btnManual').click(); true`);
    await sleep(800);
    await ev(`document.getElementById('btnManualEdit').click(); true`);
    await sleep(400);

    const open = await ev(`(() => {
      const m = document.getElementById('manualEditModal');
      const st = document.getElementById('edState').textContent.trim();
      return {
        shown: getComputedStyle(m).display !== 'none',
        locked: document.body.classList.contains('modal-open'),
        state: st,
        blocks: document.querySelectorAll('#edBlocks .blk').length,
        saveDisabled: document.getElementById('btnEditSave').disabled,
        saveText: document.getElementById('btnEditSave').textContent.trim(),
        title: document.getElementById('edPageTitle').value,
        tabs: document.querySelectorAll('#edPages .ed-tab').length,
        types: [...document.querySelectorAll('#edNewType option')].map(o => o.value),
      };
    })()`);
    check('点「编辑内容」→ 编辑弹窗打开', open.shown);
    check('编辑弹窗也锁背景滚动', open.locked);
    check('状态行是「第 1 / N 页 · M 块」', /^第 1 \/ \d+ 页 · \d+ 块$/.test(open.state), open.state);
    check('页签数与内容页数一致', open.tabs === pre.pages, `${open.tabs} / ${pre.pages}`);
    check('块列表渲染出第 1 页的块', open.blocks >= 1, `实际 ${open.blocks}`);
    check('「本页标题」带出了第 1 页标题', open.title === backup.pages[0].title, `「${open.title}」`);
    check('7 种块类型都在下拉里',
      ['p', 'h3', 'list', 'img', 'note', 'kv', 'table'].every((t) => open.types.includes(t)),
      JSON.stringify(open.types));
    check('还没改动 →「保存」是禁用的「已保存」', open.saveDisabled === true && open.saveText === '已保存',
      JSON.stringify({ d: open.saveDisabled, t: open.saveText }));

    // ---------- 加一块 / 编辑 / 上移 / 删除 ----------
    console.log('-- 分块编辑 --');
    await ev(`(() => { const s = document.getElementById('edNewType'); s.value = 'h3';
      document.getElementById('btnAddBlock').click(); return true; })()`);
    await sleep(200);
    const added = await ev(`(() => ({
      blocks: document.querySelectorAll('#edBlocks .blk').length,
      lastType: document.querySelector('#edBlocks .blk:last-child .blk-type').textContent.trim(),
    }))()`);
    check('「+ 加一块」块数 +1', added.blocks === open.blocks + 1, JSON.stringify(added));
    check('新块类型是「小标题」', added.lastType === '小标题', added.lastType);

    await ev(`(() => {
      const ta = document.querySelector('#edBlocks .blk:last-child textarea');
      ta.value = '（测试）自动化写入的小标题';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    await sleep(150);
    const typed = await ev(`(() => ({
      val: document.querySelector('#edBlocks .blk:last-child textarea').value,
      dirty: document.getElementById('edState').textContent.includes('未保存'),
      saveDisabled: document.getElementById('btnEditSave').disabled,
      saveText: document.getElementById('btnEditSave').textContent.trim(),
    }))()`);
    check('textarea 的输入写进了模型（值还在）', typed.val === '（测试）自动化写入的小标题', typed.val);
    check('一改动，状态行就标「未保存」', typed.dirty);
    check('一改动，「保存」变可点且文案回到「保存」', typed.saveDisabled === false && typed.saveText === '保存',
      JSON.stringify({ d: typed.saveDisabled, t: typed.saveText }));

    // 上移：把最后一块挪上去，类型序列应当变化
    const beforeMove = await ev(`[...document.querySelectorAll('#edBlocks .blk-type')].map(e => e.textContent.trim())`);
    await ev(`(() => { const b = document.querySelector('#edBlocks .blk:last-child');
      b.querySelector('.blk-head button[title="这一块上移"]').click(); return true; })()`);
    await sleep(200);
    const afterMove = await ev(`[...document.querySelectorAll('#edBlocks .blk-type')].map(e => e.textContent.trim())`);
    check('「↑」真的换了位置（类型序列变了）',
      JSON.stringify(beforeMove) !== JSON.stringify(afterMove),
      JSON.stringify({ beforeMove, afterMove }));

    await ev(`(() => { const b = document.querySelector('#edBlocks .blk:last-child');
      b.querySelector('.blk-head button[title="删掉这一块"]').click(); return true; })()`);
    await sleep(200);
    const afterDel = await ev(`document.querySelectorAll('#edBlocks .blk').length`);
    check('「✕ 删除」块数 -1', afterDel === added.blocks - 1, `实际 ${afterDel}`);

    // ---------- 改本页标题 / 加页 / 删页 ----------
    console.log('-- 页管理 --');
    await ev(`(() => { const i = document.getElementById('edPageTitle');
      i.value = '（测试）改过的标题'; i.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    await sleep(200);
    const tabTxt = await ev(`document.querySelector('#edPages .ed-tab').textContent.trim()`);
    check('改「本页标题」→ 页签文字跟着变', tabTxt.includes('（测试）改过的标题'), tabTxt);

    await ev(`[...document.querySelectorAll('#edPages button')].filter(b => b.textContent.trim() === '+ 加一页')[0].click(); true`);
    await sleep(250);
    const addedPage = await ev(`(() => ({
      tabs: document.querySelectorAll('#edPages .ed-tab').length,
      state: document.getElementById('edState').textContent.trim(),
      title: document.getElementById('edPageTitle').value,
    }))()`);
    check('「+ 加一页」页数 +1', addedPage.tabs === pre.pages + 1, JSON.stringify(addedPage));
    check('加完自动切到新页（状态行第 N / N 页）',
      addedPage.state.startsWith(`第 ${addedPage.tabs} / ${addedPage.tabs} 页`), addedPage.state);

    await ev(`(() => { const tabs = document.querySelectorAll('#edPages .ed-tab');
      tabs[tabs.length - 1].querySelector('.dl').click(); return true; })()`);
    await sleep(250);
    const delPage = await ev(`document.querySelectorAll('#edPages .ed-tab').length`);
    check('「✕」删页 → 页数回退（confirm 被自动接受）', delPage === pre.pages, `实际 ${delPage}`);

    // ⚠️ 删页会把当前页挪到末尾，后面的断言都是按「第 1 页」写的 → 先切回第 1 页
    await ev(`document.querySelector('#edPages .ed-tab .nm').click(); true`);
    await sleep(200);
    const backTo1 = await ev(`document.getElementById('edState').textContent.trim()`);
    check('点页签能切回第 1 页', backTo1.startsWith('第 1 / '), backTo1);

    // ---------- 上传图片 ----------
    console.log('-- 图片上传 --');
    const up = await ev(`(async () => {
      const b64 = '${TINY_PNG_B64}';
      const png = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      // ⚠️ 末尾追加 8 个随机字节：内容寻址的名字每次都不一样，
      //    免得跟「用户自己传过的同名文件」撞上（清理时误删别人的东西）。
      const extra = new Uint8Array(8);
      crypto.getRandomValues(extra);
      const bytes = new Uint8Array(png.length + extra.length);
      bytes.set(png, 0); bytes.set(extra, png.length);
      const f = new File([bytes], 'xk-probe.png', { type: 'image/png' });
      const d = await uploadManualImage(f);
      const st = await fetch(d.url, { cache: 'no-store' }).then(r => r.status);
      return { url: d.url, name: d.name, bytes: d.bytes, status: st };
    })()`);
    uploadedName = up.name;
    check('上传图片返回 /manual-asset/… 地址', /^\/manual-asset\/[0-9a-f]{12}\.png$/.test(up.url), up.url);
    check('上传后能按这个地址取到图（200）', up.status === 200, `实际 ${up.status}`);
    check('图片真的落到了 manual/img/ 下', fs.existsSync(path.join(ROOT, 'manual', 'img', up.name)), up.name);

    // 放一个图片块，正文指向刚上传的地址 → 缩略图应当能显示
    await ev(`(() => { const s = document.getElementById('edNewType'); s.value = 'img';
      document.getElementById('btnAddBlock').click(); return true; })()`);
    await sleep(250);
    await ev(`(() => {
      const blk = document.querySelector('#edBlocks .blk:last-child');
      const url = blk.querySelector('input[type=text]');
      url.value = ${JSON.stringify(up.url)};
      url.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    await sleep(200);
    const imgState = await ev(`(() => {
      const blk = document.querySelector('#edBlocks .blk:last-child');
      return {
        type: blk.querySelector('.blk-type').textContent.trim(),
        url: blk.querySelector('input[type=text]').value,
      };
    })()`);
    check('新加的是「图片」块', imgState.type === '图片', imgState.type);
    check('图片块的地址栏能编辑（并写回模型）', imgState.url === up.url, imgState.url);

    // ---------- 保存 ----------
    console.log('-- 保存 --');
    const wantBlocks = await ev(`document.querySelectorAll('#edBlocks .blk').length`);
    await ev(`document.getElementById('btnEditSave').click(); true`);
    await sleep(900);
    const saved = await ev(`(() => ({
      editorOpen: getComputedStyle(document.getElementById('manualEditModal')).display !== 'none',
      pos: document.getElementById('manualPos').textContent.trim(),
    }))()`);
    check('保存后编辑弹窗自动关掉', saved.editorOpen === false);
    check('阅读弹窗页码跟着更新（1 / 页数）', saved.pos === '1 / ' + pre.pages, saved.pos);

    const after = await ev(`fetch('/api/manual',{cache:'no-store'}).then(r => r.json()).then(d => ({
      pages: d.manual.pages.length,
      page1blocks: d.manual.pages[0].blocks.length,
      page1title: d.manual.pages[0].title,
      hasUpload: JSON.stringify(d.manual).includes('${up.url}'),
    }))`);
    check('后端内容页数 = 编辑时的页数', after.pages === pre.pages, `${after.pages} / ${pre.pages}`);
    check('后端第 1 页块数 = 编辑时的块数', after.page1blocks === wantBlocks, `${after.page1blocks} / ${wantBlocks}`);
    check('后端第 1 页标题 = 改过的标题', after.page1title === '（测试）改过的标题', after.page1title);
    check('图片块（含上传地址）真写进去了', after.hasUpload === true);

    // ---------- 取消 = 丢弃 ----------
    console.log('-- 取消不落盘 --');
    await ev(`document.getElementById('btnManualEdit').click(); true`);
    await sleep(400);
    await ev(`(() => { const i = document.getElementById('edPageTitle');
      i.value = '不该被保存的标题'; i.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    await sleep(200);
    await ev(`document.getElementById('btnEditCancel').click(); true`);
    await sleep(400);
    const cancelled = await ev(`fetch('/api/manual',{cache:'no-store'}).then(r => r.json())
      .then(d => ({ title: d.manual.pages[0].title,
        editorOpen: getComputedStyle(document.getElementById('manualEditModal')).display !== 'none' }))`);
    check('点「取消」关掉编辑弹窗', cancelled.editorOpen === false);
    check('「取消」不落盘（后端标题还是上次保存的）', cancelled.title === '（测试）改过的标题', cancelled.title);

    await ev(`document.getElementById('btnManualEdit').click(); true`);
    await sleep(400);
    const reopened = await ev(`document.getElementById('edPageTitle').value`);
    check('重新打开编辑器 → 标题回到已保存的那份', reopened === '（测试）改过的标题', reopened);

    // ---------- 目录不可写 → 禁用编辑 ----------
    console.log('-- 只读守卫 --');
    const ro = await ev(`(() => {
      const old = _manualWritable;
      _manualWritable = false;
      updateManualEditBtn();
      const b = document.getElementById('btnManualEdit');
      const out = { disabled: b.disabled, title: b.title };
      _manualWritable = old;
      updateManualEditBtn();
      out.restored = document.getElementById('btnManualEdit').disabled;
      return out;
    })()`);
    check('目录不可写 →「编辑内容」禁用（并给出提示）',
      ro.disabled === true && ro.title.includes('不可写'), JSON.stringify(ro));
    check('恢复可写后按钮重新可用', ro.restored === false);

    // ---------- 运行期异常 ----------
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
    // ⭐ 无论如何都把内容还原成跑之前那一份（这个脚本真的写了磁盘）
    try {
      if (ev && backup) {
        await ev("(async () => { await api('PUT', '/api/manual', { manual: "
          + JSON.stringify(backup) + " }); return 1; })()");
        console.log('（已把说明书内容还原成跑之前的样子）');
      }
    } catch (_) { console.log('⚠️ 还原说明书内容失败，请检查 manual/manual.json'); }
    // 删掉这一跑自己上传的图片（内容寻址的名字，只删这一个）
    try {
      if (uploadedName) fs.unlinkSync(path.join(ROOT, 'manual', 'img', uploadedName));
    } catch (_) { /* ignore */ }
    try { chrome.kill(); } catch (_) { /* ignore */ }
  }
  process.exit(fails.length === 0 ? 0 : 1);
})();
