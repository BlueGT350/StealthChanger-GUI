"""Quick offline lint for ktc-filament cfg files (no Klipper checkout needed).
Mimics Klipper: configparser with inline # ; comments, Jinja env with
'{%' '%}' / '{' '}' delimiters (no extensions, same as Klipper's
gcode_macro.py), GetStatusWrapper 'in', action_raise_error, and the
extended-gcode parser (shlex). It does NOT run Klipper and it does not
see what Mainsail shows. The authoritative test is tests/klipper_harness.py,
which runs the macros through real Klipper code and Mainsail's console filter."""
import configparser, re, shlex, sys, ast, copy, io
import jinja2

FILES = ['filament/filament_macros.cfg', 'orbiter/orbiter_addon.cfg', 'orbiter/tool_snippet_T0.cfg', 'usage/usage_addon.cfg']

def load(path):
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    cp.read_string(open(path).read(), path)
    return cp

env = jinja2.Environment('{%', '%}', '{', '}')   # exactly Klipper's PrinterGCodeMacro env
class RaiseErr(Exception): pass
def action_raise_error(msg): raise RaiseErr(msg)
def action_respond_info(msg): return ''

class Wrapper(dict):
    def __getitem__(self, k):
        if not dict.__contains__(self, k): raise KeyError(k)
        return dict.__getitem__(self, k)

ext_r = re.compile(r'^\s*(?:N[0-9]+\s*)?(?P<cmd>[a-zA-Z_][a-zA-Z0-9_]+)(?:\s+|$)(?P<args>[^#*;]*?)\s*(?:[#*;].*)?$')
problems = []

def macro_vars(cp, sec):
    d = {}
    for k, v in cp.items(sec):
        if k.startswith('variable_'):
            d[k[9:]] = ast.literal_eval(v)
    return d

def check_lines(out, label):
    cmds = []
    for line in out.split('\n'):
        line = line.strip()
        if not line: continue
        if any(c in line for c in '#;*'):
            problems.append(f'{label}: comment char in output line -> {line}')
        m = ext_r.match(line)
        if not m:
            problems.append(f'{label}: unparsable -> {line}'); continue
        cmd = m.group('cmd').upper()
        if cmd in ('G1', 'M117', 'M83', 'M400'):
            cmds.append(line); continue
        try:
            parts = [a.split('=', 1) for a in shlex.split(m.group('args'))]
            for p in parts:
                if len(p) != 2: problems.append(f'{label}: bad param {p} in {line}')
        except ValueError as e:
            problems.append(f'{label}: shlex {e} -> {line}')
        if cmd == 'SAVE_VARIABLE':
            val = dict(parts)['VALUE']
            try: ast.literal_eval(val)
            except Exception: problems.append(f'{label}: SAVE_VARIABLE value not literal -> {line}')
        if cmd == 'SET_GCODE_VARIABLE':
            ast.literal_eval(dict(parts)['VALUE'])
        cmds.append(line)
    return cmds

def make_printer(cp_all, n_tools=6, numbers=None, state='standby', variables=None,
                 sensors=None, active=0, sensor_kind='filament_switch_sensor', targets=None):
    numbers = numbers if numbers is not None else list(range(n_tools))
    p = Wrapper()
    names = [f'tool T{n}' for n in numbers]
    p['toolchanger'] = {'tool_numbers': numbers, 'tool_names': names, 'tool_number': active, 'status': 'ready'}
    settings = {}
    for i, n in enumerate(numbers):
        ext = 'extruder' if i == 0 else f'extruder{i}'
        p[f'tool T{n}'] = {'tool_number': n, 'extruder': ext, 'active': n == active, 'fan': f'T{n}_partfan'}
        p[ext] = {'temperature': 25.0, 'target': (targets or {}).get(n, 0.0)}
        settings[ext] = {'max_temp': 290.0, 'min_extrude_temp': 170.0, 'max_extrude_only_distance': 1400.0}
        if sensors is not None and n in sensors:
            p[f'{sensor_kind} filament_sensor_T{n}'] = {'filament_detected': sensors[n], 'enabled': True}
    p['configfile'] = {'settings': settings}
    p['save_variables'] = {'variables': variables or {}}
    p['print_stats'] = {'state': state}
    act_ext = p[f'tool T{active}']['extruder'] if active in numbers else 'extruder'
    p['toolhead'] = {'extruder': act_ext, 'estimated_print_time': 5000.0}
    for cp in cp_all:
        for sec in cp.sections():
            if sec.startswith('gcode_macro '):
                p[sec] = macro_vars(cp, sec)
    return p

def render(cp_all, macro, params, printer):
    for cp in cp_all:
        if cp.has_section('gcode_macro ' + macro):
            src = cp.get('gcode_macro ' + macro, 'gcode')
            break
    tmpl = env.from_string(src)
    ctx = dict(printer['gcode_macro ' + macro]) if ('gcode_macro ' + macro) in printer else {}
    ctx.update(printer=printer, params=params, rawparams='',
               action_raise_error=action_raise_error, action_respond_info=action_respond_info,
               action_emergency_stop=lambda m='': '', action_call_remote_method=lambda *a, **k: '')
    return tmpl.render(ctx)

def main():
    cps = [load(f) for f in FILES]
    # ---- 1. structure ----
    seen = {}
    for f, cp in zip(FILES, cps):
        for sec in cp.sections():
            if sec in seen and sec not in ('save_variables', 'respond'):
                problems.append(f'duplicate section [{sec}] in {f} and {seen[sec]}')
            seen[sec] = f
            if sec.startswith('gcode_macro '):
                if not cp.has_option(sec, 'gcode'): problems.append(f'{sec}: no gcode option')
                try: macro_vars(cp, sec)
                except Exception as e: problems.append(f'{sec}: variable not literal: {e}')
                try: env.from_string(cp.get(sec, 'gcode'))
                except jinja2.TemplateSyntaxError as e: problems.append(f'{sec}: Jinja syntax line {e.lineno}: {e}')
            if sec.startswith('gcode_button'):
                env.from_string(cp.get(sec, 'press_gcode'))
    # raw-text check: Klipper strips '#' and ';' as comments even mid-expression,
    # so no indented gcode line may contain them (e.g. replace('#','') breaks Klipper)
    for f in FILES:
        for i, line in enumerate(open(f).read().splitlines(), 1):
            st = line.strip()
            if line.startswith((' ', '\t')) and st and not st.startswith(('#', ';')) and ('#' in st or ';' in st):
                problems.append(f"{f}:{i} contains '#' or ';' inside a gcode line (Klipper treats it as a comment)")
    # raw-text check: Mainsail hides console lines that start with T<n>: / B: / C:
    # (after stripping echo: // !!) as temperature reports, silently
    hide = re.compile(r'(RESPOND\b[^\n]*MSG=["\']|action_respond_info\(\s*["\']|action_raise_error\(\s*["\'])\s*(T(\{[^}]*\}|\d*)|B|C):')
    for f in FILES:
        for i, line in enumerate(open(f).read().splitlines(), 1):
            if hide.search(line):
                problems.append(f"{f}:{i} message starts with T<n>: / B: / C:, Mainsail hides it by default")
    # raw-text check: every non-comment line inside a gcode block is indented
    for f in FILES:
        in_g = False
        for i, line in enumerate(open(f), 1):
            if re.match(r'^\[.*\]\s*$', line): in_g = False; continue
            if re.match(r'^(gcode|press_gcode):\s*$', line): in_g = True; continue
            if in_g and line.strip() and not line.startswith((' ', '\t', '#')):
                in_g = False
                if not re.match(r'^[a-z_]+\s*[:=]', line): problems.append(f'{f}:{i} unindented line in gcode: {line.rstrip()}')
            if '\t' in line: problems.append(f'{f}:{i} tab character')
    print('sections:', len(seen))

    # ---- 2. scenarios ----
    def run(label, macro, params, expect_error=None, **pk):
        pr = make_printer(cps, **pk)
        try:
            out = render(cps, macro, params, pr)
        except RaiseErr as e:
            if expect_error and expect_error.lower() in str(e).lower():
                print(f'  ok  {label}: refused -> {e}')
            else:
                problems.append(f'{label}: unexpected error {e}')
            return []
        if expect_error:
            problems.append(f'{label}: expected error "{expect_error}" but rendered')
        cmds = check_lines(out, label)
        print(f'  ok  {label}: {len(cmds)} lines')
        return cmds

    allS = {i: True for i in range(6)}
    c = run('load T3 docked, LOW default', 'FILAMENT_LOAD', {'TOOL': '3'}, sensors=allS)
    assert 'ACTIVATE_EXTRUDER EXTRUDER=extruder3' in c and 'ACTIVATE_EXTRUDER EXTRUDER=extruder' in c
    assert 'SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=240.0' in c, c
    assert 'G1 E60.0 F300.0' in c and 'G1 E25.0 F150' in c, c
    assert 'SAVE_VARIABLE VARIABLE=t3_loaded VALUE=True' in c
    assert 'SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=0.0' in c
    print('\n'.join('      ' + x for x in c))
    c = run('unload T1 HIGH via old t1_temp_type key', 'FILAMENT_UNLOAD', {'TOOL': '1'}, sensors=allS,
            variables={'t1_loaded': True, 't1_temp_type': 'HIGH'})
    assert 'SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=260.0' in c
    assert c.count('G1 E-75.0 F7200.0') == 2 and 'G1 E5.0 F500' in c, c
    print('\n'.join('      ' + x for x in c))
    c = run('unload keeps hotter print target 275', 'FILAMENT_UNLOAD', {'TOOL': '0'}, sensors=allS, targets={0: 275.0},
            state='paused')
    assert 'SET_HEATER_TEMPERATURE HEATER=extruder TARGET=275.0' in c and sum('TARGET=275.0' in x for x in c if x.startswith('SET_HEATER')) == 2
    run('load refused while printing', 'FILAMENT_LOAD', {'TOOL': '2'}, 'refused while printing', state='printing', sensors=allS)
    run('load refused, already loaded', 'FILAMENT_LOAD', {'TOOL': '2'}, 'already marked loaded', sensors=allS, variables={'t2_loaded': True})
    run('load FORCE when loaded', 'FILAMENT_LOAD', {'TOOL': '2', 'FORCE': '1'}, sensors=allS, variables={'t2_loaded': True})
    run('load refused, sensor empty', 'FILAMENT_LOAD', {'TOOL': '4'}, 'sensor reads empty', sensors={4: False})
    run('load unknown tool', 'FILAMENT_LOAD', {'TOOL': '9'}, 'no tool T9')
    run('load missing TOOL', 'FILAMENT_LOAD', {}, 'TOOL= is required')
    c = run('CUSTOM missing -> HIGH warn', 'FILAMENT_LOAD', {'TOOL': '0'}, sensors=allS, variables={'t0_temp_profile': 'CUSTOM'})
    assert any('WARNING' in x for x in c) and 'SET_HEATER_TEMPERATURE HEATER=extruder TARGET=260.0' in c
    c = run('CUSTOM 225 + per-tool motion', 'FILAMENT_LOAD', {'TOOL': '5'}, sensors=allS,
            variables={'t5_temp_profile': 'CUSTOM', 't5_custom_load_temp': 225, 't5_load_length': 160, 't5_load_speed': 600})
    assert 'SET_HEATER_TEMPERATURE HEATER=extruder5 TARGET=225.0' in c and c.count('G1 E53.333 F600.0') == 3, c
    c = run('reset motion (None) falls back', 'FILAMENT_LOAD', {'TOOL': '5'}, sensors=allS,
            variables={'t5_load_length': None, 't5_load_speed': None, 't5_temp_profile': None})
    assert 'G1 E60.0 F300.0' in c
    # no sensors, non-contiguous numbering, 2 tools, motion sensor kind
    c = run('2 tools [0,7], no sensors, load T7', 'FILAMENT_LOAD', {'TOOL': '7'}, numbers=[0, 7])
    assert 'ACTIVATE_EXTRUDER EXTRUDER=extruder1' in c
    run('motion-sensor kind empty refuses', 'FILAMENT_LOAD', {'TOOL': '1'}, 'sensor reads empty', sensors={1: False}, sensor_kind='filament_motion_sensor')
    # max_extrude_only_distance = 50 (Klipper default) splits moves
    pr = make_printer(cps, sensors=allS)
    for e in pr['configfile']['settings'].values(): e['max_extrude_only_distance'] = 50.0
    out = render(cps, 'FILAMENT_UNLOAD', {'TOOL': '0'}, pr); c = check_lines(out, 'unload maxdist50')
    assert c.count('G1 E-50.0 F7200.0') == 3, c; print('  ok  unload with max_extrude_only_distance 50 -> 3 x 50mm')
    # temp
    c = run('SET_FILAMENT_TEMP HIGH', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'high'})
    assert '''SAVE_VARIABLE VARIABLE=t2_temp_profile VALUE='"HIGH"\'''' in c
    c = run('SET_FILAMENT_TEMP CUSTOM TEMP=225', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'CUSTOM', 'TEMP': '225'})
    assert 'SAVE_VARIABLE VARIABLE=t2_custom_load_temp VALUE=225' in c and 'SAVE_VARIABLE VARIABLE=t2_custom_unload_temp VALUE=225' in c
    c = run('SET_FILAMENT_TEMP CUSTOM load only -> warn', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'CUSTOM', 'LOAD_TEMP': '230'})
    assert any('WARNING' in x for x in c)
    run('SET_FILAMENT_TEMP CUSTOM 300 rejected', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'CUSTOM', 'TEMP': '300'}, 'rejected')
    run('SET_FILAMENT_TEMP CUSTOM 150 rejected', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'CUSTOM', 'UNLOAD_TEMP': '150'}, 'rejected')
    run('SET_FILAMENT_TEMP bad type', 'SET_FILAMENT_TEMP', {'TOOL': '2', 'TYPE': 'MEDIUM'}, 'must be HIGH')
    # motion
    c = run('SET_FILAMENT_MOTION set', 'SET_FILAMENT_MOTION', {'TOOL': '1', 'LOAD_LENGTH': '80', 'UNLOAD_SPEED': '6000'})
    assert 'SAVE_VARIABLE VARIABLE=t1_load_length VALUE=80.0' in c
    c = run('SET_FILAMENT_MOTION reset', 'SET_FILAMENT_MOTION', {'TOOL': '1', 'RESET': '1'}, variables={'t1_load_length': 80.0})
    assert c == ['SAVE_VARIABLE VARIABLE=t1_load_length VALUE=None', c[1]], c
    run('SET_FILAMENT_MOTION out of range', 'SET_FILAMENT_MOTION', {'TOOL': '1', 'LOAD_LENGTH': '0'}, 'out of range')
    run('SET_FILAMENT_MOTION show', 'SET_FILAMENT_MOTION', {'TOOL': '1'})
    run('SET_FILAMENT_LOADED', 'SET_FILAMENT_LOADED', {'TOOL': '4', 'LOADED': '1'})
    c = run('SET_TOOL_FILAMENT', 'SET_TOOL_FILAMENT', {'TOOL': '3', 'MATERIAL': 'petg', 'COLOR': 'ff8800', 'SPOOL': "Sunlu 'grey' 1kg", 'PROFILE': 'low'})
    assert '''SAVE_VARIABLE VARIABLE=t3_color VALUE='"FF8800"\'''' in c and 'SET_FILAMENT_TEMP TOOL=3 TYPE=LOW' in c
    run('SET_TOOL_FILAMENT bad colour', 'SET_TOOL_FILAMENT', {'TOOL': '3', 'COLOR': 'red'}, 'hex')
    c = run('SHOW_TOOLS mixed', 'SHOW_TOOLS', {}, sensors={0: True, 1: False, 2: True}, variables={
        't1_loaded': True, 't2_temp_profile': 'CUSTOM', 't2_custom_load_temp': 225, 't3_material': 'PLA', 't3_color': 'FF0000', 't4_load_length': 90})
    print('\n'.join('      ' + x for x in c))
    # orbiter
    c = run('button: printing active, tangle off -> ignore', '_ORBITER_BUTTON', {'TOOL': '0'}, state='printing', sensors=allS)
    assert 'PAUSE' not in c
    c = run('button: printing docked -> ignore', '_ORBITER_BUTTON', {'TOOL': '3'}, state='printing', sensors=allS)
    assert 'PAUSE' not in c and not any(x.startswith('FILAMENT_') for x in c)
    c = run('button: paused -> ignore', '_ORBITER_BUTTON', {'TOOL': '0'}, state='paused', sensors=allS)
    assert not any(x.startswith(('FILAMENT_', 'PAUSE')) for x in c)
    c = run('button: loaded -> unload', '_ORBITER_BUTTON', {'TOOL': '2'}, sensors=allS, variables={'t2_loaded': True})
    assert 'FILAMENT_UNLOAD TOOL=2' in c and c[-1] == '_ORBITER_PRESS_DONE'
    c = run('button: empty + filament -> load', '_ORBITER_BUTTON', {'TOOL': '2'}, sensors=allS)
    assert 'FILAMENT_LOAD TOOL=2' in c
    c = run('button: empty + no filament -> nothing', '_ORBITER_BUTTON', {'TOOL': '2'}, sensors={2: False})
    assert not any(x.startswith('FILAMENT_') for x in c)
    c = run('button: no sensor -> load', '_ORBITER_BUTTON', {'TOOL': '2'})
    assert 'FILAMENT_LOAD TOOL=2' in c
    pr = make_printer(cps, sensors=allS); pr['gcode_macro _ORBITER_SETTINGS']['last_done'] = 4999.0
    c = check_lines(render(cps, '_ORBITER_BUTTON', {'TOOL': '2'}, pr), 'lockout')
    assert not any(x.startswith('FILAMENT_') for x in c); print('  ok  button: queued double press inside lockout ignored')
    c = run('press done stamp', '_ORBITER_PRESS_DONE', {})
    assert c == ['SET_GCODE_VARIABLE MACRO=_ORBITER_SETTINGS VARIABLE=last_done VALUE=5000.0']
    pr = make_printer(cps); del pr['gcode_macro _FILAMENT_SETTINGS']
    try:
        render(cps, '_ORBITER_BUTTON', {'TOOL': '0'}, pr); problems.append('core-missing not detected')
    except RaiseErr as e: print('  ok  button without core module refused ->', e)
    for m in ('_FILAMENT_SETTINGS', '_ORBITER_SETTINGS'):
        check_lines(render(cps, m, {}, make_printer(cps)), m)
    print()
    if problems:
        print('PROBLEMS:'); [print(' -', p) for p in problems]; sys.exit(1)
    print('ALL CHECKS PASSED')

main()
