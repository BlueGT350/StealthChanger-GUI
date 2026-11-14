#!/usr/bin/env python3
"""Run the ktc-filament macros through REAL Klipper code.

Uses Klipper's own modules, unmodified:
  configfile.py      ConfigFileReader (include handling, '#' stripping,
                     inline ';' comments, option merging), ConfigWrapper
  gcode.py           GCodeDispatch (line splitting, ';' truncation,
                     command detection, shlex extended params, errors)
  extras/gcode_macro.py   PrinterGCodeMacro, TemplateWrapper, GCodeMacro,
                     GetStatusWrapper, SET_GCODE_VARIABLE
  extras/respond.py  RESPOND
  extras/save_variables.py  SAVE_VARIABLE + the variables file
Only these are stubbed: the reactor, the aio executor, and the printer
objects a macro reads (KTC-Easy toolchanger / tool, extruders, filament
sensors, print_stats, toolhead, configfile settings) with get_status()
shaped like the real ones. Motion / heater / PAUSE commands are stubs
that parse their params the way the real command does and record calls.

Usage:
  git clone --depth 1 https://github.com/Klipper3d/klipper.git
  python3 tests/klipper_harness.py --klipper ./klipper [--show]
  (or set KLIPPER_PATH; on a Pi, ~/klipper is found automatically)
Needs: python3 with jinja2 (Klipper's own requirement).
"""
import sys, os, argparse, contextlib, tempfile, shutil, importlib, logging, copy, re, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FILAMENT_CFG = os.path.join(ROOT, 'filament', 'filament_macros.cfg')
ORBITER_CFG = os.path.join(ROOT, 'orbiter', 'orbiter_addon.cfg')
USAGE_CFG = os.path.join(ROOT, 'usage', 'usage_addon.cfg')
DEFAULT_VARS = os.path.join(HERE, 'fixtures', 'variables.cfg')


def find_klipper(arg):
    cands = [arg, os.environ.get('KLIPPER_PATH'), os.path.join(ROOT, '..', 'klipper-src'),
             os.path.expanduser('~/klipper')]
    for c in cands:
        if not c:
            continue
        kp = os.path.join(c, 'klippy') if os.path.isdir(os.path.join(c, 'klippy')) else c
        if os.path.exists(os.path.join(kp, 'gcode.py')):
            return os.path.abspath(kp)
    sys.exit('Klipper source not found: pass --klipper /path/to/klipper or set KLIPPER_PATH')


# ---------------------------------------------------------------- stubs
class StubReactor:
    NEVER = 9999999999999999.
    timers = None
    def register_timer(self, cb, waketime=NEVER):
        if self.timers is None: self.timers = []
        t = [cb, waketime]; self.timers.append(t); return t
    def update_timer(self, t, waketime): t[1] = waketime
    NEVER = 9999999999999999.
    def __init__(self): self.t0 = time.monotonic()
    def monotonic(self): return time.monotonic() - self.t0
    def mutex(self): return contextlib.nullcontext()
    @contextlib.contextmanager
    def assert_no_pause(self): yield


class StubExecutor:
    def submit(self, fn, *a, **kw): return fn(*a, **kw)


class StubAio:
    def allocate_executor(self, name): return StubExecutor()


class Obj:
    """A printer object with a get_status() dict (deep-copied by GetStatusWrapper)."""
    def __init__(self, status): self.status = status
    def get_status(self, eventtime): return self.status


class Printer:
    def __init__(self, klippy):
        self.configfile = importlib.import_module('configfile')
        self.gcode_mod = importlib.import_module('gcode')
        self.config_error = self.configfile.error
        self.command_error = self.gcode_mod.CommandError
        self.reactor = StubReactor()
        self.objects = {}
        self.handlers = {}
    # -- the subset of klippy.Printer the loaded modules call --
    def get_reactor(self): return self.reactor
    def get_start_args(self): return {}
    # copied from klippy.py Printer.lookup_object / lookup_objects
    def lookup_object(self, name, default=None, _required=False):
        if name in self.objects:
            return self.objects[name]
        if _required:
            raise self.config_error("Unknown config object '%s'" % (name,))
        return default
    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + ' '
        objs = [(n, self.objects[n]) for n in self.objects if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs
    def add_object(self, name, obj): self.objects[name] = obj
    def load_object(self, config, section, default=None):
        if section in self.objects:
            return self.objects[section]
        if section == 'aio_executor':
            self.objects[section] = StubAio(); return self.objects[section]
        mod = importlib.import_module('extras.' + section.split()[0])
        if section in self.objects:
            return self.objects[section]
        init = getattr(mod, 'load_config_prefix' if ' ' in section else 'load_config')
        self.objects[section] = init(config.getsection(section))
        return self.objects[section]
    def register_event_handler(self, event, cb): self.handlers.setdefault(event, []).append(cb)
    def send_event(self, event, *params): return [cb(*params) for cb in self.handlers.get(event, [])]
    def invoke_shutdown(self, msg): raise RuntimeError('SHUTDOWN: ' + msg)
    def request_exit(self, r): pass
    def get_state_message(self): return ('Printer is not ready', 'startup')


class StubButton:
    """[gcode_button] stand-in: reads pin and loads press_gcode with the REAL template loader."""
    def __init__(self, config, printer):
        self.name = config.get_name().split()[-1]
        self.pin = config.get('pin')
        gm = printer.load_object(config, 'gcode_macro')
        self.press_template = gm.load_template(config, 'press_gcode')
        self.release_template = gm.load_template(config, 'release_gcode', '')
        self.printer = printer
    def press(self):
        # gcode_button runs press_gcode through the same template path
        self.printer.lookup_object('gcode').run_script(self.press_template.render())


# ------------------------------------------------- the fake machine state
class Machine:
    """Holds the state that stub commands change and status objects report."""
    def __init__(self, printer, tool_numbers=(0, 1, 2, 3, 4, 5), active=-1, state='standby',
                 sensors='all_present', sensor_kind='filament_switch_sensor',
                 max_extrude_only_distance=1400., targets=None):
        self.p = printer
        self.calls = []
        names = ['tool T%d' % n for n in tool_numbers]
        self.ext_of = {}
        for i, n in enumerate(tool_numbers):
            ext = 'extruder' if i == 0 else 'extruder%d' % i
            self.ext_of[n] = ext
            printer.add_object(ext, Obj({'temperature': 25.0, 'target': float((targets or {}).get(n, 0.)),
                                         'power': 0., 'can_extrude': False, 'pressure_advance': 0.,
                                         'smooth_time': 0.04, 'motion_queue': None}))
        tc_params = {}
        # KTC-Easy toolchanger.get_status shape (klipper/extras/toolchanger.py)
        self.tc = Obj({**tc_params, 'name': 'toolchanger', 'status': 'ready',
                       'tool': None, 'tool_number': -1,
                       'detected_tool': None, 'detected_tool_number': -1,
                       'tool_numbers': list(tool_numbers), 'tool_names': names,
                       'has_detection': False})
        printer.add_object('toolchanger', self.tc)
        self.tools = {}
        for n in tool_numbers:
            # KTC-Easy tool.get_status shape (klipper/extras/tool.py)
            o = Obj({'name': 'tool T%d' % n, 'toolchanger': 'toolchanger', 'tool_number': n,
                     'extruder': self.ext_of[n], 'extruder_stepper': None,
                     'fan': 'fan_generic T%d_partfan' % n, 'active': False,
                     'gcode_x_offset': 0.0, 'gcode_y_offset': 0.0, 'gcode_z_offset': 0.0})
            self.tools[n] = o
            printer.add_object('tool T%d' % n, o)
        self.set_active(active)
        if sensors == 'all_present':
            sensors = {n: True for n in tool_numbers}
        for n, present in (sensors or {}).items():
            printer.add_object('%s filament_sensor_T%d' % (sensor_kind, n),
                               Obj({'filament_detected': bool(present), 'enabled': True}))
        self.print_stats = Obj({'state': state, 'filename': '', 'print_duration': 0.,
                                'total_duration': 0., 'filament_used': 0., 'message': '',
                                'info': {'total_layer': None, 'current_layer': None}})
        printer.add_object('print_stats', self.print_stats)
        self.toolhead = Obj({'extruder': self.ext_of.get(active, 'extruder'),
                             'estimated_print_time': 5000.0, 'print_time': 5000.0,
                             'homed_axes': 'xyz', 'position': [0, 0, 0, 0]})
        printer.add_object('toolhead', self.toolhead)
        # configfile.get_status()['settings']: lower-case keys, parsed values,
        # defaults included (values from the user's tools/T#.cfg)
        settings = {ext: {'max_temp': 290.0, 'min_temp': 0.0, 'min_extrude_temp': 170.0,
                          'max_extrude_only_distance': float(max_extrude_only_distance),
                          'max_extrude_only_velocity': 120.0, 'max_extrude_only_accel': 1500.0}
                    for ext in self.ext_of.values()}
        printer.add_object('configfile', Obj({'settings': settings, 'config': {},
                                              'save_config_pending': False, 'warnings': []}))
        self.register_stub_commands()

    def set_active(self, n):
        self.tc.status['tool_number'] = n
        self.tc.status['tool'] = ('tool T%d' % n) if n in self.tools else None
        for k, o in self.tools.items():
            o.status['active'] = (k == n)

    def register_stub_commands(self):
        g = self.p.lookup_object('gcode')
        def rec(name, fn=None):
            def handler(gcmd):
                self.calls.append(gcmd.get_commandline().strip())
                if fn: fn(gcmd)
            g.register_command(name, handler, desc='stub')
        def heater(gcmd):
            h = gcmd.get('HEATER'); t = gcmd.get_float('TARGET', 0.)
            o = self.p.lookup_object(h, None)
            if o is None: raise gcmd.error('The value \'%s\' is not valid for HEATER' % h)
            o.status['target'] = t
        def twait(gcmd):
            s = gcmd.get('SENSOR'); mn = gcmd.get_float('MINIMUM', float('-inf'))
            mx = gcmd.get_float('MAXIMUM', float('inf'))
            o = self.p.lookup_object(s, None)
            if o is None: raise gcmd.error('Unknown sensor \'%s\'' % s)
            o.status['temperature'] = max(o.status['temperature'], mn)
        def act(gcmd):
            e = gcmd.get('EXTRUDER')
            if self.p.lookup_object(e, None) is None:
                raise gcmd.error("'%s' is not a valid extruder." % e)
            self.toolhead.status['extruder'] = e
        def g1(gcmd):
            for k in gcmd.get_command_parameters():       # real gcode_move parses every axis as float
                gcmd.get_float(k)
        def pause(gcmd): self.print_stats.status['state'] = 'paused'
        def gstate(gcmd): gcmd.get('NAME', 'default')
        for name, fn in [('SET_HEATER_TEMPERATURE', heater), ('TEMPERATURE_WAIT', twait),
                         ('ACTIVATE_EXTRUDER', act), ('PAUSE', pause),
                         ('SAVE_GCODE_STATE', gstate), ('RESTORE_GCODE_STATE', gstate)]:
            rec(name, fn)
        for name in ('G1', 'M83', 'M400'):
            rec(name, g1 if name == 'G1' else None)
        g.register_command('M117', lambda gcmd: self.calls.append('M117 ' + gcmd.get_raw_command_parameters()))


class StubWebhooks:
    """webhooks.call_remote_method with Klipper's real error behaviour."""
    def __init__(self, printer, methods):
        self.printer = printer; self.methods = set(methods or ()); self.calls = []
    def call_remote_method(self, method, **kwargs):
        if method not in self.methods:
            raise self.printer.command_error("Remote method '%s' not registered" % (method))
        self.calls.append((method, kwargs))


# ------------------------------------------------------------- the loader
class Harness:
    def __init__(self, klippy, variables=None, include_filament=True, include_orbiter=True,
                 machine_kw=None, include_usage=True, remote_methods=('spoolman_set_active_spool',)):
        if klippy not in sys.path:
            sys.path.insert(0, klippy)
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.mkdtemp(prefix='ktcf_')
        self.varfile = os.path.join(self.tmp, 'variables.cfg')
        with open(self.varfile, 'w') as f:
            if variables is None:
                f.write(open(DEFAULT_VARS).read())
            else:   # same format save_variables.py writes
                f.write('[Variables]\n' + ''.join('%s = %r\n' % kv for kv in sorted(variables.items())))
        cfgdir = os.path.join(self.tmp, 'config')
        shutil.copytree(ROOT, os.path.join(cfgdir, 'ktc-filament'),
                        ignore=shutil.ignore_patterns('tests', '*.md', '.git'))
        main = os.path.join(cfgdir, 'printer.cfg')
        inc = []
        if include_filament:
            inc.append('[include ktc-filament/filament/filament_macros.cfg]')
        if include_orbiter:
            inc.append('[include ktc-filament/orbiter/orbiter_addon.cfg]')
            inc.append('[include ktc-filament/orbiter/orbiter_pins.cfg]')
        if include_usage:
            inc.append('[include ktc-filament/usage/usage_addon.cfg]')
        # later section wins, exactly like printer.cfg overriding an include
        inc.append('[save_variables]\nfilename: %s\n' % self.varfile)
        open(main, 'w').write('\n'.join(inc) + '\n')

        self.printer = p = Printer(klippy)
        cf = p.configfile
        reader = cf.ConfigFileReader()
        self.fileconfig = reader.build_fileconfig_with_includes(reader.read_config_file(main), main)
        self.access = {}
        root = cf.ConfigWrapper(p, self.fileconfig, self.access, 'printer')
        self.output = []
        gcode = p.gcode_mod.GCodeDispatch(p)
        p.add_object('gcode', gcode)
        gcode.register_output_handler(self.output.append)
        # observation only: log every gcode line macros hand to the dispatcher
        self.lines = []
        orig = gcode.run_script_from_command
        def logged(script, _orig=orig):
            self.lines.extend(l.strip() for l in script.split('\n') if l.strip())
            return _orig(script)
        gcode.run_script_from_command = logged
        p.add_object('webhooks', StubWebhooks(p, remote_methods))
        self.remote_calls = p.objects['webhooks'].calls
        self.buttons = {}
        for sec in self.fileconfig.sections():
            kind = sec.split()[0]
            if kind in ('gcode_macro', 'save_variables', 'respond', 'delayed_gcode'):
                p.load_object(root, sec)
            elif kind == 'gcode_button':
                self.buttons[sec.split()[1]] = StubButton(root.getsection(sec), p)
        self.machine = Machine(p, **(machine_kw or {}))
        p.send_event('klippy:ready')
        del self.output[:]
        # Klipper rejects config options nobody read
        self.unused = []
        for sec in self.fileconfig.sections():
            if sec.split()[0] not in ('gcode_macro', 'save_variables', 'respond', 'gcode_button',
                                      'delayed_gcode'):
                continue
            for opt in self.fileconfig.options(sec):
                if (sec.lower(), opt.lower()) not in self.access:
                    self.unused.append('[%s] %s' % (sec, opt))

    def run(self, script):
        """Run gcode like the Mainsail console does. Returns (output lines, error or None)."""
        start = len(self.output)
        err = None
        try:
            self.printer.lookup_object('gcode').run_script(script)
        except self.printer.command_error as e:
            err = str(e)
        lines = []
        for chunk in self.output[start:]:
            lines.extend(chunk.split('\n'))
        return lines, err

    def tick(self, name='_KTC_USAGE_WATCH'):
        """Fire a [delayed_gcode] timer once through the REAL delayed_gcode.py
        (like the reactor would). Returns (console lines, still armed)."""
        dg = self.printer.lookup_object('delayed_gcode ' + name)
        start = len(self.output)
        nxt = dg._gcode_timer_event(100.)
        lines = []
        for chunk in self.output[start:]:
            lines.extend(chunk.split('\n'))
        return lines, nxt < self.printer.reactor.NEVER

    def press(self, tool):
        start = len(self.output)
        err = None
        try:
            self.buttons['T%d_orbiter_button' % tool].press()
        except self.printer.command_error as e:
            err = str(e)
        lines = []
        for chunk in self.output[start:]:
            lines.extend(chunk.split('\n'))
        return lines, err

    def variables(self):
        return self.printer.lookup_object('save_variables').get_status(0)['variables']

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------- Mainsail console (display side)
# Port of mainsail src/plugins/helpers.ts formatConsoleMessage() and the
# default filters from src/store/gui/console/getters.ts (hideWaitTemperatures
# and hideTlCommands are both true by default in gui/console/index.ts).
MAINSAIL_DEFAULT_FILTERS = [r'^(?:ok\s+)?(B|C|T\d*):',
                            '^_TIMELAPSE_NEW_FRAME', '^TIMELAPSE_TAKE_FRAME', '^TIMELAPSE_RENDER',
                            '^_SET_TIMELAPSE_SETUP', '^HYPERLAPSE ACTION=',
                            '^SET_GCODE_VARIABLE MACRO=TIMELAPSE_']

def mainsail_format(msg):
    msg = re.sub(r'^!! ', '', msg)
    msg = msg.replace('\n!! ', '\n')
    msg = re.sub(r'^// ', '', msg)
    msg = msg.replace('\n// ', '\n')
    msg = re.sub(r'^echo:', '', msg)
    msg = re.sub(r'^debug:', '', msg)
    msg = msg.replace('\n// ', '<br>', 1)
    msg = re.sub(r'\r\n|\r|\n', '<br>', msg)
    # DOMPurify.sanitize would also eat anything that looks like an HTML tag
    return msg.strip()

def mainsail_hidden(msg):
    f = mainsail_format(msg)
    return any(re.search(r, f) for r in MAINSAIL_DEFAULT_FILTERS)


# ---------------------------------------------------------------- scenarios
class Runner:
    def __init__(self, klippy, verbose=False):
        self.klippy = klippy
        self.verbose = verbose
        self.problems = []
        self.count = 0
        self.log = []

    def p(self, s):
        self.log.append(s); print(s)

    def run(self, label, script, expect_error=None, variables=None, press=None, setup=None,
            harness_kw=None, **machine_kw):
        kw = dict(harness_kw or {})
        h = Harness(self.klippy, variables=variables if variables is not None else {},
                    machine_kw=machine_kw, **kw)
        try:
            if setup:
                setup(h)
            if press is not None:
                out, err = h.press(press)
            else:
                out, err = h.run(script)
            self.count += 1
            if h.unused:
                self.problems.append('%s: options Klipper would reject: %s' % (label, h.unused))
            for line in out:
                if mainsail_hidden(line):
                    self.problems.append('%s: Mainsail hides this line by default -> %r' % (label, line))
                if '<' in line and re.search(r'<[a-zA-Z/]', line):
                    self.problems.append('%s: Mainsail DOMPurify would eat part of %r' % (label, line))
            if expect_error:
                if err is None or expect_error.lower() not in err.lower():
                    self.problems.append('%s: expected error %r, got %r' % (label, expect_error, err))
                else:
                    self.p('  ok  %s: refused -> %s' % (label, err.strip().splitlines()[-1][-110:]))
            elif err is not None:
                self.problems.append('%s: unexpected error %s' % (label, err))
            else:
                self.p('  ok  %s: %d console lines, %d gcode lines' % (label, len(out), len(h.lines)))
            if self.verbose or label.startswith('SHOW_TOOLS'):
                for l in out:
                    self.p('        console| ' + l)
            if self.verbose:
                for l in h.lines:
                    self.p('        gcode  | ' + l)
            return h, out
        finally:
            h.close()

    def check(self, cond, label):
        if not cond:
            self.problems.append('assertion failed: ' + label)


def scenarios(r):
    allS = {i: True for i in range(6)}
    calls = lambda h: h.machine.calls

    # --- the reported bug: SHOW_TOOLS with the user's real variables.cfg
    h, out = r.run('SHOW_TOOLS user variables.cfg', 'SHOW_TOOLS',
                   variables=__import__('ast').literal_eval('{%s}' % ','.join(
                       "'%s': %s" % tuple(x.strip() for x in l.split('=', 1))
                       for l in open(DEFAULT_VARS).read().splitlines() if '=' in l)),
                   sensors=allS)
    r.check(len(out) == 7, 'SHOW_TOOLS prints header + 6 tool lines (got %d)' % len(out))
    r.check(sum(not mainsail_hidden(l) for l in out) == 7, 'all 7 SHOW_TOOLS lines visible in Mainsail')
    r.check(any('T1 |' in l and 'LOADED' in l for l in out), 'T1 shows LOADED')
    h, out = r.run('SHOW_TOOLS mixed', 'SHOW_TOOLS', sensors={0: True, 1: False, 2: True}, variables={
        't1_loaded': True, 't2_temp_profile': 'CUSTOM', 't2_custom_load_temp': 225, 't3_material': 'PLA',
        't3_color': 'FF0000', 't4_load_length': 90})
    r.check(any('MISMATCH' in l for l in out), 'SHOW_TOOLS flags T1 mismatch')
    r.check(any('CUSTOM 225C/HIGH' in l for l in out), 'SHOW_TOOLS CUSTOM text')
    r.check(any('NOT FOUND' not in l for l in out), 'sensor text')

    # --- FILAMENT_LOAD / UNLOAD (nested macros run for real)
    h, out = r.run('load T3 docked, LOW default', 'FILAMENT_LOAD TOOL=3', sensors=allS)
    c = h.lines
    r.check('ACTIVATE_EXTRUDER EXTRUDER=extruder3' in c and 'ACTIVATE_EXTRUDER EXTRUDER=extruder' in c, 'load T3 extruder switch')
    r.check('SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=240.0' in c, 'load T3 LOW 240')
    r.check('G1 E60.0 F300.0' in c and 'G1 E25.0 F150' in c, 'load T3 moves')
    r.check(h.variables().get('t3_loaded') is True, 'load T3 saved t3_loaded True (real save_variables)')
    r.check(h.machine.toolhead.status['extruder'] == 'extruder', 'load T3 restored active extruder')
    h, out = r.run('unload T1 HIGH via old t1_temp_type key', 'FILAMENT_UNLOAD TOOL=1', sensors=allS,
                   variables={'t1_loaded': True, 't1_temp_type': 'HIGH'})
    r.check('SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=260.0' in h.lines, 'unload HIGH 260')
    r.check(h.lines.count('G1 E-75.0 F7200.0') == 2 and 'G1 E5.0 F500' in h.lines, 'unload moves')
    r.check(h.variables().get('t1_loaded') is False, 'unload saved False')
    h, out = r.run('unload keeps hotter print target 275', 'FILAMENT_UNLOAD TOOL=0', sensors=allS,
                   targets={0: 275.0}, state='paused')
    r.check(sum('TARGET=275.0' in x for x in h.lines if x.startswith('SET_HEATER')) == 2, 'keeps 275')
    r.run('load refused while printing', 'FILAMENT_LOAD TOOL=2', 'refused while printing', state='printing', sensors=allS)
    r.run('load refused, already loaded', 'FILAMENT_LOAD TOOL=2', 'already marked loaded', sensors=allS, variables={'t2_loaded': True})
    r.run('load FORCE when loaded', 'FILAMENT_LOAD TOOL=2 FORCE=1', sensors=allS, variables={'t2_loaded': True})
    r.run('load refused, sensor empty', 'FILAMENT_LOAD TOOL=4', 'sensor reads empty', sensors={4: False})
    r.run('load unknown tool', 'FILAMENT_LOAD TOOL=9', 'no tool T9')
    r.run('load missing TOOL', 'FILAMENT_LOAD', 'TOOL= is required')
    r.run('load lower-case command + param', 'filament_load tool=1', sensors=allS)
    h, out = r.run('CUSTOM missing -> HIGH warn', 'FILAMENT_LOAD TOOL=0', sensors=allS, variables={'t0_temp_profile': 'CUSTOM'})
    r.check(any('WARNING' in x for x in out) and 'SET_HEATER_TEMPERATURE HEATER=extruder TARGET=260.0' in h.lines, 'CUSTOM warn')
    h, out = r.run('CUSTOM 225 + per-tool motion', 'FILAMENT_LOAD TOOL=5', sensors=allS,
                   variables={'t5_temp_profile': 'CUSTOM', 't5_custom_load_temp': 225, 't5_load_length': 160, 't5_load_speed': 600})
    r.check('SET_HEATER_TEMPERATURE HEATER=extruder5 TARGET=225.0' in h.lines and h.lines.count('G1 E53.333 F600.0') == 3, 'custom motion')
    h, out = r.run('reset motion (None) falls back', 'FILAMENT_LOAD TOOL=5', sensors=allS,
                   variables={'t5_load_length': None, 't5_load_speed': None, 't5_temp_profile': None})
    r.check('G1 E60.0 F300.0' in h.lines, 'None falls back')
    h, out = r.run('2 tools [0,7], no sensors, load T7', 'FILAMENT_LOAD TOOL=7', tool_numbers=(0, 7), sensors={})
    r.check('ACTIVATE_EXTRUDER EXTRUDER=extruder1' in h.lines, 'non-contiguous')
    r.run('motion-sensor kind empty refuses', 'FILAMENT_LOAD TOOL=1', 'sensor reads empty', sensors={1: False},
          sensor_kind='filament_motion_sensor')
    h, out = r.run('unload with max_extrude_only_distance 50', 'FILAMENT_UNLOAD TOOL=0', sensors=allS,
                   max_extrude_only_distance=50.)
    r.check(h.lines.count('G1 E-50.0 F7200.0') == 3, 'split 3x50')
    h, out = r.run('load while T2 active', 'FILAMENT_LOAD TOOL=4', sensors=allS, active=2)
    r.check(h.machine.toolhead.status['extruder'] == 'extruder2', 'active extruder restored to T2')

    # --- nozzle change: FILAMENT_UNLOAD_PARTIAL and the partial state
    P = {'t2_partial': True, 't2_partial_mm': 35.0, 't2_loaded': False}
    def heater_targets(h, ext):
        return [x for x in h.lines if x.startswith('SET_HEATER_TEMPERATURE HEATER=%s ' % ext)]
    def e_total(h):
        tot = 0.0
        for x in h.lines:
            m = re.match(r'G1 E(-?[0-9.]+)', x)
            if m:
                tot += float(m.group(1))
        return round(tot, 3)
    h, out = r.run('partial T2 default 35mm, LOW unload temp, heater off', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', sensors=allS,
                   variables={'t2_loaded': True})
    v = h.variables()
    r.check(heater_targets(h, 'extruder2') == ['SET_HEATER_TEMPERATURE HEATER=extruder2 TARGET=240.0',
                                                'SET_HEATER_TEMPERATURE HEATER=extruder2 TARGET=0'], 'heat 240 then OFF %r' % heater_targets(h, 'extruder2'))
    r.check('G1 E5.0 F500' in h.lines and 'G1 E-35.0 F7200.0' in h.lines and e_total(h) == -30.0, 'tip push 5 + pull 35 %r' % e_total(h))
    r.check(v.get('t2_partial') is True and v.get('t2_partial_mm') == 35.0 and v.get('t2_loaded') is False, 'partial saved %r' % v)
    r.check(h.machine.toolhead.status['extruder'] == 'extruder', 'partial restored active extruder')
    r.check(not any(x.startswith('T2:') for x in out) and any('NOZZLE CHANGE [4/4]' in x for x in out), 'partial console lines')
    h, out = r.run('partial LENGTH=40', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 LENGTH=40', sensors=allS, variables={'t2_loaded': True})
    r.check('G1 E-40.0 F7200.0' in h.lines and h.variables().get('t2_partial_mm') == 40.0, 'LENGTH=40')
    h, out = r.run('partial per-tool PARTIAL_LENGTH 30 + HIGH profile', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', sensors=allS,
                   variables={'t2_loaded': True, 't2_partial_length': 30.0, 't2_temp_profile': 'HIGH'})
    r.check('G1 E-30.0 F7200.0' in h.lines and 'SET_HEATER_TEMPERATURE HEATER=extruder2 TARGET=260.0' in h.lines, 'per-tool 30 at 260')
    h, out = r.run('partial LENGTH= beats per-tool', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 LENGTH=20', sensors=allS,
                   variables={'t2_loaded': True, 't2_partial_length': 30.0})
    r.check('G1 E-20.0 F7200.0' in h.lines, 'LENGTH wins')
    r.run('partial LENGTH=55 rejected (load 60 - margin 10)', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 LENGTH=55', 'rejected', sensors=allS,
          variables={'t2_loaded': True})
    r.run('partial LENGTH=2 rejected', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 LENGTH=2', 'rejected', sensors=allS, variables={'t2_loaded': True})
    h, out = r.run('partial LENGTH=100 ok with load_length 160', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 LENGTH=100', sensors=allS,
                   variables={'t2_loaded': True, 't2_load_length': 160.0})
    r.check(h.lines.count('G1 E-50.0 F7200.0') == 2, 'split 2x50 %r' % [x for x in h.lines if x.startswith('G1')])
    r.run('partial refused while printing', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', 'refused while the printer is printing', state='printing', sensors=allS)
    r.run('partial refused while paused', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', 'refused while the printer is paused', state='paused', sensors=allS)
    r.run('partial refused when already partial', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', 'already partly unloaded', sensors=allS, variables=P)
    r.run('partial refused, sensor empty', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', 'sensor reads empty', sensors={2: False})
    h, out = r.run('partial FORCE=1 with sensor empty', 'FILAMENT_UNLOAD_PARTIAL TOOL=2 FORCE=1', sensors={2: False})
    r.check(h.variables().get('t2_partial') is True, 'force partial')
    r.run('partial refused, no sensor and marked empty', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', 'marked empty', sensors={})
    h, out = r.run('partial no sensor, marked loaded', 'FILAMENT_UNLOAD_PARTIAL TOOL=2', sensors={}, variables={'t2_loaded': True})
    r.check(h.variables().get('t2_partial') is True, 'no-sensor partial')
    r.run('partial unknown tool', 'FILAMENT_UNLOAD_PARTIAL TOOL=9', 'no tool T9')
    r.run('partial missing TOOL', 'FILAMENT_UNLOAD_PARTIAL', 'TOOL= is required')
    h, out = r.run('partial while T2 active restores T2', 'FILAMENT_UNLOAD_PARTIAL TOOL=4', sensors=allS, active=2,
                   variables={'t4_loaded': True})
    r.check(h.machine.toolhead.status['extruder'] == 'extruder2', 'active T2 restored')
    # load after a partial: only the pulled-back length plus the purge
    h, out = r.run('load after partial feeds 35 + purge 25, clears partial', 'FILAMENT_LOAD TOOL=2', sensors=allS, variables=P)
    v = h.variables()
    g = [x for x in h.lines if x.startswith('G1')]
    r.check(g == ['G1 E35.0 F300.0', 'G1 E25.0 F150'], 'partial reload moves %r' % g)
    r.check(v.get('t2_loaded') is True and v.get('t2_partial') is False and v.get('t2_partial_mm') is None, 'reload vars %r' % v)
    r.check(any('nozzle change reload' in x for x in out), 'reload message')
    # full unload after a partial: no tip push, only the rest of unload_length
    h, out = r.run('unload after partial retracts 150 - 35 = 115', 'FILAMENT_UNLOAD TOOL=2', sensors=allS, variables=P)
    g = [x for x in h.lines if x.startswith('G1')]
    r.check(g == ['G1 E-57.5 F7200.0', 'G1 E-57.5 F7200.0'], 'rest of unload %r' % g)
    v = h.variables()
    r.check(v.get('t2_loaded') is False and v.get('t2_partial') is False and v.get('t2_partial_mm') is None, 'unload clears partial %r' % v)
    h, out = r.run('unload after partial, nothing left (unload_length 30)', 'FILAMENT_UNLOAD TOOL=2', sensors=allS,
                   variables=dict(P, t2_unload_length=30.0))
    r.check(not any(x.startswith('G1') for x in h.lines) and any('nothing left' in x for x in out), 'nothing left')
    # whole sequences: the same filament end position as the normal commands
    h, out = r.run('sequence partial -> full unload = normal unload total', 'FILAMENT_UNLOAD_PARTIAL TOOL=1\nFILAMENT_UNLOAD TOOL=1',
                   sensors=allS, variables={'t1_loaded': True})
    r.check(e_total(h) == 5.0 - 150.0, 'partial + unload nets %r, normal unload nets -145' % e_total(h))
    h, out = r.run('sequence partial -> load = back where it was + purge', 'FILAMENT_UNLOAD_PARTIAL TOOL=1\nFILAMENT_LOAD TOOL=1',
                   sensors=allS, variables={'t1_loaded': True})
    r.check(e_total(h) == 5.0 + 25.0 and h.variables().get('t1_loaded') is True and h.variables().get('t1_partial') is False,
            'partial + load nets tip push + purge %r' % e_total(h))
    r.check(heater_targets(h, 'extruder1')[1] == 'SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=0', 'off between')
    h, out = r.run('SET_FILAMENT_LOADED clears partial', 'SET_FILAMENT_LOADED TOOL=2 LOADED=1', variables=P)
    v = h.variables()
    r.check(v.get('t2_loaded') is True and v.get('t2_partial') is False and v.get('t2_partial_mm') is None
            and any('nozzle change state cleared' in x for x in out), 'loaded clears partial %r' % v)
    h, out = r.run('SHOW_TOOLS partial', 'SHOW_TOOLS', sensors=allS, variables=P)
    r.check(any('T2 |' in l and 'PARTIAL nozzle change, 35.0mm out' in l for l in out), 'SHOW_TOOLS partial')
    h, out = r.run('SHOW_TOOLS partial, sensor empty', 'SHOW_TOOLS', sensors={2: False}, variables=P)
    r.check(any('MISMATCH, marked partial' in l for l in out), 'partial mismatch')
    h, out = r.run('SET_FILAMENT_MOTION PARTIAL_LENGTH=30', 'SET_FILAMENT_MOTION TOOL=2 PARTIAL_LENGTH=30')
    r.check(h.variables().get('t2_partial_length') == 30.0 and any('nozzle change 30.0mm' in x for x in out), 'partial length saved')
    h, out = r.run('SET_FILAMENT_MOTION RESET clears PARTIAL_LENGTH', 'SET_FILAMENT_MOTION TOOL=2 RESET=1', variables={'t2_partial_length': 30.0})
    r.check(h.variables().get('t2_partial_length', 'x') is None, 'partial length reset')
    # Orbiter: filament still sits on the sensor, so a press must feed it back, never unload it
    h, out = r.run('button: partial + sensor filament -> LOAD back', None, press=2, sensors=allS, variables=P)
    r.check('FILAMENT_LOAD TOOL=2' in h.lines and 'FILAMENT_UNLOAD TOOL=2' not in h.lines and 'G1 E35.0 F300.0' in h.lines,
            'button reload after nozzle change')
    h, out = r.run('button: partial, no sensor -> LOAD back', None, press=2, sensors={}, variables=P)
    r.check('FILAMENT_LOAD TOOL=2' in h.lines and 'G1 E35.0 F300.0' in h.lines, 'no-sensor reload')
    h, out = r.run('button: partial + sensor empty -> nothing', None, press=2, sensors={2: False}, variables=P)
    r.check(not any(x.startswith('FILAMENT_') for x in h.lines) and any('nozzle change but the sensor reads empty' in x for x in out),
            'partial empty sensor')
    h, out = r.run('button: partial while printing -> ignored', None, press=2, sensors=allS, variables=P, state='printing', active=0)
    r.check(not any(x.startswith('FILAMENT_') for x in h.lines), 'partial printing ignored')

    # --- settings commands
    h, out = r.run('SET_FILAMENT_TEMP HIGH', 'SET_FILAMENT_TEMP TOOL=2 TYPE=high')
    r.check(h.variables().get('t2_temp_profile') == 'HIGH', 'temp HIGH saved')
    h, out = r.run('SET_FILAMENT_TEMP CUSTOM TEMP=225', 'SET_FILAMENT_TEMP TOOL=2 TYPE=CUSTOM TEMP=225')
    r.check(h.variables().get('t2_custom_load_temp') == 225 and h.variables().get('t2_custom_unload_temp') == 225, 'custom saved')
    h, out = r.run('SET_FILAMENT_TEMP CUSTOM load only -> warn', 'SET_FILAMENT_TEMP TOOL=2 TYPE=CUSTOM LOAD_TEMP=230')
    r.check(any('WARNING' in x for x in out), 'warn')
    r.run('SET_FILAMENT_TEMP CUSTOM 300 rejected', 'SET_FILAMENT_TEMP TOOL=2 TYPE=CUSTOM TEMP=300', 'rejected')
    r.run('SET_FILAMENT_TEMP CUSTOM 150 rejected', 'SET_FILAMENT_TEMP TOOL=2 TYPE=CUSTOM UNLOAD_TEMP=150', 'rejected')
    r.run('SET_FILAMENT_TEMP bad type', 'SET_FILAMENT_TEMP TOOL=2 TYPE=MEDIUM', 'must be HIGH')
    h, out = r.run('SET_FILAMENT_MOTION set', 'SET_FILAMENT_MOTION TOOL=1 LOAD_LENGTH=80 UNLOAD_SPEED=6000')
    r.check(h.variables().get('t1_load_length') == 80.0 and h.variables().get('t1_unload_speed') == 6000.0, 'motion saved')
    h, out = r.run('SET_FILAMENT_MOTION reset', 'SET_FILAMENT_MOTION TOOL=1 RESET=1', variables={'t1_load_length': 80.0})
    r.check(h.variables().get('t1_load_length', 'x') is None, 'motion reset to None')
    r.run('SET_FILAMENT_MOTION out of range', 'SET_FILAMENT_MOTION TOOL=1 LOAD_LENGTH=0', 'out of range')
    r.run('SET_FILAMENT_MOTION show', 'SET_FILAMENT_MOTION TOOL=1')
    h, out = r.run('SET_FILAMENT_LOADED', 'SET_FILAMENT_LOADED TOOL=4 LOADED=1')
    r.check(h.variables().get('t4_loaded') is True, 'loaded saved')
    h, out = r.run('SET_TOOL_FILAMENT', 'SET_TOOL_FILAMENT TOOL=3 MATERIAL=petg COLOR=ff8800 SPOOL="Sunlu \'grey\' 1kg" PROFILE=low')
    v = h.variables()
    r.check(v.get('t3_material') == 'PETG' and v.get('t3_color') == 'FF8800' and v.get('t3_temp_profile') == 'LOW', 'tool filament saved %r' % v)
    r.check(v.get('t3_spool') == "Sunlu grey 1kg", 'quotes are stripped from the spool name by design %r' % v.get('t3_spool'))
    h, out = r.run('SET_TOOL_FILAMENT PROFILE=PETG refused, nothing saved', 'SET_TOOL_FILAMENT TOOL=3 MATERIAL=PETG PROFILE=PETG', 'Nothing was saved')
    r.check(h.variables().get('t3_material') is None, 'nothing saved on bad profile')
    h, out = r.run('SET_TOOL_FILAMENT PROFILE=245 -> CUSTOM 245', 'SET_TOOL_FILAMENT TOOL=3 MATERIAL=PETG PROFILE=245')
    r.check(h.variables().get('t3_temp_profile') == 'CUSTOM' and h.variables().get('t3_custom_load_temp') == 245, 'numeric profile %r' % h.variables())
    h, out = r.run('SET_TOOL_FILAMENT material only (Mainsail form, other fields blank)', 'SET_TOOL_FILAMENT TOOL=3 MATERIAL=PETG')
    r.check(h.variables().get('t3_material') == 'PETG', 'material only')
    h, out = r.run('SET_TOOL_FILAMENT spool with double quotes', 'SET_TOOL_FILAMENT TOOL=3 SPOOL=\'Elegoo "matte" black\'')
    r.check(h.variables().get('t3_spool') == 'Elegoo matte black', 'double quotes stripped %r' % h.variables().get('t3_spool'))
    r.run('SET_TOOL_FILAMENT bad colour', 'SET_TOOL_FILAMENT TOOL=3 COLOR=red', 'hex')
    # Klipper's shlex treats # as a comment, so COLOR=#FF0000 arrives as COLOR=''
    r.run('SET_TOOL_FILAMENT COLOR=#FF0000 (Klipper cuts at #)', 'SET_TOOL_FILAMENT TOOL=3 COLOR=#FF0000',
          'without the hash sign')
    # ; inside quotes survives: extended params are shlex-parsed from the raw line
    h, out = r.run('SET_TOOL_FILAMENT SPOOL with ; in quotes', 'SET_TOOL_FILAMENT TOOL=3 SPOOL="a;b"')
    r.check(h.variables().get('t3_spool') == 'a;b', 'quoted ; kept %r' % h.variables().get('t3_spool'))
    h, out = r.run('SET_TOOL_FILAMENT CLEAR', 'SET_TOOL_FILAMENT TOOL=3 CLEAR=1', variables={'t3_material': 'PLA'})
    r.check(h.variables().get('t3_material', 'x') is None, 'clear')

    # --- Orbiter add-on: the button runs press_gcode through the real template path
    h, out = r.run('button: printing active, tangle off (default) -> ignore', None, press=0, state='printing', sensors=allS, active=0)
    r.check('PAUSE' not in h.lines and not any(x.startswith('FILAMENT_') for x in h.lines), 'tangle off ignore')
    h, out = r.run('button: printing active, ORBITER_TANGLE MODE=PAUSE -> PAUSE', None, press=0, state='printing', sensors=allS, active=0,
                   setup=lambda h: h.run('ORBITER_TANGLE MODE=PAUSE'))
    r.check('PAUSE' in h.lines, 'pause')
    h, out = r.run('button: printing active, ORBITER_TANGLE MODE=WARN -> warn only', None, press=0, state='printing', sensors=allS, active=0,
                   setup=lambda h: h.run('ORBITER_TANGLE MODE=warn'))
    r.check('PAUSE' not in h.lines and any('tangle_mode is warn' in x for x in out), 'warn')
    r.run('ORBITER_TANGLE bad mode', 'ORBITER_TANGLE MODE=MAYBE', 'must be PAUSE')
    h, out = r.run('ORBITER_TANGLE no args shows mode', 'ORBITER_TANGLE')
    r.check(any('tangle_mode is off' in x for x in out), 'show mode')
    h, out = r.run('button: printing docked -> ignore', None, press=3, state='printing', sensors=allS, active=0)
    r.check('PAUSE' not in h.lines and not any(x.startswith('FILAMENT_') for x in h.lines), 'docked ignore')
    h, out = r.run('button: paused -> ignore', None, press=0, state='paused', sensors=allS)
    r.check(not any(x.startswith(('FILAMENT_', 'PAUSE')) for x in h.lines), 'paused ignore')
    h, out = r.run('button: loaded -> unload', None, press=2, sensors=allS, variables={'t2_loaded': True})
    r.check('FILAMENT_UNLOAD TOOL=2' in h.lines and h.variables().get('t2_loaded') is False, 'button unload')
    r.check(h.lines[-1].startswith('SET_GCODE_VARIABLE MACRO=_ORBITER_SETTINGS VARIABLE=last_done'), 'press done stamp')
    h, out = r.run('button: empty + filament -> load', None, press=2, sensors=allS)
    r.check('FILAMENT_LOAD TOOL=2' in h.lines and h.variables().get('t2_loaded') is True, 'button load')
    h, out = r.run('button: empty + no filament -> nothing', None, press=2, sensors={2: False})
    r.check(not any(x.startswith('FILAMENT_') for x in h.lines), 'nothing')
    h, out = r.run('button: no sensor -> load', None, press=2, sensors={})
    r.check('FILAMENT_LOAD TOOL=2' in h.lines, 'no sensor load')
    h, out = r.run('button: double press inside lockout', None, press=2, sensors=allS,
                   setup=lambda h: h.run('SET_GCODE_VARIABLE MACRO=_ORBITER_SETTINGS VARIABLE=last_done VALUE=4999.0'))
    r.check(not any(x.startswith('FILAMENT_') for x in h.lines), 'lockout')
    def two_presses(h):
        h.press(2)
        h.lines[:] = []
    h, out = r.run('button: second real press right after a load', None, press=2, sensors=allS, setup=two_presses)
    r.check(not any(x.startswith('FILAMENT_') for x in h.lines), 'second press locked out')
    h, out = r.run('_ORBITER_PRESS_DONE', '_ORBITER_PRESS_DONE')
    r.run('button without core module', None, 'core module', press=0, harness_kw={'include_filament': False})
    r.run('button for a tool that does not exist', None, 'no tool T5', press=5, tool_numbers=(0, 1, 2, 3, 4))
    for m in ('_FILAMENT_SETTINGS', '_ORBITER_SETTINGS'):
        r.run(m + ' (startup notice)', m)

# ------------------------------------------- Mainsail macro forms and prompts
# Port of mainsail src/plugins/helpers.ts getMacroParams() (commit d8688d5).
# JS regex.exec() returns the first match, String.replace(str, '') drops the
# first copy of the matched text, and a later match for the same name
# overwrites the default while keeping the name's first position.
MS_PARAM_RE = re.compile(r"""{%?.*?params\.([A-Za-z_0-9]+)(?:\|(int|string|double))?(?:\|default\('?"?(.*?)"?'?\))?(?:\|(int|string))?.*?%?}""")
MS_PARAM_IN_RE = re.compile(r"""{%?.*?if.*?'([A-Za-z_0-9]+)' (?:not )?in params.*?%?}""")

def mainsail_macro_params(gcode):
    ret = {}
    cur = gcode
    m = MS_PARAM_RE.search(cur)
    while m:
        ret[m.group(1)] = {'type': m.group(2) or m.group(4), 'default': m.group(3)}
        cur = cur.replace(m.group(0), '', 1)
        m = MS_PARAM_RE.search(cur)
    cur = gcode
    m = MS_PARAM_IN_RE.search(cur)
    while m:
        ret.setdefault(m.group(1), {'type': None, 'default': None})
        cur = cur.replace(m.group(0), '', 1)
        m = MS_PARAM_IN_RE.search(cur)
    # MacroButton.vue refreshParams(): names starting with _ are not shown
    return {k: v for k, v in ret.items() if not k.startswith('_')}

def mainsail_form_command(macro, order, values):
    """Port of MacroButton.vue sendWithParams(): empty fields are left out."""
    parts = []
    for name in order:
        val = str(values.get(name, '')).strip()
        if val != '':
            if ' ' in val:
                val = '"%s"' % val
            parts.append('%s=%s' % (name, val))
    return macro + ' ' + ' '.join(parts)

# Port of TheMacroPrompt.vue: promptMessageExp, and the begin..show slice.
MS_PROMPT_RE = re.compile(r'^// action:prompt_(?P<type>[^\s]+) *(?P<msg>.*)$')
VUETIFY_COLORS = ('primary', 'secondary', 'info', 'success', 'warning', 'error')

def mainsail_prompt(lines):
    """Returns the prompt Mainsail would show after these console lines, or None."""
    ev = []
    for l in lines:
        if not l.startswith('// action:prompt_'):
            continue
        m = MS_PROMPT_RE.match(l)
        ev.append((m.group('type') if m else '', (m.group('msg') if m else '').strip()))
    # newest begin..show block, closed if an end comes after it
    begins = [i for i, e in enumerate(ev) if e[0] == 'begin']
    if not begins:
        return None
    b = begins[-1]
    if any(e[0] == 'end' for e in ev[b:]):
        return None
    shows = [i for i, e in enumerate(ev[b:]) if e[0] == 'show']
    if not shows:
        return None
    body = ev[b:b + shows[-1]]
    def button(msg):
        sp = msg.split('|')
        return {'label': sp[0], 'gcode': sp[1] if len(sp) > 1 else sp[0],
                'style': sp[2].strip() if len(sp) > 2 else '', 'parts': len(sp)}
    return {'title': body[0][1],
            'text': [m for t, m in body if t == 'text'],
            'buttons': [button(m) for t, m in body if t == 'button'],
            'footer': [button(m) for t, m in body if t == 'footer_button'],
            'types': [t for t, m in body],
            'groups_ok': [t for t, m in body if t.startswith('button_group')] ==
                         ['button_group_start', 'button_group_end'] * (sum(t == 'button_group_start' for t, m in body))}

def style_ok(s):
    return s == '' or s in VUETIFY_COLORS or re.fullmatch(r'[0-9A-Fa-f]{6}', s) is not None

FORM_EXPECT = {
    'FILAMENT_LOAD': ['TOOL', 'FORCE'],
    'FILAMENT_UNLOAD': ['TOOL'],
    'FILAMENT_UNLOAD_PARTIAL': ['TOOL', 'LENGTH', 'FORCE'],
    'SET_FILAMENT_LOADED': ['TOOL', 'LOADED'],
    'SET_FILAMENT_TEMP': ['TOOL', 'TYPE', 'TEMP', 'LOAD_TEMP', 'UNLOAD_TEMP'],
    'SET_FILAMENT_MOTION': ['TOOL', 'LOAD_LENGTH', 'LOAD_SPEED', 'UNLOAD_LENGTH', 'UNLOAD_SPEED', 'PARTIAL_LENGTH', 'RESET'],
    'SET_TOOL_FILAMENT': ['TOOL', 'MATERIAL', 'COLOR', 'SPOOL', 'PROFILE', 'CLEAR'],
    'SPOOL_SETUP': ['TOOL'],
    'SHOW_TOOLS': [],
    'ORBITER_TANGLE': ['MODE'],
    # usage add-on (rev 2)
    'KTC_TOOL_SYNC': ['FLUSH'],
    'KTC_USAGE_MODE': ['MODE'],
    'SET_TOOL_SPOOL': ['TOOL', 'ID'],
    'RESET_TOOL_USAGE': ['TOOL', 'QUIET'],
    'SHOW_TOOL_USAGE': [],
}

def mainsail_ui(r):
    allS = {i: True for i in range(6)}
    # --- 1. macro forms: what Mainsail shows for each user-facing macro, using the
    # gcode text exactly as Klipper's config reader hands it to configfile.settings
    h = Harness(r.klippy, variables={}, machine_kw={'sensors': allS})
    try:
        user = []
        for sec in h.fileconfig.sections():
            if sec.split()[0] != 'gcode_macro':
                continue
            name = sec.split(None, 1)[1]
            if name.startswith('_'):
                continue
            user.append(name)
            ps = mainsail_macro_params(h.fileconfig.get(sec, 'gcode'))
            want = FORM_EXPECT.get(name)
            if want is None:
                r.problems.append('Mainsail form: no expected fields listed for %s' % name)
                continue
            if list(ps) != want:
                r.problems.append('Mainsail form %s: fields %s, expected %s' % (name, list(ps), want))
            for p, d in ps.items():
                if not d['default']:
                    r.problems.append('Mainsail form %s: field %s has no hint' % (name, p))
            if 'TOOL' in ps and list(ps)[0] != 'TOOL':
                r.problems.append('Mainsail form %s: TOOL is not the first field' % name)
            r.p('  ok  Mainsail form %s: %s' % (name, ', '.join('%s [%s]' % (k, v['default']) for k, v in ps.items()) or 'no fields'))
        r.check(sorted(user) == sorted(FORM_EXPECT), 'user-facing macros %s' % sorted(user))
        # --- 2. HELP lists every user-facing macro with options and an example
        out, err = h.run('HELP')
        r.check(err is None, 'HELP ran')
        for name in user:
            line = [l for l in out if re.sub(r'^// ', '', l).split(':', 1)[0].strip().upper() == name]
            if not line:
                r.problems.append('HELP: %s missing' % name)
            elif 'Example: ' + name not in line[0]:
                r.problems.append('HELP: %s has no example -> %r' % (name, line[0]))
        r.p('  ok  HELP lists %d ktc-filament macros with examples' % len(user))
    finally:
        h.close()

    # --- 3. a blank Mainsail field is not sent, so the hint is never used as a value
    order = FORM_EXPECT['SET_TOOL_FILAMENT']
    cmd = mainsail_form_command('SET_TOOL_FILAMENT', order, {'TOOL': '3', 'MATERIAL': 'PETG', 'COLOR': ' ', 'SPOOL': ''})
    r.check(cmd == 'SET_TOOL_FILAMENT TOOL=3 MATERIAL=PETG', 'form command %r' % cmd)
    h, out = r.run('Mainsail form SET_TOOL_FILAMENT, only TOOL + MATERIAL filled', cmd)
    v = h.variables()
    r.check(v.get('t3_material') == 'PETG' and 't3_color' not in v and 't3_spool' not in v and 't3_temp_profile' not in v,
            'blank form fields saved nothing %r' % v)
    cmd = mainsail_form_command('SET_FILAMENT_TEMP', FORM_EXPECT['SET_FILAMENT_TEMP'], {'TOOL': '2', 'TYPE': 'LOW'})
    h, out = r.run('Mainsail form SET_FILAMENT_TEMP, TOOL + TYPE only', cmd)
    r.check(h.variables().get('t2_temp_profile') == 'LOW' and 't2_custom_load_temp' not in h.variables(), 'temp form')
    cmd = mainsail_form_command('FILAMENT_LOAD', FORM_EXPECT['FILAMENT_LOAD'], {'TOOL': '2'})
    r.run('Mainsail form FILAMENT_LOAD, FORCE blank still checks loaded', cmd, 'already marked loaded',
          sensors=allS, variables={'t2_loaded': True})
    cmd = mainsail_form_command('SET_FILAMENT_MOTION', FORM_EXPECT['SET_FILAMENT_MOTION'], {'TOOL': '1'})
    h, out = r.run('Mainsail form SET_FILAMENT_MOTION, TOOL only shows values', cmd)
    r.check(not any(k.startswith('t1_') for k in h.variables()), 'motion form saved nothing')
    r.run('Mainsail form ORBITER_TANGLE, MODE blank shows mode', mainsail_form_command('ORBITER_TANGLE', ['MODE'], {}))

    # --- 4. SPOOL_SETUP pop-up, clicked through with one live printer
    def check_prompt(label, out, title_has):
        pr = mainsail_prompt(out)
        if pr is None:
            r.problems.append('%s: Mainsail shows no prompt' % label)
            return None
        if title_has not in pr['title']:
            r.problems.append('%s: title %r lacks %r' % (label, pr['title'], title_has))
        if not pr['groups_ok']:
            r.problems.append('%s: unbalanced button groups %s' % (label, pr['types']))
        for b in pr['buttons'] + pr['footer']:
            if b['parts'] != 3 or not b['label'].strip() or not style_ok(b['style']):
                r.problems.append('%s: bad button %r' % (label, b))
        for b in pr['footer']:
            if b['style'] not in VUETIFY_COLORS:
                r.problems.append('%s: footer style %r is not a theme colour' % (label, b))
        for l in out:
            if mainsail_hidden(l):
                r.problems.append('%s: Mainsail hides %r' % (label, l))
        r.p('  ok  %s: "%s", %d buttons, footer %s' % (label, pr['title'], len(pr['buttons']),
                                                      '/'.join(b['label'] for b in pr['footer'])))
        if r.verbose:
            for t in pr['text']:
                r.p('        text   | ' + t)
            for b in pr['buttons'] + pr['footer']:
                r.p('        button | %s -> %s [%s]' % (b['label'], b['gcode'], b['style']))
        return pr

    def click(h, pr, label, where='buttons'):
        b = [x for x in pr[where] if x['label'].split(' (')[0].strip() == label or x['label'].startswith(label + ' ')]
        if not b:
            r.problems.append('SPOOL_SETUP: no button %r in %s' % (label, [x['label'] for x in pr[where]]))
            return [], 'no button'
        out, err = h.run(b[0]['gcode'])
        if err:
            r.problems.append('SPOOL_SETUP click %r: error %s' % (label, err))
        return out, err

    h = Harness(r.klippy, variables={'t1_color': '00AA00'}, machine_kw={'sensors': allS})
    try:
        out, err = h.run('SPOOL_SETUP')
        r.check(err is None, 'SPOOL_SETUP ran %r' % err)
        pr = check_prompt('SPOOL_SETUP step 1 tool', out, 'step 1 of 4')
        r.check(pr and [b['label'].split()[0] for b in pr['buttons']] == ['T%d' % i for i in range(6)], 'one button per tool')
        r.check(pr and pr['buttons'][1]['style'] == '00AA00', 'T1 button painted in its saved colour')
        out, err = click(h, pr, 'T1')
        pr = check_prompt('SPOOL_SETUP click T1', out, 'step 2 of 4')
        r.check(pr and [b['label'] for b in pr['buttons']][:7] == ['PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PC', 'Nylon'], 'materials')
        out, err = click(h, pr, 'PETG')
        r.check(h.variables().get('t1_material') == 'PETG', 'PETG click saved %r' % h.variables())
        pr = check_prompt('SPOOL_SETUP click PETG', out, 'step 3 of 4')
        names = ['Black', 'White', 'Gray', 'Red', 'Orange', 'Yellow', 'Green', 'Blue', 'Purple', 'Pink', 'Brown', 'Natural', 'Skip']
        r.check(pr and [b['label'] for b in pr['buttons']] == names, 'colour buttons %r' % (pr and [b['label'] for b in pr['buttons']]))
        out, err = click(h, pr, 'Orange')
        r.check(h.variables().get('t1_color') == 'FF8800', 'Orange click saved FF8800')
        pr = check_prompt('SPOOL_SETUP click Orange', out, 'step 4 of 4')
        r.check(pr and any('SET_FILAMENT_TEMP TOOL=1 TYPE=CUSTOM TEMP=240' in t for t in pr['text']), 'CUSTOM help text')
        r.check(pr and pr['buttons'][1]['label'] == 'LOW (suggested)', 'PETG suggests LOW')
        out, err = click(h, pr, 'HIGH')
        r.check(h.variables().get('t1_temp_profile') == 'HIGH', 'HIGH click saved')
        pr = check_prompt('SPOOL_SETUP click HIGH', out, 'done')
        r.check(pr and any('material PETG, colour FF8800, profile HIGH' in t for t in pr['text']), 'summary %r' % (pr and pr['text']))
        out, err = click(h, pr, 'Done', 'footer')
        r.check(mainsail_prompt(out) is None and '// action:prompt_end' in out, 'Done closes the prompt')
        # Back, Skip, Cancel, CUSTOM
        out, err = h.run('SPOOL_SETUP TOOL=4')
        pr = check_prompt('SPOOL_SETUP TOOL=4 skips tool step', out, 'T4, step 2 of 4')
        out, err = click(h, pr, 'Back', 'footer')
        pr = check_prompt('SPOOL_SETUP Back', out, 'step 1 of 4')
        out, err = click(h, pr, 'T4')
        pr = check_prompt('SPOOL_SETUP T4 again', out, 'step 2 of 4')
        out, err = click(h, pr, 'Other or skip')
        pr = check_prompt('SPOOL_SETUP material skipped', out, 'step 3 of 4')
        out, err = click(h, pr, 'Skip')
        pr = check_prompt('SPOOL_SETUP colour skipped', out, 'step 4 of 4')
        r.check(not any(k.startswith('t4_') for k in h.variables()), 'skips saved nothing')
        out, err = click(h, pr, 'CUSTOM')
        pr = check_prompt('SPOOL_SETUP click CUSTOM', out, 'done')
        r.check(h.variables().get('t4_temp_profile') == 'CUSTOM', 'CUSTOM saved')
        r.check(pr and any('custom temp is missing' in t for t in pr['text']), 'CUSTOM without temps warns')
        out, err = h.run('SPOOL_SETUP TOOL=0')
        pr = mainsail_prompt(out)
        out, err = click(h, pr, 'Cancel', 'footer')
        r.check(mainsail_prompt(out) is None, 'Cancel closes the prompt')
        out, err = h.run('SPOOL_SETUP TOOL=9')
        r.check(err is not None and 'no tool T9' in err, 'SPOOL_SETUP TOOL=9 refused %r' % err)
        r.count += 1
    finally:
        h.close()
    h, out = r.run('SPOOL_SETUP ABS suggests HIGH', 'SPOOL_SETUP TOOL=2', variables={'t2_material': 'ABS'})
    h, out = r.run('SPOOL_SETUP profile step for ABS', '_SPOOL_SETUP_SHOW STEP=PROFILE TOOL=2', variables={'t2_material': 'ABS'})
    pr = mainsail_prompt(out)
    r.check(pr and pr['buttons'][0]['label'] == 'HIGH (suggested)' and pr['buttons'][0]['style'] == 'primary', 'ABS suggests HIGH')
    h, out = r.run('SPOOL_SETUP on 2 tools [0, 7]', 'SPOOL_SETUP', tool_numbers=(0, 7))
    pr = mainsail_prompt(out)
    r.check(pr and [b['gcode'] for b in pr['buttons']] == ['_SPOOL_SETUP_SHOW STEP=MATERIAL TOOL=0', '_SPOOL_SETUP_SHOW STEP=MATERIAL TOOL=7'], 'non-contiguous tool buttons')



def usage_scenarios(r):
    """usage/usage_addon.cfg: the watcher loop, per-tool counting, Spoolman sync."""
    def mk(variables=None, **mkw):
        mkw.setdefault('active', 0)
        return Harness(r.klippy, variables=variables or {}, machine_kw=mkw)
    def problems(label, h, out):
        r.count += 1
        if h.unused:
            r.problems.append('%s: options Klipper would reject: %s' % (label, h.unused))
        for line in out:
            if mainsail_hidden(line):
                r.problems.append('%s: Mainsail hides this line by default -> %r' % (label, line))
    def run(h, label, script, expect_error=None):
        out, err = h.run(script)
        problems(label, h, out)
        if expect_error:
            r.check(err is not None and expect_error.lower() in err.lower(), '%s: expected %r got %r' % (label, expect_error, err))
        else:
            r.check(err is None, '%s: unexpected error %r' % (label, err))
        r.p('  ok  usage: %s' % label)
        return out
    def tick(h, label):
        out, armed = h.tick()
        problems(label, h, out)
        r.check(armed, '%s: watcher re-armed' % label)
        return out
    def pos(h, e): h.machine.toolhead.status['position'] = [0, 0, 0, e]
    def tool(h, n):
        h.machine.set_active(n)
        h.machine.toolhead.status['extruder'] = h.machine.ext_of[n]

    # 1. local mode: counts only while printing, high watermark, flush on change / idle
    h = mk(state='printing')
    try:
        tick(h, 'first tick learns the extruder')
        pos(h, 100.0); tick(h, 'printing 100mm')
        v = h.variables()
        r.check(v.get('t0_used_mm') is None, 'not flushed below save_every_mm while printing')
        pos(h, 90.0); tick(h, 'retract')
        pos(h, 150.0); tick(h, 'printing to 150')
        tool(h, 1); pos(h, 150.0); tick(h, 'tool change to T1 flushes T0')
        v = h.variables()
        r.check(abs(v.get('t0_used_mm', 0) - 150.0) < 0.01, 'T0 used 150 (retract not double counted), got %r' % v.get('t0_used_mm'))
        r.check(v.get('t0_loaded') is True, 'printing from T0 marked it loaded')
        pos(h, 1300.0); tick(h, 'T1 1150mm > save_every_mm')
        r.check(abs(h.variables().get('t1_used_mm', 0) - 1150.0) < 0.01, 'T1 flushed at save_every_mm')
        pos(h, 1310.0); tick(h, 'T1 +10')
        h.machine.print_stats.status['state'] = 'complete'; tick(h, 'print ended flushes')
        r.check(abs(h.variables().get('t1_used_mm', 0) - 1160.0) < 0.01, 'T1 1160 after print end')
        pos(h, 1400.0); tick(h, 'idle extrusion not counted')
        r.check(abs(h.variables().get('t1_used_mm', 0) - 1160.0) < 0.01, 'idle not counted')
        out = run(h, 'SHOW_TOOL_USAGE', 'SHOW_TOOL_USAGE')
        r.check(any('Tool T1 | 1.16 m used' in x for x in out), 'SHOW_TOOL_USAGE lists T1 %r' % out)
        run(h, 'RESET_TOOL_USAGE TOOL=1', 'RESET_TOOL_USAGE TOOL=1')
        r.check(h.variables().get('t1_used_mm') == 0.0, 'reset')
        run(h, 'RESET_TOOL_USAGE TOOL=9', 'RESET_TOOL_USAGE TOOL=9', 'no tool T9')
        r.check(not h.remote_calls, 'local mode never calls Spoolman')
        r.check(not any(x.startswith('_KTC_SPOOLMAN_SEND') for x in h.lines), 'local mode no send')
    finally:
        h.close()

    # 2. spoolman mode: active spool follows the tool, None only after a real id
    h = mk(variables={'t0_spool_id': 3, 't2_spool_id': 7}, state='printing')
    try:
        run(h, 'KTC_USAGE_MODE MODE=SPOOLMAN', 'KTC_USAGE_MODE MODE=SPOOLMAN')
        tick(h, 'spoolman first tick')
        r.check(h.remote_calls == [('spoolman_set_active_spool', {'spool_id': 3})], 'T0 -> spool 3 %r' % h.remote_calls)
        tick(h, 'no repeat')
        r.check(len(h.remote_calls) == 1, 'not re-sent every tick')
        tool(h, 1); tick(h, 'T1 has no spool')
        r.check(h.remote_calls[-1] == ('spoolman_set_active_spool', {'spool_id': None}), 'T1 -> None')
        tool(h, 2); tick(h, 'T2 spool 7')
        r.check(h.remote_calls[-1] == ('spoolman_set_active_spool', {'spool_id': 7}), 'T2 -> 7')
        run(h, 'SET_TOOL_SPOOL TOOL=2 ID=8 on the active tool', 'SET_TOOL_SPOOL TOOL=2 ID=8')
        r.check(h.remote_calls[-1] == ('spoolman_set_active_spool', {'spool_id': 8}), 'active tool switched to 8 at once')
        r.check(h.variables().get('t2_spool_id') == 8 and h.variables().get('t2_used_mm') == 0.0, 'id saved, counter reset')
        run(h, 'SET_TOOL_SPOOL TOOL=4 ID=0 clears', 'SET_TOOL_SPOOL TOOL=4 ID=0')
        r.check(h.variables().get('t4_spool_id', 'x') is None, 'cleared to None')
        run(h, 'SET_TOOL_SPOOL missing ID', 'SET_TOOL_SPOOL TOOL=4', 'use SET_TOOL_SPOOL')
        run(h, 'KTC_USAGE_MODE bad', 'KTC_USAGE_MODE MODE=CLOUD', 'must be LOCAL or SPOOLMAN')
        run(h, 'KTC_USAGE_MODE show', 'KTC_USAGE_MODE')
    finally:
        h.close()

    # 3. spoolman mode while Moonraker has no [spoolman]: logged by Klipper, nothing aborts
    h = Harness(r.klippy, variables={'ktc_usage_mode': 'spoolman', 't0_spool_id': 3},
                machine_kw={'active': 0, 'state': 'printing'}, remote_methods=())
    try:
        tick(h, 'remote method missing does not stop the watcher')
        out = run(h, 'KTC_TOOL_SYNC from a T# macro with no Spoolman', 'SET_TOOL_SPOOL TOOL=0 ID=4')
    finally:
        h.close()

    # 4. toolchanger busy: the watcher skips, still re-arms
    h = mk(state='printing')
    try:
        h.machine.tc.status['status'] = 'changing'
        out, armed = h.tick()
        r.check(armed and not any(l.startswith('KTC_TOOL_SYNC') for l in h.lines), 'skips while changing')
        r.count += 1
    finally:
        h.close()

    # 5. SET_TOOL_FILAMENT with a new spool name resets the counter
    h = mk(variables={'t1_used_mm': 500.0, 't1_spool': 'Old'})
    try:
        run(h, 'SET_TOOL_FILAMENT new SPOOL resets usage', 'SET_TOOL_FILAMENT TOOL=1 SPOOL="New roll"')
        r.check(h.variables().get('t1_used_mm') == 0.0, 'new spool name resets t1_used_mm')
        run(h, 'SET_TOOL_FILAMENT same SPOOL keeps usage', 'SAVE_VARIABLE VARIABLE=t1_used_mm VALUE=12.0\nSET_TOOL_FILAMENT TOOL=1 SPOOL="New roll"')
        r.check(h.variables().get('t1_used_mm') == 12.0, 'same spool name keeps the counter')
    finally:
        h.close()
    r.run('_USAGE_SETTINGS (notice)', '_USAGE_SETTINGS')
    r.run('usage add-on absent: filament still works', 'SET_FILAMENT_LOADED TOOL=1 LOADED=1', harness_kw={'include_usage': False})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--klipper')
    ap.add_argument('--show', action='store_true', help='print every console and gcode line')
    ap.add_argument('--out', help='also write the report to this file')
    a = ap.parse_args()
    klippy = find_klipper(a.klipper)
    r = Runner(klippy, verbose=a.show)
    rev = os.popen('git -C "%s" log -1 --format="%%h %%ci" 2>/dev/null' % klippy).read().strip()
    r.p('Real Klipper: %s %s' % (klippy, rev))
    scenarios(r)
    usage_scenarios(r)
    mainsail_ui(r)
    r.p('')
    r.p('%d scenarios run through real Klipper code' % r.count)
    if r.problems:
        r.p('PROBLEMS:')
        for x in r.problems:
            r.p(' - ' + x)
    else:
        r.p('ALL CHECKS PASSED')
    if a.out:
        open(a.out, 'w').write('\n'.join(r.log) + '\n')
    sys.exit(1 if r.problems else 0)


if __name__ == '__main__':
    main()
