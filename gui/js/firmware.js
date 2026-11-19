/* ktc-gui: MCU firmware tab.
 * Talks to the localhost firmware helper through /firmware/ on this origin
 * (nginx proxies it). The helper is the only thing that runs make or flashtool. */
(function (KTC) {
  'use strict';
  var hooks = null;
  var lines = [];
  var busy = false;
  var last = null;
  var logEl = null;
  var rootEl = null;
  var HELPER_VERSION_WARN = 'Firmware helper is out of date \u2013 it will restart automatically, or run setup.sh';

  function el(tag, attrs, kids) { return hooks.el(tag, attrs, kids); }

  function push(line) {
    lines.push(line);
    if (lines.length > 400) lines.shift();
    paintLog();
  }
  function paintLog() {
    if (!logEl) return;
    logEl.textContent = '';
    lines.forEach(function (line) {
      var bad = line.indexOf('FAIL ') === 0 || line.indexOf('RECOVERY ') === 0;
      logEl.appendChild(el('div', { class: bad ? 'err' : '', text: line }));
    });
    logEl.scrollTop = logEl.scrollHeight;
  }

  function localBlock() {
    try { return hooks.printBlock ? hooks.printBlock() : null; }
    catch (e) { return 'The print state could not be read. Flashing is refused.'; }
  }
  function blocked() {
    var local = localBlock();
    if (local) return local;
    if (last && last.print_blocked) return last.print_message || 'Firmware update refused while the printer is not idle.';
    return null;
  }

  function methodLabel(m) {
    if (m.method === 'katapult_can') {
      var iface = m.canbus_interface || 'can0';
      var extra = m.canbus_interface_default ? ' (Klipper default; this section does not set canbus_interface)' : '';
      return 'Katapult over CAN, ' + iface + extra + ', uuid ' + (m.canbus_uuid || '?');
    }
    if (m.method === 'katapult_usb') return 'Katapult over USB, including a bootloader request, on ' + (m.serial || '?');
    if (m.method === 'direct') {
      if (m.direct_kind === 'rp2040') return 'Direct USB (RP2040 boot) on ' + (m.serial || '?');
      if (m.direct_kind === 'dfu') return 'Direct USB (STM32 DFU) on ' + (m.serial || '?');
      return 'Direct USB on ' + (m.serial || '?');
    }
    return 'No flash method chosen yet';
  }

  function request(method, path, payload) {
    var opts = { method: method, headers: {} };
    if (payload) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(payload);
    }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return { status: res.status, text: text, json: data, res: res };
      });
    });
  }

  function refresh() {
    return request('GET', '/firmware/v1/status').then(function (r) {
      if (!r.json || r.json.ok !== true) {
        last = null;
        paintOffline((r.json && r.json.error) || 'Firmware helper is not running. On the printer, run setup.sh again.');
        return;
      }
      last = r.json;
      paint();
    }, function () {
      last = null;
      paintOffline('Firmware helper is not running. On the printer, run setup.sh again so this page can reach /firmware/.');
    });
  }

  function guiVersion() {
    var app = window.KTC && window.KTC.app;
    if (app && app.GUI_VERSION) return String(app.GUI_VERSION);
    return '';
  }

  function paintHelperVersion(s) {
    var node = rootEl && rootEl.querySelector('[data-fw="helper-version"]');
    if (!node) return;
    var gui = guiVersion();
    var helper = s && s.version != null && s.version !== '' ? String(s.version) : '';
    // No status (helper down) is the offline banner, not a version mismatch.
    if (s && gui && helper !== gui) {
      node.hidden = false;
      node.textContent = HELPER_VERSION_WARN;
      node.title = 'GUI v' + gui + ', firmware helper v' + (helper || 'unknown');
    } else {
      node.hidden = true;
      node.textContent = '';
      node.title = '';
    }
  }

  function paintOffline(msg) {
    var box = rootEl.querySelector('[data-fw="mcus"]');
    var host = rootEl.querySelector('[data-fw="host"]');
    if (host) host.textContent = '';
    if (!box) return;
    box.textContent = '';
    box.appendChild(el('div', { class: 'banner bad', text: msg }));
    paintHelperVersion(null);
    paintLock();
  }

  function behindBoards() {
    return ((last && last.mcus) || []).filter(function (m) {
      return m.update_state === 'behind' && m.config_saved && m.flashable;
    });
  }

  function paintLock() {
    if (!rootEl) return;
    var why = blocked();
    var note = rootEl.querySelector('[data-fw="lock"]');
    if (note) {
      note.hidden = !why;
      note.textContent = why || '';
    }
    // Buttons are built when status arrives. A print that starts later has to
    // lock them without waiting for another status fetch.
    var mcus = (last && last.mcus) || [];
    rootEl.querySelectorAll('[data-mcu]').forEach(function (card) {
      var btn = card.querySelector('button.danger');
      if (!btn) return;
      var name = card.getAttribute('data-mcu');
      var m = null;
      for (var i = 0; i < mcus.length; i++) if (mcus[i].name === name) m = mcus[i];
      var reason = why || (m && !m.flashable ? (m.block_reason || 'Not ready to flash') : '');
      btn.disabled = !!(reason || busy);
      if (reason) btn.title = reason;
    });
    var allBtn = rootEl.querySelector('[data-fw="flashall"]');
    if (allBtn) {
      var n = behindBoards().length;
      var allWhy = why || (!last ? 'Firmware helper is not running.' : (n ? '' : 'No board is behind the host Klipper version with a saved .config.'));
      allBtn.disabled = !!(allWhy || busy);
      allBtn.title = allWhy || ('Flash ' + n + ' board' + (n === 1 ? '' : 's') + ' that are behind. Stops Klipper once, then starts it at the end.');
    }
  }

  function paint() {
    var s = last;
    var host = rootEl.querySelector('[data-fw="host"]');
    host.textContent = '';
    host.appendChild(el('span', { class: 'chip', text: 'Host Klipper ' + (s.host_version || 'unknown'),
      title: 'printer.info software_version' }));
    host.appendChild(el('span', { class: 'chip ' + (s.print_blocked ? 'bad' : 'ok'),
      text: 'print_stats ' + (s.print_state || '?') }));
    if (!s.klipper_dir_ok) {
      host.appendChild(el('span', { class: 'chip warn', text: 'Klipper source missing',
        title: s.klipper_dir }));
    }
    if (!s.flashtool) {
      host.appendChild(el('span', { class: 'chip warn', text: 'Katapult flashtool not found',
        title: 'Clone Katapult to ~/katapult. Direct USB flashing does not need it.' }));
    }
    paintHelperVersion(s);
    paintLock();
    var box = rootEl.querySelector('[data-fw="mcus"]');
    box.textContent = '';
    if (s.config_error) box.appendChild(el('div', { class: 'muted', text: s.config_error }));
    (s.config_warnings || []).forEach(function (w) {
      box.appendChild(el('div', { class: 'muted small', text: w }));
    });
    if (!s.mcus || !s.mcus.length) {
      box.appendChild(el('div', { class: 'muted', text: 'No [mcu] sections found in the Klipper config.' }));
      return;
    }
    s.mcus.forEach(function (m) { box.appendChild(mcuCard(m)); });
  }

  function updateBadge(m) {
    if (!m.version || m.update_state === 'unknown' || m.update_state == null) return null;
    if (m.update_state === 'behind') {
      return el('span', { class: 'badge req', text: 'Firmware update required',
        title: 'This MCU firmware is behind the host Klipper version' });
    }
    return el('span', { class: 'badge ok', text: 'Up to date',
      title: 'This MCU firmware is not behind the host Klipper version' });
  }

  function mcuCard(m) {
    var ver = m.version || 'not connected';
    var verChip = el('span', {
      class: 'chip ' + (m.version ? 'ok' : ''),
      text: ver,
      title: 'MCU firmware version reported by Klipper'
    });
    var badge = updateBadge(m);
    var conn = m.connection === 'can' ? 'CAN ' + (m.canbus_uuid || '')
      : m.connection === 'usb' ? 'USB ' + (m.serial || '')
      : m.connection === 'host' ? 'Linux host ' + (m.serial || '')
      : m.connection === 'both' ? 'CAN and USB'
      : 'No serial or canbus_uuid';
    var cfg = m.config_saved
      ? 'CONFIG_MCU ' + (m.config_mcu || '(not set)') + ' from your saved .config'
      : 'No .config saved. Import the menuconfig file for this board. The updater will not guess the chip.';
    var shown = m.label || m.name;
    var labelInput = el('input', {
      type: 'text', class: 'fwlabel', maxlength: '48', value: shown,
      'aria-label': 'Label for ' + m.name,
      title: 'Name for this board. Saved in printer_data/config/stealthchanger-firmware/labels.json, outside this git repo. Clear it to use the automatic name (' + (m.label_derived || m.name) + ').'
    });
    labelInput.addEventListener('change', function () { commitLabel(m, labelInput); });
    labelInput.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') labelInput.blur(); });
    var head = el('div', { class: 'fwtop' }, [
      el('div', { class: 'fwidentity' }, [
        el('div', { class: 'fwname' }, [
          el('h4', { text: shown }),
          shown !== m.name ? el('div', { class: 'muted small', text: 'Klipper name: ' + m.name }) : null
        ]),
        labelInput
      ]),
      el('div', { class: 'fwstatus' }, [badge, verChip])
    ]);
    var body = el('div', { class: 'kv' }, [
      el('span', { text: 'Connection' }), el('span', { text: conn }),
      el('span', { text: 'Method' }), el('span', { text: methodLabel(m) }),
      el('span', { text: 'Build config' }), el('span', { text: cfg })
    ]);
    var kids = [head, body];
    if (m.config_mcu_mismatch && m.config_mcu && m.reported_mcu) {
      kids.push(el('div', { class: 'banner bad', 'data-fw': 'mismatch', text:
        'Saved CONFIG_MCU is ' + m.config_mcu + ' but this board reports ' + m.reported_mcu +
        '. Create or edit a .config that matches this MCU before flashing.' }));
    }
    if (m.block_reason && !m.flashable) {
      kids.push(el('div', { class: 'muted small note', text: m.block_reason }));
    }
    var actions = el('div', { class: 'btns' });
    if (m.method_choices && m.method_choices.length) {
      m.method_choices.forEach(function (choice) {
        var label = choice === 'katapult_usb' ? 'Katapult USB' : choice === 'direct' ? 'Direct USB' : 'Katapult CAN';
        var b = el('button', { type: 'button', class: 'btn' + (m.method === choice ? ' primary' : ''), text: label,
          title: choice === 'direct'
            ? 'make flash on this serial. STM32 uses DFU and RP2040 uses the boot path, from CONFIG_MCU in your .config.'
            : 'Katapult flashtool.py on this serial, including a bootloader request.',
          onclick: function () { chooseMethod(m, choice); } });
        actions.appendChild(b);
      });
    }
    var file = el('input', { type: 'file', accept: '.config,text/plain', hidden: true });
    file.addEventListener('change', function () {
      var f = file.files && file.files[0];
      file.value = '';
      if (!f) return;
      if (f.size > 256 * 1024) { hooks.toast('That .config is larger than 256KB.'); return; }
      var reader = new FileReader();
      reader.onload = function () { importConfig(m, String(reader.result || '')); };
      reader.onerror = function () { hooks.toast('Could not read that file.'); };
      reader.readAsText(f);
    });
    actions.appendChild(el('button', { type: 'button', class: 'btn', text: 'Create',
      title: 'Fill in Klipper\'s menuconfig in this page and save a .config for this board. Does not build or flash.',
      onclick: function () { openMenuconfig(m, 'defaults'); } }));
    if (m.config_saved) {
      actions.appendChild(el('button', { type: 'button', class: 'btn', text: 'Edit',
        title: 'Open the saved .config in the menuconfig form. Saving writes the file again. Does not build or flash.',
        onclick: function () { openMenuconfig(m, 'saved'); } }));
      actions.appendChild(el('button', { type: 'button', class: 'btn', text: 'Copy to\u2026',
        title: 'Copy this saved .config onto other boards. Asks before replacing a .config that is already there. Does not build or flash.',
        onclick: function () { openCopy(m); } }));
    }
    actions.appendChild(el('button', { type: 'button', class: 'btn', text: 'Import',
      title: 'Copy a Klipper menuconfig .config into printer_data/config/stealthchanger-firmware. Updates do not replace it.',
      onclick: function () { file.click(); } }));
    actions.appendChild(file);
    var why = blocked();
    var flashWhy = why || (m.flashable ? '' : (m.block_reason || 'Not ready to flash'));
    var flash = el('button', { type: 'button', class: 'btn danger', text: 'Flash', disabled: !!(flashWhy || busy),
      title: flashWhy || ('Flash ' + m.name + '. Asks first. Stops Klipper, flashes, then starts Klipper and checks the version.'),
      onclick: function () { confirmFlash(m); } });
    actions.appendChild(flash);
    kids.push(actions);
    return el('div', { class: 'card fwboard', 'data-mcu': m.name }, kids);
  }

  function chooseMethod(m, choice) {
    request('POST', '/firmware/v1/method', { mcu: m.name, method: choice }).then(function (r) {
      if (!r.json || r.json.ok !== true) {
        hooks.toast((r.json && r.json.error) || 'Could not save the flash method.');
        return;
      }
      refresh();
    }, function () { hooks.toast('Firmware helper is not running.'); });
  }

  function importConfig(m, text) {
    request('PUT', '/firmware/v1/config', { mcu: m.name, config: text }).then(function (r) {
      if (!r.json || r.json.ok !== true) {
        hooks.toast((r.json && r.json.error) || 'Could not save that .config.');
        return;
      }
      push('LOG saved .config for ' + m.name + ' (CONFIG_MCU ' + (r.json.config_mcu || '?') + ')');
      refresh();
    }, function () { hooks.toast('Firmware helper is not running.'); });
  }

  var copyDlg = null;

  function ensureCopy() {
    if (copyDlg) return copyDlg;
    copyDlg = el('dialog', { class: 'dlg fwcfg' });
    copyDlg.appendChild(el('h2', { 'data-fw': 'copy-title', text: 'Copy .config' }));
    copyDlg.appendChild(el('div', { class: 'muted small', text:
      'Copies this board\'s saved .config. It does not build or flash. Boards that already have a .config are replaced only after you confirm.' }));
    copyDlg.appendChild(el('div', { class: 'fwfields', 'data-fw': 'copy-boards' }));
    copyDlg.appendChild(el('div', { class: 'err', 'data-fw': 'copy-err' }));
    var cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
    var go = el('button', { type: 'button', class: 'btn primary', text: 'Copy', 'data-fw': 'copy-go' });
    cancel.addEventListener('click', function () { if (copyDlg.open) copyDlg.close(); });
    go.addEventListener('click', function () { submitCopy(); });
    copyDlg.appendChild(el('div', { class: 'actions' }, [cancel, go]));
    document.body.appendChild(copyDlg);
    copyDlg.addEventListener('cancel', function (ev) { ev.preventDefault(); copyDlg.close(); });
    return copyDlg;
  }

  function openCopy(m) {
    var others = ((last && last.mcus) || []).filter(function (board) {
      return board.name && board.name !== m.name;
    });
    if (!others.length) { hooks.toast('There is no other board to copy this .config to.'); return; }
    var dlg = ensureCopy();
    dlg.querySelector('[data-fw="copy-title"]').textContent = 'Copy .config from ' + (m.label || m.name);
    dlg.querySelector('[data-fw="copy-err"]').textContent = '';
    var box = dlg.querySelector('[data-fw="copy-boards"]');
    box.textContent = '';
    others.forEach(function (board) {
      var boxInput = el('input', { type: 'checkbox', value: board.name });
      var caption = (board.label || board.name) + (board.label && board.label !== board.name ? ' (' + board.name + ')' : '');
      if (board.config_saved) caption += ' — has a .config';
      box.appendChild(el('label', { class: 'fwcheck' }, [boxInput, el('span', { text: caption })]));
    });
    dlg._source = m;
    if (!dlg.open) dlg.showModal();
  }

  function submitCopy() {
    if (!copyDlg || !copyDlg._source) return;
    var source = copyDlg._source;
    var picked = [];
    var replace = [];
    copyDlg.querySelectorAll('input[type="checkbox"]').forEach(function (node) {
      if (!node.checked) return;
      picked.push(node.value);
      var board = null;
      ((last && last.mcus) || []).forEach(function (item) { if (item.name === node.value) board = item; });
      if (board && board.config_saved) replace.push((board.label || board.name));
    });
    var err = copyDlg.querySelector('[data-fw="copy-err"]');
    if (!picked.length) { err.textContent = 'Tick at least one board.'; return; }
    err.textContent = '';
    function post(overwrite) {
      request('POST', '/firmware/v1/copy', { mcu: source.name, targets: picked, overwrite: overwrite }).then(function (r) {
        if (!r.json || r.json.ok !== true) {
          var msg = (r.json && r.json.error) || 'Could not copy that .config.';
          if (copyDlg && copyDlg.open) copyDlg.querySelector('[data-fw="copy-err"]').textContent = msg;
          else hooks.toast(msg);
          return;
        }
        if (copyDlg && copyDlg.open) copyDlg.close();
        var n = (r.json.copied || []).length;
        push('LOG copied .config from ' + source.name + ' to ' + n + ' board' + (n === 1 ? '' : 's') +
          ' (CONFIG_MCU ' + (r.json.config_mcu || '?') + ')');
        refresh();
      }, function () { hooks.toast('Firmware helper is not running.'); });
    }
    if (!replace.length) { post(false); return; }
    if (copyDlg.open) copyDlg.close();
    hooks.confirmDlg(
      'Replace saved .config?',
      'These boards already have a .config. Copying replaces those files. Nothing is built or flashed.',
      replace,
      'Replace',
      'Cancel'
    ).then(function (ok) { if (ok) post(true); });
  }

  function confirmFlash(m) {
    var why = blocked();
    if (why) { hooks.toast(why); paint(); return; }
    if (!m.flashable) { hooks.toast(m.block_reason || 'This MCU is not ready to flash.'); return; }
    var summary = methodLabel(m) + (m.config_mcu ? '. CONFIG_MCU ' + m.config_mcu + ' from your .config.' : '');
    hooks.confirmDlg(
      'Flash ' + m.name + '?',
      'This builds firmware from the saved .config, stops Klipper, flashes this MCU, starts Klipper, and checks that it reconnects with a new version. Do this only when the printer is idle.',
      summary,
      'Flash',
      'Cancel'
    ).then(function (ok) {
      if (!ok) return;
      var again = blocked();
      if (again) { hooks.toast(again); paint(); return; }
      runFlash(m);
    });
  }

  function runFlash(m) {
    busy = true;
    paint();
    push('LOG starting flash of ' + (m.label || m.name));
    streamPost('/firmware/v1/flash', { mcu: m.name, confirm: true });
  }

  function commitLabel(m, input) {
    var val = String(input.value || '').replace(/\s+/g, ' ').trim();
    var effective = m.label || m.name;
    if (val === effective) return;
    var save = (val === (m.label_derived || m.name)) ? '' : val;
    if ((m.label_custom || '') === save) return;
    request('POST', '/firmware/v1/label', { mcu: m.name, label: save }).then(function (r) {
      if (!r.json || r.json.ok !== true) {
        hooks.toast((r.json && r.json.error) || 'Could not save that label.');
        input.value = effective;
        return;
      }
      refresh();
    }, function () { hooks.toast('Firmware helper is not running.'); });
  }

  function confirmFlashAll() {
    var why = blocked();
    if (why) { hooks.toast(why); paint(); return; }
    var list = behindBoards();
    if (!list.length) { hooks.toast('No board is behind the host Klipper version with a saved .config.'); return; }
    hooks.confirmDlg(
      'Flash all boards that are behind?',
      'This builds each board, stops Klipper once, flashes them in order, and starts Klipper at the end. It stops on the first failure and starts Klipper again. The print state is checked before each board.',
      list.map(function (m) { return (m.label || m.name) + ' — ' + methodLabel(m); }),
      'Flash all',
      'Cancel'
    ).then(function (ok) {
      if (!ok) return;
      var again = blocked();
      if (again) { hooks.toast(again); paint(); return; }
      runFlashAll();
    });
  }

  function runFlashAll() {
    busy = true;
    paint();
    push('LOG starting flash of every board that is behind');
    streamPost('/firmware/v1/flash_all', { confirm: true });
  }

  function streamPost(path, payload) {
    var buf = '';
    function take(chunk) {
      buf += chunk;
      var parts = buf.split('\n');
      buf = parts.pop();
      parts.forEach(function (line) { if (line) push(line); });
    }
    fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      var ctype = (res.headers.get('content-type') || '');
      if (ctype.indexOf('application/json') !== -1 || !res.ok && !res.body) {
        return res.text().then(function (text) {
          var msg = text;
          try { msg = (JSON.parse(text).error) || text; } catch (e) { /* plain text */ }
          push('FAIL ' + msg);
        });
      }
      if (!res.body || !res.body.getReader) return res.text().then(take);
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          take(dec.decode(r.value, { stream: true }));
          return pump();
        });
      }
      return pump();
    }).then(function () {
      if (buf) push(buf);
    }, function (e) {
      push('FAIL ' + (e && e.message ? e.message : e));
      push('RECOVERY If Klipper is not running, start it with: sudo systemctl start klipper. Your menuconfig file was not changed.');
    }).then(function () {
      busy = false;
      refresh();
    });
  }

  var formDlg = null;
  var formGen = 0;
  var formState = null;

  function ensureForm() {
    if (formDlg) return formDlg;
    formDlg = el('dialog', { class: 'dlg fwcfg' });
    formDlg.appendChild(el('h2', { 'data-fw': 'form-title', text: 'Menuconfig' }));
    formDlg.appendChild(el('div', { class: 'muted small', text:
      'This writes the .config for this board from Klipper\'s Kconfig, the same way menuconfig does. It does not build or flash.' }));
    formDlg.appendChild(el('div', { class: 'muted small', 'data-fw': 'form-mcu' }));
    formDlg.appendChild(el('div', { class: 'muted small fwprefill', 'data-fw': 'form-note' }));
    formDlg.appendChild(el('div', { class: 'fwfields', 'data-fw': 'form-fields' }));
    formDlg.appendChild(el('div', { class: 'err', 'data-fw': 'form-err' }));
    var cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
    var save = el('button', { type: 'button', class: 'btn primary', text: 'Save .config', 'data-fw': 'form-save' });
    cancel.addEventListener('click', function () { closeForm(); });
    save.addEventListener('click', function () { saveForm(); });
    formDlg.appendChild(el('div', { class: 'actions' }, [cancel, save]));
    document.body.appendChild(formDlg);
    formDlg.addEventListener('cancel', function (ev) { ev.preventDefault(); closeForm(); });
    return formDlg;
  }

  function closeForm() {
    formGen += 1;
    formState = null;
    if (formDlg && formDlg.open) formDlg.close();
  }

  function openMenuconfig(m, base) {
    var dlg = ensureForm();
    var gen = ++formGen;
    formState = { mcu: m.name, label: m.label || m.name, base: base, values: {}, gen: gen, req: 0, timer: null };
    dlg.querySelector('[data-fw="form-title"]').textContent = (base === 'saved' ? 'Edit' : 'Create') + ' .config — ' + (m.label || m.name);
    dlg.querySelector('[data-fw="form-err"]').textContent = '';
    dlg.querySelector('[data-fw="form-fields"]').textContent = 'Reading Kconfig…';
    if (!dlg.open) dlg.showModal();
    loadForm(gen, false);
  }

  function fieldSignature(fields) {
    var out = [];
    function walk(list) {
      (list || []).forEach(function (field) {
        var opts = (field.options || []).map(function (opt) { return opt.name; }).join(',');
        out.push([field.kind, field.name || '', field.prompt || '', opts].join('\u001f'));
        if (field.children) walk(field.children);
      });
    }
    walk(fields);
    return out.join('\u001e');
  }

  function eachField(fields, fn) {
    (fields || []).forEach(function (field) {
      fn(field);
      if (field.children) eachField(field.children, fn);
    });
  }

  function fieldControlId(field) {
    if (field.kind === 'choice') return 'choice:' + field.prompt;
    return field.name || null;
  }

  function findControl(root, id) {
    if (!id) return null;
    var nodes = root.querySelectorAll('[data-field]');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute('data-field') === id) return nodes[i];
    }
    return null;
  }

  function updateFieldsInPlace(root, fields) {
    // A focused text input keeps its node, value, and caret. Setting .value
    // or rebuilding it jumps the cursor to the start and jumbles the next key.
    eachField(fields, function (field) {
      var node = findControl(root, fieldControlId(field));
      if (!node || node === document.activeElement) return;
      if (field.kind === 'bool') {
        node.checked = !!field.value;
        return;
      }
      var next = field.value == null ? '' : String(field.value);
      if (node.value !== next) node.value = next;
    });
  }

  function captureCaret() {
    var active = document.activeElement;
    if (!active || !active.getAttribute || !formDlg || !formDlg.contains(active)) return null;
    var id = active.getAttribute('data-field');
    if (!id) return null;
    var text = active.tagName === 'INPUT' && active.type === 'text';
    return {
      id: id,
      text: text,
      start: text && typeof active.selectionStart === 'number' ? active.selectionStart : null,
      end: text && typeof active.selectionEnd === 'number' ? active.selectionEnd : null,
      value: text ? active.value : null
    };
  }

  function restoreCaret(root, saved) {
    if (!saved) return;
    var node = findControl(root, saved.id);
    if (!node) return;
    if (saved.text && saved.value != null) node.value = saved.value;
    try { node.focus({ preventScroll: true }); }
    catch (e) { node.focus(); }
    if (saved.text && saved.start != null && node.setSelectionRange) {
      var len = node.value.length;
      var start = Math.max(0, Math.min(saved.start, len));
      var end = Math.max(0, Math.min(saved.end == null ? start : saved.end, len));
      node.setSelectionRange(start, end);
    }
  }

  function loadForm(gen, fromEdit) {
    if (!formState || formState.gen !== gen) return;
    var req = ++formState.req;
    request('POST', '/firmware/v1/kconfig', {
      mcu: formState.mcu, base: formState.base, values: formState.values
    }).then(function (r) {
      if (!formState || formState.gen !== gen || formState.req !== req) return;
      var err = formDlg.querySelector('[data-fw="form-err"]');
      var box = formDlg.querySelector('[data-fw="form-fields"]');
      var note = formDlg.querySelector('[data-fw="form-note"]');
      if (!r.json || r.json.ok !== true) {
        err.textContent = (r.json && r.json.error) || 'Could not read Kconfig.';
        if (!box.childNodes.length) box.textContent = '';
        return;
      }
      err.textContent = '';
      if (note) note.textContent = r.json.prefill_note || '';
      formDlg.querySelector('[data-fw="form-mcu"]').textContent = r.json.config_mcu
        ? ('CONFIG_MCU will be ' + r.json.config_mcu)
        : 'CONFIG_MCU is not set yet. Pick a micro-controller before saving.';
      var fields = r.json.fields || [];
      var sig = fieldSignature(fields);
      var scroll = box.scrollTop;
      if (fromEdit && box.getAttribute('data-sig') === sig && box.childNodes.length) {
        updateFieldsInPlace(box, fields);
      } else {
        var caret = fromEdit ? captureCaret() : null;
        box.textContent = '';
        fields.forEach(function (field) { box.appendChild(renderField(field, gen)); });
        if (!box.childNodes.length) box.appendChild(el('div', { class: 'muted', text: 'No visible options. Check that Klipper\'s Kconfig loaded.' }));
        box.setAttribute('data-sig', sig);
        if (caret) restoreCaret(box, caret);
        box.scrollTop = scroll;
      }
    }, function () {
      if (!formState || formState.gen !== gen || formState.req !== req) return;
      formDlg.querySelector('[data-fw="form-err"]').textContent = 'Firmware helper is not running.';
    });
  }

  function scheduleForm(gen, immediate) {
    if (!formState || formState.gen !== gen) return;
    if (formState.timer) clearTimeout(formState.timer);
    if (immediate) { loadForm(gen, true); return; }
    formState.timer = setTimeout(function () { loadForm(gen, true); }, 400);
  }

  function rememberForm(node, gen) {
    if (!formState || formState.gen !== gen) return;
    var cur = collectFields(node.closest('.fwfields') || formDlg);
    Object.keys(cur).forEach(function (k) { formState.values[k] = cur[k]; });
  }

  function renderField(field, gen) {
    if (field.kind === 'menu') {
      var kids = (field.children || []).map(function (kid) { return renderField(kid, gen); });
      return el('div', { class: 'fwmenu' }, [el('h4', { class: 'fwmenu-title', text: field.prompt })].concat(kids));
    }
    if (field.kind === 'comment') return el('div', { class: 'muted small fwcomment', text: field.prompt });
    var control;
    if (field.kind === 'choice') {
      var names = (field.options || []).map(function (o) { return o.name; });
      control = el('select', { 'data-choice': names.join(','), 'data-field': 'choice:' + field.prompt, title: field.help || field.prompt });
      (field.options || []).forEach(function (o) {
        var opt = el('option', { value: o.name, text: o.prompt });
        if (o.name === field.value) opt.selected = true;
        control.appendChild(opt);
      });
      control.addEventListener('change', function () { rememberForm(control, gen); scheduleForm(gen, true); });
    } else if (field.kind === 'bool') {
      control = el('input', { type: 'checkbox', 'data-sym': field.name, 'data-field': field.name, title: field.help || field.prompt });
      control.checked = !!field.value;
      control.addEventListener('change', function () { rememberForm(control, gen); scheduleForm(gen, true); });
    } else if (field.kind === 'tristate') {
      control = el('select', { 'data-sym': field.name, 'data-field': field.name, title: field.help || field.prompt });
      ['n', 'm', 'y'].forEach(function (v) {
        var opt = el('option', { value: v, text: v });
        if (v === field.value) opt.selected = true;
        control.appendChild(opt);
      });
      control.addEventListener('change', function () { rememberForm(control, gen); scheduleForm(gen, true); });
    } else {
      control = el('input', {
        type: 'text', 'data-sym': field.name, 'data-field': field.name, value: field.value == null ? '' : String(field.value),
        title: field.help || field.prompt
      });
      // Typing only stores the value. Dependencies refresh on a short debounce,
      // on change, and on blur, without replacing this input while it is focused.
      control.addEventListener('input', function () { rememberForm(control, gen); scheduleForm(gen, false); });
      control.addEventListener('change', function () { rememberForm(control, gen); scheduleForm(gen, true); });
      control.addEventListener('blur', function () {
        rememberForm(control, gen);
        setTimeout(function () { scheduleForm(gen, true); }, 0);
      });
    }
    var label = field.kind === 'bool'
      ? el('label', { class: 'fwcheck', title: field.help || '' }, [control, el('span', { text: field.prompt })])
      : el('label', { title: field.help || '' }, [field.prompt, control]);
    var wrapKids = [label];
    (field.children || []).forEach(function (kid) { wrapKids.push(renderField(kid, gen)); });
    if (field.children && field.children.length) return el('div', { class: 'fwmenu' }, wrapKids);
    return label;
  }

  function collectFields(root) {
    var values = {};
    root.querySelectorAll('[data-sym]').forEach(function (node) {
      var name = node.getAttribute('data-sym');
      if (!name) return;
      if (node.type === 'checkbox') values[name] = node.checked ? 'y' : 'n';
      else values[name] = node.value;
    });
    root.querySelectorAll('select[data-choice]').forEach(function (node) {
      var selected = node.value;
      (node.getAttribute('data-choice') || '').split(',').forEach(function (name) {
        if (name) values[name] = name === selected ? 'y' : 'n';
      });
    });
    return values;
  }

  function saveForm() {
    if (!formState) return;
    var gen = formState.gen;
    var cur = collectFields(formDlg.querySelector('[data-fw="form-fields"]'));
    Object.keys(cur).forEach(function (k) { formState.values[k] = cur[k]; });
    var saveBtn = formDlg.querySelector('[data-fw="form-save"]');
    saveBtn.disabled = true;
    request('PUT', '/firmware/v1/kconfig', {
      mcu: formState.mcu, base: formState.base, values: formState.values
    }).then(function (r) {
      saveBtn.disabled = false;
      if (!formState || formState.gen !== gen) return;
      if (!r.json || r.json.ok !== true) {
        formDlg.querySelector('[data-fw="form-err"]').textContent = (r.json && r.json.error) || 'Could not save that .config.';
        return;
      }
      push('LOG saved .config for ' + formState.mcu + ' (CONFIG_MCU ' + (r.json.config_mcu || '?') + ')');
      closeForm();
      refresh();
    }, function () {
      saveBtn.disabled = false;
      if (formDlg) formDlg.querySelector('[data-fw="form-err"]').textContent = 'Firmware helper is not running.';
    });
  }

  function build(pane, h) {
    hooks = h;
    rootEl = pane;
    pane.appendChild(el('div', { class: 'grp' }, [
      el('h4', { text: 'Firmware' }),
      el('div', { class: 'fwtop', 'data-fw': 'host' }),
      el('div', { class: 'fwhelper-warn', 'data-fw': 'helper-version', hidden: true }),
      el('div', { class: 'banner bad', 'data-fw': 'lock', hidden: true }),
      el('div', { class: 'muted small note', text:
        'Menuconfig for each MCU is stored in printer_data/config/stealthchanger-firmware/ and is not part of this git repo, so an update does not replace it. ' +
        'Build files go in printer_data/stealthchanger-firmware-build/, outside the config folder. ' +
        'Create or Edit writes that file from Klipper\'s Kconfig and does not flash. The chip comes only from CONFIG_MCU. ' +
        'A canbus_uuid defaults to Katapult over CAN. A /dev/serial/by-id serial defaults to Katapult over USB when Katapult is installed, and to direct USB otherwise. ' +
        'Board names are saved in labels.json in that same folder.' }),
      el('div', { class: 'btns' }, [
        el('button', { type: 'button', class: 'btn', text: 'Refresh', title: 'Read MCUs, versions, and saved configs again',
          onclick: function () { refresh(); } }),
        el('button', { type: 'button', class: 'btn danger', text: 'Flash all', 'data-fw': 'flashall', disabled: true,
          title: 'Flash every board that is behind the host version and has a .config',
          onclick: function () { confirmFlashAll(); } })
      ])
    ]));
    pane.appendChild(el('div', { class: 'fwboards', 'data-fw': 'mcus' }));
    logEl = el('pre', { class: 'fwlog', id: 'fwLog' });
    pane.appendChild(el('div', { class: 'grp' }, [
      el('h4', { text: 'Flash log' }),
      el('div', { class: 'btns' }, [
        el('button', { type: 'button', class: 'btn', text: 'Clear log', onclick: function () { lines = []; paintLog(); } })
      ]),
      logEl
    ]));
    paintLog();
    if (hooks.onUpdate) hooks.onUpdate(function () { paintLock(); });
  }

  KTC.firmware = { build: build, onShow: function () { return refresh(); } };
})(window.KTC = window.KTC || {});
