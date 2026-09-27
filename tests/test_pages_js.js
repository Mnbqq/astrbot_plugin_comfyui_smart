/* ============================================================
 * 插件 Pages 前端的执行测试（Node + 最小 DOM 桩）
 *
 * 为什么需要它：
 *   插件页的 app.js 之前只被 `node --check` 检查过语法 —— 语法检查查不出
 *   「引用了已改名/不存在的变量」这类错误。实际就出过这个问题：
 *   把 gallery 改名为 galleryItems 时漏改了判断条件，
 *   页面直接报「统计读取失败：gallery is not defined」。
 *   本测试把 app.js 真正跑起来，驱动 loadStats / openDetail / copyField，
 *   断言渲染结果与弹窗内容，从而覆盖这一类问题。
 *
 * 用法：node tests/test_pages_js.js
 * ============================================================ */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let PASSED = 0;
let FAILED = 0;

function check(label, ok, extra) {
  if (ok) {
    PASSED += 1;
    console.log('  PASS ' + label + (extra === undefined ? '' : ' ' + JSON.stringify(extra)));
  } else {
    FAILED += 1;
    console.log('  FAIL ' + label + (extra === undefined ? '' : ' ' + JSON.stringify(extra)));
  }
}

/* ---------- 最小 DOM 桩 ---------- */
function makeCanvasContext(canvas) {
  const ctx = {
    canvas,
    calls: [],
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    lineCap: '',
    lineJoin: '',
    globalAlpha: 1,
    globalCompositeOperation: 'source-over',
    clearRect(...a) { ctx.calls.push(['clearRect', ...a]); },
    fillRect(...a) { ctx.calls.push(['fillRect', ...a]); },
    drawImage(...a) { ctx.calls.push(['drawImage', ...a]); },
    beginPath() { ctx.calls.push(['beginPath']); },
    moveTo(...a) { ctx.calls.push(['moveTo', ...a]); },
    lineTo(...a) { ctx.calls.push(['lineTo', ...a]); },
    stroke() { ctx.calls.push(['stroke', ctx.strokeStyle]); },
    arc(...a) { ctx.calls.push(['arc', ...a]); },
    fill() { ctx.calls.push(['fill', ctx.fillStyle]); },
    getImageData(x, y, w, h) { return { data: new Uint8ClampedArray(w * h * 4) }; },
    putImageData() {},
  };
  return ctx;
}

function makeElement(tag) {
  const el = {
    tagName: tag || 'div',
    width: 0,
    height: 0,
    _handlers: {},
    _dataUrl: '',
    _ctx: null,
    getContext() {
      if (!this._ctx) this._ctx = makeCanvasContext(this);
      return this._ctx;
    },
    toDataURL() {
      return this._dataUrl || ('data:image/png;base64,CANVAS' + this.width + 'x' + this.height);
    },
    getBoundingClientRect() {
      return { left: 0, top: 0, width: this.width || 100, height: this.height || 100 };
    },
    children: [],
    attrs: {},
    _html: '',
    value: '',
    files: [],
    checked: false,
    hidden: false,
    disabled: false,
    src: '',
    alt: '',
    textContent: '',
    style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on) { this._set.add(c); } else { this._set.delete(c); } },
      contains(c) { return this._set.has(c); },
    },
    addEventListener(type, fn) {
      this._handlers = this._handlers || {};
      (this._handlers[type] = this._handlers[type] || []).push(fn);
    },
    removeEventListener() {},
    dispatch(type, event) {
      ((this._handlers || {})[type] || []).forEach((fn) => fn(event || {}));
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    remove() {},
    select() { this.selected = true; },
    setSelectionRange() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  Object.defineProperty(el, 'parentNode', { get() { return null; } });
  return el;
}

const registry = new Map();
function getEl(id) {
  if (!registry.has(id)) registry.set(id, makeElement());
  return registry.get(id);
}

const documentStub = {
  documentElement: makeElement('html'),
  body: makeElement('body'),
  querySelector(sel) {
    const m = /^#([\w-]+)$/.exec(sel);
    return m ? getEl(m[1]) : null; // 属性选择器返回 null，app.js 里都有 if 保护
  },
  querySelectorAll() { return []; },
  getElementById(id) { return getEl(id); },
  createElement(tag) { return makeElement(tag); },
  addEventListener() {},
  execCommand() { return true; },
};

/* ---------- 造一份统计响应（含一条带完整参数的记录 + 一条老格式记录）---------- */
const statsPayload = {
  users: { u1: { name: '画手', count: 2 } },
  model_usage: { checkpoint: { 'm.safetensors': 2 }, lora: {} },
  // 记录按时间先后追加（插件就是这样写的），所以最新的一条在最后
  records: [
    { // 老版本写下的记录：没有 params 字段
      time: '2026-09-21 15:00:00', user_id: 'u1', user_name: '画手',
      positive: 'old prompt', negative: 'old negative',
      template: 'sd_checkpoint', model: 'm.safetensors', seconds: 3.0,
      images: ['images/b.png'],
    },
    {
      time: '2026-09-21 16:00:00', user_id: 'u1', user_name: '画手',
      positive: 'masterpiece, best quality, 1girl', negative: 'bad hands, extra fingers',
      template: 'sd_checkpoint', model: 'm.safetensors', seconds: 12.5,
      images: ['images/a.png'],
      params: { width: 512, height: 768, steps: 25, cfg: 7.0, sampler: 'dpmpp_2m',
                seed: 12345, arch: 'sd15', lora: 'x.safetensors', vae: '',
                template: 'sd_checkpoint', model: 'm.safetensors' },
    },
  ],
};

const bridge = {
  ready: async () => ({}),
  async apiGet(endpoint) {
    if (endpoint === 'config') return { config: {} };
    if (endpoint === 'stats') return statsPayload;
    if (endpoint === 'status') return { base_url: 'http://x', online: true, templates: [] };
    if (endpoint === 'models') return { catalog: {} };
    if (endpoint === 'templates') return { templates: [] };
    return {};
  },
  async apiPost(endpoint, payload) {
    bridge.posted.push([endpoint, payload]);
    if (endpoint === 'inpaint') {
      return { ok: true, image: 'images/inpaint-1.png', template: 'inpaint_checkpoint',
               seed: 4242, seconds: 8.5, width: 512, height: 512 };
    }
    return { ok: true };
  },
  posted: [],
};

/* 文件与图片桩：inpaintLoadFile 走的是真实代码路径（FileReader + Image） */
const imageStub = { width: 3000, height: 2000 };
class FileReaderStub {
  readAsDataURL() {
    this.result = 'data:image/png;base64,ORIGINAL';
    setTimeout(() => { if (this.onload) this.onload(); }, 0);
  }
}
class ImageStub {
  constructor() {
    this.naturalWidth = imageStub.width;
    this.naturalHeight = imageStub.height;
  }
  set src(value) { this._src = value; setTimeout(() => { if (this.onload) this.onload(); }, 0); }
  get src() { return this._src; }
}

const sandbox = {
  window: { AstrBotPluginPage: bridge, location: { origin: 'http://localhost' }, addEventListener() {} },
  FileReader: FileReaderStub,
  Image: ImageStub,
  Uint8ClampedArray,
  document: documentStub,
  navigator: {},              // 故意不给 clipboard：验证复制会退化为 execCommand
  console,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  Promise, JSON, Math, Number, String, Boolean, Array, Object, Date, RegExp, Error,
  isNaN, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const source = fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings', 'app.js'), 'utf8');
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'app.js' });

(async () => {
  await new Promise((resolve) => setTimeout(resolve, 20)); // 让 init() 跑完

  console.log('=== 插件页前端（真实执行，而非仅语法检查）===');
  const container = getEl('stats-container');

  await vm.runInContext('loadStats()', sandbox);
  const html = container.innerHTML;

  check('loadStats 未抛错（曾出现 gallery is not defined）',
        !html.includes('统计读取失败'), (html.match(/统计读取失败[^<]*/) || [''])[0]);
  check('渲染出了画廊条目', html.includes('gallery-item') && html.includes('data-index="0"'),
        (html.match(/data-index="\d+"/g) || []).length + ' 个条目');
  const figures = (html.match(/class="gallery-item"/g) || []).length;
  check('两张图各渲染一个条目', figures === 2, figures);
  check('画廊带点击提示', html.includes('点击任意图片'));
  check('最新的一张排在最前（index 0 是 16:00 那条）',
        vm.runInContext('galleryItems[0].time', sandbox) === '2026-09-21 16:00:00',
        vm.runInContext('galleryItems[0].time', sandbox));
  check('老记录（无 params）不影响渲染', html.includes('old prompt') || figures === 2);

  console.log('\n=== 作品详情弹窗 ===');
  await vm.runInContext('openDetail(0)', sandbox);
  const modal = getEl('image-modal');
  check('弹窗被打开', modal.hidden === false && modal.classList.contains('open'));
  check('填入正向提示词', getEl('modal-positive').value === 'masterpiece, best quality, 1girl',
        getEl('modal-positive').value);
  check('填入负面提示词', getEl('modal-negative').value === 'bad hands, extra fingers',
        getEl('modal-negative').value);
  check('弹窗标题带时间', String(getEl('modal-title').textContent).includes('16:00:00'),
        getEl('modal-title').textContent);
  const paramsHtml = getEl('modal-params').innerHTML;
  check('参数表含步数/CFG/采样器/种子',
        ['步数', 'CFG', '采样器', '种子'].every((k) => paramsHtml.includes(k)));
  check('参数值正确', paramsHtml.includes('12345') && paramsHtml.includes('25')
        && paramsHtml.includes('dpmpp_2m'));
  check('分辨率带单位', paramsHtml.includes('512 px') && paramsHtml.includes('768 px'));

  // 老记录（无 params）也要能打开
  await vm.runInContext('openDetail(1)', sandbox);
  check('老记录也能打开弹窗且不抛错',
        getEl('modal-positive').value === 'old prompt'
        && getEl('modal-negative').value === 'old negative',
        getEl('modal-positive').value);

  console.log('\n=== 局部重绘：涂抹遮罩 ===');

  await vm.runInContext("inpaintLoadFile({ name: 'x.png' })", sandbox);
  check('加载图片后按最长边上限缩小画布（3000x2000 → 1536x1024）',
        getEl('inpaint_canvas').width === 1536 && getEl('inpaint_canvas').height === 1024,
        [getEl('inpaint_canvas').width, getEl('inpaint_canvas').height]);
  check('原图与遮罩画布尺寸一致（后端会校验）',
        getEl('inpaint_source').width === getEl('inpaint_mask').width
        && getEl('inpaint_source').height === getEl('inpaint_mask').height,
        [getEl('inpaint_source').width, getEl('inpaint_mask').width]);
  check('还没涂任何东西时不能提交（payload 为 null）',
        vm.runInContext('inpaintPayload()', sandbox) === null);

  // 指针事件：按下 → 移动 → 抬起
  const canvasEl = getEl('inpaint_canvas');
  const maskCtx = getEl('inpaint_mask').getContext('2d');
  canvasEl.dispatch('pointerdown', { clientX: 10, clientY: 10, preventDefault() {} });
  canvasEl.dispatch('pointermove', { clientX: 60, clientY: 40 });
  canvasEl.dispatch('pointerup', {});
  const strokes = maskCtx.calls.filter((c) => c[0] === 'stroke');
  check('涂抹会在遮罩上画白线（白=要重画）',
        strokes.length >= 1 && strokes.every((c) => c[1] === '#fff'), strokes.slice(0, 3));
  check('抬起后不再继续画（pointerup 生效）',
        (() => {
          const before = maskCtx.calls.length;
          canvasEl.dispatch('pointermove', { clientX: 200, clientY: 200 });
          return maskCtx.calls.length === before;
        })());
  check('展示层画了原图与红色叠层',
        getEl('inpaint_canvas').getContext('2d').calls.some((c) => c[0] === 'drawImage'),
        getEl('inpaint_canvas').getContext('2d').calls.length);

  // 表单里的默认值：真实页面来自 HTML 的 value 属性，桩里手动给上
  getEl('inpaint_denoise').value = '0.8';
  getEl('inpaint_grow').value = '6';
  const payload = vm.runInContext('inpaintPayload()', sandbox);
  check('payload 带上原图与遮罩两张 PNG',
        !!payload && payload.image.indexOf('data:image/png') === 0
        && payload.mask.indexOf('data:image/png') === 0, payload && [payload.image.slice(0, 20), payload.mask.slice(0, 20)]);
  check('payload 带上画笔之外的参数（重绘幅度 / 遮罩外扩）',
        !!payload && payload.denoise === '0.8' && payload.grow === '6',
        payload && [payload.denoise, payload.grow]);

  // 橡皮：涂黑 = 不重画
  vm.runInContext('inpaintState.erase = true', sandbox);
  canvasEl.dispatch('pointerdown', { clientX: 20, clientY: 20, preventDefault() {} });
  canvasEl.dispatch('pointerup', {});
  check('橡皮在遮罩上画黑（黑=保留）',
        maskCtx.calls.filter((c) => c[0] === 'stroke').slice(-1)[0][1] === '#000',
        maskCtx.calls.filter((c) => c[0] === 'stroke').slice(-1)[0]);
  vm.runInContext('inpaintState.erase = false', sandbox);

  await vm.runInContext('inpaintRun()', sandbox);
  const posted = bridge.posted.filter((item) => item[0] === 'inpaint');
  check('点了重绘会 POST /inpaint',
        posted.length === 1 && posted[0][1].mask.indexOf('data:image/png') === 0,
        posted.length);
  check('结果区渲染出返回的图片',
        getEl('inpaint_result').innerHTML.includes('/api/v1/plugins/extensions/')
        && getEl('inpaint_result').innerHTML.includes('images/inpaint-1.png'),
        getEl('inpaint_result').innerHTML.slice(0, 120));
  check('清空遮罩后又不允许提交了',
        vm.runInContext('inpaintClearMask()', sandbox) === true
        && vm.runInContext('inpaintPayload()', sandbox) === null);

  console.log('\n=== 复制与关闭 ===');
  await vm.runInContext("copyField('modal-positive', '正向提示词')", sandbox);
  check('剪贴板不可用时退化为选中文本', getEl('modal-positive').selected === true);
  await vm.runInContext('closeDetail()', sandbox);
  check('弹窗可关闭', modal.hidden === true && !modal.classList.contains('open'));

  console.log('\n=== 结果：' + PASSED + ' passed, ' + FAILED + ' failed ===');
  process.exit(FAILED ? 1 : 0);
})().catch((error) => {
  console.error('测试脚本自身出错：', error);
  process.exit(1);
});
