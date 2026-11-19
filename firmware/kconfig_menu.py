"""Build a menuconfig form from Klipper's Kconfig.

Uses the kconfiglib that ships in ~/klipper/lib/kconfiglib. The HTTP
layer never chooses a path: Kconfig is <klipper>/Kconfig, and the only
file written is the board's .config in the firmware folder (the caller
does that write). Loading and evaluating a form does not flash.
"""
from __future__ import print_function

import importlib.util
import os
import threading

SYM_RE_SRC = r'^[A-Za-z_][A-Za-z0-9_]*$'
MAX_VALUE = 200
_LOADED = {}
_LOCK = threading.Lock()


class KconfigMenuError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


def kconfiglib_for(klipper_dir):
    lib = os.path.join(klipper_dir, 'lib', 'kconfiglib', 'kconfiglib.py')
    if not os.path.isfile(lib):
        raise KconfigMenuError(
            'Klipper kconfiglib was not found (%s). '
            'The menuconfig form needs the Klipper source in ~/klipper.' % lib)
    key = os.path.realpath(lib)
    mod = _LOADED.get(key)
    if mod is not None:
        return mod
    # A private module name so this does not clash with another kconfiglib
    # already imported from a different tree.
    name = 'sc_kconfiglib_%d' % (abs(hash(key)) % 100000000)
    spec = importlib.util.spec_from_file_location(name, lib)
    if spec is None or spec.loader is None:
        raise KconfigMenuError('Could not load kconfiglib from the Klipper source.')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _LOADED[key] = mod
    return mod


def _apply(mod, kconf, values):
    import re
    if not isinstance(values, dict):
        raise KconfigMenuError('values must be an object of Kconfig symbol names.')
    if len(values) > 2000:
        raise KconfigMenuError('Too many Kconfig values.')
    sym_re = re.compile(SYM_RE_SRC)
    pending = []
    for name, val in values.items():
        if not isinstance(name, str) or not sym_re.match(name) or len(name) > 80:
            raise KconfigMenuError('Unknown Kconfig symbol: %s' % name)
        if not isinstance(val, str) or len(val) > MAX_VALUE or '\n' in val or '\r' in val or '\x00' in val:
            raise KconfigMenuError('Value for %s is not valid for that symbol.' % name)
        sym = kconf.syms.get(name)
        if sym is None or getattr(sym, 'orig_type', None) not in (
                mod.BOOL, mod.TRISTATE, mod.STRING, mod.INT, mod.HEX):
            raise KconfigMenuError('Unknown Kconfig symbol: %s' % name)
        pending.append((sym, val))

    def rank(val):
        # 'n' first, then strings, then 'y', so a choice selection wins.
        if val == 'n':
            return 0
        if val == 'y':
            return 2
        return 1

    pending.sort(key=lambda item: rank(item[1]))
    bad = []
    for sym, val in pending:
        if not sym.set_value(val):
            bad.append(sym.name)
    if bad:
        raise KconfigMenuError(
            'Value is not valid for: %s. Nothing was written.' % ', '.join(bad))


def load_kconfig(klipper_dir, config_path=None, values=None):
    """Return a Kconfig with defaults, an optional .config, then values applied.

    Restores the process cwd and the Kconfig environment variables before
    returning. The object is safe to read and to write_config() to an
    absolute path afterwards.
    """
    with _LOCK:
        mod = kconfiglib_for(klipper_dir)
        # make menuconfig loads src/Kconfig. A small test tree may use Kconfig.
        filename = None
        for rel in ('src/Kconfig', 'Kconfig'):
            if os.path.isfile(os.path.join(klipper_dir, rel)):
                filename = rel
                break
        if filename is None:
            raise KconfigMenuError('Kconfig was not found in %s.' % klipper_dir)
        prev = os.getcwd()
        keys = ('srctree', 'KCONFIG_CONFIG', 'KCONFIG_CONFIG_HEADER')
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.chdir(klipper_dir)
            os.environ['srctree'] = klipper_dir
            os.environ.pop('KCONFIG_CONFIG', None)
            os.environ.pop('KCONFIG_CONFIG_HEADER', None)
            try:
                kconf = mod.Kconfig(filename, warn=False)
            except Exception as exc:
                raise KconfigMenuError('Could not read Kconfig (%s).' % exc.__class__.__name__)
            if config_path:
                if not os.path.isfile(config_path):
                    raise KconfigMenuError('Saved .config was not found.')
                try:
                    kconf.load_config(os.path.abspath(config_path))
                except Exception as exc:
                    raise KconfigMenuError(
                        'Could not read the saved .config (%s).' % exc.__class__.__name__)
            if values:
                _apply(mod, kconf, values)
            return kconf, mod
        finally:
            try:
                os.chdir(prev)
            except OSError:
                pass
            for key, val in old.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


def _shown(mod, node):
    # Same rule as Klipper's menuconfig: a prompt whose condition is met.
    # Menus also need their own visibility expression.
    if not node.prompt:
        return False
    if not mod.expr_value(node.prompt[1]):
        return False
    if node.item is mod.MENU and not mod.expr_value(node.visibility):
        return False
    return True


def _help(node):
    text = getattr(node, 'help', None)
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > 800:
        text = text[:800].rstrip() + '…'
    return text


def _choice_field(mod, node, choice):
    options = []
    current = None
    for sym in choice.syms:
        loc = sym.nodes[0] if sym.nodes else None
        if loc is None or not loc.prompt:
            continue
        if not mod.expr_value(loc.prompt[1]) or not sym.visibility:
            continue
        options.append({'name': sym.name, 'prompt': loc.prompt[0]})
        if sym.tri_value == 2:
            current = sym.name
    if current is None and choice.selection is not None:
        current = choice.selection.name
    if not options:
        return None
    return {
        'kind': 'choice',
        'prompt': node.prompt[0],
        'value': current,
        'options': options,
        'help': _help(node),
    }


def _symbol_field(mod, node, sym):
    if sym.orig_type is mod.BOOL:
        kind = 'bool'
        value = sym.tri_value == 2
    elif sym.orig_type is mod.TRISTATE:
        kind = 'tristate'
        value = {0: 'n', 1: 'm', 2: 'y'}.get(sym.tri_value, 'n')
    elif sym.orig_type is mod.STRING:
        kind = 'string'
        value = sym.str_value
    elif sym.orig_type is mod.INT:
        kind = 'int'
        value = sym.str_value
    elif sym.orig_type is mod.HEX:
        kind = 'hex'
        value = sym.str_value
    else:
        return None
    field = {
        'kind': kind,
        'name': sym.name,
        'prompt': node.prompt[0],
        'value': value,
        'help': _help(node),
    }
    span = _active_range(mod, sym)
    if span is not None:
        field['range'] = span
    return field


def _active_range(mod, sym):
    ranges = getattr(sym, 'ranges', None) or []
    for low, high, cond in ranges:
        if mod.expr_value(cond):
            return [low.str_value, high.str_value]
    return None


def _walk(mod, node, into, in_choice):
    while node is not None:
        item = node.item
        if in_choice and isinstance(item, mod.Symbol) and item.choice:
            # The choice dropdown already lists these. Keep any children
            # (menus created from a dependency) if that option is showing.
            if node.list and _shown(mod, node):
                _walk(mod, node.list, into, False)
            node = node.next
            continue
        if not _shown(mod, node):
            node = node.next
            continue
        if item is mod.COMMENT:
            into.append({'kind': 'comment', 'prompt': node.prompt[0]})
        elif item is mod.MENU:
            kids = []
            if node.list:
                _walk(mod, node.list, kids, False)
            if kids:
                into.append({'kind': 'menu', 'prompt': node.prompt[0], 'children': kids})
        elif isinstance(item, mod.Choice):
            field = _choice_field(mod, node, item)
            if field is not None:
                into.append(field)
            if node.list:
                _walk(mod, node.list, into, True)
        elif isinstance(item, mod.Symbol) and item.name and not item.is_constant:
            field = _symbol_field(mod, node, item)
            if field is not None:
                if node.list:
                    kids = []
                    _walk(mod, node.list, kids, False)
                    if kids:
                        field['children'] = kids
                into.append(field)
        elif node.list:
            _walk(mod, node.list, into, False)
        node = node.next


def schema(kconf, mod):
    """Visible menus and fields, in Kconfig order."""
    fields = []
    top = kconf.top_node.list if kconf.top_node is not None else None
    if top is not None:
        _walk(mod, top, fields, False)
    mcu = None
    sym = kconf.syms.get('MCU')
    if sym is not None and sym.str_value:
        mcu = sym.str_value
    return {'fields': fields, 'config_mcu': mcu}


def write_config_text(kconf, dest):
    """Write a .config the way menuconfig does. dest must be an absolute path.

    save_old is off so a sibling .old file is not created.
    Returns the file text.
    """
    kconf.write_config(dest, save_old=False)
    with open(dest, encoding='utf-8') as fh:
        return fh.read()


def _const_name(expr):
    if getattr(expr, 'is_constant', False):
        return expr.name
    return None


def _and_symbols(mod, expr):
    """Names AND-ed in a Kconfig expression.

    Returns None when the expression is not a flat AND of symbols, so the
    caller does not guess. A constant y contributes nothing.
    """
    if expr is None:
        return []
    if isinstance(expr, tuple):
        if expr[0] is mod.AND:
            left = _and_symbols(mod, expr[1])
            right = _and_symbols(mod, expr[2])
            if left is None or right is None:
                return None
            return left + right
        return None
    if getattr(expr, 'is_constant', False):
        if expr.name in ('y', 'm'):
            return []
        return None
    name = getattr(expr, 'name', None)
    if name:
        return [name]
    return None


def _sym_prompt(sym):
    for node in getattr(sym, 'nodes', ()) or ():
        if node.prompt:
            return node.prompt[0]
    return None


def _choice_kind(sym):
    choice = getattr(sym, 'choice', None)
    if choice is None:
        return None
    for node in getattr(choice, 'nodes', ()) or ():
        if not node.prompt:
            continue
        text = node.prompt[0].lower()
        if 'architecture' in text:
            return 'arch'
        if 'processor' in text:
            return 'processor'
        if 'communication' in text:
            return 'communication'
    return None


def _select_names(sym):
    names = []
    for item in getattr(sym, 'selects', ()) or ():
        target = item[0] if isinstance(item, tuple) else item
        name = getattr(target, 'name', None)
        if name:
            names.append(name)
    return names


def _int_literal(expr):
    """Integer carried by a Kconfig default.

    kconfiglib stores `default 72000000` as a symbol whose name is that
    number. It is not always flagged is_constant.
    """
    name = getattr(expr, 'name', None)
    if isinstance(name, str) and name.isdigit():
        return int(name)
    return None


def _clock_values(kconf, mod, required):
    """CLOCK_FREQ defaults that apply when `required` symbols are y."""
    sym = kconf.syms.get('CLOCK_FREQ')
    if sym is None:
        return set()
    req = set(required)
    found = set()
    for default, cond in sym.defaults:
        number = _int_literal(default)
        if number is None:
            continue
        names = _and_symbols(mod, cond)
        if names is None:
            continue
        have = set(names)
        if req and (req.issubset(have) or have.issubset(req)):
            found.add(number)
    return found


def _assign_y(kconf, names):
    """Set each symbol to y once it is visible.

    Architecture has to be selected before the processor symbol inside it
    becomes visible. Returns the names that are y, or None if one never
    became visible.
    """
    pending = []
    for name in names:
        if name and name not in pending:
            pending.append(name)
    assigned = []
    for _ in range(len(pending) + 2):
        if not pending:
            return assigned
        stuck = []
        moved = False
        for name in pending:
            sym = kconf.syms.get(name)
            if sym is None:
                return None
            if sym.tri_value == 2:
                if name not in assigned:
                    assigned.append(name)
                moved = True
                continue
            if sym.visibility and sym.set_value('y') and sym.tri_value == 2:
                assigned.append(name)
                moved = True
                continue
            stuck.append(name)
        pending = stuck
        if not moved:
            return None
    return None


def _mcu_candidates(kconf, mod, reported):
    sym = kconf.syms.get('MCU')
    if sym is None or not reported:
        return []
    target = reported.strip()
    found = []
    seen = set()
    for default, cond in sym.defaults:
        if _const_name(default) != target:
            continue
        names = _and_symbols(mod, cond)
        if not names:
            continue
        key = tuple(names)
        if key in seen:
            continue
        seen.add(key)
        found.append(list(names))
    return found


def _walk_comm(mod, node, into):
    while node is not None:
        item = node.item
        prompt = node.prompt[0] if node.prompt else ''
        if isinstance(item, mod.Choice) and 'ommunication' in prompt:
            for sym in item.syms:
                if sym.visibility:
                    into.append(sym)
        if node.list:
            _walk_comm(mod, node.list, into)
        node = node.next


def _pick_comm(kconf, mod, link):
    """First visible communication option that matches CAN or USB.

    USB is the option that selects USBSERIAL. CAN is the option that selects
    CANSERIAL. A USB-to-CAN bridge is used for CAN only when no native CAN
    option is visible. Pin choices are the first match in Kconfig order.
    """
    if link not in ('can', 'usb'):
        return None
    symbols = []
    top = kconf.top_node.list if kconf.top_node is not None else None
    if top is not None:
        _walk_comm(mod, top, symbols)

    def selected(sym):
        return set(_select_names(sym))

    if link == 'usb':
        for sym in symbols:
            sels = selected(sym)
            if 'USBSERIAL' in sels and 'USBCANBUS' not in sels and 'CANSERIAL' not in sels:
                return sym
        return None
    for sym in symbols:
        sels = selected(sym)
        if 'CANSERIAL' in sels and 'USBCANBUS' not in sels:
            return sym
    for sym in symbols:
        sels = selected(sym)
        if 'USBCANBUS' in sels and 'CANSERIAL' not in sels:
            return sym
    return None


def suggest_initial(kconf, mod, reported_mcu, clock_hz, link):
    """Preselect architecture, processor, and communication for a connected MCU.

    reported_mcu is the MCU string from Klipper's mcu_constants. clock_hz
    (an int, or None) picks between chips that share that string. link is
    'can', 'usb', or None, from the printer config. Returns None when the
    Kconfig defaults do not name this MCU. Does not write a file.
    """
    if not isinstance(reported_mcu, str) or not reported_mcu.strip():
        return None
    candidates = _mcu_candidates(kconf, mod, reported_mcu)
    if not candidates:
        return None
    ambiguous = False
    chosen = candidates[0] if len(candidates) == 1 else None
    if len(candidates) > 1:
        if isinstance(clock_hz, int) and not isinstance(clock_hz, bool):
            narrowed = [c for c in candidates if clock_hz in _clock_values(kconf, mod, c)]
            if len(narrowed) == 1:
                chosen = narrowed[0]
            else:
                ambiguous = True
        else:
            ambiguous = True
    assigned = []
    if chosen:
        assigned = _assign_y(kconf, chosen) or []
        if not assigned:
            chosen = None
            ambiguous = True
    if not assigned:
        shared = set(candidates[0])
        for names in candidates[1:]:
            shared &= set(names)
        ordered = [n for n in candidates[0] if n in shared]
        assigned = _assign_y(kconf, ordered) or []
    labels = {'arch': None, 'processor': None, 'communication': None}
    values = {}
    for name in assigned:
        sym = kconf.syms.get(name)
        if sym is None or sym.tri_value != 2:
            continue
        values[name] = 'y'
        kind = _choice_kind(sym)
        if kind in ('arch', 'processor') and not labels[kind]:
            labels[kind] = _sym_prompt(sym) or name
    if not ambiguous:
        comm = _pick_comm(kconf, mod, link)
        if comm is not None and comm.set_value('y') and comm.tri_value == 2:
            values[comm.name] = 'y'
            labels['communication'] = _sym_prompt(comm) or comm.name
    if not values:
        return None
    return {
        'values': values,
        'arch': labels['arch'],
        'processor': labels['processor'],
        'communication': labels['communication'],
        'ambiguous_processor': bool(ambiguous and not labels['processor']),
    }
