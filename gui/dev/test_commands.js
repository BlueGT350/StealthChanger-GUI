// Unit tests for gui/js/commands.js (the G-code builders). Run: node gui/dev/test_commands.js
'use strict';
const assert = require('assert');
const C = require('../js/commands.js');
let n = 0;
function t(name, fn) { fn(); n++; console.log('  ok    ' + name); }
const B = { material: 'PLA', color: 'D41216', spool: 'red', profile: 'LOW', load_temp: null, unload_temp: null,
  load_length: null, load_speed: null, unload_length: null, unload_speed: null, partial_length: null };
const A = (o) => Object.assign({}, B, o);

t('clean strips comment and quote chars', () => assert.strictEqual(C.clean('a#b;c"d\'e\\f*g\nh'), 'a b c d e f g h'));
t('hex', () => { assert.strictEqual(C.hex('#ff0000'), 'FF0000'); assert.strictEqual(C.hex('fff'), null); });
t('nothing changed -> no lines', () => assert.deepStrictEqual(C.editorScript(1, B, A({})), []));
t('material uppercased and quoted', () => assert.deepStrictEqual(C.editorScript(1, B, A({ material: 'petg' })),
  ['SET_TOOL_FILAMENT TOOL=1 MATERIAL="PETG"']));
t('spool name cleaned', () => assert.deepStrictEqual(C.editorScript(2, B, A({ spool: 'x;#y' })), ['SET_TOOL_FILAMENT TOOL=2 SPOOL="x y"']));
t('profile HIGH via SET_TOOL_FILAMENT', () => assert.deepStrictEqual(C.editorScript(0, B, A({ profile: 'HIGH' })),
  ['SET_TOOL_FILAMENT TOOL=0 PROFILE=HIGH']));
t('custom temps via SET_FILAMENT_TEMP', () => assert.deepStrictEqual(C.editorScript(3, B, A({ profile: 'CUSTOM', load_temp: 250, unload_temp: 245.4 })),
  ['SET_FILAMENT_TEMP TOOL=3 TYPE=CUSTOM LOAD_TEMP=250 UNLOAD_TEMP=245']));
t('motion override', () => assert.deepStrictEqual(C.editorScript(1, B, A({ load_length: 500, unload_speed: 1800 })),
  ['SET_FILAMENT_MOTION TOOL=1 LOAD_LENGTH=500 UNLOAD_SPEED=1800']));
t('clearing a field resets then re-sends the rest', () => assert.deepStrictEqual(
  C.editorScript(1, A({ load_length: 500, load_speed: 200 }), A({ load_speed: 200 })),
  ['SET_FILAMENT_MOTION TOOL=1 RESET=1', 'SET_FILAMENT_MOTION TOOL=1 LOAD_SPEED=200']));
const lim = { min_temp: 170, max_temp: 290, load_length: 60, partial_margin: 10, partial_default: 35 };
t('validate ok', () => assert.deepStrictEqual(C.validate(A({}), lim), {}));
t('validate temp cap', () => assert.ok(C.validate(A({ profile: 'CUSTOM', load_temp: 300, unload_temp: 160 }), lim).load_temp &&
  C.validate(A({ profile: 'CUSTOM', load_temp: 300, unload_temp: 160 }), lim).unload_temp));
t('validate partial vs load length', () => {
  assert.ok(C.validate(A({ partial_length: 55 }), lim).partial_length);
  assert.ok(!C.validate(A({ partial_length: 50 }), lim).partial_length);
  assert.ok(C.validate(A({ load_length: 40 }), lim).partial_length, 'default 35 > 40-10');
});
t('validate colour + material', () => { const e = C.validate(A({ color: 'zz', material: '##' }), lim); assert.ok(e.color && e.material); });
t('calibrate one', () => {
  const s = C.calibrateOne(2, 0, { 0: 'extruder', 2: 'extruder2' });
  assert.strictEqual(s[0], 'SELECT_TOOL T=0 RESTORE_AXIS=XYZ');
  assert.ok(s.indexOf('TOOL_LOCATE_SENSOR') < s.indexOf('SELECT_TOOL T=2 RESTORE_AXIS=Z'));
  assert.ok(s.includes('TEMPERATURE_WAIT SENSOR=extruder2 MINIMUM=150') && s.includes('_SAVE_TOOL_OFFSET T=2'));
  assert.throws(() => C.calibrateOne(0, 0, { 0: 'extruder' }));
});
t('part fan', () => { assert.strictEqual(C.partFan(3, 50), 'M106 S127 T3'); assert.strictEqual(C.partFan(3, 100), 'M106 S255 T3'); assert.strictEqual(C.partFan(3, 0), 'M107 T3'); });
t('tool temp', () => { assert.strictEqual(C.toolTemp(1, 240, 'extruder1', false), 'SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=240'); });
t('mark loaded uses SET_FILAMENT_LOADED', () => {
  assert.strictEqual(C.markLoaded(3, true), 'SET_FILAMENT_LOADED TOOL=3 LOADED=1');
  assert.strictEqual(C.markLoaded(0, false), 'SET_FILAMENT_LOADED TOOL=0 LOADED=0');
});
t('halo keeps hue and reaches 3:1 on the dark page', () => {
  const rgb = (s) => s.match(/\d+/g).slice(0, 3).map(Number);
  for (const hex of ['D41216', '1565C0', '2E7D32', '4A148C', 'FF8800', '00FFFF', '0B3D0B']) {
    const h = C.halo(hex);
    assert.ok(C.contrast(rgb(h), [18, 18, 18]) >= 3, hex + ' -> ' + h);
  }
  const blue = rgb(C.halo('1565C0'));
  assert.ok(blue[2] > blue[0] && blue[2] > blue[1], 'blue stays blue');
  for (const hex of ['000000', '111111', '808080', 'FFFFFF', '3A3A3C']) assert.strictEqual(C.halo(hex), 'rgba(220,224,228,0.55)', hex);
  assert.strictEqual(C.halo('bad'), 'rgba(220,224,228,0.55)');
});
const MAIN = [
  '[include toolchanger/readonly-configs/toolchanger-include.cfg]', '[include ktc-filament/filament/filament_macros.cfg]',
  '[printer]', 'kinematics: corexy', '[gcode_macro X]', 'gcode:', '  G28 ; [tool T9]', '  M118 a: b',
  C.AUTOSAVE_MARK, '#*# [tool T1]', '#*# gcode_x_offset = 0.1', '#*#', '#*# [tool_probe T1]', '#*# z_offset = -1.150'
].join('\n');
t('cfgParse sections, includes, autosave', () => {
  const p = C.cfgParse(MAIN);
  assert.deepStrictEqual(p.includes, ['toolchanger/readonly-configs/toolchanger-include.cfg', 'ktc-filament/filament/filament_macros.cfg']);
  assert.ok(p.sections.printer.kinematics && p.sections['gcode_macro X'].gcode && !p.sections['tool T9']);
  assert.deepStrictEqual(p.autosave, { 'tool T1': { gcode_x_offset: '0.1' }, 'tool_probe T1': { z_offset: '-1.150' } });
});
t('include paths and globs', () => {
  assert.strictEqual(C.pathJoin('toolchanger/readonly-configs', '../tools/T*.cfg'), 'toolchanger/tools/T*.cfg');
  assert.strictEqual(C.pathJoin('', '../x.cfg'), null);
  assert.ok(C.globRe('toolchanger/tools/T*.cfg').test('toolchanger/tools/T1.cfg'));
  assert.ok(!C.globRe('toolchanger/tools/T*.cfg').test('toolchanger/tools/sub/T1.cfg'));
  assert.ok(C.globRe('a/T[0-3].cfg').test('a/T2.cfg') && !C.globRe('a/T[0-3].cfg').test('a/T5.cfg'));
});
t('cfgConflicts finds included values only', () => {
  const inc = [{ file: 'toolchanger/tools/T1.cfg', sections: { 'tool T1': { gcode_x_offset: true, extruder: true }, 'tool_probe T1': { z_offset: true } } },
               { file: 'toolchanger/tools/T2.cfg', sections: { 'tool T2': { extruder: true } } }];
  const c = C.cfgConflicts(C.cfgParse(MAIN).autosave, { 'tool T2': { gcode_y_offset: '0.2' } }, inc);
  assert.deepStrictEqual(c.map((x) => x.section + '/' + x.option).sort(), ['tool T1/gcode_x_offset', 'tool_probe T1/z_offset']);
  const c2 = C.cfgConflicts({}, { 'tool T2': { extruder: 'x' } }, inc);
  assert.ok(c2.length === 1 && c2[0].pending && c2[0].file === 'toolchanger/tools/T2.cfg');
  assert.deepStrictEqual(C.cfgConflicts({}, {}, inc), []);
});
console.log(n + ' passed');
