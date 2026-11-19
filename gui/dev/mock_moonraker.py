#!/usr/bin/env python3
"""Mock Moonraker for the ktc-filament GUI, backed by REAL Klipper code.

Serves ../ (the static GUI) and a Moonraker-style JSON-RPC websocket at
/websocket. printer.gcode.script runs through tests/klipper_harness.py,
so FILAMENT_LOAD, SET_TOOL_FILAMENT, ORBITER_TANGLE... are the real
ktc-filament macros rendered by real Klipper gcode_macro/save_variables.
KTC-Easy commands (SELECT_TOOL, T0.., INITIALIZE_TOOLCHANGER, M104,
M106, calibration...) are stubs that only move the simulated state.

  python3 gui/dev/mock_moonraker.py --port 8090 [--modules orbiter] [--tools 6]
  open http://127.0.0.1:8090/

Test hooks (not part of Moonraker):
  POST /mock/set     {"state": "printing", "tc_status": "error", ...}
  POST /mock/restart {"modules": "base", "tools": 3, ...}  (Klipper restart)
  GET  /mock/log     every gcode script received, in order

Rev 2 additions:
  - Spoolman: server.spoolman.* and the proxy with an in-memory Spoolman DB
    (--spoolman connected|disconnected|off). The usage add-on's
    spoolman_set_active_spool remote method lands here too.
  - Config files for the SAVE_CONFIG conflict check: /server/files/config/...,
    server.files.list/roots (--configs conflict|clean). SAVE_CONFIG fails with
    Klipper's real message when an included file sets a saved option.
  - /mock/set also takes homed ("" = not homed), epos (extruder E position),
    pending ({section: {option: value}}) and spoolman (connected|disconnected).
  - machine.update.status with a [update_manager stealthchanger-gui] git_repo
    entry. /mock/set {"update": {...}} merges into it, {"update": null}
    removes it (no update_manager section).
"""
import argparse, asyncio, copy, json, math, mimetypes, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
sys.path.insert(0, os.path.join(ROOT, 'tests'))
import klipper_harness as kh            # noqa: E402
import aiohttp                            # noqa: E402
from aiohttp import web, WSMsgType      # noqa: E402

# Demo spools: one of every state the GUI draws
DEMO_VARS = {
    't0_loaded': True, 't0_material': 'PLA', 't0_color': 'D41216', 't0_spool': 'Polymaker red', 't0_temp_profile': 'LOW',
    't1_loaded': True, 't1_material': 'PETG', 't1_color': '222222', 't1_spool': 'Sunlu black 1kg', 't1_temp_profile': 'LOW',
    't2_loaded': False, 't2_material': 'ABS', 't2_color': 'F2F2F2', 't2_temp_profile': 'HIGH',
    't3_loaded': True, 't3_material': 'ASA', 't3_color': 'FF8800', 't3_spool': 'Elegoo orange', 't3_temp_profile': 'CUSTOM',
    't3_custom_load_temp': 255, 't3_custom_unload_temp': 250,
    't4_loaded': False, 't4_partial': True, 't4_partial_mm': 35.0, 't4_material': 'PETG', 't4_color': '1E88E5',
    't5_loaded': True, 't5_material': 'TPU', 't5_color': '43A047', 't5_load_speed': 150.0, 't5_unload_speed': 1800.0,
}
# T2 sensor empty, T3 sensor disabled, T5 has no sensor (flag only)
DEMO_SENSORS = {0: True, 1: True, 2: False, 3: True, 4: True}
DEMO_SENSORS_DISABLED = [3]      # T3 shows the 'sensor disabled' state
KTCE_MACROS = ['CALIBRATE_MOVE_OVER_PROBE', 'CALIBRATE_ALL_OFFSETS', 'CALIBRATE_NOZZLE_PROBE_OFFSET',
               '_SAVE_TOOL_OFFSET', '_SAVE_Z_TOOL_OFFSET', 'STOP_CRASH_DETECTION', 'START_CRASH_DETECTION',
               'INITIALIZE_TOOLCHANGER', 'VERIFY_TOOL_DETECTED', 'M104', 'M109', 'SET_TEMPERATURE_WITH_DEADBAND',
               'RESUME', 'TOOL_ALIGN_START', 'TOOL_ALIGN_TEST', 'TOOL_ALIGN_DONE', '_CALIBRATION_SWITCH',
               'PAUSE', 'CANCEL_PRINT', 'PRINT_START', 'PRINT_END']
KTCE_PY = {  # Python commands with a desc (shown in HELP / printer.gcode.help)
    'INITIALIZE_TOOLCHANGER_ORIG': 'Initialize the toolchanger', 'SET_TOOL_TEMPERATURE': 'Set temperature for tool',
    'SELECT_TOOL': 'Select active tool', 'SELECT_TOOL_ERROR': 'Raise a tool change error',
    'ENTER_DOCKING_MODE': 'Enter docking mode', 'EXIT_DOCKING_MODE': 'Exit docking mode',
    'TEST_TOOL_DOCKING': 'Unselect and select the current tool', 'SET_ACTIVE_TOOL_PROBE': 'Set the active tool probe',
    'DETECT_ACTIVE_TOOL_PROBE': 'Detect which tool is active', 'START_TOOL_PROBE_CRASH_DETECTION': 'Start crash detection',
    'STOP_TOOL_PROBE_CRASH_DETECTION': 'Stop crash detection', 'TOOL_LOCATE_SENSOR': 'Locate the calibration sensor',
    'TOOL_CALIBRATE_TOOL_OFFSET': 'Calibrate the current tool offset', 'TOOL_CALIBRATE_SAVE_TOOL_OFFSET': 'Save tool offset',
    'TOOL_CALIBRATE_PROBE_OFFSET': 'Calibrate the nozzle to probe offset', 'TOOL_CALIBRATE_QUERY_PROBE': 'Query the calibration probe',
    'G28': 'Home', 'TURN_OFF_HEATERS': 'Turn off all heaters', 'SAVE_CONFIG': 'Save config and restart',
    'SET_FAN_SPEED': 'Set fan speed', 'RESUME_BASE': 'Resume the print', 'M104.1': 'Set extruder temp',
    'M109.1': 'Set extruder temp and wait', 'BED_MESH_CLEAR': 'Clear the mesh', 'G0': 'Move', 'G4': 'Dwell',
    'FIRMWARE_RESTART': 'Restart firmware', 'RESTART': 'Restart host',
}
# synthetic config tree (NOT anyone's real printer.cfg) for the SAVE_CONFIG check
def config_tree(variant, tools):
    tc = '\n'.join([
        '[include ../tools/T*.cfg]',
        '[include ../../toolchanger-config.cfg]',
        '[toolchanger]', 'uses_axis: xyz', 't_command_restore_axis: Z', ''])
    files = {'toolchanger/readonly-configs/toolchanger-include.cfg': tc,
             'toolchanger-config.cfg': '[gcode_macro _CALIBRATION_SWITCH]\nvariable_x: 77\nvariable_y: -2\nvariable_z: 5\ngcode:\n',
             'ktc-filament/filament/filament_macros.cfg': '[gcode_macro _FILAMENT_SETTINGS]\ngcode:\n',
             'ktc-filament/usage/usage_addon.cfg': '[gcode_macro _USAGE_SETTINGS]\ngcode:\n'}
    auto = []
    for t in tools:
        lines = ['[tool T%d]' % t, 'tool_number: %d' % t, 'extruder: %s' % ('extruder' if t == 0 else 'extruder%d' % t),
                 'fan: T%d_partfan' % t, 'params_park_x: %d' % (30 + 60 * t)]
        if variant == 'conflict' and t in (1, 2):
            lines += ['gcode_x_offset: 0', 'gcode_y_offset: 0', 'gcode_z_offset: 0']
        lines += ['', '[tool_probe T%d]' % t, 'pin: ^nhkT%d:PB6' % t, 'tool: %d' % t, 'speed: 5.0', 'samples: 3']
        if variant == 'conflict' and t == 1:
            lines += ['z_offset: -0.5']
        files['toolchanger/tools/T%d.cfg' % t] = '\n'.join(lines) + '\n'
        if t:
            auto += ['#*# [tool T%d]' % t, '#*# gcode_x_offset = %.3f' % (0.12 * t - 0.31),
                     '#*# gcode_y_offset = %.3f' % (-0.08 * t + 0.17), '#*# gcode_z_offset = %.3f' % (0.021 * t - 0.05), '#*#']
        auto += ['#*# [tool_probe T%d]' % t, '#*# z_offset = -1.150', '#*#']
    files['printer.cfg'] = '\n'.join([
        '[include toolchanger/readonly-configs/toolchanger-include.cfg]',
        '[include ktc-filament/filament/filament_macros.cfg]',
        '[include ktc-filament/usage/usage_addon.cfg]',
        '[printer]', 'kinematics: corexy', 'max_velocity: 300', '',
        '#*# <---------------------- SAVE_CONFIG ---------------------->',
        '#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.', '#*#'] + auto) + '\n'
    return files


def save_config_conflicts(files, pending):
    """configfile.py cmd_SAVE_CONFIG: autosave + pending vs options in included files."""
    import configparser, fnmatch, posixpath
    def opts(text, auto=False):
        out, sec = {}, None
        for raw in text.split('\n'):
            if auto:
                if not raw.startswith('#*#'):
                    continue
                raw = raw[3:].lstrip(' ')
            line = raw.split('#')[0].split(';')[0].rstrip()
            if not line or line[0].isspace():
                continue
            if line.startswith('['):
                sec = line[1:line.index(']')].strip(); out.setdefault(sec, set()); continue
            if sec:
                out[sec].add(line.split(':')[0].split('=')[0].strip().lower())
        return out
    main = files['printer.cfg']
    mark = '#*# <---------------------- SAVE_CONFIG ---------------------->'
    regular, _, auto = main.partition(mark)
    want = opts(auto, True)
    for sec, o in (pending or {}).items():
        want.setdefault(sec, set()).update(k.lower() for k in o)
    seen, inc_opts = set(), {}
    def walk(path, text):
        for sec, o in opts(text).items():
            if sec.startswith('include '):
                pat = posixpath.normpath(posixpath.join(posixpath.dirname(path), sec[8:].strip()))
                for f in sorted(files):
                    if fnmatch.fnmatchcase(f, pat) and f not in seen:
                        seen.add(f); walk(f, files[f])
            elif path != 'printer.cfg':
                inc_opts.setdefault(sec, set()).update(o)
    walk('printer.cfg', regular)
    for sec, o in want.items():
        for k in sorted(o):
            if k in inc_opts.get(sec, ()):
                return "SAVE_CONFIG section '%s' option '%s' conflicts with included value" % (sec, k)
    return None


# demo Spoolman database (shape of Spoolman's /v1 API)
def spoolman_db():
    def fil(i, name, mat, hexc, vendor, dens):
        return {'id': i, 'name': name, 'material': mat, 'color_hex': hexc, 'density': dens, 'diameter': 1.75,
                'weight': 1000.0, 'spool_weight': 200.0, 'vendor': {'id': i, 'name': vendor}, 'registered': '2026-10-01T12:00:00Z'}
    f = [fil(1, 'PolyLite PLA Red', 'PLA', 'D41216', 'Polymaker', 1.24), fil(2, 'Black PETG', 'PETG', '222222', 'Sunlu', 1.27),
         fil(3, 'ASA Orange', 'ASA', 'FF8800', 'Elegoo', 1.07), fil(4, 'PETG Formula Blue', 'PETG', '1E88E5', 'Overture', 1.27)]
    def spool(i, fl, rem, loc=''):
        return {'id': i, 'filament': f[fl - 1], 'initial_weight': 1000.0, 'remaining_weight': rem, 'used_weight': 1000.0 - rem,
                'remaining_length': rem / (f[fl - 1]['density'] * 3.1416 * 0.0875 ** 2) * 10, 'location': loc,
                'comment': '', 'archived': False, 'registered': '2026-10-01T12:00:00Z'}
    return {'filament': f, 'vendor': [x['vendor'] for x in f], 'spool': [spool(3, 1, 812.4, 'Shelf'), spool(7, 2, 455.0), spool(8, 3, 990.0), spool(9, 4, 1000.0, 'Dry box')]}


KTCE_NODESC = ['SET_TOOL_PARAMETER', 'RESET_TOOL_PARAMETER', 'SAVE_TOOL_PARAMETER', 'VERIFY_TOOL_DETECTED_ORIG',
               'M106', 'M107']


class Sim:
    def __init__(self, args):
        self.args = args
        self.log = []
        self.store = []            # gcode_store
        self.db = {}
        self.clients = set()
        self.loop = None
        self.klippy_state = 'ready'
        self.update = self.default_update()
        self.build(vars(args).copy())

    # ------------------------------------------------------------- build
    def build(self, cfg):
        self.cfg = cfg
        mods = cfg.get('modules', 'orbiter')
        n = int(cfg.get('tools', 6))
        tools = list(range(n))
        sensors = cfg.get('sensors')
        if sensors is None:
            sensors = {k: v for k, v in DEMO_SENSORS.items() if k < n}
        variables = cfg.get('variables')
        if variables is None:
            variables = {k: v for k, v in DEMO_VARS.items() if int(k[1:k.index('_')]) < n} if mods != 'base' else {}
        active = int(cfg.get('active', 0))
        if hasattr(self, 'h'):
            self.h.close()
        self.usage = mods != 'base' and not cfg.get('no_usage')
        self.h = kh.Harness(self.args.klippy, variables=variables,
                            include_filament=mods in ('filament', 'orbiter'),
                            include_orbiter=mods == 'orbiter', include_usage=self.usage,
                            machine_kw={'tool_numbers': tools, 'active': active,
                                        'state': cfg.get('state', 'standby'), 'sensors': sensors,
                                        'max_extrude_only_distance': 150.})
        p = self.h.printer
        m = self.h.machine
        self.p, self.m = p, m
        g = p.lookup_object('gcode')
        g.register_output_handler(self.on_output)
        add = p.add_object
        Obj = kh.Obj
        settings = p.lookup_object('configfile').status['settings']
        for t in tools:
            tool = m.tools[t].status
            tool['fan'] = 'T%d_partfan' % t          # KTC-E reports the raw config value
            tool.update({'params_park_x': 30.0 + 60 * t, 'params_park_y': -6.0, 'params_park_z': 315.0,
                         'params_safe_y': 120.0, 'params_fast_speed': 20000.0, 'params_path_speed': 1200.0,
                         'gcode_x_offset': 0.0 if t == 0 else round(0.12 * t - 0.31, 3),
                         'gcode_y_offset': 0.0 if t == 0 else round(-0.08 * t + 0.17, 3),
                         'gcode_z_offset': 0.0 if t == 0 else round(0.021 * t - 0.05, 3)})
            add('fan_generic T%d_partfan' % t, Obj({'speed': 0.0, 'rpm': None}))
            add('heater_fan T%d_hotend_fan' % t, Obj({'speed': 0.0, 'rpm': None}))
            ext = m.ext_of[t]
            p.lookup_object(ext).status['temperature'] = 24.0 + t * 0.3
            settings['tool t%d' % t] = {'tool_number': t, 'extruder': ext, 'fan': 'T%d_partfan' % t,
                                        'detection_pin': '^nhkT%d:PB9' % t}
            settings['heater_fan t%d_hotend_fan' % t] = {'heater': [ext], 'heater_temp': 50.0, 'pin': 'nhkT%d:PB5' % t}
            settings['fan_generic t%d_partfan' % t] = {'pin': 'nhkT%d:PA8' % t}
            settings[ext]['max_temp'] = 300.0 if t != 5 else 280.0
        # head LEDs like the reference build: [neopixel headlight_T<n>], chain_count 3, GRBW
        # (LED 1 = logo, LEDs 2-3 = nozzle). --leds / {"leds": [...]} picks which heads have one.
        leds = cfg.get('leds')
        leds = tools if leds is None else [int(x) for x in leds if int(x) < n]
        self.leds, self.led_manual = {}, set()
        for t in leds:
            o = Obj({'color_data': [[0.0, 0.0, 0.0, 0.0] for _ in range(3)]})
            add('neopixel headlight_T%d' % t, o)
            self.leds[t] = o
            settings['neopixel headlight_t%d' % t] = {'pin': 'nhkT%d:PD3' % t, 'chain_count': 3,
                                                      'color_order': ['GRBW'], 'initial_red': 0.0}
        self.led_frame = 0
        settings['toolchanger'] = {'t_command_restore_axis': 'Z'}
        settings['tools_calibrate'] = {'pin': '^PG15'}
        settings['tool_probe_endstop'] = {}
        settings['save_variables'] = {'filename': '~/printer_data/config/variables.cfg'}
        for sec in self.h.fileconfig.sections():
            settings.setdefault(sec.lower(), {})
        m.tc.status.update({'has_detection': True, 'detected_tool': m.tc.status['tool'],
                            'detected_tool_number': active})
        self.tpe = Obj({'active_tool_number': active, 'active_tool_probe': 'tool_probe T%d' % active if active >= 0 else None,
                        'active_tool_probe_z_offset': -0.65, 'last_query': False})
        add('tool_probe_endstop', self.tpe)
        self.tcal = Obj({'last_result': [0.0, 0.0, 0.0], 'last_probe_offset': 0.0, 'calibration_probe_inactive': True,
                         'last_x_result': 0.0, 'last_y_result': 0.0, 'last_z_result': 0.0})
        add('tools_calibrate', self.tcal)
        self.pause = Obj({'is_paused': cfg.get('state') == 'paused'})
        add('pause_resume', self.pause)
        # keep the harness StubWebhooks (real remote-method error) and give it a status
        wh = p.objects['webhooks']
        wh.get_status = lambda et: {'state': 'ready', 'state_message': 'Printer is ready'}
        sm_mode = cfg.get('spoolman', 'connected')
        wh.methods = {'spoolman_set_active_spool'} if sm_mode != 'off' else set()
        _call = wh.call_remote_method
        def call_remote(method, **kw):
            _call(method, **kw)
            if method == 'spoolman_set_active_spool':
                self.set_spool(kw.get('spool_id'))
        wh.call_remote_method = call_remote
        if not hasattr(self, 'sm'):
            self.sm = {'db': spoolman_db(), 'active': None}
        self.sm['mode'] = sm_mode
        self.files = config_tree(cfg.get('configs', 'conflict'), tools)
        if not self.usage:
            self.files.pop('ktc-filament/usage/usage_addon.cfg')
            self.files['printer.cfg'] = self.files['printer.cfg'].replace('[include ktc-filament/usage/usage_addon.cfg]\n', '')
        self.pending = {}
        self.sync_pending()
        add('heater_bed', Obj({'temperature': 60.1, 'target': 60.0, 'power': 0.3}))
        add('gcode_move', Obj({'gcode_position': [175.0, 175.0, 10.0, 0.0], 'homing_origin': [0, 0, 0, 0],
                               'speed_factor': 1.0, 'extrude_factor': 1.0, 'absolute_coordinates': True}))
        for k in cfg.get('sensors_disabled', DEMO_SENSORS_DISABLED):
            o = p.lookup_object('filament_switch_sensor filament_sensor_T%s' % k, None)
            if o and int(k) < n:
                o.status['enabled'] = False
        add('idle_timeout', Obj({'state': 'Idle', 'printing_time': 0.0}))
        for name in KTCE_MACROS:
            if 'gcode_macro ' + name not in p.objects:
                add('gcode_macro ' + name, Obj({'x': 77.0, 'y': -2.0, 'z': 5.0} if name == '_CALIBRATION_SWITCH' else {}))
        for d in cfg.get('drop_macros', []):     # simulate a user without some KTC-E macro
            p.objects.pop('gcode_macro ' + d, None)
        if cfg.get('version_mismatch'):
            o = p.lookup_object('gcode_macro _ORBITER_SETTINGS', None)
            if o:
                o.variables['requires_filament_version'] = '0.2.0'
        if cfg.get('homed') is not None:
            m.toolhead.status['homed_axes'] = cfg['homed']
        if cfg.get('changing'):
            m.tc.status['status'] = 'changing'
        if cfg.get('tc_status'):
            m.tc.status['status'] = cfg['tc_status']
        self.register_ktce(g, tools, cfg)
        self.last_sent = {}

    # --------------------------------------------------- KTC-E command stubs
    def register_ktce(self, g, tools, cfg):
        m = self.m
        gc = g                               # g is rebound to a wrapper below
        def say(msg):
            gc.respond_info(msg)
        def finish_change(t):
            def done():
                if m.tc.status['status'] == 'changing':
                    m.tc.status['status'] = 'ready'
                    self.push_status()
            if self.loop:
                self.loop.call_later(float(self.cfg.get('change_time', 2.0)), done)
            else:
                done()
        def select(t, gcmd):
            if t not in m.tools:
                raise gcmd.error('Select tool: T%d not found' % t)
            if m.tc.status['status'] not in ('ready', 'changing'):
                raise gcmd.error('Cannot select tool, toolchanger status is %s' % m.tc.status['status'])
            m.tc.status['status'] = 'changing'
            m.set_active(t)
            m.toolhead.status['extruder'] = m.ext_of[t]
            m.tc.status.update({'detected_tool': 'tool T%d' % t, 'detected_tool_number': t})
            self.tpe.status.update({'active_tool_number': t, 'active_tool_probe': 'tool_probe T%d' % t})
            finish_change(t)
        def tool_of(gcmd, default_active=False):
            t = gcmd.get_int('T', None)
            if t is None and gcmd.get('TOOL', None):
                t = int(gcmd.get('TOOL').split('T')[-1])
            if t is None:
                t = gcmd.get_int('P', None)
            if t is None and default_active:
                t = m.tc.status['tool_number']
            if t is None:
                raise gcmd.error('Missing TOOL=<name> or T=<number>')
            if t not in m.tools:
                raise gcmd.error('Tool #%d is not assigned' % t)
            return t
        def set_temp(t, target):
            o = self.p.lookup_object(m.ext_of[t])
            o.status['target'] = float(target)
        def h_select(gcmd):
            select(tool_of(gcmd), gcmd)
        def h_init(gcmd):
            if gcmd.get_int('RECOVER', 0) == 1:
                m.tc.status['status'] = 'ready'
                say('Toolchanger recovered')
                return
            t = gcmd.get_int('T', self.tpe.status['active_tool_number'])
            m.tc.status['status'] = 'ready'
            m.set_active(t)
            say('initialize from detected tool')
        def h_settemp(gcmd):
            set_temp(tool_of(gcmd, True), gcmd.get_float('TARGET', 0.))
        def h_m104(gcmd):
            t = gcmd.get_int('T', None)
            if t is None:
                t = m.tc.status['tool_number']
            set_temp(t, gcmd.get_float('S', 0.))
        def h_m106(gcmd):
            t = tool_of(gcmd, True)
            self.p.lookup_object('fan_generic T%d_partfan' % t).status['speed'] = min(gcmd.get_float('S', 255.) / 255., 1.0)
        def h_m107(gcmd):
            t = tool_of(gcmd, True)
            self.p.lookup_object('fan_generic T%d_partfan' % t).status['speed'] = 0.0
        def h_off(gcmd):
            for t in m.tools:
                set_temp(t, 0)
        def h_detect(gcmd):
            say('Detected tool T%d' % self.tpe.status['active_tool_number'])
        def h_query(gcmd):
            say('Calibration probe: %s' % ('open' if self.tcal.status['calibration_probe_inactive'] else 'TRIGGERED'))
        def h_cal(gcmd):
            t = m.tc.status['tool_number']
            r = [round(0.11 * t - 0.3, 4), round(-0.07 * t + 0.15, 4), round(0.02 * t - 0.04, 4)]
            self.tcal.status.update({'last_result': r, 'last_x_result': r[0], 'last_y_result': r[1], 'last_z_result': r[2]})
            say('Tool offset is %.4f,%.4f,%.4f' % tuple(r))
        def h_home(gcmd):
            m.toolhead.status['homed_axes'] = 'xyz'
            say('Homed (mock)')
        def h_saveconfig(gcmd):
            err = save_config_conflicts(self.files, self.pending)
            if err:
                raise gcmd.error(err)
            self.pending = {}
            h_restart(gcmd)
        def h_restart(gcmd):
            if self.loop:
                self.loop.call_later(0.2, lambda: asyncio.ensure_future(self.restart(self.cfg)))
        def h_pause(gcmd):
            m.print_stats.status['state'] = 'paused'
            self.pause.status['is_paused'] = True
        def h_resume(gcmd):
            if m.print_stats.status['state'] != 'paused':
                raise gcmd.error('Print is not paused, resume aborted')
            m.print_stats.status['state'] = 'printing'
            self.pause.status['is_paused'] = False
        def h_cancel(gcmd):
            m.print_stats.status['state'] = 'cancelled'
            self.pause.status['is_paused'] = False
        def h_fan(gcmd):
            name = gcmd.get('FAN')
            o = self.p.lookup_object('fan_generic ' + name, None)
            if o:
                o.status['speed'] = gcmd.get_float('SPEED', 0.)
        def h_save(gcmd):
            t = tool_of(gcmd, True)
            p = gcmd.get('PARAMETER')
            self.pending.setdefault('tool T%d' % t, {})[p] = '%.6f' % m.tools[t].status.get(p, 0.0)
            self.sync_pending()
            say('SAVE_TOOL_PARAMETER T%d %s (pending SAVE_CONFIG)' % (t, p))
        def h_set(gcmd):
            t = tool_of(gcmd, True)
            p = gcmd.get('PARAMETER')
            v = gcmd.get_float('VALUE')
            m.tools[t].status[p] = v
        def echo(gcmd):
            say('ok (mock) %s' % gcmd.get_commandline())
        def h_setled(gcmd):
            name = gcmd.get('LED')
            t = next((k for k, o in self.leds.items() if self.p.objects.get('neopixel ' + name) is o), None)
            if t is None:
                raise gcmd.error('Unknown LED "%s"' % name)
            col = [gcmd.get_float(k, 0., minval=0., maxval=1.) for k in ('RED', 'GREEN', 'BLUE', 'WHITE')]
            idx = gcmd.get_int('INDEX', None, minval=1, maxval=3)
            cd = self.leds[t].status['color_data']
            for i in ([idx - 1] if idx else range(len(cd))):
                cd[i] = col
            self.led_manual.add(t)           # stop the demo animation overwriting it
        handlers = {'SELECT_TOOL': h_select, 'INITIALIZE_TOOLCHANGER_ORIG': h_init, 'SET_TOOL_TEMPERATURE': h_settemp,
                    'M104.1': h_m104, 'M109.1': h_m104, 'TURN_OFF_HEATERS': h_off, 'DETECT_ACTIVE_TOOL_PROBE': h_detect,
                    'TOOL_CALIBRATE_QUERY_PROBE': h_query, 'TOOL_CALIBRATE_TOOL_OFFSET': h_cal,
                    'G28': h_home, 'SAVE_CONFIG': h_saveconfig, 'FIRMWARE_RESTART': h_restart, 'RESTART': h_restart,
                    'RESUME_BASE': h_resume, 'SET_FAN_SPEED': h_fan, 'SAVE_TOOL_PARAMETER': h_save,
                    'SET_TOOL_PARAMETER': h_set, 'M106': h_m106, 'M107': h_m107}
        drop = set(cfg.get('drop_commands', []))
        _reg = g.register_command
        def reg(name, fn, desc=None):
            _reg(name, None)                 # replace a builtin/harness stub of the same name
            _reg(name, fn, desc=desc)
        g = type('G', (), {'register_command': staticmethod(reg)})
        def wrap(name, fn):
            def run(gcmd):
                m.calls.append(gcmd.get_commandline().strip())
                fn(gcmd)
            return run
        if self.leds:
            g.register_command('SET_LED', wrap('SET_LED', h_setled), desc='Set the color of an LED')
        for name, desc in KTCE_PY.items():
            if name in drop:
                continue
            g.register_command(name, wrap(name, handlers.get(name, echo)), desc=desc)
        for name in KTCE_NODESC:
            if name not in drop:
                g.register_command(name, wrap(name, handlers.get(name, echo)))
        if cfg.get('unselect'):
            g.register_command('UNSELECT_TOOL', wrap('UNSELECT_TOOL', lambda gcmd: m.set_active(-1)), desc='Unselect active tool')
        for t in tools:
            g.register_command('T%d' % t, wrap('T%d' % t, (lambda t: lambda gcmd: select(t, gcmd))(t)), desc='Select tool %d' % t)
        # KTC-E macros as stubs (the mock does not load the KTC-E cfg files)
        mac = {'INITIALIZE_TOOLCHANGER': lambda gcmd: h_init(gcmd), 'VERIFY_TOOL_DETECTED': echo,
               'M104': h_m104, 'M109': h_m104, 'RESUME': h_resume, 'PAUSE': h_pause, 'CANCEL_PRINT': h_cancel}
        for name in KTCE_MACROS:
            if name.startswith('_') or name in drop or 'gcode_macro ' + name not in self.p.objects:
                continue
            g.register_command(name, wrap(name, mac.get(name, echo)), desc='G-Code macro')

    # ------------------------------------------------------------ spoolman
    def set_spool(self, sid):
        sid = int(sid) if sid not in (None, '', 0) else None
        self.sm['active'] = sid
        self.broadcast('notify_active_spool_set', [{'spool_id': sid}])

    def sync_pending(self):
        cf = self.p.lookup_object('configfile').status
        cf['save_config_pending_items'] = copy.deepcopy(self.pending)
        cf['save_config_pending'] = bool(self.pending)

    def spoolman_proxy(self, params):
        if self.sm['mode'] == 'off':
            raise RpcError(404, 'Method not found: server.spoolman.proxy')
        v2 = params.get('use_v2_response', False)
        def fail(code, msg):
            if v2:
                return {'response': None, 'error': {'status_code': code, 'message': msg}}
            raise RpcError(code, msg)
        if self.sm['mode'] != 'connected':
            return fail(503, 'Spoolman server not available')
        meth, path, body = params.get('request_method', 'GET'), params.get('path', ''), params.get('body') or {}
        db = self.sm['db']
        parts = [x for x in path.split('/') if x]
        if parts[:1] != ['v1'] or len(parts) < 2 or parts[1] not in ('spool', 'filament', 'vendor'):
            return fail(404, 'Not found')
        kind = parts[1]
        rows = db.setdefault(kind, [])
        def ok(r):
            return {'response': r, 'response_headers': {}, 'error': None} if v2 else r
        if len(parts) == 2:
            if meth == 'GET':
                return ok(copy.deepcopy([r for r in rows if not r.get('archived') or 'allow_archived=true' in (params.get('query') or '')]))
            if meth == 'POST':
                new = dict(body)
                new['id'] = max([r['id'] for r in rows] or [0]) + 1
                if kind == 'spool':
                    fid = new.pop('filament_id', None)
                    f = next((x for x in db['filament'] if x['id'] == fid), None)
                    if not f:
                        return fail(400, 'filament_id is required')
                    new['filament'] = f
                    new.setdefault('initial_weight', f.get('weight') or 1000.0)
                    new.setdefault('remaining_weight', new['initial_weight'])
                    new['used_weight'] = new['initial_weight'] - new['remaining_weight']
                    new.setdefault('archived', False)
                if kind == 'filament':
                    vid = new.pop('vendor_id', None)
                    v = next((x for x in db.get('vendor', []) if x['id'] == vid), None)
                    if v:
                        new['vendor'] = v
                rows.append(new)
                return ok(copy.deepcopy(new))
            return fail(405, 'Method not allowed')
        row = next((r for r in rows if r['id'] == int(parts[2])), None)
        if not row:
            return fail(404, 'No %s with id %s' % (kind, parts[2]))
        if meth == 'GET':
            return ok(copy.deepcopy(row))
        if meth == 'PATCH':
            row.update({k: v for k, v in body.items() if k != 'id'})
            if kind == 'spool' and 'remaining_weight' in body:
                row['used_weight'] = (row.get('initial_weight') or 1000.0) - row['remaining_weight']
            return ok(copy.deepcopy(row))
        if meth == 'DELETE':
            rows.remove(row)
            return ok(copy.deepcopy(row))
        return fail(405, 'Method not allowed')

    # ------------------------------------------------------------ status
    def on_output(self, msg):
        for line in msg.split('\n'):
            self.store.append({'message': line, 'time': time.time(), 'type': 'response'})
            self.broadcast('notify_gcode_response', [line])
        del self.store[:-200]

    def objects(self):
        return {k: o for k, o in self.p.objects.items() if hasattr(o, 'get_status') and k not in ('gcode',)}

    def status_of(self, name, fields=None):
        o = self.objects().get(name)
        if o is None:
            return None
        st = copy.deepcopy(o.get_status(time.monotonic()))
        if fields:
            st = {k: v for k, v in st.items() if k in fields}
        return st

    def query(self, req):
        out = {}
        for name, fields in (req or {}).items():
            st = self.status_of(name, fields)
            if st is not None:
                out[name] = st
        return out

    def push_status(self):
        for ws, info in list(self.clients_info.items()):
            cur = self.query(info['sub'])
            diff = {}
            last = info.setdefault('last', {})
            for name, st in cur.items():
                prev = last.get(name, {})
                d = {k: v for k, v in st.items() if prev.get(k, object()) != v}
                if d:
                    diff[name] = d
            info['last'] = cur
            if diff:
                self.send(ws, {'jsonrpc': '2.0', 'method': 'notify_status_update', 'params': [diff, time.monotonic()]})

    def tick(self):
        if self.klippy_state != 'ready':
            return
        for t in self.m.tools:
            o = self.p.lookup_object(self.m.ext_of[t]).status
            tgt = o['target']
            amb = 24.0 + t * 0.3
            goal = tgt if tgt > 0 else amb
            o['temperature'] = round(o['temperature'] + (goal - o['temperature']) * 0.25, 2)
            o['power'] = 0.6 if tgt > o['temperature'] + 2 else (0.25 if tgt > 0 else 0.0)
            hf = self.p.lookup_object('heater_fan T%d_hotend_fan' % t).status
            hf['speed'] = 1.0 if (tgt > 0 or o['temperature'] > 50) else 0.0
        self.tick_leds()
        self.tick_print()
        self.push_status()

    def tick_print(self):
        # a print in progress extrudes on the active tool; the usage watcher runs
        # through the REAL delayed_gcode.py timer, once a second like on the printer
        m = self.m
        if m.print_stats.status['state'] == 'printing' and m.tc.status['status'] == 'ready' and self.cfg.get('extrude', True):
            pos = m.toolhead.status['position']
            pos[3] = round(pos[3] + 18.0, 2)
            sm = self.sm
            if sm['active'] is not None and sm['mode'] == 'connected':
                sp = next((x for x in sm['db']['spool'] if x['id'] == sm['active']), None)
                if sp:
                    g = 18.0 * sp['filament']['density'] * 3.1416 * 0.0875 ** 2 / 10 * 10   # mm -> g (1.75 mm)
                    sp['remaining_weight'] = round(max(0.0, sp['remaining_weight'] - g / 10), 2)
                    sp['used_weight'] = round(sp['initial_weight'] - sp['remaining_weight'], 2)
        self.ticks = getattr(self, 'ticks', 0) + 1
        if self.usage and self.ticks % 2 == 0:
            try:
                self.h.tick()
            except Exception as e:
                print('usage watcher error %r' % e, flush=True)

    def tick_leds(self):
        # demo colours roughly like a KTC-E/Stealthburner LED setup: active = green,
        # heating = pulsing yellow, changing = breathing blue, idle = dim red
        self.led_frame += 1
        f = self.led_frame
        tc = self.m.tc.status
        for t, o in self.leds.items():
            if t in self.led_manual:
                continue
            e = self.p.lookup_object(self.m.ext_of[t]).status
            active = tc.get('tool_number') == t
            wave = 0.5 + 0.5 * math.sin(f * 0.9 + t)
            if active and tc.get('status') == 'changing':
                logo = [0.0, 0.25 * wave, 0.4 + 0.6 * wave, 0.0]
            elif e['target'] > 0 and e['temperature'] < e['target'] - 3:
                logo = [0.6 + 0.4 * wave, 0.45 + 0.3 * wave, 0.0, 0.0]
            elif active:
                logo = [0.0, 1.0, 0.0, 0.0]
            else:
                logo = [0.8, 0.0, 0.0, 0.0]
            noz = [0.0, 0.0, 0.0, 1.0 if active else 0.35]
            o.status['color_data'] = [[round(x, 3) for x in logo], list(noz), list(noz)]

    # ---------------------------------------------------------- transport
    clients_info = {}

    def send(self, ws, obj):
        if not ws.closed:
            asyncio.ensure_future(ws.send_str(json.dumps(obj)))

    def broadcast(self, method, params):
        for ws in list(self.clients_info):
            self.send(ws, {'jsonrpc': '2.0', 'method': method, 'params': params})

    async def restart(self, cfg):
        self.klippy_state = 'startup'
        self.broadcast('notify_klippy_disconnected', [])
        for info in self.clients_info.values():
            info['sub'] = {}
            info['last'] = {}
        await asyncio.sleep(0.6)
        self.build(cfg)
        self.klippy_state = 'ready'
        self.broadcast('notify_klippy_ready', [])

    @staticmethod
    def default_update():
        # what Moonraker's git_repo updater reports (synthetic hashes)
        return {'name': 'stealthchanger-gui', 'configured_type': 'git_repo', 'detected_type': 'git_repo',
                'channel': 'stable', 'branch': 'main', 'remote_alias': 'origin', 'owner': '?',
                'repo_name': 'StealthChanger-GUI', 'version': 'v0.2.12', 'remote_version': 'v0.2.12',
                'rollback_version': 'v0.2.11', 'full_version_string': 'v0.2.12-0-g0123abcd',
                'current_hash': '0123abcd0123abcd0123abcd0123abcd0123abcd',
                'remote_hash': '0123abcd0123abcd0123abcd0123abcd0123abcd',
                'is_dirty': False, 'detached': False, 'commits_behind': [], 'commits_behind_count': 0,
                'pristine': True, 'corrupt': False, 'is_valid': True, 'warnings': [], 'anomalies': [],
                'git_messages': [], 'info_tags': []}

    def rpc(self, ws, method, params):
        info = self.clients_info[ws]
        not_ready = self.klippy_state != 'ready'
        if method == 'server.connection.identify':
            info['client'] = params
            return {'connection_id': id(ws)}
        if method == 'server.info':
            comps = ['database', 'history', 'file_manager'] + (['spoolman'] if self.sm['mode'] != 'off' else [])
            return {'klippy_connected': True, 'klippy_state': self.klippy_state, 'components': comps,
                    'warnings': [], 'moonraker_version': 'v0.9.3-mock', 'api_version': [1, 5, 0]}
        if method == 'printer.info':
            return {'state': self.klippy_state, 'state_message': 'Printer is ready', 'hostname': 'voron-mock',
                    'software_version': 'v0.13.0-mock', 'config_file': '/home/pi/printer_data/config/printer.cfg'}
        if method.startswith('server.spoolman.'):
            if self.sm['mode'] == 'off':
                raise RpcError(404, 'Method not found: %s' % method)
            if method == 'server.spoolman.status':
                return {'spoolman_connected': self.sm['mode'] == 'connected', 'pending_reports': [], 'spool_id': self.sm['active']}
            if method == 'server.spoolman.get_spool_id':
                return {'spool_id': self.sm['active']}
            if method == 'server.spoolman.post_spool_id':
                self.set_spool(params.get('spool_id'))
                return {'spool_id': self.sm['active']}
            if method == 'server.spoolman.proxy':
                return self.spoolman_proxy(params)
        if method == 'machine.update.status':
            vi = {'system': {'package_count': 0, 'package_list': []}}
            if self.update is not None:
                vi['stealthchanger-gui'] = self.update
            return {'busy': False, 'github_rate_limit': None, 'github_requests_remaining': None,
                    'github_limit_reset_time': None, 'version_info': vi}
        if method == 'server.files.roots':
            return [{'name': 'config', 'path': '/home/pi/printer_data/config', 'permissions': 'rw'},
                    {'name': 'gcodes', 'path': '/home/pi/printer_data/gcodes', 'permissions': 'rw'}]
        if method == 'server.files.list':
            if params.get('root', 'gcodes') != 'config':
                return []
            return [{'path': k, 'modified': 1761900000.0, 'size': len(v), 'permissions': 'rw'} for k, v in sorted(self.files.items())]
        if method == 'server.gcode_store':
            return {'gcode_store': self.store[-int(params.get('count', 100)):]}
        if method.startswith('server.database.'):
            ns = params.get('namespace')
            key = params.get('key')
            d = self.db.setdefault(ns, {})
            if method.endswith('get_item'):
                if key is None:
                    return {'namespace': ns, 'key': None, 'value': d}
                if key not in d:
                    raise RpcError(404, "Key '%s' in namespace '%s' not found" % (key, ns))
                return {'namespace': ns, 'key': key, 'value': d[key]}
            if method.endswith('post_item'):
                d[key] = params.get('value')
                return {'namespace': ns, 'key': key, 'value': d[key]}
            if method.endswith('delete_item'):
                return {'namespace': ns, 'key': key, 'value': d.pop(key, None)}
        if method.startswith('printer.') and not_ready and method not in ('printer.info',):
            raise RpcError(503, 'Klippy Disconnected')
        if method == 'printer.objects.list':
            return {'objects': sorted(self.objects())}
        if method == 'printer.objects.query':
            return {'eventtime': time.monotonic(), 'status': self.query(params.get('objects'))}
        if method == 'printer.objects.subscribe':
            info['sub'] = dict(params.get('objects') or {})
            st = self.query(info['sub'])
            info['last'] = copy.deepcopy(st)
            return {'eventtime': time.monotonic(), 'status': st}
        if method == 'printer.gcode.help':
            return dict(self.p.lookup_object('gcode').gcode_help)
        if method == 'printer.gcode.script':
            script = params.get('script', '')
            self.log.append(script)
            print('GCODE> ' + script.replace('\n', '\n       '), flush=True)
            lines, err = self.h.run(script)
            self.push_status()
            if err:
                self.on_output('!! ' + err)
                raise RpcError(400, err)
            return 'ok'
        if method == 'printer.emergency_stop':
            self.log.append('M112')
            asyncio.ensure_future(self.restart(self.cfg))
            return 'ok'
        if method in ('printer.restart', 'printer.firmware_restart'):
            asyncio.ensure_future(self.restart(self.cfg))
            return 'ok'
        raise RpcError(404, 'Method not found: %s' % method)


class RpcError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def make_app(sim):
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        sim.clients_info[ws] = {'sub': {}, 'last': {}}
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                rid = req.get('id')
                try:
                    res = sim.rpc(ws, req.get('method'), req.get('params') or {})
                    out = {'jsonrpc': '2.0', 'result': res, 'id': rid}
                except RpcError as e:
                    out = {'jsonrpc': '2.0', 'error': {'code': e.code, 'message': e.msg}, 'id': rid}
                except Exception as e:      # mock bug: report it like Moonraker would
                    out = {'jsonrpc': '2.0', 'error': {'code': 500, 'message': 'mock error: %r' % e}, 'id': rid}
                await ws.send_str(json.dumps(out))
        finally:
            sim.clients_info.pop(ws, None)
        return ws

    async def mock_set(request):
        body = await request.json()
        m = sim.m
        if 'state' in body:
            m.print_stats.status['state'] = body['state']
            sim.pause.status['is_paused'] = body['state'] == 'paused'
        if 'tc_status' in body:
            m.tc.status['status'] = body['tc_status']
        if 'sensors' in body:
            for k, v in body['sensors'].items():
                o = sim.p.lookup_object('filament_switch_sensor filament_sensor_T%s' % k, None)
                if o:
                    o.status['filament_detected'] = bool(v)
        if 'sensor_enabled' in body:
            for k, v in body['sensor_enabled'].items():
                o = sim.p.lookup_object('filament_switch_sensor filament_sensor_T%s' % k, None)
                if o:
                    o.status['enabled'] = bool(v)
        if 'homed' in body:
            m.toolhead.status['homed_axes'] = body['homed']
        if 'epos' in body:
            m.toolhead.status['position'][3] = float(body['epos'])
        if 'pending' in body:
            sim.pending = body['pending'] or {}
            sim.sync_pending()
        if 'spoolman' in body:
            sim.sm['mode'] = body['spoolman']
            sim.broadcast('notify_spoolman_status_changed', [{'spoolman_connected': body['spoolman'] == 'connected'}])
        if 'update' in body:
            if body['update'] is None:
                sim.update = None
            else:
                sim.update = dict(sim.update or sim.default_update(), **body['update'])
        if 'temps' in body:
            for k, v in body['temps'].items():
                sim.p.lookup_object(m.ext_of[int(k)]).status['temperature'] = float(v)
        sim.push_status()
        return web.json_response({'ok': True})

    async def mock_restart(request):
        body = await request.json()
        cfg = dict(vars(sim.args))
        cfg.update(body)
        await sim.restart(cfg)
        return web.json_response({'ok': True})

    async def mock_log(request):
        return web.json_response({'gcode': sim.log, 'variables': sim.h.variables(), 'db': sim.db,
                                  'spoolman': {'active': sim.sm['active'], 'spools': sim.sm['db']['spool'], 'filaments': sim.sm['db']['filament']}})

    async def config_file(request):
        path = request.match_info['path']
        if path not in sim.files:
            return web.json_response({'error': {'code': 404, 'message': 'File does not exist'}}, status=404)
        return web.Response(text=sim.files[path], content_type='text/plain')

    # Static files like an old (rev 1) nginx site: Last-Modified + ETag, no Cache-Control, so
    # browsers cache index.html heuristically. /mock/release pretends a new version was pulled:
    # index.html and app.js are served rewritten to that version, with a new Last-Modified.
    gui_ver = re.search(r"var GUI_VERSION = '([^']+)'", open(os.path.join(GUI, 'js', 'app.js')).read()).group(1)
    rel = {'index': None, 'app': None, 'time': time.time() - 30 * 86400, 'hits': []}

    async def static(request):
        rel_path = request.match_info.get('path') or 'index.html'
        full = os.path.realpath(os.path.join(GUI, rel_path))
        if not full.startswith(os.path.realpath(GUI) + os.sep) or not os.path.isfile(full):
            raise web.HTTPNotFound()
        rel['hits'].append(request.path_qs)
        body = open(full, 'rb').read()
        if rel_path == 'index.html' and rel['index']:
            body = body.decode().replace('content="%s"' % gui_ver, 'content="%s"' % rel['index']) \
                .replace('?v=%s"' % gui_ver, '?v=%s"' % rel['index']).encode()
        if rel_path == 'js/app.js' and rel['app']:
            body = body.decode().replace("var GUI_VERSION = '%s'" % gui_ver, "var GUI_VERSION = '%s'" % rel['app']).encode()
        etag = '"%x-%x"' % (len(body), hash(body) & 0xffffffff)
        lm = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(rel['time']))
        hdr = {'ETag': etag, 'Last-Modified': lm}
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=hdr)
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        return web.Response(body=body, headers=hdr, content_type=ctype)

    async def mock_release(request):
        body = await request.json()
        if 'reset' in body:
            rel.update(index=None, app=None, time=time.time() - 30 * 86400)
        if 'version' in body:   # a consistent release: index.html and app.js both move
            rel.update(index=body['version'], app=body['version'], time=time.time())
        if 'index' in body:     # or only one of them, to test the loop guard
            rel.update(index=body['index'], time=time.time())
        if 'app' in body:
            rel.update(app=body['app'], time=time.time())
        if body.get('clear_hits'):
            rel['hits'] = []
        return web.json_response({'ok': True, 'gui_version': gui_ver, 'index': rel['index'], 'app': rel['app'], 'hits': rel['hits']})

    async def mock_notify(request):
        body = await request.json()
        sim.broadcast(body['method'], body.get('params', []))
        return web.json_response({'ok': True})

    async def ticker(app):
        sim.loop = asyncio.get_running_loop()
        async def run():
            while True:
                await asyncio.sleep(0.5)
                try:
                    sim.tick()
                except Exception as e:
                    print('tick error %r' % e, flush=True)
        app['ticker'] = asyncio.ensure_future(run())

    async def firmware_proxy(request):
        # Same path the nginx site proxies, so the Firmware tab works against
        # the dev server. The helper binds to loopback only. Any failure here
        # is "helper not running": the page must not see a 500.
        offline = {'ok': False, 'error': 'Firmware helper is not running. Run setup.sh on the printer.'}
        try:
            port = os.environ.get('SC_FIRMWARE_PORT', '7140')
            tail = request.match_info.get('path', '')
            dest = 'http://127.0.0.1:%s/firmware/%s' % (port, tail)
            if request.rel_url.query_string:
                dest += '?' + request.rel_url.query_string
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=1.5)
            data = await request.read()
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {}
                ctype = request.headers.get('Content-Type')
                if ctype:
                    headers['Content-Type'] = ctype
                async with session.request(request.method, dest, data=data or None, headers=headers) as resp:
                    body = await resp.read()
                    ct = resp.headers.get('Content-Type', 'application/octet-stream')
                    return web.Response(status=resp.status, body=body, headers={'Content-Type': ct, 'Cache-Control': 'no-store'})
        except Exception as exc:
            sys.stderr.write('firmware proxy: %s\n' % exc.__class__.__name__)
            return web.json_response(offline, status=502)

    app = web.Application()
    app.on_startup.append(ticker)
    app.router.add_get('/websocket', ws_handler)
    app.router.add_post('/mock/set', mock_set)
    app.router.add_post('/mock/restart', mock_restart)
    app.router.add_get('/mock/log', mock_log)
    app.router.add_get('/server/files/config/{path:.+}', config_file)
    app.router.add_post('/mock/release', mock_release)
    app.router.add_post('/mock/notify', mock_notify)
    app.router.add_route('GET', '/firmware/{path:.*}', firmware_proxy)
    app.router.add_route('POST', '/firmware/{path:.*}', firmware_proxy)
    app.router.add_route('PUT', '/firmware/{path:.*}', firmware_proxy)
    app.router.add_get('/{path:.*}', static)
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--klipper', default=None, help='Klipper source (default: ../klipper-src or ~/klipper)')
    ap.add_argument('--modules', choices=['base', 'filament', 'orbiter'], default='orbiter')
    ap.add_argument('--tools', type=int, default=6)
    ap.add_argument('--active', type=int, default=0)
    ap.add_argument('--state', default='standby')
    ap.add_argument('--changing', action='store_true', help='start with a tool change in progress')
    ap.add_argument('--leds', type=lambda s: [int(x) for x in s.split(',') if x != ''], default=None,
                    help='tools with a neopixel headlight_T<n> (default: all), e.g. 0,1,2 or "" for none')
    ap.add_argument('--unselect', action='store_true', help='also offer UNSELECT_TOOL (require_tool_present: False)')
    ap.add_argument('--spoolman', choices=['connected', 'disconnected', 'off'], default='connected')
    ap.add_argument('--configs', choices=['conflict', 'clean'], default='conflict',
                    help='config tree for the SAVE_CONFIG check: conflict = T1/T2 set offsets in their include file')
    ap.add_argument('--no-usage', action='store_true', help='leave out the usage add-on')
    ap.add_argument('--homed', default='xyz', help='homed axes at start ("" = not homed)')
    args = ap.parse_args()
    args.klippy = kh.find_klipper(args.klipper)
    sim = Sim(args)
    print('mock Moonraker on http://%s:%d/  (Klipper %s, modules=%s, tools=%d)' % (
        args.host, args.port, args.klippy, args.modules, args.tools), flush=True)
    web.run_app(make_app(sim), host=args.host, port=args.port, print=None)


if __name__ == '__main__':
    main()
