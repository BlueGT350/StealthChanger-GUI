/* ktc-gui: pure G-code builders. No DOM, so they can be unit tested in node.
 * Klipper rules these follow:
 *  - a ; or # starts a comment, even inside quotes, so text values are
 *    stripped of # ; ' " \ and newlines, then double-quoted (shlex);
 *  - COLOR is 6 hex digits without the hash;
 *  - tools are addressed by NUMBER (TOOL=n / T=n), never list index.
 */
(function (KTC) {
  'use strict';
  var C = {};
  C.clean = function (s, max) {
    return String(s == null ? '' : s).replace(/[#;'"\\\r\n\t*]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, max || 40);
  };
  C.q = function (s) { return '"' + C.clean(s) + '"'; };
  C.hex = function (c) {
    var h = String(c || '').replace(/^#/, '').toUpperCase();
    return /^[0-9A-F]{6}$/.test(h) ? h : null;
  };
  C.num = function (v) { var n = Number(v); return (v === '' || v == null || !isFinite(n)) ? null : n; };
  var fmt = function (n) { return String(Math.round(n * 100) / 100); };

  /* Spool editor -> one multi-line script.
   * before/after: {material, color, spool, profile, load_temp, unload_temp,
   *                load_length, load_speed, unload_length, unload_speed, partial_length}
   * motion values: number = override, null = use the default. */
  C.MOTION = [['load_length', 'LOAD_LENGTH'], ['load_speed', 'LOAD_SPEED'], ['unload_length', 'UNLOAD_LENGTH'],
              ['unload_speed', 'UNLOAD_SPEED'], ['partial_length', 'PARTIAL_LENGTH']];
  C.editorScript = function (t, before, after, opts) {
    opts = opts || {};
    var lines = [], parts = [];
    var mat = C.clean(after.material, 20).toUpperCase();
    if (mat && mat !== C.clean(before.material, 20).toUpperCase()) parts.push('MATERIAL=' + C.q(mat));
    var col = C.hex(after.color);
    if (col && col !== C.hex(before.color)) parts.push('COLOR=' + col);
    var sp = C.clean(after.spool);
    if (sp !== C.clean(before.spool)) parts.push('SPOOL=' + C.q(sp));
    var prof = String(after.profile || '').toUpperCase();
    if ((prof === 'HIGH' || prof === 'LOW') && prof !== String(before.profile || '').toUpperCase()) parts.push('PROFILE=' + prof);
    if (parts.length) lines.push('SET_TOOL_FILAMENT TOOL=' + t + ' ' + parts.join(' '));
    if (prof === 'CUSTOM' && (before.profile !== 'CUSTOM' || after.load_temp !== before.load_temp ||
                              after.unload_temp !== before.unload_temp)) {
      var l = 'SET_FILAMENT_TEMP TOOL=' + t + ' TYPE=CUSTOM';
      if (after.load_temp != null) l += ' LOAD_TEMP=' + Math.round(after.load_temp);
      if (after.unload_temp != null) l += ' UNLOAD_TEMP=' + Math.round(after.unload_temp);
      lines.push(l);
    }
    var cleared = C.MOTION.some(function (m) { return before[m[0]] != null && after[m[0]] == null; });
    var mparts = [];
    C.MOTION.forEach(function (m) {
      var a = after[m[0]];
      if (a == null) return;
      if (cleared || a !== before[m[0]]) mparts.push(m[1] + '=' + fmt(a));
    });
    if (cleared) lines.push('SET_FILAMENT_MOTION TOOL=' + t + ' RESET=1');
    if (mparts.length) lines.push('SET_FILAMENT_MOTION TOOL=' + t + ' ' + mparts.join(' '));
    return lines;
  };

  /* Editor validation: returns {field: message}. lim = {min_temp, max_temp, load_length, partial_margin} */
  C.validate = function (v, lim) {
    var e = {};
    if (!C.hex(v.color)) e.color = 'Colour must be 6 hex digits';
    if (!C.clean(v.material, 20)) e.material = 'Material is required';
    if (v.profile === 'CUSTOM') {
      ['load_temp', 'unload_temp'].forEach(function (k) {
        var x = v[k];
        if (x == null) e[k] = 'Required for CUSTOM';
        else if (x > lim.max_temp || x < lim.min_temp) e[k] = 'Allowed ' + lim.min_temp + ' to ' + lim.max_temp + 'C';
      });
    }
    [['load_length', 1, 2000], ['unload_length', 1, 2000], ['load_speed', 10, 12000], ['unload_speed', 10, 12000],
     ['partial_length', 1, 2000]].forEach(function (f) {
      var x = v[f[0]];
      if (x != null && (x < f[1] || x > f[2])) e[f[0]] = 'Allowed ' + f[1] + ' to ' + f[2];
    });
    var ll = v.load_length != null ? v.load_length : lim.load_length;
    var pl = v.partial_length != null ? v.partial_length : lim.partial_default;
    if (pl != null && ll != null && (pl < 5 || pl > ll - lim.partial_margin))
      e.partial_length = 'Must be 5 to ' + (ll - lim.partial_margin) + ' mm (load length ' + ll + ' minus ' + lim.partial_margin + ')';
    return e;
  };

  /* Calibrate ONE tool offset, the CALIBRATE_ALL_OFFSETS steps for tool n only.
   * extOf: {toolNumber: extruderName}. ref = tool_numbers[0]. */
  C.calibrateOne = function (n, ref, extOf) {
    if (n === ref) throw new Error('T' + n + ' is the reference tool, its offset is 0 by definition');
    if (!extOf[n] || !extOf[ref]) throw new Error('unknown tool T' + n);
    return [
      'SELECT_TOOL T=' + ref + ' RESTORE_AXIS=XYZ', 'STOP_CRASH_DETECTION', 'CALIBRATE_MOVE_OVER_PROBE',
      'M104 S150', 'TEMPERATURE_WAIT SENSOR=' + extOf[ref] + ' MINIMUM=150', 'TOOL_LOCATE_SENSOR', 'M104 S0',
      'SELECT_TOOL T=' + n + ' RESTORE_AXIS=Z', 'STOP_CRASH_DETECTION', 'M104 S150 T' + n, 'CALIBRATE_MOVE_OVER_PROBE',
      'TEMPERATURE_WAIT SENSOR=' + extOf[n] + ' MINIMUM=150', 'TOOL_CALIBRATE_TOOL_OFFSET', '_SAVE_TOOL_OFFSET T=' + n,
      'M104 S0', 'SELECT_TOOL T=' + ref + ' RESTORE_AXIS=XZ', 'TURN_OFF_HEATERS'
    ];
  };
  /* Spool hover halo: keep the filament's own hue but make sure the glow
   * reads on the dark page (#121212): lighten the same hue until the WCAG
   * contrast ratio is at least 3:1. Near-black, near-white and grey get a
   * soft light neutral glow instead. Returns 'rgba(r,g,b,a)'. */
  function lum(r, g, b) {
    var c = [r, g, b].map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  C.contrast = function (a, b) {
    var la = lum.apply(null, a), lb = lum.apply(null, b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };
  function rgb2hsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, s = 0, d = mx - mn;
    if (d) {
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h /= 6;
    }
    return [h, s, l];
  }
  function hsl2rgb(h, s, l) {
    if (!s) return [l, l, l].map(function (v) { return Math.round(v * 255); });
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    var f = function (t) {
      t = (t + 1) % 1;
      return t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
    };
    return [f(h + 1 / 3), f(h), f(h - 1 / 3)].map(function (v) { return Math.round(v * 255); });
  }
  C.halo = function (hex, bg) {
    var h = C.hex(hex), back = bg || [0x17, 0x19, 0x1c];   // top of the rack gradient
    if (!h) return 'rgba(220,224,228,0.55)';
    var c = [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    var hsl = rgb2hsl(c[0], c[1], c[2]);
    // grey, black or white filament: a soft light neutral glow
    if (hsl[1] < 0.18 || hsl[2] < 0.06) return 'rgba(220,224,228,0.55)';
    var l = hsl[2], s = Math.max(hsl[1], 0.45), out = c;
    if (C.contrast(c, back) < 3) {
      for (l = hsl[2]; l <= 0.92; l += 0.02) {
        out = hsl2rgb(hsl[0], s, l);
        if (C.contrast(out, back) >= 3) break;
      }
    }
    return 'rgba(' + out.join(',') + ',0.85)';
  };
  /* Mark loaded / empty: the real macro is SET_FILAMENT_LOADED (filament_macros.cfg) */
  C.markLoaded = function (n, on) { return 'SET_FILAMENT_LOADED TOOL=' + n + ' LOADED=' + (on ? 1 : 0); };

  /* ---- SAVE_CONFIG "conflicts with included value" check ----
   * Klipper (configfile.py cmd_SAVE_CONFIG): the #*# block plus the pending
   * changes are merged; duplicates in the MAIN file are commented out
   * automatically, but if any of those section/option pairs is also set in an
   * INCLUDED file, SAVE_CONFIG stops with
   *   SAVE_CONFIG section 'tool T1' option 'gcode_x_offset' conflicts with included value
   * These helpers reproduce that check from the files Moonraker serves. */
  C.AUTOSAVE_MARK = '#*# <---------------------- SAVE_CONFIG ---------------------->';
  function parseLines(lines, withValues) {
    var out = {}, sec = null;
    lines.forEach(function (raw) {
      var line = raw.replace(/[#;].*$/, '').replace(/\s+$/, '');
      if (!line) return;
      if (/^\s/.test(line)) return; // continuation of a multi-line value (gcode etc.)
      var m = /^\[([^\]]+)\]/.exec(line);
      if (m) { sec = m[1].trim(); out[sec] = out[sec] || {}; return; }
      if (sec === null) return;
      var opt = line.replace(/[:=].*$/, '').trim().toLowerCase();
      if (!opt) return;
      var val = withValues ? line.slice(line.search(/[:=]/) + 1).trim() : true;
      out[sec][opt] = val;
    });
    return out;
  }
  C.cfgParse = function (text) {
    var all = String(text || '').split(/\r?\n/), regular = all, auto = [];
    var at = all.findIndex(function (l) { return l.trim() === C.AUTOSAVE_MARK; });
    if (at >= 0) {
      regular = all.slice(0, at);
      auto = all.slice(at + 1).filter(function (l) { return /^#\*#/.test(l); }).map(function (l) { return l.replace(/^#\*# ?/, ''); });
    }
    var sections = parseLines(regular, false), includes = [];
    Object.keys(sections).forEach(function (k) {
      var m = /^include\s+(.+)$/.exec(k);
      if (m) { includes.push(m[1].trim()); delete sections[k]; }
    });
    return { sections: sections, includes: includes, autosave: parseLines(auto, true) };
  };
  C.pathJoin = function (dir, rel) { // -> normalised path relative to the config root, or null if it leaves it
    var parts = (rel.charAt(0) === '/' ? [] : String(dir || '').split('/')).concat(rel.split('/')), out = [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (!p || p === '.') continue;
      if (p === '..') { if (!out.length) return null; out.pop(); } else out.push(p);
    }
    return out.join('/');
  };
  C.dirOf = function (path) { var i = path.lastIndexOf('/'); return i < 0 ? '' : path.slice(0, i); };
  C.globRe = function (pat) {
    var re = '';
    for (var i = 0; i < pat.length; i++) {
      var ch = pat[i];
      if (ch === '*') re += '[^/]*';
      else if (ch === '?') re += '[^/]';
      else if (ch === '[') { var j = pat.indexOf(']', i + 1); if (j > i) { re += '[' + pat.slice(i + 1, j).replace(/^!/, '^') + ']'; i = j; } else re += '\\['; }
      else re += ch.replace(/[.+^${}()|\\]/g, '\\$&');
    }
    return new RegExp('^' + re + '$');
  };
  /* autosave/pending: {section: {option: value}}; included: [{file, sections}] */
  C.cfgConflicts = function (autosave, pending, included) {
    var want = {}, out = [];
    [autosave || {}, pending || {}].forEach(function (src) {
      Object.keys(src).forEach(function (sec) {
        Object.keys(src[sec] || {}).forEach(function (opt) {
          var k = sec + '\u0000' + opt.toLowerCase();
          if (!want[k]) want[k] = { section: sec, option: opt.toLowerCase(), pending: src === pending };
        });
      });
    });
    Object.keys(want).forEach(function (k) {
      var w = want[k];
      (included || []).forEach(function (f) {
        if (f.sections[w.section] && f.sections[w.section][w.option] !== undefined)
          out.push({ section: w.section, option: w.option, file: f.file, pending: w.pending });
      });
    });
    return out;
  };

  C.toolTemp = function (n, target, ext, hasSetToolTemp) {
    target = Math.max(0, Math.round(target));
    return hasSetToolTemp ? 'SET_TOOL_TEMPERATURE T=' + n + ' TARGET=' + target
                          : 'SET_HEATER_TEMPERATURE HEATER=' + ext + ' TARGET=' + target;
  };
  C.partFan = function (n, pct) {
    pct = Math.max(0, Math.min(100, Math.round(pct)));
    return pct === 0 ? 'M107 T' + n : 'M106 S' + Math.round(pct * 2.55) + ' T' + n;
  };
  KTC.cmd = C;
  if (typeof module !== 'undefined') module.exports = C;
})(typeof window !== 'undefined' ? (window.KTC = window.KTC || {}) : {});
