/* ==========================================================
   企业合同智能审查系统 —— 前端逻辑
   ----------------------------------------------------------
   纯原生 JS，不引框架：无构建步骤，数据流一眼能看懂。

   核心原则：前端只管「展示什么」，不管「能不能」。
   所有权限判断后端都重做了一遍 —— 前端藏按钮只是体验，不是安全。
   ========================================================== */

// Windows 上 localhost 可能走 IPv6，后端只监听 127.0.0.1，必须写死 IPv4
const API_BASE = 'http://127.0.0.1:8210';
const TOKEN_KEY = 'contract_token';
const USER_KEY = 'contract_user';

const LEVEL_TEXT = { high: '高', medium: '中', low: '低', none: '无' };
const RISK_TEXT = { high: '高风险', medium: '中风险', low: '低风险', none: '未审查' };

const CATEGORY_TEXT = {
  payment: '付款',
  liability: '违约与责任',
  termination: '解除与终止',
  confidentiality: '保密',
  dispute: '争议解决',
  delivery: '交付与验收',
  ip: '知识产权',
  other: '其他',
};

const ACTION_TEXT = {
  login_success: '登录成功',
  login_failed: '登录失败',
  login_disabled: '账号停用',
  logout: '退出登录',
  contract_create_file: '上传合同',
  contract_create_text: '创建合同',
  contract_parse_failed: '解析失败',
  contract_review: '执行审查',
  review_feedback_accepted: '采纳意见',
  review_feedback_edited: '修改后采纳',
  review_feedback_rejected: '驳回意见',
  review_clause_feedback_accepted: '条款意见采纳',
  review_clause_feedback_edited: '条款意见改后采纳',
  review_clause_feedback_rejected: '条款意见驳回',
  eval_seed: '初始化评测集',
  eval_run: '跑评测',
  prompt_activate: '切换 Prompt',
};

const ROLE_CAN_CREATE = ['submitter', 'reviewer'];
const ROLE_CAN_REVIEW = ['reviewer'];
const ROLE_CAN_FEEDBACK = ['reviewer', 'admin'];

// ========================================================== 工具
const $ = (id) => document.getElementById(id);

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtNum(n) {
  if (n === null || n === undefined || n === '') return '0';
  return Number(n).toLocaleString('zh-CN');
}

function fmtPct(v) {
  return `${(Number(v || 0) * 100).toFixed(1)}%`;
}

// 条款标题里常自带「第X条」，避免和左侧编号重复
function clauseHeading(h) {
  return String(h || '').replace(/^第[一二三四五六七八九十百零〇\d]+\s*条\s*/, '').trim();
}

function downloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

let toastTimer = null;
function toast(msg, kind = '') {
  const el = $('toast');
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = 'toast';
  }, 2600);
}

// ========================================================== 状态
const state = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: null,
  contracts: [],
  contractTotal: 0,
  contractPage: 1,
  contractKeyword: '',
  currentContractId: null,
  currentDetail: null,
  currentView: 'contracts',
  auditPage: 1,
  auditFilter: '',
};

const CONTRACT_PAGE_SIZE = 20;
const AUDIT_PAGE_SIZE = 50;

function saveSession(token, user) {
  state.token = token;
  state.user = user;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

function clearSession() {
  state.token = '';
  state.user = null;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

// ========================================================== 请求封装
async function api(path, { method = 'GET', body, isForm = false, timeout = 180000 } = {}) {
  const headers = {};
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  if (!isForm && body !== undefined) headers['Content-Type'] = 'application/json; charset=utf-8';

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  let resp;
  try {
    resp = await fetch(API_BASE + path, {
      method,
      headers,
      body: isForm ? body : body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('请求超时，模型响应较慢，请稍后重试');
    throw new Error('无法连接后端服务，请确认后端已启动（127.0.0.1:8210）');
  }
  clearTimeout(timer);

  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }

  if (!resp.ok) {
    // 401 统一登出（token 过期）
    if (resp.status === 401 && state.token) {
      clearSession();
      showLogin();
      throw new Error('登录已过期，请重新登录');
    }
    let msg = '请求失败';
    if (data && typeof data.detail === 'string') {
      msg = data.detail;
    } else if (data && Array.isArray(data.detail)) {
      // pydantic 校验错误
      msg = data.detail
        .map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`)
        .join('；');
    }
    throw new Error(msg);
  }
  return data;
}

// ========================================================== 登录
function showLogin() {
  $('screenLogin').classList.add('active');
  $('screenApp').classList.remove('active');
  $('loginErr').textContent = '';
  $('loginPass').value = '';
  setTimeout(() => $('loginUser').focus(), 50);
}

function showApp() {
  $('screenLogin').classList.remove('active');
  $('screenApp').classList.add('active');

  const u = state.user;
  const display = u.display_name || u.username;
  $('userName').textContent = display;
  $('userRole').textContent = u.role_name || u.role;
  $('userAvatar').textContent = (display || '?').slice(0, 1);

  // 按角色隐藏 tab
  document.querySelectorAll('#navTabs .tab').forEach((t) => {
    t.setAttribute('role', 'tab');
    const allow = t.dataset.roles;
    if (!allow) {
      t.classList.remove('hidden');
      return;
    }
    t.classList.toggle('hidden', !allow.split(',').includes(u.role));
  });

  // 上传合同按钮只对有权限的角色显示
  $('btnNewContract').style.display = ROLE_CAN_CREATE.includes(u.role) ? '' : 'none';

  const scope = {
    submitter: '你只能看到本部门提交的合同（跨部门隔离在 SQL 层完成）',
    reviewer: '法务审查员可查看全部合同；你可对 AI 意见提交反馈',
    finance: '财务仅可见付款、交付等条款，违约责任与解除条款对你不可见',
    admin: '管理员只看统计、评测、Prompt 与审计；合同正文按职责分离对你不可见',
  };
  $('contractScopeHint').textContent = scope[u.role] || '';

  // 管理员没有合同视图，落地页直接给看板
  switchView(u.role === 'admin' ? 'dashboard' : 'contracts');
}

async function doLogin(ev) {
  ev.preventDefault();
  const btn = $('loginBtn');
  const errEl = $('loginErr');
  errEl.textContent = '';

  const username = $('loginUser').value.trim();
  const password = $('loginPass').value;

  if (!username) {
    errEl.textContent = '请输入用户名';
    $('loginUser').focus();
    return;
  }
  if (!password) {
    errEl.textContent = '请输入密码';
    $('loginPass').focus();
    return;
  }
  if (password.length < 6) {
    errEl.textContent = '密码至少 6 位';
    $('loginPass').focus();
    return;
  }

  btn.disabled = true;
  btn.classList.add('loading');
  btn.textContent = '登录中';
  try {
    const data = await api('/api/auth/login', { method: 'POST', body: { username, password } });
    saveSession(data.access_token, data.user);
    $('loginPass').value = '';
    showApp();
    await loadContracts();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.textContent = '登 录';
  }
}

function doLogout() {
  api('/api/auth/logout', { method: 'POST' }).catch(() => {});
  clearSession();
  state.contracts = [];
  state.contractTotal = 0;
  state.currentDetail = null;
  state.currentContractId = null;
  state.auditFilter = '';
  state.auditPage = 1;
  $('auditAction').innerHTML = '<option value="">全部操作</option>';
  showLogin();
}

// ========================================================== 视图切换
function switchView(name) {
  state.currentView = name;
  document.querySelectorAll('#navTabs .tab').forEach((t) => {
    const active = t.dataset.view === name;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
  });
  const targetId = 'view' + name.charAt(0).toUpperCase() + name.slice(1);
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('active', v.id === targetId);
  });

  if (name === 'dashboard') loadDashboard();
  if (name === 'eval') loadEval();
  if (name === 'audit') loadAudit(1);
  if (name === 'prompts') loadPrompts();
}

// ========================================================== 合同列表
async function loadContracts(keyword = state.contractKeyword, page = 1) {
  state.contractKeyword = keyword || '';
  state.contractPage = page;
  const listEl = $('contractList');
  listEl.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载中…</div>';
  $('contractPager').innerHTML = '';
  try {
    const params = new URLSearchParams({ page: String(page), size: String(CONTRACT_PAGE_SIZE) });
    if (state.contractKeyword) params.set('keyword', state.contractKeyword);
    const data = await api(`/api/contracts?${params.toString()}`);
    state.contracts = data.items || [];
    state.contractTotal = data.total || 0;
    $('contractCount').textContent = `共 ${state.contractTotal} 份合同`;
    renderContractList();
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

function renderContractList() {
  const listEl = $('contractList');
  const total = state.contractTotal;
  const page = state.contractPage;

  if (!state.contracts.length) {
    listEl.innerHTML = state.contractKeyword
      ? `<div class="empty-state"><div class="empty-icon">🔍</div><p>没有匹配「${esc(state.contractKeyword)}」的合同</p><span>换个关键词试试</span></div>`
      : '<div class="empty-state"><div class="empty-icon">📂</div><p>还没有合同</p><span>点右上角「新建合同」开始</span></div>';
    $('contractPager').innerHTML = '';
    return;
  }

  listEl.innerHTML = state.contracts
    .map((c) => {
      const lvl = c.risk_level || 'none';
      const active = c.id === state.currentContractId ? ' active' : '';
      return `
      <button class="contract-item${active}" data-id="${c.id}">
        <div class="ci-top">
          <span class="ci-title">${esc(c.title)}</span>
          <span class="pill pill-${lvl}">${RISK_TEXT[lvl] || lvl}</span>
        </div>
        <div class="ci-meta">
          <span>${esc(c.contract_type)}</span>
          <span class="dot">·</span>
          <span>${fmtNum(c.char_count)} 字</span>
          <span class="dot">·</span>
          <span>${c.finding_count} 条意见</span>
          <span class="dot">·</span>
          <span>${fmtTime(c.created_at)}</span>
        </div>
      </button>`;
    })
    .join('');

  listEl.querySelectorAll('.contract-item').forEach((btn) => {
    btn.addEventListener('click', () => openContract(Number(btn.dataset.id)));
  });

  // 分页：独立容器，每次重绘覆盖而不是追加
  const totalPages = Math.ceil(total / CONTRACT_PAGE_SIZE);
  const pager = $('contractPager');
  if (totalPages > 1) {
    pager.innerHTML = `
      <div class="pagination">
        <button class="btn btn-ghost btn-sm" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
        <span>第 ${page} / ${totalPages} 页 · 共 ${total} 条</span>
        <button class="btn btn-ghost btn-sm" data-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
      </div>`;
    pager.querySelectorAll('[data-page]').forEach((b) => {
      b.addEventListener('click', () => loadContracts(state.contractKeyword, Number(b.dataset.page)));
    });
  } else {
    pager.innerHTML = '';
  }
}

async function openContract(id) {
  state.currentContractId = id;
  renderContractList();
  const el = $('contractDetail');
  el.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载合同…</div>';
  try {
    const d = await api(`/api/contracts/${id}`);
    state.currentDetail = d;
    renderContractDetail(d);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

function renderContractDetail(d) {
  const r = d.latest_review;
  const canReview = ROLE_CAN_REVIEW.includes(state.user.role);
  const canFeedback = ROLE_CAN_FEEDBACK.includes(state.user.role);

  // 条款级反馈按条款号归组，同一个条款以最后一次为准
  const fbByClause = {};
  ((r && r.reviewer_edits && r.reviewer_edits.clause_feedbacks) || []).forEach((fb) => {
    if (fb && fb.clause_no != null) fbByClause[fb.clause_no] = fb;
  });
  const FB_STATE = {
    accepted: ['已采纳', 'pill-ok'],
    edited: ['改后采纳', 'pill-medium'],
    rejected: ['已驳回', 'pill-high'],
  };

  // 意见按条款号归组，渲染时贴在对应条款下面
  const byClause = {};
  if (r && r.findings) {
    r.findings.forEach((f) => {
      (byClause[f.clause_no] = byClause[f.clause_no] || []).push(f);
    });
  }

  const banner = r
    ? `<div class="review-banner${r.degraded ? ' warn' : ''}">
         <b>AI 审查结论：</b>${esc(r.summary || '（无摘要）')}
         ${r.degraded ? `<br><span class="pill pill-degraded">降级模式</span> ${esc(r.degrade_reason)}` : ''}
         <div class="detail-meta">
           <span>模型 ${esc(r.model_name)}</span>
           <span class="dot">·</span>
           <span>${fmtNum(r.total_tokens)} tokens</span>
           <span class="dot">·</span>
           <span>${r.latency_ms} ms</span>
           <span class="dot">·</span>
           <span>${fmtTime(r.created_at)}</span>
         </div>
       </div>`
    : canReview
    ? `<div class="review-banner">这份合同还没有审查过。点右侧「开始审查」，AI 会逐条读条款并给出风险意见。</div>`
    : `<div class="review-banner">这份合同还没有审查过。审查由法务发起，结果出来后这里会显示 AI 意见。</div>`;

  const clausesHtml = (d.clauses || [])
    .map((c) => {
      const fs = byClause[c.order_no] || [];
      const worst = fs.some((f) => f.level === 'high') ? ' has-high' : fs.length ? ' has-issue' : '';
      const findingsHtml = fs
        .map(
          (f) => `
        <div class="finding">
          <div class="finding-top">
            <span class="pill pill-${f.level}">${LEVEL_TEXT[f.level] || f.level}</span>
            <span class="pill pill-none">${CATEGORY_TEXT[f.category] || f.category}</span>
            <span class="conf">置信度 ${(Number(f.confidence || 0) * 100).toFixed(0)}%</span>
          </div>
          <div class="finding-issue">${esc(f.issue)}</div>
          ${f.suggestion ? `<div class="finding-sug"><b>建议：</b>${esc(f.suggestion)}</div>` : ''}
        </div>`
        )
        .join('');

      const clauseFeedback =
        r && canFeedback && fs.length
          ? `<div class="clause-feedback" data-order-no="${c.order_no}">
               <span class="fb-label">该条款意见：</span>
               <button class="btn btn-ghost btn-xs" data-fb-clause="accepted">✓ 采纳</button>
               <button class="btn btn-ghost btn-xs" data-fb-clause="edited">✎ 改后采纳</button>
               <button class="btn btn-ghost btn-xs" data-fb-clause="rejected">✕ 驳回</button>
             </div>`
          : '';

      return `
      <div class="clause${worst}" data-clause-id="${c.id}" data-order-no="${c.order_no}">
        <div class="clause-head">
          <span class="clause-no">第 ${c.order_no} 条</span>
          <span class="clause-heading">${esc(clauseHeading(c.heading) || '（无标题）')}</span>
          <span class="pill pill-none">${CATEGORY_TEXT[c.category] || c.category}</span>
          ${fbByClause[c.order_no]
            ? `<span class="pill ${(FB_STATE[fbByClause[c.order_no].action] || ['', 'pill-none'])[1]}" title="该条款的反馈已记录">${(FB_STATE[fbByClause[c.order_no].action] || [fbByClause[c.order_no].action, ''])[0]}</span>`
            : ''}
          <button class="copy-clause" title="复制条款内容" data-clause-id="${c.id}">复制</button>
        </div>
        <div class="clause-content">${esc(c.content)}</div>
        ${findingsHtml}
        ${clauseFeedback}
      </div>`;
    })
    .join('');

  // 条款被权限过滤掉时，要明确告知，否则用户以为 AI 漏了
  const hiddenCount = (d.clause_total || 0) - (d.clauses || []).length;
  const hiddenNote =
    hiddenCount > 0
      ? `<p class="perm-note hint">有 ${hiddenCount} 条条款因权限设置对你不可见（字段级权限，后端根本不会返回）。</p>`
      : '';

  let feedbackHtml = '';
  if (r && canFeedback) {
    if (r.reviewer_action) {
      const act = { accepted: '已采纳', edited: '修改后采纳', rejected: '已驳回' }[r.reviewer_action] || r.reviewer_action;
      feedbackHtml = `<div class="feedback-bar">
        <span class="fb-done">✓ 你的反馈：${act}${r.reviewer_note ? `（${esc(r.reviewer_note)}）` : ''}</span>
      </div>`;
    } else {
      feedbackHtml = `<div class="feedback-bar">
        <span class="fb-label">对这次 AI 意见的整体评价：</span>
        <button class="btn btn-ghost btn-sm" data-fb="accepted">✓ 采纳</button>
        <button class="btn btn-ghost btn-sm" data-fb="edited">✎ 改后采纳</button>
        <button class="btn btn-ghost btn-sm" data-fb="rejected">✕ 驳回</button>
      </div>`;
    }
  }

  $('contractDetail').innerHTML = `
    <div class="detail-head">
      <div>
        <div class="detail-title">${esc(d.title)}</div>
        <div class="detail-meta">
          <span>${esc(d.contract_type)}</span>
          <span class="dot">·</span>
          <span>${esc(d.original_filename)}</span>
          <span class="dot">·</span>
          <span>${fmtNum(d.char_count)} 字 / ${(d.clauses || []).length} 条条款</span>
          <span class="dot">·</span>
          <span>${esc(d.submitter_name)}</span>
          <span class="dot">·</span>
          <span>${fmtTime(d.created_at)}</span>
        </div>
      </div>
      <div class="detail-actions">
        ${r ? `<span class="pill pill-${r.risk_level}">${RISK_TEXT[r.risk_level] || r.risk_level}</span>` : ''}
        ${canReview ? `<button class="btn btn-primary btn-sm" id="btnReview">${r ? '重新审查' : '开始审查'}</button>` : ''}
      </div>
    </div>
    ${banner}
    <div class="detail-body">
      <div class="section-title">条款（${(d.clauses || []).length} 条）</div>
      ${hiddenNote}
      ${clausesHtml || '<div class="empty-state"><p>没有可见条款</p></div>'}
      ${feedbackHtml}
    </div>
  `;

  $('btnReview')?.addEventListener('click', () => runReview(d.id));

  document.querySelectorAll('[data-fb]').forEach((b) => {
    b.addEventListener('click', () => submitFeedback(d.id, r.id, b.dataset.fb));
  });

  // 复制条款内容
  document.querySelectorAll('.copy-clause').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const clauseEl = btn.closest('.clause');
      const text = clauseEl?.querySelector('.clause-content')?.textContent?.trim() || '';
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = '已复制';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = '复制';
          btn.classList.remove('copied');
        }, 1200);
      }).catch(() => toast('复制失败，请手动选择文本', 'bad'));
    });
  });

  // 条款级反馈（走专用接口）
  document.querySelectorAll('.clause-feedback').forEach((box) => {
    const orderNo = Number(box.dataset.orderNo);
    box.querySelectorAll('[data-fb-clause]').forEach((b) => {
      b.addEventListener('click', () => submitClauseFeedback(d.id, r.id, orderNo, b.dataset.fbClause));
    });
  });
}

async function runReview(contractId) {
  const btn = $('btnReview');
  if (btn) {
    btn.disabled = true;
    btn.classList.add('loading');
    btn.textContent = '审查中';
  }
  const detail = $('contractDetail');
  const banner = detail.querySelector('.review-banner');
  if (banner) {
    banner.classList.remove('warn');
    banner.innerHTML = '<b>AI 正在逐条阅读合同…</b> 通常需要 5–9 秒，模型较慢时会自动降级到规则引擎。';
  }
  try {
    await api(`/api/contracts/${contractId}/review`, { method: 'POST' });
    toast('审查完成', 'ok');
    await openContract(contractId);
    await loadContracts(state.contractKeyword, state.contractPage);
  } catch (e) {
    toast(e.message, 'bad');
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('loading');
      btn.textContent = '重新审查';
    }
    await openContract(contractId);
  }
}

async function submitFeedback(contractId, reviewId, action) {
  let note = '';
  if (action === 'rejected') {
    const ans = prompt('驳回理由（会进 badcase 队列，用于后续改进）：');
    if (ans === null) return;
    note = ans;
  }
  try {
    await api(`/api/contracts/${contractId}/review/${reviewId}/feedback`, {
      method: 'POST',
      body: { action, note },
    });
    toast('反馈已记录', 'ok');
    await openContract(contractId);
  } catch (e) {
    toast(e.message, 'bad');
  }
}

async function submitClauseFeedback(contractId, reviewId, clauseNo, action) {
  let note = '';
  if (action === 'rejected') {
    const ans = prompt(`第 ${clauseNo} 条驳回理由：`);
    if (ans === null) return;
    note = ans;
  }
  try {
    await api(`/api/contracts/${contractId}/review/${reviewId}/clause-feedback`, {
      method: 'POST',
      body: { clause_no: clauseNo, action, note },
    });
    toast(`第 ${clauseNo} 条反馈已记录`, 'ok');
    await openContract(contractId);
  } catch (e) {
    toast(e.message, 'bad');
  }
}

// ========================================================== 新建合同
let modalMode = 'file';
let textValidationTimer = null;
let parserValidationTimer = null;
let lastValidatedText = '';
let lastFocusedEl = null;

function openModal() {
  lastFocusedEl = document.activeElement;
  $('modalMask').classList.add('show');
  $('newErr').textContent = '';
  $('newTitle').value = '';
  $('textContent').value = '';
  $('fileInput').value = '';
  $('textCounter').textContent = '0 字符';
  $('textCounter').className = 'counter';
  $('textClauseList').innerHTML = '';
  $('textClauseList').classList.remove('show');
  $('textQuality').innerHTML = '';
  lastValidatedText = ''; // 否则重新打开后输入同样的文本不会触发解析预览
  $('panelFile').querySelector('.dropzone')?.classList.remove('has-file');
  switchModalMode('file');
  setTimeout(() => $('newTitle').focus(), 60);
}

function closeModal() {
  $('modalMask').classList.remove('show');
  clearTimeout(textValidationTimer);
  clearTimeout(parserValidationTimer);
  // 焦点归还给打开弹窗的按钮，键盘用户不会「丢位置」
  if (lastFocusedEl && typeof lastFocusedEl.focus === 'function') {
    lastFocusedEl.focus();
  }
  lastFocusedEl = null;
}

// 弹窗内的焦点陷阱：Tab / Shift+Tab 不能跑到背景页面上
function trapModalFocus(e) {
  if (e.key !== 'Tab' || !$('modalMask').classList.contains('show')) return;
  const nodes = $('modalMask').querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  );
  const focusable = [...nodes].filter((el) => !el.disabled && el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function switchModalMode(mode) {
  modalMode = mode;
  document.querySelectorAll('.mtab').forEach((x) => x.classList.toggle('active', x.dataset.mode === mode));
  $('panelFile').classList.toggle('active', mode === 'file');
  $('panelText').classList.toggle('active', mode === 'text');
}

function updateTextValidation(text) {
  const counterEl = $('textCounter');
  const previewEl = $('textClauseList');
  const qualityEl = $('textQuality');
  if (!counterEl) return;

  counterEl.textContent = `${text.length} 字符`;
  counterEl.className = 'counter' + (text.length > 60000 ? ' warn' : text.trim() ? ' ok' : '');

  clearTimeout(textValidationTimer);
  textValidationTimer = setTimeout(() => {
    if (!text.trim()) {
      counterEl.className = 'counter';
      previewEl.innerHTML = '';
      previewEl.classList.remove('show');
      qualityEl.innerHTML = '';
      return;
    }
    const lines = text.split('\n').filter((l) => l.trim());
    const clauseLines = lines.filter((l) => /^第[一二三四五六七八九十百零〇\d]+\s*条\s*/.test(l.trim()));
    if (!clauseLines.length) {
      counterEl.className = 'counter warn';
      qualityEl.innerHTML = '<div class="quality-box"><span class="q-issue">⚠ 未识别到条款分段，请按「第X条 标题」的格式书写，例如：第一条 付款方式</span></div>';
      previewEl.innerHTML = '';
      previewEl.classList.remove('show');
    } else {
      counterEl.className = 'counter ok';
    }
  }, 300);

  // 后端实时解析验证（防抖 800ms，避免过度请求）
  clearTimeout(parserValidationTimer);
  if (text.trim() && text !== lastValidatedText) {
    parserValidationTimer = setTimeout(async () => {
      if (!text.trim()) return;
      lastValidatedText = text;
      try {
        const data = await api('/api/parser/validate', {
          method: 'POST',
          body: { content: text },
          timeout: 30000,
        });
        renderParserValidation(data);
      } catch (e) {
        console.warn('Parser validation failed:', e.message);
      }
    }, 800);
  }
}

function renderParserValidation(data) {
  const previewEl = $('textClauseList');
  const qualityEl = $('textQuality');
  if (!previewEl) return;

  const clauses = data.clauses || [];
  previewEl.innerHTML = clauses.length
    ? `<div class="parser-box">${clauses
        .map(
          (c) => `
        <div class="parser-clause">
          <div class="pc-top">
            <span class="pill pill-none">${CATEGORY_TEXT[c.category] || c.category}</span>
            <span class="mono">第 ${c.order_no} 条</span>
            <span class="pc-head">${esc(clauseHeading(c.heading) || '（无标题）').slice(0, 30)}</span>
          </div>
          <div class="pc-body">${esc(c.content).slice(0, 80)}</div>
        </div>`
        )
        .join('')}</div>`
    : '';
  previewEl.classList.toggle('show', clauses.length > 0);

  // 质量自检与错误提示：无论是否解析出条款都要展示
  let html = '';
  if (data.quality) {
    const q = data.quality;
    html += `
      <div class="quality-box">
        <div class="q-row">
          <span>字符数：<b>${q.char_count || 0}</b></span>
          <span>条款数：<b>${q.clause_count || 0}</b></span>
          <span>标题命中：<b>${q.heading_hits || 0}</b></span>
        </div>
        ${q.issues && q.issues.length ? `<div class="q-issue">${q.issues.map((i) => `⚠ ${esc(i)}`).join('；')}</div>` : ''}
      </div>`;
  }
  if (data.error) {
    html += `<div class="quality-box"><span class="q-issue">⚠ ${esc(data.error)}</span></div>`;
  }
  qualityEl.innerHTML = html;
}

async function submitNewContract() {
  const btn = $('modalSubmit');
  const errEl = $('newErr');
  errEl.textContent = '';

  const title = $('newTitle').value.trim();
  const ctype = $('newType').value;

  if (!title) {
    errEl.textContent = '请填写合同标题';
    $('newTitle').focus();
    return;
  }

  btn.disabled = true;
  btn.classList.add('loading');
  btn.textContent = '创建中';
  try {
    let created;
    if (modalMode === 'file') {
      const f = $('fileInput').files[0];
      if (!f) throw new Error('请选择要上传的合同文件');
      if (f.size > 20 * 1024 * 1024) throw new Error('文件超过 20MB 上限');
      const fd = new FormData();
      fd.append('file', f);
      fd.append('title', title);
      fd.append('contract_type', ctype);
      created = await api('/api/contracts/upload', { method: 'POST', body: fd, isForm: true });
    } else {
      const content = $('textContent').value.trim();
      if (!content) throw new Error('请粘贴合同正文');
      created = await api('/api/contracts/text', {
        method: 'POST',
        body: { title, contract_type: ctype, content },
      });
    }
    closeModal();
    toast(created.duplicate ? '已存在内容相同的合同，已为你打开原合同' : '合同已创建', 'ok');
    await loadContracts(state.contractKeyword, 1);
    await openContract(created.id);
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.textContent = '创建';
  }
}

// ========================================================== 看板
const STAT_ICONS = {
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  thumb: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 22V11l5-9a2 2 0 0 1 3 2l-1 5h5a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 17.6 22z"/><path d="M7 22H3V11h4"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  coins: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
};

async function loadDashboard() {
  const el = $('dashBody');
  el.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载中…</div>';
  try {
    const d = await api('/api/dashboard');

    const stat = (label, value, sub = '', cls = '', icon = 'doc') =>
      `<div class="stat-card">
         <div class="stat-top">
           <span class="stat-label">${label}</span>
           <span class="stat-ico">${STAT_ICONS[icon] || ''}</span>
         </div>
         <div class="stat-value ${cls}">${value}</div>
         ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
       </div>`;

    const bars = (obj, colorMap = {}) => {
      const entries = Object.entries(obj || {});
      if (!entries.length) return '<p class="hint">暂无数据</p>';
      const max = Math.max(...entries.map(([, v]) => v)) || 1;
      return entries
        .map(([k, v]) => {
          const cls = colorMap[k] || '';
          return `<div class="bar-row">
            <span class="bar-label">${esc(RISK_TEXT[k] || CATEGORY_TEXT[k] || k)}</span>
            <div class="bar-track"><div class="bar-fill ${cls}" style="width:${(v / max) * 100}%"></div></div>
            <span class="bar-num">${v}</span>
          </div>`;
        })
        .join('');
    };

    const recentRows = (d.recent || [])
      .map(
        (x) => `<tr>
        <td>${esc(x.title)}</td>
        <td><span class="pill pill-${x.risk_level}">${RISK_TEXT[x.risk_level] || x.risk_level}</span></td>
        <td class="num">${x.findings}</td>
        <td>${x.degraded ? '<span class="pill pill-degraded">降级</span>' : '<span class="pill pill-ok">正常</span>'}</td>
        <td class="num">${x.latency_ms}ms</td>
        <td class="mono">${fmtTime(x.created_at)}</td>
      </tr>`
      )
      .join('');

    el.innerHTML = `
      <div class="stat-grid">
        ${stat('合同总数', fmtNum(d.total_contracts), '按权限范围统计', '', 'doc')}
        ${stat('已审查', fmtNum(d.reviewed_contracts), `待审查 ${d.pending_contracts} 份`, 'accent', 'check')}
        ${stat('风险意见总数', fmtNum(d.total_findings), `高风险合同 ${d.high_risk_contracts} 份`, 'high', 'alert')}
        ${stat('降级审查', fmtNum(d.degraded_reviews), '模型不可用时走规则引擎', d.degraded_reviews ? 'medium' : '', 'shield')}
        ${stat('意见采纳率', fmtPct(d.review_accept_rate), '法务采纳 / 已反馈', 'low', 'thumb')}
        ${stat('平均耗时', `${d.avg_latency_ms}ms`, '单份合同端到端', '', 'clock')}
        ${stat('累计 token', fmtNum(d.total_tokens), `约 ¥${Number(d.total_cost_cny || 0).toFixed(4)}`, '', 'coins')}
      </div>

      <div class="panel-grid">
        <div class="card panel">
          <div class="panel-title">风险等级分布</div>
          ${bars(d.by_risk, { high: 'high', medium: 'medium', low: 'low', none: '' })}
        </div>
        <div class="card panel">
          <div class="panel-title">问题类别分布</div>
          ${bars(d.by_category)}
        </div>
      </div>

      <div class="card panel" style="margin-top:16px">
        <div class="panel-title">最近审查记录</div>
        ${
          recentRows
            ? `<div class="table-wrap"><table class="data">
                 <thead><tr><th>合同</th><th>风险等级</th><th>意见数</th><th>状态</th><th>耗时</th><th>时间</th></tr></thead>
                 <tbody>${recentRows}</tbody>
               </table></div>`
            : '<p class="hint">还没有审查记录</p>'
        }
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

// ========================================================== 评测
let evalPollTimer = null;

function scheduleEvalPoll() {
  clearTimeout(evalPollTimer);
  evalPollTimer = setTimeout(() => {
    // 用户已经切走就不用再刷新了，回来时会重新加载
    if (state.currentView === 'eval') loadEval();
  }, 3000);
}

async function loadEval() {
  clearTimeout(evalPollTimer);
  const el = $('evalBody');
  el.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载中…</div>';
  try {
    const [runs, prompts] = await Promise.all([
      api('/api/eval/runs'),
      api('/api/prompts').catch(() => []),
    ]);
    state.evalRuns = runs;
    state.promptVersions = prompts;

    // 下拉：prompt 版本
    const sel = $('evalPrompt');
    sel.innerHTML =
      prompts
        .map((p) => `<option value="${p.id}">v${p.version}${p.is_active ? '（当前生效）' : ''}</option>`)
        .join('') || '<option value="">无版本</option>';

    if (!runs.length) {
      el.innerHTML = `<div class="empty-state">
        <div class="empty-icon">🧪</div>
        <p>还没有跑分记录</p>
        <span>点「跑一次评测」用 20 份合成合同测当前 prompt 的召回率</span>
      </div>`;
      return;
    }

    // 后台任务可能正在跑：新记录状态为 running / failed，其余都算 done
    const statusOf = (r) => (r.detail || {}).status || 'done';
    const running = runs.find((r) => statusOf(r) === 'running');
    const doneRuns = runs.filter((r) => statusOf(r) === 'done');
    const latest = doneRuns[0];
    const prev = doneRuns[1];

    let statusHtml = '';
    if (running) {
      statusHtml = `<div class="compare-note">
        <span class="spinner"></span>
        <b>评测正在后台运行</b>（#${running.id}，${fmtTime((running.detail || {}).started_at)} 开始）。
        页面会自动刷新；也可以先去处理别的事，结果不会丢。
      </div>`;
      scheduleEvalPoll();
    } else if (statusOf(runs[0]) === 'failed') {
      statusHtml = `<div class="compare-note" style="border-left-color:var(--high);background:var(--high-dim)">
        <b>最近一次评测失败：</b>${esc((runs[0].detail || {}).error || '未知错误')}
      </div>`;
    }

    const metric = (name, val, desc, pct, color) => `
      <div class="metric">
        <div class="metric-name">${name}</div>
        <div class="metric-val" style="color:${color}">${val}</div>
        <div class="metric-desc">${desc}</div>
        <div class="metric-bar"><i style="width:${Math.max(0, Math.min(100, pct))}%;background:${color}"></i></div>
      </div>`;

    let compareHtml = '';
    if (latest && prev) {
      const dRecall = (latest.recall - prev.recall) * 100;
      const dF1 = (latest.f1 - prev.f1) * 100;
      const arrow = dRecall > 0 ? '↑' : dRecall < 0 ? '↓' : '→';
      const color = dRecall > 0 ? 'var(--low)' : dRecall < 0 ? 'var(--high)' : 'var(--text-3)';
      compareHtml = `<div class="compare-note">
        <b>与上一次跑分对比</b>（${esc(prev.label)} → ${esc(latest.label)}）：
        召回率 ${fmtPct(prev.recall)} → ${fmtPct(latest.recall)}
        <span style="color:${color}">${arrow} ${Math.abs(dRecall).toFixed(1)} 个点</span>；
        F1 ${fmtPct(prev.f1)} → ${fmtPct(latest.f1)}
        <span style="color:${color}">${dF1 > 0 ? '↑' : dF1 < 0 ? '↓' : '→'} ${Math.abs(dF1).toFixed(1)}</span>。
        这一屏就是「改 prompt 有没有用」的证据。
      </div>`;
    }

    const runRows = runs
      .map((r) => {
        const st = statusOf(r);
        const badge =
          st === 'running'
            ? ' <span class="pill pill-degraded">运行中</span>'
            : st === 'failed'
            ? ' <span class="pill pill-high">失败</span>'
            : '';
        return `<tr>
        <td class="num">#${r.id}</td>
        <td>${esc(r.label)}${badge}</td>
        <td class="mono">${esc(r.model_name)}</td>
        <td class="num">${st === 'done' ? `${r.caught_total}/${r.planted_total}` : '—'}</td>
        <td class="num" style="color:var(--low)">${st === 'done' ? fmtPct(r.recall) : '—'}</td>
        <td class="num" style="color:var(--medium)">${st === 'done' ? fmtPct(r.false_positive_rate) : '—'}</td>
        <td class="num">${st === 'done' ? fmtPct(r.f1) : '—'}</td>
        <td class="num">${st === 'done' ? `${r.avg_latency_ms}ms` : '—'}</td>
        <td class="mono">${fmtTime(r.created_at)}</td>
      </tr>`;
      })
      .join('');

    // 逐用例明细（只展示最近一次完成的跑分）
    const detail = (latest && latest.detail) || {};
    const caseRows = (detail.cases || [])
      .map((c) => {
        const ok = c.caught >= c.planted;
        return `<tr>
        <td class="mono">${esc(c.case_id)}</td>
        <td>${esc(c.title)}</td>
        <td class="num">${c.caught}/${c.planted}</td>
        <td class="num">${c.findings}</td>
        <td class="num">${c.false_positives}</td>
        <td>${ok ? '<span class="pill pill-ok">全抓到</span>' : `<span class="pill pill-high">漏 ${c.missed.length}</span>`}</td>
        <td class="mono" style="font-size:11px">${esc((c.missed || []).join(', ')) || '—'}</td>
      </tr>`;
      })
      .join('');

    const metricsHtml = latest
      ? `<div class="metric-row">
        ${metric('召回率 Recall', fmtPct(latest.recall), `抓到 ${latest.caught_total} / 埋雷 ${latest.planted_total}`, latest.recall * 100, 'var(--low)')}
        ${metric('误报率 FPR', fmtPct(latest.false_positive_rate), `误报 ${latest.false_positive_total} 条`, latest.false_positive_rate * 100, 'var(--medium)')}
        ${metric('F1 分数', fmtPct(latest.f1), '召回与精确的调和平均', latest.f1 * 100, 'var(--accent-2)')}
        ${metric('平均耗时', `${latest.avg_latency_ms}ms`, `${latest.total_cases} 份合同`, Math.min(100, latest.avg_latency_ms / 100), 'var(--text-2)')}
      </div>
      ${compareHtml}`
      : '';

    const detailHtml = latest
      ? `<div class="card panel" style="margin-top:16px">
        <div class="panel-title">最新一次逐用例明细（${esc(latest.label)}）</div>
        <p class="hint" style="margin-bottom:10px">
          每份合同都预埋了 2–5 个已知风险点。「抓到」= 模型报出了同一个条款号、且类别或关键词匹配。
          漏报的条款号列在最后一列，可以直接拿去改 prompt。
        </p>
        <div class="table-wrap"><table class="data">
          <thead><tr>
            <th>用例</th><th>标题</th><th>抓到/埋雷</th><th>报出</th><th>误报</th><th>结果</th><th>漏报条款</th>
          </tr></thead>
          <tbody>${caseRows || '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-3)">没有明细</td></tr>'}</tbody>
        </table></div>
      </div>`
      : '';

    el.innerHTML = `
      ${statusHtml}
      ${metricsHtml}
      <div class="card panel" style="margin-top:${statusHtml || metricsHtml ? '16px' : '0'}">
        <div class="panel-title">
          历史跑分（${runs.length} 次）
          <button class="btn btn-ghost btn-sm" id="btnExportEval" style="margin-left:auto">📥 导出 CSV</button>
        </div>
        <div class="table-wrap"><table class="data">
          <thead><tr>
            <th>#</th><th>标签</th><th>模型</th><th>抓到/埋雷</th>
            <th>召回率</th><th>误报率</th><th>F1</th><th>平均耗时</th><th>时间</th>
          </tr></thead>
          <tbody>${runRows}</tbody>
        </table></div>
      </div>
      ${detailHtml}
    `;

    $('btnExportEval')?.addEventListener('click', () => exportEvalCSV(runs, latest));
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

function exportEvalCSV(runs, latest) {
  if (!latest) {
    toast('还没有已完成的评测结果', 'bad');
    return;
  }
  const summaryHeaders = ['#', '标签', '模型', '抓到/埋雷', '召回率', '误报率', 'F1', '平均耗时(ms)', '时间'];
  const summaryRows = runs.map((r) => [
    r.id,
    r.label,
    r.model_name,
    `${r.caught_total}/${r.planted_total}`,
    (r.recall * 100).toFixed(1) + '%',
    (r.false_positive_rate * 100).toFixed(1) + '%',
    (r.f1 * 100).toFixed(1) + '%',
    r.avg_latency_ms,
    fmtTime(r.created_at),
  ]);

  const detailHeaders = ['用例', '标题', '抓到/埋雷', '报出', '误报', '结果', '漏报条款'];
  const detail = latest.detail || {};
  const detailRows = (detail.cases || []).map((c) => [
    c.case_id,
    c.title,
    `${c.caught}/${c.planted}`,
    c.findings,
    c.false_positives,
    c.caught >= c.planted ? '全抓到' : `漏 ${c.missed.length}`,
    (c.missed || []).join(', ') || '—',
  ]);

  const csv = [
    '=== 历史跑分汇总 ===',
    summaryHeaders.map((h) => `"${h}"`).join(','),
    ...summaryRows.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(',')),
    '',
    '=== 最新一次逐用例明细 ===',
    detailHeaders.map((h) => `"${h}"`).join(','),
    ...detailRows.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(',')),
  ].join('\n');

  downloadBlob(csv, `eval-results-${new Date().toISOString().slice(0, 10)}.csv`, 'text/csv');
  toast('评测结果已导出', 'ok');
}

async function runEvalAction(rulesOnly) {
  const btn = rulesOnly ? $('btnEvalRules') : $('btnEvalRun');
  const label = rulesOnly ? '规则引擎基线' : '手动跑分';
  const pv = $('evalPrompt').value;

  btn.disabled = true;
  btn.classList.add('loading');
  const orig = btn.textContent;
  btn.textContent = '跑分中';

  $('evalBody').innerHTML = `<div class="loading-inline">
    <span class="spinner"></span>
    正在提交评测任务…
  </div>`;

  try {
    const params = new URLSearchParams({ label });
    if (rulesOnly) params.set('rules_only', 'true');
    else if (pv) params.set('prompt_version_id', pv);
    // 后端已改为后台任务：提交立即返回，进度靠 /api/eval/runs 轮询
    await api(`/api/eval/run?${params.toString()}`, { method: 'POST', timeout: 30000 });
    toast('评测已提交，正在后台运行', 'ok');
    await loadEval();
  } catch (e) {
    toast(e.message, 'bad');
    $('evalBody').innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.textContent = orig;
  }
}

// ========================================================== 审计
async function loadAudit(page = 1) {
  state.auditPage = page;
  const el = $('auditBody');
  el.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载中…</div>';
  try {
    const params = new URLSearchParams({ page: String(page), size: String(AUDIT_PAGE_SIZE) });
    if (state.auditFilter) params.set('action', state.auditFilter);

    const [actions, rows] = await Promise.all([
      api('/api/audit/actions'),
      api(`/api/audit?${params.toString()}`),
    ]);

    const sel = $('auditAction');
    if (sel.options.length <= 1) {
      sel.innerHTML =
        '<option value="">全部操作</option>' +
        actions.map((a) => `<option value="${a.action}">${ACTION_TEXT[a.action] || a.action}（${a.count}）</option>`).join('');
    }
    sel.value = state.auditFilter || '';

    const body = rows.items
      .map(
        (a) => `<tr>
        <td class="mono">${fmtTime(a.created_at)}</td>
        <td>${a.username ? esc(a.username) : '<span style="color:var(--text-3)">（未登录）</span>'}</td>
        <td>${ACTION_TEXT[a.action] || esc(a.action)}</td>
        <td class="mono" style="font-size:11px">${esc(a.action)}</td>
        <td>${esc(a.target_type)}${a.target_id ? ` #${a.target_id}` : ''}</td>
        <td>${a.success ? '<span class="pill pill-ok">成功</span>' : '<span class="pill pill-high">失败</span>'}</td>
        <td class="mono">${esc(a.ip_address)}</td>
      </tr>`
      )
      .join('');

    const totalPages = Math.ceil(rows.total / AUDIT_PAGE_SIZE);

    el.innerHTML = `
      <div class="card panel">
        <div class="panel-title">
          共 ${rows.total} 条（按时间倒序）
          <button class="btn btn-ghost btn-sm" id="btnExportAudit" style="margin-left:auto">📥 导出 CSV</button>
        </div>
        <p class="hint" style="margin-bottom:10px">
          审计日志只增不改：后端没有任何 UPDATE / DELETE 接口。这是合规审计的前提。
        </p>
        <div class="table-wrap"><table class="data">
          <thead><tr>
            <th>时间</th><th>操作人</th><th>操作</th><th>动作码</th><th>对象</th><th>结果</th><th>IP</th>
          </tr></thead>
          <tbody>${body || '<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--text-3)">暂无记录</td></tr>'}</tbody>
        </table></div>
        ${
          totalPages > 1
            ? `<div class="pagination">
                 <button class="btn btn-ghost btn-sm" data-audit-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
                 <span>第 ${page} / ${totalPages} 页</span>
                 <button class="btn btn-ghost btn-sm" data-audit-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
               </div>`
            : ''
        }
      </div>
    `;

    $('btnExportAudit')?.addEventListener('click', exportAuditCSV);
    el.querySelectorAll('[data-audit-page]').forEach((b) => {
      b.addEventListener('click', () => loadAudit(Number(b.dataset.auditPage)));
    });
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

async function exportAuditCSV() {
  try {
    const params = new URLSearchParams();
    if (state.auditFilter) params.set('action', state.auditFilter);
    const resp = await fetch(`${API_BASE}/api/audit/export?${params.toString()}`, {
      headers: state.token ? { Authorization: `Bearer ${state.token}` } : {},
    });
    if (!resp.ok) throw new Error(`导出失败（HTTP ${resp.status}）`);
    const blob = await resp.blob();
    downloadBlob(blob, `audit-log-${new Date().toISOString().slice(0, 10)}.csv`, 'text/csv');
    toast('审计日志已导出', 'ok');
  } catch (e) {
    toast(e.message, 'bad');
  }
}

// ========================================================== Prompt 版本
async function loadPrompts() {
  const el = $('promptBody');
  el.innerHTML = '<div class="loading-inline"><span class="spinner"></span>加载中…</div>';
  try {
    const list = await api('/api/prompts');
    state.promptVersions = list;
    const canActivate = state.user.role === 'admin';

    const cards = await Promise.all(
      list.map(async (p) => {
        let detail = null;
        try {
          detail = await api(`/api/prompts/${p.id}`);
        } catch {
          /* 忽略 */
        }
        return `
        <div class="card panel" style="margin-bottom:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">
            <div style="display:flex;align-items:center;gap:10px">
              <span class="metric-val" style="font-size:19px;margin:0">v${p.version}</span>
              ${p.is_active ? '<span class="pill pill-ok">当前生效</span>' : '<span class="pill pill-none">未启用</span>'}
              <span class="hint" style="margin:0">${fmtTime(p.created_at)}</span>
            </div>
            ${canActivate && !p.is_active ? `<button class="btn btn-primary btn-sm" data-activate="${p.id}">启用此版本</button>` : ''}
          </div>
          <p style="font-size:12.5px;color:var(--text-2);line-height:1.7;margin-bottom:12px">${esc(p.note)}</p>
          ${
            detail
              ? `<details>
                   <summary style="cursor:pointer;font-size:12px;color:var(--accent-2);margin-bottom:8px">查看完整 prompt 内容</summary>
                   <div class="section-title">System Prompt</div>
                   <pre style="background:var(--bg-3);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:11.5px;line-height:1.65;overflow-x:auto;white-space:pre-wrap;font-family:var(--mono);color:var(--text-2)">${esc(detail.system_prompt)}</pre>
                   <div class="section-title">User Template</div>
                   <pre style="background:var(--bg-3);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:11.5px;line-height:1.65;overflow-x:auto;white-space:pre-wrap;font-family:var(--mono);color:var(--text-2)">${esc(detail.user_template)}</pre>
                 </details>`
              : ''
          }
        </div>`;
      })
    );

    el.innerHTML = `
      <div class="compare-note">
        <b>为什么 prompt 要版本化：</b>
        prompt 改动对效果的影响往往比换模型还大，但它天然不像代码那样有 diff。
        把它存进数据库、带版本号和生效时间，才能做到「改坏了五分钟内回滚」，
        也才能用同一套评测集量化「v1 → v2 到底提升了多少」。
      </div>
      ${cards.join('')}
    `;

    el.querySelectorAll('[data-activate]').forEach((b) => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          await api(`/api/prompts/${b.dataset.activate}/activate`, { method: 'POST' });
          toast('已切换生效版本', 'ok');
          await loadPrompts();
        } catch (e) {
          toast(e.message, 'bad');
          b.disabled = false;
        }
      });
    });
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(e.message)}</p></div>`;
  }
}

// ========================================================== 绑定事件
function bindEvents() {
  // 演示账号一键填充
  document.querySelectorAll('.demo-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      $('loginUser').value = chip.dataset.u;
      $('loginPass').value = 'demo123456';
      $('loginErr').textContent = '';
      $('loginPass').focus();
    });
  });

  $('loginForm').addEventListener('submit', doLogin);
  $('logoutBtn').addEventListener('click', doLogout);

  // 导航
  document.querySelectorAll('#navTabs .tab').forEach((t) => {
    t.addEventListener('click', () => switchView(t.dataset.view));
  });

  // 合同搜索
  let searchTimer = null;
  $('contractSearch').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => loadContracts(value, 1), 320);
  });

  // 新建合同弹窗
  $('btnNewContract').addEventListener('click', openModal);
  $('modalClose').addEventListener('click', closeModal);
  $('modalCancel').addEventListener('click', closeModal);
  $('modalSubmit').addEventListener('click', submitNewContract);
  $('modalMask').addEventListener('click', (e) => {
    if (e.target === $('modalMask')) closeModal();
  });

  // 弹窗内 tab
  document.querySelectorAll('.mtab').forEach((t) => {
    t.addEventListener('click', () => switchModalMode(t.dataset.mode));
  });

  // 文件选择后更新提示
  $('fileInput').addEventListener('change', (e) => {
    const f = e.target.files[0];
    const zone = e.target.closest('.dropzone');
    if (f) {
      zone.classList.add('has-file');
      zone.querySelector('b').textContent = f.name;
      zone.querySelectorAll('span').forEach((s) => {
        if (!s.classList.contains('dz-icon')) s.textContent = `${(f.size / 1024).toFixed(1)} KB · 点击可重新选择`;
      });
      if (!$('newTitle').value.trim()) {
        $('newTitle').value = f.name.replace(/\.[^.]+$/, '');
      }
    }
  });

  // 文本输入实时验证
  $('textContent').addEventListener('input', (e) => updateTextValidation(e.target.value));

  // 看板 / 审计 / 评测 刷新
  $('btnRefreshDash').addEventListener('click', loadDashboard);
  $('btnRefreshAudit').addEventListener('click', () => loadAudit(state.auditPage));
  $('auditAction').addEventListener('change', (e) => {
    state.auditFilter = e.target.value;
    loadAudit(1);
  });
  $('btnEvalRun').addEventListener('click', () => runEvalAction(false));
  $('btnEvalRules').addEventListener('click', () => runEvalAction(true));

  // 快捷键：Esc 关闭弹窗，Ctrl/Cmd+Enter 提交，Tab 限制在弹窗内
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('modalMask').classList.contains('show')) {
      closeModal();
      return;
    }
    trapModalFocus(e);
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      if ($('modalMask').classList.contains('show') && !$('modalSubmit').disabled) {
        $('modalSubmit').click();
      } else if ($('screenLogin').classList.contains('active') && !$('loginBtn').disabled) {
        $('loginBtn').click();
      }
    }
  });
}

// ========================================================== 启动
async function boot() {
  bindEvents();

  if (!state.token) {
    showLogin();
    return;
  }

  // 有 token 就验一下还有效吗
  try {
    const user = await api('/api/auth/me');
    state.user = user;
    showApp();
    await loadContracts();
  } catch {
    clearSession();
    showLogin();
  }
}

boot();
