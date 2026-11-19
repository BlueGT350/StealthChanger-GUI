#!/usr/bin/env python3
"""Firmware helper tests. No real flashtool, make, or printer.

  python3 tests/test_firmware.py

Covers print refusal, Katapult CAN, Katapult USB (bootloader request),
direct STM32 DFU and RP2040 boot, and rejection of arbitrary commands.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'firmware'))
import firmware_helper as fw  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ok   ', name)
    else:
        FAIL += 1
        print('  FAIL ', name, '' if detail is None else detail)


HOST = 'v0.13.0-100-gabc1234'
OLD = 'v0.13.0-90-gf00d111'
USB_MAIN = '/dev/serial/by-id/usb-Klipper_stm32f446xx_AAAA1111BBBB2222-if00'
USB_EBB = '/dev/serial/by-id/usb-Klipper_rp2040_CCCC3333DDDD4444-if00'
USB_EVIL = '/dev/serial/by-id/usb-Klipper_stm32_$(touch-FLAG)-if00'
CAN_UUID = 'a1b2c3d4e5f6'

STM32_CONFIG = """#
# Klipper menuconfig for tests
#
CONFIG_LOW_LEVEL_OPTIONS=y
CONFIG_MCU="stm32f446xx"
"""
RP2040_CONFIG = 'CONFIG_MCU="rp2040"\nCONFIG_MACH_RP2040=y\n'
UNKNOWN_CONFIG = 'CONFIG_MCU="samd21"\n'
SHELL_CONFIG = STM32_CONFIG + '$(touch FLAG)\n`touch FLAG`\n; rm -rf /\n'


class MoonState(object):
    def __init__(self):
        self.states = ['standby']
        self.versions = {}
        self.host = HOST
        self.klippy = 'ready'
        self.constants = {}
        self.lock = threading.Lock()

    def next_state(self):
        with self.lock:
            if not self.states:
                return 'standby'
            if len(self.states) == 1:
                return self.states[0]
            return self.states.pop(0)


class MoonHandler(fw.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, payload, status=200):
        raw = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        st = self.server.state
        path = self.path
        if path.startswith('/printer/objects/query') and 'print_stats' in path:
            self._send({'result': {'status': {'print_stats': {'state': st.next_state()}}}})
            return
        if path.startswith('/printer/info'):
            self._send({'result': {'state': st.klippy, 'software_version': st.host}})
            return
        if path.startswith('/printer/objects/list'):
            objs = ['mcu' if n == 'mcu' else 'mcu ' + n for n in st.versions]
            self._send({'result': {'objects': objs}})
            return
        if path.startswith('/printer/objects/query'):
            status = {}
            for name, ver in list(st.versions.items()):
                key = 'mcu' if name == 'mcu' else 'mcu ' + name
                entry = {'mcu_version': ver}
                consts = (st.constants or {}).get(name)
                if consts:
                    entry['mcu_constants'] = consts
                status[key] = entry
            self._send({'result': {'status': status}})
            return
        self._send({'error': 'not found'}, 404)


class Runner(object):
    def __init__(self, serial_dir, state):
        self.calls = []
        self.serial_dir = serial_dir
        self.state = state
        self.make_bin = 'make'
        self.fail_when = None
        self.build_listings = []

    def run(self, argv, on_line=None):
        if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
            raise AssertionError('argv must be a list of strings: %r' % (argv,))
        self.calls.append(list(argv))
        if self.fail_when and self.fail_when(argv):
            if on_line:
                on_line('mock failed')
            return 1, 'fail'
        if on_line:
            on_line('mock ' + os.path.basename(argv[0]))
        if '-r' in argv and '-d' in argv:
            device = argv[argv.index('-d') + 1]
            base = os.path.basename(device)
            src = device if os.path.exists(device) else os.path.join(self.serial_dir, base)
            if os.path.exists(src):
                renamed = base.replace('Klipper', 'katapult').replace('klipper', 'katapult')
                os.rename(src, os.path.join(self.serial_dir, renamed))
        if argv and argv[0] == self.make_bin and 'flash' not in argv:
            out = None
            for arg in argv:
                if arg.startswith('OUT='):
                    out = arg.split('=', 1)[1]
            if out:
                listing = sorted(os.listdir(out)) if os.path.isdir(out) else None
                self.build_listings.append((out, listing))
                os.makedirs(out, exist_ok=True)
                with open(os.path.join(out, 'klipper.bin'), 'wb') as fh:
                    fh.write(b'BIN')
        if len(argv) >= 2 and argv[-2] == 'start' and argv[-1] == 'klipper':
            # The new firmware reports the host version.
            for key in list(self.state.versions):
                self.state.versions[key] = self.state.host
        return 0, 'ok'


def start_server(handler, state=None):
    httpd = fw.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    if state is not None:
        httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_json(port, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'http://127.0.0.1:%d%s' % (port, path), data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode('utf-8')
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8')


class World(object):
    def __init__(self, state_queue=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.config = os.path.join(self.root, 'config')
        self.fwdir = os.path.join(self.config, 'stealthchanger-firmware')
        self.build = os.path.join(self.root, 'stealthchanger-firmware-build')
        self.klipper = os.path.join(self.root, 'klipper')
        self.serial = os.path.join(self.root, 'by-id')
        os.makedirs(self.config)
        os.makedirs(self.klipper)
        os.makedirs(self.serial)
        with open(os.path.join(self.klipper, 'Makefile'), 'w', encoding='utf-8') as fh:
            fh.write('all:\n')
        self.flashtool = os.path.join(self.root, 'flashtool.py')
        with open(self.flashtool, 'w', encoding='utf-8') as fh:
            fh.write('# mock flashtool\n')
        boards = os.path.join(self.config, 'boards')
        extra = os.path.join(self.config, 'extra')
        os.makedirs(boards)
        os.makedirs(extra)
        with open(os.path.join(boards, 'toolhead.cfg'), 'w', encoding='utf-8') as fh:
            fh.write('[mcu toolhead]\ncanbus_uuid: %s\n' % CAN_UUID)
        with open(os.path.join(extra, 'ebb.cfg'), 'w', encoding='utf-8') as fh:
            fh.write('[mcu ebb]\nserial: %s\n' % USB_EBB)
        outside = os.path.join(self.root, 'outside.cfg')
        with open(outside, 'w', encoding='utf-8') as fh:
            fh.write('[mcu stolen]\nserial: /dev/ttyACM0\n')
        printer = os.path.join(self.config, 'printer.cfg')
        with open(printer, 'w', encoding='utf-8') as fh:
            fh.write(
                '[include boards/toolhead.cfg]\n'
                '[include extra/*.cfg]\n'
                '[include ../outside.cfg]\n'
                '[mcu]\n'
                'serial: %s\n'
                '[mcu host]\n'
                'serial: /tmp/klipper_host_mcu\n'
                '[mcu evil]\n'
                'serial: %s\n' % (USB_MAIN, USB_EVIL))
        self.printer = printer
        self.state = MoonState()
        if state_queue:
            self.state.states = list(state_queue)
        self.state.versions = {'mcu': OLD, 'toolhead': OLD, 'ebb': OLD}
        self.moon = start_server(MoonHandler, self.state)
        self.runner = Runner(self.serial, self.state)
        self.svc = fw.FirmwareService(
            runner=self.runner,
            moonraker='http://127.0.0.1:%d' % self.moon.server_address[1],
            config_dir=self.fwdir,
            build_dir=self.build,
            printer_cfg=self.printer,
            klipper_dir=self.klipper,
            flashtool=self.flashtool,
            serial_by_id=self.serial,
            sudo='sudo',
            systemctl='systemctl',
            python_bin=sys.executable,
            make_bin='make',
            verify_seconds=2,
            verify_pause=0.02)

    def close(self):
        self.moon.shutdown()
        self.tmp.cleanup()

    def plant(self, serial):
        path = os.path.join(self.serial, os.path.basename(serial))
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('device')
        return path

    def save(self, name, text, method=None):
        body = self.svc.save_config({'mcu': name, 'config': text})
        if method:
            self.svc.set_method({'mcu': name, 'method': method})
        return body

    def logs_of(self, name):
        logs = []
        self.svc.flash({'mcu': name, 'confirm': True}, logs.append)
        return logs


def argv_ok(calls, flashtool):
    for argv in calls:
        exe = argv[0]
        if exe == 'make':
            if argv[1] != '-C':
                return False
        elif exe == sys.executable:
            if len(argv) < 2 or argv[1] != flashtool:
                return False
            flags = [a for a in argv[2:] if a.startswith('-')]
            if any(a not in ('-i', '-u', '-d', '-f', '-r') for a in flags):
                return False
        elif exe == 'sudo':
            if argv[1:] not in (
                    ['-n', 'systemctl', 'stop', 'klipper'],
                    ['-n', 'systemctl', 'start', 'klipper'],
                    ['-n', 'systemctl', 'is-active', 'klipper']):
                return False
        else:
            return False
        if any(tok in a for a in argv for tok in ('$(', '`', ';', '|', '\n', 'touch FLAG', 'rm -rf')):
            return False
    return True


def test_parse_and_refuse_escape():
    print('config parse')
    w = World()
    try:
        sections, warnings = fw.parse_mcu_sections(w.config, w.printer)
        names = [s['name'] for s in sections]
        check('mcu, included toolhead, wildcard ebb, host, evil, outside file',
              set(names) == {'toolhead', 'ebb', 'mcu', 'host', 'evil', 'stolen'}, names)
        check('includes are read even outside the config dir, with no refusal warning',
              not any('refusing' in x or 'leaves the config' in x for x in warnings), warnings)
        check('version relation',
              fw.version_relation(HOST, OLD) == 'behind'
              and fw.version_relation(OLD, HOST) == 'ahead'
              and fw.version_relation(HOST, HOST + '-dirty') == 'current'
              and fw.version_relation(HOST, None) == 'unknown'
              and fw.version_relation('not-a-version', 'other') == 'behind')
        check('friendly labels',
              fw.friendly_label('mcu', [0]) == 'Main Board'
              and fw.friendly_label('EBB0', [0]) == 'T0'
              and fw.friendly_label('EBB0', [1, 0]) == 'T0, T1'
              and fw.friendly_label('toolhead', []) == 'toolhead')
        by = {s['name']: s['options'] for s in sections}
        check('can uuid kept, chip not invented', by['toolhead'].get('canbus_uuid') == CAN_UUID and 'mcu_type' not in by['toolhead'])
        check('CONFIG_MCU quoted', fw.config_mcu_type(STM32_CONFIG) == 'stm32f446xx')
        check('missing CONFIG_MCU is none', fw.config_mcu_type('CONFIG_FOO=y\n') is None)
        check('direct kinds', fw.direct_kind('stm32f446xx')[0] == 'dfu' and fw.direct_kind('rp2040')[0] == 'rp2040')
        check('unknown chip is not a direct kind', fw.direct_kind('samd21')[0] is None)
    finally:
        w.close()


def test_status_and_guess_refusal():
    print('status')
    w = World()
    try:
        desc = w.svc.describe()
        by = {m['name']: m for m in desc['mcus']}
        check('host version and mismatch',
              desc['host_version'] == HOST and by['mcu']['version'] == OLD and by['mcu']['version_mismatch'] is True
              and by['mcu']['update_state'] == 'behind' and by['mcu']['label'] == 'Main Board')
        check('helper reports its own version', desc['version'] == fw.HELPER_VERSION == '0.2.12')
        check('can method is Katapult CAN, interface defaults to can0',
              by['toolhead']['method'] == 'katapult_can' and by['toolhead']['canbus_interface'] == 'can0'
              and by['toolhead']['canbus_interface_default'] is True
              and by['toolhead']['method_choices'] == ['katapult_can'])
        check('serial by-id defaults to Katapult USB when Katapult is installed',
              by['mcu']['method'] == 'katapult_usb' and by['ebb']['method'] == 'katapult_usb'
              and by['mcu']['method_choices'] == ['katapult_usb', 'direct'])
        w.svc.flashtool = None
        bare = {m['name']: m for m in w.svc.describe()['mcus']}
        check('serial by-id defaults to direct when Katapult is not installed',
              bare['mcu']['method'] == 'direct' and bare['ebb']['method'] == 'direct')
        check('canbus_uuid still defaults to Katapult CAN without the flashtool',
              bare['toolhead']['method'] == 'katapult_can')
        w.svc.flashtool = w.flashtool
        check('no .config means not flashable, and the text says it will not guess',
              by['mcu']['flashable'] is False and 'will not guess' in (by['mcu']['block_reason'] or '').lower())
        check('host mcu is not flashable', by['host']['connection'] == 'host' and by['host']['flashable'] is False)
        check('shell-looking serial is not flashable', by['evil']['flashable'] is False and 'serial path' in by['evil']['block_reason'])
        w.save('mcu', UNKNOWN_CONFIG, 'direct')
        reason = {m['name']: m for m in w.svc.describe()['mcus']}['mcu']['block_reason']
        check('unknown CONFIG_MCU is refused for direct', 'samd21' in reason and 'will not guess' in reason.lower())
        w.save('ebb', RP2040_CONFIG)  # method is the by-id default, not a saved choice
        ebb = {m['name']: m for m in w.svc.describe()['mcus']}['ebb']
        check('by-id board with a .config uses the default method',
              ebb['method'] == 'katapult_usb' and ebb['flashable'] is True and ebb['update_state'] == 'behind', ebb)
    finally:
        w.close()


def test_print_refusal():
    print('print refusal')
    for state in ('printing', 'paused'):
        w = World([state])
        try:
            w.save('toolhead', STM32_CONFIG)
            w.runner.calls.clear()
            logs = []
            try:
                w.svc.flash({'mcu': 'toolhead', 'confirm': True}, logs.append)
                check('%s raises' % state, False, 'no exception')
            except fw.ApiError as exc:
                check('%s is 409 and names the state' % state, exc.status == 409 and state in exc.message, exc.message)
            check('%s does not run make, flashtool, or systemctl' % state, w.runner.calls == [], w.runner.calls)
            check('%s does not claim Klipper was stopped' % state, not any(l.startswith('RECOVERY') for l in logs))
        finally:
            w.close()
    w = World()
    try:
        w.moon.shutdown()
        w.save('toolhead', STM32_CONFIG)
        # save_config does not need Moonraker. flash does.
        try:
            w.svc.flash({'mcu': 'toolhead', 'confirm': True}, lambda line: None)
            check('moonraker down refuses', False)
        except fw.ApiError as exc:
            check('moonraker down refuses before any command', exc.status == 503 and w.runner.calls == [], (exc.status, w.runner.calls))
    finally:
        w.tmp.cleanup()


def test_second_check():
    print('print state changes during the build')
    w = World(['standby', 'standby', 'printing'])
    try:
        w.save('toolhead', STM32_CONFIG)
        logs = []
        try:
            w.svc.flash({'mcu': 'toolhead', 'confirm': True}, logs.append)
            check('second check raises', False)
        except fw.ApiError as exc:
            check('second check is 409 right before flashing', exc.status == 409 and 'right before flashing' in exc.message, exc.message)
        joined = '\n'.join(' '.join(c) for c in w.runner.calls)
        check('build ran', any(c[0] == 'make' and 'flash' not in c for c in w.runner.calls), w.runner.calls)
        check('Klipper was not stopped', 'systemctl stop' not in joined and 'flashtool' not in joined, joined)
        check('recovery says Klipper was not stopped', any('was not stopped' in l for l in logs), logs)
    finally:
        w.close()


def test_can_usb_direct():
    print('flash paths')
    w = World()
    try:
        flag = os.path.join(w.root, 'FLAG')
        saved = w.save('toolhead', SHELL_CONFIG)
        check('import reports CONFIG_MCU and does not run the file',
              saved['config_mcu'] == 'stm32f446xx' and not os.path.exists(flag))
        check('config path stays in the firmware folder', saved['path'].startswith(w.fwdir) and saved['path'].endswith('toolhead.config'))
        original = open(saved['path'], encoding='utf-8').read()
        w.svc.describe()
        check('status does not overwrite the .config', open(saved['path'], encoding='utf-8').read() == original)

        logs = w.logs_of('toolhead')
        text = '\n'.join(logs)
        check('CAN requests the bootloader then flashes',
              any(c[1] == w.flashtool and c[2:] == ['-i', 'can0', '-u', CAN_UUID, '-r'] for c in w.runner.calls))
        flashed = [c for c in w.runner.calls if len(c) > 2 and c[1] == w.flashtool and '-f' in c]
        check('CAN firmware file is the build output', len(flashed) == 1 and flashed[0][flashed[0].index('-f') + 1].endswith(
            os.path.join('toolhead', 'klipper.bin')) and os.path.isfile(flashed[0][flashed[0].index('-f') + 1]))
        build_out = [a.split('=', 1)[1] for c in w.runner.calls if c[0] == 'make' and 'flash' not in c for a in c if a.startswith('OUT=')]
        check('build OUT ends with / and is outside the config directory',
              build_out == [os.path.join(w.build, 'toolhead') + '/']
              and not fw.inside(w.fwdir, build_out[0].rstrip('/'))
              and not fw.inside(w.config, build_out[0].rstrip('/')), build_out)
        check('CAN stream ends with the new version', 'DONE mcu=toolhead' in text and 'mismatch=0' in text, text)
        check('shell text in the .config was never an argument', argv_ok(w.runner.calls, w.flashtool) and not os.path.exists(flag))
        check('Klipper stop then start',
              [c[-2:] for c in w.runner.calls if c[0] == 'sudo'] == [['stop', 'klipper'], ['start', 'klipper']])

        w.runner.calls.clear()
        w.plant(USB_MAIN)
        w.save('mcu', STM32_CONFIG, 'katapult_usb')
        # version was updated to HOST by the previous start; put an old one back
        w.state.versions['mcu'] = OLD
        logs = w.logs_of('mcu')
        text = '\n'.join(logs)
        usb_calls = [c for c in w.runner.calls if len(c) > 1 and c[1] == w.flashtool]
        check('USB requests the bootloader on the Klipper serial',
              any('-r' in c and '-d' in c and os.path.basename(USB_MAIN) in ' '.join(c) for c in usb_calls), usb_calls)
        check('USB then flashes the Katapult device',
              any('-f' in c and 'katapult' in ' '.join(c).lower() and 'aaaa1111bbbb2222' in ' '.join(c).lower() for c in usb_calls), usb_calls)
        check('USB stream done', 'DONE mcu=mcu' in text, text)

        w.runner.calls.clear()
        w.plant(USB_EBB)
        w.save('ebb', RP2040_CONFIG, 'direct')
        w.state.versions['ebb'] = OLD
        logs = w.logs_of('ebb')
        text = '\n'.join(logs)
        flash_make = [c for c in w.runner.calls if c[0] == 'make' and 'flash' in c]
        check('RP2040 direct uses make flash and names the boot path',
              len(flash_make) == 1 and any(a.startswith('FLASH_DEVICE=') and os.path.basename(USB_EBB) in a for a in flash_make[0])
              and 'RP2040' in text and 'flashtool' not in '\n'.join(' '.join(c) for c in w.runner.calls), text)
        direct_out = [a.split('=', 1)[1] for a in flash_make[0] if a.startswith('OUT=')] if flash_make else []
        check('direct flash OUT ends with / outside the config directory',
              direct_out == [os.path.join(w.build, 'ebb') + '/']
              and any(a.startswith('KCONFIG_CONFIG=') and a.endswith('ebb.config') for a in flash_make[0]), direct_out)
        check('whitelist held for every command', argv_ok(w.runner.calls, w.flashtool))

        w.runner.calls.clear()
        w.save('mcu', STM32_CONFIG, 'direct')
        w.state.versions['mcu'] = OLD
        logs = w.logs_of('mcu')
        text = '\n'.join(logs)
        check('STM32 direct names DFU', 'DFU' in text and any(
            c[0] == 'make' and 'flash' in c and any('FLASH_DEVICE=' in a and 'stm32f446xx' in a for a in c)
            for c in w.runner.calls), text)
    finally:
        w.close()


def test_version_unchanged():
    print('version did not change')
    w = World()
    try:
        w.save('toolhead', STM32_CONFIG)
        w.state.versions['toolhead'] = HOST  # already the host version; start keeps it
        logs = []
        try:
            w.svc.flash({'mcu': 'toolhead', 'confirm': True}, logs.append)
            check('unchanged version fails', False)
        except fw.ApiError as exc:
            check('unchanged version is an error', 'did not change' in exc.message, exc.message)
        check('recovery mentions the MCU not coming back on new firmware',
              any(l.startswith('RECOVERY') and 'did not come back' in l for l in logs), logs)
        joined = '\n'.join(' '.join(c) for c in w.runner.calls)
        check('Klipper was started again', joined.count('start klipper') >= 1)
    finally:
        w.close()


def test_http_whitelist():
    print('http whitelist')
    w = World(['printing'])
    try:
        w.save('toolhead', STM32_CONFIG)
        fw.Handler.service = w.svc
        httpd = start_server(fw.Handler)
        port = httpd.server_address[1]
        try:
            code, body = http_json(port, 'POST', '/firmware/v1/flash',
                                   {'mcu': 'toolhead', 'confirm': True})
            check('printing is HTTP 409', code == 409 and 'printing' in body, (code, body))
            check('printing ran no commands', w.runner.calls == [], w.runner.calls)

            w.state.states = ['standby']
            code, body = http_json(port, 'POST', '/firmware/v1/flash',
                                   {'mcu': 'toolhead', 'confirm': True, 'command': 'touch FLAG'})
            check('extra command field is 400', code == 400 and 'unknown field' in body and 'command' in body, (code, body))
            check('rejected command ran nothing', w.runner.calls == [])
            code, body = http_json(port, 'POST', '/firmware/v1/run', {'command': 'id'})
            check('unknown action is 404', code == 404 and 'unknown action' in body, (code, body))
            code, body = http_json(port, 'POST', '/firmware/v1/flash',
                                   {'mcu': 'toolhead', 'confirm': 'true'})
            check('string confirm is refused', code == 400 and 'confirm' in body, (code, body))
            code, body = http_json(port, 'POST', '/firmware/v1/flash',
                                   {'mcu': 'toolhead', 'confirm': False})
            check('confirm false is refused', code == 400, (code, body))
            code, body = http_json(port, 'POST', '/firmware/v1/flash',
                                   {'mcu': 'mcu;rm', 'confirm': True})
            check('mcu name with a command is refused', code == 400, (code, body))
            code, body = http_json(port, 'GET', '/firmware/v1/status?cmd=id')
            check('status ignores a command query', code == 200 and json.loads(body)['ok'] is True)
            code, body = http_json(port, 'PUT', '/firmware/v1/config',
                                   {'mcu': '../x', 'config': STM32_CONFIG})
            check('config path traversal is refused', code == 400, (code, body))
            check('still no commands after the rejected requests', w.runner.calls == [], w.runner.calls)
            code, body = http_json(port, 'GET', '/firmware/v1/status')
            mcus = {m['name']: m for m in json.loads(body)['mcus']}
            check('status lists the CAN and USB mcus', 'toolhead' in mcus and 'mcu' in mcus and mcus['toolhead']['config_mcu'] == 'stm32f446xx')
        finally:
            httpd.shutdown()
    finally:
        w.close()


SAMPLE_KCONFIG = """
mainmenu "Klipper Firmware Configuration"

config LOW_LEVEL_OPTIONS
    bool "Enable extra low-level configuration options"

choice
    prompt "Micro-controller Architecture"
    default MCU_STM32

config MCU_STM32
    bool "STMicroelectronics STM32"

config MCU_RP2040
    bool "Raspberry Pi RP2040"

endchoice

config MCU
    string
    default "stm32f446xx" if MCU_STM32
    default "rp2040" if MCU_RP2040

menu "STM32 options"
    depends on MCU_STM32

config FLASH_START
    hex "Application start address"
    default 0x8008000

config SERIAL_BAUD
    int "Baud rate"
    default 250000

endmenu

menu "RP2040 options"
    depends on MCU_RP2040

config RP2040_FLASH
    bool "Add flash chip support"
    default y

endmenu
"""


def _field_names(fields):
    found = []
    for field in fields or []:
        if field.get('name'):
            found.append(field['name'])
        for opt in field.get('options') or []:
            found.append(opt.get('name'))
        found.extend(_field_names(field.get('children')))
    return found


def _find_kconfiglib():
    cands = []
    if os.environ.get('KLIPPER_PATH'):
        cands.append(os.path.join(os.environ['KLIPPER_PATH'], 'lib', 'kconfiglib', 'kconfiglib.py'))
    cands.extend((
        os.environ.get('SC_KCONFIGLIB'),
        os.path.expanduser('~/klipper/lib/kconfiglib/kconfiglib.py'),
        os.path.join(ROOT, '..', 'klipper-src', 'lib', 'kconfiglib', 'kconfiglib.py'),
        '/tmp/kconfiglib-src/kconfiglib.py',
    ))
    for path in cands:
        if path and os.path.isfile(path):
            return path
    return None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def test_includes_and_labels():
    print('includes and labels')
    tmp = tempfile.TemporaryDirectory()
    moon = None
    try:
        root = tmp.name
        outside = os.path.join(root, 'outside')
        config = os.path.join(root, 'config')
        _write(os.path.join(outside, 'mainsail.cfg'), '# mainsail\n')
        _write(os.path.join(outside, 'ktc-filament', 'filament', 'filament_macros.cfg'),
               '[gcode_macro _FILAMENT_SETTINGS]\ngcode:\n')
        _write(os.path.join(outside, 'ktc-filament', 'orbiter', 'orbiter_addon.cfg'),
               '[gcode_macro _ORBITER]\ngcode:\n')
        _write(os.path.join(outside, 'ktc-filament', 'usage', 'usage_addon.cfg'),
               '[gcode_macro _USAGE]\ngcode:\n')
        _write(os.path.join(outside, 'extra.cfg'), '[mcu extra]\ncanbus_uuid: aabbccddeeff\n')
        os.makedirs(config)
        os.symlink(os.path.join(outside, 'mainsail.cfg'), os.path.join(config, 'mainsail.cfg'))
        os.symlink(os.path.join(outside, 'ktc-filament'), os.path.join(config, 'ktc-filament'))
        _write(os.path.join(config, 'toolchanger', 'tools', 'T0.cfg'),
               '[mcu EBB0]\ncanbus_uuid: %s\n'
               '[tool T0]\ntool_number: 0\nextruder: extruder\n'
               '[extruder]\nstep_pin: EBB0:PA1\ndir_pin: EBB0:PA2\nheater_pin: EBB0:PB0\n' % CAN_UUID)
        _write(os.path.join(config, 'toolchanger', 'tools', 'T1.cfg'),
               '[mcu EBB1]\ncanbus_uuid: b1b2c3d4e5f6\n'
               '[tool T1]\ntool_number: 1\nextruder: extruder1\n'
               '[extruder1]\nstep_pin: EBB1:PA1\nheater_pin: EBB1:PB1\n')
        _write(os.path.join(config, 'toolchanger', 'toolchanger-config.cfg'), '# toolchanger\n')
        _write(os.path.join(config, 'toolchanger', 'readonly-configs', 'toolchanger-include.cfg'),
               '[include ../tools/T*.cfg]\n[include ../toolchanger-config.cfg]\n')
        _write(os.path.join(config, 'loop.cfg'), '[include loop.cfg]\n')
        printer = os.path.join(config, 'printer.cfg')
        _write(printer,
               '[include mainsail.cfg]\n'
               '[include ktc-filament/filament/filament_macros.cfg]\n'
               '[include ktc-filament/orbiter/orbiter_addon.cfg]\n'
               '[include ktc-filament/usage/usage_addon.cfg]\n'
               '[include toolchanger/readonly-configs/toolchanger-include.cfg]\n'
               '[include ../outside/extra.cfg]\n'
               '[include loop.cfg]\n'
               '[mcu]\nserial: %s\n'
               '[mcu bothboard]\nserial: %s\ncanbus_uuid: c1c2c3c4c5c6\n' % (USB_MAIN, USB_EBB))
        parsed = fw.parse_printer(config, printer)
        warnings = parsed['warnings']
        specs = (
            'mainsail.cfg', 'filament_macros.cfg', 'orbiter_addon.cfg', 'usage_addon.cfg',
            '../tools/T*.cfg', '../toolchanger-config.cfg')
        check('normal includes produce no warnings',
              all(not any(spec in w for w in warnings) for spec in specs)
              and not any('refusing' in w or 'leaves the config' in w for w in warnings), warnings)
        check('recursive include is the only guard, and it finishes',
              any('recursive' in w and 'loop.cfg' in w for w in warnings), warnings)
        names = [s['name'] for s in parsed['sections']]
        check('mcus from symlinks, globs, and parent dirs',
              set(names) >= {'mcu', 'EBB0', 'EBB1', 'extra', 'bothboard'}, names)
        check('tool pins name T0 and T1',
              parsed['mcu_tools'].get('EBB0') == {0} and parsed['mcu_tools'].get('EBB1') == {1},
              parsed['mcu_tools'])
        state = MoonState()
        state.versions = {'mcu': HOST, 'EBB0': OLD, 'EBB1': OLD, 'extra': HOST, 'bothboard': OLD}
        moon = start_server(MoonHandler, state)
        svc = fw.FirmwareService(
            runner=Runner(os.path.join(root, 'by-id'), state),
            moonraker='http://127.0.0.1:%d' % moon.server_address[1],
            config_dir=os.path.join(config, 'stealthchanger-firmware'),
            printer_cfg=printer,
            klipper_dir=os.path.join(root, 'klipper'),
            flashtool=os.path.join(root, 'flashtool.py'),
            serial_by_id=os.path.join(root, 'by-id'),
            verify_seconds=1, verify_pause=0.01)
        os.makedirs(os.path.join(root, 'klipper'), exist_ok=True)
        with open(os.path.join(root, 'klipper', 'Makefile'), 'w', encoding='utf-8') as fh:
            fh.write('all:\n')
        with open(svc.flashtool, 'w', encoding='utf-8') as fh:
            fh.write('# mock\n')
        by = {m['name']: m for m in svc.describe()['mcus']}
        check('derived labels',
              by['mcu']['label'] == 'Main Board' and by['EBB0']['label'] == 'T0'
              and by['EBB1']['label'] == 'T1' and by['extra']['label'] == 'extra',
              {k: v['label'] for k, v in by.items()})
        check('both serial and canbus_uuid defaults to Katapult CAN',
              by['bothboard']['method'] == 'katapult_can' and 'katapult_can' in by['bothboard']['method_choices'])
        check('badges',
              by['mcu']['update_state'] == 'current' and by['EBB0']['update_state'] == 'behind')
        saved = svc.set_label({'mcu': 'EBB0', 'label': 'Orbiter 0'})
        check('label file stays in the firmware folder',
              saved['path'].endswith(os.path.join('stealthchanger-firmware', 'labels.json'))
              and os.path.isfile(saved['path']))
        check('custom label is what status returns',
              {m['name']: m['label'] for m in svc.describe()['mcus']}['EBB0'] == 'Orbiter 0')
        svc.set_label({'mcu': 'EBB0', 'label': '   '})
        check('clearing the label restores the automatic name',
              {m['name']: m['label'] for m in svc.describe()['mcus']}['EBB0'] == 'T0')
        try:
            svc.set_label({'mcu': '../x', 'label': 'nope'})
            check('label path is refused', False)
        except fw.ApiError as exc:
            check('label path is refused', exc.status == 400, exc.message)
        check('labels did not run a command', svc.runner.calls == [])
    finally:
        if moon is not None:
            moon.shutdown()
        tmp.cleanup()


def test_flash_all():
    print('flash all')
    w = World(['printing'])
    try:
        w.save('toolhead', STM32_CONFIG)
        w.save('mcu', STM32_CONFIG)
        w.runner.calls.clear()
        try:
            w.svc.flash_all({'confirm': True}, lambda line: None)
            check('flash all refuses while printing', False)
        except fw.ApiError as exc:
            check('flash all refuses while printing', exc.status == 409 and 'printing' in exc.message, exc.message)
        check('printing runs no commands', w.runner.calls == [], w.runner.calls)
    finally:
        w.close()

    w = World(['standby', 'standby', 'standby', 'printing'])
    try:
        w.save('toolhead', STM32_CONFIG)
        w.save('mcu', STM32_CONFIG)
        w.runner.calls.clear()
        logs = []
        try:
            w.svc.flash_all({'confirm': True}, logs.append)
            check('second board print check raises', False)
        except fw.ApiError as exc:
            check('second board print check raises', exc.status == 409 and 'before mcu' in exc.message, exc.message)
        joined = '\n'.join(' '.join(c) for c in w.runner.calls)
        check('stopped before Klipper was stopped', 'systemctl stop' not in joined, joined)
        check('recovery says Klipper was not stopped', any('was not stopped' in line for line in logs), logs)
    finally:
        w.close()

    w = World()
    try:
        w.save('toolhead', STM32_CONFIG)
        w.save('mcu', STM32_CONFIG)
        w.plant(USB_MAIN)
        w.runner.calls.clear()
        w.state.versions['toolhead'] = OLD
        w.state.versions['mcu'] = OLD
        # ebb is behind too, but it has no .config, so it is not in the batch.
        logs = []
        result = w.svc.flash_all({'confirm': True}, logs.append)
        text = '\n'.join(logs)
        sudo = [c[-2:] for c in w.runner.calls if c[0] == 'sudo']
        check('flash all returns both boards', result['mcus'] == ['toolhead', 'mcu'], result['mcus'])
        check('Klipper stopped once and started once', sudo == [['stop', 'klipper'], ['start', 'klipper']], sudo)
        stop_at = next(i for i, c in enumerate(w.runner.calls) if c[0] == 'sudo')
        makes = [c for c in w.runner.calls[:stop_at] if c[0] == 'make']
        check('both boards build before Klipper stops', len(makes) == 2, makes)
        check('log names each board', 'toolhead' in text and '--- ' in text and 'LOG flashed mcu' in text, text)
        check('ebb was not flashed', 'LOG flashed ebb' not in text and 'DONE mcu=ebb' not in text)
        check('whitelist held', argv_ok(w.runner.calls, w.flashtool))

        w.runner.calls.clear()
        w.state.versions['toolhead'] = OLD
        w.state.versions['mcu'] = OLD
        w.plant(USB_MAIN)
        w.runner.fail_when = lambda argv: any(a.endswith(os.path.join('mcu', 'klipper.bin')) for a in argv)
        logs = []
        try:
            w.svc.flash_all({'confirm': True}, logs.append)
            check('flash all stops on the first failure', False)
        except fw.ApiError as exc:
            check('flash all stops on the first failure', exc.status == 500, exc.message)
        text = '\n'.join(logs)
        check('failure flashed the first board only',
              'LOG flashed toolhead' in text and 'LOG flashed mcu' not in text, text)
        check('failure starts Klipper again',
              any(c[-2:] == ['start', 'klipper'] for c in w.runner.calls) and any('RECOVERY' in line for line in logs))
    finally:
        w.close()


def test_kconfig_form():
    print('menuconfig form')
    src = _find_kconfiglib()
    if not src:
        check('real kconfiglib is available', False, 'set SC_KCONFIGLIB or use ~/klipper/lib/kconfiglib/kconfiglib.py')
        return
    w = World()
    try:
        lib = os.path.join(w.klipper, 'lib', 'kconfiglib')
        os.makedirs(lib)
        shutil.copy(src, os.path.join(lib, 'kconfiglib.py'))
        with open(os.path.join(w.klipper, 'Kconfig'), 'w', encoding='utf-8') as fh:
            fh.write(SAMPLE_KCONFIG)
        w.runner.calls.clear()
        form = w.svc.kconfig_schema({'mcu': 'toolhead', 'base': 'defaults'})
        names = _field_names(form['fields'])
        check('defaults show the STM32 menu and CONFIG_MCU',
              form['config_mcu'] == 'stm32f446xx' and 'FLASH_START' in names and 'RP2040_FLASH' not in names
              and 'MCU_STM32' in names and form['wrote'] is False, (form['config_mcu'], names))
        check('reading the form does not write or flash',
              w.runner.calls == [] and not os.path.exists(os.path.join(w.fwdir, 'toolhead.config')))
        changed = w.svc.kconfig_schema({
            'mcu': 'toolhead', 'base': 'defaults',
            'values': {'MCU_RP2040': 'y', 'MCU_STM32': 'n'}})
        changed_names = _field_names(changed['fields'])
        check('dependencies hide STM32 and show RP2040',
              changed['config_mcu'] == 'rp2040' and 'RP2040_FLASH' in changed_names and 'FLASH_START' not in changed_names,
              (changed['config_mcu'], changed_names))
        saved = w.svc.kconfig_save({
            'mcu': 'toolhead', 'base': 'defaults',
            'values': {'MCU_RP2040': 'y', 'MCU_STM32': 'n', 'LOW_LEVEL_OPTIONS': 'y'}})
        text = open(saved['path'], encoding='utf-8').read()
        check('save writes the board .config like menuconfig',
              saved['config_mcu'] == 'rp2040'
              and saved['path'] == os.path.join(w.fwdir, 'toolhead.config')
              and 'CONFIG_MCU="rp2040"' in text
              and 'CONFIG_MCU_RP2040=y' in text
              and 'CONFIG_LOW_LEVEL_OPTIONS=y' in text
              and not os.path.exists(saved['path'] + '.old'), text)
        check('menuconfig did not flash', w.runner.calls == [])
        edited = w.svc.kconfig_schema({
            'mcu': 'toolhead', 'base': 'saved',
            'values': {'MCU_STM32': 'y', 'MCU_RP2040': 'n', 'FLASH_START': '0x8000'}})
        check('editing the saved config brings the STM32 fields back',
              edited['config_mcu'] == 'stm32f446xx' and 'FLASH_START' in _field_names(edited['fields']))
        try:
            w.svc.kconfig_schema({'mcu': 'toolhead', 'values': {'../evil': 'y'}})
            check('symbol names cannot be paths', False)
        except fw.ApiError as exc:
            check('symbol names cannot be paths', exc.status == 400 and 'Unknown' in exc.message, exc.message)
        try:
            w.svc.kconfig_save({'mcu': 'toolhead', 'values': {'FLASH_START': '0x1\nCONFIG_MCU="nope"'}})
            check('newline values are refused', False)
        except fw.ApiError as exc:
            check('newline values are refused', exc.status == 400, exc.message)
        try:
            w.svc.kconfig_save({'mcu': '../x', 'base': 'defaults'})
            check('kconfig mcu path is refused', False)
        except fw.ApiError as exc:
            check('kconfig mcu path is refused', exc.status == 400, exc.message)
        check('rejected menuconfig ran nothing', w.runner.calls == [])
        # the earlier save is still the rp2040 file; the newline refusal must not replace it
        check('a refused save leaves the .config alone', 'CONFIG_MCU="rp2040"' in open(saved['path'], encoding='utf-8').read())
    finally:
        w.close()


def test_build_dir_is_cleaned():
    print('build directory')
    w = World()
    try:
        w.save('toolhead', STM32_CONFIG)
        stale = os.path.join(w.build, 'toolhead', 'autoconf.h')
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('stale')
        kept = os.path.join(w.build, 'mcu', 'keep')
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(kept, 'w', encoding='utf-8') as fh:
            fh.write('keep')
        w.runner.calls.clear()
        w.logs_of('toolhead')
        check('make saw an empty board directory',
              w.runner.build_listings and w.runner.build_listings[0][1] == []
              and w.runner.build_listings[0][0].endswith('/'), w.runner.build_listings)
        check('another board\'s build files are left in place', os.path.isfile(kept))
        check('stale autoconf.h was removed before make', not os.path.isfile(stale))
        fw_file = [a for c in w.runner.calls if '-f' in c for a in c if a.endswith('klipper.bin')]
        check('flashtool is given the bin outside the config directory',
              len(fw_file) == 1 and fw_file[0].startswith(w.build) and not fw.inside(w.config, fw_file[0]), fw_file)
    finally:
        w.close()


PREFILL_KCONFIG = SAMPLE_KCONFIG + """
config USBSERIAL
    bool
config CANSERIAL
    bool
config USBCANBUS
    bool

choice
    prompt "Communication interface"
    config COMM_USB
        bool "USB"
        select USBSERIAL
    config COMM_CAN
        bool "CAN bus"
        select CANSERIAL
    config COMM_BRIDGE
        bool "USB to CAN bus bridge"
        select USBCANBUS
endchoice
"""


def _selected(fields):
    found = {}

    def walk(items):
        for field in items or []:
            if field.get('kind') == 'choice':
                found['choice:' + field.get('prompt', '')] = field.get('value')
            elif field.get('name'):
                found[field['name']] = field.get('value')
            walk(field.get('children'))

    walk(fields)
    return found


def test_copy_and_prefill():
    print('copy config and MCU prefill')
    w = World()
    try:
        saved = w.save('toolhead', STM32_CONFIG)
        w.save('mcu', RP2040_CONFIG)
        w.runner.calls.clear()
        try:
            w.svc.copy_config({'mcu': 'toolhead', 'targets': ['toolhead']})
            check('refuses to copy onto itself', False)
        except fw.ApiError as exc:
            check('refuses to copy onto itself', exc.status == 400 and 'itself' in exc.message, exc.message)
        try:
            w.svc.copy_config({'mcu': 'toolhead', 'targets': ['../x']})
            check('copy path is refused', False)
        except fw.ApiError as exc:
            check('copy path is refused', exc.status == 400, exc.message)
        try:
            w.svc.copy_config({'mcu': 'toolhead', 'targets': ['nope']})
            check('copy target must be a real MCU', False)
        except fw.ApiError as exc:
            check('copy target must be a real MCU', exc.status == 404, exc.message)
        try:
            w.svc.copy_config({'mcu': 'toolhead', 'targets': ['mcu'], 'command': 'id'})
            check('copy rejects an extra field', False)
        except fw.ApiError as exc:
            check('copy rejects an extra field', exc.status == 400 and 'unknown field' in exc.message, exc.message)
        try:
            w.svc.copy_config({'mcu': 'toolhead', 'targets': ['mcu']})
            check('overwrite of an existing .config is refused', False)
        except fw.ApiError as exc:
            check('overwrite of an existing .config is refused', exc.status == 409 and 'mcu' in exc.message, exc.message)
        check('refused copy left the target .config',
              'CONFIG_MCU="rp2040"' in open(os.path.join(w.fwdir, 'mcu.config'), encoding='utf-8').read())
        copied = w.svc.copy_config({'mcu': 'toolhead', 'targets': ['ebb']})
        ebb_text = open(os.path.join(w.fwdir, 'ebb.config'), encoding='utf-8').read()
        check('copy writes the source .config onto a board that has none',
              copied['config_mcu'] == 'stm32f446xx' and ebb_text == open(saved['path'], encoding='utf-8').read())
        replaced = w.svc.copy_config({'mcu': 'toolhead', 'targets': ['mcu', 'ebb'], 'overwrite': True})
        check('overwrite replaces every ticked board',
              [item['mcu'] for item in replaced['copied']] == ['mcu', 'ebb']
              and open(os.path.join(w.fwdir, 'mcu.config'), encoding='utf-8').read() == ebb_text)
        check('copy does not run a command', w.runner.calls == [])

        w.state.constants = {'toolhead': {'MCU': 'stm32f446xx', 'CLOCK_FREQ': 180000000}}
        lib = os.path.join(w.klipper, 'lib', 'kconfiglib')
        os.makedirs(lib, exist_ok=True)
        src = _find_kconfiglib()
        if not src:
            check('kconfiglib is available for prefill', False)
        else:
            shutil.copy(src, os.path.join(lib, 'kconfiglib.py'))
            with open(os.path.join(w.klipper, 'Kconfig'), 'w', encoding='utf-8') as fh:
                fh.write(PREFILL_KCONFIG)
            form = w.svc.kconfig_schema({'mcu': 'toolhead', 'base': 'defaults'})
            chosen = _selected(form['fields'])
            check('Create preselects the reported MCU and CAN',
                  form['config_mcu'] == 'stm32f446xx'
                  and chosen.get('choice:Micro-controller Architecture') == 'MCU_STM32'
                  and chosen.get('choice:Communication interface') == 'COMM_CAN'
                  and form.get('prefill', {}).get('MCU_STM32') == 'y'
                  and form.get('prefill', {}).get('COMM_CAN') == 'y'
                  and 'CAN' in (form.get('prefill_note') or ''),
                  (form.get('config_mcu'), chosen, form.get('prefill_note')))
            w.state.constants['mcu'] = {'MCU': 'rp2040', 'CLOCK_FREQ': 12000000}
            usb = w.svc.kconfig_schema({'mcu': 'mcu', 'base': 'defaults'})
            usb_chosen = _selected(usb['fields'])
            check('Create preselects USB from a serial connection',
                  usb['config_mcu'] == 'rp2040'
                  and usb_chosen.get('choice:Micro-controller Architecture') == 'MCU_RP2040'
                  and usb_chosen.get('choice:Communication interface') == 'COMM_USB',
                  usb_chosen)
        w.state.constants['toolhead'] = {'MCU': 'rp2040', 'CLOCK_FREQ': 12000000}
        described = {item['name']: item for item in w.svc.describe()['mcus']}
        check('saved CONFIG_MCU that differs from the board is a mismatch',
              described['toolhead']['config_mcu'] == 'stm32f446xx'
              and described['toolhead']['reported_mcu'] == 'rp2040'
              and described['toolhead']['config_mcu_mismatch'] is True)
        w.state.constants['toolhead'] = {'MCU': 'STM32F446XX'}
        described = {item['name']: item for item in w.svc.describe()['mcus']}
        check('the same MCU string with different case is not a mismatch',
              described['toolhead']['config_mcu_mismatch'] is False)
    finally:
        w.close()


def find_klipper_tree():
    for cand in (
            os.environ.get('KLIPPER_PATH'),
            os.path.join(ROOT, '..', 'klipper-src'),
            os.path.expanduser('~/klipper')):
        if cand and os.path.isfile(os.path.join(cand, 'src', 'Kconfig')) and os.path.isfile(os.path.join(cand, 'Makefile')):
            return os.path.abspath(cand)
    return None


def test_real_klipper_tree():
    print('real Klipper tree')
    tree = find_klipper_tree()
    if not tree:
        print('  skip  no klipper-src, KLIPPER_PATH, or ~/klipper')
        return
    kconf, mod = fw.kconfig_menu.load_kconfig(tree)
    info = fw.kconfig_menu.suggest_initial(kconf, mod, 'stm32f446xx', 180000000, 'can')
    values = (info or {}).get('values') or {}
    check('real Kconfig preselects STM32F446 and CAN',
          info and values.get('MACH_STM32') == 'y' and values.get('MACH_STM32F446') == 'y'
          and kconf.syms['MCU'].str_value == 'stm32f446xx'
          and any(name.startswith('STM32_CANBUS_') and values.get(name) == 'y' for name in values)
          and not any('USBCAN' in name for name in values), values)
    usb_k, usb_mod = fw.kconfig_menu.load_kconfig(tree)
    usb = fw.kconfig_menu.suggest_initial(usb_k, usb_mod, 'rp2040', 12000000, 'usb')
    usb_values = (usb or {}).get('values') or {}
    check('real Kconfig preselects RP2040 USB',
          usb_values.get('MACH_RPXXXX') == 'y' and usb_values.get('MACH_RP2040') == 'y'
          and usb_values.get('RPXXXX_USB') == 'y' and usb_k.syms['MCU'].str_value == 'rp2040', usb_values)
    ambiguous, amb_mod = fw.kconfig_menu.load_kconfig(tree)
    amb = fw.kconfig_menu.suggest_initial(ambiguous, amb_mod, 'stm32f103xe', None, 'can')
    amb_values = (amb or {}).get('values') or {}
    check('a shared MCU string does not guess the processor',
          amb and amb.get('ambiguous_processor') and 'MACH_STM32F103' not in amb_values
          and 'MACH_N32G45x' not in amb_values and amb_values.get('MACH_STM32') == 'y', amb)
    picked, pick_mod = fw.kconfig_menu.load_kconfig(tree)
    pick = fw.kconfig_menu.suggest_initial(picked, pick_mod, 'stm32f103xe', 72000000, 'usb')
    pick_values = (pick or {}).get('values') or {}
    check('CLOCK_FREQ picks the processor when the MCU string is shared',
          pick_values.get('MACH_STM32F103') == 'y' and 'MACH_N32G45x' not in pick_values
          and picked.syms['MCU'].str_value == 'stm32f103xe', pick_values)

    if not shutil.which('gcc') or not shutil.which('make'):
        check('gcc and make are available to prove the OUT path', False)
        return
    w = World()
    try:
        w.svc.klipper_dir = tree
        w.svc.runner = fw.RealRunner()
        cfg = os.path.join(w.fwdir, 'mcu.config')
        os.makedirs(w.fwdir, exist_ok=True)
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write('CONFIG_MACH_LINUX=y\n')
        scratch = os.path.join(w.root, 'olddef') + os.sep
        os.makedirs(scratch, exist_ok=True)
        subprocess.check_call(
            ['make', '-C', tree, 'olddefconfig', 'KCONFIG_CONFIG=' + cfg, 'OUT=' + scratch],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        logs = []
        w.svc._build('mcu', logs.append)
        out = w.svc._out_dir('mcu')
        check('helper build OUT ends with / outside config',
              out.endswith('/') and out.startswith(w.build + os.sep) and not fw.inside(w.config, out.rstrip('/')))
        check('real make wrote sched.o under OUT/src and autoconf.h',
              os.path.isfile(os.path.join(out, 'src', 'sched.o'))
              and os.path.isfile(os.path.join(out, 'autoconf.h'))
              and not os.path.isdir(out.rstrip('/') + 'src'),
              out)
    finally:
        w.close()


def stage_helper_tree():
    tmp = tempfile.TemporaryDirectory()
    firm = os.path.join(tmp.name, 'firmware')
    os.makedirs(firm)
    src = os.path.join(ROOT, 'firmware')
    for name in os.listdir(src):
        if name.endswith('.py'):
            shutil.copy(os.path.join(src, name), os.path.join(firm, name))
    return tmp, firm


def poke_helper(firm):
    path = os.path.join(firm, 'firmware_helper.py')
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write('\n# code changed %s\n' % time.time())


def helper_service(firm, exits):
    return fw.FirmwareService(
        runner=Runner(firm, MoonState()),
        moonraker='http://127.0.0.1:9',
        config_dir=os.path.join(firm, 'cfg'),
        build_dir=os.path.join(firm, 'build'),
        printer_cfg=os.path.join(firm, 'printer.cfg'),
        klipper_dir=os.path.join(firm, 'klipper'),
        flashtool=os.path.join(firm, 'flashtool.py'),
        serial_by_id=os.path.join(firm, 'by-id'),
        firmware_dir=firm,
        on_code_change=lambda: exits.append('exit'),
        code_check_interval=0.05,
        verify_seconds=1,
        verify_pause=0.01)


def test_helper_restarts_when_code_changes():
    print('helper restarts when its code changes')
    tmp, firm = stage_helper_tree()
    httpd = None
    svc3 = None
    try:
        check('code check is every 10 to 30 seconds', 10 <= fw.CODE_CHECK_SECONDS <= 30)
        base = fw.code_fingerprint(firm)
        with open(os.path.join(tmp.name, 'VERSION'), 'w', encoding='utf-8') as fh:
            fh.write('0.2.12\n')
        check('VERSION file is part of the fingerprint', fw.code_fingerprint(firm) != base)
        git = os.path.join(tmp.name, '.git')
        os.makedirs(os.path.join(git, 'refs', 'heads'))
        with open(os.path.join(git, 'HEAD'), 'w', encoding='utf-8') as fh:
            fh.write('ref: refs/heads/main\n')
        ref = os.path.join(git, 'refs', 'heads', 'main')
        with open(ref, 'w', encoding='utf-8') as fh:
            fh.write(('a' * 40) + '\n')
        with_head = fw.code_fingerprint(firm)
        with open(ref, 'w', encoding='utf-8') as fh:
            fh.write(('b' * 40) + '\n')
        check('git HEAD is part of the fingerprint', fw.code_fingerprint(firm) != with_head)

        exits = []
        svc = helper_service(firm, exits)
        check('startup fingerprint requests nothing',
              svc.poll_code_change() == 'unchanged' and not svc.exit_requested)
        poke_helper(firm)
        check('fingerprint change requests an exit when idle',
              svc.poll_code_change() == 'exit' and svc.exit_requested)
        check('restart callback runs once',
              svc.trigger_restart() is True and exits == ['exit'] and svc.trigger_restart() is False)

        exits2 = []
        svc2 = helper_service(firm, exits2)
        svc2._begin_job({'mcu': 'mcu', 'action': 'flash'})
        poke_helper(firm)
        check('change during a job is deferred',
              svc2.poll_code_change() == 'deferred' and not svc2.exit_requested)
        check('a deferred job does not restart', svc2.trigger_restart() is False and exits2 == [])
        check('ending the job requests the exit', svc2._end_job() == 'exit' and svc2.exit_requested)
        check('restart runs after the job', svc2.trigger_restart() is True and exits2 == ['exit'])

        exits3 = []
        svc3 = helper_service(firm, exits3)
        with svc3._lock:
            svc3._job = {'mcu': 'toolhead', 'action': 'flash'}
        poke_helper(firm)
        svc3.start_code_watch()
        deadline = time.time() + 2
        while time.time() < deadline and not svc3._reload_deferred:
            time.sleep(0.05)
        check('watch thread defers the exit while a job is running',
              svc3._reload_deferred and exits3 == [] and not svc3.exit_requested)
        with svc3._lock:
            svc3._job = None
        deadline = time.time() + 2
        while time.time() < deadline and not exits3:
            time.sleep(0.05)
        check('watch thread requests an exit after the job',
              exits3 == ['exit'] and svc3.exit_requested)

        exits4 = []
        svc4 = helper_service(firm, exits4)
        fw.Handler.service = svc4
        httpd = start_server(fw.Handler)
        poke_helper(firm)
        code, body = http_json(httpd.server_address[1], 'POST', '/firmware/v1/flash',
                               {'mcu': 'toolhead', 'confirm': True})
        check('idle code change refuses the flash and restarts',
              code == 503 and 'restarting' in body and exits4 == ['exit'], (code, body, exits4))
    finally:
        if svc3 is not None:
            svc3.stop_code_watch()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        tmp.cleanup()


def test_flash_defers_code_restart():
    print('code change during a flash waits until the job ends')
    tmp, firm = stage_helper_tree()
    w = World()
    exits = []
    started = threading.Event()
    release = threading.Event()
    try:
        w.svc._firmware_dir = firm
        w.svc._code_fp = fw.code_fingerprint(firm)
        w.svc._on_code_change = lambda: exits.append('exit')
        w.svc._exit_requested = False
        w.svc._reload_deferred = False
        w.svc._restart_started = False
        w.save('toolhead', STM32_CONFIG)
        inner = w.runner.run

        def run(argv, on_line=None):
            if argv and argv[0] == 'make' and 'flash' not in argv and not started.is_set():
                started.set()
                if not release.wait(5):
                    raise AssertionError('build was not released')
            return inner(argv, on_line)

        w.runner.run = run
        logs = []
        errors = []

        def go():
            try:
                w.svc.flash({'mcu': 'toolhead', 'confirm': True}, logs.append)
            except Exception as exc:
                errors.append(exc)

        th = threading.Thread(target=go)
        th.start()
        check('build started while the helper code is current', started.wait(5))
        poke_helper(firm)
        state = w.svc.poll_code_change()
        check('change during the flash is deferred',
              state == 'deferred' and not w.svc.exit_requested, state)
        check('no restart while the flash is running',
              w.svc.trigger_restart() is False and exits == [])
        release.set()
        th.join(5)
        text = '\n'.join(logs)
        check('flash thread finished', not th.is_alive() and not errors, errors)
        check('flash finished after the code change', 'DONE mcu=toolhead' in text, text)
        check('exit is requested once the job has ended', w.svc.exit_requested)
        check('restart runs after the flash', w.svc.trigger_restart() is True and exits == ['exit'])
    finally:
        release.set()
        w.close()
        tmp.cleanup()


def test_source_and_bind():
    print('no arbitrary execution')
    src = open(os.path.join(ROOT, 'firmware', 'firmware_helper.py'), encoding='utf-8').read()
    check('shell is never enabled', 'shell=True' not in src and 'shell = True' not in src and 'shell=False' in src)
    check('no os.system or popen', 'os.system' not in src and 'os.popen' not in src and 'subprocess.call(' not in src)
    check('main rejects arguments', fw.main(['firmware_helper.py', 'id']) == 2)
    try:
        fw.assert_bind('0.0.0.0')
        check('refuses non-loopback bind', False)
    except SystemExit as exc:
        check('refuses non-loopback bind', '127.0.0.1' in str(exc), exc)


def main():
    test_parse_and_refuse_escape()
    test_status_and_guess_refusal()
    test_print_refusal()
    test_second_check()
    test_can_usb_direct()
    test_version_unchanged()
    test_includes_and_labels()
    test_flash_all()
    test_kconfig_form()
    test_http_whitelist()
    test_build_dir_is_cleaned()
    test_copy_and_prefill()
    test_real_klipper_tree()
    test_helper_restarts_when_code_changes()
    test_flash_defers_code_restart()
    test_source_and_bind()
    print()
    print('%d passed, %d failed' % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
