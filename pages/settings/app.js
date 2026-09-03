/* ============================================================
 * ComfyUI 智能绘图插件 · Pages 配置页前端交互脚本
 * 通过 window.AstrBotPluginPage bridge 调用后端 Web API。
 * ============================================================ */

const bridge = window.AstrBotPluginPage;

/* ---------- 字段映射：DOM id -> 嵌套配置路径 ---------- */
// 配置是嵌套结构，例如 server.base_url、model_switch.checkpoint，
// 前端用扁平 id，读取/写入时做路径展开。
const FIELD_MAP = {
  server_base_url: ['server', 'base_url'],
  server_timeout: ['server', 'timeout'],
  ms_checkpoint: ['model_switch', 'checkpoint'],
  ms_controlnet: ['model_switch', 'controlnet'],
  ms_vae: ['model_switch', 'vae'],
  ms_lora: ['model_switch', 'lora'],
  llm_provider: ['llm_settings', 'provider'],
  llm_model: ['llm_settings', 'model'],
  llm_base_url: ['llm_settings', 'base_url'],
  llm_api_key: ['llm_settings', 'api_key'],
  draw_width: ['draw_settings', 'default_width'],
  draw_height: ['draw_settings', 'default_height'],
  draw_steps: ['draw_settings', 'default_steps'],
  draw_sampler: ['draw_settings', 'default_sampler'],
  draw_cfg: ['draw_settings', 'default_cfg'],
  draw_negative: ['draw_settings', 'default_negative'],
  out_mention: ['output', 'mention_trigger_user'],
  perm_admins: ['permission', 'admin_ids'],
  perm_whitelist: ['permission', 'whitelist_user_ids'],
  perm_blacklist: ['permission', 'blacklist_user_ids'],
  perm_daily_limit: ['permission', 'daily_limit'],
  perm_cooldown: ['permission', 'cooldown_seconds'],
};

// 文本/数组字段（textarea 存列表）
const ARRAY_FIELDS = ['perm_admins', 'perm_whitelist', 'perm_blacklist'];
// 布尔字段（开关）
const BOOL_FIELDS = ['ms_checkpoint', 'ms_controlnet', 'ms_vae', 'ms_lora', 'out_mention'];
// 数字字段
const NUM_FIELDS = ['server_timeout', 'draw_width', 'draw_height', 'draw_steps', 'draw_cfg', 'perm_daily_limit', 'perm_cooldown'];

let dirty = false;

/* ---------- 小工具 ---------- */
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

function toast(msg, type) {
  type = type || 'info';
  const wrap = $('#toast-container');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.textContent = msg;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, 2600);
}

/* ---------- 嵌套路径读写 ---------- */
function getByPath(obj, path) {
  let cur = obj;
  for (const k of path) {
    if (cur == null) return undefined;
    cur = cur[k];
  }
  return cur;
}

function setByPath(obj, path, value) {
  let cur = obj;
  for (let i = 0; i < path.length - 1; i++) {
    const k = path[i];
    if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = {};
    cur = cur[k];
  }
  cur[path[path.length - 1]] = value;
  return obj;
}

/* ---------- 配置读写 ---------- */
function fillForm(cfg) {
  Object.keys(FIELD_MAP).forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const path = FIELD_MAP[id];
    let v = getByPath(cfg, path);
    if (v === undefined || v === null) v = '';
    if (ARRAY_FIELDS.indexOf(id) >= 0) {
      el.value = Array.isArray(v) ? v.join('\n') : v;
    } else if (BOOL_FIELDS.indexOf(id) >= 0) {
      el.checked = !!v;
    } else {
      el.value = v;
    }
  });
}

function collectForm() {
  const out = {};
  Object.keys(FIELD_MAP).forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const path = FIELD_MAP[id];
    let v;
    if (ARRAY_FIELDS.indexOf(id) >= 0) {
      v = el.value.split('\n').map((s) => s.trim()).filter(Boolean);
    } else if (BOOL_FIELDS.indexOf(id) >= 0) {
      v = el.checked;
    } else if (NUM_FIELDS.indexOf(id) >= 0) {
      const n = parseFloat(el.value);
      v = isNaN(n) ? 0 : n;
    } else {
      v = el.value;
    }
    setByPath(out, path, v);
  });
  return out;
}

async function loadConfig() {
  try {
    const data = await bridge.apiGet('config');
    // bridge 对 {status:"ok",data:value} 会直接 resolve 为 value
    const cfg = (data && data.data) ? data.data : (data || {});
    fillForm(cfg);
    setSaveState('配置已同步', false);
    dirty = false;
  } catch (e) {
    setSaveState('加载失败', true);
    toast('配置加载失败：' + e.message, 'error');
  }
}

async function saveConfig() {
  try {
    const payload = collectForm();
    await bridge.apiPost('config', payload);
    dirty = false;
    setSaveState('已保存', false);
    toast('配置已保存', 'success');
  } catch (e) {
    toast('保存失败：' + e.message, 'error');
  }
}

function setSaveState(text, isWarning) {
  const el = $('#save-state');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('warning', !!isWarning);
}

function markDirty() {
  if (!dirty) {
    dirty = true;
    setSaveState('有未保存更改', true);
  }
}

/* ---------- Tab 切换 ---------- */
function bindTabs() {
  const navItems = $$('.nav-item');
  const panes = $$('.tab-pane');
  const titleEl = $('#active-title');

  navItems.forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-target');
      navItems.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      panes.forEach((p) => {
        const isActive = p.id === target;
        p.classList.toggle('active', isActive);
        if (isActive && titleEl) {
          titleEl.textContent = p.getAttribute('data-title') || '';
        }
      });
      if (target === 'tab-stats') loadStats();
    });
  });
}

/* ---------- 模型刷新 ---------- */
async function refreshModels() {
  const btn = $('[data-action="refresh-models"]');
  if (btn) btn.disabled = true;
  toast('正在拉取并分析模型…（可能需要一会）', 'info');
  try {
    const result = await bridge.apiPost('models/refresh');
    if (result && result.ok) {
      toast('模型已刷新', 'success');
    } else {
      toast((result && result.message) || '刷新失败', 'error');
    }
  } catch (e) {
    toast('刷新模型失败：' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------- 统计 ---------- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (m) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '"', "'": '&#39;' }[m];
  });
}

async function loadStats() {
  const c = $('#stats-container');
  if (!c) return;
  try {
    const data = await bridge.apiGet('stats');
    const d = (data && data.data) ? data.data : (data || {});
    renderStats(d);
  } catch (e) {
    c.innerHTML = '<p class="muted">统计加载失败：' + esc(e.message) + '</p>';
  }
}

function renderStats(d) {
  const c = $('#stats-container');
  if (!c) return;
  const modelUsage = d.model_usage || {};
  const users = d.users || {};
  const records = d.records || [];
  const prompts = d.prompts || [];

  function tableFrom(obj) {
    const keys = Object.keys(obj);
    if (keys.length === 0) return '<p class="muted">暂无记录</p>';
    let html = '<table class="stat-table"><thead><tr><th>名称</th><th>次数</th></tr></thead><tbody>';
    keys.forEach((k) => {
      let val = obj[k];
      if (val && typeof val === 'object' && val.count !== undefined) val = val.count;
      html += '<tr><td>' + esc(k) + '</td><td>' + esc(val) + '</td></tr>';
    });
    html += '</tbody></table>';
    return html;
  }

  const recent = (Array.isArray(records) ? records : []).slice(-10).reverse();
  let recordHtml = recent.length === 0
    ? '<p class="muted">暂无出图记录</p>'
    : '<ul class="record-list">' + recent.map((r) => {
        const t = r.time || r.ts || '';
        const u = r.user_name || r.user || r.user_id || '';
        return '<li><span class="rec-time">' + esc(t) + '</span><span class="rec-user">' + esc(u) + '</span></li>';
      }).join('') + '</ul>';

  const recentPrompts = (Array.isArray(prompts) ? prompts : []).slice(-10).reverse();
  let promptHtml = recentPrompts.length === 0
    ? '<p class="muted">暂无提示词记录</p>'
    : '<ul class="record-list">' + recentPrompts.map((p) => {
        const pos = typeof p === 'string' ? p : (p.positive || '');
        return '<li><span class="rec-prompt">' + esc(pos) + '</span></li>';
      }).join('') + '</ul>';

  // 作品画廊：从最近记录里收集图片
  const galleryHtml = buildGallery(recent);

  c.innerHTML =
    '<div class="stats-grid">' +
      '<div class="stat-block"><h4>模型调用</h4>' + tableFrom(modelUsage) + '</div>' +
      '<div class="stat-block"><h4>用户出图</h4>' + tableFrom(users) + '</div>' +
      '<div class="stat-block"><h4>最近出图</h4>' + recordHtml + '</div>' +
      '<div class="stat-block"><h4>最近提示词</h4>' + promptHtml + '</div>' +
    '</div>' +
    '<div class="stat-block gallery-block"><h4>🎨 作品画廊</h4>' + galleryHtml + '</div>';
}

/* 把图片相对引用（images/xxx.png）拼成可访问 URL */
function imageUrl(ref) {
  if (!ref) return '';
  // 已是完整 URL 直接返回
  if (/^https?:\/\//.test(ref)) return ref;
  // 已是绝对路径（以 / 开头）直接返回
  if (ref.charAt(0) === '/') return ref;
  // 相对引用：拼到当前 AstrBot 插件扩展路径（前导 / 确保绝对路径）
  return '/api/v1/plugins/extensions/astrbot_plugin_comfyui_smart/' + ref;
}

/* 构建画廊 HTML */
function buildGallery(records) {
  const items = [];
  (records || []).forEach((r) => {
    const imgs = r.images || [];
    imgs.forEach((ref) => {
      items.push({ ref: ref, time: r.time, user: r.user_name, prompt: r.positive });
    });
  });
  if (items.length === 0) return '<p class="muted">暂无作品，出图后这里会展示</p>';
  return '<div class="gallery-grid">' + items.map((it) => {
    const url = imageUrl(it.ref);
    return '<figure class="gallery-item">' +
      '<img src="' + esc(url) + '" loading="lazy" alt="' + esc(it.prompt || '') + '" />' +
      '<figcaption>' +
        '<span class="gal-prompt">' + esc(it.prompt || '') + '</span>' +
        '<span class="gal-meta">' + esc(it.user || '') + ' · ' + esc(it.time || '') + '</span>' +
      '</figcaption></figure>';
  }).join('') + '</div>';
}

/* ---------- 事件绑定 ---------- */
function bindEvents() {
  const saveBtn = $('[data-action="save-config"]');
  if (saveBtn) saveBtn.addEventListener('click', saveConfig);

  const refreshModelBtn = $('[data-action="refresh-models"]');
  if (refreshModelBtn) refreshModelBtn.addEventListener('click', refreshModels);

  const refreshStatsBtn = $('[data-action="refresh-stats"]');
  if (refreshStatsBtn) refreshStatsBtn.addEventListener('click', loadStats);

  Object.keys(FIELD_MAP).forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('input', markDirty);
    el.addEventListener('change', markDirty);
  });

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      saveConfig();
    }
  });
}

/* ---------- 初始化 ---------- */
async function init() {
  bindTabs();
  bindEvents();
  try {
    await bridge.ready();
  } catch (e) {
    // 桥接未就绪时仍尝试加载（可能失败）
  }
  loadConfig();
}

init();