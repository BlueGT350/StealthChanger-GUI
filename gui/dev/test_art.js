// Geometry checks for the CAD-traced toolheads. Run: node gui/dev/test_art.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const artPath = path.join(__dirname, '..', 'js', 'art.js');
const src = fs.readFileSync(artPath, 'utf8');
const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: artPath });
const A = sandbox.window.KTC.art;
let n = 0;
function t(name, fn) {
  fn();
  n++;
  console.log('  ok    ' + name);
}

function svg(model, colours) {
  return A.toolhead(model, colours || { body: '#a3a7ab', accent: '#D41216' });
}
function nums(d) {
  const out = [];
  const re = /-?\d+(?:\.\d+)?/g;
  let m;
  while ((m = re.exec(d))) out.push(parseFloat(m[0]));
  return out;
}

t('models stay Anthead, Stealthburner, Dragonburner', () => {
  assert.strictEqual([].map.call(A.MODELS, function (m) { return m.id; }).join(','),
    'anthead,stealthburner,dragonburner');
});

t('anthead is still the CAD trace', () => {
  const s = svg('anthead');
  assert.ok(s.indexOf('data-model="anthead"') >= 0);
  assert.ok(s.indexOf('class="grillewin"') >= 0);
  assert.ok(s.indexOf('class="rotor hotend"') >= 0);
  assert.ok(s.indexOf('class="logolit"') >= 0);
  assert.ok((s.match(/<path /g) || []).length > 80, 'anthead lost its shaded faces');
  assert.ok(s.indexOf('translate(10,20)') < 0);
});

t('dragonburner is the v8 cowl, on the anthead scale', () => {
  const s = svg('dragonburner');
  assert.ok(s.indexOf('data-model="dragonburner"') >= 0);
  assert.ok(s.indexOf('viewBox="0 -48 160 270"') >= 0);
  assert.ok(s.indexOf('translate(10,20)') < 0, 'still on the old offset drawing');
  assert.ok(s.indexOf('M42,6H98') < 0, 'rough polygon still present');
  assert.ok(s.indexOf('class="aura"') >= 0);
  assert.ok(s.indexOf('class="tube"') >= 0);
  assert.ok(s.indexOf('class="rotor hotend"') >= 0);
  assert.ok(s.indexOf('class="logolit"') >= 0);
  assert.ok(s.indexOf('class="nozlight"') >= 0);
  assert.ok(s.indexOf('class="air"') >= 0);
  assert.ok(s.indexOf('class="grillewin"') >= 0);
  const logo = (s.match(/class="logolit" d="([^"]+)"/) || [])[1];
  assert.ok(logo && logo.split('M').length - 1 === 3, 'Voron diffuser is not three bars');
  const origin = (s.match(/class="rotor hotend" style="transform-origin:([^"]+)"/) || [])[1];
  const xy = origin.split(' ').map(parseFloat);
  assert.ok(Math.abs(xy[0] - 80) < 2 && xy[1] > 130 && xy[1] < 165, origin);
  const aura = (s.match(/class="aura" d="([^"]+)"/) || [])[1];
  const a = nums(aura);
  let minX = 1e9, maxX = -1e9, maxY = -1e9, minY = 1e9;
  for (let i = 0; i < a.length; i += 2) {
    minX = Math.min(minX, a[i]); maxX = Math.max(maxX, a[i]);
    minY = Math.min(minY, a[i + 1]); maxY = Math.max(maxY, a[i + 1]);
  }
  const w = maxX - minX;
  assert.ok(w > 90 && w < 140, 'cowl width is not the 55 mm CAD shell ' + w);
  assert.ok(minX > 10 && maxX < 150, 'cowl not centred');
  assert.ok(maxY > 200 && maxY < 220, 'duct exits not at the nozzle line');
  assert.ok(minY > 40 && minY < 120, 'cowl top is not at the CAD height ' + minY);
  // The main face is the chosen colour, not a darkened stand-in.
  assert.ok(s.toLowerCase().indexOf('#a3a7ab') >= 0);
  assert.ok(s.toLowerCase().indexOf('#d41216') >= 0, 'accent ducts missing');
});

t('stealthburner is the CW2 assembly, on the anthead scale', () => {
  const s = svg('stealthburner');
  assert.ok(s.indexOf('data-model="stealthburner"') >= 0);
  assert.ok(s.indexOf('viewBox="0 -48 160 270"') >= 0);
  assert.ok(s.indexOf('translate(10,20)') < 0, 'still on the old offset drawing');
  assert.ok(s.indexOf('class="aura"') >= 0);
  assert.ok(s.indexOf('class="tube"') >= 0);
  assert.ok(s.indexOf('class="rotor hotend"') >= 0);
  assert.ok(s.indexOf('class="logolit"') >= 0);
  assert.ok(s.indexOf('class="glow"') >= 0);
  assert.ok(s.indexOf('class="nozlight"') >= 0);
  assert.ok(s.indexOf('class="air"') >= 0);
  assert.ok(s.indexOf('class="grillewin"') >= 0);
  assert.ok((s.match(/<path /g) || []).length > 80, 'not enough shaded faces');
  // Voron mark: three contours in the diffuser
  const logo = (s.match(/class="logolit" d="([^"]+)"/) || [])[1];
  assert.ok(logo && logo.split('M').length - 1 === 3, logo);
  // fan rotor sits on the cowl's round opening, on the nozzle axis
  const origin = (s.match(/class="rotor hotend" style="transform-origin:([^"]+)"/) || [])[1];
  const xy = origin.split(' ').map(parseFloat);
  assert.ok(Math.abs(xy[0] - 80) < 2 && Math.abs(xy[1] - 126) < 4, origin);
  // true-scale outline: wider than the old invented shape, nozzle still at y 210
  const aura = (s.match(/class="aura" d="([^"]+)"/) || [])[1];
  const a = nums(aura);
  let minX = 1e9, maxX = -1e9, maxY = -1e9, minY = 1e9;
  for (let i = 0; i < a.length; i += 2) {
    minX = Math.min(minX, a[i]); maxX = Math.max(maxX, a[i]);
    minY = Math.min(minY, a[i + 1]); maxY = Math.max(maxY, a[i + 1]);
  }
  assert.ok(maxX - minX > 120, 'outline narrower than the CAD cowl ' + (maxX - minX));
  assert.ok(minX < 20 && maxX > 140, 'outline not centred in the viewBox');
  assert.ok(maxY > 205 && maxY < 220, 'nozzle tip not at y 210');
  assert.ok(minY < -40, 'Clockwork top missing');
});

t('body and accent colours both paint the stealthburner', () => {
  const s = svg('stealthburner', { body: '#336699', accent: '#cc3300' });
  assert.ok(s.indexOf('#336699') >= 0 || s.toLowerCase().indexOf('336699') >= 0);
  assert.ok(s.toLowerCase().indexOf('cc3300') >= 0, 'accent printhead missing');
  const ant = svg('anthead', { body: '#336699', accent: '#cc3300' });
  assert.ok(ant.toLowerCase().indexOf('336699') >= 0 && ant.toLowerCase().indexOf('cc3300') >= 0);
});

function chans(hex) {
  const h = String(hex).replace('#', '');
  return [0, 2, 4].map((i) => parseInt(h.substr(i, 2), 16));
}
t('stealthburner shades keep the chosen hue', () => {
  // Dark, white, saturated red, blue, green, mid grey. md is the colour
  // itself; highlights and shadows stay on the same channel order.
  ['#111111', '#f4f4f4', '#e10600', '#0057ff', '#00a84a', '#8a8d91'].forEach((hex) => {
    const tones = A.sbTones(hex);
    assert.strictEqual(tones.md.toLowerCase(), hex.toLowerCase(), hex);
    const base = chans(hex);
    const order = (xs) => xs.map((_, i) => i).sort((i, j) => xs[j] - xs[i] || i - j).join('');
    ['hi', 'lt', 'dk', 'dd', 'xd'].forEach((k) => {
      const c = chans(tones[k]);
      if (Math.max(...base) - Math.min(...base) > 18)
        assert.strictEqual(order(c), order(base), hex + ' ' + k + ' ' + tones[k]);
    });
    const drawn = svg('stealthburner', { body: hex, accent: '#d41216' });
    assert.ok(drawn.toLowerCase().indexOf(hex.toLowerCase()) >= 0, 'face lost ' + hex);
    const db = svg('dragonburner', { body: hex, accent: '#d41216' });
    assert.ok(db.toLowerCase().indexOf(hex.toLowerCase()) >= 0, 'dragon face lost ' + hex);
    assert.ok(db.toLowerCase().indexOf(A.sbTones(hex).xd.toLowerCase()) >= 0, 'dragon rim lost ' + hex);
    // Side blowers stay this black, whatever body and accent are.
    assert.ok(db.toLowerCase().indexOf(A.tones('#2a2b2e').md.toLowerCase()) >= 0, 'fan black lost ' + hex);
    assert.ok(db.indexOf('#8d9196') >= 0, 'fan rim lost ' + hex);
  });
  assert.ok(Math.max(...chans(A.sbTones('#111111').hi)) < 90, A.sbTones('#111111').hi);
  assert.ok(Math.min(...chans(A.sbTones('#f7f7f7').xd)) > 90, A.sbTones('#f7f7f7').xd);
  // Anthead keeps the shared HSL tones. Stealthburner does not use that highlight.
  const sbHi = A.sbTones('#e10600').hi.toLowerCase();
  const antHi = A.tones('#e10600', '#a3a7ab').hi.toLowerCase();
  assert.notStrictEqual(sbHi, antHi);
  assert.ok(svg('stealthburner', { body: '#e10600', accent: '#0057ff' }).toLowerCase().indexOf(sbHi) >= 0);
  assert.ok(svg('anthead', { body: '#e10600', accent: '#0057ff' }).toLowerCase().indexOf(antHi) >= 0);
  assert.ok(svg('anthead', { body: '#e10600', accent: '#0057ff' }).toLowerCase().indexOf(sbHi) < 0);
  assert.ok(svg('stealthburner', { body: '#e10600', accent: '#0057ff' }).toLowerCase().indexOf(antHi) < 0);
});

t('unknown model still falls back to anthead', () => {
  assert.ok(svg('nope').indexOf('data-model="anthead"') >= 0);
});

console.log(n + ' passed');
