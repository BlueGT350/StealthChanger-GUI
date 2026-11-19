/* ktc-gui rev 2: StealthChanger / ktc-filament control page.
 * Talks to Moonraker over its JSON-RPC websocket. No build step, no CDN.
 * Flow: connect -> identify -> printer.info -> detect modules -> subscribe -> render on diffs. */
(function (KTC) {
  'use strict';
  var GUI_VERSION = '0.2.12';
  /* #brandLogo in index.html is the Voron Design header mark.
     Future hook: follow Mainsail's selected logo by swapping that SVG.
     Not implemented; data-logo stays "voron". */
  var C = KTC.cmd, ART = KTC.art;
  var $ = function (id) { return document.getElementById(id); };
  var DB_NS = 'ktc_gui';
  var DEF_HEAD = { model: 'anthead', extruder: 'orbiter25', extruderKeep: 'orbiter25', body: '#a3a7ab', accent: '#D41216', led: '', logoIdx: '1', nozIdx: '2-3' };
  /* Head LEDs: led '' = auto (the LED object whose name has T<n>), 'none' = no LED,
   * else a Klipper object name such as 'neopixel headlight_T0'. Index lists are
   * 1-based like SET_LED INDEX= (1 = first LED in the chain): '1', '2-3', '2,3'. */
  var LED_RE = /^(neopixel|dotstar|led|pca9533|pca9632) /;
  var MATERIALS = ['PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PC', 'NYLON'];
  var DEF_SWATCH = [['Red', 'D41216'], ['Black', '222222'], ['White', 'F2F2F2'], ['Grey', '8A8A8A'], ['Orange', 'FF8800'],
    ['Yellow', 'FDD835'], ['Green', '43A047'], ['Blue', '1E88E5'], ['Purple', '8E24AA'], ['Pink', 'F06292'],
    ['Brown', '6D4C41'], ['Natural', 'E8DCC0']];
  /* KTC-E commands registered without a description, so they are not in gcode.help */
  var NODESC = ['SET_TOOL_PARAMETER', 'RESET_TOOL_PARAMETER', 'SAVE_TOOL_PARAMETER', 'VERIFY_TOOL_DETECTED_ORIG'];
  var MAC = { fil: 'gcode_macro _FILAMENT_SETTINGS', orb: 'gcode_macro _ORBITER_SETTINGS',
    es: 'gcode_macro _ENDLESS_SPOOL_SETTINGS', setup: 'gcode_macro _SPOOL_SETUP_SHOW',
    use: 'gcode_macro _USAGE_SETTINGS', sync: 'gcode_macro KTC_TOOL_SYNC' };

  var S = {
    rpc: null, connected: false, ready: false, klippy: 'unknown', detecting: false,
    objects: new Set(), help: {}, st: {}, mods: {}, tools: [], names: {}, info: {},
    prefs: { heads: {} }, prefsStore: 'db', buttons: [], updaters: [], ui: {}, tab: null, homing: false,
    prevActive: null, sm: { ok: false, connected: false, spools: [], byId: {}, active: null, loading: false, err: null },
    conflicts: null
  };

  /* ---------- small helpers ---------- */
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
    (kids || []).forEach(function (c) { if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }
  function st(name) { return S.st[name] || {}; }
  function fmt(n, d) { return (n == null || isNaN(n)) ? '-' : Number(n).toFixed(d == null ? 0 : d); }
  function sgn(n) { n = Number(n) || 0; return (n >= 0 ? '+' : '') + n.toFixed(3); }
  function lc(s) { return String(s || '').toLowerCase(); }
  function has(cmd) {
    cmd = String(cmd).toUpperCase();
    if (S.help[cmd] != null) return true;
    if (S.objects.has('gcode_macro ' + cmd) || S.objects.has('gcode_macro ' + lc(cmd))) return true;
    return S.objects.has('toolchanger') && NODESC.indexOf(cmd) >= 0;
  }
  function settings() { return st('configfile').settings || {}; }
  function fvars() { return st(MAC.fil); }
  function svars() { return (st('save_variables').variables) || {}; }
  function tv(n, key) { return svars()['t' + n + '_' + key]; }
  // saved profile: t#_temp_profile, older configs used t#_temp_type
  function tprof(n) { var p = tv(n, 'temp_profile'); if (p == null || p === '') p = tv(n, 'temp_type'); return p == null || p === '' ? null : String(p).toUpperCase(); }
  function vcmp(a, b) { // same major.minor
    var pa = String(a || '').split('.'), pb = String(b || '').split('.');
    return pa[0] === pb[0] && pa[1] === pb[1];
  }

  /* ---------- console and banners ---------- */
  function conAdd(text, cls) {
    var box = $('conLines');
    String(text).split('\n').forEach(function (line) {
      if (!line) return;
      var c = cls || (/^!!/.test(line) ? 'err' : '');
      box.appendChild(el('div', { class: c, text: line }));
    });
    while (box.childNodes.length > 400) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }
  function toast(msg, kind) {
    var b = el('div', { class: 'banner toast ' + (kind || 'bad'), text: msg, role: 'alert' });
    $('banners').appendChild(b);
    setTimeout(function () { b.remove(); }, 8000);
  }
  function banner(id, msg, kind) {
    var b = document.querySelector('[data-banner="' + id + '"]');
    if (!msg) { if (b) b.remove(); return; }
    if (!b) { b = el('div', { 'data-banner': id }); $('banners').appendChild(b); }
    b.className = 'banner ' + (kind || '');
    b.textContent = msg;
  }

  /* ---------- sending G-code ---------- */
  function send(script) {
    if (Array.isArray(script)) script = script.join('\n');
    conAdd(script.split('\n').map(function (l) { return '> ' + l; }).join('\n'), 'cmd');
    return S.rpc.call('printer.gcode.script', { script: script });
  }
  function run(script, btn) {
    if (btn) { btn.dataset.busy = '1'; updateButtons(); }
    return send(script).then(function () {}, function (e) {
      var msg = String(e.message || e), m = /SAVE_CONFIG section '([^']+)' option '([^']+)' conflicts with included value/.exec(msg);
      if (m) {
        banner('cfgerr', 'SAVE_CONFIG failed: [' + m[1] + '] ' + m[2] + ' is also set in an included file, so Klipper will not write it. ' +
          'Copy the value into the #*# block at the bottom of printer.cfg (under "#*# [' + m[1] + ']"), then remove the line from the included file, both before any restart. ' +
          'The Calibration tab lists every clash.', 'bad');
        checkConflicts(true);
      }
      toast('Klipper refused: ' + msg);
    }).then(function () { if (btn) { delete btn.dataset.busy; updateButtons(); } });
  }

  /* ---------- confirm / value dialogs ---------- */
  function confirmDlg(title, text, cmd, yesLabel, noLabel) {
    return new Promise(function (resolve) {
      var d = $('dlgConfirm');
      $('cfTitle').textContent = title;
      $('cfText').textContent = text;
      $('cfCmd').textContent = Array.isArray(cmd) ? cmd.join('\n') : (cmd || '');
      $('cfYes').textContent = yesLabel || 'Run';
      $('cfNo').textContent = noLabel || 'Cancel';
      d.returnValue = '';
      d.onclose = function () { resolve(d.returnValue === 'yes'); };
      d.showModal();
    });
  }
  function valueDlg(o) { // {title, label, value, min, max, step, range, offLabel}
    return new Promise(function (resolve) {
      var d = $('dlgValue'), inp = $('valInput'), rng = $('valRange');
      $('valTitle').textContent = o.title;
      $('valLabel').firstChild.textContent = o.label;
      inp.min = o.min; inp.max = o.max; inp.step = o.step || 1; inp.value = o.value;
      inp.title = 'Allowed ' + o.min + ' to ' + o.max;
      inp.placeholder = o.min + ' to ' + o.max;
      rng.hidden = !o.range;
      if (o.range) { rng.min = o.min; rng.max = o.max; rng.value = o.value; rng.oninput = function () { inp.value = rng.value; }; }
      inp.oninput = function () { if (o.range) rng.value = inp.value; };
      $('valOff').textContent = o.offLabel || 'Off';
      $('valErr').textContent = '';
      d.returnValue = '';
      $('valOk').onclick = function (ev) {
        var v = Number(inp.value);
        if (inp.value === '' || isNaN(v) || v < o.min || v > o.max) {
          ev.preventDefault(); $('valErr').textContent = 'Enter a value from ' + o.min + ' to ' + o.max;
        }
      };
      d.onclose = function () {
        if (d.returnValue === 'ok') resolve(Number(inp.value));
        else if (d.returnValue === 'off') resolve(0);
        else resolve(null);
      };
      d.showModal();
      inp.focus(); inp.select();
    });
  }

  /* ---------- locks ---------- */
  function printState() { return st('print_stats').state || 'standby'; }
  function isPaused() { return printState() === 'paused' || !!st('pause_resume').is_paused; }
  function lockReason(kind) {
    if (!S.connected) return 'Not connected to Moonraker';
    if (!S.ready) return 'Klipper is not ready (' + S.klippy + ')';
    if (kind === 'motion' || kind === 'change') {
      if (S.homing) return 'Homing...';
      if (printState() === 'printing') return 'Locked while printing';
      if (isPaused()) return 'Locked while a print is paused (resume or cancel first)';
      if (st('toolchanger').status === 'changing') return 'Wait, a tool change is in progress';
      if (kind === 'change' && st('toolchanger').status === 'error') return 'Toolchanger is in error, use Recovery first';
    }
    return null;
  }
  /* KTC-E aborts a tool change on unhomed axes unless on_axis_not_homed: home,
   * and the calibration macros move to fixed XY. Buttons with home:true check
   * toolhead.homed_axes at click time and put G28 in front when needed.
   * A tool change itself is not sent until X, Y and Z are homed: sending T#
   * while unhomed resets KTC tool state. The user is asked first. */
  function needsHome() {
    var h = lc(st('toolhead').homed_axes);
    return ['x', 'y', 'z'].some(function (a) { return h.indexOf(a) < 0; });
  }
  function isToolchange(script) {
    var lines = (Array.isArray(script) ? script : String(script).split('\n'))
      .map(function (l) { return l.trim(); }).filter(Boolean);
    return lines.length > 0 && lines.every(function (l) {
      return /^(T\d+|SELECT_TOOL|UNSELECT_TOOL)\b/i.test(l);
    });
  }
  function withHome(script) {
    if (!needsHome() || !has('G28')) return script;
    return ['G28'].concat(Array.isArray(script) ? script : String(script).split('\n'));
  }
  var homeWait = [];
  function watchHomed(fn) {
    homeWait.push(fn);
    return function () {
      var i = homeWait.indexOf(fn);
      if (i >= 0) homeWait.splice(i, 1);
    };
  }
  /* Yes on the homing dialog: G28, then the tool change once homed_axes is xyz
   * or the command finishes. An error is shown and the tool change is not sent. */
  function homeThen(script, btn) {
    if (!needsHome()) { run(script, btn); return; }
    S.homing = true;
    if (btn) btn.dataset.busy = '1';
    banner('home', 'Homing...', 'info');
    updateButtons();
    var settled = false;
    function cleanup() {
      S.homing = false;
      if (btn) delete btn.dataset.busy;
      banner('home', null);
      updateButtons();
    }
    function succeed() {
      if (settled) return;
      settled = true;
      stop();
      cleanup();
      run(script, btn);
    }
    function fail(e) {
      if (settled) return;
      settled = true;
      stop();
      cleanup();
      toast('Klipper refused: ' + String((e && e.message) || e));
    }
    var stop = watchHomed(function () { if (!needsHome()) succeed(); });
    send('G28').then(succeed, fail);
  }
  var HOME_NOTE = 'The printer is not homed (homed: ', HOME_NOTE2 = '), so G28 runs first.';
  /* o: {label, title, kind: free|motion|change, cmd: string|array|fn, confirm: obj|fn, when: fn -> reason|null, cls, fn,
   *     home: true = prepend G28 when X, Y or Z is not homed, intercept: fn(script) -> Promise<bool> run it} */
  function mkBtn(o) {
    var b = el('button', { type: 'button', class: 'btn ' + (o.cls || ''), text: o.label });
    b._o = o;
    b.addEventListener('click', function () {
      if (b.disabled) return;
      if (o.fn) { o.fn(b); return; }
      var script;
      try { script = typeof o.cmd === 'function' ? o.cmd() : o.cmd; } catch (e) { toast(e.message || String(e)); return; }
      if (!script) return;
      if (needsHome() && (o.kind === 'change' || isToolchange(script))) {
        confirmDlg('Homing required', 'The printer must be homed (XYZ) before changing tools. Home now?',
          '', 'Yes', 'No').then(function (ok) { if (ok) homeThen(script, b); });
        return;
      }
      var homing = false;
      if (o.home && needsHome() && has('G28')) { script = withHome(script); homing = true; }
      var hn = homing ? HOME_NOTE + ((st('toolhead').homed_axes || 'none').toUpperCase() || 'none') + HOME_NOTE2 : '';
      var go = function () {
        if (!o.intercept) { run(script, b); return; }
        Promise.resolve(o.intercept(script)).then(function (ok) { if (ok) run(script, b); });
      };
      if (o.confirm) {
        var cf = typeof o.confirm === 'function' ? o.confirm() : o.confirm;
        confirmDlg(cf.title || o.label, (hn ? hn + ' ' : '') + cf.text, script, cf.yes).then(function (ok) { if (ok) go(); });
      } else if (homing) {
        confirmDlg('Home first?', hn + ' Then: ' + (o.title || o.label) + '.', script, 'Home and run').then(function (ok) { if (ok) go(); });
      } else go();
    });
    S.buttons.push(b);
    return b;
  }
  function updateButtons() {
    S.buttons = S.buttons.filter(function (b) { return b.isConnected; });
    S.buttons.forEach(function (b) {
      var o = b._o, why = lockReason(o.kind || 'free');
      if (!why && o.when) { try { why = o.when(); } catch (e) { why = String(e.message || e); } }
      if (!why && b.dataset.busy) why = 'Running...';
      b.disabled = !!why;
      b.classList.toggle('locked', !!why && /^Locked/.test(why));
      b.title = why || o.title || '';
    });
  }

  /* ---------- prefs (Moonraker database, localStorage fallback) ---------- */
  function headPref(n) { return ART.normalizeHead(Object.assign({}, DEF_HEAD, (S.prefs.heads || {})[n] || {})); }
  function loadPrefs() {
    return S.rpc.call('server.database.get_item', { namespace: DB_NS, key: 'prefs' }).then(function (r) {
      S.prefsStore = 'db'; S.prefs = (r && r.value) || { heads: {} };
    }, function (e) {
      if (e.code === 404) { S.prefsStore = 'db'; S.prefs = { heads: {} }; return; }
      S.prefsStore = 'local';
      try { S.prefs = JSON.parse(localStorage.getItem('ktc_gui_prefs')) || { heads: {} }; } catch (x) { S.prefs = { heads: {} }; }
    });
  }
  function savePrefs() {
    var p = S.prefsStore === 'db'
      ? S.rpc.call('server.database.post_item', { namespace: DB_NS, key: 'prefs', value: S.prefs })
      : Promise.reject(new Error('no db'));
    return p.catch(function () {
      S.prefsStore = 'local';
      localStorage.setItem('ktc_gui_prefs', JSON.stringify(S.prefs));
    });
  }

  /* ---------- connection ---------- */
  function hostSetting() {
    var q = new URLSearchParams(location.search).get('host');
    return q || localStorage.getItem('ktc_gui_host') || location.host;
  }
  function wsUrl(host) { return (location.protocol === 'https:' ? 'wss://' : 'ws://') + host + '/websocket'; }
  function connect() {
    var host = hostSetting();
    if (!host) {
      banner('conn', 'No printer address. Open settings (gear icon) and enter the Moonraker address, e.g. printer.local:7125', 'bad');
      return;
    }
    var rpc = S.rpc = new KTC.Rpc(wsUrl(host));
    rpc.on('_open', function () {
      S.connected = true; setChip('connChip', 'connected', 'ok', 'Moonraker at ' + host);
      banner('conn', null);
      rpc.call('server.connection.identify', { client_name: 'ktc-gui', version: GUI_VERSION, type: 'web', url: location.href })
        .catch(function () {})
        .then(loadPrefs)
        .then(loadRepoVersion)
        .then(function () { checkServed('connect', true); })
        .then(function () { return rpc.call('server.gcode_store', { count: 50 }).catch(function () { return null; }); })
        .then(function (r) {
          $('conLines').textContent = '';
          ((r && r.gcode_store) || []).forEach(function (g) {
            conAdd((g.type === 'command' ? '> ' : '') + g.message, g.type === 'command' ? 'cmd' : '');
          });
        })
        .then(startKlippy);
    });
    rpc.on('_close', function () {
      var was = S.connected;
      S.connected = false; S.ready = false;
      setChip('connChip', 'disconnected', 'bad', 'Reconnecting to ' + host);
      banner('conn', 'Lost connection to Moonraker at ' + host + ', reconnecting...', 'bad');
      if (was) updateAll();
    });
    rpc.on('notify_klippy_ready', function () { detect(); loadRepoVersion(); checkServed('klippy ready', true); });
    rpc.on('notify_update_refreshed', function (p) {
      var vi = (p && p[0] && p[0].version_info) || null;
      if (vi) { S.repo = repoInfo(vi[UPDATER]); showRepoVersion(); }
      checkServed('update refreshed', true);
    });
    rpc.on('notify_update_response', function (p) {   // Update Manager progress; check when it is done
      if (p && p[0] && p[0].complete) setTimeout(function () { loadRepoVersion(); checkServed('update done', true); }, 1500);
    });
    rpc.on('notify_klippy_shutdown', function () { S.ready = false; S.klippy = 'shutdown'; klippyBanner(); updateAll(); startKlippy(); });
    rpc.on('notify_klippy_disconnected', function () {
      S.ready = false; S.klippy = 'disconnected'; klippyBanner(); updateAll();
      clearTimeout(kTimer); kTimer = setTimeout(startKlippy, 1500);
    });
    rpc.on('notify_status_update', function (p) { mergeStatus(p[0]); scheduleUpdate(); });
    rpc.on('notify_gcode_response', function (p) { conAdd(p[0]); });
    rpc.on('notify_active_spool_set', function (p) { S.sm.active = p && p[0] ? p[0].spool_id : null; smLoad(); scheduleUpdate(); });
    rpc.on('notify_spoolman_status_changed', function (p) {
      S.sm.connected = !!(p && p[0] && p[0].spoolman_connected);
      if (S.sm.connected) smCheck(); else scheduleUpdate();
    });
    rpc.connect();
  }
  function klippyBanner(msg) {
    if (S.ready) { banner('klippy', null); return; }
    banner('klippy', 'Klipper is ' + S.klippy + (msg ? ': ' + msg : '') + '. Waiting for it to be ready...',
      S.klippy === 'shutdown' || S.klippy === 'error' ? 'bad' : '');
    setChip('stateChip', 'klipper ' + S.klippy, 'bad');
  }
  var kTimer = null;
  function startKlippy() {
    if (!S.connected) return;
    clearTimeout(kTimer);
    S.rpc.call('printer.info').then(function (info) {
      $('printerName').textContent = info.hostname ? '@ ' + info.hostname : '';
      S.klippy = info.state;
      if (info.state === 'ready') return detect();
      S.ready = false; klippyBanner(info.state_message); updateAll();
      kTimer = setTimeout(startKlippy, 2000);
    }, function () { S.klippy = 'disconnected'; klippyBanner(); kTimer = setTimeout(startKlippy, 2000); });
  }
  function mergeStatus(diff) {
    Object.keys(diff || {}).forEach(function (k) { S.st[k] = Object.assign(S.st[k] || {}, diff[k]); });
    if (!diff || !diff.toolhead || diff.toolhead.homed_axes == null) return;
    homeWait.slice().forEach(function (fn) { try { fn(); } catch (e) { console.error(e); } });
  }

  /* ---------- detection ---------- */
  function findObj(cands) {
    for (var i = 0; i < cands.length; i++) {
      if (S.objects.has(cands[i])) return cands[i];
      var low = lc(cands[i]);
      var hit = Array.from(S.objects).find(function (o) { return lc(o) === low; });
      if (hit) return hit;
    }
    return null;
  }
  function detect() {
    if (S.detecting) return S.detecting;
    var rpc = S.rpc;
    S.detecting = Promise.all([
      rpc.call('printer.objects.list'),
      rpc.call('printer.gcode.help').catch(function () { return {}; })
    ]).then(function (r) {
      S.objects = new Set(r[0].objects || []);
      S.help = {};
      Object.keys(r[1] || {}).forEach(function (k) { S.help[k.toUpperCase()] = r[1][k]; });
      var q = { configfile: ['settings', 'config', 'save_config_pending'] };
      ['toolchanger', MAC.fil, MAC.orb, MAC.es, MAC.use].forEach(function (o) { if (S.objects.has(o)) q[o] = null; });
      return rpc.call('printer.objects.query', { objects: q });
    }).then(function (r) {
      S.st = r.status || {};
      if (!S.objects.has('toolchanger')) {
        S.ready = true; S.klippy = 'ready'; S.mods = { warn: [] }; S.tools = [];
        banner('klippy', null);
        banner('detect', 'No [toolchanger] object found. This GUI needs klipper-toolchanger-easy (KTC-E) installed and configured.', 'bad');
        buildEmpty('No toolchanger found.');
        return null;
      }
      banner('detect', null);
      var tc = st('toolchanger');
      S.tools = (tc.tool_numbers || []).slice();
      S.names = {};
      (tc.tool_names || []).forEach(function (nm, i) { S.names[S.tools[i]] = nm; });
      var q = {};
      S.tools.forEach(function (n) { q[S.names[n]] = null; });
      return rpc.call('printer.objects.query', { objects: q }).then(function (r2) {
        mergeStatus(r2.status);
        detectModules();
        resolveTools();
        return subscribe();
      });
    }).then(function () {
      S.detecting = false;
    }, function (e) {
      S.detecting = false;
      console.warn('detect failed', e);
      if (S.connected) {
        banner('detect', 'Could not read the printer: ' + (e.message || e) + '. Retrying...', 'bad');
        clearTimeout(kTimer); kTimer = setTimeout(startKlippy, 3000);
      }
    });
    return S.detecting;
  }
  function cfgHas(section) {
    var cfg = st('configfile').config || {}, low = lc(section);
    return Object.keys(cfg).some(function (k) { return lc(k) === low; });
  }
  function detectModules() {
    var m = S.mods = { base: true, warn: [] };
    m.fil = S.objects.has(MAC.fil);
    m.orb = S.objects.has(MAC.orb);
    m.es = S.objects.has(MAC.es);
    m.filV = m.fil ? st(MAC.fil).module_version : null;
    m.orbV = m.orb ? st(MAC.orb).module_version : null;
    m.esV = m.es ? st(MAC.es).module_version : null;
    m.use = S.objects.has(MAC.use);
    m.useV = m.use ? st(MAC.use).module_version : null;
    if (m.use && !m.fil) m.warn.push('Usage add-on found without the filament module. Include filament_macros.cfg before usage_addon.cfg.');
    if (m.use && m.useV && !vcmp(m.useV, GUI_VERSION))
      m.warn.push('Usage add-on is v' + m.useV + ' but this GUI is v' + GUI_VERSION + '. Update both together.');
    if (!m.fil && cfgHas(MAC.fil)) m.warn.push('[' + MAC.fil + '] is in the config but Klipper did not load it. Check filament_macros.cfg is included.');
    if (m.fil && m.filV && !vcmp(m.filV, GUI_VERSION))
      m.warn.push('Filament module is v' + m.filV + ' but this GUI is v' + GUI_VERSION + '. Some buttons may not match. Update both together.');
    if (m.fil && !m.filV) m.warn.push('Filament module has no module_version. Is filament_macros.cfg out of date?');
    if (m.orb) {
      if (!m.fil) {
        m.warn.push('Orbiter add-on found but the filament module is missing. The add-on needs it, so Orbiter controls are hidden.');
        m.orbOff = true;
      } else {
        var req = st(MAC.orb).requires_filament_version;
        if (req && m.filV && !vcmp(req, m.filV))
          m.warn.push('Orbiter add-on v' + m.orbV + ' expects filament module v' + req + ' but v' + m.filV + ' is installed.');
      }
    }
    if (m.es && !m.fil) m.warn.push('Endless spool settings found without the filament module.');
    if (!has('SELECT_TOOL')) m.warn.push('SELECT_TOOL is not registered. Is KTC-E loaded?');
    banner('mods', m.warn.length ? m.warn.join('\n') : null, '');
  }
  function resolveTools() {
    var cfg = settings();
    S.info = {};
    S.tools.forEach(function (n) {
      var t = st(S.names[n]), i = { n: n, name: S.names[n], ext: t.extruder || null };
      var fan = t.fan;
      if (fan) i.fan = fan.indexOf(' ') >= 0 ? findObj([fan]) : findObj(['fan_generic ' + fan, fan]);
      i.fanName = fan ? String(fan).replace(/^fan_generic /, '') : null;
      // hotend fan: the heater_fan whose heater list holds this extruder
      Object.keys(cfg).forEach(function (sec) {
        if (i.hfan || !/^heater_fan /.test(sec) || !i.ext) return;
        var hs = cfg[sec].heater;
        hs = Array.isArray(hs) ? hs : String(hs || 'extruder').split(',');
        if (hs.map(function (h) { return lc(String(h).trim()); }).indexOf(lc(i.ext)) >= 0) i.hfan = findObj([sec]);
      });
      if (!i.hfan) i.hfan = findObj(['heater_fan T' + n + '_hotend_fan', 'heater_fan hotend_fan_T' + n]);
      // filament sensor
      var names = [];
      if (S.mods.fil) {
        var fv = st(MAC.fil), map = fv.sensor_map || {};
        if (Object.prototype.hasOwnProperty.call(map, String(n))) names = map[String(n)] ? [map[String(n)]] : [];
        else names = [String(fv.sensor_pattern || 'filament_sensor_T{n}').replace('{n}', n)];
      } else names = ['filament_sensor_T' + n, 'T' + n + '_filament_sensor'];
      names.forEach(function (nm) {
        if (!i.sensor) i.sensor = findObj(['filament_switch_sensor ' + nm, 'filament_motion_sensor ' + nm]);
      });
      var ec = cfg[lc(i.ext)] || {};
      i.led = ledFor(n);
      i.maxTemp = ec.max_temp || 300;
      i.minExtrude = ec.min_extrude_temp != null ? ec.min_extrude_temp : 170;
      S.info[n] = i;
    });
  }
  /* ---------- head LEDs ---------- */
  function ledObjs() { return Array.from(S.objects).filter(function (o) { return LED_RE.test(o); }).sort(); }
  function ledFor(n, pick) {
    var v = String(pick != null ? pick : headPref(n).led || '').trim();
    if (lc(v) === 'none') return null;
    if (v) return findObj([v, 'neopixel ' + v]);
    var re = new RegExp('(^|[^a-z0-9])t' + n + '($|[^0-9])', 'i');
    return ledObjs().find(function (o) { return re.test(o.replace(LED_RE, '')); }) || null;
  }
  function parseIdx(s, fallback) {
    var out = [];
    String(s == null ? '' : s).split(',').forEach(function (part) {
      var m = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!m) return;
      var a = +m[1], b = m[2] ? +m[2] : a;
      for (var k = Math.min(a, b); k <= Math.max(a, b) && out.length < 64; k++) if (k >= 1) out.push(k - 1);
    });
    return out.length ? out : fallback;
  }
  /* average [r,g,b,w] over the indices, white added to every channel, then
   * split into a full-strength colour and a brightness (the brightest channel) */
  function ledMix(data, idx) {
    var acc = [0, 0, 0], cnt = 0;
    (idx || []).forEach(function (k) {
      var c = data[k]; if (!c) return;
      var w = +c[3] || 0;
      for (var j = 0; j < 3; j++) acc[j] += Math.min(1, (+c[j] || 0) + w);
      cnt++;
    });
    if (!cnt) return null;
    acc = acc.map(function (x) { return x / cnt; });
    var a = Math.max(acc[0], acc[1], acc[2]);
    if (a <= 0.004) return { col: '#ffffff', a: 0 };
    return { col: 'rgb(' + acc.map(function (x) { return Math.round(x / a * 255); }).join(',') + ')', a: Math.min(1, a) };
  }
  function ledInfo(n) {
    var i = S.info[n] || {};
    return i.led ? i.led + ' (logo LED ' + (headPref(n).logoIdx || '1') + ', nozzle LED ' + (headPref(n).nozIdx || '2-3') + ')' : 'no LED mapped';
  }
  function subscribe() {
    var objs = { toolchanger: null, configfile: ['save_config_pending', 'save_config_pending_items'], print_stats: ['state', 'filename'],
      pause_resume: ['is_paused'], toolhead: ['homed_axes', 'extruder'], webhooks: ['state', 'state_message'] };
    ['save_variables', 'tool_probe_endstop', 'tools_calibrate', MAC.fil, MAC.orb, MAC.es, MAC.setup, MAC.use, MAC.sync,
     'gcode_macro _CALIBRATION_SWITCH'].forEach(function (o) { if (S.objects.has(o)) objs[o] = null; });
    S.tools.forEach(function (n) {
      var i = S.info[n];
      objs[i.name] = null;
      if (i.ext && S.objects.has(i.ext)) objs[i.ext] = ['temperature', 'target', 'power'];
      if (i.fan) objs[i.fan] = ['speed', 'rpm'];
      if (i.hfan) objs[i.hfan] = ['speed', 'rpm'];
      if (i.sensor) objs[i.sensor] = ['filament_detected', 'enabled'];
    });
    ledObjs().forEach(function (o) { objs[o] = ['color_data']; });
    return S.rpc.call('printer.objects.subscribe', { objects: objs }).then(function (r) {
      mergeStatus(r.status);
      S.ready = true; S.klippy = 'ready';
      banner('klippy', null);
      buildAll();
      updateAll();
      smCheck();
      if (has('SAVE_CONFIG')) checkConflicts(true);
    });
  }

  /* ---------- usage add-on + Spoolman (items 9 and 13) ----------
   * Local mode: the usage add-on counts mm per tool into variables.cfg
   * (t#_used_mm). Spoolman mode: each tool gets a Spoolman spool id
   * (t#_spool_id) and the add-on tells Moonraker which spool is active after
   * every tool change, so Moonraker's own Spoolman code books the usage.
   * Spoolman itself is reached only through Moonraker's proxy. */
  var DENSITY = { PLA: 1.24, PETG: 1.27, ABS: 1.04, ASA: 1.07, TPU: 1.21, PC: 1.20, NYLON: 1.14 };
  function usageMode() { return lc(svars().ktc_usage_mode) === 'spoolman' ? 'spoolman' : 'local'; }
  function toolSpoolId(n) { var v = Number(tv(n, 'spool_id')); return v > 0 ? v : null; }
  function usedMm(n) { return Number(tv(n, 'used_mm')) || 0; }
  function mmToG(mm, dens) { return mm * Math.PI * 0.875 * 0.875 / 1000 * (dens || 1.24); } // 1.75 mm filament
  function spoolLabel(sp) {
    if (!sp) return '-';
    var f = sp.filament || {};
    return '#' + sp.id + ' ' + [f.vendor && f.vendor.name, f.name || f.material].filter(Boolean).join(' ');
  }
  function smUsable() { return S.mods.use && S.sm.ok && S.sm.connected; }
  function usageLine(n) {
    if (!S.mods.use) return null;
    if (usageMode() === 'spoolman') {
      var id = toolSpoolId(n);
      if (!id) return { text: 'no Spoolman spool', title: 'Spoolman mode: pick a spool for T' + n + ' in the Filament tab' };
      var sp = S.sm.byId[id];
      if (!sp) return { text: 'spool #' + id, title: S.sm.connected ? 'Spool #' + id + ' not found in Spoolman' : 'Spoolman is not connected' };
      return { text: fmt(sp.remaining_weight) + ' g left', title: spoolLabel(sp) + ', ' + fmt(sp.used_weight) + ' g used (Spoolman)' +
        (S.sm.active === id ? '. Active spool' : '') };
    }
    var mm = usedMm(n);
    var g = mmToG(mm, DENSITY[String(tv(n, 'material') || '').toUpperCase()]);
    return { text: fmt(mm / 1000, 2) + ' m used', title: 'Used since the last spool reset: ' + fmt(mm / 1000, 2) + ' m, about ' + fmt(g) + ' g (local count)' };
  }
  function smProxy(method, path, body) {
    var a = { request_method: method, path: path, use_v2_response: true };
    if (body) a.body = body;
    return S.rpc.call('server.spoolman.proxy', a).then(function (r) {
      if (r && r.error) throw new Error('Spoolman ' + (r.error.status_code || '') + ': ' + (r.error.message || 'error'));
      return r && Object.prototype.hasOwnProperty.call(r, 'response') ? r.response : r;
    });
  }
  function smCheck() {
    return S.rpc.call('server.info').then(function (info) {
      S.sm.ok = (info.components || []).indexOf('spoolman') >= 0;
      if (!S.sm.ok) { S.sm.connected = false; return null; }
      return S.rpc.call('server.spoolman.status').then(function (r) {
        S.sm.connected = !!r.spoolman_connected;
        S.sm.active = r.spool_id != null ? r.spool_id : null;
        if (S.sm.connected) return smLoad();
      });
    }).catch(function (e) { S.sm.err = e.message || String(e); }).then(scheduleUpdate);
  }
  function smLoad() {
    if (!S.sm.ok || !S.sm.connected || S.sm.loading) return Promise.resolve();
    S.sm.loading = true;
    return smProxy('GET', '/v1/spool').then(function (list) {
      S.sm.spools = (list || []).filter(function (s) { return !s.archived; }).sort(function (a, b) { return a.id - b.id; });
      S.sm.byId = {};
      (list || []).forEach(function (s) { S.sm.byId[s.id] = s; });
      S.sm.err = null;
    }, function (e) { S.sm.err = e.message || String(e); }).then(function () { S.sm.loading = false; scheduleUpdate(); });
  }
  function smFilaments() { return smProxy('GET', '/v1/filament'); }
  // keep remaining weights fresh while printing (Moonraker reports usage to Spoolman as it prints)
  setInterval(function () {
    if (!document.hidden && S.ready && smUsable() && usageMode() === 'spoolman') smLoad();
  }, 15000);

  function assignSpool(n, id) {
    var lines = ['SET_TOOL_SPOOL TOOL=' + n + ' ID=' + (id || 0)];
    var sp = id ? S.sm.byId[id] : null;
    if (sp && S.mods.fil && has('SET_TOOL_FILAMENT')) {
      var f = sp.filament || {};
      var before = { material: tv(n, 'material') || '', color: tv(n, 'color') || '', spool: tv(n, 'spool') || '', profile: tprof(n) };
      var after = Object.assign({}, before, { material: f.material || before.material, color: f.color_hex || before.color,
        spool: spoolLabel(sp).slice(0, 40) });
      lines = lines.concat(C.editorScript(n, before, after));
    }
    return lines;
  }
  function buildUsagePanel(p) {
    if (!S.mods.use) {
      add(p, grp('Filament usage', [el('span', { class: 'muted small', text: 'Not installed.' })],
        'Add usage/usage_addon.cfg (see the README) to count filament per tool' + (S.sm.ok ? ' and link tools to Spoolman spools. Moonraker has Spoolman set up.' : '.')));
      return;
    }
    var g = el('div', { class: 'grp usage', id: 'usagePanel' });
    g.appendChild(el('h4', { text: 'Filament usage' }));
    var seg = el('div', { class: 'seg', role: 'radiogroup', id: 'usageSeg', title: 'Where filament use is tracked' });
    var segBtns = [['local', 'Local'], ['spoolman', 'Spoolman']].map(function (m) {
      var b = mkBtn({ label: m[1], cmd: 'KTC_USAGE_MODE MODE=' + m[0].toUpperCase(),
        title: m[0] === 'local' ? 'Count used filament per tool in variables.cfg' : 'Book used filament to Spoolman spools through Moonraker',
        when: function () {
          if (m[0] !== 'spoolman') return null;
          if (!S.sm.ok) return 'Spoolman is not set up in Moonraker (no [spoolman] section in moonraker.conf)';
          if (!S.sm.connected) return 'Moonraker cannot reach the Spoolman server';
          return null;
        } });
      b.className = ''; b.dataset.v = m[0]; b.setAttribute('role', 'radio'); seg.appendChild(b); return [m[0], b];
    });
    var smChip = el('span', { class: 'chip' });
    var refresh = mkBtn({ label: 'Refresh', title: 'Reload spools from Spoolman', fn: function () { smCheck(); } });
    var addBtn = mkBtn({ label: 'Add spool', title: 'Create a new spool in Spoolman', when: function () { return smUsable() ? null : 'Spoolman is not connected'; },
      fn: function () { openSpoolDlg(null); } });
    g.appendChild(el('div', { class: 'btns' }, [el('span', { class: 'muted small', text: 'Track with' }), seg, smChip, refresh, addBtn]));
    var note = el('div', { class: 'muted small note' });
    g.appendChild(note);
    var tbl = el('table', { class: 'ftab utab' });
    g.appendChild(el('div', { class: 'tblwrap' }, [tbl]));
    var rows = {};
    S.tools.forEach(function (n) {
      var r = rows[n] = { dot: el('span', { class: 'dot' }), name: el('span'), amt: el('span'), acts: el('td', { class: 'acts' }) };
      r.sel = el('select', { title: 'Spoolman spool on T' + n, 'data-tool': n });
      r.sel.addEventListener('change', function () {
        var id = Number(r.sel.value) || 0;
        var why = lockReason('free');
        if (why) { toast(why); r.key = null; return; }
        var script = assignSpool(n, id);
        confirmDlg('Spool for T' + n + '?', id ? 'T' + n + ' gets ' + spoolLabel(S.sm.byId[id]) + '. Its used counter restarts and the spool editor fields follow the Spoolman spool.' :
          'T' + n + ' will have no Spoolman spool. Usage on this tool is not booked to Spoolman.', script, 'Assign').then(function (ok) {
          if (ok) run(script); else r.key = null;
          scheduleUpdate();
        });
      });
      r.reset = mkBtn({ label: 'New spool', title: 'Zero the T' + n + ' used counter (RESET_TOOL_USAGE TOOL=' + n + ')', cmd: 'RESET_TOOL_USAGE TOOL=' + n,
        confirm: { title: 'New spool on T' + n + '?', yes: 'Reset', text: 'The T' + n + ' used counter goes back to 0.' } });
      r.edit = mkBtn({ label: 'Edit spool', title: 'Edit this Spoolman spool', when: function () { return smUsable() && toolSpoolId(n) && S.sm.byId[toolSpoolId(n)] ? null : 'No Spoolman spool on T' + n; },
        fn: function () { openSpoolDlg(S.sm.byId[toolSpoolId(n)]); } });
      tbl.appendChild(el('tr', { 'data-tool': n }, [el('td', { text: 'T' + n }), el('td', null, [r.dot, ' ', r.name]), el('td', null, [r.amt]), r.acts]));
    });
    S.updaters.push(function () {
      var mode = usageMode(), sm = mode === 'spoolman';
      segBtns.forEach(function (x) { x[1].classList.toggle('sel', x[0] === mode); x[1].setAttribute('aria-checked', x[0] === mode ? 'true' : 'false'); });
      smChip.textContent = !S.sm.ok ? 'Spoolman: not set up' : S.sm.connected ? (S.sm.loading ? 'Spoolman: loading' : 'Spoolman: connected') : 'Spoolman: offline';
      smChip.className = 'chip ' + (!S.sm.ok ? '' : S.sm.connected ? 'ok' : 'warn');
      smChip.title = S.sm.err || (S.sm.active ? 'Active spool in Moonraker: #' + S.sm.active : 'No active spool in Moonraker');
      refresh.hidden = !S.sm.ok; addBtn.hidden = !sm;
      var tc = st('toolchanger'), act = tc.tool_number;
      var mism = sm && act >= 0 && toolSpoolId(act) !== (S.sm.active || null) && S.sm.connected;
      note.textContent = sm ? (S.sm.connected ? 'Used filament is booked to the spool on the active tool. The add-on switches the active spool after every tool change.' +
          (mism ? ' Moonraker has spool ' + (S.sm.active ? '#' + S.sm.active : 'none') + ' active but T' + act + ' has ' + (toolSpoolId(act) ? '#' + toolSpoolId(act) : 'none') + '; it syncs within a second.' : '')
          : 'Spoolman is offline. Usage is not booked until it is back.')
        : 'Counted on the printer in variables.cfg (t#_used_mm). Weight is an estimate from the material.';
      S.tools.forEach(function (n) {
        var r = rows[n], id = toolSpoolId(n), sp = id ? S.sm.byId[id] : null, key = [mode, id, S.sm.spools.length, S.sm.connected].join('|');
        var col = sm && sp ? C.hex(sp.filament && sp.filament.color_hex) : C.hex(tv(n, 'color'));
        r.dot.style.background = '#' + (col || '777777');
        if (sm) {
          r.name.textContent = sp ? spoolLabel(sp) + (sp.filament && sp.filament.material ? ' (' + sp.filament.material + ')' : '') : id ? 'spool #' + id : 'no spool';
          r.amt.textContent = sp ? fmt(sp.remaining_weight) + ' g left' + (S.sm.active === id ? ', active' : '') : '-';
        } else {
          var ul = usageLine(n);
          r.name.textContent = [tv(n, 'material'), tv(n, 'spool')].filter(Boolean).join(', ') || '-';
          r.amt.textContent = ul ? ul.text + ' (~' + fmt(mmToG(usedMm(n), DENSITY[String(tv(n, 'material') || '').toUpperCase()])) + ' g)' : '-';
        }
        if (r.key !== key) {
          r.key = key;
          r.acts.innerHTML = '';
          if (sm) {
            r.sel.innerHTML = '';
            r.sel.appendChild(el('option', { value: 0, text: 'no spool' }));
            S.sm.spools.forEach(function (s) {
              r.sel.appendChild(el('option', { value: s.id, text: spoolLabel(s) + ' ' + fmt(s.remaining_weight) + 'g' }));
            });
            if (id && !S.sm.byId[id]) r.sel.appendChild(el('option', { value: id, text: 'spool #' + id + ' (not in Spoolman)' }));
            r.sel.value = String(id || 0);
            r.sel.disabled = !smUsable();
            r.acts.appendChild(r.sel); r.acts.appendChild(r.edit);
          } else r.acts.appendChild(r.reset);
        }
      });
    });
    p.appendChild(g);
  }

  /* Spoolman add / edit spool dialog. New filaments are created on the fly. */
  function openSpoolDlg(sp) {
    var d = $('dlgSpool'), err = $('spErr'), fsel = $('spFil');
    err.textContent = '';
    $('spTitle').textContent = sp ? 'Edit spool #' + sp.id : 'Add spool';
    var f0 = sp ? sp.filament || {} : {};
    $('spRem').value = sp ? fmt(sp.remaining_weight, 1) : 1000;
    $('spInit').value = sp ? fmt(sp.initial_weight || (sp.filament && sp.filament.weight) || 1000) : 1000;
    $('spLoc').value = sp ? sp.location || '' : '';
    $('spComment').value = sp ? sp.comment || '' : '';
    $('spVendor').value = ''; $('spName').value = ''; $('spMat').value = 'PLA'; $('spHex').value = 'FF8800'; $('spColor').value = '#ff8800';
    fsel.innerHTML = '';
    fsel.appendChild(el('option', { value: 'new', text: 'New filament...' }));
    var newBox = $('spNew');
    var sync = function () { newBox.hidden = fsel.value !== 'new'; };
    fsel.onchange = sync;
    $('spColor').oninput = function () { $('spHex').value = $('spColor').value.slice(1).toUpperCase(); };
    $('spHex').oninput = function () { var h = C.hex($('spHex').value); if (h) $('spColor').value = '#' + h; };
    smFilaments().then(function (fl) {
      (fl || []).forEach(function (f) {
        fsel.appendChild(el('option', { value: f.id, text: [f.vendor && f.vendor.name, f.name, f.material].filter(Boolean).join(' ') + (f.color_hex ? ' #' + f.color_hex : '') }));
      });
      fsel.value = sp && f0.id != null ? String(f0.id) : (fl && fl.length ? String(fl[0].id) : 'new');
      sync();
    }, function (e) { err.textContent = e.message || String(e); sync(); });
    $('spSave').onclick = function (ev) {
      ev.preventDefault();
      var rem = Number($('spRem').value), init = Number($('spInit').value);
      if (!(rem >= 0) || !(init > 0) || rem > init * 1.5) { err.textContent = 'Enter the remaining and full weight in grams'; return; }
      var getFil;
      if (fsel.value === 'new') {
        var mat = C.clean($('spMat').value, 20).toUpperCase(), hex = C.hex($('spHex').value);
        if (!mat) { err.textContent = 'Enter the material'; return; }
        if (!hex) { err.textContent = 'Colour needs 6 hex digits'; return; }
        var vn = C.clean($('spVendor').value, 40);
        var body = { name: C.clean($('spName').value, 60) || (mat + ' #' + hex), material: mat, color_hex: hex,
          density: DENSITY[mat] || 1.24, diameter: 1.75, weight: init };
        getFil = (vn ? smProxy('GET', '/v1/vendor').then(function (vs) {
          var v = (vs || []).find(function (x) { return lc(x.name) === lc(vn); });
          return v || smProxy('POST', '/v1/vendor', { name: vn });
        }).then(function (v) { body.vendor_id = v.id; }) : Promise.resolve()).then(function () {
          return smProxy('POST', '/v1/filament', body);
        }).then(function (f) { return f.id; });
      } else getFil = Promise.resolve(Number(fsel.value));
      $('spSave').disabled = true;
      getFil.then(function (fid) {
        var b = { filament_id: fid, remaining_weight: rem, initial_weight: init, location: C.clean($('spLoc').value, 60), comment: C.clean($('spComment').value, 200) };
        return sp ? smProxy('PATCH', '/v1/spool/' + sp.id, b) : smProxy('POST', '/v1/spool', b);
      }).then(function (res) {
        toast((sp ? 'Spool #' + sp.id + ' saved' : 'Spool #' + res.id + ' added') + ' in Spoolman', 'ok');
        d.close('ok');
        return smLoad();
      }, function (e) { err.textContent = e.message || String(e); }).then(function () { $('spSave').disabled = false; });
    };
    d.showModal();
  }

  /* ---------- SAVE_CONFIG conflict check (item 2) ----------
   * Klipper refuses SAVE_CONFIG when an option it wants to write to the #*#
   * block is also set in an included file ("SAVE_CONFIG section 'tool T1'
   * option 'gcode_x_offset' conflicts with included value"). Read printer.cfg
   * and its includes through Moonraker's file API and warn before it happens. */
  var CF = { busy: null, at: 0 };
  function cfgFetch(path) {
    var host = hostSetting();
    var url = (location.protocol === 'https:' ? 'https://' : 'http://') + host + '/server/files/config/' +
      path.split('/').map(encodeURIComponent).join('/');
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' reading ' + path);
      return r.text();
    });
  }
  function checkConflicts(force) {
    if (CF.busy) return CF.busy;
    if (!force && S.conflicts && Date.now() - CF.at < 60000) return Promise.resolve(S.conflicts);
    var main = null;
    CF.busy = Promise.all([S.rpc.call('printer.info'), S.rpc.call('server.files.roots').catch(function () { return []; }),
                           S.rpc.call('server.files.list', { root: 'config' }).catch(function () { return []; })]).then(function (r) {
      var root = ((r[1] || []).find(function (x) { return x.name === 'config'; }) || {}).path || '';
      var cf = String(r[0].config_file || '');
      main = root && cf.indexOf(root + '/') === 0 ? cf.slice(root.length + 1) : cf.split('/').pop() || 'printer.cfg';
      var all = (r[2] || []).map(function (f) { return f.path || f.filename; }).filter(Boolean);
      var seen = {}, inc = [];
      var walk = function (path, depth) {
        if (seen[path] || depth > 8) return Promise.resolve();
        seen[path] = true;
        return cfgFetch(path).then(function (text) {
          var pr = C.cfgParse(text);
          if (path === main) main = { path: path, parsed: pr }; else inc.push({ file: path, sections: pr.sections });
          var kids = [];
          pr.includes.forEach(function (pat) {
            var full = C.pathJoin(C.dirOf(path), pat);
            if (!full) return;
            if (/[*?[]/.test(full)) { var re = C.globRe(full); all.filter(function (f) { return re.test(f); }).sort().forEach(function (f) { kids.push(f); }); }
            else kids.push(full);
          });
          return kids.reduce(function (pp, k) { return pp.then(function () { return walk(k, depth + 1); }); }, Promise.resolve());
        });
      };
      return walk(main, 0).then(function () {
        S.conflicts = { ok: true, autosave: main.parsed.autosave, inc: inc, files: inc.length + 1, main: main.path };
      });
    }).catch(function (e) {
      S.conflicts = { ok: false, err: e.message || String(e), list: [] };
    }).then(function () { CF.busy = null; CF.at = Date.now(); scheduleUpdate(); return S.conflicts; });
    return CF.busy;
  }
  // re-evaluated on every update so a new SAVE_TOOL_PARAMETER result shows up at once
  function conflictList() {
    var c = S.conflicts;
    if (!c || !c.ok) return [];
    return C.cfgConflicts(c.autosave, st('configfile').save_config_pending_items || {}, c.inc);
  }
  function conflictText(list) {
    return list.map(function (c) { return '[' + c.section + '] ' + c.option + ' is set in ' + c.file + (c.pending ? ' (new result waiting)' : ' (already in the #*# block)'); });
  }
  function renderConflicts(box) {
    var c = S.conflicts, list = conflictList();
    var key = JSON.stringify([c && c.ok, c && c.err, c && c.files, list]);
    if (box._key === key) return;
    box._key = key;
    box.innerHTML = '';
    if (!c) { box.hidden = true; return; }
    box.hidden = false;
    if (!c.ok) {
      box.className = 'cfbox muted small';
      box.textContent = 'Could not read the config files to check for SAVE_CONFIG conflicts (' + c.err + '). SAVE_CONFIG still works; if Klipper reports "conflicts with included value", see the README.';
      return;
    }
    if (!list.length) {
      box.className = 'cfbox ok small';
      box.textContent = 'Config check: ' + c.files + ' files read, SAVE_CONFIG can write every offset.';
      return;
    }
    box.className = 'cfbox bad';
    box.appendChild(el('b', { text: 'SAVE_CONFIG will fail: ' + list.length + ' value' + (list.length > 1 ? 's are' : ' is') + ' set in an included file' }));
    var ul = el('ul');
    conflictText(list).forEach(function (t) { ul.appendChild(el('li', { text: t })); });
    box.appendChild(ul);
    box.appendChild(el('div', { class: 'small', text: 'Klipper only saves gcode_x_offset, gcode_y_offset, gcode_z_offset and [tool_probe] z_offset in the #*# block at the bottom of ' +
      c.main + ', and refuses when the same option is also in an included file. Fix: first copy each value into the #*# block (e.g. "#*# [tool T1]" then "#*# gcode_x_offset = 0.120"; ' +
      'z_offset is required under "#*# [tool_probe T1]"), then delete or comment out the line in the included file. Do both edits before any restart, otherwise Klipper starts with the offset missing. Then press Re-check.' }));
  }
  function saveConfigGate() {
    return checkConflicts(true).then(function (c) {
      var list = conflictList();
      if (!c || !c.ok || !list.length) return true;
      return confirmDlg('SAVE_CONFIG will fail', 'Klipper will refuse with "conflicts with included value" because: ' + conflictText(list).join('; ') +
        '. Move these values into the #*# block of ' + c.main + ' and remove them from the included file first (see the Calibration tab). Try anyway?', 'SAVE_CONFIG', 'Try anyway');
    });
  }

  /* ---------- spool state ---------- */
  function spoolState(n) {
    var i = S.info[n] || {}, sv = i.sensor ? st(i.sensor) : null;
    var flag = S.mods.fil ? tv(n, 'loaded') : undefined;
    var partial = !!(S.mods.fil && tv(n, 'partial'));
    var r = { cls: 'unknown', text: 'unknown', tag: null, warn: null, title: '', loaded: !!flag, partial: partial,
      tracked: flag != null,
      sensor: sv ? (sv.enabled === false ? 'disabled' : (sv.filament_detected ? 'detected' : 'empty')) : 'none' };
    // the tool printing right now is loaded, whatever the saved flag says
    var tc = st('toolchanger'), ps = printState();
    r.printing = tc.tool_number === n && (ps === 'printing' || ps === 'paused');
    if (r.printing && !partial && r.sensor !== 'empty') {
      r.cls = 'loaded'; r.loaded = true;
      r.text = ps === 'paused' ? 'loaded (paused)' : 'printing';
      r.title = 'The active tool in a print has filament in it' + (flag ? '' : '. Not marked loaded yet; the usage add-on marks it, or use Mark loaded');
      if (S.mods.fil && !flag) r.tag = 'not marked loaded';
      return r;
    }
    if (partial) {
      r.cls = 'partial'; r.text = 'nozzle change, ' + fmt(tv(n, 'partial_mm') || fvars().partial_unload_length) + 'mm out';
      r.title = 'Filament pulled back for a nozzle change. Press Load to feed it back.';
      return r;
    }
    if (r.sensor === 'disabled') {
      r.cls = 'disabled'; r.text = 'sensor disabled';
      r.title = 'The filament sensor is disabled (SET_FILAMENT_SENSOR SENSOR=... ENABLE=1 turns it back on)';
      if (S.mods.fil && flag != null) r.tag = flag ? 'marked loaded' : 'marked empty';
    } else if (r.sensor === 'detected') {
      if (!S.mods.fil) { r.cls = 'loaded'; r.text = 'filament detected'; r.title = 'From the filament sensor'; }
      else if (flag) { r.cls = 'loaded'; r.text = 'loaded'; r.title = 'Loaded to the nozzle, sensor sees filament'; }
      else { r.cls = 'ready'; r.text = 'ready to load'; r.title = 'The sensor sees filament but it is not loaded to the nozzle'; }
    } else if (r.sensor === 'empty') {
      r.cls = 'empty'; r.text = 'empty'; r.title = 'The filament sensor reads empty';
      if (flag) r.warn = 'Marked loaded but the sensor reads empty. Runout or a broken strand?';
    } else if (!S.mods.fil || flag == null) {
      r.cls = 'unknown'; r.text = 'unknown'; r.title = 'No filament sensor and nothing tracked yet';
    } else {
      r.cls = flag ? 'loaded' : 'empty'; r.text = flag ? 'loaded' : 'empty'; r.tag = 'tracked, no sensor';
      r.title = 'No filament sensor on this tool. State comes from the last load or unload.';
    }
    return r;
  }
  function profileInfo(n) {
    // mirrors FILAMENT_LOAD / UNLOAD: unknown -> LOW, CUSTOM without a saved temp -> HIGH temp
    var f = fvars(), p = tprof(n) || String(f.default_profile || 'LOW').toUpperCase(), lt, ut, warn = '';
    if (p === 'CUSTOM') {
      lt = tv(n, 'custom_load_temp'); ut = tv(n, 'custom_unload_temp');
      if (lt == null) { lt = f.high_load_temp; warn = 'no custom load temp saved, HIGH is used'; }
      if (ut == null) { ut = f.high_unload_temp; warn = warn || 'no custom unload temp saved, HIGH is used'; }
    } else if (p === 'HIGH') { lt = f.high_load_temp; ut = f.high_unload_temp; }
    else { p = 'LOW'; lt = f.low_load_temp; ut = f.low_unload_temp; }
    return { profile: p, load: lt, unload: ut, warn: warn };
  }

  /* ---------- building the rack ---------- */
  function buildEmpty(msg) {
    S.buttons = []; S.updaters = []; S.ui = {};
    $('rack').innerHTML = '';
    $('rack').style.setProperty('--cols', 1);
    $('rack').appendChild(el('div', { class: 'overlay', text: msg || 'Waiting for the printer...' }));
    $('tabs').innerHTML = ''; $('tabBody').innerHTML = ''; $('modChips').innerHTML = '';
  }
  function buildAll() {
    S.buttons = []; S.updaters = []; S.ui = {};
    var rack = $('rack');
    rack.innerHTML = '';
    rack.style.setProperty('--cols', S.tools.length || 1);
    rack.dataset.cols = S.tools.length;
    S.tools.forEach(function (n) {
      S.ui[n] = {};
      rack.appendChild(el('div', { class: 'col', 'data-tool': n }, [buildSpool(n), buildHead(n)]));
    });
    // no drop animation for the first paint after a rebuild, only for real tool changes
    rack.classList.add('noanim');
    setTimeout(function () { rack.classList.remove('noanim'); }, 120);
    buildTabs();
    buildChips();
    requestAnimationFrame(seatFilament);
  }
  /* The spool line is the only filament stroke. Its end is the inlet of the
   * head's tube (y2, plus any extruder mount shift), in screen pixels. */
  function seatFilament() {
    document.querySelectorAll('#rack .col').forEach(function (col) {
      var spool = col.querySelector('.spool'), svg = col.querySelector('.headsvg');
      var tube = svg && svg.querySelector('.tube');
      if (!spool || !tube || !svg.viewBox.baseVal.height) return;
      var y2 = parseFloat(tube.getAttribute('y2'));
      var node = tube.parentNode;
      while (node && node !== svg) {
        var tr = node.getAttribute && node.getAttribute('transform');
        var mm = tr && /translate\(\s*[^,]+,\s*([^)]+)\)/.exec(tr);
        if (mm) y2 += parseFloat(mm[1]);
        node = node.parentNode;
      }
      var vb = svg.viewBox.baseVal, sr = svg.getBoundingClientRect();
      if (!sr.height) return;
      var ty = 0, move = svg.closest('.headmove'), tf = move && getComputedStyle(move).transform;
      if (tf && tf !== 'none') { try { ty = new DOMMatrix(tf).m42; } catch (e) { ty = 0; } }
      var inlet = (sr.top - ty) + (y2 - vb.y) / vb.height * sr.height;
      spool.style.setProperty('--to-inlet', (inlet - spool.getBoundingClientRect().bottom).toFixed(2) + 'px');
    });
  }
  function buildChips() {
    var c = $('modChips'), m = S.mods;
    c.innerHTML = '';
    c.appendChild(el('span', { class: 'chip info', text: 'KTC-E, ' + S.tools.length + ' tools',
      title: 'toolchanger: ' + S.tools.map(function (n) { return 'T' + n; }).join(' ') }));
    if (m.fil) c.appendChild(el('span', { class: 'chip ok', text: 'Filament v' + (m.filV || '?'), title: 'ktc-filament module found (_FILAMENT_SETTINGS)' }));
    if (m.orb && !m.orbOff) c.appendChild(el('span', { class: 'chip ok', text: 'Orbiter v' + (m.orbV || '?'), title: 'Orbiter add-on found (_ORBITER_SETTINGS)' }));
    if (m.es) c.appendChild(el('span', { class: 'chip ok', text: 'Endless spool v' + (m.esV || '?') }));
    if (m.use) c.appendChild(el('span', { class: 'chip ok', text: 'Usage v' + (m.useV || '?'), title: 'Usage add-on found (_USAGE_SETTINGS), mode ' + usageMode() }));
    if (m.warn.length) c.appendChild(el('span', { class: 'chip warn', text: m.warn.length + ' warning' + (m.warn.length > 1 ? 's' : ''), title: m.warn.join('\n') }));
  }
  /* Each column: the spool on top with its filament line running down,
   * then the head hanging from the shared dock rail, then the head's stats
   * and the spool buttons. */
  function buildSpool(n) {
    var u = S.ui[n];
    var card = el('div', { class: 'spool', 'data-spool': n });
    var pick = el('button', { type: 'button', class: 'pick', html: ART.spool(), 'aria-label': 'Edit spool T' + n,
      onclick: function () { if (S.mods.fil) openEditor(n); } });
    if (!S.mods.fil) { pick.disabled = true; pick.title = 'Install the ktc-filament module to track spools'; }
    else pick.title = 'Edit the T' + n + ' spool: material, colour, name, temps, lengths';
    u.pick = pick;
    u.mat = el('span', { class: 'mat' }); u.name = el('div', { class: 'name' });
    u.state = el('span', { class: 'state' }); u.tag = el('span', { class: 'tag' });
    u.prof = el('div', { class: 'prof' });
    u.use = el('div', { class: 'use', hidden: true });
    u.warn = el('div', { class: 'warnline' });
    card.appendChild(el('div', { class: 'feed', 'aria-hidden': 'true' }));
    card.appendChild(pick);
    card.appendChild(el('div', { class: 'meta' }, [el('div', { class: 'lbl' }, [el('b', { text: 'T' + n }), u.mat]),
      S.mods.fil ? u.prof : null, u.name, u.use, el('div', { class: 'schips' }, [u.state, u.tag])]));
    card.appendChild(u.warn);
    u.spool = card;
    return card;
  }
  function spoolButtons(n) {
    var row = null;
    if (S.mods.fil) {
      var row = el('div', { class: 'btnrow' });
      if (has('FILAMENT_LOAD')) row.appendChild(mkBtn({ label: 'Load', kind: 'motion', cmd: 'FILAMENT_LOAD TOOL=' + n, cls: 'ld',
        title: 'Heat T' + n + ', feed to the nozzle and purge (FILAMENT_LOAD TOOL=' + n + ')',
        when: function () {
          var s = spoolState(n);
          if (s.partial) return null;
          if (s.sensor === 'empty') return 'Sensor reads empty. Insert filament first';
          if (s.loaded) return 'Already loaded. Unload first';
          return null;
        } }));
      if (has('FILAMENT_UNLOAD')) row.appendChild(mkBtn({ label: 'Unload', kind: 'motion', cmd: 'FILAMENT_UNLOAD TOOL=' + n, cls: 'ul',
        title: 'Heat T' + n + ', form the tip and pull the filament out (FILAMENT_UNLOAD TOOL=' + n + ')',
        when: function () {
          var s = spoolState(n);
          if (!s.loaded && !s.partial && s.tracked && (s.sensor === 'empty' || s.sensor === 'none')) return 'Already unloaded';
          return null;
        } }));
      if (has('FILAMENT_UNLOAD_PARTIAL')) row.appendChild(mkBtn({ label: 'Nozzle change', kind: 'motion', cls: 'warn pt',
        cmd: 'FILAMENT_UNLOAD_PARTIAL TOOL=' + n,
        title: 'Pull the filament back just out of the hotend so you can swap the nozzle (FILAMENT_UNLOAD_PARTIAL TOOL=' + n + ')',
        when: function () {
          var s = spoolState(n);
          if (s.partial) return 'Already pulled back. Press Load when the new nozzle is in';
          if (!s.loaded) return 'Only for a loaded tool';
          return null;
        },
        confirm: function () {
          var p = profileInfo(n), len = tv(n, 'partial_length') || fvars().partial_unload_length;
          return { title: 'Nozzle change on T' + n + '?', yes: 'Pull back',
            text: 'T' + n + ' heats to ' + fmt(p.unload) + 'C, forms the tip and pulls the filament back ' + fmt(len) +
              'mm. It stays in the extruder gears and the heater turns off. Swap the nozzle, then press Load. ' +
              'A new nozzle changes this tool\'s offsets, so recalibrate them after.' };
        } }));
    }
    return row;
  }
  var FAN_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 11a1 1 0 1 0 0 2a1 1 0 0 0 0-2m.5-9C17 2 17.1 5.6 14.7 6.8c-1 .5-1.4 1.5-1.6 2.4c.5.2.9.5 1.2.9c3.7-2 7.7-1.2 7.7 2.4c0 4.5-3.6 4.6-4.8 2.2c-.5-1-1.5-1.4-2.4-1.6c-.2.5-.5.9-.9 1.2c2 3.7 1.2 7.7-2.4 7.7c-4.5 0-4.6-3.6-2.2-4.8c1-.5 1.4-1.5 1.6-2.4c-.5-.2-.9-.5-1.2-.9C6 15.9 2 15.1 2 11.5C2 7 5.6 6.9 6.8 9.3c.5 1 1.5 1.4 2.4 1.6c.2-.5.5-.9.9-1.2C8.1 6 8.9 2 12.5 2Z"/></svg>';
  function buildHead(n) {
    var u = S.ui[n], i = S.info[n], p = headPref(n);
    var card = el('div', { class: 'head', 'data-head': n });
    card.appendChild(el('div', { class: 'rail', 'aria-hidden': 'true' }));
    u.headBtn = mkBtn({ label: '', kind: 'change', cmd: 'T' + n, title: 'Click to pick up T' + n, home: true,
      when: function () { return st('toolchanger').tool_number === n ? 'T' + n + ' is on the shuttle' : null; } });
    u.headBtn.className = 'headwrap';
    u.headBtn.setAttribute('aria-label', 'Pick up T' + n);
    u.headBtn.appendChild(el('div', { class: 'dock', html: ART.dock() }));
    u.move = el('div', { class: 'headmove', html: ART.toolhead(p.model, p) });
    u.headBtn.appendChild(u.move);
    card.appendChild(u.headBtn);
    u.glow = u.move.querySelector('.glow');
    u.svg = u.move.querySelector('.headsvg');
    u.svg.setAttribute('data-led', i.led || '');
    u.move.title = 'T' + n + ' LEDs: ' + ledInfo(n);
    var stats = el('div', { class: 'stats' });
    u.badges = el('div', { class: 'badges' });
    stats.appendChild(el('div', { class: 'hd' }, [el('h3', { text: 'T' + n }), u.badges]));
    u.temp = el('button', { type: 'button', class: 'temp', title: 'Set the T' + n + ' nozzle temperature (max ' + i.maxTemp + 'C)',
      onclick: function () { setTemp(n); } });
    if (!i.ext) { u.temp.disabled = true; u.temp.title = 'No extruder configured for this tool'; }
    stats.appendChild(u.temp);
    u.hfan = el('span', { class: 'fan', title: i.hfan ? 'Hotend fan (' + i.hfan + ')' : 'No hotend fan found for ' + (i.ext || 'this tool'),
      html: FAN_ICON + '<span></span>' });
    u.pfan = el('button', { type: 'button', class: 'fan', html: FAN_ICON + '<span></span>',
      title: i.fan ? 'Part fan (' + i.fan + '), click to set' : 'No part fan configured on this tool',
      onclick: function () { setFan(n); } });
    if (!i.fan) u.pfan.disabled = true;
    stats.appendChild(el('div', { class: 'fans' }, [u.pfan, u.hfan]));
    u.sens = el('span', { class: 'sens' }, [el('i'), el('span')]);
    stats.appendChild(el('div', { class: 'srow' }, [u.sens, el('span', { class: 'model', text: ART.label(p.model),
      title: 'Toolhead model (change it in Settings)' })]));
    u.offs = el('div', { class: 'offs' });
    stats.appendChild(u.offs);
    var row = spoolButtons(n);
    if (row) stats.appendChild(row);
    card.appendChild(stats);
    u.head = card;
    return card;
  }
  function setTemp(n) {
    var i = S.info[n], e = st(i.ext), why = lockReason('free');
    if (why) { toast(why); return; }
    valueDlg({ title: 'T' + n + ' nozzle temperature', label: 'Target C (0 to ' + i.maxTemp + ')',
      value: Math.round(e.target || 0) || 200, min: 0, max: i.maxTemp }).then(function (v) {
      if (v == null) return;
      run(C.toolTemp(n, v, i.ext, has('SET_TOOL_TEMPERATURE')));
    });
  }
  function setFan(n) {
    var i = S.info[n], why = lockReason('free');
    if (!i.fan) return;
    if (why) { toast(why); return; }
    var sp = Math.round((st(i.fan).speed || 0) * 100);
    valueDlg({ title: 'T' + n + ' part fan', label: 'Speed %', value: sp || 100, min: 0, max: 100, range: true })
      .then(function (v) {
        if (v == null) return;
        run(has('M106') || S.objects.has('toolchanger') ? C.partFan(n, v)
          : 'SET_FAN_SPEED FAN=' + i.fanName + ' SPEED=' + (v / 100).toFixed(2));
      });
  }

  /* ---------- live update ---------- */
  var upPending = false;
  function scheduleUpdate() {
    if (upPending) return;
    upPending = true;
    requestAnimationFrame(function () { upPending = false; updateAll(); });
  }
  function updateAll() {
    document.body.classList.toggle('offline', !S.ready);
    if (S.ready && S.objects.has('toolchanger')) {
      updateChips();
      var tc = st('toolchanger');
      S.tools.forEach(function (n) { updateTool(n, tc); });
      if (tc.status !== 'changing') S.prevActive = tc.tool_number;
      S.updaters.forEach(function (f) { try { f(); } catch (e) { console.error(e); } });
      var cl = conflictList(), ck = cl.map(function (c) { return c.section + '.' + c.option; }).join(',');
      if (ck !== S.cfgBannerKey) {
        S.cfgBannerKey = ck;
        banner('cfg', cl.length ? 'SAVE_CONFIG will fail: ' + conflictText(cl).slice(0, 3).join('; ') + (cl.length > 3 ? ' and ' + (cl.length - 3) + ' more' : '') +
          '. Open Calibration > Save for the fix.' : null, 'warn');
        if (!cl.length) banner('cfgerr', null);
      }
    }
    updateButtons();
  }
  /* Which commit is installed. setup.sh registers the clone with Moonraker as
   * [update_manager stealthchanger-gui]; machine.update.status reports its git
   * version, so the page shows what landed after an Update Manager update. */
  var UPDATER = 'stealthchanger-gui';
  function repoInfo(u) {
    if (!u) return null;
    var hash = String(u.current_hash || ''), rhash = String(u.remote_hash || '');
    var full = String(u.full_version_string || u.version || '?');
    var base = (/^v?(\d+\.\d+\.\d+)/.exec(String(u.version || '')) || [])[1] || '';
    return {
      version: String(u.version || '?'), full: full, commit: hash.slice(0, 7),
      behind: Number(u.commits_behind_count) || 0,
      update: !!(rhash && hash && rhash !== hash && (Number(u.commits_behind_count) || 0) > 0),
      remote: String(u.remote_version || ''), dirty: !!u.is_dirty,
      stale: !!(base && base !== GUI_VERSION),   // files on the Pi are newer/older than this page
      warn: [].concat(u.warnings || [], u.anomalies || [])
    };
  }
  function showRepoVersion() {
    var r = S.repo, tag = $('verTag'), line = $('repoVersion');
    var text = 'v' + GUI_VERSION, title = 'GUI v' + GUI_VERSION, cls = 'vertag';
    var about = 'Not installed through the Update Manager (no [update_manager ' + UPDATER + '] in moonraker.conf).';
    if (r) {
      text += ' \u00b7 ' + (r.commit || r.version);
      title = 'GUI v' + GUI_VERSION + '\nInstalled: ' + r.full + (r.commit ? ' (commit ' + r.commit + ')' : '');
      about = 'Installed from git: ' + r.full + (r.commit ? ', commit ' + r.commit : '') + (r.dirty ? ', with local edits' : '') + '.';
      if (r.update) {
        text += ' \u2191'; cls += ' warn';
        title += '\nUpdate available: ' + (r.remote || r.behind + ' commit(s)') + '. Mainsail > Machine > Update Manager > ' + UPDATER;
        about += ' Update available (' + (r.remote || r.behind + ' commits') + '): Mainsail > Machine > Update Manager.';
      } else {
        title += '\nUp to date';
        about += ' Up to date.';
      }
      if (r.stale) {
        cls += ' warn';
        var why = RL.served === GUI_VERSION
          ? ' The web server still serves v' + GUI_VERSION + ': the GUI folder nginx uses is not the git clone. Run setup.sh again to link it.'
          : RL.pending ? ' This page reloads itself to v' + RL.pending + ' when idle.'
          : RL.served ? '' : ' Reload the page to load the new files.';
        title += '\nThe printer has ' + r.version + ' but this page is v' + GUI_VERSION + '.' + why;
        about += ' This page is v' + GUI_VERSION + ' but the printer has ' + r.version + '.' + why;
      }
      if (r.warn.length) title += '\n' + r.warn.join('\n');
    }
    if (tag) { tag.textContent = text; tag.title = title; tag.className = cls; }
    if (line) line.textContent = about;
  }
  function loadRepoVersion() {
    if (!S.rpc) return Promise.resolve();
    return S.rpc.call('machine.update.status', {}).then(function (r) {
      S.repo = repoInfo(((r && r.version_info) || {})[UPDATER]);
    }, function () { S.repo = null; }).then(showRepoVersion);
  }


  /* ---------- reload after an update (no Ctrl+F5) ----------
   * index.html carries <meta name="ktc-gui-version"> and loads every file with ?v=<version>,
   * so a fresh index.html always brings fresh JS/CSS. To make sure index.html itself is fresh
   * (old nginx sites send no Cache-Control, so browsers may reuse it for hours), the page asks the
   * web server which version it serves now (no-store fetch, unique URL) on connect, Klipper ready,
   * Update Manager notices, when the tab comes back, and every 10 minutes. If that differs from
   * GUI_VERSION it reloads itself on a unique URL, but only when idle: never while printing or
   * changing tools, with a dialog open, or while something is being typed. Loop guard: at most two
   * tries per version in 15 minutes, then it asks for a manual reload instead. */
  var RL_KEY = 'ktc_gui_reload', RL_MAX = 2, RL_WINDOW = 15 * 60000;
  var RL = { idleMs: 60000, served: null, pending: null, arrived: null, lastCheck: 0, lastInput: 0, timer: null, going: false };
  function rlGet() { try { return JSON.parse(localStorage.getItem(RL_KEY) || 'null'); } catch (e) { return null; } }
  function rlSet(v) { try { if (v) localStorage.setItem(RL_KEY, JSON.stringify(v)); else localStorage.removeItem(RL_KEY); } catch (e) { /* private mode */ } }
  function pageDir() { return location.pathname.replace(/[^/]*$/, ''); }
  function rlArrive() {  // runs once at start: note a self-reload and tidy the address bar
    var q = new URLSearchParams(location.search), v = q.get('_v');
    if (v != null) {
      RL.arrived = v.replace(/_[^_]*$/, ''); q.delete('_v');
      var qs = q.toString();
      try { history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : '') + location.hash); } catch (e) { /* ignore */ }
    }
    var rec = rlGet();
    if (rec && rec.to === GUI_VERSION) rlSet(null);   // the reload worked
    document.addEventListener('input', function (ev) { if (ev.isTrusted !== false) RL.lastInput = Date.now(); }, true);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) checkServed('visible'); });
    setInterval(function () { checkServed('timer'); }, 10 * 60000);
  }
  function servedVersion() {
    var url = pageDir() + 'index.html?_=' + Date.now().toString(36);
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) return null;
      return r.text().then(function (t) {
        var m = /<meta\s+name="ktc-gui-version"\s+content="([^"]+)"/.exec(t);
        return m ? m[1] : null;   // null: an old index.html without the tag (rev 1 copy)
      });
    }, function () { return null; });
  }
  function checkServed(why, force) {
    var now = Date.now();
    if (!force && now - RL.lastCheck < 3000) return Promise.resolve(RL.served);
    RL.lastCheck = now;
    return servedVersion().then(function (v) {
      RL.served = v;
      RL.pending = v && v !== GUI_VERSION ? v : null;
      if (RL.pending) tryReload(why); else { reloadNotice(null); clearInterval(RL.timer); RL.timer = null; }
      showRepoVersion();
      return v;
    });
  }
  function busyReason() {
    var p = printState(), a = document.activeElement;
    if (!S.connected) return 'the printer connection is back';
    if (S.ready && (p === 'printing' || p === 'paused')) return 'the print is finished';
    if (S.ready && st('toolchanger').status === 'changing') return 'the tool change is done';
    if (document.querySelector('dialog[open]')) return 'you close the dialog';
    if (document.querySelector('[data-busy]')) return 'the running command is done';
    if (a && a.id !== 'conInput' && (a.tagName === 'TEXTAREA' || a.tagName === 'SELECT' || (a.tagName === 'INPUT' && !/^(button|submit|checkbox|radio|range|color)$/.test(a.type))))
      return 'you leave the text box';
    if ($('conInput') && $('conInput').value.trim()) return 'the console line is sent or cleared';
    if (Date.now() - RL.lastInput < RL.idleMs) return 'you stop editing for a minute';
    return null;
  }
  function reloadNotice(msg, kind, button) {
    var b = document.querySelector('[data-banner="reload"]');
    if (!msg) { if (b) b.remove(); return; }
    if (!b) { b = el('div', { 'data-banner': 'reload', role: 'status' }); $('banners').appendChild(b); }
    b.className = 'banner ' + (kind || 'info');
    b.textContent = msg + ' ';
    if (button) b.appendChild(el('button', { type: 'button', class: 'btn small', id: 'btnReloadNow', text: 'Reload now',
      onclick: function () { doReload(true); } }));
  }
  function tryReload(why) {
    var to = RL.pending;
    if (!to || RL.going) return;
    var rec = rlGet(), tries = rec && rec.to === to && Date.now() - rec.t < RL_WINDOW ? rec.n : 0;
    if (RL.arrived === to) tries = Math.max(tries, RL_MAX);   // came from a reload for this version and still old
    if (tries >= RL_MAX) {
      clearInterval(RL.timer); RL.timer = null;
      reloadNotice('The printer has GUI v' + to + ' but the browser keeps loading v' + GUI_VERSION +
        '. Reload by hand (Ctrl+F5, or clear this site\'s cache).', 'warn', false);
      return;
    }
    var busy = busyReason();
    if (busy) {
      reloadNotice('GUI v' + to + ' is installed. This page reloads when ' + busy + '.', 'info', true);
      if (!RL.timer) RL.timer = setInterval(function () { if (RL.pending) tryReload('idle'); }, 3000);
      return;
    }
    doReload(false);
  }
  function doReload(byUser) {
    var to = RL.pending || RL.served || GUI_VERSION;
    var rec = rlGet(), n = rec && rec.to === to && Date.now() - rec.t < RL_WINDOW ? rec.n : 0;
    rlSet({ to: to, n: n + 1, t: Date.now(), user: !!byUser });
    RL.going = true; clearInterval(RL.timer); RL.timer = null;
    reloadNotice('Loading GUI v' + to + '...', 'info', false);
    // refresh the cached copies the address bar uses, then open a URL no cache has seen
    var refresh = [location.pathname + location.search, pageDir() + 'index.html'].map(function (u) {
      return fetch(u, { cache: 'reload' }).catch(function () {});
    });
    var timeout = new Promise(function (res) { setTimeout(res, 3000); });
    Promise.race([Promise.all(refresh), timeout]).then(function () {
      var q = new URLSearchParams(location.search);
      q.set('_v', to + '_' + Date.now().toString(36));
      location.replace(location.pathname + '?' + q.toString() + location.hash);
    });
  }

  function setChip(id, text, cls, title) {
    var c = $(id); c.textContent = text; c.className = 'chip ' + (cls || ''); if (title != null) c.title = title;
  }
  function updateChips() {
    var p = printState(), fn = st('print_stats').filename;
    setChip('stateChip', p, p === 'printing' ? 'info' : p === 'paused' ? 'warn' : p === 'error' ? 'bad' : '',
      'print_stats.state' + (fn ? ': ' + fn : ''));
    var tc = st('toolchanger'), s = tc.status || '?';
    setChip('tcChip', s + (tc.tool_number >= 0 ? ', T' + tc.tool_number : ', no tool'),
      s === 'ready' ? 'ok' : s === 'changing' ? 'info' : s === 'error' ? 'bad' : 'warn', 'toolchanger.status and the tool on the shuttle');
  }
  function updateTool(n, tc) {
    var u = S.ui[n], i = S.info[n];
    if (!u || !u.spool) return;
    // spool
    var s = spoolState(n);
    var fil = u.pick.querySelector('.fil');
    if (fil) fil.setAttribute('fill', S.mods.fil ? '#' + (C.hex(tv(n, 'color')) || '777777') : '#5a5a5a');
    u.spool.className = 'spool ' + s.cls;
    var col = u.spool.parentNode, fc = S.mods.fil ? C.hex(tv(n, 'color')) : null;
    if (col) {
      col.style.setProperty('--fil', fc ? '#' + fc : '#8a8f96');
      col.style.setProperty('--halo', C.halo(fc));
      col.dataset.fil = s.partial ? 'partial' : (s.loaded || s.cls === 'loaded') ? 'loaded'
        : s.cls === 'ready' ? 'ready' : s.cls === 'empty' ? 'empty' : 'unknown';
    }
    if (S.mods.fil) {
      var mat = tv(n, 'material');
      u.mat.textContent = mat ? String(mat).toUpperCase() : 'not set';
      u.mat.classList.toggle('muted', !mat);
      u.name.textContent = tv(n, 'spool') || (mat ? '\u00a0' : 'click the spool');
      u.name.title = tv(n, 'spool') || '';
      var pi = profileInfo(n);
      u.prof.innerHTML = '';
      u.prof.appendChild(document.createTextNode('Temps '));
      u.prof.appendChild(el('b', { text: pi.profile }));
      u.prof.appendChild(document.createTextNode(' ' + fmt(pi.load) + '/' + fmt(pi.unload) + 'C'));
      if (pi.warn) u.prof.appendChild(el('span', { class: 'warnmark', text: ' !' }));
      u.prof.title = 'Load ' + fmt(pi.load) + 'C, unload ' + fmt(pi.unload) + 'C (' + pi.profile + ' profile)' + (pi.warn ? '. CUSTOM but ' + pi.warn : '');
    } else {
      u.mat.textContent = 'spool';
      u.name.textContent = i.sensor ? 'sensor only' : 'no sensor';
    }
    u.state.className = 'state ' + s.cls; u.state.textContent = s.text; u.state.title = s.title || '';
    u.tag.textContent = s.tag || ''; u.tag.hidden = !s.tag;
    u.warn.textContent = s.warn || ''; u.warn.hidden = !s.warn;
    var ul = usageLine(n);
    u.use.hidden = !ul; u.use.textContent = ul ? ul.text : ''; u.use.title = ul ? ul.title : '';
    // head
    var active = tc.tool_number === n, changing = tc.status === 'changing';
    u.head.classList.toggle('active', active);
    animState(u, 'dropping', changing && active);
    animState(u, 'rising', changing && !active && S.prevActive === n);
    var e = i.ext ? st(i.ext) : {}, t = e.temperature, tg = e.target;
    u.temp.innerHTML = '';
    u.temp.appendChild(document.createTextNode(fmt(t, 0) + '\u00b0'));
    u.temp.appendChild(el('small', { text: ' / ' + (tg ? fmt(tg) + '\u00b0' : 'off') }));
    u.temp.classList.toggle('hot', t > 50);
    if (u.glow) u.glow.setAttribute('opacity', Math.max(0, Math.min(0.9, ((t || 0) - 40) / 200)).toFixed(2));
    var cd = i.led ? st(i.led).color_data : null, p = headPref(n);
    var lo = cd ? ledMix(cd, parseIdx(p.logoIdx, [0])) : null, nz = cd ? ledMix(cd, parseIdx(p.nozIdx, [1, 2])) : null;
    u.head.classList.toggle('noled', !lo);
    var ledKey = JSON.stringify([lo, nz]);
    if (u.ledKey !== ledKey) {
      u.ledKey = ledKey;
      u.svg.style.setProperty('--logo', lo ? lo.col : '#fff'); u.svg.style.setProperty('--logo-a', lo ? lo.a.toFixed(3) : '0');
      u.svg.style.setProperty('--noz', nz ? nz.col : '#fff'); u.svg.style.setProperty('--noz-a', nz ? nz.a.toFixed(3) : '0');
    }
    var hf = i.hfan ? (st(i.hfan).speed || 0) : 0, pf = i.fan ? (st(i.fan).speed || 0) : 0;
    u.head.classList.toggle('fan-on', hf > 0);
    u.head.classList.toggle('pfan-on', pf > 0);
    u.hfan.classList.toggle('on', hf > 0);
    u.pfan.classList.toggle('on', pf > 0);
    u.hfan.lastChild.textContent = 'hotend ' + (i.hfan ? (hf > 0 ? 'on' : 'off') : 'n/a');
    u.pfan.lastChild.textContent = 'part ' + (i.fan ? Math.round(pf * 100) + '%' : 'n/a');
    var tt = st(i.name);
    var sn = { detected: ['on', 'sensor: filament'], empty: ['off', 'sensor: empty'], disabled: ['dis', 'sensor disabled'], none: ['na', 'no sensor'] }[s.sensor];
    u.sens.className = 'sens ' + sn[0]; u.sens.lastChild.textContent = sn[1];
    u.sens.title = i.sensor ? 'Filament sensor ' + i.sensor : 'This tool has no filament sensor';
    u.offs.innerHTML = S.tools[0] === n ? '<b>reference tool</b><br>offsets 0'
      : 'X <b>' + sgn(tt.gcode_x_offset) + '</b> Y <b>' + sgn(tt.gcode_y_offset) + '</b><br>Z <b>' + sgn(tt.gcode_z_offset) + '</b>';
    u.offs.title = S.tools[0] === n ? 'Other tools are measured against this one' : 'gcode offsets from [' + i.name + ']';
    // badges
    u.badges.innerHTML = '';
    if (active) u.badges.appendChild(el('span', { class: 'badge act', text: changing ? 'CHANGING' : 'ACTIVE', title: 'On the shuttle (toolchanger.tool_number)' }));
    if (tc.has_detection && tc.detected_tool_number === n)
      u.badges.appendChild(el('span', { class: 'badge det', text: 'detected', title: 'The detection pin sees this tool on the shuttle' }));
    var bt = (S.mods.orb && !S.mods.orbOff) ? (st(MAC.orb).button_tools || []) : [];
    if (bt.map(Number).indexOf(n) >= 0)
      u.badges.appendChild(el('span', { class: 'badge orb', text: 'Orbiter', title: 'Orbiter 2.5 buttons and sensor handled by the Orbiter add-on' }));
  }

  /* Tool change animation (item 14). The moving head loops 'lift' while
   * toolchanger.status is 'changing'. Dropping the class the moment the change
   * ends made the head jump. Now it finishes the cycle it is in (the next
   * animationiteration, or animationend for the one-shot 'settle'), then the
   * current position is frozen inline and released so the normal 0.9s
   * transform transition eases it into its final place. */
  function animState(u, cls, on) {
    var h = u.head, m = u.move, key = '_end_' + cls;
    if (on) {
      if (u[key]) { u[key](true); }
      if (!h.classList.contains(cls)) h.classList.add(cls);
      return;
    }
    if (!h.classList.contains(cls) || u[key]) return;
    var rack = $('rack');
    if (document.hidden || (rack && rack.classList.contains('noanim')) || !m) { h.classList.remove(cls); return; }
    var ev = cls === 'dropping' ? 'animationiteration' : 'animationend', timer = null;
    var onEv = function (e) { if (e.target === m) finish(false); };
    var finish = function (cancel) {
      m.removeEventListener(ev, onEv); clearTimeout(timer); u[key] = null;
      if (cancel) return;
      var aura = m.querySelector('.aura');
      var tf = getComputedStyle(m).transform, ao = aura ? getComputedStyle(aura).opacity : null;
      m.style.transform = tf === 'none' ? '' : tf;
      if (aura) aura.style.opacity = ao;
      h.classList.remove(cls);
      void m.offsetWidth;                         // commit the frozen frame
      m.style.transform = '';
      if (aura) aura.style.opacity = '';
      h.dataset.anim = 'eased';
    };
    u[key] = finish;
    if (m.getAnimations && !m.getAnimations().some(function (a) { return a.playState === 'running'; })) { finish(false); return; }
    m.addEventListener(ev, onEv);
    timer = setTimeout(function () { finish(false); }, 1700);   // one full cycle is 1.6s
  }

  /* ---------- tabs ---------- */
  function buildTabs() {
    var tabs = [['tool', 'Tool actions', buildToolTab], ['cal', 'Calibration', buildCalTab], ['rec', 'Recovery', buildRecTab]];
    if (S.mods.fil) tabs.push(['fil', 'Filament', buildFilTab]);
    if (S.mods.es && S.mods.fil) tabs.push(['es', 'Endless spool', buildEsTab]);
    tabs.push(['fw', 'Firmware', buildFirmwareTab]);
    var nav = $('tabs'), body = $('tabBody');
    nav.innerHTML = ''; body.innerHTML = '';
    var want = localStorage.getItem('ktc_gui_tab');
    if (!tabs.some(function (t) { return t[0] === want; })) want = 'tool';
    tabs.forEach(function (t) {
      var pane = el('div', { class: 'pane', id: 'pane-' + t[0], role: 'tabpanel' });
      t[2](pane);
      body.appendChild(pane);
      nav.appendChild(el('button', { type: 'button', role: 'tab', 'data-tab': t[0], text: t[1], onclick: function () { showTab(t[0]); } }));
    });
    showTab(want);
  }
  function showTab(id) {
    localStorage.setItem('ktc_gui_tab', id);
    Array.prototype.forEach.call($('tabs').children, function (b) { b.setAttribute('aria-selected', b.dataset.tab === id ? 'true' : 'false'); });
    Array.prototype.forEach.call($('tabBody').children, function (p) { p.hidden = p.id !== 'pane-' + id; });
    // The firmware helper is contacted only while this tab is open, so a
    // missing helper does not mark every other tab with a failed request.
    if (id === 'fw' && KTC.firmware && KTC.firmware.onShow) KTC.firmware.onShow();
  }
  function grp(title, kids, note) {
    kids = kids.filter(Boolean);
    if (!kids.length) return null;
    var g = el('div', { class: 'grp' }, [el('h4', { text: title }), el('div', { class: 'btns' }, kids)]);
    if (note) g.appendChild(el('div', { class: 'muted small note', text: note }));
    return g;
  }
  function add(p, g) { if (g) p.appendChild(g); }
  function toolSelect(exclude, title) {
    var s = el('select', { title: title || 'Tool' });
    S.tools.forEach(function (n) { if (n !== exclude) s.appendChild(el('option', { value: n, text: 'T' + n })); });
    return s;
  }
  function kvBox(rows) { // rows: [[label, fn]]
    var box = el('div', { class: 'kv' });
    var cells = rows.map(function (r) {
      var v = el('span'); box.appendChild(el('span', { text: r[0] })); box.appendChild(v); return [v, r[1]];
    });
    S.updaters.push(function () { cells.forEach(function (c) { c[0].textContent = c[1](); }); });
    return box;
  }
  function maybe(cmd, o) { return has(cmd) ? mkBtn(o) : null; }
  function noTool() { return st('toolchanger').tool_number < 0 ? 'No tool on the shuttle' : null; }

  function buildToolTab(p) {
    add(p, grp('Toolchanger', [
      maybe('G28', { label: 'Home all', kind: 'motion', cmd: 'G28', title: 'Home XYZ (G28)' }),
      maybe('INITIALIZE_TOOLCHANGER', { label: 'Initialize toolchanger', kind: 'motion', cmd: 'INITIALIZE_TOOLCHANGER',
        title: 'Re-read which tool is on the shuttle and reset the toolchanger (INITIALIZE_TOOLCHANGER)' }),
      maybe('DETECT_ACTIVE_TOOL_PROBE', { label: 'Detect active tool', cmd: 'DETECT_ACTIVE_TOOL_PROBE',
        title: 'Ask the tool probes which tool is on the shuttle (DETECT_ACTIVE_TOOL_PROBE)' }),
      maybe('UNSELECT_TOOL', { label: 'Park tool', kind: 'change', cmd: 'UNSELECT_TOOL', when: noTool, home: true,
        title: 'Put the current tool back in its dock (UNSELECT_TOOL)' }),
      maybe('TEST_TOOL_DOCKING', { label: 'Test docking', kind: 'motion', cmd: 'TEST_TOOL_DOCKING', cls: 'warn', when: noTool, home: true,
        title: 'Dock and pick up the current tool to test the path (TEST_TOOL_DOCKING)',
        confirm: { title: 'Test docking?', text: 'The current tool is docked and picked up again. Make sure the dock path is clear.' } })
    ]));
    add(p, grp('Pick up tool', S.tools.map(function (n) {
      return mkBtn({ label: 'T' + n, kind: 'change', cmd: 'T' + n, title: 'Pick up T' + n + ' (T' + n + ')', home: true,
        when: function () { return st('toolchanger').tool_number === n ? 'T' + n + ' is on the shuttle' : null; } });
    }), 'Same as clicking a toolhead above. If X, Y and Z are not homed, nothing is sent until you agree to home.'));
    add(p, grp('Heaters', [
      maybe('TURN_OFF_HEATERS', { label: 'All heaters off', cmd: 'TURN_OFF_HEATERS', title: 'Turn every heater off (TURN_OFF_HEATERS)',
        when: function () { return printState() === 'printing' ? 'Locked while printing' : null; } })
    ]));
    p.appendChild(kvBox([
      ['Status', function () { return st('toolchanger').status || '-'; }],
      ['On shuttle', function () { var t = st('toolchanger').tool_number; return t >= 0 ? 'T' + t : 'none'; }],
      ['Detected', function () { var tc = st('toolchanger'); return !tc.has_detection ? 'no detection pins' : tc.detected_tool_number >= 0 ? 'T' + tc.detected_tool_number : 'none'; }],
      ['Homed axes', function () { return (st('toolhead').homed_axes || '').toUpperCase() || 'none'; }]
    ]));
  }
  function buildCalTab(p) {
    var ref = S.tools[0], extOf = {};
    S.tools.forEach(function (n) { extOf[n] = S.info[n].ext; });
    var canOne = S.tools.length > 1 && ['CALIBRATE_MOVE_OVER_PROBE', 'TOOL_LOCATE_SENSOR', 'TOOL_CALIBRATE_TOOL_OFFSET',
      '_SAVE_TOOL_OFFSET', 'SELECT_TOOL', 'STOP_CRASH_DETECTION'].every(has);
    var one = null;
    if (canOne) {
      var oneSel = toolSelect(ref, 'Tool to calibrate. The reference tool T' + ref + ' runs first to find the sensor');
      oneSel.id = 'calOneTool';
      one = el('span', { class: 'inline' }, [oneSel, mkBtn({ label: 'Calibrate one tool offset', kind: 'motion', home: true,
        title: 'T' + ref + ' finds the calibration sensor first, then only the chosen tool is measured and saved',
        cmd: function () { return C.calibrateOne(Number(oneSel.value), ref, extOf); },
        confirm: function () { return { title: 'Calibrate T' + oneSel.value + '?', yes: 'Calibrate',
          text: 'T' + ref + ' (reference) is picked up and heated to 150C to find the sensor, then T' + oneSel.value +
            ' is measured. Nozzles must be clean. Press SAVE_CONFIG after to keep the result.' }; } })]);
    }
    add(p, grp('Tool offsets', [
      maybe('CALIBRATE_ALL_OFFSETS', { label: 'Calibrate all offsets', kind: 'motion', cmd: 'CALIBRATE_ALL_OFFSETS', home: true,
        title: 'Measure every tool against T' + ref + ' on the calibration sensor (CALIBRATE_ALL_OFFSETS)',
        confirm: { title: 'Calibrate all tool offsets?', yes: 'Calibrate',
          text: 'Every tool is picked up, heated to 150C and touched on the calibration sensor. Nozzles must be clean. Press SAVE_CONFIG after to keep the results.' } }),
      one,
      maybe('CALIBRATE_NOZZLE_PROBE_OFFSET', { label: 'Calibrate probe Z offset', kind: 'motion', cmd: 'CALIBRATE_NOZZLE_PROBE_OFFSET', home: true,
        title: 'Measure the nozzle to probe Z offset (CALIBRATE_NOZZLE_PROBE_OFFSET)',
        confirm: { title: 'Calibrate nozzle probe offset?', yes: 'Calibrate', text: 'Uses the calibration sensor. Nozzle must be clean.' } })
    ], 'T' + ref + ' is the reference tool. Other offsets are measured relative to it. Not homed? G28 runs first.'));
    var sens = st('gcode_macro _CALIBRATION_SWITCH');
    var sensChip = null;
    if (S.objects.has('tools_calibrate')) {
      sensChip = el('span', { class: 'chip', title: 'tools_calibrate sensor state' });
      S.updaters.push(function () {
        var v = st('tools_calibrate').calibration_probe_inactive;
        sensChip.textContent = v == null ? 'sensor ?' : v ? 'sensor open' : 'sensor TRIGGERED';
        sensChip.className = 'chip ' + (v === false ? 'warn' : 'ok');
      });
    }
    add(p, grp('Calibration sensor', [
      maybe('CALIBRATE_MOVE_OVER_PROBE', { label: 'Move over sensor', kind: 'motion', cmd: 'CALIBRATE_MOVE_OVER_PROBE', home: true,
        title: 'Move the nozzle above the calibration sensor' + (sens.x != null ? ' (X' + sens.x + ' Y' + sens.y + ' Z' + sens.z + ')' : '') }),
      maybe('QUERY_PROBE', { label: 'Query probe', cmd: 'QUERY_PROBE', title: 'Report the tool probe state (QUERY_PROBE)' }),
      sensChip
    ]));
    add(p, grp('Dock alignment', [
      maybe('TOOL_ALIGN_START', { label: 'Align start', kind: 'motion', cmd: 'TOOL_ALIGN_START', home: true, title: 'Start the dock alignment helper (TOOL_ALIGN_START)' }),
      maybe('TOOL_ALIGN_TEST', { label: 'Align test', kind: 'motion', cmd: 'TOOL_ALIGN_TEST', title: 'Test the dock position (TOOL_ALIGN_TEST)' }),
      maybe('TOOL_ALIGN_DONE', { label: 'Align done', kind: 'motion', cmd: 'TOOL_ALIGN_DONE', title: 'Finish dock alignment (TOOL_ALIGN_DONE)' })
    ]));
    var pend = el('span', { class: 'chip' });
    S.updaters.push(function () {
      var pd = st('configfile').save_config_pending;
      pend.textContent = pd ? 'unsaved results' : 'nothing to save';
      pend.className = 'chip ' + (pd ? 'warn' : '');
    });
    var cfBox = el('div', { class: 'cfbox', id: 'cfBox', hidden: true });
    S.updaters.push(function () { renderConflicts(cfBox); });
    var sg = grp('Save', [
      maybe('SAVE_CONFIG', { label: 'SAVE_CONFIG', kind: 'motion', cmd: 'SAVE_CONFIG', cls: 'primary',
        title: 'Write calibration results to printer.cfg and restart Klipper',
        when: function () { return st('configfile').save_config_pending ? null : 'Nothing to save yet'; },
        intercept: saveConfigGate,
        confirm: { title: 'SAVE_CONFIG?', yes: 'Save and restart', text: 'Writes the pending results to printer.cfg and restarts Klipper.' } }),
      pend,
      mkBtn({ label: 'Re-check config', title: 'Read printer.cfg and its includes again and look for values SAVE_CONFIG cannot save',
        fn: function () { checkConflicts(true); } })
    ]);
    sg.appendChild(cfBox);
    add(p, sg);
    if (S.objects.has('tools_calibrate')) {
      p.appendChild(el('div', { class: 'grp' }, [el('h4', { text: 'Last measurement' }), kvBox([
        ['X', function () { return fmt(st('tools_calibrate').last_x_result, 4); }],
        ['Y', function () { return fmt(st('tools_calibrate').last_y_result, 4); }],
        ['Z', function () { return fmt(st('tools_calibrate').last_z_result, 4); }]
      ])]));
    }
  }
  function buildRecTab(p) {
    add(p, grp('Get going again', [
      maybe('RESUME', { label: 'Resume print', cmd: 'RESUME', cls: 'primary', title: 'Resume the paused print (RESUME)',
        when: function () { return isPaused() ? null : 'Only while a print is paused'; } }),
      maybe('INITIALIZE_TOOLCHANGER', { label: 'Initialize toolchanger', kind: 'motion', cmd: 'INITIALIZE_TOOLCHANGER',
        title: 'Detect the tool on the shuttle and reset the toolchanger (INITIALIZE_TOOLCHANGER)' }),
      maybe('INITIALIZE_TOOLCHANGER_ORIG', { label: 'Recover from error', cls: 'danger', cmd: 'INITIALIZE_TOOLCHANGER_ORIG RECOVER=1',
        title: 'Clear a toolchanger error and continue (INITIALIZE_TOOLCHANGER_ORIG RECOVER=1)',
        when: function () { return st('toolchanger').status === 'error' ? null : 'Only when the toolchanger is in error'; },
        confirm: { title: 'Recover the toolchanger?', yes: 'Recover',
          text: 'Only use this after fixing the cause by hand: the right tool on the shuttle, nothing jammed.' } })
    ]));
    add(p, grp('Crash detection', [
      maybe('START_CRASH_DETECTION', { label: 'Crash detection on', cmd: 'START_CRASH_DETECTION', title: 'START_CRASH_DETECTION' }),
      maybe('STOP_CRASH_DETECTION', { label: 'Crash detection off', cmd: 'STOP_CRASH_DETECTION', cls: 'warn', title: 'STOP_CRASH_DETECTION' })
    ]));
    add(p, grp('Docking mode', [
      maybe('ENTER_DOCKING_MODE', { label: 'Enter docking mode', kind: 'motion', cmd: 'ENTER_DOCKING_MODE', title: 'ENTER_DOCKING_MODE' }),
      maybe('EXIT_DOCKING_MODE', { label: 'Exit docking mode', kind: 'motion', cmd: 'EXIT_DOCKING_MODE', title: 'EXIT_DOCKING_MODE' })
    ]));
    if (has('VERIFY_TOOL_DETECTED')) {
      var vs = toolSelect(null, 'Tool that should be on the shuttle');
      add(p, grp('Verify', [el('span', { class: 'inline' }, [vs, mkBtn({ label: 'Verify tool detected', cls: 'danger', kind: 'motion',
        cmd: function () { return 'VERIFY_TOOL_DETECTED T=' + vs.value; },
        title: 'Check the detected tool matches. On a mismatch Klipper does an emergency stop (M112)',
        confirm: function () { return { title: 'Verify T' + vs.value + '?', yes: 'Verify',
          text: 'If the detected tool is not T' + vs.value + ', Klipper does an EMERGENCY STOP (M112) and needs a firmware restart.' }; } })])]));
    }
    if (has('RESET_TOOL_PARAMETER')) {
      var ts = toolSelect(null, 'Tool'), ps = el('select', { title: 'Parameter changed with SET_TOOL_PARAMETER' });
      var fill = function () {
        ps.innerHTML = '';
        Object.keys(st(S.names[Number(ts.value)])).filter(function (k) { return /^params_/.test(k); }).sort()
          .forEach(function (k) { ps.appendChild(el('option', { value: k, text: k })); });
      };
      ts.addEventListener('change', fill); fill();
      add(p, grp('Tool parameters', [el('span', { class: 'inline' }, [ts, ps, mkBtn({ label: 'Reset', cmd: function () {
        return ps.value ? 'RESET_TOOL_PARAMETER T=' + ts.value + ' PARAMETER=' + ps.value : null;
      }, title: 'Undo a SET_TOOL_PARAMETER change for this run (RESET_TOOL_PARAMETER)' })])]));
    }
  }
  function buildFilTab(p) {
    add(p, grp('Overview', [
      maybe('SHOW_TOOLS', { label: 'Show all tools', cmd: 'SHOW_TOOLS', title: 'Print every tool\'s spool setup to the console (SHOW_TOOLS)' }),
      maybe('SPOOL_SETUP', { label: 'Spool setup wizard (Mainsail)', cmd: 'SPOOL_SETUP',
        title: 'Opens the step-by-step spool setup pop-up in Mainsail or Fluidd (SPOOL_SETUP). This page does not draw Klipper pop-ups, use the spool editor here instead' })
    ]));
    var tbl = el('table', { class: 'ftab' });
    tbl.appendChild(el('tr', null, ['Tool', 'Spool', 'State', 'Temps', 'Fix tracking'].map(function (h) { return el('th', { text: h }); })));
    S.tools.forEach(function (n) {
      var dot = el('span', { class: 'dot' }), sp = el('span'), stt = el('span', { class: 'state' }), tmp = el('span');
      var acts = el('td', { class: 'acts' }, [
        maybe('SET_FILAMENT_LOADED', { label: 'Mark loaded', cmd: C.markLoaded(n, true),
          title: 'Correct the tracked state without moving filament (' + C.markLoaded(n, true) + ')',
          when: function () { return tv(n, 'loaded') && !tv(n, 'partial') ? 'Already marked loaded' : null; } }),
        maybe('SET_FILAMENT_LOADED', { label: 'Mark empty', cmd: C.markLoaded(n, false),
          title: 'Correct the tracked state without moving filament (' + C.markLoaded(n, false) + ')',
          when: function () { return tv(n, 'loaded') === false && !tv(n, 'partial') ? 'Already marked empty' : null; } }),
        el('button', { type: 'button', class: 'btn', text: 'Edit', title: 'Open the T' + n + ' spool editor', onclick: function () { openEditor(n); } })
      ]);
      tbl.appendChild(el('tr', null, [el('td', { text: 'T' + n }), el('td', null, [dot, ' ', sp]), el('td', null, [stt]), el('td', null, [tmp]), acts]));
      S.updaters.push(function () {
        dot.style.background = '#' + (C.hex(tv(n, 'color')) || '777777');
        sp.textContent = [tv(n, 'material'), tv(n, 'spool')].filter(Boolean).join(', ') || '-';
        var s = spoolState(n); stt.className = 'state ' + s.cls; stt.textContent = s.text;
        var pi = profileInfo(n); tmp.textContent = pi.profile + ' ' + fmt(pi.load) + '/' + fmt(pi.unload) + 'C';
      });
    });
    p.appendChild(el('div', { class: 'grp tblwrap' }, [el('h4', { text: 'Tools' }), tbl]));
    buildUsagePanel(p);
    p.appendChild(el('div', { class: 'grp' }, [el('h4', { text: 'Defaults (_FILAMENT_SETTINGS)' }), kvBox([
      ['HIGH', function () { var f = fvars(); return fmt(f.high_load_temp) + '/' + fmt(f.high_unload_temp) + 'C load/unload'; }],
      ['LOW', function () { var f = fvars(); return fmt(f.low_load_temp) + '/' + fmt(f.low_unload_temp) + 'C load/unload'; }],
      ['New tools use', function () { return fvars().default_profile || '-'; }],
      ['Load', function () { var f = fvars(); return fmt(f.load_length) + 'mm at ' + fmt(f.load_speed) + 'mm/min, purge ' + fmt(f.purge_length) + 'mm'; }],
      ['Unload', function () { var f = fvars(); return fmt(f.unload_length) + 'mm at ' + fmt(f.unload_speed) + 'mm/min'; }],
      ['Nozzle change', function () { var f = fvars(); return f.partial_unload_length != null ? 'pull back ' + fmt(f.partial_unload_length) + 'mm' : 'not available'; }]
    ]), el('div', { class: 'muted small note', text: 'Defaults live in filament_macros.cfg. Per-tool values are set in the spool editor.' })]));
    if (S.mods.orb && !S.mods.orbOff && has('ORBITER_TANGLE')) {
      var seg = el('div', { class: 'seg', role: 'radiogroup', id: 'tangleSeg', title: 'What happens when the Orbiter tangle sensor trips' });
      var segBtns = ['PAUSE', 'WARN', 'OFF'].map(function (m) {
        var b = mkBtn({ label: m, cmd: 'ORBITER_TANGLE MODE=' + m,
          title: { PAUSE: 'Pause the print on a tangle', WARN: 'Only show a warning', OFF: 'Ignore the tangle sensor' }[m] });
        b.className = ''; b.dataset.v = m; b.setAttribute('role', 'radio'); seg.appendChild(b); return [m, b];
      });
      S.updaters.push(function () {
        var cur = String(st(MAC.orb).tangle_mode || '').toUpperCase();
        segBtns.forEach(function (x) { x[1].classList.toggle('sel', x[0] === cur); x[1].setAttribute('aria-checked', x[0] === cur ? 'true' : 'false'); });
      });
      add(p, grp('Orbiter 2.5', [el('span', { class: 'muted small', text: 'Tangle mode' }), seg],
        'Orbiter buttons on ' + ((st(MAC.orb).button_tools || []).map(function (n) { return 'T' + n; }).join(' ') || 'no tools') +
        '. Tangle mode can be changed mid-print.'));
    }
  }
  function buildEsTab(p) {
    p.appendChild(el('div', { class: 'muted', text: 'Endless spool module v' + (S.mods.esV || '?') + ' found. Its controls arrive with the endless spool GUI update.' }));
  }
  /* Local print_stats check. The helper checks again, including immediately
   * before it stops Klipper. Missing state refuses: we will not flash when
   * we cannot see whether a print is running. */
  function firmwarePrintBlock() {
    if (!S.connected) return 'Not connected to Moonraker. Flashing is refused because the print state cannot be checked.';
    var ps = st('print_stats').state;
    if (!ps) return 'print_stats.state is not available yet. Flashing is refused.';
    if (ps === 'printing' || ps === 'paused') {
      return 'Firmware update refused: the printer is ' + ps + '. Wait until the print is idle.';
    }
    return null;
  }
  function buildFirmwareTab(p) {
    if (!KTC.firmware) {
      p.appendChild(el('div', { class: 'muted', text: 'Firmware tab did not load.' }));
      return;
    }
    KTC.firmware.build(p, {
      el: el,
      confirmDlg: confirmDlg,
      toast: toast,
      printBlock: firmwarePrintBlock,
      onUpdate: function (fn) { S.updaters.push(fn); }
    });
  }

  /* ---------- spool editor ---------- */
  var ED = { n: null, before: null };
  var ED_FIELDS = { material: 'edOther', color: 'edHex', load_temp: 'edLoadT', unload_temp: 'edUnloadT', load_length: 'edLL',
    load_speed: 'edLS', unload_length: 'edUL', unload_speed: 'edUS', partial_length: 'edPL' };
  var ED_MOTION = [['edLL', 'load_length'], ['edLS', 'load_speed'], ['edUL', 'unload_length'], ['edUS', 'unload_speed'], ['edPL', 'partial_length']];
  function edValues() {
    var mat = $('edMaterial').value === '__other' ? $('edOther').value : $('edMaterial').value;
    var sel = document.querySelector('#edProfile .sel');
    return {
      material: mat, color: $('edHex').value, spool: $('edSpool').value, profile: sel ? sel.dataset.v : 'LOW',
      load_temp: C.num($('edLoadT').value), unload_temp: C.num($('edUnloadT').value),
      load_length: C.num($('edLL').value), load_speed: C.num($('edLS').value), unload_length: C.num($('edUL').value),
      unload_speed: C.num($('edUS').value), partial_length: $('edPL').closest('label').hidden ? null : C.num($('edPL').value)
    };
  }
  function edLimits() {
    var i = S.info[ED.n], f = fvars();
    return { min_temp: i.minExtrude, max_temp: i.maxTemp, load_length: f.load_length,
      partial_margin: f.partial_margin != null ? f.partial_margin : 10,
      partial_default: f.partial_unload_length != null ? f.partial_unload_length : null };
  }
  function edRefresh() {
    var v = edValues(), lim = edLimits(), errs = C.validate(v, lim), bad = Object.keys(errs);
    Object.keys(ED_FIELDS).forEach(function (k) { $(ED_FIELDS[k]).classList.toggle('bad', !!errs[k]); });
    $('edErr').textContent = bad.map(function (k) { return k.replace('_', ' ') + ': ' + errs[k]; }).join('\n');
    $('edCustom').hidden = v.profile !== 'CUSTOM';
    var f = fvars();
    $('edProfInfo').textContent = v.profile === 'HIGH' ? 'Load ' + f.high_load_temp + 'C, unload ' + f.high_unload_temp + 'C'
      : v.profile === 'LOW' ? 'Load ' + f.low_load_temp + 'C, unload ' + f.low_unload_temp + 'C'
      : 'Allowed ' + lim.min_temp + ' to ' + lim.max_temp + 'C on this tool (min_extrude_temp to max_temp)';
    var lines = bad.length ? [] : C.editorScript(ED.n, ED.before, v);
    $('edPreview').textContent = lines.join('\n');
    $('edSave').disabled = !!bad.length || !lines.length;
    $('edSave').title = bad.length ? 'Fix the fields in red first' : !lines.length ? 'Nothing changed' : 'Send the commands shown above';
    document.querySelectorAll('#edSwatches button').forEach(function (b) { b.classList.toggle('sel', b.dataset.hex === C.hex(v.color)); });
  }
  function openEditor(n) {
    if (!S.mods.fil || !S.ready) return;
    ED.n = n;
    var i = S.info[n], f = fvars();
    var mat = String(tv(n, 'material') || '').toUpperCase();
    var prof = tprof(n) || String(f.default_profile || 'LOW').toUpperCase();
    if (['HIGH', 'LOW', 'CUSTOM'].indexOf(prof) < 0) prof = 'LOW';
    var num = function (k) { var x = tv(n, k); return x == null || x === '' ? null : Number(x); };
    ED.before = { material: mat, color: C.hex(tv(n, 'color')) || '', spool: tv(n, 'spool') || '', profile: tprof(n) || prof,
      load_temp: num('custom_load_temp'), unload_temp: num('custom_unload_temp'), load_length: num('load_length'), load_speed: num('load_speed'),
      unload_length: num('unload_length'), unload_speed: num('unload_speed'), partial_length: num('partial_length') };
    $('edTitle').textContent = 'T' + n + ' spool';
    var opt = Array.prototype.find.call($('edMaterial').options, function (o) { return o.value !== '__other' && o.value.toUpperCase() === mat; });
    $('edMaterial').value = opt ? opt.value : (mat ? '__other' : 'PLA');
    $('edOther').value = opt ? '' : mat;
    $('edOtherWrap').hidden = $('edMaterial').value !== '__other';
    var hex = ED.before.color || 'D41216';
    $('edHex').value = hex; $('edColor').value = '#' + hex;
    $('edSpool').value = ED.before.spool;
    document.querySelectorAll('#edProfile button').forEach(function (b) { b.classList.toggle('sel', b.dataset.v === prof); });
    $('edLoadT').value = ED.before.load_temp != null ? ED.before.load_temp : '';
    $('edUnloadT').value = ED.before.unload_temp != null ? ED.before.unload_temp : '';
    ['edLoadT', 'edUnloadT'].forEach(function (id) {
      $(id).min = i.minExtrude; $(id).max = i.maxTemp; $(id).placeholder = i.minExtrude + ' to ' + i.maxTemp;
    });
    ED_MOTION.forEach(function (m) {
      var x = ED.before[m[1]], d = m[1] === 'partial_length' ? f.partial_unload_length : f[m[1]];
      $(m[0]).value = x != null ? x : '';
      $(m[0]).placeholder = d != null ? 'default ' + d : 'default';
    });
    $('edPL').closest('label').hidden = f.partial_unload_length == null;
    var sw = $('edSwatches'); sw.innerHTML = '';
    var cols = (st(MAC.setup).colors && st(MAC.setup).colors.length) ? st(MAC.setup).colors : DEF_SWATCH;
    cols.forEach(function (c) {
      var h = C.hex(c[1]); if (!h) return;
      sw.appendChild(el('button', { type: 'button', title: c[0] + ' #' + h, 'data-hex': h, style: 'background:#' + h,
        'aria-label': c[0], onclick: function () { $('edHex').value = h; $('edColor').value = '#' + h; edRefresh(); } }));
    });
    edRefresh();
    $('dlgEditor').showModal();
  }
  function wireEditor() {
    var SUGGEST = { PLA: 'LOW', PETG: 'LOW', TPU: 'LOW', ABS: 'HIGH', ASA: 'HIGH', PC: 'HIGH', Nylon: 'HIGH' };
    $('edMaterial').addEventListener('change', function () {
      $('edOtherWrap').hidden = this.value !== '__other';
      var cur = document.querySelector('#edProfile .sel'), prof = SUGGEST[this.value];
      if (prof && (!cur || cur.dataset.v !== 'CUSTOM'))
        document.querySelectorAll('#edProfile button').forEach(function (b) { b.classList.toggle('sel', b.dataset.v === prof); });
      edRefresh();
    });
    $('edColor').addEventListener('input', function () { $('edHex').value = this.value.slice(1).toUpperCase(); edRefresh(); });
    $('edHex').addEventListener('input', function () { var h = C.hex(this.value); if (h) $('edColor').value = '#' + h; edRefresh(); });
    ['edOther', 'edSpool', 'edLoadT', 'edUnloadT', 'edLL', 'edLS', 'edUL', 'edUS', 'edPL'].forEach(function (id) { $(id).addEventListener('input', edRefresh); });
    document.querySelectorAll('#edProfile button').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('#edProfile button').forEach(function (x) { x.classList.toggle('sel', x === b); });
        if (b.dataset.v === 'CUSTOM' && $('edLoadT').value === '') {
          var f = fvars(), hi = /^(ABS|ASA|PC|NYLON)$/.test(String(edValues().material).toUpperCase());
          $('edLoadT').value = hi ? f.high_load_temp : f.low_load_temp;
          $('edUnloadT').value = hi ? f.high_unload_temp : f.low_unload_temp;
        }
        edRefresh();
      });
    });
    $('edCancel').addEventListener('click', function () { $('dlgEditor').close(); });
    $('edForm').addEventListener('submit', function (ev) { ev.preventDefault(); });
    $('edSave').addEventListener('click', function () {
      var lines = C.editorScript(ED.n, ED.before, edValues());
      if (!lines.length) return;
      $('edSave').disabled = true;
      send(lines).then(function () { $('dlgEditor').close(); }, function (e) {
        $('edErr').textContent = 'Klipper refused: ' + (e.message || e); $('edSave').disabled = false;
      });
    });
  }

  /* ---------- settings ---------- */
  function openSettings() {
    $('setHost').value = localStorage.getItem('ktc_gui_host') || '';
    var box = $('setHeads'); box.innerHTML = '';
    S.tools.forEach(function (n) {
      var p = headPref(n);
      var sel = el('select', { 'data-n': n, class: 'mdl', title: 'Head for T' + n });
      ART.MODELS.forEach(function (m) { sel.appendChild(el('option', { value: m.id, text: m.label, selected: m.id === p.model })); });
      var ex = el('select', { class: 'ex', title: 'Extruder for T' + n });
      function fillEx(model, chosen) {
        var spec = ART.extruderChoices(model);
        ex.innerHTML = '';
        ex.disabled = !!spec.disabled;
        if (spec.disabled) {
          ex.appendChild(el('option', { value: spec.id, text: spec.label }));
          ex.title = ART.label(model) + ' has a built-in ' + spec.label;
        } else {
          ART.EXTRUDERS.forEach(function (m) {
            ex.appendChild(el('option', { value: m.id, text: m.label, selected: m.id === chosen }));
          });
          ex.title = 'Extruder for T' + n;
        }
      }
      ex.dataset.keep = p.extruderKeep || p.extruder || 'orbiter25';
      fillEx(p.model, ex.dataset.keep);
      sel.addEventListener('change', function () {
        if (!ex.disabled && (ex.value === 'orbiter25' || ex.value === 'wwbmg')) ex.dataset.keep = ex.value;
        fillEx(sel.value, ex.dataset.keep || 'orbiter25');
      });
      ex.addEventListener('change', function () {
        if (!ex.disabled && (ex.value === 'orbiter25' || ex.value === 'wwbmg')) ex.dataset.keep = ex.value;
      });
      var row = el('div', { class: 'sethead' }, [el('b', { text: 'T' + n }),
        el('div', { class: 'picks' }, [
          el('label', { class: 'pick', text: 'Head' }, [sel]),
          el('label', { class: 'pick', text: 'Extruder' }, [ex])]),
        el('input', { type: 'color', class: 'bodyc', value: p.body, title: 'T' + n + ' body colour' }),
        el('input', { type: 'color', class: 'accc', value: p.accent, title: 'T' + n + ' duct and grille colour' })]);
      box.appendChild(row);
      var ls = el('select', { class: 'ledo', title: 'T' + n + ' LED object (auto picks the LED whose name has T' + n + ')' });
      var auto = ledFor(n, '');
      ls.appendChild(el('option', { value: '', text: 'LED: auto (' + (auto ? auto.replace(LED_RE, '') : 'none found') + ')', selected: !p.led }));
      ls.appendChild(el('option', { value: 'none', text: 'LED: none', selected: lc(p.led) === 'none' }));
      var objsL = ledObjs();
      if (p.led && lc(p.led) !== 'none' && objsL.indexOf(p.led) < 0) objsL.push(p.led);
      objsL.forEach(function (o) { ls.appendChild(el('option', { value: o, text: o, selected: o === p.led })); });
      row.appendChild(el('div', { class: 'setled' }, [el('span', { class: 'muted small', text: 'LEDs' }), ls,
        el('label', { class: 'small', text: 'logo ' }, [el('input', { type: 'text', class: 'lidx', value: p.logoIdx, size: 4,
          title: 'LED number(s) for the logo, 1 = first in the chain (as SET_LED INDEX=)' })]),
        el('label', { class: 'small', text: 'nozzle ' }, [el('input', { type: 'text', class: 'nidx', value: p.nozIdx, size: 4,
          title: 'LED number(s) for the nozzle light, e.g. 2-3' })])]));
    });
    if (!S.tools.length) box.appendChild(el('div', { class: 'muted small', text: 'Connect to a printer to set up toolheads.' }));
    $('setBodyAll').value = DEF_HEAD.body; $('setAccentAll').value = DEF_HEAD.accent;
    showRepoVersion(); loadRepoVersion();
    $('guiVersion').textContent = GUI_VERSION + (S.prefsStore === 'local' ? ' (database unavailable, prefs kept in this browser)' : '');
    $('dlgSettings').returnValue = '';
    $('dlgSettings').showModal();
  }
  function wireSettings() {
    $('btnSettings').addEventListener('click', openSettings);
    $('setApplyAll').addEventListener('click', function () {
      document.querySelectorAll('#setHeads .bodyc').forEach(function (i) { i.value = $('setBodyAll').value; });
      document.querySelectorAll('#setHeads .accc').forEach(function (i) { i.value = $('setAccentAll').value; });
    });
    $('dlgSettings').addEventListener('close', function () {
      if ($('dlgSettings').returnValue !== 'ok') return;
      var heads = {};
      document.querySelectorAll('#setHeads .sethead').forEach(function (row) {
        var s = row.querySelector('.mdl');
        var lr = row.querySelector('.setled');
        var exs = row.querySelector('.ex');
        var ev = exs && !exs.disabled ? exs.value : (exs && exs.dataset.keep) || 'orbiter25';
        if (ev !== 'orbiter25' && ev !== 'wwbmg') ev = exs && exs.dataset.keep || 'orbiter25';
        if (ev !== 'orbiter25' && ev !== 'wwbmg') ev = 'orbiter25';
        heads[s.dataset.n] = { model: s.value, extruder: ev, extruderKeep: ev,
          body: row.querySelector('.bodyc').value, accent: row.querySelector('.accc').value,
          led: lr ? lr.querySelector('.ledo').value : '', logoIdx: lr ? lr.querySelector('.lidx').value.trim() || '1' : '1',
          nozIdx: lr ? lr.querySelector('.nidx').value.trim() || '2-3' : '2-3' };
      });
      var host = $('setHost').value.trim().replace(/^wss?:\/\//, '').replace(/\/.*$/, '');
      var hostChanged = host !== (localStorage.getItem('ktc_gui_host') || '');
      if (host) localStorage.setItem('ktc_gui_host', host); else localStorage.removeItem('ktc_gui_host');
      if (hostChanged) {
        var u = new URL(location.href); u.searchParams.delete('host');
        location.href = u.toString();
        return;
      }
      if (Object.keys(heads).length) {
        S.prefs.heads = heads;
        savePrefs().then(function () { if (S.ready && S.tools.length) { resolveTools(); buildAll(); updateAll(); } });
      }
    });
  }

  /* ---------- console wiring ---------- */
  function wireConsole() {
    $('conForm').addEventListener('submit', function (ev) {
      ev.preventDefault();
      var v = $('conInput').value.trim();
      if (!v || !S.connected) return;
      $('conInput').value = '';
      send(v).catch(function () {}); // errors come back as "!! ..." console lines
    });
    $('btnConClear').addEventListener('click', function () { $('conLines').textContent = ''; });
    function conArrow() { // down arrow = hide the log, up arrow = show it
      var closed = $('console').classList.contains('closed');
      $('btnConToggle').innerHTML = closed ? '&#9652;' : '&#9662;';
      $('btnConToggle').title = closed ? 'Show the console log' : 'Hide the console log';
    }
    $('btnConToggle').addEventListener('click', function () {
      var c = $('console'); c.classList.toggle('closed');
      localStorage.setItem('ktc_gui_console', c.classList.contains('closed') ? 'closed' : 'open');
      conArrow();
    });
    if (localStorage.getItem('ktc_gui_console') === 'closed') $('console').classList.add('closed');
    conArrow();
    $('btnCollapse').addEventListener('click', function () { $('controls').classList.toggle('collapsed'); });
  }

  /* ---------- start ---------- */
  KTC.app = { S: S, GUI_VERSION: GUI_VERSION, loadRepoVersion: loadRepoVersion, checkServed: checkServed, RL: RL, busyReason: busyReason, spoolState: spoolState, detect: detect };
  function start() {
    rlArrive(); wireEditor(); wireSettings(); wireConsole(); showRepoVersion();
    if (window.ResizeObserver) new ResizeObserver(function () { seatFilament(); }).observe($('rack'));
    else window.addEventListener('resize', seatFilament);
    buildEmpty('Connecting to Moonraker...');
    connect();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})(window.KTC = window.KTC || {});
