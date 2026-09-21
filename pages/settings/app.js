/* ============================================================
 * ComfyUI 智能绘图 · 插件 Pages 前端
 *
 * 与旧版的关键区别：
 *  1. 保存时只提交本页管理的字段（服务端做深度合并），因此表单漏掉某个配置项
 *     也不会把它清空 —— 旧版提交整个嵌套对象，导致 output.save_image 每次保存被抹掉。
 *  2. 整数/浮点分开解析（旧版统一 parseFloat，把 int 字段写成了 120.0）。
 *  3. bridge 不存在时明确报错，而不是静默失败让用户以为「配置无效」。
 * ============================================================ */

const bridge = window.AstrBotPluginPage;
const PLUGIN_NAME = 'astrbot_plugin_comfyui_smart';

/* 字段定义：id -> 配置路径与类型 */
const FIELDS = [
  { id: 'server_base_url', path: ['server', 'base_url'], kind: 'string' },
  { id: 'server_timeout', path: ['server', 'timeout'], kind: 'int' },
  { id: 'server_poll_interval', path: ['server', 'poll_interval'], kind: 'float' },
  { id: 'server_max_tasks_ahead', path: ['server', 'max_tasks_ahead'], kind: 'int' },

  { id: 'llm_enable_optimize', path: ['llm_settings', 'enable_prompt_optimize'], kind: 'bool' },
  { id: 'llm_provider', path: ['llm_settings', 'provider'], kind: 'string' },
  { id: 'llm_base_url', path: ['llm_settings', 'base_url'], kind: 'string' },
  { id: 'llm_api_key', path: ['llm_settings', 'api_key'], kind: 'string' },
  { id: 'llm_model', path: ['llm_settings', 'model'], kind: 'string' },

  { id: 'draw_override_arch', path: ['draw_settings', 'override_arch_defaults'], kind: 'bool' },
  { id: 'draw_arch_override', path: ['draw_settings', 'arch_override'], kind: 'string' },
  { id: 'draw_width', path: ['draw_settings', 'default_width'], kind: 'int' },
  { id: 'draw_height', path: ['draw_settings', 'default_height'], kind: 'int' },
  { id: 'draw_steps', path: ['draw_settings', 'default_steps'], kind: 'int' },
  { id: 'draw_cfg', path: ['draw_settings', 'default_cfg'], kind: 'float' },
  { id: 'draw_sampler', path: ['draw_settings', 'default_sampler'], kind: 'string' },
  { id: 'draw_quality_tags', path: ['draw_settings', 'add_quality_tags'], kind: 'bool' },
  { id: 'draw_negative_mode', path: ['draw_settings', 'negative_mode'], kind: 'select',
    options: ['merge', 'guard_only', 'custom_only', 'raw'] },
  { id: 'draw_force_vae', path: ['draw_settings', 'force_vae'], kind: 'string' },
  { id: 'draw_negative', path: ['draw_settings', 'default_negative'], kind: 'text' },

  { id: 'i2i_enable', path: ['i2i', 'enable'], kind: 'bool' },
  { id: 'i2i_denoise', path: ['i2i', 'denoise'], kind: 'float' },
  { id: 'i2i_max_side', path: ['i2i', 'max_side'], kind: 'int' },
  { id: 'i2i_subfolder', path: ['i2i', 'subfolder'], kind: 'string' },

  { id: 'hires_enable', path: ['hires', 'enable'], kind: 'bool' },
  { id: 'hires_scale', path: ['hires', 'scale'], kind: 'float' },
  { id: 'hires_denoise', path: ['hires', 'denoise'], kind: 'float' },
  { id: 'hires_steps', path: ['hires', 'steps'], kind: 'int' },
  { id: 'hires_method', path: ['hires', 'method'], kind: 'select',
    options: ['bislerp', 'bilinear', 'bicubic', 'area', 'nearest-exact'] },

  { id: 'out_mention', path: ['output', 'mention_trigger_user'], kind: 'bool' },
  { id: 'out_show_params', path: ['output', 'show_params'], kind: 'bool' },
  { id: 'out_keep_images', path: ['output', 'keep_images'], kind: 'int' },
  { id: 'out_image_age', path: ['output', 'image_max_age_days'], kind: 'int' },

  { id: 'perm_whitelist', path: ['permission', 'whitelist_user_ids'], kind: 'list' },
  { id: 'perm_blacklist', path: ['permission', 'blacklist_user_ids'], kind: 'list' },
  { id: 'perm_daily_limit', path: ['permission', 'daily_limit'], kind: 'int' },
  { id: 'perm_cooldown', path: ['permission', 'cooldown_seconds'], kind: 'int' },
  { id: 'perm_admin_bypass', path: ['permission', 'admin_bypass'], kind: 'bool' },

  { id: 'agent_enable_tool', path: ['agent', 'enable_llm_tool'], kind: 'bool' },
];

let dirty = false;
/* 画廊条目：供详情弹窗使用 */
let galleryItems = [];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(message, type) {
  const wrap = $('#toast-container');
  const el = document.createElement('div');
  el.className = 'toast toast-' + (type || 'info');
  el.textContent = message;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 3000);
}

function setSaveState(text, warning) {
  const el = $('#save-state');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('warning', !!warning);
}

function getByPath(source, path) {
  let current = source;
  for (const key of path) {
    if (current == null || typeof current !== 'object') return undefined;
    current = current[key];
  }
  return current;
}

function setByPath(target, path, value) {
  let current = target;
  for (let i = 0; i < path.length - 1; i += 1) {
    const key = path[i];
    if (current[key] == null || typeof current[key] !== 'object') current[key] = {};
    current = current[key];
  }
  current[path[path.length - 1]] = value;
  return target;
}

function fillForm(config) {
  FIELDS.forEach((field) => {
    const el = document.getElementById(field.id);
    if (!el) return;
    const value = getByPath(config, field.path);
    if (field.kind === 'bool') { el.checked = !!value; return; }
    if (field.kind === 'list') {
      el.value = Array.isArray(value) ? value.join('\n') : (value || '');
      return;
    }
    el.value = value === undefined || value === null ? '' : value;
  });
}

/* 只收集本页管理的字段：服务端会与磁盘配置做深度合并 */
function collectPatch() {
  const patch = {};
  FIELDS.forEach((field) => {
    const el = document.getElementById(field.id);
    if (!el) return;
    let value;
    switch (field.kind) {
      case 'bool':
        value = !!el.checked;
        break;
      case 'list':
        value = el.value.split('\n').map((s) => s.trim()).filter(Boolean);
        break;
      case 'int': {
        const n = parseInt(el.value, 10);
        value = Number.isFinite(n) ? n : 0;
        break;
      }
      case 'float': {
        const f = parseFloat(el.value);
        value = Number.isFinite(f) ? f : 0;
        break;
      }
      default:
        value = el.value;
    }
    setByPath(patch, field.path, value);
  });
  return patch;
}

async function loadConfig() {
  try {
    const data = await bridge.apiGet('config');
    const config = (data && data.config) ? data.config : (data || {});
    fillForm(config);
    dirty = false;
    setSaveState('配置已同步', false);
  } catch (error) {
    setSaveState('加载失败', true);
    toast('配置加载失败：' + error.message, 'error');
  }
}

async function saveConfig() {
  const button = $('[data-action="save-config"]');
  if (button) button.disabled = true;
  try {
    const patch = collectPatch();
    await bridge.apiPost('config', patch);
    dirty = false;
    setSaveState('已保存', false);
    toast('配置已保存并写入磁盘', 'success');
  } catch (error) {
    setSaveState('保存失败', true);
    toast('保存失败：' + error.message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (m) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]
  ));
}

async function loadStatus() {
  const box = $('#status-container');
  if (!box) return;
  box.innerHTML = '<p class="muted">读取中…</p>';
  try {
    const info = await bridge.apiGet('status');
    const rows = [
      ['服务地址', info.base_url || '—'],
      ['连接状态', info.online ? '✅ 正常' : ('❌ ' + (info.error || '不可用'))],
      ['设备', info.device || '—'],
      ['队列', info.queue ? ('执行中 ' + info.queue.running + ' · 等待中 ' + info.queue.pending) : '—'],
      ['模板', (info.templates || []).length + ' 个'],
    ];
    box.innerHTML = '<table class="stat-table"><tbody>' + rows.map(
      (row) => '<tr><td>' + esc(row[0]) + '</td><td>' + esc(row[1]) + '</td></tr>'
    ).join('') + '</tbody></table>';
  } catch (error) {
    box.innerHTML = '<p class="muted">状态读取失败：' + esc(error.message) + '</p>';
  }
}

async function loadTemplates() {
  const box = $('#templates-container');
  if (!box) return;
  try {
    const data = await bridge.apiGet('templates');
    const templates = data.templates || [];
    if (!templates.length) {
      box.innerHTML = '<p class="muted">没有加载到任何模板</p>';
      return;
    }
    box.innerHTML = '<table class="stat-table"><thead><tr><th>模板</th><th>架构</th><th>加载方式</th><th>节点数</th></tr></thead><tbody>'
      + templates.map((t) => '<tr><td>' + esc(t.name) + '</td><td>' + esc(t.arch)
        + '</td><td>' + esc(t.loader) + '</td><td>' + esc(t.nodes) + '</td></tr>').join('')
      + '</tbody></table>'
      + '<p class="muted">自定义模板目录：' + esc(data.user_dir || '') + '</p>'
      + ((data.failed && data.failed.length)
        ? '<p class="muted">⚠️ 加载失败：' + esc(data.failed.join('、')) + '</p>' : '');
  } catch (error) {
    box.innerHTML = '<p class="muted">模板读取失败：' + esc(error.message) + '</p>';
  }
}

async function loadModels() {
  const box = $('#models-container');
  if (!box) return;
  box.innerHTML = '<p class="muted">读取中…</p>';
  try {
    const data = await bridge.apiGet('models');
    const catalog = data.catalog || {};
    const folders = Object.keys(catalog).sort();
    if (!folders.length) {
      box.innerHTML = '<p class="muted">没有发现模型，请检查 ComfyUI 地址后点「刷新模型」</p>';
      return;
    }
    box.innerHTML = folders.map((folder) => {
      const files = catalog[folder] || [];
      const shown = files.slice(0, 30).map((f) => '<li>' + esc(f) + '</li>').join('');
      const more = files.length > 30 ? '<li class="muted">… 共 ' + files.length + ' 个</li>' : '';
      return '<div class="stat-block"><h4>' + esc(folder) + '（' + files.length + '）</h4><ul class="record-list">' + shown + more + '</ul></div>';
    }).join('');
  } catch (error) {
    box.innerHTML = '<p class="muted">模型读取失败：' + esc(error.message) + '</p>';
  }
}

async function refreshModels() {
  const button = $('[data-action="refresh-models"]');
  if (button) button.disabled = true;
  toast('正在读取 ComfyUI 的模型清单…', 'info');
  try {
    const result = await bridge.apiPost('models/refresh');
    if (result && result.ok) {
      toast(result.message || '模型已刷新', 'success');
      loadModels();
    } else {
      toast((result && result.message) || '刷新失败', 'error');
    }
  } catch (error) {
    toast('刷新失败：' + error.message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadStats() {
  const box = $('#stats-container');
  if (!box) return;
  try {
    const stats = await bridge.apiGet('stats');
    const users = stats.users || {};
    const usage = stats.model_usage || {};
    const records = stats.records || [];

    const table = (obj, transform) => {
      const keys = Object.keys(obj);
      if (!keys.length) return '<p class="muted">暂无记录</p>';
      return '<table class="stat-table"><thead><tr><th>名称</th><th>次数</th></tr></thead><tbody>'
        + keys.map((k) => '<tr><td>' + esc(k) + '</td><td>' + esc(transform ? transform(obj[k]) : obj[k]) + '</td></tr>').join('')
        + '</tbody></table>';
    };

    // 展平出画廊条目，并保留完整记录供弹窗展示
    galleryItems = records.slice(-60).reverse().flatMap((record) => (record.images || []).map((ref) => ({
      url: '/api/v1/plugins/extensions/' + PLUGIN_NAME + '/' + ref,
      ref: ref,
      prompt: record.positive,
      negative: record.negative,
      model: record.model || (record.params || {}).model || '',
      template: record.template || (record.params || {}).template || '',
      seconds: record.seconds,
      params: record.params || {},
      meta: (record.user_name || '') + ' · ' + (record.time || ''),
      time: record.time || '',
      user: record.user_name || '',
    })));

    box.innerHTML = '<div class="stats-grid">'
      + '<div class="stat-block"><h4>用户出图</h4>' + table(users, (v) => v.count) + '</div>'
      + '<div class="stat-block"><h4>底模调用</h4>' + table(usage.checkpoint || {}) + '</div>'
      + '<div class="stat-block"><h4>LoRA 调用</h4>' + table(usage.lora || {}) + '</div>'
      + '<div class="stat-block"><h4>最近记录</h4>' + (records.length
        ? '<ul class="record-list">' + records.slice(-10).reverse().map((r) => '<li><span class="rec-time">' + esc(r.time) + '</span><span class="rec-user">' + esc(r.user_name) + '</span></li>').join('') + '</ul>'
        : '<p class="muted">暂无出图记录</p>') + '</div>'
      + '</div>'
      + '<div class="stat-block gallery-block"><h4>🎨 作品画廊</h4>' + (galleryItems.length
        ? '<p class="muted gallery-hint">点击任意图片可查看正/负面提示词与完整参数</p>'
          + '<div class="gallery-grid">' + galleryItems.map((item, index) =>
            '<figure class="gallery-item" data-index="' + index + '" title="点击查看提示词与参数">'
            + '<img src="' + esc(item.url) + '" loading="lazy" alt="" '
            + 'onerror="this.parentNode.classList.add(\'missing\')" />'
            + '<figcaption><span class="gal-prompt">' + esc(item.prompt || '')
            + '</span><span class="gal-meta">' + esc(item.meta) + '</span></figcaption></figure>').join('') + '</div>'
        : '<p class="muted">暂无作品，出图后这里会展示</p>') + '</div>';
  } catch (error) {
    box.innerHTML = '<p class="muted">统计读取失败：' + esc(error.message) + '</p>';
  }
}

/* ---------- 作品详情弹窗 ---------- */

const PARAM_LABELS = [
  ['model', '底模'], ['template', '模板'], ['arch', '架构'],
  ['width', '宽度'], ['height', '高度'],
  ['steps', '步数'], ['cfg', 'CFG'], ['sampler', '采样器'], ['seed', '种子'],
  ['lora', 'LoRA'], ['vae', 'VAE'],
];

function renderParams(item) {
  const params = item.params || {};
  const rows = [];
  PARAM_LABELS.forEach(([key, label]) => {
    let value = params[key];
    if (value === undefined || value === null || value === '') {
      if (key === 'model') value = item.model;
      else if (key === 'template') value = item.template;
      else return;
    }
    if (key === 'width' || key === 'height') value = value + ' px';
    rows.push('<tr><td>' + esc(label) + '</td><td>' + esc(value) + '</td></tr>');
  });
  rows.push('<tr><td>耗时</td><td>' + esc((item.seconds || 0) + ' 秒') + '</td></tr>');
  rows.push('<tr><td>时间</td><td>' + esc(item.time) + '</td></tr>');
  rows.push('<tr><td>发起人</td><td>' + esc(item.user) + '</td></tr>');
  return '<table class="stat-table"><tbody>' + rows.join('') + '</tbody></table>';
}

function openDetail(index) {
  const item = galleryItems[index];
  const modal = $('#image-modal');
  if (!item || !modal) return;
  modal.hidden = false;
  modal.classList.add('open');
  $('#modal-image').src = item.url;
  $('#modal-image').alt = item.prompt || '';
  $('#modal-params').innerHTML = renderParams(item);
  $('#modal-positive').value = item.prompt || '';
  $('#modal-negative').value = item.negative || '（未记录）';
  $('#modal-title').textContent = item.time ? ('作品详情 · ' + item.time) : '作品详情';
}

function closeDetail() {
  const modal = $('#image-modal');
  if (!modal) return;
  modal.classList.remove('open');
  modal.hidden = true;
}

async function copyField(fieldId, label) {
  const el = document.getElementById(fieldId);
  if (!el) return;
  const text = el.value || '';
  if (!text) {
    toast(label + '为空', 'error');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast(label + '已复制', 'success');
    return;
  } catch (error) {
    // 插件页在 iframe 里，剪贴板权限可能被拒；退化为选中文本让用户手动复制
  }
  try {
    el.removeAttribute('readonly');
    el.select();
    document.execCommand('copy');
    el.setAttribute('readonly', 'readonly');
    el.setSelectionRange(0, 0);
    toast(label + '已复制', 'success');
  } catch (error) {
    toast('复制失败，请手动选择文本复制', 'error');
  }
}

function bindModal() {
  const container = $('#stats-container');
  if (container) {
    container.addEventListener('click', (event) => {
      const figure = event.target.closest('.gallery-item');
      if (!figure) return;
      openDetail(Number(figure.getAttribute('data-index')));
    });
  }
  const modal = $('#image-modal');
  if (modal) {
    modal.addEventListener('click', (event) => {
      // 点遮罩或关闭按钮都收起
      if (event.target === modal || event.target.closest('[data-action="close-modal"]')) {
        closeDetail();
      }
    });
  }
  const bind = (action, handler) => {
    const el = document.querySelector('[data-action="' + action + '"]');
    if (el) el.addEventListener('click', handler);
  };
  bind('copy-positive', () => copyField('modal-positive', '正向提示词'));
  bind('copy-negative', () => copyField('modal-negative', '负面提示词'));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDetail();
  });
}

function bindTabs() {
  const items = $$('.nav-item');
  const panes = $$('.tab-pane');
  const title = $('#active-title');
  items.forEach((button) => {
    button.addEventListener('click', () => {
      const target = button.getAttribute('data-target');
      items.forEach((b) => b.classList.remove('active'));
      button.classList.add('active');
      panes.forEach((pane) => {
        const active = pane.id === target;
        pane.classList.toggle('active', active);
        if (active && title) title.textContent = pane.getAttribute('data-title') || '';
      });
      if (target === 'tab-stats') loadStats();
      if (target === 'tab-status') { loadStatus(); loadTemplates(); }
      if (target === 'tab-model') loadModels();
    });
  });
}

function bindEvents() {
  const save = $('[data-action="save-config"]');
  if (save) save.addEventListener('click', saveConfig);
  const refresh = $('[data-action="refresh-models"]');
  if (refresh) refresh.addEventListener('click', refreshModels);
  const reloadModels = $('[data-action="reload-models"]');
  if (reloadModels) reloadModels.addEventListener('click', loadModels);
  const reloadStats = $('[data-action="reload-stats"]');
  if (reloadStats) reloadStats.addEventListener('click', loadStats);
  const reloadStatus = $('[data-action="reload-status"]');
  if (reloadStatus) reloadStatus.addEventListener('click', () => { loadStatus(); loadTemplates(); });

  FIELDS.forEach((field) => {
    const el = document.getElementById(field.id);
    if (!el) return;
    const mark = () => { if (!dirty) { dirty = true; setSaveState('有未保存更改', true); } };
    el.addEventListener('input', mark);
    el.addEventListener('change', mark);
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 's') {
      event.preventDefault();
      saveConfig();
    }
  });
}

async function init() {
  if (!bridge || typeof bridge.apiGet !== 'function') {
    setSaveState('bridge 不可用', true);
    const box = $('#toast-container');
    if (box) {
      const el = document.createElement('div');
      el.className = 'toast toast-error show';
      el.textContent = '未能连接到 AstrBot Pages bridge：请确认是通过 WebUI 的插件页打开本页面。';
      box.appendChild(el);
    }
    return;
  }
  bindTabs();
  bindEvents();
  bindModal();
  try {
    await bridge.ready();
  } catch (error) {
    // bridge 上下文未就绪不阻塞配置加载
  }
  await loadConfig();
}

init();
