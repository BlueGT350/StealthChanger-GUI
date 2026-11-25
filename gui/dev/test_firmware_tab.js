// Firmware tab: one card per board, and a warning when the helper version differs.
// Run: node gui/dev/test_firmware_tab.js
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'gui', 'js', 'app.js'), 'utf8');
const helperSrc = fs.readFileSync(path.join(ROOT, 'firmware', 'firmware_helper.py'), 'utf8');
const css = fs.readFileSync(path.join(ROOT, 'gui', 'style.css'), 'utf8');
const guiVersion = (appSrc.match(/var GUI_VERSION = '([^']+)'/) || [])[1];
assert.strictEqual(guiVersion, '0.2.14', 'GUI_VERSION');
assert.ok(helperSrc.indexOf("HELPER_VERSION = '0.2.14'") >= 0, 'HELPER_VERSION');

function Element(tag) {
  this.tag = tag;
  this.children = [];
  this.attrs = {};
  this.className = '';
  this._text = '';
  this.hidden = false;
  this.disabled = false;
  this.value = '';
  this.title = '';
  this.style = {};
  this.parentNode = null;
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this._listeners = {};
}
Element.prototype.setAttribute = function (k, v) {
  this.attrs[k] = String(v);
  if (k === 'class') this.className = String(v);
  if (k === 'hidden') this.hidden = true;
  if (k === 'disabled') this.disabled = true;
  if (k === 'value') this.value = String(v);
  if (k === 'title') this.title = String(v);
};
Element.prototype.getAttribute = function (k) {
  if (k === 'class') return this.className || null;
  if (!Object.prototype.hasOwnProperty.call(this.attrs, k)) return null;
  return this.attrs[k];
};
Element.prototype.appendChild = function (child) {
  child.parentNode = this;
  this.children.push(child);
  return child;
};
Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
Element.prototype.click = function () {
  (this._listeners.click || []).forEach(function (fn) { fn({}); });
};
Object.defineProperty(Element.prototype, 'textContent', {
  get: function () { return this._text; },
  set: function (v) {
    this._text = v == null ? '' : String(v);
    this.children = [];
  }
});
function classHas(el, name) {
  return (' ' + (el.className || '') + ' ').indexOf(' ' + name + ' ') >= 0;
}
function matches(el, sel) {
  if (sel.charAt(0) === '.') {
    return sel.slice(1).split('.').every(function (name) { return classHas(el, name); });
  }
  var attr = sel.match(/^\[([A-Za-z0-9_-]+)(?:="([^"]*)")?\]$/);
  if (attr) {
    var val = el.getAttribute(attr[1]);
    if (val == null) return false;
    return attr[2] == null ? true : val === attr[2];
  }
  var parts = sel.split('.');
  if (parts[0] && el.tag !== parts[0]) return false;
  for (var i = 1; i < parts.length; i++) if (!classHas(el, parts[i])) return false;
  return true;
}
function queryAll(el, sel) {
  var out = [];
  (el.children || []).forEach(function (child) {
    if (matches(child, sel)) out.push(child);
    queryAll(child, sel).forEach(function (hit) { out.push(hit); });
  });
  return out;
}
Element.prototype.querySelectorAll = function (sel) { return queryAll(this, sel); };
Element.prototype.querySelector = function (sel) { return queryAll(this, sel)[0] || null; };

function el(tag, attrs, kids) {
  var e = document.createElement(tag);
  if (attrs) Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (v == null || v === false) return;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  });
  (kids || []).forEach(function (c) {
    if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return e;
}

global.window = global;
global.document = {
  createElement: function (tag) { return new Element(tag); },
  createTextNode: function (text) { var n = new Element('#text'); n.textContent = text; return n; }
};
global.KTC = { app: { GUI_VERSION: guiVersion } };

let statusBody = null;
global.fetch = function () {
  return Promise.resolve({
    status: 200,
    text: function () { return Promise.resolve(JSON.stringify(statusBody)); }
  });
};

eval(fs.readFileSync(path.join(ROOT, 'gui', 'js', 'firmware.js'), 'utf8'));

function mcu(extra) {
  return Object.assign({
    name: 'mcu',
    label: 'Main Board',
    label_derived: 'Main Board',
    version: 'v0.13.0-90-gabc',
    update_state: 'behind',
    connection: 'usb',
    serial: '/dev/serial/by-id/usb-Klipper_stm32f446xx_AAAA1111BBBB2222-if00',
    config_saved: true,
    config_mcu: 'stm32f446xx',
    method: 'katapult_usb',
    method_choices: ['katapult_usb', 'direct'],
    flashable: true,
    block_reason: '',
    config_mcu_mismatch: false,
    direct_kind: 'dfu',
    canbus_uuid: '',
    canbus_interface: '',
    canbus_interface_default: false
  }, extra);
}

function status(version) {
  return {
    ok: true,
    version: version,
    host_version: 'v0.13.0-100-gabc',
    print_state: 'standby',
    print_blocked: false,
    klipper_dir_ok: true,
    klipper_dir: '/home/pi/klipper',
    flashtool: '/home/pi/katapult/scripts/flashtool.py',
    config_warnings: [],
    config_error: null,
    mcus: [
      mcu({ name: 'mcu', label: 'Main Board', update_state: 'behind' }),
      mcu({
        name: 'EBB0', label: 'T0', label_derived: 'T0', update_state: 'current',
        version: 'v0.13.0-100-gabc', connection: 'usb',
        serial: '/dev/serial/by-id/usb-Klipper_rp2040_CCCC3333DDDD4444-if00',
        config_mcu: 'rp2040', direct_kind: 'rp2040'
      }),
      mcu({
        name: 'extra', label: 'T1', label_derived: 'T1', update_state: 'behind',
        connection: 'can', serial: '', canbus_uuid: 'a1b2c3d4e5f6',
        method: 'katapult_can', method_choices: ['katapult_can'],
        config_saved: false, config_mcu: '', flashable: false,
        block_reason: 'No .config saved. The updater will not guess the chip.'
      })
    ]
  };
}

function buttonTexts(card) {
  return card.querySelectorAll('button').map(function (b) { return b.textContent; });
}
function inside(node, ancestor) {
  while (node) {
    if (node === ancestor) return true;
    node = node.parentNode;
  }
  return false;
}

let passed = 0;
function check(name, cond, detail) {
  if (!cond) {
    console.log('  FAIL ', name, detail == null ? '' : detail);
    process.exitCode = 1;
    return;
  }
  passed += 1;
  console.log('  ok   ', name);
}

const pane = document.createElement('div');
const hooks = { el: el, toast: function () {}, printBlock: function () { return null; }, confirmDlg: function () { return Promise.resolve(false); } };
let confirmArgs = null;
hooks.confirmDlg = function (title, text, list, yes) {
  confirmArgs = { title: title, text: text, list: list, yes: yes };
  return Promise.resolve(false);
};
KTC.firmware.build(pane, hooks);

function show(version) {
  statusBody = status(version);
  return KTC.firmware.onShow();
}

show('0.2.11').then(function () {
  const cards = pane.querySelectorAll('.fwboard');
  check('three board cards', cards.length === 3, cards.length);
  check('cards use the shared card style', cards.every(function (c) { return classHas(c, 'card') && classHas(c, 'fwboard'); }));
  const names = cards.map(function (c) { return c.getAttribute('data-mcu'); });
  check('one card per board', names.join(',') === 'mcu,EBB0,extra', names.join(','));
  const box = pane.querySelector('[data-fw="mcus"]');
  check('cards sit in a gapped stack', box && classHas(box, 'fwboards'));
  cards.forEach(function (card) {
    const texts = buttonTexts(card);
    const name = card.getAttribute('data-mcu');
    check(name + ' keeps Flash, Create, and Import inside its card',
      texts.indexOf('Flash') >= 0 && texts.indexOf('Create') >= 0 && texts.indexOf('Import') >= 0, texts);
    check(name + ' buttons are inside that card',
      card.querySelectorAll('button').every(function (btn) { return inside(btn, card); }));
  });
  const main = cards[0];
  const t0 = cards[1];
  const t1 = cards[2];
  check('Main Board name and update badge are in its card',
    main.querySelector('h4').textContent === 'Main Board'
    && main.querySelector('.badge.req').textContent === 'Firmware update required');
  check('T0 up to date badge is in its card',
    t0.querySelector('h4').textContent === 'T0' && t0.querySelector('.badge.ok').textContent === 'Up to date');
  check('saved .config keeps Edit and Copy to… inside that card',
    buttonTexts(main).indexOf('Edit') >= 0 && buttonTexts(main).some(function (t) { return t.indexOf('Copy to') === 0; }));
  check('a board without a .config does not show Edit or Copy',
    buttonTexts(t1).indexOf('Edit') < 0 && !buttonTexts(t1).some(function (t) { return t.indexOf('Copy to') === 0; }));
  check('T0 buttons are not inside the Main Board card',
    t0.querySelectorAll('button').every(function (btn) { return !inside(btn, main); }));
  const warn = pane.querySelector('[data-fw="helper-version"]');
  check('version mismatch shows the helper warning',
    warn && warn.hidden === false && warn.textContent === 'Firmware helper is out of date \u2013 it will restart automatically, or run setup.sh',
    warn && warn.textContent);
  check('board cards leave a gap and wrap on a phone',
    /\.fwboards\s*\{[^}]*gap:\s*14px/.test(css)
    && /@media \(max-width:\s*700px\)\s*\{[\s\S]*\.fwboard\s+\.fwlabel\s*\{\s*width:\s*100%/.test(css)
    && /\.card\s*\{[^}]*border:\s*1px solid var\(--line\)[^}]*border-radius:\s*8px/.test(css));
  function allText(node) {
    var text = node._text || '';
    (node.children || []).forEach(function (child) { text += ' ' + allText(child); });
    return text;
  }
  check('the menuconfig storage paragraph is gone',
    allText(pane).indexOf('Menuconfig for each MCU is stored') < 0
    && allText(pane).indexOf('Board names are saved in labels.json') < 0);
  const flashAll = pane.querySelector('[data-fw="flashall"]');
  check('Flash All label and tooltip',
    flashAll && flashAll.textContent === 'Flash All'
    && flashAll.title === 'Bring all boards that are behind up to the installed Klipper version',
    flashAll && [flashAll.textContent, flashAll.title]);
  check('progress bar is in the tab and hidden until a flash',
    pane.querySelector('[data-fw="progress"]') && pane.querySelector('[data-fw="progress"]').hidden === true
    && /\.fwprogress-track\.indeterminate::after[\s\S]*fwprogress-slide/.test(css)
    && /\.badge\.ahead\s*\{[^}]*var\(--amber\)/.test(css)
    && /@media \(max-width:\s*700px\)\s*\{[\s\S]*\.fwprogress-meta\s*\{\s*flex-direction:\s*column/.test(css));
  confirmArgs = null;
  flashAll.click();
  check('Flash All confirmation only lists boards that are behind and ready',
    confirmArgs && confirmArgs.yes === 'Flash All'
    && confirmArgs.title === 'Flash All boards that are behind?'
    && confirmArgs.list.length === 1 && confirmArgs.list[0].indexOf('Main Board') === 0, confirmArgs);
  return show(guiVersion);
}).then(function () {
  const warn = pane.querySelector('[data-fw="helper-version"]');
  check('matching helper version hides the warning', warn && warn.hidden === true && warn.textContent === '',
    warn && [warn.hidden, warn.textContent]);
  statusBody = status(guiVersion);
  statusBody.mcus.push(mcu({
    name: 'toolhead', label: 'Toolhead', label_derived: 'Toolhead', update_state: 'ahead',
    version: 'v0.13.0-200-gabc', connection: 'can', serial: '', canbus_uuid: 'abcdef012345',
    method: 'katapult_can', method_choices: ['katapult_can']
  }));
  statusBody.mcus.push(mcu({
    name: 'mystery', label: 'Mystery', update_state: 'unknown', version: '',
    config_saved: false, flashable: false, block_reason: 'No version'
  }));
  return KTC.firmware.onShow().then(function () {
    const ahead = pane.querySelector('[data-mcu="toolhead"]');
    const badge = ahead && ahead.querySelector('.badge.ahead');
    check('Firmware Ahead badge text and colour class',
      badge && badge.textContent === 'Firmware Ahead' && badge.title === ''
      && !ahead.querySelector('.badge.ok') && !ahead.querySelector('.badge.req'),
      badge && [badge.className, badge.textContent, badge.title]);
    confirmArgs = null;
    pane.querySelector('[data-fw="flashall"]').click();
    check('Flash All does not include ahead, current, or unknown boards',
      confirmArgs && confirmArgs.list.length === 1 && confirmArgs.list[0].indexOf('Main Board') === 0
      && confirmArgs.list.every(function (line) {
        return line.indexOf('Toolhead') !== 0 && line.indexOf('T0') !== 0 && line.indexOf('Mystery') !== 0;
      }), confirmArgs && confirmArgs.list);
    statusBody.job = {
      action: 'flash_all', stage: 'uploading', board_index: 2, board_total: 3,
      label: 'T0', mcu: 'EBB0', percent: 40
    };
    return KTC.firmware.onShow();
  }).then(function () {
    const box = pane.querySelector('[data-fw="progress"]');
    const title = pane.querySelector('[data-fw="progress-title"]');
    const stage = pane.querySelector('[data-fw="progress-stage"]');
    const track = pane.querySelector('[data-fw="progress-track"]');
    const bar = pane.querySelector('[data-fw="progress-bar"]');
    check('progress shows board N of M, the name, and the upload step',
      box && box.hidden === false && title.textContent === 'Board 2 of 3 \u00b7 T0'
      && stage.textContent === 'Uploading 40%' && track.className.indexOf('indeterminate') < 0
      && bar.style.width && bar.style.width !== '0%',
      title && [title.textContent, stage && stage.textContent, bar && bar.style.width, track && track.className]);
    statusBody.job = {
      action: 'flash_all', stage: 'building', board_index: 1, board_total: 3,
      label: 'Main Board', mcu: 'mcu', percent: null
    };
    return KTC.firmware.onShow();
  }).then(function () {
    const track = pane.querySelector('[data-fw="progress-track"]');
    const stage = pane.querySelector('[data-fw="progress-stage"]');
    check('a long step without a percent keeps moving',
      stage.textContent === 'Building' && track.className.indexOf('indeterminate') >= 0,
      stage && [stage.textContent, track && track.className]);
    return show('0.2.15');
  });
}).then(function () {
  const warn = pane.querySelector('[data-fw="helper-version"]');
  check('a newer helper still warns, because the versions differ',
    warn && warn.hidden === false && warn.textContent.indexOf('Firmware helper is out of date') === 0);
  global.fetch = function () { return Promise.reject(new Error('down')); };
  return KTC.firmware.onShow();
}).then(function () {
  const warn = pane.querySelector('[data-fw="helper-version"]');
  const box = pane.querySelector('[data-fw="mcus"]');
  const offline = box && box.querySelector('.banner.bad');
  check('helper offline hides the version warning',
    warn && warn.hidden === true && offline && offline.textContent.indexOf('not running') >= 0,
    warn && [warn.hidden, offline && offline.textContent]);
  console.log('\n' + passed + ' passed');
  if (process.exitCode) process.exit(process.exitCode);
}).catch(function (err) {
  console.error(err);
  process.exit(1);
});
