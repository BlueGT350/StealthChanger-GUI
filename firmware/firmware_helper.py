#!/usr/bin/env python3
"""Localhost firmware helper for the StealthChanger GUI.

The GUI is static files plus Moonraker. Flashing has to run on the printer,
so this process listens on 127.0.0.1 only. nginx proxies /firmware/ to it
(same origin as the GUI). It does not run shell commands from the request.

Whitelisted actions:
  GET  /firmware/v1/status
  PUT  /firmware/v1/config     {"mcu", "config"}
  POST /firmware/v1/method     {"mcu", "method"}
  POST /firmware/v1/label      {"mcu", "label"}
  POST /firmware/v1/kconfig    {"mcu", "base"?, "values"?}   schema only, no write
  PUT  /firmware/v1/kconfig    {"mcu", "base"?, "values"?}   write that board's .config
  POST /firmware/v1/copy       {"mcu", "targets": [...], "overwrite": false}
  POST /firmware/v1/flash      {"mcu", "confirm": true}
  POST /firmware/v1/flash_all  {"confirm": true}

Flash methods. A saved choice wins. Otherwise:
  canbus_uuid    Katapult over CAN (the selector shows that)
  serial by-id   Katapult over USB when Katapult's flashtool is installed,
                 otherwise direct USB
  katapult_can   flashtool.py -i can0 -u <uuid>
  katapult_usb   flashtool.py -d <serial> -r, then flash the Katapult device
  direct         make flash FLASH_DEVICE=<serial>
                 STM32 uses Klipper's DFU path, RP2040 uses the boot path.
                 The chip comes from CONFIG_MCU in the user's .config.

Menuconfig files and board labels live in
~/printer_data/config/stealthchanger-firmware/ and are never written by a
git update. Build output lives in
~/printer_data/stealthchanger-firmware-build/<mcu>/, outside the Mainsail
config folder. The chip is never guessed: with no .config, or no CONFIG_MCU,
flashing is refused. The in-browser form writes the .config through
Klipper's kconfiglib and does not flash. Create can preselect the
architecture, processor, and communication interface from the MCU Klipper
is already talking to.

Moonraker updates the git clone but cannot restart this user service, so a
running helper would keep the old code. At startup the process fingerprints
firmware/*.py, a repo VERSION file when one exists, and git HEAD. Every few
seconds, and before each action, it checks that fingerprint. If the code
changed and no build or flash is running, the process exits. The unit's
Restart=always starts the new code. A job in progress defers the exit until
the flash has finished.
"""
from __future__ import print_function

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kconfig_menu

HELPER_VERSION = '0.2.12'
# How often the running process compares its code to the startup fingerprint.
# Short enough that an update is picked up before the next flash, long enough
# that the check is not a busy loop. Before-action checks cover the gap.
CODE_CHECK_SECONDS = 15
MAX_BODY = 300 * 1024
MAX_CONFIG = 256 * 1024
NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,40}$')
UUID_RE = re.compile(r'^[0-9a-fA-F]{12}$')
IFACE_RE = re.compile(r'^can[0-9]{1,2}$')
SERIAL_RE = re.compile(
    r'^/dev/(serial/by-(id|path)/[A-Za-z0-9._+-]+|tty(ACM|USB)[0-9]{1,3})$')
CONFIG_LINE_RE = re.compile(r'^CONFIG_[A-Z0-9_]+=')
METHODS = ('katapult_can', 'katapult_usb', 'direct')
BLOCKED_PRINT = ('printing', 'paused')
# argv[0] of every subprocess this process is allowed to start.
# Tests check the runner never sees anything else.

RECOVERY_STOPPED = (
    'Klipper was stopped for flashing, and a start was attempted afterwards. '
    'If this MCU did not reconnect, power-cycle that board. Leave it in the '
    'bootloader only if you are about to flash again. If Klipper itself is '
    'not running, start it with: sudo systemctl start klipper. '
    'Your menuconfig file was not changed. It is still in '
    'printer_data/config/stealthchanger-firmware/.')
RECOVERY_NOT_STOPPED = (
    'Klipper was not stopped, and nothing was flashed. '
    'Your menuconfig file was not changed.')
RECOVERY_VERIFY = (
    'Klipper was stopped for flashing and a start was attempted afterwards, '
    'but this MCU did not come back on the new firmware. '
    'Power-cycle that board if it stayed in the bootloader. '
    'If Klipper is not running, start it with: sudo systemctl start klipper. '
    'Your menuconfig file was not changed.')
RESTART_MESSAGE = 'Firmware helper is restarting to load updated code.'


def _git_dir(repo):
    """Resolve .git for a normal checkout or a worktree gitdir file. No subprocess."""
    git_path = os.path.join(repo, '.git')
    if os.path.isdir(git_path):
        return git_path
    if not os.path.isfile(git_path):
        return None
    try:
        with open(git_path, encoding='utf-8') as fh:
            line = fh.read().strip()
    except OSError:
        return None
    if not line.startswith('gitdir: '):
        return None
    raw = line[len('gitdir: '):].strip()
    if not os.path.isabs(raw):
        raw = os.path.join(repo, raw)
    return raw if os.path.isdir(raw) else None


def _git_head(repo):
    """Commit the checkout is on, or '' when this directory is not a git repo."""
    git_dir = _git_dir(repo)
    if not git_dir:
        return ''
    try:
        with open(os.path.join(git_dir, 'HEAD'), encoding='utf-8') as fh:
            line = fh.read().strip()
    except OSError:
        return ''
    if not line.startswith('ref: '):
        return line
    ref = line[len('ref: '):].strip()
    # ref is like refs/heads/main. Reject anything that would leave git_dir.
    if ref and '..' not in ref.split('/') and not os.path.isabs(ref):
        ref_path = os.path.join(git_dir, ref)
        if os.path.isfile(ref_path):
            try:
                with open(ref_path, encoding='utf-8') as fh:
                    return fh.read().strip()
            except OSError:
                return line
    packed = os.path.join(git_dir, 'packed-refs')
    if os.path.isfile(packed):
        try:
            with open(packed, encoding='utf-8') as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw or raw.startswith('#') or raw.startswith('^'):
                        continue
                    parts = raw.split(' ', 1)
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
        except OSError:
            pass
    return line


def code_fingerprint(firmware_dir):
    """Hash of the helper's own code, taken at startup and checked later.

    Covers every firmware/*.py (path, mtime, size, and contents), a VERSION
    file next to the firmware directory when the repo has one, and git HEAD.
    Menuconfig and build output are outside this directory and do not count:
    saving a .config must not restart the helper.
    """
    firmware_dir = os.path.abspath(firmware_dir)
    repo = os.path.dirname(firmware_dir)
    digest = hashlib.sha256()
    paths = sorted(glob.glob(os.path.join(firmware_dir, '*.py')))
    version_path = os.path.join(repo, 'VERSION')
    if os.path.isfile(version_path):
        paths.append(version_path)
    if not paths:
        digest.update(b'empty')
    for path in paths:
        rel = os.path.relpath(path, repo).replace('\\', '/')
        digest.update(rel.encode('utf-8', 'replace'))
        digest.update(b'\0')
        try:
            st = os.stat(path)
            digest.update(str(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))).encode('ascii'))
            digest.update(b'\0')
            digest.update(str(st.st_size).encode('ascii'))
            digest.update(b'\0')
            with open(path, 'rb') as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b'missing')
        digest.update(b'\n')
    digest.update(b'HEAD\0')
    digest.update(_git_head(repo).encode('utf-8', 'replace'))
    return digest.hexdigest()


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _reject_extra(data, allowed):
    if not isinstance(data, dict):
        raise ApiError(400, 'expected a JSON object')
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ApiError(
            400,
            'unknown field: %s. This service does not run shell commands.' % ', '.join(extra))


def safe_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ApiError(
            400,
            'MCU name must be letters, numbers, "_" and "-" '
            '(so its config file stays inside the firmware folder).')
    return name


def valid_serial(path):
    return isinstance(path, str) and SERIAL_RE.match(path) is not None and '..' not in path


def valid_uuid(value):
    return isinstance(value, str) and UUID_RE.match(value) is not None


def valid_iface(value):
    return isinstance(value, str) and IFACE_RE.match(value) is not None


def inside(root, path):
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def _reported_mcu_name(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_]+', text or '') and len(text) <= 64:
        return text
    return None


def _clock_hz(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _prefill_note(info, reported, clock, link):
    bits = []
    chosen = ' / '.join(part for part in (info.get('arch'), info.get('processor')) if part)
    if chosen:
        bits.append(chosen)
    if info.get('communication'):
        bits.append(info['communication'])
    detail = reported
    if clock:
        detail += ', %d Hz' % clock
    if link == 'can':
        detail += ', CAN'
    elif link == 'usb':
        detail += ', USB'
    text = 'Preselected from the connected MCU (%s)' % detail
    if bits:
        text += ': ' + '; '.join(bits) + '.'
    else:
        text += '.'
    if info.get('communication'):
        text += ' Communication pins are the first matching option, so check them before saving.'
    if info.get('ambiguous_processor'):
        text += ' More than one processor uses this MCU string, so the processor was left unselected.'
    return text


def config_mcu_type(text):
    """Return CONFIG_MCU from a Kconfig file, or None. Never invent one."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('CONFIG_MCU='):
            val = line.split('=', 1)[1].strip().strip('"').strip("'")
            if re.fullmatch(r'[A-Za-z0-9_]+', val or ''):
                return val
            return None
    return None


def direct_kind(config_mcu):
    """How `make flash` talks to this chip, from the user's CONFIG_MCU.

    Returns (kind, log line) or (None, None) when the file does not name
    an STM32 or RP2040. Callers must refuse instead of guessing.
    """
    if not config_mcu:
        return None, None
    m = config_mcu.lower()
    if m.startswith('stm32'):
        return 'dfu', (
            'Direct USB flash for %s: make flash uses the STM32 DFU path '
            '(FLASH_DEVICE is this MCU\'s serial).' % config_mcu)
    if m.startswith('rp2040') or m.startswith('rp235'):
        return 'rp2040', (
            'Direct USB flash for %s: make flash uses the RP2040 boot path '
            '(FLASH_DEVICE is this MCU\'s serial).' % config_mcu)
    return None, None


def _norm_version(value):
    if not value:
        return ''
    text = str(value).strip()
    if text.endswith('-dirty'):
        text = text[:-6]
    return text


def versions_match(host, mcu):
    if not host or not mcu:
        return None
    return _norm_version(host) == _norm_version(mcu)


_GIT_DESC = re.compile(
    r'^v?(?P<maj>\d+)\.(?P<min>\d+)\.(?P<pat>\d+)(?:-(?P<n>\d+)-g[0-9a-fA-F]+)?$')


def version_relation(host, mcu):
    """How the MCU firmware compares with the host Klipper version.

    'unknown' when either side is missing, 'current' when they name the
    same commit, 'behind' when the MCU is older (or the strings differ and
    cannot be ordered), 'ahead' when the MCU is newer.
    """
    if not host or not mcu:
        return 'unknown'
    left = _norm_version(host)
    right = _norm_version(mcu)
    if not left or not right:
        return 'unknown'
    if left == right:
        return 'current'

    def key(text):
        match = _GIT_DESC.match(text)
        if not match:
            return None
        return (
            int(match.group('maj')), int(match.group('min')), int(match.group('pat')),
            int(match.group('n') or 0))

    host_key = key(left)
    mcu_key = key(right)
    if host_key is not None and mcu_key is not None:
        if mcu_key < host_key:
            return 'behind'
        if mcu_key > host_key:
            return 'ahead'
        return 'current'
    # Different and not a git describe we can order. The firmware does not
    # match the host, which is the case the updater is for.
    return 'behind'


def friendly_label(name, tool_nums):
    """Automatic board name. A saved label replaces this."""
    if name == 'mcu':
        return 'Main Board'
    nums = sorted({int(n) for n in (tool_nums or [])})
    if nums:
        return ', '.join('T%d' % n for n in nums)
    return name


def _strip_comment(line):
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            continue
        if ch in '#;':
            break
        out.append(ch)
    return ''.join(out).strip()


_TOOL_HEADER = re.compile(r'^tool\s+T?(\d+)$', re.IGNORECASE)
_EXTRUDER_HEADER = re.compile(r'^extruder\d*$', re.IGNORECASE)


def _include_matches(source_file, spec):
    """Resolve one [include] the way Klipper does.

    The spec is relative to the directory of the file that contains it.
    Globs are expanded. Symlinks are followed by the filesystem when the
    file is opened. This is read-only: a path outside the config directory
    is still opened. Missing files and empty globs yield nothing.
    """
    spec = spec.strip()
    if len(spec) >= 2 and spec[0] == spec[-1] and spec[0] in ('"', "'"):
        spec = spec[1:-1].strip()
    if not spec:
        return []
    dirname = os.path.dirname(os.path.abspath(source_file))
    pattern = spec if os.path.isabs(spec) else os.path.join(dirname, spec)
    try:
        matches = glob.glob(pattern)
    except OSError:
        return []
    found = []
    for path in sorted(matches):
        try:
            if os.path.isfile(path):
                found.append(path)
        except OSError:
            continue
    return found


def _pin_mcu_name(value):
    """MCU named by a Klipper pin, or 'mcu' when the pin has no prefix."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    while text[:1] in '!~^':
        text = text[1:].lstrip()
    if not text or text.lower() in ('none', 'disabled'):
        return None
    if ':' not in text:
        return 'mcu'
    name, pin = text.split(':', 1)
    name = name.strip()
    if not pin.strip() or not NAME_RE.match(name):
        return None
    return name


def parse_printer(config_root, printer_cfg=None):
    """Read MCUs and the tool/extruder pins that name them.

    Includes follow Klipper: relative to the including file, globs, and
    symlinks. Parsing is read-only. The only stops are a file cycle and a
    cap of 200 files. Nothing is skipped for leaving the config directory.
    """
    start = os.path.abspath(printer_cfg or os.path.join(os.path.realpath(config_root), 'printer.cfg'))
    warnings = []
    mcu_sections = []
    tools = []
    # header -> {pin option: mcu name}, later lines win per option
    pin_mcus = {}
    stack = []
    done = set()
    capped = [False]
    count = [0]

    def remember(path):
        try:
            return os.path.abspath(path), os.path.realpath(path)
        except OSError:
            return None, None

    def walk(path):
        abs_path, real = remember(path)
        if abs_path is None:
            warnings.append('could not read %s' % os.path.basename(path))
            return
        if abs_path in stack or real in stack:
            warnings.append('skipped a recursive include of %s' % os.path.basename(path))
            return
        if real in done or abs_path in done:
            return
        if count[0] >= 200:
            if not capped[0]:
                warnings.append('stopped after 200 config files')
                capped[0] = True
            return
        count[0] += 1
        stack.append(abs_path)
        stack.append(real)
        try:
            try:
                text = open(path, encoding='utf-8').read()
            except (OSError, UnicodeError):
                warnings.append('could not read %s' % os.path.basename(path))
                return
            _parse_file(path, text)
        finally:
            stack.pop()
            stack.pop()
            done.add(real)
            done.add(abs_path)

    def _parse_file(path, text):
        current = None
        for raw in text.splitlines():
            line = _strip_comment(raw)
            if not line or raw[:1].isspace():
                continue
            inc = re.match(r'^\[include\s+(.+)]$', line)
            if inc:
                current = None
                for item in _include_matches(path, inc.group(1)):
                    walk(item)
                continue
            sec = re.match(r'^\[([^]]+)]$', line)
            if sec:
                header = sec.group(1).strip()
                current = None
                if header == 'mcu' or header.startswith('mcu '):
                    name = 'mcu' if header == 'mcu' else header[4:].strip()
                    current = {'name': name, 'section': header, 'options': {}, 'file': path, 'kind': 'mcu'}
                    mcu_sections.append(current)
                else:
                    tool = _TOOL_HEADER.match(header)
                    if tool:
                        current = {
                            'num': int(tool.group(1)), 'options': {}, 'file': path, 'kind': 'tool'}
                        tools.append(current)
                    elif _EXTRUDER_HEADER.match(header):
                        current = {'header': header, 'kind': 'pins'}
                        pin_mcus.setdefault(header, {})
            if current is None or ':' not in line:
                continue
            key, val = line.split(':', 1)
            key = key.strip().lower()
            val = val.strip()
            if current.get('kind') == 'mcu':
                if key in ('serial', 'canbus_uuid', 'canbus_interface') and key not in current['options']:
                    current['options'][key] = val
            elif current.get('kind') == 'tool':
                if key not in current['options']:
                    current['options'][key] = val
            elif current.get('kind') == 'pins' and (key == 'pin' or key.endswith('_pin')):
                named = _pin_mcu_name(val)
                if named:
                    pin_mcus.setdefault(current['header'], {})[key] = named
        return

    if os.path.isfile(start):
        walk(start)
    by_name = {}
    order = []
    for sec in mcu_sections:
        if sec['name'] not in by_name:
            order.append(sec['name'])
        by_name[sec['name']] = sec
    mcu_tools = {}
    for tool in tools:
        number = tool['num']
        raw_num = (tool['options'].get('tool_number') or '').strip()
        if re.fullmatch(r'\d+', raw_num or ''):
            number = int(raw_num)
        extruder = (tool['options'].get('extruder') or '').strip()
        if not extruder:
            extruder = 'extruder' if number == 0 else 'extruder%d' % number
        for mcu_name in pin_mcus.get(extruder, {}).values():
            mcu_tools.setdefault(mcu_name, set()).add(number)
    return {
        'sections': [by_name[n] for n in order],
        'warnings': warnings,
        'mcu_tools': mcu_tools,
    }


def parse_mcu_sections(config_root, printer_cfg=None):
    """Read [mcu] sections from printer.cfg and its includes.

    Includes are resolved like Klipper (relative to the including file,
    globs, symlinks) and read wherever they point. The chip type is not
    inferred from the serial path.
    """
    parsed = parse_printer(config_root, printer_cfg)
    return parsed['sections'], parsed['warnings']


def classify_connection(options):
    serial = options.get('serial') or ''
    uuid = options.get('canbus_uuid') or ''
    iface = options.get('canbus_interface') or ''
    if serial.startswith('/tmp/') or 'klipper_host' in serial:
        return 'host'
    has_usb = bool(serial)
    has_can = bool(uuid)
    if has_usb and has_can:
        return 'both'
    if has_can:
        return 'can'
    if has_usb:
        return 'usb'
    return 'none'


def _default_iface(options):
    iface = options.get('canbus_interface') or ''
    if valid_iface(iface):
        return iface, False
    if iface:
        return None, False  # set, but not a name we will pass to flashtool
    return 'can0', True  # Klipper's own default when the option is omitted


class RealRunner(object):
    """subprocess with a list of arguments. shell is never enabled."""

    def run(self, argv, on_line=None):
        if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or a == '' for a in argv):
            raise ApiError(500, 'internal error: refusing an empty or non-string argument list')
        proc = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, shell=False)
        lines = []
        try:
            for line in proc.stdout:
                line = line.rstrip('\n')
                lines.append(line)
                if on_line:
                    on_line(line)
        finally:
            code = proc.wait()
        return code, '\n'.join(lines)


class FirmwareService(object):
    def __init__(self, runner=None, moonraker=None, config_dir=None, build_dir=None, printer_cfg=None,
                 klipper_dir=None, flashtool=None, serial_by_id=None, sudo=None,
                 systemctl=None, python_bin=None, make_bin=None, verify_seconds=None,
                 verify_pause=None, firmware_dir=None, on_code_change=None,
                 code_check_interval=None):
        self.runner = runner or RealRunner()
        self.moonraker = (moonraker or os.environ.get('SC_MOONRAKER_URL') or 'http://127.0.0.1:7125').rstrip('/')
        data = os.environ.get('SC_PRINTER_DATA') or os.path.expanduser('~/printer_data')
        self.config_root = os.path.join(data, 'config')
        self.config_dir = config_dir or os.environ.get('SC_FIRMWARE_CONFIG_DIR') or os.path.join(
            self.config_root, 'stealthchanger-firmware')
        # Object files must not live under config/: Mainsail lists that folder,
        # and Klipper's Makefile joins $(OUT) onto "src/..." so OUT needs a
        # trailing slash. One directory per board, wiped before each build.
        self.build_dir = build_dir or os.environ.get('SC_FIRMWARE_BUILD_DIR') or os.path.join(
            os.path.dirname(self.config_root), 'stealthchanger-firmware-build')
        self.printer_cfg = printer_cfg or os.environ.get('SC_PRINTER_CFG') or os.path.join(
            self.config_root, 'printer.cfg')
        self.klipper_dir = klipper_dir or os.environ.get('SC_KLIPPER_DIR') or os.path.expanduser('~/klipper')
        self.flashtool = flashtool if flashtool is not None else self._find_flashtool()
        self.serial_by_id = serial_by_id or os.environ.get('SC_SERIAL_BY_ID') or '/dev/serial/by-id'
        self.sudo = sudo or os.environ.get('SC_SUDO') or 'sudo'
        self.systemctl = systemctl or os.environ.get('SC_SYSTEMCTL') or 'systemctl'
        self.python_bin = python_bin or os.environ.get('SC_PYTHON') or sys.executable
        self.make_bin = make_bin or os.environ.get('SC_MAKE') or 'make'
        self.verify_seconds = int(verify_seconds if verify_seconds is not None else os.environ.get('SC_VERIFY_SECONDS', '45'))
        self.verify_pause = float(verify_pause if verify_pause is not None else os.environ.get('SC_VERIFY_PAUSE', '1'))
        self._lock = threading.Lock()
        self._job = None
        # Code the process actually loaded. A later Moonraker update changes
        # the files under this directory; the running interpreter does not.
        self._firmware_dir = os.path.abspath(
            firmware_dir or os.path.dirname(os.path.abspath(__file__)))
        self._code_fp = code_fingerprint(self._firmware_dir)
        self._on_code_change = on_code_change
        self._exit_requested = False
        self._reload_deferred = False
        self._restart_started = False
        self.code_check_interval = float(
            CODE_CHECK_SECONDS if code_check_interval is None else code_check_interval)
        self._watch_stop = None
        self._watch_thread = None

    @property
    def exit_requested(self):
        with self._lock:
            return self._exit_requested

    def poll_code_change(self):
        """Return 'unchanged', 'deferred', or 'exit'.

        'exit' means the code on disk no longer matches the process and no
        build or flash is running, so the process should stop. 'deferred'
        means it changed during a job: the exit waits until the job ends.
        A fingerprint that cannot be read leaves the process running.
        """
        try:
            current = code_fingerprint(self._firmware_dir)
        except Exception as exc:
            sys.stderr.write('firmware-helper could not fingerprint its code: %s\n' % exc)
            return 'unchanged'
        with self._lock:
            if current == self._code_fp:
                self._reload_deferred = False
                return 'unchanged'
            if self._job:
                self._reload_deferred = True
                return 'deferred'
            self._exit_requested = True
            return 'exit'

    def _begin_job(self, job):
        with self._lock:
            if self._exit_requested or self._reload_deferred:
                raise ApiError(503, RESTART_MESSAGE + ' Nothing was flashed.')
            if self._job:
                raise ApiError(409, 'A firmware job is already running for %s.' % self._job.get('mcu'))
            self._job = job

    def _end_job(self):
        with self._lock:
            self._job = None
        return self.poll_code_change()

    def trigger_restart(self):
        """Run the exit callback once, and only after an exit was requested."""
        with self._lock:
            if not self._exit_requested or self._restart_started:
                return False
            self._restart_started = True
            callback = self._on_code_change
        if callback:
            callback()
        return True

    def start_code_watch(self):
        """Check the fingerprint on CODE_CHECK_SECONDS (or the test interval)."""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop = threading.Event()

        def loop():
            while not self._watch_stop.wait(self.code_check_interval):
                self.poll_code_change()
                if self.exit_requested:
                    self.trigger_restart()
                    return

        self._watch_thread = threading.Thread(target=loop, name='firmware-code-watch', daemon=True)
        self._watch_thread.start()

    def stop_code_watch(self):
        if self._watch_stop:
            self._watch_stop.set()
        thread = self._watch_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _find_flashtool(self):
        env = os.environ.get('SC_FLASHTOOL')
        if env:
            return env if os.path.isfile(env) else None
        home = os.path.expanduser('~')
        for folder in ('katapult', 'Katapult', 'CanBoot'):
            path = os.path.join(home, folder, 'scripts', 'flashtool.py')
            if os.path.isfile(path):
                return path
        return None

    # ----- moonraker -----
    def _moon(self, path, timeout=5):
        url = self.moonraker + path
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, OSError) as exc:
            raise ApiError(503, 'Moonraker did not answer (%s). %s' % (exc.__class__.__name__, url))

    def print_state(self):
        """Return the lowercase print_stats.state, or raise if it cannot be read."""
        data = self._moon('/printer/objects/query?print_stats')
        try:
            state = data['result']['status']['print_stats']['state']
        except (KeyError, TypeError):
            raise ApiError(
                503,
                'Moonraker did not return print_stats.state. '
                'Flashing is refused because a print might be running.')
        if not isinstance(state, str) or not state.strip():
            raise ApiError(
                503,
                'print_stats.state was empty. Flashing is refused because a print might be running.')
        return state.strip().lower()

    def _print_block(self, state):
        if state in BLOCKED_PRINT:
            return (
                'Firmware update refused: the printer is %s. '
                'Wait until the print is idle (print_stats.state is not printing or paused).' % state)
        return None

    def assert_idle(self, when):
        state = self.print_state()
        msg = self._print_block(state)
        if msg:
            raise ApiError(409, msg + ' Checked ' + when + '.')
        return state

    def _host_and_versions(self):
        """Host version, klippy state, per-MCU version, and mcu_constants.

        mcu_constants supplies the MCU string and CLOCK_FREQ the connected
        board actually reported. A missing Moonraker answer leaves the maps
        empty; callers still show the boards from printer.cfg.
        """
        host = None
        klippy = None
        versions = {}
        reports = {}
        try:
            info = self._moon('/printer/info')
            result = info.get('result') or {}
            host = result.get('software_version') or None
            klippy = result.get('state') or None
        except ApiError:
            return host, klippy, versions, reports
        try:
            listed = self._moon('/printer/objects/list')
            names = []
            for obj in (listed.get('result') or {}).get('objects') or []:
                if obj == 'mcu' or (isinstance(obj, str) and obj.startswith('mcu ')):
                    names.append(obj)
            if not names:
                return host, klippy, versions, reports
            query = '&'.join(urllib.parse.quote(n) for n in names)
            data = self._moon('/printer/objects/query?' + query)
            status = (data.get('result') or {}).get('status') or {}
            for obj in names:
                key = 'mcu' if obj == 'mcu' else obj.split(' ', 1)[1]
                entry = status.get(obj) or {}
                ver = entry.get('mcu_version')
                if isinstance(ver, str) and ver.strip():
                    versions[key] = ver.strip()
                consts = entry.get('mcu_constants')
                if isinstance(consts, dict):
                    mcu_name = _reported_mcu_name(consts.get('MCU'))
                    clock = _clock_hz(consts.get('CLOCK_FREQ'))
                    if mcu_name or clock is not None:
                        reports[key] = {'mcu': mcu_name, 'clock': clock}
        except ApiError:
            pass
        return host, klippy, versions, reports

    # ----- discovery -----
    def _stored_method(self, name):
        path = os.path.join(self.config_dir, name + '.method')
        try:
            text = open(path, encoding='utf-8').read().strip()
        except OSError:
            return None
        return text if text in METHODS else None

    def _stored_config(self, name):
        path = os.path.join(self.config_dir, name + '.config')
        try:
            text = open(path, encoding='utf-8').read()
        except OSError:
            return None, None
        return text, config_mcu_type(text)

    def _katapult_detected(self):
        return bool(self.flashtool) and os.path.isfile(self.flashtool)

    def _auto_method(self, conn, serial, choices, stored):
        """Saved choice, otherwise Katapult CAN for a canbus_uuid, and for a
        /dev/serial/by-id path Katapult USB when Katapult is installed else direct.
        """
        if stored in choices:
            return stored
        if conn in ('can', 'both') and 'katapult_can' in choices:
            return 'katapult_can'
        by_id = isinstance(serial, str) and serial.startswith('/dev/serial/by-id/')
        if conn == 'usb' and by_id:
            if self._katapult_detected() and 'katapult_usb' in choices:
                return 'katapult_usb'
            if 'direct' in choices:
                return 'direct'
        return None

    def _load_labels(self):
        path = os.path.join(self.config_dir, 'labels.json')
        try:
            raw = open(path, encoding='utf-8').read()
        except OSError:
            return {}, None
        try:
            data = json.loads(raw)
        except ValueError:
            return None, 'labels.json could not be read. Board names are the automatic ones.'
        if not isinstance(data, dict):
            return None, 'labels.json could not be read. Board names are the automatic ones.'
        out = {}
        for key, val in data.items():
            if isinstance(key, str) and isinstance(val, str) and NAME_RE.match(key):
                text = ' '.join(val.split())
                if text:
                    out[key] = text[:48]
        return out, None

    def describe(self):
        sections, warnings, mcu_tools = [], [], {}
        cfg_error = None
        if os.path.isfile(self.printer_cfg):
            try:
                parsed = parse_printer(os.path.dirname(self.printer_cfg), self.printer_cfg)
                sections = parsed['sections']
                warnings = list(parsed['warnings'])
                mcu_tools = parsed['mcu_tools']
            except ApiError as exc:
                cfg_error = exc.message
        else:
            cfg_error = 'printer.cfg was not found at %s' % self.printer_cfg
        labels, label_warning = self._load_labels()
        if label_warning:
            warnings.append(label_warning)
        if labels is None:
            labels = {}
        try:
            print_state = self.print_state()
            print_message = self._print_block(print_state)
        except ApiError as exc:
            print_state = 'unknown'
            print_message = exc.message
        host, klippy, versions, reports = self._host_and_versions()
        klipper_ok = os.path.isdir(self.klipper_dir) and os.path.isfile(os.path.join(self.klipper_dir, 'Makefile'))
        mcus = []
        for sec in sections:
            mcus.append(self._describe_one(sec, host, versions, reports, klipper_ok, labels, mcu_tools))
        with self._lock:
            job = dict(self._job) if self._job else None
        return {
            'ok': True,
            'helper': 'stealthchanger-firmware',
            'version': HELPER_VERSION,
            'config_dir': self.config_dir,
            'build_dir': self.build_dir,
            'print_state': print_state,
            'print_blocked': print_state in BLOCKED_PRINT or print_state == 'unknown',
            'print_message': print_message,
            'host_version': host,
            'klippy_state': klippy,
            'klipper_dir': self.klipper_dir,
            'klipper_dir_ok': klipper_ok,
            'flashtool': self.flashtool,
            'config_warnings': warnings,
            'config_error': cfg_error,
            'job': job,
            'mcus': mcus,
        }

    def _describe_one(self, sec, host, versions, reports, klipper_ok, labels=None, mcu_tools=None):
        name = sec['name']
        options = sec['options']
        conn = classify_connection(options)
        serial = options.get('serial') or None
        uuid = options.get('canbus_uuid') or None
        iface, iface_default = _default_iface(options)
        if options.get('canbus_interface') and not valid_iface(options.get('canbus_interface')):
            iface = None
        name_ok = isinstance(name, str) and NAME_RE.match(name) is not None
        text = None
        config_mcu = None
        stored = None
        if name_ok:
            text, config_mcu = self._stored_config(name)
            stored = self._stored_method(name)
        choices = self._method_choices(conn)
        method = self._auto_method(conn, serial, choices, stored)
        kind = None
        if config_mcu and method == 'direct':
            kind, _line = direct_kind(config_mcu)
        version = versions.get(name) if name_ok else None
        report = ((reports or {}).get(name) or {}) if name_ok else {}
        reported_mcu = report.get('mcu')
        reported_clock = report.get('clock')
        config_mismatch = None
        if config_mcu and reported_mcu:
            config_mismatch = config_mcu.lower() != reported_mcu.lower()
        block = self._block_reason(
            name_ok, conn, serial, uuid, iface, method, choices, text, config_mcu, kind, klipper_ok)
        match = versions_match(host, version)
        relation = version_relation(host, version)
        derived = friendly_label(name if name_ok else str(name), (mcu_tools or {}).get(name))
        custom = (labels or {}).get(name) if name_ok else None
        return {
            'name': name if name_ok else str(name),
            'section': sec['section'],
            'label': custom or derived,
            'label_derived': derived,
            'label_custom': custom,
            'connection': conn,
            'serial': serial,
            'canbus_uuid': uuid,
            'canbus_interface': iface,
            'canbus_interface_default': bool(iface_default and conn in ('can', 'both')),
            'version': version,
            'version_mismatch': (not match) if match is not None else None,
            'update_state': relation,
            'method': method,
            'method_choices': list(choices),
            'config_saved': bool(text),
            'config_mcu': config_mcu,
            'reported_mcu': reported_mcu,
            'reported_clock': reported_clock,
            'config_mcu_mismatch': config_mismatch,
            'direct_kind': kind,
            'flashable': block is None,
            'block_reason': block,
        }

    def _method_choices(self, conn):
        if conn == 'can':
            return ['katapult_can']
        if conn == 'usb':
            return ['katapult_usb', 'direct']
        if conn == 'both':
            return ['katapult_can', 'katapult_usb', 'direct']
        return []

    def _block_reason(self, name_ok, conn, serial, uuid, iface, method, choices, text, config_mcu, kind, klipper_ok):
        if not name_ok:
            return 'This MCU name cannot be used as a config file name. Rename the [mcu] section.'
        if conn == 'host':
            return 'This is the Linux host MCU. This updater flashes USB and CAN boards only.'
        if conn == 'none':
            return 'This [mcu] section has no serial and no canbus_uuid.'
        if conn in ('usb', 'both') and serial and not valid_serial(serial):
            return ('The serial path in this [mcu] section is not a /dev/serial/by-id or tty device '
                    'this helper will open.')
        if conn in ('can', 'both') and uuid and not valid_uuid(uuid):
            return 'canbus_uuid must be 12 hex digits. Nothing was guessed from the serial path.'
        if not klipper_ok:
            return 'Klipper source was not found at %s (need its Makefile).' % self.klipper_dir
        if not text:
            return ('Import a Klipper menuconfig .config for this MCU. '
                    'The updater will not guess the chip.')
        if not config_mcu:
            return ('The saved .config does not set CONFIG_MCU. '
                    'Import the menuconfig file for this board. The updater will not guess the chip.')
        if not method:
            return ('Choose Katapult over USB or direct USB flashing for this MCU. '
                    'The updater will not guess.')
        if method not in choices:
            return 'That flash method does not match how this MCU is connected.'
        if method == 'katapult_can':
            if not valid_uuid(uuid or ''):
                return 'canbus_uuid must be 12 hex digits. Nothing was guessed from the serial path.'
            if not iface:
                return 'canbus_interface is not a name like can0. Set it in the [mcu] section.'
            if not self.flashtool:
                return 'Katapult flashtool.py was not found. Clone Katapult to ~/katapult (scripts/flashtool.py).'
        if method == 'katapult_usb':
            if not valid_serial(serial or ''):
                return 'The serial path in this [mcu] section is not a /dev/serial/by-id or tty device this helper will open.'
            if not self.flashtool:
                return 'Katapult flashtool.py was not found. Clone Katapult to ~/katapult (scripts/flashtool.py).'
        if method == 'direct':
            if not valid_serial(serial or ''):
                return 'The serial path in this [mcu] section is not a /dev/serial/by-id or tty device this helper will open.'
            if not kind:
                return (
                    'CONFIG_MCU is %s. Direct USB flashing here is STM32 DFU or RP2040 boot, '
                    'detected from that line. This value is neither, so nothing will be flashed. '
                    'The updater will not guess. Import an STM32 or RP2040 .config, or choose Katapult over USB.'
                    % config_mcu)
        return None

    def _section(self, name):
        safe_name(name)
        sections, _warnings = parse_mcu_sections(os.path.dirname(self.printer_cfg), self.printer_cfg)
        for sec in sections:
            if sec['name'] == name:
                return sec
        raise ApiError(404, 'No [mcu] section named %s in the Klipper config.' % name)

    def _mkdir_config(self):
        os.makedirs(self.config_dir, exist_ok=True)

    def save_config(self, data):
        _reject_extra(data, ('mcu', 'config'))
        name = safe_name(data.get('mcu'))
        self._section(name)  # must be a real MCU; do not create arbitrary files
        text = data.get('config')
        if not isinstance(text, str):
            raise ApiError(400, 'config must be the text of a Kconfig .config file.')
        if '\x00' in text:
            raise ApiError(400, 'config must be a text .config file, not a binary.')
        raw = text.encode('utf-8')
        if len(raw) > MAX_CONFIG:
            raise ApiError(400, 'config is larger than 256KB.')
        if not any(CONFIG_LINE_RE.match(line.strip()) for line in text.splitlines()):
            raise ApiError(400, 'That file has no CONFIG_ lines. Import a Klipper menuconfig .config.')
        mcu_type = config_mcu_type(text)
        if not mcu_type:
            raise ApiError(
                400,
                'That .config does not set CONFIG_MCU. Import the menuconfig file for this board. '
                'The updater will not guess the chip.')
        self._mkdir_config()
        path = os.path.join(self.config_dir, name + '.config')
        if not inside(self.config_dir, path):
            raise ApiError(400, 'refusing to write a config outside the firmware folder.')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(text if text.endswith('\n') else text + '\n')
        os.replace(tmp, path)
        return {'ok': True, 'mcu': name, 'config_mcu': mcu_type, 'path': path}

    def set_method(self, data):
        _reject_extra(data, ('mcu', 'method'))
        name = safe_name(data.get('mcu'))
        method = data.get('method')
        if method not in METHODS:
            raise ApiError(400, 'method must be one of: %s.' % ', '.join(METHODS))
        sec = self._section(name)
        choices = self._method_choices(classify_connection(sec['options']))
        if method not in choices:
            raise ApiError(
                400,
                'This MCU is connected as %s, so the flash method has to be one of: %s.' % (
                    classify_connection(sec['options']), ', '.join(choices) or 'none'))
        self._mkdir_config()
        path = os.path.join(self.config_dir, name + '.method')
        if not inside(self.config_dir, path):
            raise ApiError(400, 'refusing to write outside the firmware folder.')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(method + '\n')
        return {'ok': True, 'mcu': name, 'method': method}

    def set_label(self, data):
        _reject_extra(data, ('mcu', 'label'))
        name = safe_name(data.get('mcu'))
        self._section(name)
        label = data.get('label')
        if label is None:
            label = ''
        if not isinstance(label, str):
            raise ApiError(400, 'label must be text.')
        label = ' '.join(label.split())
        if len(label) > 48:
            raise ApiError(400, 'A label can be at most 48 characters.')
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
            raise ApiError(400, 'A label must be plain text.')
        labels, warning = self._load_labels()
        if labels is None:
            raise ApiError(400, warning or 'labels.json could not be read, so it was not overwritten.')
        if label:
            labels[name] = label
        else:
            labels.pop(name, None)
        self._mkdir_config()
        path = os.path.join(self.config_dir, 'labels.json')
        if not inside(self.config_dir, path):
            raise ApiError(400, 'refusing to write outside the firmware folder.')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(labels, fh, indent=2, sort_keys=True)
            fh.write('\n')
        os.replace(tmp, path)
        return {'ok': True, 'mcu': name, 'label': label or None, 'path': path}

    def copy_config(self, data):
        """Copy one board's saved .config onto other boards from printer.cfg.

        overwrite must be true when a target already has a file. Names are
        checked the same way as a save, so the request cannot write outside
        the firmware folder or invent a board.
        """
        _reject_extra(data, ('mcu', 'targets', 'overwrite'))
        src = safe_name(data.get('mcu'))
        self._section(src)
        text, mcu_type = self._stored_config(src)
        if not text or not mcu_type:
            raise ApiError(400, 'This board has no saved .config to copy.')
        targets = data.get('targets')
        if not isinstance(targets, list) or not targets:
            raise ApiError(400, 'Choose at least one other board.')
        if len(targets) > 32:
            raise ApiError(400, 'Too many boards.')
        overwrite = data.get('overwrite', False)
        if overwrite is not True and overwrite is not False:
            raise ApiError(400, 'overwrite must be true or false.')
        names = []
        for item in targets:
            name = safe_name(item)
            if name == src:
                raise ApiError(400, 'Choose other boards. A .config is not copied onto itself.')
            if name in names:
                raise ApiError(400, 'That board is listed twice.')
            self._section(name)
            names.append(name)
        existing = [name for name in names if self._stored_config(name)[0]]
        if existing and overwrite is not True:
            raise ApiError(
                409,
                'These boards already have a .config: %s. Nothing was copied.' % ', '.join(existing))
        self._mkdir_config()
        copied = []
        for name in names:
            path = os.path.join(self.config_dir, name + '.config')
            if not inside(self.config_dir, path):
                raise ApiError(400, 'refusing to write a config outside the firmware folder.')
            tmp = path + '.tmp'
            payload = text if text.endswith('\n') else text + '\n'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(payload)
            os.replace(tmp, path)
            copied.append({'mcu': name, 'config_mcu': mcu_type, 'path': path})
        return {'ok': True, 'mcu': src, 'config_mcu': mcu_type, 'copied': copied}

    def _kconfig_form(self, data, write):
        """Load Klipper's Kconfig and either describe the form or write .config.

        The only output path is this board's file in the firmware folder.
        This does not build or flash.
        """
        _reject_extra(data, ('mcu', 'base', 'values'))
        name = safe_name(data.get('mcu'))
        self._section(name)
        text, _mcu_type = self._stored_config(name)
        base = data.get('base')
        if base is None:
            base = 'saved' if text else 'defaults'
        if base not in ('saved', 'defaults'):
            raise ApiError(400, 'base must be "saved" or "defaults".')
        if base == 'saved' and not text:
            raise ApiError(400, 'This board has no saved .config yet. Create one from the defaults.')
        values = data.get('values', {})
        if not isinstance(values, dict):
            raise ApiError(400, 'values must be an object of Kconfig symbol names.')
        config_path = None
        if base == 'saved':
            config_path = os.path.join(self.config_dir, name + '.config')
            if not inside(self.config_dir, config_path):
                raise ApiError(400, 'refusing to read a config outside the firmware folder.')
        try:
            kconf, mod = kconfig_menu.load_kconfig(
                self.klipper_dir, config_path, values or None)
            prefill = None
            if not values and base == 'defaults':
                prefill = self._apply_prefill(name, kconf, mod)
            form = kconfig_menu.schema(kconf, mod)
        except kconfig_menu.KconfigMenuError as exc:
            raise ApiError(400, exc.message)
        if not write:
            body = {
                'ok': True,
                'mcu': name,
                'base': base,
                'config_mcu': form.get('config_mcu'),
                'fields': form['fields'],
                'wrote': False,
            }
            if prefill:
                body['prefill'] = prefill['values']
                body['prefill_note'] = prefill['note']
                body['reported_mcu'] = prefill.get('reported_mcu')
                body['reported_clock'] = prefill.get('reported_clock')
            return body
        self._mkdir_config()
        path = os.path.join(self.config_dir, name + '.config')
        tmp = path + '.tmp'
        if not inside(self.config_dir, path) or not inside(self.config_dir, tmp):
            raise ApiError(400, 'refusing to write a config outside the firmware folder.')
        try:
            written = kconfig_menu.write_config_text(kconf, tmp)
        except OSError as exc:
            raise ApiError(500, 'Could not write the .config (%s). Nothing was flashed.' % exc.__class__.__name__)
        mcu_type = config_mcu_type(written)
        if not mcu_type:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ApiError(
                400,
                'That menuconfig does not set CONFIG_MCU. Nothing was written. '
                'The updater will not guess the chip.')
        os.replace(tmp, path)
        return {
            'ok': True,
            'mcu': name,
            'base': base,
            'config_mcu': mcu_type,
            'path': path,
            'fields': form['fields'],
            'wrote': True,
        }

    def _link_kind(self, name):
        sec = self._section(name)
        conn = classify_connection(sec['options'])
        if conn in ('can', 'both'):
            return 'can'
        if conn == 'usb':
            return 'usb'
        return None

    def _apply_prefill(self, name, kconf, mod):
        """Select Kconfig symbols from the MCU Klipper already reported.

        An empty Create form is the only caller. A saved .config, or any
        values the page already sent, is left alone. Moonraker being down
        just means the form opens on Kconfig's own defaults.
        """
        _host, _klippy, _versions, reports = self._host_and_versions()
        report = reports.get(name) or {}
        reported = report.get('mcu')
        if not reported:
            return None
        clock = report.get('clock')
        link = self._link_kind(name)
        info = kconfig_menu.suggest_initial(kconf, mod, reported, clock, link)
        if not info:
            return None
        info = dict(info)
        info['note'] = _prefill_note(info, reported, clock, link)
        info['reported_mcu'] = reported
        info['reported_clock'] = clock
        return info

    def kconfig_schema(self, data):
        return self._kconfig_form(data, write=False)

    def kconfig_save(self, data):
        return self._kconfig_form(data, write=True)

    # ----- flash -----
    def _run(self, argv, log, label):
        log('LOG ' + label)
        log('LOG $ ' + ' '.join(argv))
        code, _out = self.runner.run(argv, on_line=lambda line: log('LOG ' + line))
        if code != 0:
            raise ApiError(500, '%s failed (exit %s).' % (label, code))
        return code

    def _systemctl(self, verb, log):
        if verb not in ('stop', 'start', 'is-active'):
            raise ApiError(500, 'refusing systemctl %s' % verb)
        argv = [self.sudo, '-n', self.systemctl, verb, 'klipper']
        log('LOG $ ' + ' '.join(argv))
        code, out = self.runner.run(argv, on_line=lambda line: log('LOG ' + line))
        return code, out

    def _board_out(self, name, clean):
        """This board's build directory, always with a trailing slash.

        Klipper's Makefile compiles $(OUT)src/sched.o. Without the slash that
        becomes <board>src/sched.o and the build cannot find autoconf.h.
        clean wipes just this board so a previous .config cannot linger.
        """
        name = safe_name(name)
        os.makedirs(self.build_dir, exist_ok=True)
        raw = os.path.join(self.build_dir, name)
        if not inside(self.build_dir, raw):
            raise ApiError(500, 'refusing to use a build directory outside the firmware build folder.')
        if clean:
            if os.path.islink(raw):
                os.unlink(raw)
            elif os.path.isdir(raw):
                shutil.rmtree(raw)
            elif os.path.lexists(raw):
                os.remove(raw)
            os.makedirs(raw)
        elif os.path.islink(raw):
            raise ApiError(500, 'refusing to use a build directory outside the firmware build folder.')
        if not inside(self.build_dir, raw):
            raise ApiError(500, 'refusing to use a build directory outside the firmware build folder.')
        path = os.path.realpath(raw) if os.path.isdir(raw) else raw
        if not path.endswith('/'):
            path += '/'
        return path

    def _out_dir(self, name):
        return self._board_out(name, clean=False)

    def _config_path(self, name):
        path = os.path.join(self.config_dir, name + '.config')
        if not os.path.isfile(path) or not inside(self.config_dir, path):
            raise ApiError(400, 'Saved .config is missing for %s.' % name)
        return path

    def _build(self, name, log):
        out = self._board_out(name, clean=True)
        cfg = self._config_path(name)
        argv = [self.make_bin, '-C', self.klipper_dir,
                'KCONFIG_CONFIG=' + cfg, 'OUT=' + out, '-j4']
        self._run(argv, log, 'build Klipper for %s' % name)
        return out

    def _firmware_file(self, out):
        root = os.path.realpath(out)
        for fname in ('klipper.bin', 'klipper.uf2'):
            path = os.path.join(out, fname)
            if os.path.isfile(path) and inside(root, path):
                return os.path.realpath(path)
        raise ApiError(500, 'The build did not produce klipper.bin or klipper.uf2 in the OUT directory.')

    def _flashtool(self, args, log, label):
        if not self.flashtool or not os.path.isfile(self.flashtool):
            raise ApiError(500, 'Katapult flashtool.py is not available.')
        argv = [self.python_bin, self.flashtool] + list(args)
        self._run(argv, log, label)

    def _safe_by_id(self, path):
        """A device path this process listed inside the by-id directory.

        The HTTP client never supplies this path. The basename is the same
        shape as a real /dev/serial/by-id name, and the real path has to
        stay inside that directory.
        """
        base = os.path.basename(path or '')
        if not re.fullmatch(r'[A-Za-z0-9._+-]+', base):
            return False
        return os.path.isdir(self.serial_by_id) and inside(self.serial_by_id, path)

    def _usable_device(self, path):
        return valid_serial(path) or self._safe_by_id(path)

    def _resolve_serial(self, serial):
        if not valid_serial(serial):
            raise ApiError(400, 'serial path failed validation. Nothing was flashed.')
        if os.path.exists(serial):
            return serial
        alt = os.path.join(self.serial_by_id, os.path.basename(serial))
        if self._safe_by_id(alt) and os.path.exists(alt):
            return os.path.realpath(alt)
        return serial

    def _list_by_id(self):
        folder = self.serial_by_id
        if not os.path.isdir(folder):
            return []
        found = []
        for name in sorted(os.listdir(folder)):
            if name.startswith('.') or not re.fullmatch(r'[A-Za-z0-9._+-]+', name):
                continue
            path = os.path.join(folder, name)
            if self._usable_device(path):
                found.append(path)
        return found

    def _serial_token(self, serial):
        base = os.path.basename(serial)
        match = re.search(r'_([0-9A-Fa-f]{8,})', base)
        return match.group(1).lower() if match else None

    def _katapult_matches(self, token):
        matches = []
        for path in self._list_by_id():
            low = os.path.basename(path).lower()
            if 'katapult' not in low and 'canboot' not in low:
                continue
            if token and token not in low:
                continue
            if self._usable_device(path):
                matches.append(path)
        return matches

    def _flash_can(self, desc, fw, log):
        iface = desc['canbus_interface']
        uuid = desc['canbus_uuid']
        if not valid_iface(iface) or not valid_uuid(uuid):
            raise ApiError(400, 'CAN interface or uuid failed validation. Nothing was flashed.')
        if not inside(self.build_dir, fw):
            raise ApiError(500, 'firmware file is outside the build directory. Nothing was flashed.')
        self._flashtool(['-i', iface, '-u', uuid, '-r'], log, 'request Katapult bootloader over CAN')
        self._flashtool(['-i', iface, '-u', uuid, '-f', fw], log, 'flash Katapult over CAN')

    def _flash_usb_katapult(self, desc, fw, log):
        serial = self._resolve_serial(desc['serial'])
        if not self._usable_device(serial):
            raise ApiError(400, 'serial path failed validation. Nothing was flashed.')
        if not inside(self.build_dir, fw):
            raise ApiError(500, 'firmware file is outside the build directory. Nothing was flashed.')
        before = set(self._list_by_id())
        token = self._serial_token(desc['serial'])
        requested = False
        if os.path.exists(serial):
            try:
                self._flashtool(['-d', serial, '-r'], log, 'request Katapult bootloader over USB')
                requested = True
            except ApiError as exc:
                log('LOG bootloader request failed: %s' % exc.message)
        else:
            log('LOG %s is not present; looking for a Katapult USB device' % serial)
        matches = self._katapult_matches(token)
        if token is None:
            fresh = [p for p in matches if p not in before]
            matches = fresh if fresh else matches
        if len(matches) != 1:
            raise ApiError(
                500,
                'Expected one Katapult USB device for this MCU and found %d. '
                'Nothing else was flashed. Put only this board in the bootloader and try again.' % len(matches))
        target = matches[0]
        if not self._usable_device(target):
            raise ApiError(500, 'Katapult device path failed validation. Nothing was flashed.')
        log('LOG Katapult device: ' + target + (' (bootloader was requested)' if requested else ''))
        self._flashtool(['-d', target, '-f', fw], log, 'flash Katapult over USB')

    def _flash_direct(self, desc, name, log):
        serial = desc['serial']
        kind, line = direct_kind(desc.get('config_mcu'))
        if not kind or not line:
            raise ApiError(
                400,
                'Direct flashing needs CONFIG_MCU to be an STM32 or RP2040. '
                'The updater will not guess.')
        device = self._resolve_serial(serial)
        if not self._usable_device(device):
            raise ApiError(400, 'serial path failed validation. Nothing was flashed.')
        log('LOG ' + line)
        out = self._out_dir(name)
        cfg = self._config_path(name)
        argv = [self.make_bin, '-C', self.klipper_dir, 'flash',
                'FLASH_DEVICE=' + device, 'KCONFIG_CONFIG=' + cfg, 'OUT=' + out]
        self._run(argv, log, 'direct USB flash (%s)' % kind)

    def _verify(self, name, before, log):
        deadline = time.time() + self.verify_seconds
        last = None
        while time.time() < deadline:
            host, klippy, versions, _reports = self._host_and_versions()
            last = versions.get(name)
            log('LOG Klipper %s, %s version %s' % (klippy or '?', name, last or '(not connected)'))
            if last:
                changed = before is None or last != before
                match = versions_match(host, last)
                if not changed:
                    raise ApiError(
                        500,
                        'The MCU reconnected but the firmware version did not change (still %s).' % last)
                if match is False:
                    log('LOG version mismatch: MCU %s, host Klipper %s' % (last, host))
                log('DONE mcu=%s version=%s host=%s mismatch=%s' % (
                    name, last, host or '', '1' if match is False else '0'))
                return {'version': last, 'host': host, 'mismatch': match is False}
            time.sleep(self.verify_pause)
        raise ApiError(
            500,
            'Klipper was started, but %s did not report a firmware version in time.' % name)

    def flash(self, data, log):
        """Flash one MCU. log(line) receives streamed text lines.

        print_stats is checked before any build and again immediately
        before Klipper is stopped. confirm must be JSON true.
        """
        _reject_extra(data, ('mcu', 'confirm'))
        if data.get('confirm') is not True:
            raise ApiError(400, 'Flash requires confirm: true. Nothing was flashed.')
        name = safe_name(data.get('mcu'))
        self.assert_idle('before starting')
        # describe() also queries print state; the explicit check above is the gate.
        desc = None
        for item in self.describe()['mcus']:
            if item['name'] == name:
                desc = item
                break
        if desc is None:
            raise ApiError(404, 'No [mcu] section named %s.' % name)
        if not desc['flashable']:
            raise ApiError(400, desc['block_reason'] or 'This MCU cannot be flashed.')
        self._begin_job({'mcu': name, 'action': 'flash'})
        phase = 'idle'
        before = desc.get('version')
        try:
            log('LOG flash %s via %s' % (name, desc['method']))
            # Build while Klipper is still running (separate OUT directory).
            # The print-state check below is the last gate before it is stopped.
            self._build(name, log)
            fw = self._firmware_file(self._out_dir(name))
            self.assert_idle('right before flashing')
            code, _out = self._systemctl('stop', log)
            if code != 0:
                raise ApiError(
                    500,
                    'Could not stop Klipper (sudo -n systemctl stop klipper failed). '
                    'Allow that command for this user. Nothing was flashed.')
            phase = 'stopped'
            log('LOG Klipper is stopped')
            self._flash_image(desc, name, fw, log)
            self._require_started(log)
            phase = 'started'
            return self._verify(name, before, log)
        except Exception:
            if phase == 'stopped':
                log('LOG flashing failed; starting Klipper again')
                try:
                    self._systemctl('start', log)
                except Exception as exc:
                    log('LOG could not start Klipper: %s' % exc)
                log('RECOVERY ' + RECOVERY_STOPPED)
            elif phase == 'started':
                log('RECOVERY ' + RECOVERY_VERIFY)
            else:
                log('RECOVERY ' + RECOVERY_NOT_STOPPED)
            raise
        finally:
            self._end_job()

    def _flash_image(self, desc, name, fw, log):
        if desc['method'] == 'katapult_can':
            self._flash_can(desc, fw, log)
        elif desc['method'] == 'katapult_usb':
            self._flash_usb_katapult(desc, fw, log)
        elif desc['method'] == 'direct':
            self._flash_direct(desc, name, log)
        else:
            raise ApiError(400, 'Unknown flash method. Nothing was flashed.')

    def _require_started(self, log):
        code, _out = self._systemctl('start', log)
        if code != 0:
            raise ApiError(
                500,
                'Could not start Klipper (sudo -n systemctl start klipper failed). '
                'The board may already be flashed. Start Klipper, then check the MCU.')
        log('LOG Klipper start requested')

    def _assert_idle_or_stopped(self, name, log):
        """Print check before flashing one board of a batch.

        Once Klipper has been stopped, Moonraker may not answer. A print
        cannot be running then, so a failed read is not a refusal. An
        explicit printing or paused state still refuses.
        """
        try:
            state = self.print_state()
        except ApiError as exc:
            log('LOG print state before %s could not be read (%s). '
                'Klipper is stopped, so this is not a running print.' % (name, exc.message.split('.')[0]))
            return
        msg = self._print_block(state)
        if msg:
            raise ApiError(409, msg + ' Checked before %s.' % name)

    def boards_to_flash(self):
        """MCUs whose firmware is behind the host and that have a saved .config."""
        out = []
        for item in self.describe()['mcus']:
            if item.get('update_state') == 'behind' and item.get('config_saved') and item.get('flashable'):
                out.append(item)
        return out

    def flash_all(self, data, log):
        """Flash every behind MCU that has a .config.

        One confirmation. Print state is checked before each board, before
        Klipper is stopped. Klipper is stopped once and started once, at the
        end, or again if a board fails. The first failure stops the batch.
        """
        _reject_extra(data, ('confirm',))
        if data.get('confirm') is not True:
            raise ApiError(400, 'Flash requires confirm: true. Nothing was flashed.')
        self.assert_idle('before starting')
        targets = self.boards_to_flash()
        if not targets:
            raise ApiError(
                400,
                'No MCU is behind the host Klipper version with a saved .config. Nothing was flashed.')
        names = [item['name'] for item in targets]
        self._begin_job({'mcu': ','.join(names), 'action': 'flash_all'})
        phase = 'idle'
        try:
            for item in targets:
                self.assert_idle('before ' + item['name'])
                log('LOG --- %s (%s) ---' % (item.get('label') or item['name'], item['name']))
                self._build(item['name'], log)
            self.assert_idle('right before flashing')
            code, _out = self._systemctl('stop', log)
            if code != 0:
                raise ApiError(
                    500,
                    'Could not stop Klipper (sudo -n systemctl stop klipper failed). '
                    'Allow that command for this user. Nothing was flashed.')
            phase = 'stopped'
            log('LOG Klipper is stopped')
            for item in targets:
                self._assert_idle_or_stopped(item['name'], log)
                name = item['name']
                log('LOG flash %s via %s' % (name, item['method']))
                fw = self._firmware_file(self._out_dir(name))
                self._flash_image(item, name, fw, log)
                log('LOG flashed %s' % name)
            self._require_started(log)
            phase = 'started'
            results = []
            for item in targets:
                results.append(self._verify(item['name'], item.get('version'), log))
            return {'mcus': names, 'results': results}
        except Exception:
            if phase == 'stopped':
                log('LOG flashing failed; starting Klipper again')
                try:
                    self._systemctl('start', log)
                except Exception as exc:
                    log('LOG could not start Klipper: %s' % exc)
                log('RECOVERY ' + RECOVERY_STOPPED)
            elif phase == 'started':
                log('RECOVERY ' + RECOVERY_VERIFY)
            else:
                log('RECOVERY ' + RECOVERY_NOT_STOPPED)
            raise
        finally:
            self._end_job()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    service = None

    def log_message(self, fmt, *args):
        sys.stderr.write('firmware-helper %s\n' % (fmt % args))

    def _peer_ok(self):
        return self.client_address[0] in ('127.0.0.1', '::1')

    def _json(self, status, payload):
        raw = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or '0')
        except ValueError:
            raise ApiError(400, 'expected a JSON object')
        if n < 0 or n > MAX_BODY:
            raise ApiError(400, 'body is too large')
        raw = self.rfile.read(n) if n else b''
        if not raw:
            raise ApiError(400, 'expected a JSON object')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise ApiError(400, 'expected a JSON object')
        if not isinstance(data, dict):
            raise ApiError(400, 'expected a JSON object')
        return data

    def _route(self):
        if not self._peer_ok():
            self._json(403, {'ok': False, 'error': 'This helper accepts connections from localhost only.'})
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip('/') or '/'
        key = (self.command, path)
        routes = {
            ('GET', '/firmware/v1/status'): self._status,
            ('PUT', '/firmware/v1/config'): self._config,
            ('POST', '/firmware/v1/method'): self._method,
            ('POST', '/firmware/v1/label'): self._label,
            ('POST', '/firmware/v1/kconfig'): self._kconfig,
            ('PUT', '/firmware/v1/kconfig'): self._kconfig_save,
            ('POST', '/firmware/v1/copy'): self._copy,
            ('POST', '/firmware/v1/flash'): self._flash,
            ('POST', '/firmware/v1/flash_all'): self._flash_all,
        }
        fn = routes.get(key)
        if fn is None:
            self._json(404, {'ok': False, 'error': 'unknown action'})
            return
        # Status still answers so the page can show that this process is old.
        # Anything that would build or flash waits for the restarted process.
        self.service.poll_code_change()
        if self.service.exit_requested and key != ('GET', '/firmware/v1/status'):
            if self.command in ('POST', 'PUT'):
                try:
                    self._read_json()
                except ApiError:
                    pass
            self._json(503, {'ok': False, 'error': RESTART_MESSAGE})
            self.service.trigger_restart()
            return
        try:
            fn()
        except ApiError as exc:
            self._json(exc.status, {'ok': False, 'error': exc.message})
        except Exception as exc:
            self._json(500, {'ok': False, 'error': 'firmware helper failed: %s' % exc})
        if self.service.exit_requested:
            self.service.trigger_restart()

    def _status(self):
        self._json(200, self.service.describe())

    def _config(self):
        self._json(200, self.service.save_config(self._read_json()))

    def _method(self):
        self._json(200, self.service.set_method(self._read_json()))

    def _label(self):
        self._json(200, self.service.set_label(self._read_json()))

    def _kconfig(self):
        self._json(200, self.service.kconfig_schema(self._read_json()))

    def _kconfig_save(self):
        self._json(200, self.service.kconfig_save(self._read_json()))

    def _copy(self):
        self._json(200, self.service.copy_config(self._read_json()))

    def _stream(self, run):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def log(line):
            try:
                self.wfile.write((line + '\n').encode('utf-8', 'replace'))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        try:
            run(log)
        except ApiError as exc:
            log('FAIL ' + exc.message)
        except Exception as exc:
            log('FAIL firmware helper failed: %s' % exc)
        if self.service.exit_requested:
            self.service.trigger_restart()

    def _flash(self):
        data = self._read_json()
        # Refuse, in JSON, before the stream starts, when the request itself
        # is not a confirmed flash of an idle printer.
        _reject_extra(data, ('mcu', 'confirm'))
        if data.get('confirm') is not True:
            raise ApiError(400, 'Flash requires confirm: true. Nothing was flashed.')
        safe_name(data.get('mcu'))
        self.service.assert_idle('before starting')
        self._stream(lambda log: self.service.flash(data, log))

    def _flash_all(self):
        data = self._read_json()
        _reject_extra(data, ('confirm',))
        if data.get('confirm') is not True:
            raise ApiError(400, 'Flash requires confirm: true. Nothing was flashed.')
        self.service.assert_idle('before starting')
        if not self.service.boards_to_flash():
            raise ApiError(
                400,
                'No MCU is behind the host Klipper version with a saved .config. Nothing was flashed.')
        self._stream(lambda log: self.service.flash_all(data, log))

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_DELETE(self):
        self._json(404, {'ok': False, 'error': 'unknown action'})


def assert_bind(bind):
    if bind not in ('127.0.0.1',):
        raise SystemExit('Refusing to listen on %s. This helper binds to 127.0.0.1 only.' % bind)


def serve(service, port, bind='127.0.0.1'):
    assert_bind(bind)
    Handler.service = service
    httpd = ThreadingHTTPServer((bind, port), Handler)

    def _restart():
        # shutdown() waits until serve_forever notices. A separate thread lets
        # the request that noticed the update finish its response first.
        threading.Thread(target=httpd.shutdown, name='firmware-helper-exit', daemon=True).start()

    service._on_code_change = _restart
    service.start_code_watch()
    sys.stderr.write('stealthchanger-firmware listening on http://%s:%d/\n' % (bind, port))
    try:
        httpd.serve_forever()
    finally:
        service.stop_code_watch()
    return httpd


def main(argv):
    if argv[1:]:
        sys.stderr.write('This helper takes no arguments. Actions come from the localhost API.\n')
        return 2
    bind = os.environ.get('SC_FIRMWARE_BIND', '127.0.0.1')
    assert_bind(bind)
    try:
        port = int(os.environ.get('SC_FIRMWARE_PORT', '7140'))
    except ValueError:
        sys.stderr.write('SC_FIRMWARE_PORT must be a number.\n')
        return 2
    if port < 1 or port > 65535:
        sys.stderr.write('SC_FIRMWARE_PORT is out of range.\n')
        return 2
    serve(FirmwareService(), port, bind)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv) or 0)
