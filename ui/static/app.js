/* ==========================================================================
   南苑抢课助手 · 前端逻辑
   纯原生 JS，零依赖。所有网络调用都走本地后端 API。
   ========================================================================== */

'use strict';

// ---------- 常量 ----------

//: ⭐ 「抢课」单项的尝试次数上限（2026-10-01 由 50 下调到 20）。
//:
//: 🔴 这是 `engine/plan.py::MAX_ATTEMPTS` 的**镜像**：前端零构建、没法 import 后端，
//:    所以只能各写一份。**改一处必须改另一处**（与 58px 行高那对的纪律相同）。
//:
//: ⚠️ 它只管「抢课」。将来「蹲课」（蹲退课名额）的时间尺度是小时级，
//:    不要图省事把这个数调大 —— 那会把抢课也一起拖长。
const MAX_ATTEMPTS = 20;

// ---------- 小工具 ----------

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const opt = { method, headers: {} };
  // 访问口令（防同网段他人访问）：校验通过后存 localStorage，每次请求带上。
  const key = localStorage.getItem('xk_access_key');
  if (key) opt.headers['X-Access-Key'] = key;
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* 可能是空响应 */ }
  if (!r.ok) {
    const d = data && data.detail;
    const msg = (d && typeof d === 'object') ? (d.message || d.detail || '请求失败')
              : (d || data || ('HTTP ' + r.status));
    const err = new Error(msg);
    err.status = r.status;
    err.kind = (d && d.kind) || (data && data.kind);
    err.raw = d && d.raw;
    err.retryable = d && d.retryable;
    // 乐观锁冲突（409）时后端会把**服务器当前的**版本号带回来
    err.planRev = d && d.plan_rev;
    err.count = d && d.count;
    // 登录态失效：立刻把顶部胶囊翻成红的。
    // 只翻牌不播日志 —— 调用方自己会 logLine(e.message)，避免同一件事打两遍。
    if (err.kind === 'session_expired') noteSessionExpired(err.message);
    // 访问口令错误/缺失：弹出访问码层，让用户重输。
    if (err.kind === 'access_denied') {
      localStorage.removeItem('xk_access_key');
      showAccessGate();
    }
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });

// ---------- 状态 ----------

const S = {
  courses: [],
  plan: [],          // {kch_id, kklxdm, kcmc, jsxx, do_id, cxbj, fxbj, priority, slots, ...}
  tabs: [],          // 课程类别 Tab（每个 Tab 独立选课上下文）
  tabsCached: false, // 上面这批 Tab 是否来自缓存（未开放期解析不到就沿用上次的）
  running: false,
  hydrated: false,   // 是否已从后台 /api/state 恢复过清单（首次加载）
  planRev: null,     // 后端清单版本号（乐观锁）：提交时原样带回去，对不上就 409
  // 后端认不认识「派发方式」（serial / round_robin）这个字段。
  // null = 还没问过；false = 后端较旧（静态文件按需读盘会先于后端重启生效），
  // 这时下拉要禁用并说明原因 —— 不许出现「界面选了轮流、实际在串行」。
  modeSupported: null,

  // ---- 会话（登录态）----
  // ⚠️ 「持有凭据」≠「登录态有效」：凭据在服务端内存里不会自己过期，
  // 但教务那边的会话会（退出登录 / Cookie 到期 / 被挤下线）。所以看 sessionState，
  // 不看 hasSession；并靠 /api/session/check 定期真的打一次教务来核实。
  sessionState: 'none',   // none | expired | ok | unverified
  sessionInvalidMsg: '',
  sessionCheckedAt: 0,    // 服务端上次**联网核实**的时刻（epoch 秒），0=从没核实过（仅供展示）
  sessionNextCheckMs: 0,  // 下次允许探活的**本地**时刻（ms）；成功失败都退避，防空转
  sessionChecking: false, // 探活请求是否在飞（防止轮询叠请求）
  planMeta: null,    // 清单落盘槽位描述（路径/项数/存入时刻/是否用户主动加载而来）
  snapshot: null,    // **磁盘上**「上次保存的数据」的元信息（只读）；内存有清单时为 null
  snapshotDismissed: false, // 本次会话内用户已点 ✕ 收起「上次保存的数据」条（不删磁盘快照）
  coursesSnap: null, // **磁盘上**「上次搜到的课程」的摘要（按课程类别分桶）
  clock: null,       // 时钟校准快照（/api/state 下发的 clock）
  schedule: null,    // 定时开抢信息 {start_at, warmup_s, fire_lead_s, server_now}
  credit: null,      // 学分要求快照（/api/state 下发的 credit，含清单待加选学分）
  seq: 0,
  evtSource: null,

  // ---- 已选课程（只在建会话 / 点刷新时拉取，绝不进轮询）----
  selected: [],          // 规范化后的已选课程（含 slots）
  selectedLoaded: false,
  selectedAt: 0,

  // ---- 课表与冲突 ----
  conflicts: {},     // key(=do_id 或 kch_id) -> {level, hits, count}
  conflictsKnown: false, // 已选是否已加载（没加载就判不了冲突）
  mutex: [],         // 清单内部互斥的项对 [[i, j], ...]，来源于后端 Plan.conflict_pairs()
  weekFilter: 0,     // 0=全部周次
  expanded: new Map(),   // boxId -> {i, items}，用于清单变化后重算冲突

  // ---- 蹲课（2026-10-01，独立清单）----
  wait: [],          // 蹲课清单项（与 S.plan 同构）
  waitRev: null,     // 蹲课清单版本号（乐观锁）
  waitHydrated: false,
  waitRunning: false,
  waitInterval: 1000,// 请求间隔 ms（800 / 1000 两档）
  waitStartAt: null, // 开始时刻（unix 秒，来自后端回显）
  waitDeadlineS: 0,  // 结束-开始的秒数（后端回显）
  waitDeadlineAt: null, // 结束时刻（unix 秒，来自 /api/wait/plan 回显）
};

// ---------- 访问口令（防同网段他人访问） ----------

/* 访问码遮罩层。开启口令保护时，它是**第一道门**：输对口令才能看到登录门。
   口令校验通过后存 localStorage.xk_access_key，之后 api() 每次请求自动带上。
   ⚠️ 不要无条件清空输入框：用户可能正在输入，反复 showAccessGate 会把数字清掉。 */
function showAccessGate() {
  const gate = $('accessGate');
  if (!gate) return;
  const wasHidden = gate.style.display === 'none';
  gate.style.display = '';
  const input = $('accessKeyInput');
  if (input && wasHidden) { input.value = ''; setTimeout(() => input.focus(), 0); }
}

function hideAccessGate() {
  const gate = $('accessGate');
  if (gate) gate.style.display = 'none';
}

/* 口令校验通过后调它，让「等待口令」的 init 继续往下走。 */
let _resolveAccess = null;
function _accessGranted() {
  if (_resolveAccess) { const r = _resolveAccess; _resolveAccess = null; r(); }
}

/* 校验访问口令：查 /api/access/status 决定是否弹访问码层。
   返回一个 Promise：不需要口令时立即 resolve；需要口令且未通过时，
   **等到用户输对口令才 resolve**（这样 init 的轮询不会在口令通过前启动）。 */
function ensureAccess() {
  return new Promise((resolve) => {
    (async () => {
      try {
        const st = await fetch('/api/access/status').then(r => r.json());
        if (!st || !st.required) {
          localStorage.removeItem('xk_access_key');
          hideAccessGate();
          resolve();
          return;
        }
        const key = localStorage.getItem('xk_access_key');
        if (key && key.length === st.key_len) {
          const v = await fetch('/api/access/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key }),
          }).then(r => r.json());
          if (v && v.ok) { hideAccessGate(); resolve(); return; }
        }
        localStorage.removeItem('xk_access_key');
        showAccessGate();
        _resolveAccess = resolve;   // 等用户输对口令后由 _accessGranted 触发
      } catch (_) {
        hideAccessGate();
        resolve();  // 网络异常时别把人锁在门外
      }
    })();
  });
}

$('btnAccessKey').onclick = async () => {
  const input = $('accessKeyInput');
  const key = (input.value || '').trim();
  const msgEl = $('accessMsg');
  if (!key) { if (msgEl) msgEl.textContent = '请输入访问口令'; return; }
  try {
    const v = await fetch('/api/access/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }).then(r => r.json());
    if (v && v.ok) {
      localStorage.setItem('xk_access_key', key);
      if (msgEl) msgEl.textContent = '';
      hideAccessGate();
      _accessGranted();          // 让 init 继续（启动轮询等）
      refreshState();
    } else {
      if (msgEl) msgEl.textContent = '口令错误，请重试';
      input.value = '';
      input.focus();
    }
  } catch (_) {
    if (msgEl) msgEl.textContent = '网络错误，请重试';
  }
};

$('accessKeyInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('btnAccessKey').click();
});

// ---------- 顶栏胶囊 ----------

function setPill(el, text, cls, pulse) {
  el.className = 'pill' + (cls ? ' ' + cls : '');
  el.innerHTML = `<span class="dot${pulse ? ' pulse' : ''}"></span>${esc(text)}`;
}

/* 画「登录态」那枚胶囊。
   ⚠️ 判据是 S.sessionState，**不是**「服务端有没有会话对象」——
   后者只说明「内存里攥着一份凭据」，教务那边早就可能把它踢了。
   四个取值分别对应四种话术，别合并：
     none        → 未登录
     expired     → 登录已失效（红，鼠标悬停看原因）
     ok          → 已登录（真的联网核实过）
     unverified  → 已登录·未核实（黄，提醒你别太信这枚胶囊） */
function renderSessionPill() {
  const el = $('pillSession');
  switch (S.sessionState) {
    case 'ok':
      setPill(el, '已登录', 'ok');
      el.title = S.sessionCheckedAt
        ? `已联网核实：${new Date(S.sessionCheckedAt * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`
        : '';
      break;
    case 'expired':
      setPill(el, '登录已失效', 'err');
      el.title = (S.sessionInvalidMsg || '教务那边已经不认这个登录态了')
        + '\n请重新登录（输入学号密码）。';
      break;
    case 'unverified':
      setPill(el, '已登录·未核实', 'warn');
      el.title = '还没联网核实过这个登录态是否有效';
      break;
    default:
      setPill(el, '未登录', '');
      el.title = '';
  }
  renderGate();   // 登录态一变，就切换「登录门 / 主体」的显隐
}

/* ⭐ 登录门（2026-10-01）：未登录时只显示登录卡，登录成功后才进入南苑抢课助手主体。
   判据仍是 S.sessionState（不是「服务端有没有会话对象」）——与 renderSessionPill 一致：
     none / expired → 显示登录门、藏起主体；
     ok / unverified → 显示主体、藏起登录门。
   ⚠️ 顶栏胶囊（pillSession/pillOpen/pillTask 等）在登录门阶段仍保留，
   但顶部「学期信息」等主体专属胶囊一起藏掉，避免「门还没开就露半截身子」。 */
function renderGate() {
  const gate = $('loginGate');
  const main = $('mainApp');
  const logged = (S.sessionState === 'ok' || S.sessionState === 'unverified');
  if (gate) gate.style.display = logged ? 'none' : '';
  if (main) main.style.display = logged ? '' : 'none';
  // ⭐ 登录门阶段整个顶栏藏掉，只留居中的登录卡（干净的登录页）；
  // 登录成功进入主体后才显示顶栏（含学期信息 / 任务状态 / 退出登录等）。
  const topbar = $('topbar');
  if (topbar) topbar.style.display = logged ? '' : 'none';
  // 主体专属的顶栏胶囊（学期信息）只在进入主体后才有意义
  const pillSem = $('pillSemester');
  if (pillSem) pillSem.style.display = logged ? '' : 'none';
  // 未登录时顶栏其余状态胶囊也一并收起，只留标题 + 登录态一枚
  for (const id of ['pillOpen', 'pillClock', 'pillTask']) {
    const el = document.getElementById(id);
    if (el) el.style.display = logged ? '' : 'none';
  }
  // 「退出登录」按钮只在登录后（主体页）显示；登录门里没有会话可清
  const btnDrop = $('btnDrop');
  if (btnDrop) btnDrop.style.display = logged ? '' : 'none';
}

/* 任何一条接口报回「登录态失效」时立刻翻牌 + 播一条日志 —— 不必等下一次轮询，
   也不必等用户去点一下操作才发现。（服务端那边已同时记为失效。）

   ⚠️ 日志必须在这里播，不能靠调用方：`api()` 只负责翻牌、不播日志（免得同一件事
   被各调用方重复打），而大部分调用方只会记一条「XX 失败：…」，
   **不会解释「你的登录已经失效了、该怎么办」**。没有这条日志，用户看到的就是
   「胶囊突然变红、什么都没说」。 */
function noteSessionExpired(msg) {
  if (S.sessionState === 'expired') return;   // 只播一次，别刷屏
  S.sessionState = 'expired';
  if (msg) S.sessionInvalidMsg = msg;
  renderSessionPill();
  setPill($('pillOpen'), '状态未知', '');
  S.isOpen = null;
  logLine(`⚠️ 登录态已失效（${msg || '教务那边已经不认这个登录态了'}）；`
    + '请重新登录（输入学号密码）', 'err');
}

/* 定期探活：登录态这件事只能**真的打一次教务**才知道，
   服务端的内存快照永远不会自己变。一次探活就是一个请求，所以别太频。 */
const SESSION_CHECK_EVERY_MS = 45000;
function maybeCheckSession() {
  if (S.sessionChecking) return;
  if (S.sessionState === 'none' || S.sessionState === 'expired') return;  // 没什么可核的
  if (S.running) return;      // 抢课中让位（后端也会跳过，别白跑一趟）
  // ⚠️ 退避用**本地**时刻，别用服务端下发的 checked_at：
  //   · 服务端那个是 epoch，跟浏览器本地钟有偏差（本项目专门校准过这个偏差），
  //     拿它减 Date.now() 会让间隔忽长忽短；
  //   · 更要紧的是——**失败时也要退避**。只在成功时记账的话，一次网络抖动就会让
  //     3 秒轮询每轮都重试一次，白白空转。
  const now = Date.now();
  if (now < S.sessionNextCheckMs) return;
  S.sessionNextCheckMs = now + SESSION_CHECK_EVERY_MS;
  S.sessionChecking = true;
  api('GET', '/api/session/check')
    .then((r) => {
      if (r && r.skipped) return;      // 后端说抢课中，跳过
      S.sessionCheckedAt = r.checked_at || 0;
      if (S.sessionState !== 'expired') {
        S.sessionState = 'ok';
        renderSessionPill();
      }
    })
    .catch((e) => {
      // 服务端把 session_expired 翻成 409 + kind：api() 已顺手翻牌，
      // noteSessionExpired 也把原因写进日志了，这里不再重复。
      if (e.kind !== 'session_expired') logLine('会话探活失败：' + e.message, 'warn');
    })
    .finally(() => { S.sessionChecking = false; });
}

/* 拉一次状态快照。
   `opts.quietRevLog`：清单因乐观锁冲突被重新载入时用 —— 冲突原因已经由
   调用方（syncPlan）用更明确的文案报过了，这里不再重复第二条日志。 */
async function refreshState(opts) {
  const quietRevLog = !!(opts && opts.quietRevLog);
  try {
    const s = await api('GET', '/api/state');
    if (s.school) $('schoolName').textContent = s.school.name;
    // 登录卡副标题也随学校动态显示（通用化：不写死「广州南方学院」）
    if (s.school && s.school.name) {
      const sub = $('loginSchoolSub');
      if (sub) sub.textContent = s.school.name + ' · 正方教务';
    }
    // 作息表随状态下发；有变化就重画课表（否则左侧栏时间不会更新）
    if (s.school && applyJieTime(s.school.jie_time)) renderTimetable();

    // 会话与选课期状态。
    //
    // ⚠️ 这里**不能**只按 `has_session` 显示「已登录」（2026-09-30 修的真 bug）：
    // 凭据躺在服务进程内存里不会自己过期，但教务那边的会话会 —— 用户在网站点了
    // 退出登录、Cookie 到期、被别处挤下线，服务端都**不会自己知道**。
    // 于是顶部状态栏会一直显示「已登录」，与实际完全脱节。
    // 现在服务端多报一个 session_state（none/expired/ok/unverified），
    // 并且前端会定期去 /api/session/check 真的打一次教务核实。
    S.sessionState = s.session_state || (s.has_session ? 'ok' : 'none');
    S.sessionInvalidMsg = s.session_invalid_msg || '';
    S.sessionCheckedAt = s.session_checked_at || 0;
    renderSessionPill();
    // `inited` / `is_open` 同样只是**上次 init 时**的快照，登录态一失效就毫无意义
    // （不能一边说「登录已失效」一边说「选课已开放」，那是自相矛盾）。
    if (S.sessionState === 'expired') {
      setPill($('pillOpen'), '状态未知', '');
    } else if (s.inited) {
      if (s.is_open) setPill($('pillOpen'), '选课已开放', 'ok');
      else setPill($('pillOpen'), '未到选课期', 'warn');
    } else {
      setPill($('pillOpen'), '状态未知', '');
    }
    // 存下来供学分条等组件区分「未开放」与「未登录」两种读不到数据的情形
    S.isOpen = (s.inited && S.sessionState === 'ok') ? !!s.is_open : null;

    // 时钟：定时开抢的命根子，状态要一眼看见
    S.clock = s.clock || null;
    S.schedule = s.schedule || null;
    renderClock(s.clock);

    // 顶部固定栏「任务状态」胶囊：抢课**或**蹲课任一在跑都算「运行中」。
    // 蹲课是独立任务（s.wait_running），但它同样在发请求，顶部状态不该在蹲课时
    // 还显示「空闲」——用户会以为没在跑。
    if (s.running || s.wait_running) {
      setPill($('pillTask'), '运行中', 'info', true);
    } else {
      setPill($('pillTask'), '空闲', '');
    }
    S.running = s.running;
    // ⚠️ 这里**不要**去修正事件游标 `S.seq`。
    // 曾经想在这里写「S.seq > 服务端 seq 就归零」，但那是个竞态：`s` 是几秒前
    // 拉回来的快照，而 SSE 可能刚刚推来一条更大的序号 —— 于是这个判断会在
    // **同一进程内**误触发。游标该由「开始」那次显式归零（见 startBtn 处理器），
    // 陈旧游标则由后端在入口夹掉（`RUNTIME.clamp_since`）。两边各管一头，不打架。
    // 磁盘上「上次保存的数据」的元信息：内存里已有清单时后端会返回 null/{}。
    // ⚠️ 必须放在下面「恢复清单」那段**之前** —— 那段会调 renderPlan()，
    // 而 renderPlan() 里要顺带画这条快照条（清单为空时才显示）。
    S.snapshot = (s.snapshot && s.snapshot.count) ? s.snapshot : null;
    // 磁盘上「上次搜到的课程」的摘要（按课程类别分桶）。它**不随内存清单清空** ——
    // 课程数据是查课期攒下来的素材，清单加载进内存之后照样可能想回看。
    S.coursesSnap = (s.courses_snapshot && s.courses_snapshot.exists) ? s.courses_snapshot : null;
    renderCoursesSnapBtn();
    // 「开始」按钮的可用状态统一由 updateStartBtn() 判定（含"拨到了过去"这条）
    updateStartBtn();
    $('btnStop').disabled = !s.running;
    // 抢课运行中**不许改派发方式**：Plan 已经被 runner 拿在手里跑了，改了也不会生效。
    // 所以直接把下拉禁掉 —— 若只是「提示一下再让用户以为改了」，界面显示的值
    // 就会与正在执行的模式不一致（这正是这个项目栽过的「文案撒谎」）。
    // 同理：后端不认识这个字段时也禁掉（老后端重启前，选了也不会生效）。
    // ⚠️ 判据是「/api/state 有没有下发这个字段」，不是猜版本号。
    // ⚠️ 后端那边**永远会给一个字符串**（清单为空时给默认的 'serial'），
    //    所以这里 `typeof !== 'string'` 只可能是老后端（字段根本不存在）。
    //    这条断言成立的前提就写在 ui/state.py 的 summary() 里，改那边要回来看这里。
    S.modeSupported = typeof s.retry_mode === 'string' && s.retry_mode !== '';
    const _mode = $('optRetryMode');
    if (_mode) {
      _mode.disabled = !!s.running || !S.modeSupported;
      // 后端不认这个字段时，它跑的一定是串行（老默认）—— 那就把下拉**拨回串行**，
      // 别让控件停在一个「不会被执行」的值上（显示值必须等于执行值）。
      if (!S.modeSupported) _mode.value = 'serial';
    }
    renderModeHint();

    // 从后台恢复清单（并在**服务端版本号变了**时重新载入）。
    //
    // 必要性：S.plan 是前端的本地编辑副本，刷新页面（或电脑休眠唤醒）后就丢了，
    // 而定时开抢动辄要等几十分钟，中途刷新一次清单「消失」是致命的。
    //
    // ⭐ 但「恢复」必须同时满足两件事（2026-09-30 修）：
    //   ① 首次加载要恢复 —— 否则刷新页面清单就没了；
    //   ② **服务端版本号一变就要重新载入** —— 清单会被别的来源改写
    //      （另一个标签页、自检脚本、进程重启后从磁盘恢复、换学期被作废……）。
    //   旧代码只在 `!S.hydrated` 时才恢复，还额外要求「本地清单为空」——
    //   于是页面只要加载过一次，它就**永远**拿着陈旧副本，此后任何一次
    //   加课/删课/调序都会把服务端那份整个盖掉（这么清空过用户清单两次）。
    //   现在：服务端是权威，版本号不一致就重新载入；提交时再带 `base_version`，
    //   万一还是撞上（比如刚被改的那一瞬间）由后端 409 兜底。
    const rev = (typeof s.plan_rev === 'number') ? s.plan_rev : null;
    // ⚠️ 用 `>` 而不是 `!==`：版本号是单调递增的，而 /api/state 是**轮询**的。
    // 若用 `!==`，一个「发出得比我们这次提交更早、回来得更晚」的陈旧响应
    // （它的 rev 比当前小）会让页面把刚提交的清单又退回旧版本，闪一下再改回来。
    // 版本号变小只可能是服务端重启（进程内计数从 0 开始）——
    // 那种情况下**不**自动覆盖用户的本地副本，交给提交时的 409 兜底更稳妥。
    const revChanged = rev !== null && S.planRev !== null && rev > S.planRev;
    if (!S.hydrated || revChanged) {
      const first = !S.hydrated;
      S.hydrated = true;
      if (revChanged && !quietRevLog) {
        logLine(`清单已被其它页面或脚本改动（版本 ${S.planRev} → ${rev}），`
          + '已按服务端最新状态重新载入', 'warn');
      }
      S.planRev = rev;
      S.plan = (s.items || []).map((it) => ({
        kch_id: it.kch_id,
        do_id: it.do_id || '',
        jxb_id: it.jxb_id || '',     // 刷新页面后要能认回同一个教学班（令牌会换）
        kklxdm: it.kklxdm || '',
        // Tab 下标：kklxdm 会重复（两个板块课都是 06），回传它才能精确回位
        tab_index: typeof it.tab_index === 'number' ? it.tab_index : -1,
        kcmc: it.kcmc || '',
        jsxx: it.jsxx || '',
        xf: it.xf || '',             // 学分：学分条要用它算「待加选」
        jxbmc: it.jxbmc || '',
        cxbj: it.cxbj || '0',
        fxbj: it.fxbj || '0',
        priority: it.priority || 0,
        max_attempts: it.max_attempts || MAX_ATTEMPTS,
        interval_ms: it.interval_ms || 800,
        // 单项时间预算（秒，0 = 不限）。原样带回，别在这次「状态→本地副本」的
        // 往返里把它丢了 —— 丢了就等于用户的设置被静默重置。
        budget_s: it.budget_s || 0,
        precheck: it.precheck !== false,
        slots: it.slots || [],       // 课表要用它画「待选」那一层
      }));
      S.mutex = (s.mutex || []).slice();
      // 派发方式跟着**服务端那份**走：它是落盘的（Plan.retry_mode），刷新页面后
      // 界面必须显示服务端真正会用的模式，否则用户以为选了「轮流发送」而实际在串行。
      // ⚠️ 只在版本号变了 / 首次载入时回填（就在这个 if 里），避免轮询把用户在
      //    本次交互中刚改的值闪回去。选项是静态的两个，所以赋值不会被浏览器忽略。
      if (typeof s.retry_mode === 'string' && s.retry_mode) {
        const sel = $('optRetryMode');
        if (sel && sel.value !== s.retry_mode) sel.value = s.retry_mode;
      }
      renderModeHint();
      renderPlan();
      renderTimetable();             // 清单内容变了，课表上「待选」那层也要跟着变
      if (S.plan.length) {
        // ⚠️ 启动**不再自动恢复**清单（2026-09-30 用户要求），所以这里「来源」的
        // 可能只有两种：用户点了「加载查看」把本地那份拉进来（`restored=true`），
        // 或者本会话里一门门加的（`restored=false`，走的是重新载入那条路）。
        // 说清来源很有必要：恢复的是「要抢哪些课」，运行结果（已抢到/重试次数）
        // 一律重置为「待选」—— 否则界面会显示一个不成立的「已抢到」。
        const fromFile = !!(s.plan_meta && s.plan_meta.restored);
        const where = s.plan_meta && s.plan_meta.path ? `，落盘于 ${s.plan_meta.path}` : '';
        logLine(`${revChanged ? '已重新载入' : '已载入'} ${S.plan.length} 项抢课清单`
          + `（来源：${fromFile ? '本地保存的数据' : '后台会话'}`
          + `${fromFile ? '；状态已重置为「待选」' : ''}）${where}`, 'info');
      }
      // 已选课程同理：后台有缓存就直接用，**不重新打教务**。只在首次做，
      // 免得版本一变就重拉一遍（清单变了不代表已选变了）。
      if (first && s.selected && s.selected.loaded && !S.selectedLoaded) {
        loadSelected(false).catch(() => {});
      }
      // 清单里已有内容 → 补一次冲突标注（刷新页面后 S.conflicts 是空的）
      if (S.plan.length) refreshPlanConflicts().catch(() => {});
    } else {
      // 没重新载入就不动 S.planRev：这里只可能是「响应比当前版本旧」的陈旧轮询，
      // 把版本号记低一点只会让我们拿旧号去提交、被自己挡在 409 上。
      S.mutex = s.mutex || S.mutex;
    }

    if (s.items && s.items.length) renderTaskList(s.items);
    S.planMeta = s.plan_meta || null;
    S.credit = s.credit || null;
    renderCredit(S.credit);
    renderSemester(S.credit);

    // ⭐ 蹲课状态（2026-10-01）：独立清单，与抢课清单同构的 hydrate。
    //   ⚠️ 字段名跟着 ui/state.py::summary() 走：wait_items / wait_rev /
    //      wait_running / wait_interval_ms / wait_start_at / wait_deadline_s。
    //   用「版本号只增不减」同一套判断：wait_rev 单调递增，陈旧轮询响应不覆盖。
    const wrev = (typeof s.wait_rev === 'number') ? s.wait_rev : null;
    const wrevChanged = wrev !== null && S.waitRev !== null && wrev > S.waitRev;
    if (!S.waitHydrated || wrevChanged) {
      S.waitHydrated = true;
      S.waitRev = wrev;
      S.wait = (s.wait_items || []).map((it) => ({
        kch_id: it.kch_id,
        do_id: it.do_id || '',
        jxb_id: it.jxb_id || '',
        kklxdm: it.kklxdm || '',
        tab_index: typeof it.tab_index === 'number' ? it.tab_index : -1,
        kcmc: it.kcmc || '',
        jsxx: it.jsxx || '',
        xf: it.xf || '',
        jxbmc: it.jxbmc || '',
        jxdd: it.jxdd || '',
        cxbj: it.cxbj || '0',
        fxbj: it.fxbj || '0',
        priority: it.priority || 0,
        slots: it.slots || [],
        sksj: it.sksj || '',
        // ⭐ 蹲课项的运行态：won = 蹲到了名额（退课名额被抢到）；其余按运行中/pending 展示
        state: it.state || 'pending',
        attempts: it.attempts || 0,
      }));
    }
    S.waitRunning = !!s.wait_running;
    // ⭐ 运行态同步：蹲课项的 `state`（pending → running → won）**每次**轮询都在变，
    //    但它不触发 wait_rev 变化（版本号只随清单增删变）。所以这里**每轮**把后端
    //    最新 state 回填到 S.wait 对应项 —— 否则蹲到了名额，界面却迟迟不亮「已蹲到」。
    //    按 do_id 匹配（蹲课允许同课不同班，kch_id 会撞）；do_id 为空再回落到 kch_id。
    if (S.wait.length) {
      const live = (s.wait_items || []);
      for (const p of S.wait) {
        const m = live.find((x) => (p.do_id && x.do_id === p.do_id)
          || (!p.do_id && x.kch_id === p.kch_id));
        if (m) { p.state = m.state || 'pending'; p.attempts = m.attempts || 0; }
      }
    }
    S.waitInterval = (s.wait_interval_ms === 800 || s.wait_interval_ms === 1000)
      ? s.wait_interval_ms : 1000;
    S.waitStartAt = s.wait_start_at || null;
    S.waitDeadlineS = s.wait_deadline_s || 0;
    renderWait();
    updateWaitBtn();
    $('btnStopWait').disabled = !S.waitRunning;
    // 清单没走「重新载入」那条路时（例如只是空清单、或已选缓存变了），
    // renderPlan 不会被执行 —— 这里补一次，保证快照条跟着最新的 sessionState / isOpen 更新。
    renderSnapshotBar();
    return s;
  } catch (e) {
    logLine('状态刷新失败：' + e.message, 'err');
    return null;
  }
}

// ---------- 时钟与倒计时 ----------

function renderClock(c) {
  if (!c || !c.synced) {
    setPill($('pillClock'), '时钟未校准', S.running ? 'warn' : '');
    return;
  }
  const off = c.offset_ms;
  const dir = off >= 0 ? '慢' : '快';        // 服务器快 → 本地慢
  setPill($('pillClock'), `本地钟${dir} ${Math.abs(off)}ms`, 'ok');
  $('pillClock').title =
    `服务器 - 本地 = ${off >= 0 ? '+' : ''}${off} ms\n` +
    `不确定度 ±${c.uncertainty_ms} ms（${c.samples} 次采样，最小 RTT ${c.rtt_ms} ms）\n` +
    `建议提前开火 ${c.lead_ms} ms`;
}

// 服务器当前时刻 = 本地时刻 + 偏差，据此做本地倒计时（无需再问后端）
function serverNow() {
  const c = S.clock;
  const base = Date.now() / 1000;
  return c && c.synced ? base + (c.offset_ms || 0) / 1000 : base;
}

// ---------- 开抢时刻：日期分段 + 时/分/秒拨轮 ----------
//
// 目标：把开抢时刻**手动拨到某一秒**，并且
// **界面上显示的那一刻 = 真正提交给后端的那一刻**（同一个字符串）。
//
// 数据流向只有一条，任何地方想改时刻都得走它：
//     拨轮 / 分段 →-syncStartAt()-> `#startAt`.value →-planBody()-> 后端
// ⚠️ 不要新增第二条路径（比如某处直接读拨轮数字自己拼串）——
//    那样迟早出现「界面显示 12:00:03、实际提交 12:00:01」。

const WHEEL_H = 28;          // 单格高度；镜像 style.css 的 `:root --wheel-h`，改一处必须改另一处
//: ⭐ 拨轮的「一格」是多少：鼠标滚轮**每滚一个档位就走一格**（不是跳好几格）。
//:
//: 为什么必须自己接管滚轮：早先靠原生滚动 + scroll-snap，滚轮的 delta 有多大就走多远。
//: Windows 鼠标滚轮一个档位 deltaY = 100（像素），而单格只有 28px →
//: 一滑就跳 3~4 格，根本拨不准（用户 2026-10-01 的反馈）。
//: 现在每个 wheel 事件最多只走一格，多出来的零头直接丢掉。
const WHEELS = {};           // 'wH' | 'wM' | 'wS' -> {el, max, v}
let schedDay = 'now';        // 'now' = 立即开始（不定时）；'0' = 今天；'1' = 明天
const pad2 = (n) => String(n).padStart(2, '0');

function buildWheelCol(id, max, onChange) {
  const el = $(id);
  if (!el) return;
  // 上下各垫一个"空格"，好让第一个 0 / 最后一个 59 也能滚到正中。
  // ⚠️ 垫片必须与数字格等高，否则首尾两格永远吸不到中线（会歪半格）。
  const gap = `<div class="wi" style="height:${WHEEL_H}px"></div>`;
  let items = '';
  for (let i = 0; i <= max; i++) items += `<div class="wi" data-v="${i}">${pad2(i)}</div>`;
  el.innerHTML = gap + items + gap;

  const st = { el, max, v: 0, onChange: onChange || null };
  el.addEventListener('scroll', () => {
    // 滚动是高频事件，但这里只有一次除法 + 几次 class 切换，够便宜，不必节流。
    const c = Math.max(0, Math.min(max, Math.round(el.scrollTop / WHEEL_H)));
    if (c === st.v) return;
    st.v = c;
    paintWheel(st);
    if (st.onChange) st.onChange(); else syncStartAt();
  }, { passive: true });
  // 方向键：比拖滚动条精准，顺带让这个控件能被键盘操作
  el.addEventListener('keydown', (e) => {
    const d = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    setWheel(st, st.v + d);
  });
  // ⭐ 鼠标滚轮：**一个档位走一格**。
  //
  // 不同设备/不同浏览器的 deltaY 量纲不一样，先归一成「格」再累加：
  //   · deltaMode = 0（像素，绝大多数）：一格 = WHEEL_H 像素
  //   · deltaMode = 1（行）/ 2（页）：一格 = 1 行
  // ⚠️ 必须**攒够一格**（|acc| ≥ 1）才动。踩过：写成"只要 acc 非零就走一格"，
  //    结果触控板那种"一次只发几像素、但一秒发几十次"的设备**每来一个事件就走一格**，
  //    一划就飞出去几十格（实测 5 次 ×5px 走了 5 格）。
  // ⚠️ 但**每个事件最多只走一格**：像素模式下 Windows 鼠标一格就是 100px
  //    （≈3.6 格），若老老实实按累加值走，就退回成「一滑跳好几格」了。
  //    走完把零头清零，下一个档位从干净的 0 开始累。
  let acc = 0;
  el.addEventListener('wheel', (e) => {
    if (wheelDisabled()) return;      // 灰掉时不抢滚轮，让页面能正常滚
    e.preventDefault();               // ⚠️ 必须：否则页面会跟着一起滚
    acc += e.deltaY / (e.deltaMode === 0 ? WHEEL_H : 1);
    if (Math.abs(acc) < 1) return;    // 小碎步先攒着（触控板）
    const dir = acc > 0 ? 1 : -1;
    acc = 0;
    setWheel(st, st.v + dir);
  }, { passive: false });
  // ⭐ 鼠标左键点某一格 → **直接精确选它**。
  // 比滚轮/拖动靠谱：想拨到 37 就点 37，不用先滚过去再担心吸歪半格。
  // ⚠️ 上下那两块"垫片"没有 data-v，不是数字，点了什么都不做。
  el.addEventListener('click', (e) => {
    if (wheelDisabled()) return;
    const tick = e.target && e.target.closest ? e.target.closest('.wi') : null;
    if (!tick || tick.dataset.v === undefined) return;
    setWheel(st, Number(tick.dataset.v));
  });
  // 三种输入方式（滚轮 / 方向键 / 点击）之后都要清掉累加器，
  // 否则上一次滚动的零头会跟下一次点击后的滚动叠在一起。
  el.addEventListener('keydown', () => { acc = 0; });
  WHEELS[id] = st;
}

/* 「立即开始」时拨轮是灰的（`.wheel.off`）—— 那时它不参与定时，
   滚轮/点击都不该被它吃掉，页面要能正常滚。 */
function wheelDisabled() {
  const box = $('wheel');
  return !!box && box.classList.contains('off');
}

function paintWheel(st) {
  for (const c of st.el.children) {
    if (c.dataset.v === undefined) continue;      // 跳过上下垫片
    c.classList.toggle('on', Number(c.dataset.v) === st.v);
  }
}

function setWheel(st, v) {
  if (!st) return;
  st.v = Math.max(0, Math.min(st.max, v));
  st.el.scrollTop = st.v * WHEEL_H;
  paintWheel(st);
  // ⚠️ 上面这行会异步再触发一次 scroll 事件，但那次的 v 与 st.v 相等，
  //    会在 `if (c === st.v) return` 处直接返回 —— 不会递归、不会打架。
  // 🔴 但**正因为它会提前返回，那次的 scroll 事件不会执行 onChange/syncStartAt** ——
  //    所以这里必须自己同步一次，否则「拨轮显示 15:30:07、实际按 14:30:07 开火」。
  //    踩过：滚轮/点击/方向键改走 setWheel 之后，提交串再也没被刷新过，
  //    而走原生滚动（= 上面那个 c !== st.v 的分支）的老路径反而是对的 ——
  //    这种"只有一半路径对"的错最难看出来，断言必须盖住每一条输入方式。
  if (st.onChange) st.onChange(); else syncStartAt();
}

/* 把「目标日 + 三个拨轮」拼成提交串，写进 `#startAt`。
 *
 * 格式与后端 `engine/plan.py::parse_when` 对齐：
 *   · 今天 / 明天 → **完整日期** `YYYY-MM-DD HH:MM:SS`
 *   · 立即开始   → 空串（后端据此判定不定时，见 ui/app.py 的 `if body.start_at.strip()`）
 *
 * ⚠️ 定时一律写完整日期，**不用** `"12:00:00"` 那种"今天"简写：
 *    简写只能表达今天，而"今天"一旦已经过去，后端会**直接报错拒绝**
 *    （`parse_when` 刻意不偷偷顺延到明天 —— 抢课时刻差一天是灾难）。
 *    既然界面上就有「明天」这个选项，就把日期算好一并送过去：
 *    语义唯一，也不会撞上那条拒绝路径。 */
function syncStartAt() {
  const on = schedDay !== 'now';
  const wheel = $('wheel');
  if (wheel) wheel.classList.toggle('off', !on);

  let v = '';
  if (on && WHEELS.wH) {
    const d = new Date();
    if (schedDay === '1') d.setDate(d.getDate() + 1);
    v = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
        `${pad2(WHEELS.wH.v)}:${pad2(WHEELS.wM.v)}:${pad2(WHEELS.wS.v)}`;
  }
  const box = $('startAt');
  if (box) box.value = v;
  renderStartEcho(v);
  // ⚠️ 必须顺手把「开始」按钮的状态也重算一遍。踩过：拨到一个过去的时刻 →
  //    按钮被禁用；此后把拨轮往未来拨 → 这一刻已经合法了，但按钮**还停在禁用上**，
  //    用户看到的是「明明拨对了却点不动」（要等 3 秒轮询才恢复，非常像卡死）。
  updateStartBtn();
}

// 上一次回显的内容指纹。`tickCountdown` 每 200ms 会来刷一次，
// 不加这个的话每秒要重写 5 次 innerHTML（还会打断用户选中文字）。
let _echoKey = null;
let _echoOk = false;

/* 拨轮的即时回显 + 「拨到过去」的告警。
   返回「这一刻是否可用」，好让「开始抢课」按钮跟着一起禁/启。

   ⚠️ 用 serverNow() 而不是 Date.now()：倒计时的基准必须和真正开火那一侧一致
   （本地钟有偏差时，按本地钟判"还没到"可能是假的）。 */
function renderStartEcho(v) {
  const el = $('startEcho');
  if (!el) return false;

  // 已经跑起来了就闭嘴：那时倒计时归 #schedHint 管，两个 note 都亮着
  // 会让人分不清哪个是"将要开抢"、哪个是"正在数秒"。
  if (!v || S.running) {
    if (_echoKey !== '') { el.style.display = 'none'; _echoKey = ''; }
    _echoOk = false;
    return false;
  }

  // 「拨到过去」的黄色告警，但**只在「确实没有正在跑的定时任务」时才亮**。
  // 定时任务到点后，后端清单仍带着旧的（已过去的）start_at，轮询把拨轮值也刷成
  // 过去时刻 —— 这时若照常亮「已经过去了」，会和 tickCountdown 的静默打架，
  // 每 200ms 一亮一灭地闪（2026-10-01 用户反馈）。定时已到点属于正常收尾，不是拨错。
  if (S.schedule && S.schedule.start_at && S.schedule.start_at <= serverNow()) {
    if (_echoKey !== 'fired') { el.style.display = 'none'; _echoKey = 'fired'; }
    _echoOk = false;
    return false;
  }

  // "YYYY-MM-DD HH:MM:SS" 直接喂给 new Date() 在部分浏览器会被当成 UTC，
  // 换成 `/` 分隔走的是"按本地时区解析"这条确定路径。
  const remain = (new Date(v.replace(/-/g, '/')).getTime() - serverNow() * 1000) / 1000;
  const ok = remain > 0;
  const key = v + '|' + Math.floor(remain);
  if (key === _echoKey) return ok;

  _echoKey = key;
  _echoOk = ok;
  el.style.display = '';
  if (!ok) {
    el.className = 'note warn';
    el.innerHTML = `⚠ 拨到的 <strong>${v}</strong> <strong>已经过去了</strong>` +
      `（现在 ${fmtTs(serverNow())}）。请往后拨，或把左边分段改成「明天」。`;
    return false;
  }
  el.className = 'note info';
  el.innerHTML = `将在 <strong>${v}</strong> 开抢　·　还有 <strong>${fmtRemain(remain)}</strong>` +
    `　·　到点前会先预热，把教学班全部解析好再等点`;
  return true;
}

/* 「开始抢课」按钮可用状态的**唯一**判据。
   ⚠️ 集中成一处：它原先散在 4 个地方各写一遍，很容易改了 3 处漏 1 处，
   漏掉那处就会留下一个「点得动、但后端一定拒」的按钮（显示值 ≠ 可执行值）。 */
function updateStartBtn() {
  const v = ($('startAt') || {}).value || '';
  const usable = renderStartEcho(v);          // 顺带把回显刷新一遍
  const btn = $('btnStart');
  if (btn) btn.disabled = S.running || S.plan.length === 0 || (!!v && !usable);
}

function initStartWheel() {
  if (!$('wheel')) return;
  buildWheelCol('wH', 23);
  buildWheelCol('wM', 59);
  buildWheelCol('wS', 59);

  // 初值 = 当前时刻（就近到秒）。用户多半是在「现在 + 几分钟」上微调，
  // 从当前时刻起步比从 00:00:00 起步少拨几十下。
  const d = new Date();
  setWheel(WHEELS.wH, d.getHours());
  setWheel(WHEELS.wM, d.getMinutes());
  setWheel(WHEELS.wS, d.getSeconds());

  const seg = $('segDay');
  for (const b of seg.querySelectorAll('button')) {
    b.classList.toggle('on', b.dataset.day === schedDay);
    b.onclick = () => {
      schedDay = b.dataset.day;
      for (const x of seg.querySelectorAll('button')) x.classList.toggle('on', x === b);
      syncStartAt();          // 它内部会重算「开始」按钮的可用状态
    };
  }
  syncStartAt();
}

// ---------- 学分条 ----------
//
// 数据分两半，来源不同，别混：
//   已选 / 上限 / 最低  ← 教务选课首页的「本学期选课要求」（爬来的，可能读不到）
//   待加选学分          ← 本地清单里所有课的学分之和（一定算得出来）
// 「照这份清单抢会超额」是教务驳回的头号原因，所以超额要显眼地红出来。

// 清单待加选学分 = S.plan 里所有课的学分之和。
// 学分是字符串（可能是 ""、"-"、"2.0"），非法值按 0 计 —— 宁可少算也别让整条显示崩掉。
function planCreditLocal() {
  let t = 0;
  for (const p of (S.plan || [])) {
    const x = Number(p.xf);
    if (Number.isFinite(x)) t += x;
  }
  return Math.round(t * 100) / 100;
}

/* 顶部固定栏的「学期信息」胶囊：学年 / 学期 / 轮次 + 选课时间（来自教务选课首页）。
   数据源是 S.credit（后端 credit_snapshot → CreditInfo.as_dict），字段 year/term/round/time_text。 */
function renderSemester(c) {
  const el = $('pillSemester');
  if (!el) return;
  const year = (c && c.year) || '';
  const term = (c && c.term) || '';
  const round = (c && c.round) || '';
  const time = (c && c.time_text) || '';
  // 学年/学期/轮次至少有一项才算「读到」，否则显示占位。
  if (!year && !term && !round) {
    el.textContent = '学期信息';
    el.classList.add('muted');
    el.title = '尚未从教务选课首页读到学期/轮次信息';
    return;
  }
  const seg = [];
  if (year) seg.push(`${year} 学年`);
  if (term) seg.push(`${term} 学期`);
  if (round) seg.push(round);
  const head = seg.join(' ');
  el.textContent = head + (time ? `（选课时间：${time}）` : '');
  el.classList.remove('muted');
  el.title = (time ? `选课时间：${time}` : '') + '　·　来自教务选课首页';
}

function renderCredit(c) {
  const box = $('creditBar');
  if (!box) return;
  if (!c) { box.hidden = true; box.innerHTML = ''; delete box.dataset.sig; return; }

  const num = (v) => (v === null || v === undefined || v === '') ? null : Number(v);
  const fx = (v) => { const x = num(v); return x === null ? '—' : x.toFixed(1); };

  const used = num(c.used), max = num(c.max), min = num(c.min);
  // 待加选学分**以本地清单现算**为准：用户点「加入清单」的那一刻就要看到数字变，
  // 等 3 秒轮询从后端取会明显迟滞。后端也会算一份（plan_credit），供 CLI 用，
  // 两边数据源本来就是同一份清单，不会打架。
  const want = planCreditLocal();
  const remain = num(c.remain);
  const over = remain !== null && want > remain + 1e-9;
  const hasPage = !!c.found && (used !== null || max !== null);

  const parts = [];

  // 总学分要求 —— 刻意跟教务页面上的说法保持一致（「总学分最低 X 最高 Y」），
  // 用户一眼能对上教务那边看到的字样。只写「上限」会让人不确定这是不是同一个数。
  if (min !== null || max !== null) {
    const seg = [];
    if (min !== null) seg.push(`最低 <b>${fx(min)}</b>`);
    if (max !== null) seg.push(`最高 <b>${fx(max)}</b>`);
    parts.push(`<span class="kv">总学分 ${seg.join(' / ')}</span>`);
    parts.push('<span class="sep">·</span>');
  }

  if (used !== null) {
    parts.push(`<span class="kv">已选 <b>${fx(used)}</b></span>`);
    // 进度条画的是「已选 / 最高」——超过最高才标红
    if (max !== null && max > 0) {
      const pct = Math.max(0, Math.min(100, (used / max) * 100));
      const cls = used > max ? 'over' : (used >= max ? 'full' : '');
      parts.push(`<span class="gauge ${cls}"><i style="width:${pct.toFixed(1)}%"></i></span>`);
    }
    parts.push('<span class="sep">·</span>');
  } else if (!hasPage) {
    parts.push(S.isOpen === false
      ? '<span class="unknown">未开放，无学分数据</span><span class="sep">·</span>'
      : '<span class="unknown">未读到教务学分</span><span class="sep">·</span>');
  }

  parts.push(`<span class="kv want">待加选 <b>${fx(want)}</b></span>`);

  if (remain !== null) {
    parts.push('<span class="sep">·</span>');
    if (over) {
      const overBy = Math.round((want - remain) * 100) / 100;
      parts.push(`<span class="kv over" title="清单学分超过剩余额度，教务会驳回">超出 <b>${fx(overBy)}</b></span>`);
    } else {
      parts.push(`<span class="kv">余 <b>${fx(remain)}</b></span>`);
    }
  } else if (!hasPage) {
    // 区分两种「读不到」：未开放期 vs 真没登录。
    // 关闭期明明已登录却写「未登录」，会把用户误导去重新登录（2026-09-29 实测发现）。
    parts.push(S.isOpen === false
      ? '<span class="unknown">未开放，无上限对照</span>'
      : '<span class="unknown">未登录，无上限对照</span>');
  }

  const tip = [];
  if (c.year && c.term) tip.push(`${c.year} 学年 第 ${c.term} 学期`);
  if (c.round) tip.push(c.round);
  if (min !== null) tip.push(`总学分最低 ${fx(min)}`);
  if (max !== null) tip.push(`最高 ${fx(max)}`);
  if (used !== null) tip.push(`本学期已选学分 ${fx(used)}`);
  tip.push(`清单待加选学分 ${fx(want)}`);
  if (remain !== null) tip.push(`剩余可选学分 ${fx(remain)}`);
  if (num(c.projected) !== null) tip.push(`这份清单全抢到后合计 ${fx(c.projected)}`);
  if (c.time_text) tip.push(`选课时间 ${c.time_text}`);
  if (!hasPage) tip.push('（未读到教务的学分要求，点 ↻ 重试）');

  parts.push(
    '<button class="refresh" id="btnCreditRefresh" ' +
    'title="重新向教务取一次学分要求（纯读取，零副作用）">↻</button>'
  );

  const html = parts.join('');
  // 3 秒轮询会反复进来：内容没变就别重设 innerHTML，
  // 否则按钮每 3 秒被重建一次，鼠标悬停/点击都会被打断。
  if (box.dataset.sig !== html) {
    box.dataset.sig = html;
    box.innerHTML = html;
  }
  box.hidden = false;
  box.title = tip.join('\n');
}

async function refreshCredit(btn) {
  if (btn) btn.disabled = true;
  try {
    const c = await api('GET', '/api/credit?refresh=1');
    S.credit = c;
    renderCredit(c);
    renderSemester(c);
  } catch (e) {
    logLine('取学分要求失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function fmtRemain(sec) {
  if (sec <= 0) return '已到点';
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const pad = (n) => String(n).padStart(2, '0');
  return (h > 0 ? h + ':' : '') + pad(m) + ':' + pad(s);
}

function tickCountdown() {
  const hint = $('schedHint');
  const sc = S.schedule;
  // 没有「仍在未来」的定时任务 → 让拨轮的预览接手这条提示（每 200ms 顺带刷新倒计时数字）。
  // 反之，真跑起来了就让位给 #schedHint —— 两个 note 同时亮着会分不清谁是谁。
  if (!sc || !sc.start_at || sc.start_at <= serverNow()) {
    hint.style.display = 'none';
    hint.innerHTML = '';
    // 没有定时、或定时时刻已到点：#schedHint 不再抢这条提示，交给拨轮预览。
    renderStartEcho(($('startAt') || {}).value || '');
    return;
  }
  renderStartEcho('');
  const remain = sc.start_at - serverNow();
  const lead = (S.clock && S.clock.lead_ms ? S.clock.lead_ms : 300) / 1000;
  const toFire = remain - lead;
  if (toFire <= 0) {
    // 已进入开火窗口：不再显示「已进入开火窗口（提前 …ms 开火，残余 …s）」这条提示
    // （用户 2026-10-01 要求去掉，免得一直挂在抢课清单上）。进入开火窗口后静默，
    // 真正的动作由「开始抢课」按钮 + 实时日志接手。
    hint.style.display = 'none';
    hint.innerHTML = '';
  } else {
    hint.style.display = '';
    hint.className = 'note info';
    hint.innerHTML = `目标开抢时刻 <strong>${fmtTs(sc.start_at)}</strong>　·　` +
      `距开火 <strong>${fmtRemain(toFire)}</strong>　·　` +
      `提前 ${Math.round(lead * 1000)}ms`;
  }
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

setInterval(tickCountdown, 200);

// ---------- 日志 ----------

const LOG_MAX = 400;
function logLine(msg, cls) {
  const box = $('log');
  const div = document.createElement('div');
  div.className = 'ln';
  div.innerHTML = `<span class="t">${now()}</span><span class="m ${cls || ''}">${esc(msg)}</span>`;
  box.appendChild(div);
  while (box.childElementCount > LOG_MAX) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

// ---------- 课程类别 Tab ----------

/* 每个类别（主修 / 板块课 / 公选 / 英语分项 / 特殊课程）都有独立的选课上下文
   （rwlx、bklx_id、加密串都不同），必须选对类别才能查到对的课。
   注意 kklxdm 会重复（两个板块课都是 06），所以下拉框的 value 用下标。*/

async function loadTabs() {
  const sel = $('tabSel');
  try {
    const r = await api('GET', '/api/tabs');
    S.tabs = r.items || [];
    S.tabsCached = !!r.cached;
    if (!S.tabs.length) {
      sel.innerHTML = '<option value="">— 无可用课程类别 —</option>';
      updateTabHint();
      return;
    }
    sel.innerHTML = S.tabs
      .map((t, i) => `<option value="${i}">${esc(t.name)}</option>`)
      .join('');
    sel.value = '0';
    if (S.tabsCached) {
      // 未开放期的 Index 页一个 Tab 都没有，这批是上一轮开放期抓到的缓存。
      // 列出来是为了"刷新一下类别就没了"不再发生，但必须说清它现在不能用来查课。
      logLine(`课程类别来自缓存（${S.tabs.length} 个）：${S.tabs.map((t) => t.name).join(' / ')}`
        + '；教务当前未到选课期，这批类别只作展示，恢复开放后会自动重新抓取', 'warn');
    } else {
      logLine(`读取到 ${S.tabs.length} 个课程类别：${S.tabs.map((t) => t.name).join(' / ')}`, 'info');
    }
  } catch (e) {
    sel.innerHTML = '<option value="">— 读取类别失败 —</option>';
    logLine('读取课程类别失败：' + e.message, 'err');
  }
  updateTabHint();
}

function currentTabIndex() {
  const i = parseInt($('tabSel').value, 10);
  return Number.isFinite(i) ? i : -1;
}

function currentTab() {
  const i = currentTabIndex();
  return i >= 0 ? S.tabs[i] : null;
}

function updateTabHint() {
  const t = currentTab();
  const el = $('tabHint');
  if (!t) {
    el.style.display = 'none';
    return;
  }
  el.style.display = '';
  el.innerHTML = `当前类别：<strong>${esc(t.name)}</strong>（kklxdm=${esc(t.kklxdm)}）
    &nbsp;·&nbsp;切换类别的选课上下文不同，切换后需重新搜索`;
}

/* 切换课程类别。抽成命名函数是因为「加载离线课程数据」也要走这条
   （它可能要把下拉切到有数据的那一类），走同一条路才不会两边行为不一致。 */
function onTabChange() {
  updateTabHint();
  S.courses = [];
  $('courseList').innerHTML = '<div class="empty">类别已切换，请重新搜索</div>';
  renderCourseHint('');
  // 按钮文案带的是「当前类别上次存了几条」→ 换类别就得跟着变
  renderCoursesSnapBtn();
}

$('tabSel').addEventListener('change', onTabChange);

// ---------- 会话 ----------

$('btnPassword').onclick = async () => {
  const u = ($('userInput').value || '').trim();
  const p = $('passInput').value || '';
  const btn = $('btnPassword');
  if (!u || !p) {
    $('sessionMsg').innerHTML = `<span class="pill warn"><span class="dot"></span>请先填学号和密码</span>`;
    return;
  }
  btn.disabled = true; btn.textContent = '登录中…';
  try {
    // ⭐ 方案 A（2026-10-02）：登录与 init 拆开，先见界面、后填数据。
    //   ① 只登录（with_init=false）—— 尽早拿到会话；
    //   ② 立刻刷新状态 → session_state 变 unverified → renderGate 切进主界面；
    //   ③ 再补 init + 各项数据（此时用户已看到主界面，不再盯着登录卡干等）。
    //   登录卡上的等待从「登录+init」≈2.5s 降到「仅登录」≈1.5s。
    await api('POST', '/api/session/password', { username: u, password: p, with_init: false });
    $('passInput').value = '';   // 立刻从界面清掉密码
    $('sessionMsg').innerHTML =
      `<span class="pill ok"><span class="dot"></span>登录成功，正在加载选课上下文…</span>`;
    logLine(`账号密码登录成功（学号 ${u}），正在加载选课上下文…`, 'ok');

    // ② 切界面（不等 init）
    await refreshState();

    // ③ 补 init 与各面板数据
    const ir = await api('POST', '/api/init');
    logLine(`选课上下文就绪：抽取 ${ir.field_count} 个字段、${ir.tabs} 个课程类别`, 'ok');
    if (!ir.is_open) logLine('提示：当前不在选课开放期，抢课需要等开放后再试', 'warn');
    await refreshState();
    await loadTabs();
    await loadSelected(true);
    await loadAcademic();
  } catch (e) {
    $('sessionMsg').innerHTML = `<span class="pill err"><span class="dot"></span>${esc(e.message)}</span>`;
    logLine('账号密码登录失败：' + e.message, 'err');
    // ⭐ 失败后把界面拉回与真实会话状态一致的位置：若 init 报「会话失效」，
    //   后端已 detach，这里 refreshState 会把界面切回登录卡（而不是停在空白主界面）。
    await refreshState().catch(() => {});
  } finally {
    btn.disabled = false; btn.textContent = '登 录';
  }
};

$('btnDrop').onclick = async () => {
  try {
    await api('DELETE', '/api/session');
    $('sessionMsg').innerHTML = '';
    S.tabs = []; S.courses = []; S.plan = [];
    S.selected = []; S.selectedLoaded = false;
    S.conflicts = {}; S.conflictsKnown = false; S.weekFilter = 0;
    S.expanded.clear();
    $('tabSel').innerHTML = '<option value="">— 请先建立会话 —</option>';
    updateTabHint();
    renderCourses([]);
    renderPlan();
    renderSelected();
    renderTimetable();
    logLine('会话已清除', 'info');
    await refreshState();
  } catch (e) { logLine('清除失败：' + e.message, 'err'); }
};

// ---------- 查课 ----------

/* 「📂 上次数据」按钮：把落盘的搜索结果（按课程类别分桶）拉出来看。
   为什么挂在「2 搜索课程」的标题栏右侧：它就是「搜索」的**离线替代品** ——
   未开放期 `query_courses` 会被本地闸门挡下（教务这时只会给框架页），
   而上次开放期搜到的课程数据还躺在磁盘上，正好拿出来回看。

   ⭐ 显示条件有**两条**（2026-09-30 用户要求）：
     ① 教务**未开放**（`S.isOpen === false`）—— 开放期「搜索」是实时的，
        把离线入口摆在那儿纯属碍眼；
     ② 磁盘上确实有桶（`S.coursesSnap.exists`）—— 没落过盘就别摆空按钮。
   ⚠️ `S.isOpen === null` 表示「状态未知」（还没建会话 / 会话已失效 / 没 init），
      **不显示** —— 用户要的是「只在确认未开放时显示」，未知不等于未开放。
      若哪天想在未知状态下也能离线看课程，把下面那行改成 `S.isOpen === true` 即可。

   文案带上「共 N 门」，悬停把每个类别的门数与保存时间列清楚 ——
   让用户在点之前就知道能拿到什么。 */
function renderCoursesSnapBtn() {
  const btn = $('btnCoursesSnap');
  if (!btn) return;
  // ① 只在未开放期露面（含「未知」在内的其它一切状态一律隐藏）
  if (S.isOpen !== false) {
    btn.style.display = 'none';
    return;
  }
  // ② 磁盘上没有落盘课程就没什么可看的
  const s = S.coursesSnap;
  if (!s || !s.exists || !(s.buckets || []).length) {
    btn.style.display = 'none';
    return;
  }
  const when = s.saved_at
    ? new Date(s.saved_at * 1000).toLocaleString('zh-CN', { hour12: false })
    : '时间未知';
  const lines = (s.buckets || []).map((b) => {
    const t = b.saved_at ? new Date(b.saved_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    return `· ${b.kklxmc || b.kklxdm}：${b.count} 个教学班${b.keyword ? `（关键词「${b.keyword}」）` : ''}${t ? '，' + t : ''}`;
  });
  // 当前类别有没有桶，直接写在文案里 —— 省得用户点了才发现不对。
  // ⚠️ 没有当前类别的桶时也要带个数字（总门数）：按钮上什么都不显示的话，
  // 用户根本不知道里面有没有东西、要不要点。
  const cur = pickCoursesBucket(s);
  btn.style.display = '';
  btn.textContent = cur ? `📂 上次数据（${cur.count}）` : `📂 上次数据（共 ${s.total || 0}）`;
  btn.title = `上次搜索保存的课程数据（最近的 ${when}）：\n${lines.join('\n')}`
    + (cur
      ? `\n\n点一下 → 加载「${cur.kklxmc || cur.kklxdm}」这一类别的课程列表`
      : '\n\n当前类别上次没保存过；点一下会自动切到有数据的类别');
  // 能走到这里就一定是未开放期了 —— 这会儿「搜索」根本用不了，
  // 它是这页唯一能看课的路子，所以**只要露面就是主操作色**，不再分三六九等。
  //（旧版按「当前类别有没有数据」决定高亮；那样会出现「未开放期 + 别的类别有数据」
  //  时按钮灰扑扑、容易被当成装饰的情况，而它恰恰是这时最该点的那个。）
  btn.classList.add('primary');
}

/* 在快照摘要里挑出「当前类别」那个桶。判据必须与后端
   `pick_courses_bucket` 一致（精确 key → 类别名 → kklxdm），否则会出现
   「按钮说没有、点下去却加载出来了」这种自相矛盾。 */
function pickCoursesBucket(snap) {
  const buckets = (snap && snap.buckets) || [];
  const t = currentTab();
  const kklxdm = (t && t.kklxdm) || $('tabSel').value || '';
  const kklxmc = (t && t.name) || '';
  const exact = `${kklxdm}|${kklxmc}`;
  return buckets.find((b) => b.key === exact)
    || (kklxmc ? buckets.find((b) => (b.kklxmc || '') === kklxmc) : null)
    || (kklxdm ? buckets.find((b) => (b.kklxdm || '') === kklxdm) : null)
    || null;
}

function renderCourseHint(text, kind) {
  const el = $('courseHint');
  if (!el) return;
  if (!text) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.className = 'note' + (kind ? ' ' + kind : ' info');
  el.style.display = '';
  el.innerHTML = text;
}

/* ====================================================================
   筛选器（复刻教务选课页「筛选」面板 + 我方新增「只看时间冲突」）

   ⭐ 这些条件**全部由教务服务端执行**（课程列表接口不返回上课时间等字段，
     客户端筛不了）—— 值拼成查询参数交给 `/api/courses`。
   ⚠️ 多选（星期/节次）用**重复参数**传：`sksj=1&sksj=3`，**不是逗号**。
   ==================================================================== */

// 星期（1-7）/ 节次（1-15）的 chip 标签。节次号我校是连续的 1-15（无缺号），
// 但**相邻编号之间时间并不均匀**（午休夹在 5 与 6 之间）—— 见 core/config.py::jie_time。
const WEEK_DAYS = ['一', '二', '三', '四', '五', '六', '日'];

function buildFilterChips() {
  const wrapSksj = $('fSksj');
  const wrapSkjc = $('fSkjc');
  if (!wrapSksj || !wrapSkjc) return;

  const mk = (wrap, values, label) => {
    wrap.innerHTML = '';
    for (const v of values) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip';
      b.dataset.v = String(v);
      b.textContent = label(v);
      b.title = label(v);
      b.onclick = () => { b.classList.toggle('on'); updateFilterHint(); };
      wrap.appendChild(b);
    }
  };
  mk(wrapSksj, [1, 2, 3, 4, 5, 6, 7], (i) => '周' + WEEK_DAYS[i - 1]);
  mk(wrapSkjc, Array.from({ length: 15 }, (_, i) => i + 1), (i) => String(i));
}

// 收集当前生效的筛选条件 → 拼成 `/api/courses` 的查询串。
// 返回 { query: string, summary: string[] }
function buildFilterQuery() {
  const multi = [];
  const single = [];
  const summary = [];

  for (const [key, label] of [['sksj', '星期'], ['skjc', '节次']]) {
    const wrap = document.getElementById('f' + key[0].toUpperCase() + key.slice(1));
    const picked = wrap ? [...wrap.querySelectorAll('.chip.on')].map((b) => b.dataset.v) : [];
    if (picked.length) {
      multi.push(...picked.map((v) => key + '=' + encodeURIComponent(v)));
      summary.push(label + ' ' + picked.map((v) => (key === 'sksj' ? '周' + WEEK_DAYS[+v - 1] : v)).join('、'));
    }
  }

  const singleDefs = [
    ['xf', '学分', (v) => v],
    ['cx', '是否重修', (v) => (v === '1' ? '重修' : '非重修')],
    ['yl', '有无余量', (v) => (v === '1' ? '有余量' : '无余量')],
    ['sksjct', '时间冲突', (v) => (v === '1' ? '只看冲突' : '只看不冲突')],
  ];
  for (const [key, label, fmt] of singleDefs) {
    const el = document.getElementById('f' + key[0].toUpperCase() + key.slice(1));
    const v = el ? el.value : '';
    if (v) {
      single.push(key + '=' + encodeURIComponent(v));
      summary.push(label + ' ' + fmt(v));
    }
  }

  const query = [...multi, ...single].join('&');
  return { query, summary };
}

function updateFilterHint() {
  const el = $('filterHint');
  if (!el) return;
  const { summary } = buildFilterQuery();
  if (summary.length) {
    el.className = 'note info';
    el.style.display = '';
    el.innerHTML = '已启用筛选：<b>' + summary.map(esc).join('；') + '</b>'
      + '（点击「搜索」生效）';
  } else {
    el.style.display = 'none';
    el.innerHTML = '';
  }
}

function resetFilters() {
  document.querySelectorAll('.chips .chip.on').forEach((b) => b.classList.remove('on'));
  for (const id of ['fXf', 'fCx', 'fYl', 'fSksjct']) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  updateFilterHint();
}

/* 加载离线课程数据。⚠️ 加载出来的**只能看、不能选班** ——
   `/api/classes` 在未开放期同样被闸门挡下（教学班数据没落盘）。
   这一点必须说清楚，否则用户点了「选班」以为坏了。 */
async function loadCoursesSnapshot() {
  const btn = $('btnCoursesSnap');
  if (btn) btn.disabled = true;
  try {
    const t = currentTab();
    const q = '?kklxdm=' + encodeURIComponent((t && t.kklxdm) || '')
      + '&kklxmc=' + encodeURIComponent((t && t.name) || '');
    let r = await api('GET', '/api/courses/snapshot/load' + q);
    let autoNote = '';
    if (!r.ok) {
      // ⚠️ 当前类别上次没存过。**必须自动换一个有数据的类别** ——
      // 按钮的悬停说明就是这么承诺的，而且「点了什么都不发生」是最差的体验
      //（用户只会以为坏了）。摘要已按保存时间倒序，所以 [0] 就是最近搜的那一类。
      const avail = r.available || [];
      if (!avail.length) {
        logLine(r.reason || '没有可用的课程数据', 'warn');
        return;
      }
      const pick = avail[0];
      // ⚠️ 别说「当前类别上次没保存过」——下拉空的时候**根本没有当前类别**
      //（未开放期建立会话拿不到 Tab），那是句假话。
      autoNote = currentTab()
        ? `（当前类别上次没保存过，已改用「${pick.kklxmc || pick.kklxdm}」）`
        : `（课程类别下拉是空的：未开放期建立会话拿不到 Tab；已改用「${pick.kklxmc || pick.kklxdm}」）`;
      if (btn) btn.textContent = '加载中…';
      r = await api('GET', '/api/courses/snapshot/load'
        + '?kklxdm=' + encodeURIComponent(pick.kklxdm || '')
        + '&kklxmc=' + encodeURIComponent(pick.kklxmc || ''));
      if (!r.ok) {
        logLine((r.reason || '加载课程数据失败') + autoNote, 'warn');
        return;
      }
    }
    const b = r.bucket || {};
    // ⚠️ 先切类别、再渲染：`onTabChange()` 会把课程列表清成「请重新搜索」，
    // 顺序反了就会出现「类别切过去了、列表却是空的」。
    // 之所以要切：否则会出现「下拉显示『主修课程』、列表里全是体育课」的错乱。
    //
    // ⚠️ 但**下拉里得有这个选项**才谈得上「切」。未开放期重新建立会话时，
    //    课程 Tab 一个都拿不到（Tab 只在开放期的 Index 页出现，见
    //    `core/client.py::_adopt_tabs`），下拉是空的 —— 这时 `value=` 赋值
    //    会被浏览器直接忽略（选项不存在），却还照样宣称「已自动切到 X」，
    //    就是**文案撒谎**（这个项目已经栽过一次，见「已恢复」那条）。
    //    所以改成：能切才切并报「已切」，不能切就如实说明。
    const want = String(b.tab_index ?? '');
    const canSwitch = typeof b.tab_index === 'number' && b.tab_index >= 0
      && Array.prototype.some.call($('tabSel').options, (o) => o.value === want);
    let switched = '';
    if (canSwitch && b.tab_index !== currentTabIndex()) {
      $('tabSel').value = want;
      onTabChange();
      switched = `（已自动切到「${b.kklxmc || b.kklxdm}」）`;
    } else if (!canSwitch) {
      switched = '（课程类别下拉是空的：未开放期建立会话拿不到 Tab，'
        + '所以只是把这份数据按保存时的类别列出来，没有切类别）';
    }
    S.courses = groupByCourse(r.rows || []);
    renderCourses(S.courses);
    const when = b.saved_at
      ? new Date(b.saved_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '时间未知';
    logLine(`已加载上次搜索的课程数据：${b.kklxmc || b.kklxdm} 共 ${r.count} 个教学班`
      + `${b.keyword ? `（搜索关键词「${b.keyword}」）` : ''}，保存于 ${when}`
      + switched + autoNote, autoNote ? 'warn' : 'ok');
    const why = (S.isOpen === false)
      ? '教务当前未开放，查不到课 —— 这份就是这时用来代替「搜索」的。'
      : '等教务未开放、搜不到课时，这个按钮就是「搜索」的替代品。';
    renderCourseHint(
      `📂 这是**上次搜索保存下来的**课程数据（类别「${esc(b.kklxmc || b.kklxdm)}」，保存在 ${esc(when)}）。`
      + (autoNote ? esc(autoNote) + '<br>' : '')
      + esc(why)
      + '<br>⚠️ 「选班」需要教务开放后才能用（教学班数据没有落盘）。'
      + (S.isOpen === true ? '<br>现在是开放期 —— 点「搜索」拿到的才是实时数据。' : ''),
      S.isOpen === false ? 'warn' : 'info');
  } catch (e) {
    logLine('加载课程数据失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
    renderCoursesSnapBtn();
  }
}

$('btnCoursesSnap').onclick = loadCoursesSnapshot;

$('btnSearch').onclick = async () => {
  const kw = $('kw').value.trim();
  const ti = currentTabIndex();
  if (ti < 0) { logLine('请先建立会话（以读取课程类别）', 'warn'); return; }
  const btn = $('btnSearch');
  btn.disabled = true; btn.textContent = '搜索中…';
  // ⭐ 带上筛选条件（若已启用）。
  const filter = buildFilterQuery();
  try {
    const r = await api('GET', '/api/courses?keyword=' + encodeURIComponent(kw)
                        + '&tab_index=' + ti + '&size=200'
                        + (filter.query ? '&' + filter.query : ''));
    S.courses = groupByCourse(r.rows || []);
    renderCourses(S.courses);
    renderCourseHint('');           // 实时数据 → 清掉「这是离线数据」的提示
    const t = currentTab();
    // 结果标题：有筛选时把条件带出来，让用户知道「现在的列表是被筛过的」。
    // ⚠️ 标题栏左边已经是「搜索课程」，这里只放筛选说明（不再重复「搜索结果」四个字）。
    const titleEl = $('courseResultTitle');
    if (titleEl) {
      titleEl.textContent = filter.summary.length ? filter.summary.join('；') : '';
    }
    const cntEl = $('courseCount');
    if (cntEl) cntEl.textContent = r.count + ' 个教学班 / ' + S.courses.length + ' 门课';
    logLine(`[${t ? t.name : ''}] 搜索「${kw || '全部'}」${filter.summary.length ? '（筛选：' + filter.summary.join('；') + '）' : ''}→ ${r.count} 个教学班 / ${S.courses.length} 门课`, 'info');
    if (!r.count) logLine('该类别下没有可选教学班（可能已选满或本轮未开放，或筛选条件过窄）', 'warn');
  } catch (e) {
    $('courseList').innerHTML = `<div class="empty">搜索失败：${esc(e.message)}</div>`;
    logLine('搜索失败：' + e.message, 'err');
    // 未开放期搜不了是**正常**的，不是故障 —— 顺手把出路指给用户
    if (e.kind === 'not_open') {
      const snap = S.coursesSnap;
      if (snap && snap.exists) {
        logLine('教务未开放，查不到课。点「搜索课程」标题栏右侧的「📂 上次数据」'
          + '可以看上次搜到的课程。', 'warn');
        renderCourseHint('教务未开放，查不到实时课程。'
          + '点上面「搜索课程」标题栏右侧的「📂 上次数据」，'
          + '可以查看上次搜索保存的课程列表。', 'warn');
      }
    }
  } finally {
    btn.disabled = false; btn.textContent = '搜索';
  }
};

/* 后端返回的是「教学班」级行，同一门课会有多行 → 按 kch_id 聚合成一门课，
   教学班数记在 jxb_count，容量等细节在展开时再查。*/
function groupByCourse(rows) {
  const map = new Map();
  for (const r of rows) {
    const kch = r.kch_id || r.kch || '';
    if (!kch) continue;
    if (!map.has(kch)) {
      map.set(kch, {
        kch_id: kch,
        kch: r.kch || '',
        kcmc: r.kcmc || '(未命名)',
        kklxdm: r.kklxdm || '',
        jxb_count: 0,
        jxbmc: r.jxbmc || '',
        yxzrs: r.yxzrs || '',
        cxbj: r.cxbj || '0',
        fxbj: r.fxbj || '0',
        xxkbj: r.xxkbj || '0',
        xf: r.xf || '',
        _kklxdmRow: r.kklxdm || '',
      });
    }
    map.get(kch).jxb_count += 1;
  }
  return Array.from(map.values());
}

$('kw').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btnSearch').click(); });

// ---------- 已选课程（懒加载：只在建会话 / 点刷新时拉） ----------

/* 为什么不做自动刷新：
   用户明确要求「不要影响抢课程序的速度」。已选列表走的是教务的实时查询接口，
   抢课期间每一毫秒带宽都金贵。所以只有两个触发点：
     1) 会话刚建立时（顺带把课表和冲突判定需要的时段一起拿到）
     2) 用户点「刷新」
   页面刷新后走 ?cached=1，只读本服务内存缓存，不再打教务。 */

let selLoading = false;

async function loadSelected(force, _retry) {
  if (selLoading) return null;
  selLoading = true;
  const btn = $('btnReloadSel');
  if (btn) { btn.disabled = true; btn.textContent = force ? '拉取中…' : '读取中…'; }
  try {
    const r = await api('GET', force ? '/api/selected' : '/api/selected?cached=1');
    if (r.stale) {
      // 服务重启过 / 还没拉过 → 才真去问一次教务
      selLoading = false;
      if (!_retry) return loadSelected(true, true);
      throw new Error('缓存不可用');
    }
    S.selected = r.courses || [];
    S.selectedLoaded = true;
    S.selectedAt = Date.now();
    renderSelected();
    renderTimetable();
    await refreshConflictsForExpanded();
    if (force) {
      const nT = S.selected.reduce((a, c) => a + (c.slots || []).length, 0);
      logLine(`已选课程 ${r.count} 门 / ${nT} 个时段已更新（含教师职称、时间、地点、自选否）`, 'ok');
    }
    return r;
  } catch (e) {
    logLine('读取已选课程失败：' + e.message, 'err');
    $('selHint').innerHTML = `<span style="color:var(--red)">读取失败：${esc(e.message)}</span>`;
    return null;
  } finally {
    selLoading = false;
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}

/* 学生学业情况统计：拉取 + 渲染成表格（放在课程表上方）。 */
let acadLoading = false;

async function loadAcademic() {
  if (acadLoading) return;
  acadLoading = true;
  const btn = $('btnAcademic');
  if (btn) { btn.disabled = true; btn.textContent = '拉取中…'; }
  try {
    const r = await api('GET', '/api/academic');
    renderAcademic(r);
    logLine(`学业情况统计已更新（课程性质 ${(r.kcxz || []).length} 类 / 学生 ${(r.students || []).length} 名）`, 'ok');
    return r;
  } catch (e) {
    logLine('读取学业情况失败：' + e.message, 'err');
    $('academic').innerHTML = `<div class="empty" style="color:var(--red)">读取失败：${esc(e.message)}</div>`;
    return null;
  } finally {
    acadLoading = false;
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}

function renderAcademic(r) {
  const card = $('academicCard');
  const box = $('academic');
  if (!box) return;
  const kcxz = (r && r.kcxz) || [];
  const students = (r && r.students) || [];
  if (!students.length) {
    if (card) card.style.display = 'none';
    box.innerHTML = '<div class="empty">暂无学业情况数据</div>';
    return;
  }
  if (card) card.style.display = '';
  // 取第一个学生（学生端就本人一条）
  const s = students[0];
  const base = [
    ['学号', s.XH], ['姓名', s.XM], ['性别', s.XBMC], ['学院', s.JGMC],
    ['专业', s.ZYMC], ['年级', s.NJMC], ['班级', s.BJMC], ['校区', s.XQMC],
  ].filter(([, v]) => v != null && v !== '');

  // 基础信息条
  const infoHtml = base.map(([k, v]) =>
    `<span class="kv"><span class="muted">${esc(k)}</span> <b>${esc(v)}</b></span>`
  ).join('<span class="sep">·</span>');

  // 汇总：毕业要求学分 / 获得总学分
  const yq = s.YQZDXF, hd = s.HDXF;
  const summaryHtml =
    `<span class="kv">毕业要求 <b>${esc(yq == null ? '—' : yq)}</b></span>` +
    `<span class="sep">·</span>` +
    `<span class="kv">已获总学分 <b>${esc(hd == null ? '—' : hd)}</b></span>`;

  // 表头：课程性质（要求学分）
  const headCells = kcxz.map(c =>
    `<th title="${esc(c.name)}要求学分 ${esc(c.require == null ? '' : c.require)}">${esc(c.name)}</th>`
  ).join('');

  // 三行：要求 / 获得 / 在修
  const rowOf = (prefix) => kcxz.map(c => {
    const v = s[prefix + c.rn];
    return `<td>${v == null ? '—' : esc(v)}</td>`;
  }).join('');

  box.innerHTML =
    `<div class="acad-info">${infoHtml}</div>` +
    `<div class="acad-summary">${summaryHtml}</div>` +
    `<div class="tt-scroll"><table class="acad-table"><thead><tr>` +
    `<th>学分</th>${headCells}</tr></thead><tbody>` +
    `<tr><td class="acad-label">要求</td>${rowOf('KCXZYQXF')}</tr>` +
    `<tr><td class="acad-label">获得</td>${rowOf('KCXZXF')}</tr>` +
    `<tr><td class="acad-label">在修</td>${rowOf('KCXZZXXF')}</tr>` +
    `</tbody></table></div>`;
}

function renderSelected() {
  const box = $('selList');
  if (!S.selectedLoaded) {
    $('selCount').textContent = '未加载';
    box.innerHTML = '<div class="empty">建立会话后会读取一次；之后只在你点「刷新」时才重新拉取</div>';
    return;
  }
  // ⭐ 按「能否退课」分两组（用户 2026-09-30 指定的「已抢」定义）：
  //    可退课 = 已抢到手；不可退 = 教务锁定的课（系统调整/不提供退课入口），不算抢来的。
  //    两组并存：「已抢」是新增的一类，不取代「已选」。
  const won = S.selected.filter((c) => c.can_drop);
  const other = S.selected.filter((c) => !c.can_drop);
  const stamp = new Date(S.selectedAt).toLocaleTimeString('zh-CN', { hour12: false });
  $('selCount').textContent = `共 ${S.selected.length} 门 · 已抢 ${won.length} · 已选 ${other.length} · ${stamp} 读`;

  if (!S.selected.length) {
    box.innerHTML = '<div class="empty">这学期还没有已选课程</div>';
    return;
  }
  const group = (title, tip, list) => {
    if (!list.length) return '';
    return `<div class="sel-group"><div class="sel-group-h" title="${esc(tip)}">${esc(title)}</div>` +
      list.map(selItemHtml).join('') + '</div>';
  };
  box.innerHTML =
    group(`已抢 ${won.length}`,
          '能退课 = 抢课抢到了（教务已选列表给了「退课」按钮）', won) +
    group(`已选 ${other.length}`,
          '教务未提供退课入口（系统调整/锁定），不算抢课所得', other);
}

/* 单条已选/已抢课程卡片。 */
function selItemHtml(c) {
  // 「自选否」：教务原文 1=自选上 / 0=系统调整
  const zixf = c.zixf_text
    ? `<span class="pill ${c.zixf === '1' ? 'info' : ''}"><span class="dot"></span>${esc(c.zixf_text)}</span>`
    : '';
  const jieCount = (c.slots || []).length;
  // 「已抢」标记：能退课才给，与课表格子的绿色一致
  const wonTag = c.can_drop
    ? '<span class="pill won"><span class="dot"></span>已抢</span>' : '';
  return `<div class="sel-item">
      <div class="nm">
        ${esc(c.kcmc || c.kch_id)}
        ${c.xf ? `<span class="xf-badge">${esc(c.xf)}<em>学分</em></span>` : ''}
        ${wonTag}
        ${zixf}
        ${c.kklxmc ? `<span class="pill"><span class="dot"></span>${esc(c.kklxmc)}</span>` : ''}
        ${jieCount ? `<span class="pill"><span class="dot"></span>${jieCount} 个时段</span>` : ''}
        <span class="dropzone" data-zone="${esc(c.kch_id)}">${dropEntryHtml(c)}</span>
      </div>
      <div class="grid">
        <div class="kv"><span class="k">教师</span><span class="v">${esc(c.jsxx || '—')}</span></div>
        <div class="kv"><span class="k">时间</span><span class="v">${esc(c.sksj || '—')}</span></div>
        <div class="kv"><span class="k">地点</span><span class="v">${esc(c.jxdd_text || '—')}</span></div>
        <div class="kv"><span class="k">教学班</span><span class="v">${esc(c.jxbmc || '—')}</span></div>
      </div>
    </div>`;
}

/* 退课入口 —— **完全按教务来**，我方不自作主张：
     教务已选列表给这一行渲染「退课」按钮 → 我们才给按钮；
     教务渲染蓝色的「已选」二字        → 我们只显示同样的蓝字，**绝不给退课入口**。
   判定逻辑见 core/drop.py（逐字复刻教务 zzxkYzbChoosedZy.js 的 isktk），
   蓝字颜色 #428bca 也是照抄教务（zzxkYzbChoosedZy.js:202 的 #428BCA）。 */
function dropEntryHtml(c) {
  if (c.can_drop) {
    return `<button class="tiny danger" data-dropok="${esc(c.kch_id)}"
                    data-jxbmc="${esc(c.jxbmc || '')}"
                    title="退课不可恢复，会再让你确认一次">退课</button>`;
  }
  const why = c.drop_block || '教务当前不提供该课的退课入口';
  return `<span class="yixuan" title="教务显示「已选」，不可退课：${esc(why)}">已选</span>`;
}

function dropZoneOf(kchId) {
  const zones = $('selList').querySelectorAll('.dropzone');
  for (const z of zones) if (z.dataset.zone === kchId) return z;
  return null;
}

// 第一步：把按钮换成「确认退课 / 取消」。退课不可逆，所以要拦一道。
function askDropConfirm(kchId, jxbmc) {
  const c = S.selected.find((x) => x.kch_id === kchId);
  const zone = dropZoneOf(kchId);
  if (!c || !zone) return;
  const det = [c.kcmc, c.jsxx, c.sksj].filter(Boolean).join(' · ');
  zone.innerHTML =
    `<span class="confirmq">退掉「${esc(c.kcmc || kchId)}」？` +
    `<span class="muted"> ${esc(det)}</span></span>` +
    `<button class="tiny danger" data-dropy="${esc(kchId)}" data-jxbmc="${esc(jxbmc || '')}">确认退课</button>` +
    `<button class="tiny" data-dropn="1">取消</button>`;
}

// 第二步：真退。后端会拿教务**刚给的**数据重算一次资格，不通过一个写请求都不发。
// 注意：不传 do_id —— 教务的教学班令牌是「每次查询重新下发的一次性加密串」，
// 从缓存带过去必然对不上。后端会在同一次查询里取新令牌再提交。
async function doDrop(kchId, jxbmc) {
  const zone = dropZoneOf(kchId);
  if (zone) zone.innerHTML = '<span class="confirmq">退课中…</span>';
  try {
    const r = await api('POST', '/api/drop', { kch_id: kchId, jxbmc: jxbmc || '' });
    if (r.ok) {
      logLine(`退课成功：${r.kcmc}（教务返回码 ${r.code}）`, 'ok');
      if (r.warning) logLine(r.warning, 'warn');
    } else {
      logLine(`退课失败：${r.kcmc} · ${r.msg}（教务返回码 ${r.code}）`, 'err');
    }
  } catch (e) {
    // 多为「教务当前不允许退」—— 后端已说明是哪一条不满足
    logLine('退课未执行：' + e.message, 'err');
  } finally {
    // 退没退成，都以教务为准重拉一遍：课表 / 冲突 / 按钮状态一起刷新
    await loadSelected(true);
    if (S.plan.length) await refreshPlanConflicts();
    else renderTimetable();
  }
}

$('selList').addEventListener('click', (e) => {
  const ok = e.target.closest('[data-dropok]');
  if (ok) { askDropConfirm(ok.dataset.dropok, ok.dataset.jxbmc || ''); return; }
  const yes = e.target.closest('[data-dropy]');
  if (yes) { doDrop(yes.dataset.dropy, yes.dataset.jxbmc || ''); return; }
  if (e.target.closest('[data-dropn]')) renderSelected();  // 取消 → 还原
});

$('btnReloadSel').onclick = () => loadSelected(true);
$('btnAcademic').onclick = () => loadAcademic();

// ---------- 课表 ----------

const WD_NAMES = { 1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六', 7: '周日' };
const JIE_FLOOR = 12;   // 节次行数下限：全按数据算的话只有 2 节课时会塌成一条

/* 节次 → 上下课时间（我校 2026-2027 作息，用户提供；6、7 节 2026-10-01 补齐）。
   编号是连续的 1-15，但**间隔不均匀**（午休夹在 5 和 6 之间），
   所以这是一张**查表**而不是按节次递增的公式 —— 必须查表，不能算。
   ⚠️ 历史上这里缺 6、7 节，被误当成"我校没有这两节课"，还配了"缺号行压成细线"的逻辑；
   结果教务数据里真的排了「星期一第6-9节」，那两节画不出来。
   **表里查不到只代表"不知道时间"，不代表学校没有这两节。**

   这里内置一份是为了首帧就能渲染（不等 /api/state 回来）；后端 /api/state 的
   `school.jie_time` 会覆盖它 —— 多学校复用时改后端配置即可，不用动前端。 */
let JIE_TIME = {
  1:  '08:00-08:40',
  2:  '08:50-09:30',
  3:  '09:45-10:25',
  4:  '10:35-11:15',
  5:  '11:20-12:00',
  6:  '12:50-13:30',
  7:  '13:40-14:20',
  8:  '14:30-15:10',
  9:  '15:15-15:55',
  10: '16:10-16:50',
  11: '16:55-17:35',
  12: '18:45-19:25',
  13: '19:30-20:10',
  14: '20:15-20:55',
  15: '21:05-21:45',
};

/** 后端下发的作息表覆盖内置表（值相同则不动，避免无谓重绘）。 */
function applyJieTime(map) {
  if (!map || typeof map !== 'object') return false;
  const keys = Object.keys(map);
  if (!keys.length) return false;
  const same = keys.length === Object.keys(JIE_TIME).length
    && keys.every((k) => JIE_TIME[k] === map[k]);
  if (same) return false;
  JIE_TIME = { ...map };
  return true;
}

/* 课表的两层：
     - 已选课程（来自 /api/selected，蓝色）
     - 待选课程（来自抢课清单，橙色）
   周次过滤：课都是按周上的，同一时间段可能被好几门课分周占用，不按周看会糊成一片。 */

/* 课表数据源。
 *
 * ⭐ 三类课程（用户 2026-09-30 指定）：
 *    won      —— 已抢：来自 /api/selected 且**能退课**（can_drop）。
 *                判据完全交给教务（core/drop.py 逐字复刻 isktk）——能退课就意味着
 *                是抢课抢到手的、教务还留着退课入口。
 *    selected —— 已选：来自 /api/selected 但**不能退课**（教务锁定/系统调整/未开放退课）。
 *    pending  —— 待选：来自抢课清单，还没抢到。
 *    三者并存，「已选」不会被「已抢」取代 —— 已抢是**新增**的一类。
 */
function timetableEntries() {
  const out = [];
  for (const c of S.selected) {
    out.push({
      kind: c.can_drop ? 'won' : 'selected',
      key: c.kch_id, kch_id: c.kch_id,
      kcmc: c.kcmc, meta: c.jsxx, addr: c.jxdd_text, slots: c.slots || [],
    });
  }
  for (const p of S.plan) {
    out.push({
      kind: 'pending', key: p.do_id || p.kch_id, kch_id: p.kch_id,
      kcmc: p.kcmc, meta: p.jsxx, addr: p.jxdd || '', slots: p.slots || [],
    });
  }
  return out;
}

function levelOf(key) {
  const c = S.conflicts[key];
  return c ? c.level : 'none';
}

/* 冲突标记的**四种**视觉状态：
     none   —— 没冲突。
     soft   —— 与**已选**（或清单内其他待选）节次占位重叠、但**周次错开** → 实际不撞，
               只做浅色提示（用户 2026-09-30 要求恢复此项图例）。
     mutex  —— 与「清单里的其他待选课」**真的**撞时间（星期+节次+周次都重叠）。
               这是用户自己押的注，属于正常操作，标记成互斥即可，不该看起来像报错。
     hard   —— 与**已选/已抢**星期/节次/周次全撞（基本抢不上）。

   判据是后端 /api/conflict 的 `vs`（只统计 hard 命中）与 `vs_soft`（只统计 soft 命中）。
   必须带来源：`hits` 被截断到 6 条不能自己数，而且「撞已选」与「撞清单内其他待选」
   的严重程度完全不同。`vs` 的判定与 `Plan.conflict_pairs`（执行期真正跳过的那些）一致。 */
function markOf(key) {
  const c = S.conflicts[key];
  if (!c || !c.hits || !c.hits.length) return 'none';
  const vs = c.vs || [];
  const soft = c.vs_soft || [];
  if (vs.includes('selected')) return 'hard';       // 与已选（含已抢）真撞
  if (vs.includes('pending')) return 'mutex';       // 与清单内其他待选真撞 → 互斥押注
  if (soft.length) return 'soft';                   // 仅占位重叠、周次错开 → 浅色提示
  return 'none';
}

/* 这门课的这个教学班是不是已经在抢课清单里了（决定按钮显示「+ 加入」还是「✓ 已加入」）。
   do_id 优先；没解析出 do_id 的只能退回按课程号比。 */
function isInPlan(jxb, kch) {
  return S.plan.some((p) => (jxb.do_id ? p.do_id === jxb.do_id : (!p.do_id && p.kch_id === kch)));
}

/* 这个教学班是不是已经在蹲课清单里了（决定「+ 蹲」还是「✓ 已蹲」）。 */
function isInWait(jxb, kch) {
  return S.wait.some((p) => (jxb.do_id ? p.do_id === jxb.do_id : (!p.do_id && p.kch_id === kch)));
}

/* 把同一天的时段整理成「课表格子」。
 *
 * ⭐ 2026-09-30 最终定稿（用户明确要求）：
 *    课表就是**普通矩形表格**，结构不动。只需让**每个色块填满它自己对应的节次高度**。
 *
 * 分块规则：**把节次区间「有重叠」的课合并成一个格子**（经典区间合并）。
 *   · 格子跨度 = 组内所有课的 min(start) ~ max(end)
 *   · 格子的 rowspan = 该跨度，于是色块高度 = 它自己那段节次
 *   · 一格内多门课 → **全部显示、纵向均分**（2026-10-01 口径；此前是「折叠成 +N」）
 *
 * ⚠️ 为什么必须合并「重叠」区间（两个真实坑，别再踩）：
 *   ① 课表靠 <td rowspan> 画格子。同一行同一天若出现**两个** <td>，
 *      浏览器会把第二个顺延到下一列 —— 实测「周二 3-5节」+「周二 4-5节」，
 *      篮球被画到了**周四**。
 *   ② 但合并也不能只看「区间完全相同」：3-5 与 4-5 区间不同、却**重叠**，
 *      若各自成块，就同时踩中坑 ①（篮球又跑到周四）。
 *      → 判据必须是**区间相交**，不是**区间相等**。
 *
 * ⚠️ 不相交的区间（如 3-5 与 8-10）**不合并**，各自一个格子 ——
 *   它们是不同时段，中间隔着别的行，本就该分开画。
 *   🔴 判据是**相交**，不是「起点 ≤ 上一段终点 + 1」（见下面 mergeDayBlocks 内的血泪注释）。
 *
 * 例：周二有 3-5(舞蹈,6-17周) / 3-5(周次错开,1-5周) / 4-5(篮球,6-17周)
 *     → 三者两两相交 → 合并成 **1 个 3-5 的格子**，rowspan=3；
 *       里面 3 门课纵向均分显示。这样同一行同一天只有一个 <td>，不会错位。
 *
 * ⚠️ 合并只是"为了不撞坑①"的排版手段，**不代表这些课冲突** ——
 *    周次错开的课合在一格、实际并不撞，属于正常情况。 */
function mergeDayBlocks(list) {
  if (!list.length) return [];

  // 按开始节次排序，然后线性合并**相交**的区间
  const sorted = [...list].sort((a, b) => a.s.start - b.s.start || a.s.end - b.s.end);
  const groups = [];
  let cur = null;
  for (const x of sorted) {
    // 🔴 判据必须是**区间相交**（`x.start <= cur.end`），
    //    **绝不能写成 `x.start <= cur.end + 1`**（"+1 = 允许相邻也并"）。
    //
    //    加 1 的后果（2026-10-01 用户报障实测）：
    //      周一 4-5(社会网络分析) + 6-9(大学英语) + 8-9(大学英语) + 10-11(创业基础)
    //      → 6 与 5+1 相等 → 并成 4-9；10 与 9+1 相等 → 再并成 **4-11 一个 8 行巨块**。
    //      于是 4-5 那门课被迫和 6-9、10-11 的课**共享同一格的高度**，
    //      而这几行又被别的天（周五 8-10 节挤了 5 门课）撑到 165/165/149/134px
    //      → 4-5 的色块被按比例拉到 210px，视觉上"从第 4 节一直延伸到 7、8 节"。
    //      用户原话：「周一 45 节的课被延伸到了 67 节，后面的课都被往后推迟了两个节次」。
    //
    //    ⚠️ 「相邻也并」当时是怕撞坑 ①（同一行同一天出现两个 <td>）。但那是**多余的**：
    //      坑 ① 只在两段**相交**时才发生（如 3-5 与 4-5，第 4 行同时被两段覆盖）。
    //      不相交的两段（4-5 与 8-9）在同一个 <tr> 里各发各的 <td>，
    //      一个在上一个在下，**永远不可能同一行出现两个**。
    //    ⚠️ 「节次号相邻」和「时间相邻」是两回事 —— 判据只能按区间相交，不能按号码连续性推。
    //      本校作息里 5 节 12:00 下课、6 节 12:50 才上（午休 50 分钟），
    //      7 节 14:20 下课、8 节 14:30 就上；**光看号码根本看不出这些间隔**。
    //      （2026-10-01 之前这张作息表还缺 6、7 节，当时"5 和 6 相邻"更是完全错的假设。）
    if (cur && x.s.start <= cur.end) {
      cur.items.push(x);
      cur.start = Math.min(cur.start, x.s.start);
      cur.end = Math.max(cur.end, x.s.end);
    } else {
      cur = { start: x.s.start, end: x.s.end, items: [x] };
      groups.push(cur);
    }
  }

  for (const b of groups) {
    // 组内排序：先按开始节次，再按类别权重（已抢 → 已选 → 待选）
    b.items.sort((p, q) =>
      p.s.start - q.s.start ||
      p.s.end - q.s.end ||
      KIND_RANK[p.e.kind] - KIND_RANK[q.e.kind]);
    b.key = b.start + '-' + b.end;
    // 有效节数（跨度内**有作息时间**的节数）—— 只作参考，不参与布局计算
    let hop = 0;
    for (let k = b.start; k <= b.end; k++) if (JIE_TIME[k]) hop++;
    b.hop = Math.max(hop, 1);
  }
  return groups;
}


/* 同一格内多门课时的排序权重：已抢 → 已选 → 待选。
   已抢排最前，因为那是「已经到手」的课，视觉上该压在上面。 */
const KIND_RANK = { won: 0, selected: 1, pending: 2 };

function renderTimetable() {
  const wrap = $('timetable');
  const all = timetableEntries();

  // 周次下拉的选项由数据决定（不写死 1-20 周这种）
  const weeks = new Set();
  for (const e of all) for (const s of e.slots) for (const w of s.weeks || []) weeks.add(w);
  refreshWeekOptions(weeks);

  const entries = all
    .map((e) => ({
      ...e,
      slots: S.weekFilter ? e.slots.filter((s) => !s.weeks || s.weeks.includes(S.weekFilter)) : e.slots,
    }))
    .filter((e) => e.slots.length);

  if (!entries.length) {
    wrap.innerHTML = `<div class="empty">${S.selectedLoaded
      ? (S.weekFilter ? `第 ${S.weekFilter} 周没有课` : '还没有任何课（已选 / 清单都为空）')
      : '建立会话后自动读取已选课程'}</div>`;
    $('ttHint').textContent = '';
    return;
  }

  let maxJie = 0;
  for (const e of entries) for (const s of e.slots) maxJie = Math.max(maxJie, s.end);
  maxJie = Math.max(maxJie, JIE_FLOOR);

  // 星期 → 合并后的块；再建「第几行开始」的索引，渲染时按行取
  const blocks = {};   // weekday -> [block]
  const startsAt = {}; // "weekday-row" -> block
  for (let w = 1; w <= 7; w++) {
    const here = [];
    for (const e of entries) for (const s of e.slots) if (s.weekday === w) here.push({ e, s });
    blocks[w] = mergeDayBlocks(here);
    for (const b of blocks[w]) startsAt[w + '-' + b.start] = b;
  }

  const html = ['<table class="tt"><thead><tr><th class="jie">节次</th>'];
  for (let w = 1; w <= 7; w++) html.push(`<th>${WD_NAMES[w]}</th>`);
  html.push('</tr></thead><tbody>');

  for (let j = 1; j <= maxJie; j++) {
    // 节次格里带上下课时间：左侧一栏同时承载「第几节」和「几点上」，
    // 省得用户为了排课再回去翻作息表。
    // 本校作息 1-15 连续、**没有缺号**（2026-10-01 补齐 6、7 节后），所以这里 isGap 恒为 false。
    // 这段"缺号行"逻辑**保留不删** —— 换学校 / 换学期时作息表仍可能缺号，
    // 那时若把缺号行按普通空行渲染，视觉上「第 5 节」下面紧接着「第 6 节」，
    // 会让人误以为两节相连（实际中间可能隔着很长的空档），所以缺号行由 CSS
    // 压成一道细窄的间隔线（见 style.css 的 .jie-gap）。
    //
    // ⚠️ 关键：缺号行**必须照常发满 8 个 <td>**，不能图省事用 colspan 合并。
    // 因为课程的 rowspan 是按「节次差」算的，缺号行也参与行号推进；
    // 一旦这里少发 <td>，上方跨行的课程块就会错位，整张表跟着歪。
    const t = JIE_TIME[j];
    const isGap = !t;
    const rowCls = isGap ? ' class="jie-gap"' : '';
    html.push(`<tr${rowCls}>`);
    html.push(
      `<td class="jie${isGap ? ' jie-gap-cell' : ''}">` +
      `<span class="jie-n">${j}</span>` +
      (isGap ? '' : `<span class="jie-t">${t}</span>`) +
      '</td>'
    );
    for (let w = 1; w <= 7; w++) {
      const blk = startsAt[w + '-' + j];
      if (blk) {
        // ⭐ 2026-10-01 第四版（用户要求）：「8-10 节的课不该排到 11 节的位置」+
        //    「装不下就把课表拉长」。于是格内不再做「N 等分」，而是**按节次分区**：
        //    同一个 (start,end) 的课 = 一个 `.zone`，每个区只占它自己那一段节次。
        //
        //    相邻两区的边界（关键规则，用户 2026-10-01 明确选定）：
        //      · 两组**真的共用某个节次**（如 8-10 与 10-11 共用第 10 节）
        //        → 边界落在那一节的**正中间**（两组平分第 10 节），并画一条**虚线**隔离
        //      · 只是首尾相接（前一组 end+1 == 后一组 start，如 10-11 与 12-13）
        //        → 边界就是行缝，**不画线**（用户：「只在真的共用了节次时加」）
        //
        //    节次坐标用「连续坐标」表示：第 j 节 = 区间 [j-1, j]。于是 8-10 = [7,10]、
        //    10-11 = [9,11]，重叠段 [9,10] 的中点 9.5 就是边界 —— 正好是第 10 节的正中。
        //    这个公式对「首尾相接」也成立：10-11=[9,11] 与 12-13=[11,13] → 中点 11 = 行缝。
        const span = blk.end - blk.start + 1;
        // ⭐ 「色块填满它对应的节次」——高度用**行数 × 行高**显式算出来。
        //
        // 为什么不用 CSS 的 height:100%：td 是 table-cell 时，其高度由「行高之和」
        // 决定，而 tbody 行并没有一个"确定高度"可供百分比解析，`height:100%`
        // 会退化成 height:auto（实测色块只有 64px，格子却有 116px，底下空一截）。
        // 显式算 px 最稳：span 行 × 单行高 58px − 上下边框/内边距。
        //
        // 58px 与 style.css 里 `.tt td.jie{height:58px}` 是同一个基准，
        // 两处必须保持一致（改一处就要改另一处）。
        //
        // ⭐ 这里算出来的只是**下限**。renderTimetable 末尾会按各区的内容需要
        //    重算 `--slot-h`（并把该跨的那几行撑高）—— 见那里的「按节次分区 + 拉长」。
        const ROW_H = 58, PAD_V = 4;      // PAD_V = td 的 padding-top+bottom
        const spanH = span * ROW_H - PAD_V * 2;

        // —— 按节次区间把格内的课分成「区」 ——
        //
        // 一个区 = 格内**同一片节次占位**里的所有课。
        //
        // ⚠️ 归组判据是**区间包含**（含相等），不是「起点终点完全相同」。
        //    同一门课经常有两条排课、且一条**完全包含**另一条，例如：
        //      大学英语(一)A班 → 「周一 6-9 节{7周}」 + 「周一 8-9 节{6-17周}」
        //    8-9 那条被 6-9 那条完全包含。只按 (start,end) 精确归组的话它们会变成两个区，
        //    而区的 [ua,ub) 是按「相邻两区取重叠段中点」算的（见下面）——
        //    **包含关系下这个中点会落到被包含区的起点之后**：
        //      z(6-9).ub = toU((8-1 + 9)/2) = toU(8) → 落在第 9 节的**开头**，
        //      于是 8-9 那门课被画到第 9 节的位置（用户说的"被往后推迟"）。
        //    并进同一个区就没这个问题 —— 它们的占位本就叠在同一片节次上，
        //    上下排列才是对的（周次过滤后通常只剩其中一条）。
        const zones = [];
        for (const it of blk.items) {
          const host = zones.find((z) => (it.s.start >= z.start && it.s.end <= z.end)
                                      || (z.start >= it.s.start && z.end <= it.s.end));
          if (host) {
            host.items.push(it);
            host.start = Math.min(host.start, it.s.start);
            host.end = Math.max(host.end, it.s.end);
          } else {
            zones.push({ start: it.s.start, end: it.s.end, items: [it] });
          }
        }
        zones.sort((a, b) => a.start - b.start);
        // u = 连续坐标下的位置（0 = 本格顶边）。第 j 节 = [j-1, j]，所以平移到本格起点。
        const toU = (abs) => abs - (blk.start - 1);
        zones.forEach((z, i) => {
          const next = zones[i + 1];
          z.ua = i === 0 ? 0 : zones[i - 1].ub;
          z.ub = next ? toU(((next.start - 1) + z.end) / 2) : span;
          // 只有「真的共用了节次」才画虚线：重叠段长度 > 0 ⇔ 本区起点 - 1 < 上一区终点
          // ⚠️ 挂给**下面**那一区（CSS 是 `.zone.sep::before` 的 border-top，画在区的顶边），
          //    它的顶边才是两组的分界线；挂给上一区会把虚线画到整格的顶上（踩过）。
          z.sep = i > 0 && (z.start - 1) < zones[i - 1].end;
        });

        html.push(
          `<td rowspan="${span}" class="tt-cell" data-j="${blk.start}" ` +
          `style="--slot-h:${spanH}px">` +
          '<div class="slot-stack">' +
          zones.map((z) =>
            `<div class="zone${z.sep ? ' sep' : ''}" data-ua="${z.ua}" data-ub="${z.ub}">` +
            z.items.map(chipHtml).join('') + '</div>').join('') +
          '</div></td>'
        );
      } else if (!(blocks[w] || []).some((b) => b.start < j && b.end >= j)) {
        // 没被上方的 rowspan 盖住 → 这一格是真的空
        html.push(`<td class="empty-cell${isGap ? ' jie-gap-bar' : ''}"></td>`);
      }
      // 否则：本格被上方的 rowspan 盖住，本行**不发** <td>，交给浏览器自动跳过
    }
    html.push('</tr>');
  }
  html.push('</tbody></table>');
  wrap.innerHTML = html.join('');

  // ⭐⭐ 2026-10-01 第四版定稿：「按节次分区 + 装不下就把课表拉长」。
  //
  // 要解决的问题（用户原话）：
  //   「8-10节有很多课，导致他都排到11节的位置了，…你可以单独对8-10节做好延伸」
  //   「课程底色色块做好完全填充对应的节次」
  // 根因：整格做 N 等分时，8-10 节的 5 门课被平摊到 6 行里，自然会「排到第 11 节的位置」。
  //
  // 做法：格内已按节次分成若干 `.zone`（见上面 renderTimetable 的 HTML 构建），这里
  //   ① 量每个区里每块「把课程名 + 副信息完整排下来」需要多高；
  //   ② 区高 = max(内容需要的高度, 这个区对应节次在原网格里占的高度)
  //      —— 装得下就**完全填充它对应的节次**；装不下就把这个区（以及它跨的那些行）撑高；
  //   ③ 行高：每个区换算成「每节次单位多少像素」的速率，按它覆盖的行做加权分摊，
  //      多格抢同一行取最大。这样「一个格子高 == 它跨的那些行高之和」恒成立，
  //      而且**延伸只发生在它自己那几行**（8-10 的课不会再把第 11 节撑走）。
  //   ④ 再按最终行高把每个区**撑满**（别的格子可能把这几行顶得更高），
  //      并给每块写 `min-height = 它自己需要的高度` → 谁都别被压扁。
  //
  // 🔴 历史（别退回去）：`.tight`（直接 display:none）→ `dense/micro/nano`（省略号压缩）
  //    → 现在这版（**一个字都不许丢，装不下就拉长**）。前两代都被用户否决。
  //
  // ⚠️ 判据是**实测内容高度**，不是「几门课」也不是固定阈值：按门数猜会误判；
  //    固定阈值（曾用 46px）在课程名折成两行时会判「放得下」，结果被裁断。
  // ⚠️ 量的是**子元素的真实高度**（`.nm` / `.meta`）：它们的高度由内容决定、
  //    不受父级 `overflow:hidden` 影响，所以首帧就能量准，不用先"清空高度再量"。
  const GAP = 2;          // .zone 的 gap，改这里要同步 style.css
  const ROW_H = 58;       // 与 CSS `.tt td.jie{height:58px}` 同一基准
  const trs = [...wrap.querySelectorAll('tbody > tr')];   // trs[节次-1] = 那一节的行
  const cellsInfo = [];

  // —— ① 量「每个区把内容完整排下来」需要多高 ——
  for (const td of wrap.querySelectorAll('td.tt-cell')) {
    const stack = td.querySelector(':scope > .slot-stack');
    if (!stack) continue;
    const zones = [...stack.children].filter((z) => z.classList.contains('zone'));
    if (!zones.length) continue;
    const span = Number(td.getAttribute('rowspan')) || 1;
    const j0 = Number(td.dataset.j) || 0;
    const baseH = parseFloat(td.style.getPropertyValue('--slot-h')) || 0;   // span×58−8
    // td 比 stack 多出来的那几像素（内边距 + 边框）。实测 ≈8px，行高要把它补回去，
    // 否则每一格都会莫名其妙地"长"8px、整张表被撑开。
    const slack = Math.max(0, td.getBoundingClientRect().height - baseH);

    const zs = zones.map((z) => {
      const slots = [...z.children];
      const need = slots.map((s) => {
        const cs = getComputedStyle(s);
        let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
              + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
        for (const c of s.children) h += c.getBoundingClientRect().height;
        return Math.ceil(h);
      });
      const ua = Number(z.dataset.ua) || 0, ub = Number(z.dataset.ub) || 0;
      const L = Math.max(ub - ua, 0.01);                       // 这个区占几个节次
      const content = need.reduce((a, b) => a + b, 0) + (slots.length - 1) * GAP;
      // 原网格里这一段节次有多高：整格 span×58−slack，按节次比例分给这个区
      const grid = (ROW_H - slack / span) * L;
      return { z, slots, need, ua, ub, L, h: Math.max(content, grid) };
    });
    const H = zs.reduce((a, x) => a + x.h, 0) || 1;
    cellsInfo.push({ td, stack, zs, span, j0, h: H, slack });
  }

  // —— ② 由「各区需要的高度」反推「行高诉求」 ——
  //
  //    ⚠️ 格的「行高诉求」必须把各区**累加**，不能取 max：
  //      区之间是连续的（前区终点 == 后区起点），只有累加才能保证
  //      「前区的像素高度 == 它覆盖的那些行高之和」—— 这正是虚线能落在节次正中的前提。
  //      （跨格才取 max：同一节次只有一行，要同时满足所有星期。）
  //
  //    🔴 别把这段写成「量行高 → 撑满区 → 再量行高……」的迭代：
  //      行高被邻居顶高后，本来不需要那么高的格子也会去**填满**它跨的整段行，
  //      一填满，它报给这些行的「诉求」就跟着涨 → 行更高 → 再填满 …… **正反馈发散**。
  //      实测 3 轮就把整格从 756px 顶到 1123px（1.49 倍）。
  //      单趟 + 下面 ③④ 各扫一次，语义确定、不会自激。
  const rowWant = new Array(trs.length).fill(0);
  for (const c of cellsInfo) {
    // 行高总量要比 stack 多出 slack（td 的边框 + 内边距），速率先放大这么多
    const sf = (c.h + c.slack) / c.h;
    for (let d = 0; d < c.span; d++) {
      let v = 0;
      for (const x of c.zs) {
        const ov = Math.max(0, Math.min(x.ub, d + 1) - Math.max(x.ua, d));
        if (ov > 0) v += (x.h / x.L) * sf * ov;   // 每节次几个像素 × 本行占了几个节次
      }
      const idx = c.j0 + d - 1;
      if (idx >= 0 && idx < rowWant.length && v > rowWant[idx]) rowWant[idx] = v;
    }
  }

  // —— ③ 定行高：只给**真的需要更高**的行写行内高度，其余交还 CSS ——
  //
  //    · 普通行（rowWant ≤ 58）→ 不写，交给 `.tt td.jie{height:58px}` 兜底
  //      （节次列要放下「节号 + 上下课时间」两行 ≈ 26px，58px 是它的下限）
  //    · 缺号行（我校无 6、7 节）→ 不写，交给 `.tt tr.jie-gap td{height:16px}` 压成细线
  //    · 有课的行（rowWant > 0）→ 按内容需要撑高
  //
  // ⚠️ 原来这里是**无条件**写 `max(58, rowWant)`。那会用**行内样式压过 CSS**：
  //    缺号行本该是 16px 的一道细线（提示"这里隔着午休、没有这两节"），
  //    却被撑成 58px 的正常空行 —— 整表凭空高出 2 行，节号 5 → 6 → 7 → 8
  //    看起来也像"连着上的四节"。
  // ⚠️ 但缺号行**也可能真的有课**：教务数据里就有「星期一第6-9节{7周}」这种
  //    跨过 6、7 节的排课。此时 rowWant > 0，照常撑高 —— 有课就得看得见，
  //    不能为了"这是缺号行"就把课压没。
  for (let i = 0; i < trs.length; i++) {
    trs[i].style.height = rowWant[i] > 0 ? rowWant[i].toFixed(2) + 'px' : '';
  }
  void wrap.offsetHeight;   // 强制重排，下面要读回真实行高

  // —— ④ 按最终行高把每一格的区撑满（别的格子可能把这几行顶得更高） ——
  for (const c of cellsInfo) {
    let rowsPx = 0;
    for (let d = 0; d < c.span; d++) {
      const tr = trs[c.j0 + d - 1];
      rowsPx += tr ? tr.getBoundingClientRect().height : ROW_H;
    }
    const target = Math.max(c.h, rowsPx - c.slack);   // stack 的目标高度
    const k = target / c.h;
    c.td.style.setProperty('--slot-h', target.toFixed(2) + 'px');
    let used = 0;
    c.zs.forEach((x, i) => {
      // 最后一个区用「目标 − 已用」兜底，保证 Σ区高 == 目标高，不留缝也不溢出
      const px = i === c.zs.length - 1 ? target - used : x.h * k;
      used += px;
      x.z.style.height = px.toFixed(2) + 'px';
      // 每块至少留够自己内容需要的高度（拉长只加不减，谁都别被压扁）
      x.slots.forEach((s, n) => { s.style.minHeight = x.need[n] + 'px'; });
    });
  }
  void wrap.offsetHeight;


  const nWon = entries.filter((e) => e.kind === 'won').length;
  const nSel = entries.filter((e) => e.kind === 'selected').length;
  const nPend = entries.filter((e) => e.kind === 'pending').length;
  $('ttHint').textContent =
    `已抢 ${nWon} · 已选 ${nSel} · 待选 ${nPend} · 共 ${maxJie} 节` +
    (S.mutex.length ? ` · ${S.mutex.length} 对互斥` : '');
}

/* 课表格子。已抢绿、已选蓝、待选橙；待选再叠一层冲突标记（见 markOf）。 */
function chipHtml({ e, s }) {
  const mark = e.kind === 'pending' ? markOf(e.key) : 'none';
  const cls = e.kind === 'pending' ? (mark === 'none' ? 'pending' : mark) : e.kind;
  const head = [e.addr, e.meta].filter(Boolean).join(' · ');
  const tag = mark === 'hard' ? '⚠ 与已选时间冲突'
            : mark === 'mutex' ? '⇄ 与清单内其他待选真的撞了'
            : '';
  // 不标色但值得一提：只与清单内其他待选「占位重叠、周次错开」——不算冲突、不参与互斥
  const quiet = (e.kind === 'pending' && mark === 'none'
                 && (((S.conflicts[e.key] || {}).vs_soft) || []).length)
    ? '（与清单内其他课节次占位重叠、周次错开，实际不撞，不参与互斥）' : '';
  const kindCn = e.kind === 'pending' ? '待选（抢课清单）'
               : e.kind === 'won' ? '已抢（能退课 = 抢到手，教务仍留有退课入口）'
               : '已选（教务未提供退课入口，非抢课所得）';
  const title = [
    e.kcmc,
    head,
    s.jie_text,
    s.weeks_text || '周次未标注',
    kindCn,
    tag,
    quiet,
    mark === 'mutex' ? '星期+节次+周次都重叠，只能上成其中一门：谁先抢到，另一门自动不再提交' : '',
  ].filter(Boolean).join('\n');
  // ⚠️ 两条副信息必须包在**同一个 `.meta`** 里 —— 格子里它们要能**自然回流**：
  //    内容短就并成一行（`节次·周次 · 地点`），长就自己折行，一个字符都不省略。
  //    拆成两个平级块的话，CSS 没法只让「它们俩」并排。
  return `<span class="slot ${cls}" title="${esc(title)}">` +
    `<span class="nm">${tag ? '⚠ ' : ''}${esc(e.kcmc || '')}</span>` +
    `<span class="meta">` +
      `<span class="mt">${esc(s.jie_text)}${s.weeks_text ? ' · ' + esc(s.weeks_text) : ''}</span>` +
      (head ? `<span class="mt mt-ad">${esc(head)}</span>` : '') +
    `</span>` +
    '</span>';
}

function refreshWeekOptions(weeks) {
  const sel = $('weekFilter');
  const maxW = weeks.size ? Math.max(...weeks) : 0;
  if (S.weekFilter > maxW) S.weekFilter = 0;
  if (sel.dataset.max === String(maxW)) return;   // 选项没变就别重建（会打断用户选择）
  sel.dataset.max = String(maxW);
  const opts = ['<option value="0">全部</option>'];
  for (let w = 1; w <= maxW; w++) opts.push(`<option value="${w}">第 ${w} 周</option>`);
  sel.innerHTML = opts.join('');
  sel.value = String(S.weekFilter);
}

$('weekFilter').onchange = () => {
  S.weekFilter = parseInt($('weekFilter').value, 10) || 0;
  renderTimetable();
};

// ---------- 冲突判定 ----------

/* 对手方 = 已选课程 + 抢课清单里的其他课（都在后端内存里，不发外部请求）。
   硬冲突（星期 + 节次 + 周次都重叠）→ 拦下，不给加。
   软冲突（节次重叠但周次错开）→ 放行，但提示一句：
   我校教务在提交时自己也查冲突，判定口径可能只看节次，所以不能保证一定过。 */

async function checkConflicts(candidates) {
  if (!candidates.length) return { known: S.conflictsKnown, results: {} };
  const r = await api('POST', '/api/conflict', { items: candidates });
  S.conflictsKnown = !!r.known;
  Object.assign(S.conflicts, r.results || {});
  return r;
}

async function refreshConflictsForExpanded() {
  const boxes = Array.from(S.expanded.values());
  if (!boxes.length) return;
  for (const ex of boxes) {
    try {
      await checkConflicts(candidatesOf(ex.items, ex.i, ex.kch));
      renderClassRows(ex);
    } catch (_) { /* 判定失败不影响主流程，只是没有提示 */ }
  }
}

/* 立即重画所有展开的教学班列表（**不重新打网络**）。
   用途：addToPlan / addToWait / removeFromPlan / removeFromWait 改完本地清单后，
   教学班行上的「✓ 已加入」/「✓ 已蹲」按钮要**立刻**翻转，不能等网络回来或 3 秒轮询。
   ⚠️ 与 refreshConflictsForExpanded 的分工：它只重画按钮态（isInPlan/isInWait 读 S.plan/S.wait，
   都是本地同步值），冲突标注仍由 refreshConflictsForExpanded 那趟网络重判 ——
   两者可以并行，一个管「秒显」、一个管「精确」。 */
function rerenderExpanded() {
  for (const ex of S.expanded.values()) {
    try { renderClassRows(ex); } catch (_) { /* 单个列表重画失败不影响其它 */ }
  }
}

function candidatesOf(items, i, kch) {
  return items.map((j, k) => ({
    key: j.do_id || `${kch}-${k}`,
    kch_id: kch,
    kcmc: j.kcmc || '',
    slots: j.slots || [],
  }));
}

// ---------- 查课结果 ----------

function renderCourses(rows) {
  const box = $('courseList');
  if (!rows.length) {
    box.innerHTML = '<div class="empty">没有结果</div>';
    return;
  }
  box.innerHTML = rows.map((r, i) => {
    const kch = r.kch_id || r.kch || '';
    const name = r.kcmc || '(未命名)';
    const extra = ` · ${r.jxb_count || 0} 个教学班${r.kch ? ' · ' + esc(r.kch) : ''}`;
    const warn = (r.cxbj === '1') ? ' <span class="pill warn"><span class="dot"></span>重修</span>' : '';
    // 学分徽章：与已选列表同一套样式，扫一眼就知道这门课占多少学分
    const xfv = (r.xf === undefined || r.xf === null) ? '' : String(r.xf).trim();
    const xf = xfv ? `<span class="xf-badge">${esc(xfv)}<em>学分</em></span>` : '';
    return `<div class="item clickable" data-course="${i}"
                 title="点这一格任意处都能展开选班（等价于右侧「选班」按钮）">
      <div class="main">
        <div class="title">${esc(name)}${xf}${warn}</div>
        <div class="meta">${esc(kch)}${extra}</div>
        <div class="classes" id="cls-${i}"></div>
      </div>
      <div class="actions"><button class="tiny" data-i="${i}" data-act="expand">选班</button></div>
    </div>`;
  }).join('');

  // 整格可点：点条格任意处都能展开/收起选班
  box.querySelectorAll('.item[data-course]').forEach((el) => {
    const i = parseInt(el.dataset.course, 10);
    el.onclick = (e) => {
      // 「选班」按钮自己绑了 handler，让它处理，别重复触发（否则展开又被收起）
      if (e.target.closest('button')) return;
      // 展开出来的教学班列表就在这一格里面 —— 点它绝不能被当成「收起这一格」，
      // 否则用户想点某个班，结果整个列表被收掉了
      if (e.target.closest('.classes')) return;
      const btn = el.querySelector('button[data-act="expand"]');
      if (btn && !btn.disabled) expandClasses(i, btn);
    };
  });

  box.querySelectorAll('button[data-act="expand"]').forEach((b) => {
    b.onclick = () => expandClasses(parseInt(b.dataset.i, 10), b);
  });
}

async function expandClasses(i, btn) {
  const r = S.courses[i];
  if (!r) return;
  const kch = r.kch_id || r.kch || '';
  const box = $('cls-' + i);
  if (box.dataset.loaded === '1') {
    box.innerHTML = '';
    box.dataset.loaded = '0';
    S.expanded.delete(box.id);
    btn.textContent = '选班';
    return;
  }
  btn.disabled = true; btn.textContent = '加载中…';
  try {
    // 必须带上 tab_index —— 教学班的可查性依赖对应类别的上下文
    const res = await api('GET', '/api/classes?kch_id=' + encodeURIComponent(kch)
                          + '&tab_index=' + currentTabIndex());
    const items = res.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="muted" style="margin-top:6px">无可用教学班（可能未开放）</div>';
    } else {
      const ex = { i, kch, items, box };
      S.expanded.set(box.id, ex);
      if (!S.selectedLoaded) {
        // 没已选数据就判不了冲突 —— 顺手补一次（这是用户主动展开，不是轮询）
        box.innerHTML = '<div class="muted" style="margin-top:6px">正在读取已选课程以判定时间冲突…</div>';
        await loadSelected(false);
      }
      try {
        await checkConflicts(candidatesOf(items, i, kch));
      } catch (e) {
        logLine('冲突判定失败（不影响选班）：' + e.message, 'warn');
      }
      renderClassRows(ex);
    }
    box.dataset.loaded = '1';
    btn.textContent = '收起';
  } catch (e) {
    box.innerHTML = `<div class="muted" style="margin-top:6px">加载失败：${esc(e.message)}</div>`;
    logLine('查教学班失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function renderClassRows(ex) {
  const { i, kch, items, box } = ex;
  const free = items.filter((j) => !j.is_full).length;
  const added = items.filter((j) => isInPlan(j, kch)).length;
  const head = `共 ${items.length} 个教学班，${free} 个未满`
    + (added ? ` · <b>${added} 个已加入清单</b>` : '')
    + (S.conflictsKnown
        ? ' · 冲突不拦，仅提示'
        : ' · <span style="color:var(--amber)">已选课表未加载，冲突无法判定</span>');

  box.innerHTML = `<div class="muted" style="margin-top:6px">${head}</div>` +
    items.map((j, k) => {
      const key = j.do_id || `${kch}-${k}`;
      const mark = markOf(key);
      const info = S.conflicts[key];
      const inPlan = isInPlan(j, kch);

      // 待选课**允许**时间冲突：按钮一律可用，只把风险写清楚。
      // 真正的取舍交给清单顺序 + 执行期的互斥跳过（谁先抢到，真撞的那些就不发了）。
      // 已经在清单里的班 → 按钮变成「✓ 已加入」，**再点一次就是移除**
      // （跟加进来是同一个按钮，不用跑去清单那边找「删」）。
      let cls = 'primary';
      let label = '+ 加入';
      let tip = '';
      if (inPlan) {
        cls = 'added';
        label = '✓ 已加入';
        tip = '这个教学班已在抢课清单里 —— 再点一次即可从清单移除。'
            + '想换班直接点同一门课的其他教学班（会自动替换，同一门课只押一个班）';
      } else if (mark === 'hard') {
        cls = 'conflict';
        label = '+ 加入（与已选撞）';
        tip = '与已选/已抢课程星期/节次/周次全撞，加进去基本抢不到；'
            + '但抢课期间已抢/已选会变，所以仍允许加，只做提示';
      } else if (mark === 'mutex') {
        cls = 'mutexwarn';
        label = '+ 加入（清单内互斥）';
        tip = '与清单里已有的待选课**真的**撞时间（星期+节次+周次都重叠）：'
            + '只能上成其中一门。按清单顺序抢，谁先抢到，另一门自动不再提交';
      } else if (j.is_full) {
        // ⚠️ 2026-10-01：满员已改成**终态失败、不重试** —— 不能再写「仍可蹲守」
        // （抢课循环不再等人退课，那句话会让人以为加进去迟早能抢到）。
        // 想等退课名额，走「+ 蹲」按钮（加入独立的蹲课清单，按时间轮流发送）。
        tip = '已满：加进去也只打一发，满员即判失败（不会自动等退课名额）。'
            + '想蹲退课名额，点「+ 蹲」加入蹲课清单。';
      }

      const warnHtml = mark !== 'none' && info && info.hits.length
        ? `<div class="cls-warn${mark === 'hard' ? '' : ' soft'}">` +
          (inPlan ? '<b>已加入清单 · </b>' : '') +
          info.hits.slice(0, 3).map((h) => esc(h.detail)).join('<br>') +
          (info.count > 3 ? `<br>…另有 ${info.count - 3} 处` : '') +
          '</div>'
        : '';

      // 学分只在**课程**那一层显示（课程名旁边的徽章），教学班这行不再重复 ——
      // 同一门课各班学分一般相同，重复显示只是占地方、把时间/教师挤窄。
      return `<div class="cls-row">
        <div class="row" style="align-items:center">
          <span class="pill ${j.is_full ? 'err' : 'ok'}"><span class="dot"></span>${j.is_full ? '满' : '可'}</span>
          <span style="flex:1;min-width:0;font-size:12px">
            <b>${esc(j.sksj_text || j.sksj || '时间待定')}</b>
            · ${esc(j.teacher_text || j.jsxx || '教师待定')}
            · ${esc(j.yxzrs)}/${esc(j.jxbrl)}
            ${j.jxdd_text || j.jxdd ? ' · ' + esc(j.jxdd_text || j.jxdd) : ''}
          </span>
          <button class="tiny ${cls}" data-k="${k}"
                  title="${esc(tip)}">${esc(label)}</button>
          ${isInWait(j, kch)
            ? '<button class="tiny added" data-wk="' + k + '" title="已在蹲课清单里，再点移除">✓ 已蹲</button>'
            : '<button class="tiny" data-wk="' + k + '" title="蹲这个班的退课名额（按时间轮流发送，不按次数）">+ 蹲</button>'}
        </div>
        ${warnHtml}
      </div>`;
    }).join('');

  box.querySelectorAll('button[data-k]').forEach((b2) => {
    b2.onclick = () => {
      const j = items[parseInt(b2.dataset.k, 10)];
      if (!j) return;
      // 同一个按钮两种语义：已在清单里 → 再点就是**移除**；否则加入。
      // 判据用 isInPlan（跟按钮文案同源），保证「看到什么就点什么」不会错位。
      if (isInPlan(j, kch)) {
        removeFromPlan(kch, (S.courses[i] || {}).kcmc || kch);
      } else {
        addToPlan(S.courses[i], j);
      }
    };
  });

  box.querySelectorAll('button[data-wk]').forEach((b2) => {
    b2.onclick = () => {
      const j = items[parseInt(b2.dataset.wk, 10)];
      if (!j) return;
      if (isInWait(j, kch)) {
        // 蹲课允许同课不同班 → 用 do_id 精确删这一个班，别用 kch_id（会误删同课其他班）。
        removeFromWait(j.do_id || kch, (S.courses[i] || {}).kcmc || kch);
      } else {
        addToWait(S.courses[i], j);
      }
    };
  });
}


// ---------- 清单 ----------

/* 清单变了要干四件事，顺序不能反：
   1) 推给后端（POST /api/plan）—— 后端据此算出「互斥项对」，也让 /api/conflict
      能看见清单里的其他项。界面标注与执行期跳过共用同一个 Plan，口径必然一致。
   2) 让后端重新判一遍每一项的冲突等级（对手方 = 已选 + 清单其他项）。
   3) 重画课表与清单。
   4) 重画展开着的教学班列表（按钮要从「+ 加入」变成「✓ 已加入」，冲突提示也要刷新）。
   抢课运行中绝不动后端 plan（会把正在跑的计划替换掉）。 */
async function commitPlan() {
  renderPlan();
  renderTimetable();
  await syncPlan();
  await refreshPlanConflicts();
  await refreshConflictsForExpanded();
}

/* 组装 `POST /api/plan` 的请求体。
   ⚠️ 必须带上 `base_version`（乐观锁）—— S.plan 只是本地副本，
   若它已经是陈旧的，这一次提交就会把服务端那份**整个盖掉**。
   只在极早期还没拿到版本号时（S.planRev === null）才不带。 */
function planBody(startAt) {
  const b = {
    items: S.plan,
    stop_on_first_win: $('optStopFirst').checked,
    // 派发方式（serial / round_robin）。⚠️ 它必须在**启动那一刻**跟着 plan 一起提交：
    // 后端是拿这个 Plan 去跑的，界面上改了但没提交等于没改（会和界面显示不一致）。
    retry_mode: ($('optRetryMode') || {}).value || 'round_robin',
    start_at: startAt || '',
  };
  if (S.planRev !== null) b.base_version = S.planRev;
  return b;
}

/* 派发方式的说明文字。刻意写成「什么时候该选哪个」而不是术语解释 ——
   用户是在抢课现场做决定，需要的是判断依据，不是名词定义。

   ⚠️ 后端不认识这个开关时（页面比后端新：静态文件按需读盘会立刻生效，
   而 Python 那侧要重启 serve.py），**不能装作能用** —— 用户选了「轮流发送」
   却照样在串行跑，就是「显示值 ≠ 执行值」。所以直接说明并禁用（见 applyState）。 */
function renderModeHint() {
  const el = $('modeHint');
  if (!el) return;
  if (S.modeSupported === false) {
    el.innerHTML = '<strong>当前后端版本较旧，还不支持切换派发方式</strong>（它连'
      + '「派发方式」这个字段都不会读）。重启一次 <code>serve.py</code> 之后这个下拉就能用了；'
      + '现在正在跑的是<strong>串行</strong>。';
    return;
  }
  const rr = ($('optRetryMode') || {}).value === 'round_robin';
  el.innerHTML = rr
    ? '<strong>轮流发送</strong>：每回合给清单里每一项各发一个请求，'
      + '<strong>不等上一发回应</strong>就发下一项 —— 教务卡顿、某一发迟迟不回时，'
      + '其余课程照样能先提交一遍，不会一起干等。<br>'
      + '代价：开抢瞬间火力是摊开的（不是集中赌第一门）。'
      + '若只押一门势在必得的课，用「串行」更划算。'
    : '<strong>串行</strong>：第 1 项抢到（或判定失败）才轮到第 2 项，火力集中在最优先的课上。<br>'
      + '⚠️ 但教务高峰很卡时，某一发请求迟迟不回来会把后面所有课一起冻住 —— '
      + '这种情况下改用「轮流发送」。';
}

/* 409（乐观锁冲突）的统一处理。返回 true 表示「已经处理掉了」。

   后端的意思很明确：**你手上这份已经过期，本次提交一个字节都没写**。
   唯一正确的恢复方式 = 拉一次最新状态、把 S.plan 换成服务端那份，
   并明确告诉用户「刚才那一步没生效，请重做」。

   ⚠️ 绝不能拿本地陈旧副本重试 —— 那正是当初把用户清单清空两次的原因。 */
async function handlePlanConflict(e) {
  if (!e || e.kind !== 'plan_conflict') return false;
  logLine(e.message, 'err');
  const s = await refreshState({ quietRevLog: true });
  if (s) {
    logLine(`已载入服务端最新清单（版本 ${S.planRev}，共 ${S.plan.length} 项）；`
      + '你刚才那一步操作没有生效，请重新做一次', 'warn');
  }
  return true;
}

/* 把本地清单推给后端。
   ⚠️ 抢课运行中绝不动后端 plan（会把正在跑的计划替换掉），见 commitPlan 的注释。 */
async function syncPlan() {
  if (S.running) return;
  try {
    const r = await api('POST', '/api/plan', planBody(''));
    S.mutex = r.mutex || [];
    // 把服务端的新版本号记下来，后续提交都用它 —— 不回写的话，
    // 下一次提交又带着旧版本号，会被自己刚写的这一版挡在 409 上。
    if (typeof r.plan_rev === 'number') S.planRev = r.plan_rev;
  } catch (e) {
    if (await handlePlanConflict(e)) {
      renderPlan();
      renderTimetable();
      return;
    }
    logLine('清单同步失败（不影响本地清单）：' + e.message, 'err');
  }
  renderPlan();
  renderTimetable();
}

const planKey = (p) => p.do_id || p.kch_id;

async function refreshPlanConflicts() {
  if (!S.plan.length) { renderTimetable(); renderPlan(); return; }
  const cands = S.plan.map((p) => ({
    key: planKey(p), kch_id: p.kch_id, kcmc: p.kcmc || '', slots: p.slots || [],
  }));
  try {
    await checkConflicts(cands);
  } catch (_) { /* 判定失败只是没有提示，不影响清单本身 */ }
  renderTimetable();
  renderPlan();
}

function addToPlan(course, jxb) {
  const kch = course.kch_id || course.kch || '';
  const t = currentTab();
  const key = jxb.do_id || kch;
  const lv = levelOf(key);
  const info = S.conflicts[key];

  // 铁律 #6：同一门课只押一个班 → 先移除同课程的旧项
  S.plan = S.plan.filter((p) => p.kch_id !== kch);
  S.plan.push({
    kch_id: kch,
    do_id: jxb.do_id || '',
    // 教学班的**稳定** id。do_id 是每次查询都会重新下发的一次性加密令牌，
    // 抢课过程中令牌过期要「重新绑定同一个班」，只能靠 jxb_id 认回它 ——
    // 少了这个，刷新时就可能顺手换到别的班，等于擅自改了用户的选择。
    jxb_id: jxb.jxb_id || '',
    kklxdm: course.kklxdm || (t ? t.kklxdm : ''),
    // 板块下标。必须带：kklxdm 会重复（两个板块课都是 06），
    // 只存 kklxdm 的话加「大英一」的课会查到「大学体育」板块去。
    tab_index: currentTabIndex(),
    kcmc: course.kcmc || '(未命名)',
    jsxx: jxb.teacher_text || jxb.jsxx || '',
    jxbmc: jxb.kcmc || '',
    jxdd: jxb.jxdd_text || jxb.jxdd || '',
    xf: jxb.xf || course.xf || '',
    cxbj: course.cxbj || '0',
    fxbj: course.fxbj || '0',
    priority: S.plan.length,
    max_attempts: MAX_ATTEMPTS,
    interval_ms: 800,
    precheck: true,
    slots: jxb.slots || [],     // 课表画「待选」那一层、以及判互斥都要用
  });

  const timeTip = (jxb.sksj_text || jxb.sksj || '').replace(/<br\s*\/?>/gi, ' / ');
  const why = info && info.hits.length ? '：' + info.hits[0].detail : '';
  if (lv === 'hard') {
    logLine(`⚠ 已加入（与已有安排时间冲突）：${course.kcmc || kch}（${timeTip}）${why}` +
            `\n   → 冲突不拦；抢课时若同节次的其他课先抢到，本项会自动跳过`, 'warn');
  } else if (lv === 'soft') {
    logLine(`⚠ 已加入（节次重叠、周次错开）：${course.kcmc || kch}（${timeTip}）${why}`, 'warn');
  } else {
    logLine(`已加入清单：${course.kcmc || kch}（${timeTip}）`, 'ok');
  }

  // 清单变了 → 其他候选的冲突结论也跟着变，全部重算
  // ⚠️ 先「秒显」再「同步」：rerenderExpanded 只重画按钮态（本地值，立刻生效），
  //    commitPlan 里的网络同步/冲突重判放后台，别让按钮干等网络回来。
  rerenderExpanded();
  commitPlan().catch((e) => logLine('清单刷新失败：' + e.message, 'err'));
  return true;
}

/* 从抢课清单里移除一门课。
 *
 * 按 **kch_id** 移除，不是按 do_id —— 铁律 #6 保证同一门课在清单里只有一个班，
 * 所以课号就是这一项的唯一标识；用 do_id 反而会在「令牌刚换过、前端手中那份已过期」
 * 时匹配不上。返回是否真的移除了（没这项就返回 false，不重复刷日志）。
 */
function removeFromPlan(kch, label) {
  const before = S.plan.length;
  S.plan = S.plan.filter((p) => p.kch_id !== kch);
  if (S.plan.length === before) return false;
  S.plan.forEach((p, k) => { p.priority = k; });   // 顺序即优先级，删完要重排
  logLine(`已从清单移除：${label || kch}`, 'info');
  rerenderExpanded();                              // 教学班行的「✓ 已加入」立刻翻回「+ 加入」
  commitPlan().catch((e) => logLine('清单刷新失败：' + e.message, 'err'));
  return true;
}

/* 「上次保存的数据」条 —— 启动**不再自动恢复**清单（2026-09-30 用户要求）：
   磁盘上那份只当素材，用户主动点才进内存。典型时机就是查课期与抢课期之间
   那段「未开放期」：教务什么都查不到，把上次存的数据拿出来正好看课。

   显示条件（后端 `snapshot` 字段已经帮我们判好了一半）：
     · 磁盘上确实有快照（`exists`/`count`），而且
     · **内存里现在没有清单** —— 有清单时后端直接返回 {}，不会来回啰嗦。
   所以只要拿到了内容就显示；未开放期额外加一句提示，因为那时它最有意义。 */
function renderSnapshotBar() {
  const el = $('snapshotBar');
  const s = S.snapshot;
  if (!el) return;
  if (!s || !s.count || S.snapshotDismissed) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const when = s.saved_at
    ? new Date(s.saved_at * 1000).toLocaleString('zh-CN', { hour12: false })
    : '时间未知';
  const courses = (s.courses || []).join('、');
  const more = s.count > (s.courses || []).length ? ` 等 ${s.count} 门` : '';
  // 「未开放期」= 登录态有效、但教务还没开选课。那时课搜不到、已选也刷不出来，
  // 正是这份数据最有用的时候，所以说得明确一点，别让用户以为界面坏了。
  const closed = (S.sessionState === 'ok') && (S.isOpen === false);
  const tip = closed
    ? '当前教务未开放，查不到课程 —— 可以加载上次保存的数据来查看。'
    : '启动时不会自动载入清单；要用上次那份可以点右边加载。';
  el.className = 'note' + (closed ? ' warn' : ' info');
  el.style.display = '';
  el.innerHTML =
    `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">`
    + `<span>📂 上次保存的数据：<b>${s.count} 项</b>`
    + `<span class="muted">（${esc(when)}${s.semester ? '，' + esc(s.semester) : ''}）</span></span>`
    + `<span class="spacer" style="flex:1"></span>`
    + `<button class="tiny primary" id="btnLoadSnapshot">加载查看</button>`
    + `<button class="tiny" id="btnCloseSnapshot" title="关闭这条提示（不会删除磁盘上保存的数据）"
             style="margin-left:-4px;padding:3px 7px;line-height:1;color:var(--text-3)">✕</button>`
    + `</div>`
    + `<div class="muted" style="margin-top:4px">${esc(tip)}`
    + (courses ? `<br>内容：${esc(courses)}${esc(more)}` : '')
    + `</div>`;
  const btn = $('btnLoadSnapshot');
  if (btn) btn.onclick = () => loadSnapshot(btn);
  // ✕ 关闭：记下「本次会话已收起」，让 3 秒轮询 / 清空清单等触发的重绘不再把它弹回来。
  // **不动磁盘快照** —— 数据还在 state/plan.json；本次会话内不再打扰，
  // 但下次刷新页面（会话重置）后仍会出现，符合「上次保存的数据」的定位。
  const close = $('btnCloseSnapshot');
  if (close) close.onclick = () => {
    S.snapshotDismissed = true;
    el.style.display = 'none';
    el.innerHTML = '';
  };
}

/* 加载落盘快照。⚠️ 加载进来的会**直接成为当前抢课清单**（用户选定的语义），
   所以内存里已经有清单时必须先确认 —— 覆盖用户正在攒的东西是这个项目最容易
   踩的雷（清单被清空过两次）。 */
async function loadSnapshot(btn) {
  const s = S.snapshot || {};
  const n = s.count || 0;
  if (S.plan.length) {
    const ok = window.confirm(
      `当前已经有 ${S.plan.length} 项清单。\n\n`
      + `加载上次保存的数据会**替换**掉它（上次那份共 ${n} 项）。\n`
      + `确定要替换吗？`);
    if (!ok) return;
  }
  if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
  try {
    const r = await api('POST', '/api/plan/load', {});
    logLine(`已加载上次保存的数据：${r.count} 项（来自 ${(r.plan_meta || {}).path || '本地文件'}）；`
      + '状态已重置为「待选」，可直接开始抢课', 'ok');
    // ⚠️ 刻意**不**在这里改 S.planRev：让 refreshState 看到服务端版本号变大，
    // 由那条既有的「服务端是权威」路径把 S.plan 整体重建（含课表、冲突标注）。
    // 自己手动拼一遍 S.plan 只会多出一份会和后端走偏的代码。
    await refreshState();
  } catch (e) {
    logLine('加载失败：' + e.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '加载查看'; }
  }
}

function renderPlan() {
  const box = $('planList');
  $('planCount').textContent = S.plan.length + ' 项';
  updateStartBtn();
  // 「上次保存的数据」条：只有清单为空且磁盘上有快照时才显示，
  // 所以放在这里（清空清单后它能立刻出现，加载完又能立刻消失）
  renderSnapshotBar();
  // 清单变了 → 待加选学分立刻跟着变（不等 3 秒轮询）
  if (S.credit) renderCredit(S.credit);
  if (!S.plan.length) {
    box.innerHTML = '<div class="empty">从左侧课程列表点击「+ 加入清单」</div>';
    return;
  }
  // 序号 = 抢课优先顺序；互斥项来自后端 Plan.conflict_pairs()（下标对）
  const mutexOf = (i) => S.mutex
    .filter(([a, b]) => a === i || b === i)
    .map(([a, b]) => (a === i ? b : a) + 1);

  box.innerHTML = S.plan.map((p, i) => {
    // 按下标取板块名：kklxdm 会重复，按它找会永远显示第一个「板块课」
    const _tab = (typeof p.tab_index === 'number' && p.tab_index >= 0)
      ? S.tabs[p.tab_index]
      : S.tabs.find((t) => t.kklxdm === p.kklxdm);
    const tabName = (_tab || {}).name || p.kklxdm || '';
    const mk = markOf(planKey(p));
    const mtx = mutexOf(i);
    const warn = mk === 'hard'
      ? ' <span class="pill err"><span class="dot"></span>与已选时间冲突</span>'
      : (mk === 'soft' ? ' <span class="pill warn"><span class="dot"></span>与已选节次重叠</span>' : '');
    const mtxPill = mtx.length
      ? ` <span class="pill warn" title="这几项**真的**撞时间（星期+节次+周次都重叠），` +
        `只能上成其中一门；抢课时按本表顺序来，谁先抢到，与之相撞的自动跳过"><span class="dot"></span>` +
        `⇄ 互斥 #${mtx.join(', #')}</span>`
      : '';
    return `
    <div class="item">
      <div class="ord">${i + 1}</div>
      <div class="main">
        <div class="title">${esc(p.kcmc)}${warn}${mtxPill}</div>
        <div class="meta">
          ${esc(p.kch_id)} ${p.do_id ? '· 已定班' : '· 待定班'}
          ${tabName ? '· ' + esc(tabName) : ''}
          ${p.xf ? '· ' + esc(p.xf) + ' 学分' : ''}
          · 上限 ${p.max_attempts} 次 / ${p.interval_ms}ms
        </div>
        <div class="meta">
          ${esc(p.sksj || (p.slots || []).map((s) => s.text).join(' / ') || '时间待定')}
          ${p.jsxx ? ' · ' + esc(p.jsxx) : ''}${p.jxdd ? ' · ' + esc(p.jxdd) : ''}
        </div>
      </div>
      <div class="actions">
        <button class="tiny" data-up="${i}" ${i === 0 ? 'disabled' : ''} title="上移（提高抢课优先级）">↑</button>
        <button class="tiny" data-down="${i}" ${i === S.plan.length - 1 ? 'disabled' : ''} title="下移（降低抢课优先级）">↓</button>
        <button class="tiny" data-del="${i}" title="从清单移除">删</button>
      </div>
    </div>`;
  }).join('');

  // 任何顺序调整／删除 → 立刻同步给后端（互斥关系随顺序一起重算）
  const mutate = (fn, msg) => {
    fn();
    S.plan.forEach((p, k) => { p.priority = k; });
    if (msg) logLine(msg, 'info');
    commitPlan().catch((e) => logLine('清单刷新失败：' + e.message, 'err'));
  };

  box.querySelectorAll('button[data-del]').forEach((b) => {
    b.onclick = () => {
      const i = parseInt(b.dataset.del, 10);
      const p = S.plan[i];
      if (p) removeFromPlan(p.kch_id, p.kcmc);   // 与教学班列表的「再点一次移除」同一套逻辑
    };
  });
  box.querySelectorAll('button[data-up]').forEach((b) => {
    b.onclick = () => {
      const i = parseInt(b.dataset.up, 10);
      mutate(() => { [S.plan[i - 1], S.plan[i]] = [S.plan[i], S.plan[i - 1]]; });
    };
  });
  box.querySelectorAll('button[data-down]').forEach((b) => {
    b.onclick = () => {
      const i = parseInt(b.dataset.down, 10);
      mutate(() => { [S.plan[i + 1], S.plan[i]] = [S.plan[i], S.plan[i + 1]]; });
    };
  });
}

$('btnClearPlan').onclick = () => {
  S.plan = [];
  S.mutex = [];
  renderPlan();
  renderTimetable();
  syncPlan().catch(() => {});
  refreshConflictsForExpanded().catch(() => {});
  logLine('清单已清空', 'info');
};

// 学分条的 ↻ 用委托绑在容器上：渲染函数每次都会重建那个按钮，
// 直接绑按钮会随重建一起失效。
$('creditBar').addEventListener('click', (e) => {
  const b = e.target.closest('#btnCreditRefresh');
  if (b) refreshCredit(b);
});

// 派发方式切换 → 立刻同步给后端。
// ⚠️ 必须同步：后端是拿 Plan 去跑的，界面上改了而不提交，等于**没改**，
//    界面显示就会与实际执行不一致。运行中该下拉是禁用的（见 applyState）。
$('optRetryMode').addEventListener('change', () => {
  renderModeHint();
  if (S.running) return;          // 双保险：禁用之外再挡一次
  // ⚠️ 清单为空时**不提交**：POST /api/plan 传空清单会把落盘那份一并清掉
  //（`_persist_plan` 里「空清单 = 删文件」），而用户此刻只是在挑模式、
  // 并没有要清空「上次保存的数据」。这个选择会在下一次加课/启动时自然带上。
  if (!S.plan.length) return;
  syncPlan().catch((e) => logLine('派发方式同步失败：' + e.message, 'err'));
});
renderModeHint();

// ---------- 任务 ----------

$('btnStart').onclick = async () => {
  if (!S.plan.length) return;
  $('btnStart').disabled = true;
  const startAt = ($('startAt').value || '').trim();
  try {
    const plan = await api('POST', '/api/plan', planBody(startAt));
    if (typeof plan.plan_rev === 'number') S.planRev = plan.plan_rev;
    await api('POST', '/api/start');
    $('log').innerHTML = '';
    // ⭐ 游标必须归零**再**订阅：日志面板刚被清空，我们要的就是「本轮从第一条开始」。
    //
    // 为什么不干脆沿用 `S.seq`：它是**进程内**的序号，而浏览器这份游标能跨进程存活。
    // 服务重启过（或上一轮残留）时 `S.seq` 会比服务端序号还大，于是 SSE 那边
    // `seq > since` 永远不成立 —— **实时日志整段空白**。
    // 2026-10-01 用户报的「串行模式实时日志里怎么不显示了」就是这个：只要点过
    // 第二次「开始」日志就不出来（第一轮正常，所以很难联想到序号）。
    //
    // 归零安全：`RUNTIME.start()` 会清空事件缓冲（只保留本轮），所以 `since=0`
    // 拿到的正好是本轮已有的事件，不会把上一轮的日志倒进来、也不会重复。
    // ⚠️ 只在这里归零 —— SSE **断线重连**时（`subscribe()` 里那条重连）绝不能归零，
    //    那时面板里已经有内容了，重放会变成重复行。
    S.seq = 0;
    if (plan.scheduled) {
      logLine('定时开抢已启动，目标时刻 ' + fmtTs(plan.start_at) +
              '（会先预热，把教学班全部解析好再等点）', 'ok');
    } else {
      logLine('抢课任务已启动（立即模式）', 'ok');
    }
    $('progressText').textContent = plan.scheduled ? '等待开抢时刻…' : '运行中…';
    subscribe();
    await refreshState();
  } catch (e) {
    // 乐观锁冲突：清单在你点「开始」的这一刻被别处改过 → 本次启动**没生效**。
    // 已经重新载入了最新清单，让用户看清楚再点一次；不要把它说成「启动失败」。
    if (await handlePlanConflict(e)) {
      renderPlan();          // 它内部会调 updateStartBtn()
      renderTimetable();
      return;
    }
    logLine('启动失败：' + e.message, 'err');
    updateStartBtn();
  }
};

$('btnClock').onclick = async () => {
  const btn = $('btnClock');
  btn.disabled = true;
  btn.textContent = '采样中…';
  try {
    const c = await api('GET', '/api/clock?samples=6');
    S.clock = c;
    renderClock(c);
    if (c.synced) {
      const dir = c.offset_ms >= 0 ? '慢' : '快';
      logLine(`时钟校准：本地钟比服务器${dir} ${Math.abs(c.offset_ms)}ms，` +
              `不确定度 ±${c.uncertainty_ms}ms，建议提前 ${c.lead_ms}ms 开火`, 'ok');
    } else {
      logLine('时钟校准失败：响应里没有可解析的 Date 头', 'err');
    }
  } catch (e) {
    logLine('时钟校准失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '校准时钟';
  }
};

$('btnStop').onclick = async () => {
  try {
    await api('POST', '/api/stop');
    logLine('已请求停止，等待当前尝试结束…', 'warn');
  } catch (e) { logLine('停止失败：' + e.message, 'err'); }
};

// ---------- 蹲课（2026-10-01）----------
//
// 蹲课 = 长时间蹲「已满」课的退课名额。与抢课的关键差异：
//   · 派发方式**固定**「轮流发送」（round_robin），后端在 /api/wait/plan 里写死；
//   · **按时间不按次数**：没有次数上限，靠「开始时刻 + 结束时刻」框定区间；
//   · 每条申请请求间隔 1s / 0.8s 两档，用户自选。
// 蹲课复用抢课的拨轮控件（buildWheelCol / setWheel 都支持 onChange 回调），
// 只是有两套（开始 / 结束）共 6 个拨轮，各自把值拼进自己的隐藏字段。

// 蹲课拨轮：'waitSH' | 'waitSM' | 'waitSS'（开始） / 'waitEH' | 'waitEM' | 'waitES'（结束）
// 复用全局 WHEELS 表 —— buildWheelCol 按 id 存进去，setWheel/syncWaitAt 按 id 取。
let waitStartDay = 'now';   // 'now' = 立即；'0' = 今天；'1' = 明天
let waitEndDay = '0';       // 结束**没有**「立即」——按时间，终点必填，默认今天

/* 把「目标日 + 三个拨轮」拼成提交串。
   返回 { start, deadline } 两个「YYYY-MM-DD HH:MM:SS」串（或 start 为 '' 表示立即）。
   ⚠️ 复用 syncStartAt 的思路：定时一律写完整日期，语义唯一。 */
function waitTimes() {
  const d = new Date();
  const dayStr = (offset) => {
    const x = new Date(d);
    if (offset) x.setDate(x.getDate() + offset);
    return `${x.getFullYear()}-${pad2(x.getMonth() + 1)}-${pad2(x.getDate())}`;
  };
  let start = '';
  if (waitStartDay !== 'now' && WHEELS.waitSH) {
    start = `${dayStr(waitStartDay === '1' ? 1 : 0)} ` +
      `${pad2(WHEELS.waitSH.v)}:${pad2(WHEELS.waitSM.v)}:${pad2(WHEELS.waitSS.v)}`;
  }
  let deadline = '';
  if (WHEELS.waitEH) {
    deadline = `${dayStr(waitEndDay === '1' ? 1 : 0)} ` +
      `${pad2(WHEELS.waitEH.v)}:${pad2(WHEELS.waitEM.v)}:${pad2(WHEELS.waitES.v)}`;
  }
  return { start, deadline };
}

/* 蹲课时间拨轮变化后：重算回显 + 「开始蹲课」按钮可用态。
   与 syncStartAt 对称，只是这里读的是蹲课那两套拨轮。 */
function syncWaitAt() {
  const on = waitStartDay !== 'now';
  const sw = $('waitStartWheel');
  if (sw) sw.classList.toggle('off', !on);
  const { start, deadline } = waitTimes();
  renderWaitEcho(start, deadline);
  updateWaitBtn();
}

/* 蹲课回显：把「开始 → 结束」这个区间说清楚，并提示按时间不按次数。
   结束必须晚于开始，否则红字告警并禁用「开始蹲课」。 */
function renderWaitEcho(start, deadline) {
  const el = $('waitEcho');
  if (!el) return;
  const show = deadline;
  if (!show) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  const sStr = start ? `<strong>${start}</strong>` : '（立即）';
  const ok = !start || (new Date(deadline.replace(/-/g, '/')).getTime()
    > new Date(start.replace(/-/g, '/')).getTime());
  if (!ok) {
    el.className = 'note warn';
    el.innerHTML = `⚠ 结束时刻 <strong>${deadline}</strong> 必须晚于开始时刻 ` +
      `${sStr}。请把「结束」往后拨，或把「开始」改成更早的日期。`;
  } else {
    el.className = 'note info';
    el.innerHTML = `从 ${sStr} 蹲到 <strong>${deadline}</strong>` +
      `　·　按时间不按次数，间隔 <strong>${S.waitInterval}ms</strong>，轮流发送。`;
  }
}

/* 「开始蹲课」按钮可用状态的**唯一**判据。
   条件：清单非空、未在跑、结束时刻已定且（若设了开始）结束晚于开始。 */
function updateWaitBtn() {
  const { start, deadline } = waitTimes();
  const ok = !start || (new Date(deadline.replace(/-/g, '/')).getTime()
    > new Date(start.replace(/-/g, '/')).getTime());
  const btn = $('btnStartWait');
  if (btn) btn.disabled = S.waitRunning || S.wait.length === 0 || !deadline || !ok;
}

/* 蹲课清单渲染。与 renderPlan 对称，但蹲课项**没有**上移/下移（轮流发送，顺序无优先级语义），
   只保留「删」。蹲课项不画冲突 pill（蹲的是退课名额，时间冲突不影响蹲）。 */
function renderWait() {
  const box = $('waitList');
  const cnt = $('waitCount');
  if (cnt) cnt.textContent = S.wait.length + ' 项';
  updateWaitBtn();
  if (!box) return;
  if (!S.wait.length) {
    box.innerHTML = '<div class="empty">在搜索结果的课程里点「+ 蹲」加入要蹲名额的课</div>';
    return;
  }
  box.innerHTML = S.wait.map((p, i) => {
    const tabName = (typeof p.tab_index === 'number' && p.tab_index >= 0)
      ? (S.tabs[p.tab_index] || {}).name
      : (S.tabs.find((t) => t.kklxdm === p.kklxdm) || {}).name;
    // ⭐ 蹲到了名额（won）→ 序号下面亮「已蹲到」；否则不显示。
    const wonTag = p.state === 'won'
      ? '<div class="won-tag">已蹲到</div>' : '';
    return `
    <div class="item${p.state === 'won' ? ' won' : ''}">
      <div class="ord-wrap"><div class="ord">${i + 1}</div>${wonTag}</div>
      <div class="main">
        <div class="title">${esc(p.kcmc)}</div>
        <div class="meta">
          ${esc(p.kch_id)}
          ${tabName ? '· ' + esc(tabName) : ''}
          ${p.xf ? '· ' + esc(p.xf) + ' 学分' : ''}
          · 蹲退课名额（按时间）
        </div>
        <div class="meta">
          ${esc(p.sksj || (p.slots || []).map((s) => s.text).join(' / ') || '时间待定')}
          ${p.jsxx ? ' · ' + esc(p.jsxx) : ''}${p.jxdd ? ' · ' + esc(p.jxdd) : ''}
        </div>
      </div>
      <div class="actions">
        <button class="tiny" data-wdel="${i}" title="从蹲课清单移除">删</button>
      </div>
    </div>`;
  }).join('');
  box.querySelectorAll('button[data-wdel]').forEach((b) => {
    b.onclick = () => {
      const i = parseInt(b.dataset.wdel, 10);
      const p = S.wait[i];
      // 蹲课允许同课不同班 → 用 do_id 精确删这一项，别用 kch_id（会误删同课其他班）。
      if (p) removeFromWait(p.do_id || p.kch_id, p.kcmc);
    };
  });
}

function removeFromWait(kch, label) {
  const before = S.wait.length;
  // ⚠️ 蹲课允许同课不同班，所以**不能按 kch_id 移除**（那会一次删掉同课所有班）。
  //   这里 `kch` 参数实际可能传的是 do_id（见 renderClassRows 的 data-wk 点击），
  //   优先按 do_id 匹配；否则按 kch_id 匹配（但只删第一项，保留同课其他班）。
  let idx = S.wait.findIndex((p) => p.do_id && p.do_id === kch);
  if (idx < 0) idx = S.wait.findIndex((p) => p.kch_id === kch);
  if (idx < 0) return false;
  S.wait.splice(idx, 1);
  S.wait.forEach((p, k) => { p.priority = k; });
  logLine(`已从蹲课清单移除：${label || kch}`, 'info');
  renderWait();
  rerenderExpanded();                              // 教学班行的「✓ 已蹲」立刻翻回「+ 蹲」
  syncWait().catch((e) => logLine('蹲课清单同步失败：' + e.message, 'err'));
  return true;
}

/* 组装 POST /api/wait/plan 的请求体。与 planBody 对称，带乐观锁。 */
function waitBody() {
  const { start, deadline } = waitTimes();
  const b = {
    items: S.wait,
    interval_ms: S.waitInterval,
    start_at: start,
    deadline_at: deadline,
  };
  if (S.waitRev !== null) b.base_version = S.waitRev;
  return b;
}

/* 把本地蹲课清单推给后端（独立 /api/wait/plan）。 */
async function syncWait() {
  if (S.waitRunning) return;
  try {
    const r = await api('POST', '/api/wait/plan', waitBody());
    if (typeof r.wait_rev === 'number') S.waitRev = r.wait_rev;
    // 后端会把解析好的开始/结束时刻（unix 秒）回显过来，存下供回显/判断。
    if (r.start_at != null) S.waitStartAt = r.start_at;
    if (r.deadline_at != null) S.waitDeadlineAt = r.deadline_at;
    S.waitInterval = r.interval_ms || S.waitInterval;
  } catch (e) {
    if (e.kind === 'plan_conflict') {
      logLine(e.message, 'err');
      await refreshState({ quietRevLog: true });
      renderWait();
      return;
    }
    logLine('蹲课清单同步失败（不影响本地清单）：' + e.message, 'err');
  }
  renderWait();
}

/* 从搜索结果的教学班加进蹲课清单（蹲退课名额）。
   ⚠️ 与抢课不同（2026-10-01 用户要求）：蹲课**允许同一节课的多个教学班**并存 ——
   蹲的是「哪个班有人退课」，多押几个班等于多几个中签机会。
   所以判重按**教学班（do_id / jxb_id）**，不按课程号 kch_id（抢课铁律 #6 在这里不适用）。 */
function addToWait(course, jxb) {
  const kch = course.kch_id || course.kch || '';
  // 同一教学班不重复加（do_id 优先；没 do_id 退回 jxb_id 判重），同课不同班可共存。
  const dup = (p) => {
    if (jxb.do_id) return p.do_id === jxb.do_id;
    if (jxb.jxb_id) return p.jxb_id === jxb.jxb_id;
    return false;
  };
  if (S.wait.some(dup)) return false;
  S.wait.push({
    kch_id: kch,
    do_id: jxb.do_id || '',
    jxb_id: jxb.jxb_id || '',
    kklxdm: course.kklxdm || '',
    tab_index: currentTabIndex(),
    kcmc: course.kcmc || '(未命名)',
    jsxx: jxb.teacher_text || jxb.jsxx || '',
    jxbmc: jxb.kcmc || '',
    jxdd: jxb.jxdd_text || jxb.jxdd || '',
    xf: jxb.xf || course.xf || '',
    cxbj: course.cxbj || '0',
    fxbj: course.fxbj || '0',
    priority: S.wait.length,
    slots: jxb.slots || [],
  });
  const timeTip = (jxb.sksj_text || jxb.sksj || '').replace(/<br\s*\/?>/gi, ' / ');
  logLine(`已加入蹲课：${course.kcmc || kch}（${timeTip}）`, 'ok');
  renderWait();
  // ⚠️ 补上教学班行的「✓ 已蹲」秒显 —— 之前 addToWait 没重画教学班列表，
  //    「✓ 已蹲」只能靠 3 秒轮询，所以时有时无。
  rerenderExpanded();
  syncWait().catch((e) => logLine('蹲课清单同步失败：' + e.message, 'err'));
  return true;
}

/* 蹲课两套拨轮 + 间隔档 + 开始/结束日期分段。 */
function initWaitWheel() {
  // 开始时刻：时/分/秒
  buildWheelCol('waitSH', 23, syncWaitAt);
  buildWheelCol('waitSM', 59, syncWaitAt);
  buildWheelCol('waitSS', 59, syncWaitAt);
  // 结束时刻：时/分/秒
  buildWheelCol('waitEH', 23, syncWaitAt);
  buildWheelCol('waitEM', 59, syncWaitAt);
  buildWheelCol('waitES', 59, syncWaitAt);

  // 初值 = 当前时刻（就近到秒），结束默认 = 当前 + 1 小时
  const d = new Date();
  const e = new Date(d.getTime() + 3600 * 1000);
  setWheel(WHEELS.waitSH, d.getHours());
  setWheel(WHEELS.waitSM, d.getMinutes());
  setWheel(WHEELS.waitSS, d.getSeconds());
  setWheel(WHEELS.waitEH, e.getHours());
  setWheel(WHEELS.waitEM, e.getMinutes());
  setWheel(WHEELS.waitES, e.getSeconds());

  // 间隔档（1s / 0.8s）
  const seg = $('waitIntervalSeg');
  if (seg) {
    const paint = () => {
      for (const b of seg.querySelectorAll('button')) {
        b.classList.toggle('on', Number(b.dataset.ms) === S.waitInterval);
      }
    };
    paint();
    for (const b of seg.querySelectorAll('button')) {
      b.onclick = () => {
        S.waitInterval = Number(b.dataset.ms);
        paint();
        syncWaitAt();      // 回显里带间隔数字
      };
    }
  }

  // 开始日期分段（立即 / 今天 / 明天）
  const sseg = $('waitStartSeg');
  if (sseg) {
    const paint = () => {
      for (const b of sseg.querySelectorAll('button')) {
        b.classList.toggle('on', b.dataset.day === waitStartDay);
      }
    };
    paint();
    for (const b of sseg.querySelectorAll('button')) {
      b.onclick = () => {
        waitStartDay = b.dataset.day;
        paint();
        syncWaitAt();
      };
    }
  }

  // 结束日期分段（今天 / 明天）
  const eseg = $('waitEndSeg');
  if (eseg) {
    const paint = () => {
      for (const b of eseg.querySelectorAll('button')) {
        b.classList.toggle('on', b.dataset.day === waitEndDay);
      }
    };
    paint();
    for (const b of eseg.querySelectorAll('button')) {
      b.onclick = () => {
        waitEndDay = b.dataset.day;
        paint();
        syncWaitAt();
      };
    }
  }

  syncWaitAt();
}

// 「开始蹲课」：先提交清单（带时间+间隔+乐观锁），再启动。
$('btnStartWait').onclick = async () => {
  if (!S.wait.length) return;
  $('btnStartWait').disabled = true;
  try {
    const r = await api('POST', '/api/wait/plan', waitBody());
    if (typeof r.wait_rev === 'number') S.waitRev = r.wait_rev;
    await api('POST', '/api/wait/start');
    logLine(`蹲课已启动：${S.wait.length} 项，间隔 ${S.waitInterval}ms，轮流发送`, 'ok');
    // ⭐ 与抢课 btnStart 对齐：重置事件游标再订阅，否则蹲课的实时日志
    //   要么被上一轮抢课的陈旧游标夹掉、要么整段空白（「开始蹲课后没日志」的根因）。
    S.seq = 0;
    $('log').innerHTML = '';
    subscribe();
    await refreshState();
  } catch (e) {
    if (e.kind === 'plan_conflict') {
      logLine(e.message, 'err');
      await refreshState({ quietRevLog: true });
      renderWait();
      updateWaitBtn();
      return;
    }
    logLine('蹲课启动失败：' + e.message, 'err');
    updateWaitBtn();
  }
};

$('btnStopWait').onclick = async () => {
  try {
    await api('POST', '/api/wait/stop');
    logLine('已请求停止蹲课，等待当前尝试结束…', 'warn');
  } catch (e) { logLine('停止蹲课失败：' + e.message, 'err'); }
};

$('btnClearWait').onclick = async () => {
  S.wait = [];
  renderWait();
  rerenderExpanded();                              // 教学班行的「✓ 已蹲」立刻翻回「+ 蹲」
  try {
    const r = await api('POST', '/api/wait/clear');
    if (typeof r.wait_rev === 'number') S.waitRev = r.wait_rev;
    logLine('蹲课清单已清空', 'info');
  } catch (e) { logLine('清空蹲课清单失败：' + e.message, 'err'); }
};

function renderTaskList(items) {
  const box = $('taskList');
  if (!items.length) {
    box.innerHTML = '<div class="empty">尚未启动抢课任务</div>';
    return;
  }
  const LABEL = {
    pending: ['待处理', ''], running: ['进行中', 'info'],
    won: ['已抢到', 'ok'], failed: ['失败', 'err'],
    skipped: ['跳过', 'warn'], aborted: ['已中止', ''],
  };
  box.innerHTML = items.map((it) => {
    const [txt, cls] = LABEL[it.state] || ['未知', ''];
    const pct = it.max_attempts ? Math.min(100, Math.round(it.attempts / it.max_attempts * 100)) : 0;
    return `<div class="item">
      <div class="main">
        <div class="title">${esc(it.kcmc)}</div>
        <div class="meta">
          ${esc(it.kch_id)} · 尝试 ${it.attempts}/${it.max_attempts}
          ${it.last_msg ? ' · ' + esc(it.last_msg) : ''}
        </div>
        <div class="bar"><i class="${it.state === 'won' ? 'ok' : ''}" style="width:${pct}%"></i></div>
      </div>
      <div class="actions"><span class="pill ${cls}"><span class="dot${it.state === 'running' ? ' pulse' : ''}"></span>${txt}</span></div>
    </div>`;
  }).join('');
}

// ---------- 事件流 ----------

function subscribe() {
  if (S.evtSource) { S.evtSource.close(); S.evtSource = null; }
  const es = new EventSource('/api/events/stream?since=' + S.seq);
  S.evtSource = es;

  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    S.seq = Math.max(S.seq, ev.seq || 0);
    handleEvent(ev);
  };
  es.addEventListener('done', (m) => {
    try {
      const snap = JSON.parse(m.data);
      renderTaskList(snap.items || []);
      $('progressText').textContent =
        `结束：成功 ${snap.counts.won || 0} / 共 ${(snap.items || []).length}`;
    } catch (_) {}
    es.close();
    S.evtSource = null;
    logLine('任务结束', 'info');
    refreshState();
  });
  es.onerror = () => {
    // 任务结束后后端关流是正常现象；只在仍运行时提示。
    // ⚠️ 蹲课也走这条事件流：重连判据要同时看抢课(S.running)与蹲课(S.waitRunning)，
    //   否则蹲课进行中连接断了既不提示、也不重连，日志会静默消失。
    if (S.running || S.waitRunning) logLine('事件流中断，正在重连…', 'warn');
    es.close();
    S.evtSource = null;
    if (S.running || S.waitRunning) setTimeout(subscribe, 1500);
  };
}

const KIND_HINT = {
  session_expired: '登录态失效，请重新粘贴 Cookie',
  context_invalid: '上下文失效，需重新初始化',
  not_open: '当前不在选课阶段',
  maintenance: '教务系统维护中',
  // ⚠️ 满员在**抢课**里是终态失败（一次都不重试），在**蹲课**里却是「继续蹲退课名额」
  //   （蹲的正是满员名额）。所以这里的中性文案只说「教学班已满」，不加「不重试」——
  //   抢课的失败事件走 give_up（日志「⛔ …教学班已满」），蹲课的等待事件走 retry_wait
  //   （日志「✗ …继续蹲退课名额」），各自的消息已经说清了重不重试，这里别再补一刀。
  full: '教学班已满',
  conflict: '时间冲突',
  already_taken: '同课程只能选一个教学班',
  // ⭐ 与上面那条**必须分开**：这条是「我们押的这个班已经在你名下了」= 判**已抢到**。
  // 合并的话，一旦提交超时但实际生效（重试会拿到这个语义），界面就会把
  // 已经抢到的课报成「失败」。
  already_in_class: '该教学班已在你名下（按已抢到处理）',
  quota_exceeded: '超出类别门次限制',
  too_frequent: '选课频率过高，降速中',
  network: '网络异常/超时（请求没走到教务），会自动重试',
  unknown: '未识别的返回',
};

function handleEvent(ev) {
  const tail = ev.item_key ? `[${ev.item_key}] ` : '';
  const hint = ev.kind ? ('（' + (KIND_HINT[ev.kind] || ev.kind) + '）') : '';
  switch (ev.type) {
    case 'plan_start':
      logLine(ev.message, 'info'); break;
    case 'attempt':
      logLine(`${tail}${ev.message}`, 'info'); break;
    case 'success':
      logLine(`${tail}✅ ${ev.message}（第 ${ev.attempt} 次，${ev.elapsed_ms}ms）`, 'ok'); break;
    case 'retry_wait':
      logLine(`${tail}✗ ${ev.message}${hint} → 重试`, 'warn'); break;
    case 'give_up':
      logLine(`${tail}⛔ ${ev.message}${hint}`, 'err'); break;
    case 'need_login':
      // 引擎在后台撞上「登录态失效」时会推这条。
      // 翻牌 + 日志都由 noteSessionExpired 一手包办（它只播一次），
      // 这里不必再 logLine，否则同一件事会打两遍。
      noteSessionExpired(ev.message);
      refreshState().catch(() => {});
      break;
    // ---- 定时开抢 ----
    case 'clock':
      logLine('⏱ ' + ev.message, 'info'); break;
    case 'prewarm_start':
      logLine('🔥 ' + ev.message, 'info'); break;
    case 'prewarm_ready':
      logLine('✅ ' + ev.message, 'ok'); break;
    case 'countdown':
      logLine('⏳ ' + ev.message, 'info'); break;
    case 'fire':
      logLine('🚀 ' + ev.message, 'ok'); break;
    case 'log':
      logLine(ev.message, ev.kind && ev.kind !== 'unknown' ? 'warn' : ''); break;
    default:
      logLine(ev.message || ev.type, '');
  }
  // 每次事件后轻量刷新任务列表
  throttleRefresh();
}

let refreshTimer = null;
function throttleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refreshState();
  }, 600);
}

// ---------- 初始化 ----------

(async function init() {
  logLine('界面就绪。请输入学号和密码登录。', 'info');
  // ⭐ 访问口令是**第一道门**：先确保口令校验通过，再进登录门。
  // 不需要口令时 ensureAccess 会立即放行；需要时它会弹访问码层，等用户输对。
  await ensureAccess();
  initStartWheel();          // 必须在 refreshState 之前：它要先把 #startAt 备好
  initWaitWheel();           // 蹲课两套拨轮（开始/结束）+ 间隔档 + 日期分段
  buildFilterChips();        // 筛选器：星期/节次 chips
  const btnReset = $('btnFilterReset');
  if (btnReset) btnReset.onclick = resetFilters;
  // 学分/下拉类筛选条件：改动只更新提示（真正生效在「搜索」那一刻）
  for (const id of ['fXf', 'fCx', 'fYl', 'fSksjct']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', updateFilterHint);
  }
  renderSelected();
  renderTimetable();
  const s = await refreshState();
  if (s && s.has_session) {
    // ⭐ 2026-10-02：会话存在但尚未 init 时先补一次（方案 A 的配套）。
    //   场景：登录成功（with_init=false）后用户立刻刷新页面 —— 此时后端可能还没
    //   跑完 init，client.tabs 是空的，直接 loadTabs() 会让「课程类别」下拉变空。
    //   失败也不致命（只记一条日志），下面的 loadTabs 等照常尝试。
    if (!s.inited) {
      try {
        await api('POST', '/api/init');
        await refreshState();
      } catch (e) {
        logLine('初始化选课上下文失败：' + e.message, 'err');
      }
    }
    await loadTabs();
    // 只读后台缓存。若服务重启过（缓存没了）loadSelected 会自己回退成真拉一次。
    await loadSelected(false);
    // 学业情况统计是**只读、无缓存**的，刷新页面后要重新拉一次，否则会「丢失」
    // （它不像已选课程那样有后端缓存兜底）。数据量小，不拖慢抢课。
    await loadAcademic();
  }
  // 兜底轮询（**不含**已选课程，避免拖慢抢课）。
  // `maybeCheckSession` 搭这趟车：每 3 秒问一次内存快照，但每 ~45 秒才真的
  // 打一次教务核实登录态 —— 否则「已登录」这枚胶囊永远不会知道自己已经过期了。
  setInterval(() => { refreshState(); maybeCheckSession(); }, 3000);
  maybeCheckSession();               // 进页面立刻核一次，不用等 45 秒
})();

/* ⭐ 在线检测通道（2026-10-02）：页面开着就保持一条 SSE 长连接。
   两个用途：
     1) 关闭页面/浏览器时连接**真实断开**，后端（exe 窗口模式）据此自动退出，
        免去手动去托盘点「停止服务」；
     2) 后端要退出时（托盘点「停止服务」/自动退出）会推 `shutdown` 事件，
        页面据此提示「服务已停止」并**尝试**关闭标签页。

   为什么不用定时心跳：浏览器会**节流后台标签的定时器**（可能降到每分钟一次），
   用它判「人在不在」会误判；而连接断开不受节流影响。刷新页面会短暂断开并
   自动重连 —— 后端留了宽限期，不会误退，所以 onerror 里不必急着处理。 */
(function keepPresence() {
  let fails = 0;
  let handled = false;

  /* 后端已没了（收到 shutdown，或重连反复失败）→ 尝试关闭本标签页；
     关不掉（浏览器禁止服务器/脚本关闭「用户自己打开」的标签页）就显示提示，
     免得继续留着一个点不动的假界面。 */
  function onBackendGone() {
    if (handled) return;
    handled = true;
    try { window.close(); } catch (e) { /* 多数浏览器会拒绝，交给下面的遮罩 */ }
    // close 成功 → 页面已关，定时器不执行；失败 → 400ms 后显示提示遮罩
    setTimeout(showStoppedOverlay, 400);
  }

  try {
    const es = new EventSource('/api/presence');
    es.onopen = () => { fails = 0; };
    es.addEventListener('shutdown', onBackendGone);   // 后端主动通知（快，~2s 内）
    es.onerror = () => {
      // 兜底：后端崩溃、来不及发 shutdown 时，靠「重连连续失败」判定
      fails += 1;
      if (fails >= 4) onBackendGone();
    };
  } catch (e) { /* 极老浏览器不支持 EventSource：后端会因「从未连过」而不自动退出 */ }
})();

function showStoppedOverlay() {
  if (document.getElementById('stoppedOverlay')) return;
  const d = document.createElement('div');
  d.id = 'stoppedOverlay';
  d.style.cssText =
    'position:fixed;inset:0;z-index:99999;background:#fff;display:flex;' +
    'flex-direction:column;align-items:center;justify-content:center;gap:14px;' +
    'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#333;' +
    'text-align:center;padding:24px';
  d.innerHTML =
    '<div style="font-size:44px;line-height:1">⏹️</div>' +
    '<div style="font-size:20px;font-weight:700">服务已停止</div>' +
    '<div style="font-size:14px;color:#666;max-width:22em">' +
    '后端已退出，本页面已失效。你可以直接关闭这个标签页；' +
    '需要继续使用就重新双击「南苑抢课助手」。</div>';
  document.body.appendChild(d);
}
