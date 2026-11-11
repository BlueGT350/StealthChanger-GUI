// Head / extruder composition, migration, and the built-in selector.
// Run: node gui/dev/test_extruder.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const artPath = path.join(__dirname, '..', 'js', 'art.js');
const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(artPath, 'utf8'), sandbox, { filename: artPath });
const A = sandbox.window.KTC.art;
let n = 0;
function t(name, fn) {
  fn();
  n++;
  console.log('  ok    ' + name);
}
function svg(model, extruder, colours) {
  return A.toolhead(model, colours || { body: '#a3a7ab', accent: '#D41216' }, extruder);
}
function paths(s) {
  const out = [];
  const re = /<path\b[^>]*\bd="([^"]+)"/g;
  let m;
  while ((m = re.exec(s))) if (m[1].length > 80) out.push(m[1]);
  return out;
}
function group(s) {
  const i = s.indexOf('class="extruder"');
  if (i < 0) return '';
  return s.slice(i, s.indexOf('</g>', i));
}
function pathYs(s) {
  const ys = [];
  const re = /\bd="([^"]+)"/g;
  let m;
  while ((m = re.exec(s))) {
    const nums = m[1].match(/-?\d+(?:\.\d+)?/g) || [];
    for (let i = 1; i < nums.length; i += 2) ys.push(parseFloat(nums[i]));
  }
  return ys;
}

const baked = svg('anthead');
const antWw = svg('anthead', 'wwbmg');
const dbOrb = svg('dragonburner', 'orbiter25');
const dbWw = svg('dragonburner', 'wwbmg');
const sb = svg('stealthburner', 'wwbmg');
const orbOnly = paths(baked).filter((d) => antWw.indexOf(d) < 0);
const dbOrbPeek = svg('dragonburner', 'orbiter25');
const orbFace = orbOnly.filter((d) => dbOrbPeek.indexOf(d) >= 0)[0];
const wwFace = paths(group(antWw)).filter((d) => baked.indexOf(d) < 0)[0];

t('default anthead is the assembled Orbiter, drawn once', () => {
  assert.ok(baked.indexOf('data-model="anthead"') >= 0);
  assert.ok(baked.indexOf('data-extruder="orbiter25"') >= 0);
  assert.ok(baked.indexOf('class="extruder"') < 0, 'Orbiter would be drawn on top of the one already in the head');
  assert.ok(baked.indexOf('cx="67.55"') >= 0, 'Orbiter hex cap missing');
  assert.ok(orbOnly.length > 10, 'no Orbiter-only faces');
  assert.ok(orbFace, 'Orbiter face was not reused for the other heads');
  assert.strictEqual(baked.split(orbFace).length - 1, 1);
});

t('anthead wristwatch replaces the Orbiter', () => {
  assert.ok(antWw.indexOf('data-extruder="wwbmg"') >= 0);
  assert.ok(antWw.indexOf('class="extruder"') >= 0);
  assert.ok(antWw.indexOf('cx="67.55"') < 0, 'Orbiter hex cap still on the head');
  assert.ok(wwFace && wwFace.length > 80);
  assert.ok(antWw.indexOf(orbFace) < 0);
  assert.ok(antWw.indexOf('translate(0,0)') >= 0 || antWw.indexOf('translate(0, 0)') >= 0);
});

t('dragonburner shows either extruder on the cowl mount', () => {
  assert.ok(dbOrb.indexOf('class="extruder" data-extruder="orbiter25"') >= 0);
  assert.ok(dbOrb.indexOf('translate(0,-5.5)') >= 0);
  assert.ok(dbOrb.indexOf(orbFace) >= 0);
  assert.ok(dbOrb.indexOf('cx="67.55"') >= 0);
  assert.ok(dbWw.indexOf('data-extruder="wwbmg"') >= 0);
  assert.ok(dbWw.indexOf('translate(0,-5.5)') >= 0);
  assert.ok(dbWw.indexOf(wwFace) >= 0);
  assert.ok(dbWw.indexOf(orbFace) < 0, 'Orbiter left on the Dragon Wristwatch');
  assert.ok(dbOrb.indexOf('class="nozlight" cx="80" cy="210"') >= 0);
  assert.ok(dbWw.indexOf('class="nozlight" cx="80" cy="210"') >= 0);
  // Cowl aura stays the first outline, so the nozzle baseline check still sees the cowl.
  assert.ok(dbOrb.indexOf('class="aura"') < dbOrb.indexOf('class="extruder"'));
});

t('stealthburner keeps Clockwork and does not add an extruder', () => {
  assert.ok(sb.indexOf('data-extruder="clockwork2"') >= 0);
  assert.ok(sb.indexOf('class="extruder"') < 0);
  assert.ok(sb.indexOf(orbFace) < 0);
  assert.ok(sb.indexOf(wwFace) < 0);
  const ys = pathYs(sb);
  assert.ok(Math.min.apply(null, ys) < -40, 'Clockwork top missing');
});

t('extruder art stays hardware-coloured and the nozzle line does not move', () => {
  ['orbiter25', 'wwbmg'].forEach((ex) => {
    const s = A.toolhead('dragonburner', { body: '#00aa00', accent: '#d41216' }, ex);
    assert.ok(s.toLowerCase().indexOf('#00aa00') >= 0, 'cowl lost the body colour');
    const g = group(s);
    assert.ok(g.indexOf('#00aa00') < 0, 'extruder took the body colour');
    assert.ok(s.indexOf('#8d9196') >= 0, 'black fan rim missing');
    assert.ok(s.toLowerCase().indexOf(A.tones('#2a2b2e').md.toLowerCase()) >= 0);
    assert.ok(s.indexOf('class="nozlight" cx="80" cy="210"') >= 0);
  });
  const aura = (s) => (s.match(/class="aura" d="([^"]+)"/) || [])[1];
  const ay = (d) => pathYs('<path d="' + d + '"/>');
  const orbTip = Math.max.apply(null, ay(aura(svg('dragonburner', 'orbiter25', { body: '#00aa00', accent: '#d41216' }))));
  const wwTip = Math.max.apply(null, ay(aura(svg('dragonburner', 'wwbmg', { body: '#00aa00', accent: '#d41216' }))));
  assert.strictEqual(orbTip, wwTip, 'cowl moved when the extruder changed');
  assert.ok(orbTip > 200 && orbTip < 220, orbTip);
  const ant = A.toolhead('anthead', { body: '#00aa00', accent: '#d41216' }, 'wwbmg');
  assert.ok(group(ant).indexOf('#00aa00') < 0);
  assert.ok(ant.indexOf('cy="210"') >= 0);
});

t('tools keep independent heads and extruders', () => {
  const a = A.normalizeHead({ model: 'anthead', extruder: 'orbiter25', body: '#112233', accent: '#abcdef', led: 'headlight_T0' });
  const b = A.normalizeHead({ model: 'dragonburner', extruder: 'wwbmg', body: '#445566', accent: '#fedcba', led: 'headlight_T1' });
  assert.strictEqual(a.extruder, 'orbiter25');
  assert.strictEqual(b.extruder, 'wwbmg');
  assert.strictEqual(a.body, '#112233');
  assert.strictEqual(b.led, 'headlight_T1');
  assert.notStrictEqual(a.model, b.model);
  const sa = A.toolhead(a.model, a);
  const sb2 = A.toolhead(b.model, b);
  assert.ok(sa.indexOf('data-extruder="orbiter25"') >= 0);
  assert.ok(sb2.indexOf('data-extruder="wwbmg"') >= 0);
  assert.ok(sa.indexOf(wwFace) < 0);
  assert.ok(sb2.indexOf(wwFace) >= 0);
});

t('legacy prefs migrate without dropping colours', () => {
  const old = A.normalizeHead({ model: 'anthead', body: '#a3a7ab', accent: '#D41216', led: 'headlight_T2', logoIdx: '1', nozIdx: '2-3' });
  assert.strictEqual(old.model, 'anthead');
  assert.strictEqual(old.extruder, 'orbiter25');
  assert.strictEqual(old.extruderKeep, 'orbiter25');
  assert.strictEqual(old.body, '#a3a7ab');
  assert.strictEqual(old.accent, '#D41216');
  assert.strictEqual(old.led, 'headlight_T2');
  assert.strictEqual(old.logoIdx, '1');
  const legacy = A.normalizeHead({ model: 'anthead-ww', body: '#010203', accent: '#445566', led: '' });
  assert.strictEqual(legacy.model, 'anthead');
  assert.strictEqual(legacy.extruder, 'wwbmg');
  assert.strictEqual(legacy.body, '#010203');
  const bad = A.normalizeHead({ model: 'dragonburner', extruder: 'sherpa', accent: '#778899' });
  assert.strictEqual(bad.extruder, 'orbiter25');
  assert.strictEqual(bad.accent, '#778899');
  const back = A.normalizeHead({ model: 'stealthburner', extruder: 'wwbmg', extruderKeep: 'wwbmg', body: '#111111' });
  assert.strictEqual(back.model, 'stealthburner');
  assert.strictEqual(back.extruder, 'wwbmg', 'remembered extruder should survive the built-in head');
  assert.strictEqual(back.body, '#111111');
  const remembered = A.normalizeHead({ model: 'anthead', extruderKeep: 'wwbmg' });
  assert.strictEqual(remembered.extruder, 'wwbmg');
});

function tubeEnd(s) {
  const g = s.match(/class="extruder"[^>]*transform="translate\([^,]+,([^)]+)\)"[\s\S]*?<line class="tube" x1="80" y1="[^"]+" x2="80" y2="([^"]+)"/);
  if (g) return parseFloat(g[2]) + parseFloat(g[1]);
  const m = s.match(/<line class="tube" x1="80" y1="[^"]+" x2="80" y2="([^"]+)"/);
  return parseFloat(m[1]);
}
t('wristwatch filament tube meets the centerline inlet on both heads', () => {
  const antOrb = tubeEnd(baked);
  const antWwEnd = tubeEnd(antWw);
  const dbOrbEnd = tubeEnd(dbOrb);
  const dbWwEnd = tubeEnd(dbWw);
  const sbEnd = tubeEnd(sb);
  // Orbiter collet and Clockwork inlet stay where they were.
  assert.ok(Math.abs(antOrb - (-43)) < 0.2, antOrb);
  assert.ok(Math.abs(sbEnd - (-44.04)) < 0.2, sbEnd);
  // Dragon's mount shift moves the same Orbiter tube with the extruder.
  assert.ok(Math.abs(dbOrbEnd - (antOrb - 5.5)) < 0.2, dbOrbEnd);
  // WW tube reaches the motor plate on the filament axis (top near y 0) and stops inside it.
  assert.ok(antWwEnd > 0 && antWwEnd < 8, antWwEnd);
  assert.ok(Math.abs(dbWwEnd - (antWwEnd - 5.5)) < 0.2, dbWwEnd);
});

t('front socket caps stay black on dragon and stealthburner', () => {
  ['#111111', '#ffffff', '#e10600', '#0057ff'].forEach((hex) => {
    ['dragonburner', 'stealthburner'].forEach((model) => {
      const s = A.toolhead(model, { body: hex, accent: '#d41216' });
      const screws = s.match(/<g class="screw">[\s\S]*?<\/g>/g) || [];
      assert.strictEqual(screws.length, 2, model + ' ' + hex);
      screws.forEach((g) => {
        assert.ok(g.indexOf('fill="#16181b"') >= 0, 'cap');
        assert.ok(g.indexOf('fill="#070808"') >= 0, 'socket');
        assert.ok(g.toLowerCase().indexOf(hex.toLowerCase()) < 0, 'cap took the body colour');
      });
    });
  });
  assert.strictEqual((svg('anthead').match(/class="screw"/g) || []).length, 0);
});

t('the filament path is one spool stroke, not a second dashed tube', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  assert.ok(css.indexOf('.headsvg .tube { stroke: none; fill: none; }') >= 0);
  assert.ok(css.indexOf('repeating-linear-gradient(to bottom, var(--fil, #8a8f96) 0 5px, transparent 5px 10px)') >= 0);
  assert.ok(css.indexOf('bottom: calc(-1 * var(--to-inlet, 80px) - var(--head-shift))') >= 0);
  assert.ok(css.indexOf('stroke-dasharray') < 0, 'a dashed SVG tube would be a second stroke');
  assert.ok(antWw.indexOf('<line class="tube" x1="80"') >= 0);
});

t('stealthburner disables the extruder selector', () => {
  const off = A.extruderChoices('stealthburner');
  assert.strictEqual(off.disabled, true);
  assert.strictEqual(off.label, 'Clockwork 2');
  assert.strictEqual(off.id, 'clockwork2');
  ['anthead', 'dragonburner'].forEach((model) => {
    const on = A.extruderChoices(model);
    assert.strictEqual(on.disabled, false);
    assert.strictEqual([].map.call(on.options, (o) => o.id).join(','), 'orbiter25,wwbmg');
  });
});

console.log(n + ' passed');
