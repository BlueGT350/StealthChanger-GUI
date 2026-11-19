#!/usr/bin/env python3
"""End-to-end tests for the ktc-gui against the mock Moonraker (real Klipper macros).

    python3 -m venv venv && venv/bin/pip install aiohttp playwright && venv/bin/playwright install chromium
    venv/bin/python gui/dev/test_gui.py [--chrome /usr/bin/google-chrome] [--shots]

--shots also writes screenshots to gui/dev/screenshots/.
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, 'screenshots')
PORT = 8099
BASE = 'http://127.0.0.1:%d' % PORT

def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def log():
    return api('/mock/log')

def gcode():
    return '\n'.join(log()['gcode'])

class T:
    passed = 0
    failed = []

def check(name, cond, detail=''):
    if cond:
        T.passed += 1
        print('  ok   ', name)
    else:
        T.failed.append(name)
        print('  FAIL ', name, detail)

GEO_JS = '''() => [0, 1, 5].map(n => {
          const c = document.querySelector('.col[data-tool="' + n + '"]'), sp = c.querySelector('.spoolsvg').getBoundingClientRect(),
                f = c.querySelector('.feed').getBoundingClientRect(), t = c.querySelector('.headsvg .tube').getBoundingClientRect(),
                m = c.querySelector('.spool .meta .lbl').getBoundingClientRect(), fx = f.left + f.width / 2;
          return [+(fx - (sp.left + sp.width * 0.92)).toFixed(1), +(fx - (t.left + t.width / 2)).toFixed(1), m.left > f.right, m.top < sp.bottom,
                  Math.abs(f.top - (sp.top + sp.height / 2)) < 2, f.bottom > t.top]; })'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--chrome', default=None, help='use this Chrome/Chromium binary')
    ap.add_argument('--shots', action='store_true')
    ap.add_argument('--klipper', default=None)
    a = ap.parse_args()
    cmd = [sys.executable, os.path.join(HERE, 'mock_moonraker.py'), '--port', str(PORT)]
    if a.klipper:
        cmd += ['--klipper', a.klipper]
    mock = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                log(); break
            except Exception:
                time.sleep(0.1)
        run(a)
    finally:
        mock.terminate()
        try:
            out = mock.communicate(timeout=5)[0].decode(errors='replace')
        except Exception:
            out = ''
        if T.failed and out:
            print('--- mock output ---\n' + out[-3000:])
    print('\n%d passed, %d failed' % (T.passed, len(T.failed)))
    if T.failed:
        print('FAILED: ' + ', '.join(T.failed))
    sys.exit(1 if T.failed else 0)

def shot(page, name):
    # viewport shot from the top: full_page captures misplace the sticky header and console
    page.evaluate('window.scrollTo(0, 0)')
    page.wait_for_timeout(150)
    page.screenshot(path=os.path.join(SHOTS, name))

def restart(page, **cfg):
    full = {'modules': 'orbiter', 'tools': 6, 'active': 0, 'state': 'standby'}
    full.update(cfg)
    api('/mock/restart', full)
    page.wait_for_timeout(300)
    page.wait_for_function('() => window.KTC.app.S.ready && document.querySelectorAll(".col").length == %d' % full['tools'],
                           timeout=10000)
    page.wait_for_timeout(300)

def run(a):
    with sync_playwright() as p:
        kw = {'executable_path': a.chrome, 'args': ['--no-sandbox']} if a.chrome else {}
        b = p.chromium.launch(**kw)
        ctx = b.new_context(viewport={'width': 1400, 'height': 1000})
        page = ctx.new_page()
        errors = []
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: errors.append('pageerror: %s' % e))
        page.goto(BASE + '/')
        page.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=10000)
        page.wait_for_timeout(500)

        print('6 tools, filament + orbiter')
        gv0 = page.evaluate('window.KTC.app.GUI_VERSION')
        html = open(os.path.join(HERE, '..', 'index.html')).read()
        vs = set(re.findall(r'\?v=([0-9.]+)"', html)) | set(re.findall(r'name="ktc-gui-version" content="([^"]+)"', html))
        check('index.html ?v= and version tag match GUI_VERSION', vs == {gv0} and len(re.findall(r'\?v=[0-9.]+"', html)) == 6, (vs, gv0))
        check('six columns', page.locator('.col').count() == 6)
        check('module chips', page.locator('#modChips').inner_text().count('v0.2.0') == 3, page.locator('#modChips').inner_text())
        check('filament tab', page.locator('#tabs [data-tab=fil]').count() == 1)
        check('no endless spool tab', page.locator('#tabs [data-tab=es]').count() == 0)
        check('T0 active highlighted', 'active' in page.locator('.head').nth(0).get_attribute('class'))
        states = [page.locator('.col[data-tool="%d"] .spool .state' % n).first.inner_text() for n in range(6)]
        check('spool states', states[0] == 'loaded' and states[2] == 'empty' and 'nozzle change' in states[4]
              and 'sensor disabled' in page.locator('.col[data-tool="3"] .spool').inner_text()
              and 'no sensor' in page.locator('.col[data-tool="5"] .spool').inner_text(), states)
        check('T3 CUSTOM temps shown', 'CUSTOM 255/250' in page.locator('.col[data-tool="3"] .prof').inner_text())
        anim = page.evaluate('''() => {
          const h = document.querySelector('.head'); const was = h.className;
          const r = h.querySelector('.rotor.hotend'), air = h.querySelector('.air');
          const off = [getComputedStyle(r).animationPlayState, getComputedStyle(air).opacity];
          h.classList.add('fan-on', 'pfan-on');
          const on = [getComputedStyle(r).animationPlayState, getComputedStyle(air).animationName];
          h.className = was; return off.concat(on); }''')
        check('fan animations follow fan state', anim == ['paused', '0', 'running', 'blow'], anim)
        check('heads drawn with shaded faces and a honeycomb grille',
              page.evaluate("() => { const s = document.querySelector('.headsvg'); return new Set([...s.querySelectorAll('path[fill^=\\'#\\']')].map(p => p.getAttribute('fill'))).size > 8 && !!s.querySelector('[fill-rule=evenodd]') && !!s.querySelector('.aura') && !!s.querySelector('.glow'); }"))
        art = page.evaluate('''() => { const s = document.querySelector('.headsvg'), lg = s.querySelector('.logolit');
          return [s.getAttribute('viewBox'), (lg.getAttribute('d').match(/Z/g) || []).length, !!s.querySelector('.logoglow'),
                  !!s.querySelector('ellipse.nozlight'), (s.querySelector('.grillewin[fill-rule=evenodd]').getAttribute('d').match(/Z/gi) || []).length,
                  !!s.querySelector('.rotor.hotend'), !!s.querySelector('.tube'), s.querySelectorAll('.rotor').length]; }''')
        # the CAD hex grill shows the fan through 7 full cells + 6 part cells at the fan's edge
        check('Anthead art (from CAD): 3-bar Voron logo LED, nozzle light, 13 open grille cells, no side blowers', art[0] == '0 -48 160 270' and art[1] == 3 and art[2]
              and art[3] and art[4] == 13 and art[5] and art[6] and art[7] == 1, art)
        geo = page.evaluate(GEO_JS)
        check('filament leaves the spool OD and runs straight into the Orbiter inlet, details beside the line',
              all(abs(g[0]) <= 2.5 and abs(g[1]) <= 1.5 and all(g[2:]) for g in geo), geo)
        check('docks drawn under the heads', page.locator('.head .dock svg.docksvg').count() == 6
              and page.evaluate("() => { const d = document.querySelectorAll('.dock')[2].getBoundingClientRect(), h = document.querySelectorAll('.headsvg')[2].getBoundingClientRect(); return d.top > h.top + h.height * 0.8 && d.top < h.bottom; }"))
        check('no calibrate sensor location button', page.get_by_text('sensor location').count() == 0)
        if a.shots:
            shot(page, 'desktop_1400_overview.png')
            # close crop of the first three heads at 2x, to judge the art
            zc = b.new_context(viewport={'width': 1400, 'height': 1000}, device_scale_factor=2)
            zp = zc.new_page()
            zp.goto(BASE + '/')
            zp.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=10000)
            zp.wait_for_timeout(1500)
            r0 = zp.locator('.col').nth(0).bounding_box(); r2 = zp.locator('.col').nth(2).bounding_box()
            zp.screenshot(path=os.path.join(SHOTS, 'heads_closeup.png'),
                          clip={'x': r0['x'] - 8, 'y': r0['y'], 'width': r2['x'] + r2['width'] - r0['x'] + 16, 'height': r0['height']})
            zc.close()

        print('editor')
        page.locator('.col[data-tool="1"] .pick').click()
        page.wait_for_selector('#dlgEditor[open]')
        check('editor prefilled', page.input_value('#edMaterial') == 'PETG' and page.input_value('#edSpool') == 'Sunlu black 1kg')
        check('save disabled when unchanged', page.locator('#edSave').is_disabled())
        page.select_option('#edMaterial', 'ABS')
        check('ABS picks HIGH', page.locator('#edProfile .sel').get_attribute('data-v') == 'HIGH')
        page.fill('#edSpool', 'eSun "black"; #2')
        page.fill('#edHex', '112233')
        page.fill('#edLL', '500')
        prev = page.inner_text('#edPreview')
        check('preview strips comment chars', ';' not in prev and '#' not in prev and 'SPOOL="eSun black 2"' in prev, prev)
        if a.shots:
            shot(page, 'desktop_1400_editor.png')
        page.click('#edSave')
        page.wait_for_function('() => !document.getElementById("dlgEditor").open', timeout=8000)
        page.wait_for_timeout(400)
        v = log()['variables']
        check('editor saved via macros', v.get('t1_material') == 'ABS' and v.get('t1_temp_profile') == 'HIGH'
              and v.get('t1_color') == '112233' and v.get('t1_spool') == 'eSun black 2' and v.get('t1_load_length') == 500.0,
              {k: v.get(k) for k in v if k.startswith('t1_')})
        check('card updated', 'ABS' in page.locator('.col[data-tool="1"] .spool').inner_text())

        page.locator('.col[data-tool="1"] .pick').click()
        page.wait_for_selector('#dlgEditor[open]')
        page.click('#edProfile [data-v=CUSTOM]')
        check('CUSTOM prefills temps', page.input_value('#edLoadT') != '')
        page.fill('#edLoadT', '400')
        check('temp above max_temp blocked', page.locator('#edSave').is_disabled() and 'edLoadT' and
              'bad' in page.get_attribute('#edLoadT', 'class'))
        page.fill('#edLoadT', '250')
        page.fill('#edUnloadT', '245')
        page.fill('#edLL', '')
        page.fill('#edPL', '600')
        check('partial longer than load blocked', page.locator('#edSave').is_disabled())
        page.fill('#edPL', '40')
        prev = page.inner_text('#edPreview')
        check('custom + reset preview', 'SET_FILAMENT_TEMP TOOL=1 TYPE=CUSTOM LOAD_TEMP=250 UNLOAD_TEMP=245' in prev
              and 'SET_FILAMENT_MOTION TOOL=1 RESET=1' in prev and 'PARTIAL_LENGTH=40' in prev, prev)
        page.click('#edSave')
        page.wait_for_function('() => !document.getElementById("dlgEditor").open', timeout=8000)
        page.wait_for_timeout(400)
        v = log()['variables']
        check('custom temps + motion saved', v.get('t1_temp_profile') == 'CUSTOM' and v.get('t1_custom_load_temp') == 250
              and v.get('t1_load_length') is None and v.get('t1_partial_length') == 40.0,
              {k: v.get(k) for k in v if k.startswith('t1_')})

        print('nozzle change (partial unload)')
        page.locator('.col[data-tool="0"] .pt').click()
        page.wait_for_selector('#dlgConfirm[open]')
        check('confirm shows command', 'FILAMENT_UNLOAD_PARTIAL TOOL=0' in page.inner_text('#cfCmd'))
        page.click('#cfYes')
        page.wait_for_function('() => /nozzle change/.test(document.querySelector(\'.col[data-tool="0"] .spool .state\').textContent)',
                               timeout=15000)
        v = log()['variables']
        check('partial saved', v.get('t0_partial') is True and v.get('t0_loaded') is False, {k: v.get(k) for k in v if k.startswith('t0_')})
        check('partial button disabled after', page.locator('.col[data-tool="0"] .pt').is_disabled())
        check('load enabled after partial', page.locator('.col[data-tool="0"] .ld').is_enabled())

        print('print lock')
        api('/mock/set', {'state': 'printing'})
        page.wait_for_timeout(400)
        ld = page.locator('.col[data-tool="4"] .ld')
        check('load locked while printing', ld.is_disabled() and 'Locked' in (ld.get_attribute('title') or ''), ld.get_attribute('title'))
        check('head pick-up locked', page.locator('.col[data-tool="2"] .headwrap').is_disabled())
        check('state chip printing', 'printing' in page.inner_text('#stateChip'))
        page.click('#tabs [data-tab=fil]')
        check('tangle mode allowed mid-print', page.locator('#tangleSeg button').first.is_enabled())
        page.click('#tangleSeg [data-v=WARN]')
        page.wait_for_timeout(500)
        check('tangle mode sent', 'ORBITER_TANGLE MODE=WARN' in gcode())
        check('tangle seg follows state', page.locator('#tangleSeg .sel').get_attribute('data-v') == 'WARN')
        api('/mock/set', {'state': 'paused'})
        page.wait_for_timeout(400)
        page.click('#tabs [data-tab=rec]')
        check('resume enabled when paused', page.get_by_role('button', name='Resume print').is_enabled())
        if a.shots:
            shot(page, 'desktop_1400_paused_locked.png')
        api('/mock/set', {'state': 'standby'})
        page.wait_for_timeout(400)
        check('unlocked after print', page.locator('.col[data-tool="4"] .ld').is_enabled())

        print('recovery')
        rb = page.get_by_role('button', name='Recover from error')
        check('recover hidden-off when not error', rb.is_disabled())
        api('/mock/set', {'tc_status': 'error'})
        page.wait_for_timeout(400)
        check('recover enabled on error', rb.is_enabled())
        check('error chip', 'error' in page.inner_text('#tcChip'))
        api('/mock/set', {'tc_status': 'ready'})

        print('tool change in progress')
        api('/mock/set', {'tc_status': 'changing'})
        page.wait_for_timeout(400)
        hw = page.locator('.col[data-tool="3"] .headwrap')
        check('pick-up locked while changing', hw.is_disabled() and 'tool change' in (hw.get_attribute('title') or ''), hw.get_attribute('title'))
        check('CHANGING badge + animation', 'CHANGING' in page.locator('.head').nth(0).inner_text()
              and 'dropping' in page.locator('.head').nth(0).get_attribute('class'))
        api('/mock/set', {'tc_status': 'ready'})
        page.wait_for_timeout(400)
        check('unlocked after change', hw.is_enabled())

        print('calibration')
        page.click('#tabs [data-tab=cal]')
        page.select_option('#calOneTool', '2')
        page.get_by_role('button', name='Calibrate one tool offset').click()
        page.wait_for_selector('#dlgConfirm[open]')
        cmd = page.inner_text('#cfCmd')
        check('calibrate one runs T0 first then T2', cmd.index('SELECT_TOOL T=0') < cmd.index('TOOL_LOCATE_SENSOR')
              < cmd.index('SELECT_TOOL T=2') < cmd.index('_SAVE_TOOL_OFFSET T=2'), cmd)
        check('calibrate one excludes reference', page.locator('#calOneTool option[value="0"]').count() == 0)
        page.click('#cfNo')
        if a.shots:
            shot(page, 'desktop_1400_calibration.png')

        print('tool change + fans')
        page.locator('.col[data-tool="2"] .headwrap').click()
        page.wait_for_function('() => document.querySelectorAll(".head")[2].classList.contains("active")', timeout=15000)
        check('picked up T2', 'T2' in page.inner_text('#tcChip'))

        print('settings / prefs in Moonraker DB')
        page.click('#btnSettings')
        page.wait_for_selector('#dlgSettings[open]')
        page.select_option('#setHeads .mdl[data-n="2"]', 'stealthburner')
        page.click('#setSave')
        page.wait_for_timeout(600)
        db = log()['db']
        check('prefs saved to ktc_gui namespace', json.dumps(db).find('stealthburner') >= 0, db)
        check('T2 art switched', page.locator('.col[data-tool="2"] svg.headsvg[data-model=stealthburner]').count() == 1)
        page.click('#btnSettings')
        page.wait_for_selector('#dlgSettings[open]')
        page.select_option('#setHeads .mdl[data-n="2"]', 'anthead')
        page.click('#setSave')
        page.wait_for_timeout(600)
        check('T2 art back to Anthead', page.locator('.col[data-tool="2"] svg.headsvg[data-model=anthead]').count() == 1)

        print('head LEDs (neopixel color_data)')
        def led(n):
            return page.evaluate('''n => { const s = document.querySelectorAll('.headsvg')[n], c = getComputedStyle(s);
              return [s.dataset.led, c.getPropertyValue('--logo').trim(), c.getPropertyValue('--logo-a').trim(),
                      c.getPropertyValue('--noz').trim(), c.getPropertyValue('--noz-a').trim(),
                      getComputedStyle(s.querySelector('.logolit')).opacity, document.querySelectorAll('.head')[n].classList.contains('noled')]; }''', n)
        def sendg(g):
            page.evaluate("g => KTC.app.S.rpc.call('printer.gcode.script', {script: g})", g)
        def settle(n, want):
            try:
                page.wait_for_function('([n, w]) => getComputedStyle(document.querySelectorAll(".headsvg")[n]).getPropertyValue("--logo").trim() == w',
                                       arg=[n, want], timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(500)          # let the 0.3s opacity transition finish
        check('LED auto-mapped by T<n>', all(led(n)[0] == 'neopixel headlight_T%d' % n for n in range(6)), [led(n)[0] for n in range(6)])
        check('idle demo LEDs: red logo, dim white nozzle', led(3)[1:5] == ['rgb(255,0,0)', '0.800', 'rgb(255,255,255)', '0.350'], led(3))
        sendg('SET_LED LED=headlight_T3 RED=0 GREEN=0 BLUE=1 WHITE=0 INDEX=1\nSET_LED LED=headlight_T3 RED=1 GREEN=0 BLUE=0 WHITE=0 INDEX=2\n'
              'SET_LED LED=headlight_T3 RED=0 GREEN=0 BLUE=0 WHITE=0 INDEX=3')
        settle(3, 'rgb(0,0,255)')
        v = led(3)
        check('logo glow follows color_data index 1', v[1:3] == ['rgb(0,0,255)', '1.000'] and v[5] == '1' and not v[6], v)
        check('nozzle light = average of LEDs 2-3', v[3:5] == ['rgb(255,0,0)', '0.500'], v)
        sendg('SET_LED LED=headlight_T3 RED=0.2 GREEN=0 BLUE=0 WHITE=0.5 INDEX=1')
        settle(3, 'rgb(255,182,182)')
        v = led(3)
        check('white channel mixed in, brightness scales', v[1:3] == ['rgb(255,182,182)', '0.700'] and v[5] == '0.7', v)
        def set_led(n, obj, li, ni):
            page.click('#btnSettings')
            page.wait_for_selector('#dlgSettings[open]')
            row = '#setHeads .sethead:nth-child(%d) ' % (n + 1)
            page.select_option(row + '.ledo', obj)
            page.fill(row + '.lidx', li)
            page.fill(row + '.nidx', ni)
            page.click('#setSave')
            page.wait_for_timeout(700)
        set_led(3, 'none', '1', '2-3')
        v = led(3)
        check('LED mapping "none": dim neutral logo', v[0] == '' and v[6] and v[5] == '0'
              and page.evaluate("() => getComputedStyle(document.querySelectorAll('.head')[3].querySelector('.logobase')).opacity") == '0.8', v)
        check('LED mapping saved to Moonraker DB', log()['db'].get('ktc_gui', {}).get('prefs', {}).get('heads', {}).get('3', {}).get('led') == 'none',
              log()['db'])
        set_led(3, '', '2', '1')
        v = led(3)
        check('logo index setting picks another LED', v[0] == 'neopixel headlight_T3' and v[1:3] == ['rgb(255,0,0)', '1.000']
              and v[3:5] == ['rgb(255,182,182)', '0.700'], v)
        set_led(3, 'neopixel headlight_T1', '1', '2-3')
        check('LED mapped to another object', led(3)[0] == 'neopixel headlight_T1', led(3))
        set_led(3, '', '1', '2-3')

        print('console')
        page.fill('#conInput', 'SHOW_TOOLS')
        page.press('#conInput', 'Enter')
        page.wait_for_timeout(800)
        check('console shows responses', 'T0' in page.inner_text('#conLines') and '> SHOW_TOOLS' in page.inner_text('#conLines'))

        for mods, tools, fname in [('base', 6, 'desktop_1400_base.png'), ('filament', 6, None), ('orbiter', 3, 'desktop_1400_3tools.png')]:
            print('config: %s, %d tools' % (mods, tools))
            restart(page, modules=mods, tools=tools)
            check('%s/%d columns' % (mods, tools), page.locator('.col').count() == tools)
            fil = page.locator('#tabs [data-tab=fil]').count()
            check('%s/%d filament tab' % (mods, tools), fil == (0 if mods == 'base' else 1))
            check('%s/%d load buttons' % (mods, tools), page.locator('.ld').count() == (0 if mods == 'base' else tools))
            page.click('#tabs [data-tab=fil]') if fil else None
            check('%s/%d tangle seg' % (mods, tools), page.locator('#tangleSeg').count() == (1 if mods == 'orbiter' else 0))
            if a.shots and fname:
                shot(page, fname)
        restart(page, leds=[0, 1])
        v = [led(n) for n in range(6)]
        check('heads without an LED object show a dim logo', not v[1][6] and all(x[6] and x[0] == '' for x in v[2:]), v)
        restart(page)

        if a.shots:
            print('tablet')
            tab = ctx.new_page()
            tab.set_viewport_size({'width': 820, 'height': 1180})
            tab.on('pageerror', lambda e: errors.append('pageerror: %s' % e))
            tab.goto(BASE + '/?host=127.0.0.1:%d' % PORT)
            tab.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=10000)
            tab.wait_for_timeout(800)
            tab.screenshot(path=os.path.join(SHOTS, 'tablet_820.png'))
            sw = tab.evaluate('document.documentElement.scrollWidth')
            rw = tab.evaluate('(() => { const r = document.getElementById("rack"); return r.scrollWidth - r.clientWidth; })()')
            check('tablet: nothing scrolls sideways', sw <= 820 and rw <= 0, (sw, rw))
            geo = tab.evaluate(GEO_JS)
            check('tablet: filament line off the spool OD into the inlet', all(abs(g[0]) <= 2.5 and abs(g[1]) <= 1.5 and all(g[2:]) for g in geo), geo)
            tab.click('#tabs [data-tab=fil]')
            tab.evaluate('document.getElementById("controls").scrollIntoView()')
            tab.wait_for_timeout(300)
            tab.screenshot(path=os.path.join(SHOTS, 'tablet_820_filament.png'))
            tab.close()

        print('rev 2: home before a tool change')
        restart(page)
        api('/mock/set', {'homed': ''})
        page.wait_for_timeout(400)
        n0 = len(log()['gcode'])
        page.locator('.col[data-tool="3"] .headwrap').click()
        page.wait_for_selector('#dlgConfirm[open]')
        dlg = page.inner_text('#dlgConfirm')
        check('not homed: homing modal', page.inner_text('#cfTitle') == 'Homing required'
              and page.inner_text('#cfText') == 'The printer must be homed (XYZ) before changing tools. Home now?'
              and page.inner_text('#cfYes') == 'Yes' and page.inner_text('#cfNo') == 'No'
              and (page.text_content('#cfCmd') or '').strip() == '', dlg)
        if a.shots:
            page.screenshot(path=os.path.join(SHOTS, 'homing_required.png'))
        page.click('#cfNo')
        page.wait_for_function('() => !document.getElementById("dlgConfirm").open')
        page.wait_for_timeout(300)
        check('No sends no gcode', log()['gcode'][n0:] == [], log()['gcode'][n0:])
        check('No leaves the tool on the shuttle', 'active' in page.locator('.head').nth(0).get_attribute('class')
              and 'active' not in (page.locator('.head').nth(3).get_attribute('class') or ''))

        page.click('#tabs [data-tab=tool]')
        page.wait_for_timeout(200)
        n0 = len(log()['gcode'])
        page.locator('#pane-tool').get_by_role('button', name='T4', exact=True).click()
        page.wait_for_selector('#dlgConfirm[open]')
        check('T# button asks when not homed', page.inner_text('#cfTitle') == 'Homing required')
        page.click('#cfNo')
        page.wait_for_timeout(300)
        check('T# No sends no gcode', log()['gcode'][n0:] == [], log()['gcode'][n0:])

        api('/mock/set', {'homed': 'xy'})
        page.wait_for_timeout(300)
        n0 = len(log()['gcode'])
        page.locator('.col[data-tool="3"] .headwrap').click()
        page.wait_for_selector('#dlgConfirm[open]')
        check('missing Z still asks before a tool change', page.inner_text('#cfTitle') == 'Homing required')
        page.click('#cfNo')
        page.wait_for_timeout(200)
        check('missing Z No sends nothing', log()['gcode'][n0:] == [], log()['gcode'][n0:])
        api('/mock/set', {'homed': ''})
        page.wait_for_timeout(300)

        page.evaluate('''() => {
          const rpc = window.KTC.app.S.rpc, orig = rpc.call.bind(rpc);
          window.__rpcCall = orig;
          rpc.call = function (method, params) {
            if (method === 'printer.gcode.script' && params && String(params.script).trim() === 'G28')
              return Promise.reject(new Error('Homing failed'));
            return orig(method, params);
          };
        }''')
        n0 = len(log()['gcode'])
        page.locator('.col[data-tool="3"] .headwrap').click()
        page.wait_for_selector('#dlgConfirm[open]')
        page.click('#cfYes')
        page.wait_for_selector('#banners .toast', timeout=5000)
        check('G28 error is shown and the tool is not changed', 'Homing failed' in page.inner_text('#banners')
              and 'active' in page.locator('.head').nth(0).get_attribute('class')
              and not any(g.strip() == 'T3' for g in log()['gcode'][n0:]),
              (page.inner_text('#banners'), log()['gcode'][n0:]))
        page.evaluate('() => { window.KTC.app.S.rpc.call = window.__rpcCall; }')
        page.wait_for_function('() => !window.KTC.app.S.homing')

        # hold G28 so the busy state is visible, then let the real command run
        page.evaluate('''() => {
          const rpc = window.KTC.app.S.rpc, orig = rpc.call.bind(rpc);
          window.__rpcCall = orig;
          rpc.call = function (method, params) {
            if (method === 'printer.gcode.script' && params && String(params.script).trim() === 'G28')
              return new Promise(function (resolve, reject) {
                setTimeout(function () { orig(method, params).then(resolve, reject); }, 400);
              });
            return orig(method, params);
          };
        }''')
        n0 = len(log()['gcode'])
        page.locator('.col[data-tool="3"] .headwrap').click()
        page.wait_for_selector('#dlgConfirm[open]')
        page.click('#cfYes')
        page.wait_for_selector('[data-banner=home]', timeout=2000)
        check('Yes shows a homing busy state and has not changed tools yet',
              page.inner_text('[data-banner=home]') == 'Homing...'
              and page.locator('.col[data-tool="3"] .headwrap').is_disabled()
              and not any(g.strip() == 'T3' for g in log()['gcode'][n0:]),
              (page.inner_text('[data-banner=home]'), log()['gcode'][n0:]))
        page.wait_for_function('() => document.querySelectorAll(".head")[3].classList.contains("active")', timeout=15000)
        sent = [g.strip() for g in log()['gcode'][n0:]]
        check('Yes sends G28 then the toolchange', sent[:2] == ['G28', 'T3'], sent)
        page.evaluate('() => { window.KTC.app.S.rpc.call = window.__rpcCall; }')
        page.wait_for_function('() => window.KTC.app.S.st.toolchanger.status === "ready"', timeout=15000)

        n0 = len(log()['gcode'])
        page.locator('.col[data-tool="1"] .headwrap').click()
        page.wait_for_function('() => document.querySelectorAll(".head")[1].classList.contains("active")', timeout=15000)
        sent = [g.strip() for g in log()['gcode'][n0:]]
        check('homed: direct toolchange, no modal', page.locator('#dlgConfirm[open]').count() == 0
              and sent == ['T1'], sent)
        page.wait_for_function('() => window.KTC.app.S.st.toolchanger.status === "ready"', timeout=15000)

        print('rev 2: home before moves (item 1)')
        page.click('#tabs [data-tab=cal]')
        page.wait_for_timeout(200)
        page.get_by_role('button', name='Calibrate all offsets').click()
        page.wait_for_selector('#dlgConfirm[open]')
        check('homed printer: no G28 added', 'G28' not in page.inner_text('#cfCmd'), page.inner_text('#cfCmd'))
        page.click('#cfNo')
        api('/mock/set', {'homed': 'xy'})
        page.wait_for_timeout(300)
        page.get_by_role('button', name='Calibrate all offsets').click()
        page.wait_for_selector('#dlgConfirm[open]')
        check('Z not homed: calibrate all gets G28', page.inner_text('#cfCmd').strip().startswith('G28'), page.inner_text('#cfCmd'))
        page.click('#cfNo')
        api('/mock/set', {'homed': 'xyz'})

        print('rev 2: SAVE_CONFIG conflicts (item 2)')
        page.wait_for_function('() => !!document.querySelector("[data-banner=cfg]")', timeout=8000)
        bn = page.inner_text('[data-banner=cfg]')
        check('banner names the clashing values', 'SAVE_CONFIG will fail' in bn and 'gcode_x_offset' in bn, bn)
        cf = page.locator('#cfBox')
        check('calibration tab lists every clash', 'bad' in cf.get_attribute('class') and 'tool T1' in cf.inner_text()
              and 'tool_probe T1' in cf.inner_text() and 'z_offset' in cf.inner_text() and '#*#' in cf.inner_text(), cf.inner_text())
        if a.shots:
            page.locator('#cfBox').scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.screenshot(path=os.path.join(SHOTS, 'desktop_1400_saveconfig_conflict.png'))
        api('/mock/set', {'pending': {'tool T2': {'gcode_x_offset': '0.111'}}})
        page.wait_for_timeout(500)
        check('a new pending offset joins the list live', 'tool T2' in page.inner_text('#cfBox'), page.inner_text('#cfBox'))
        n0 = len(log()['gcode'])
        page.get_by_role('button', name='SAVE_CONFIG', exact=False).first.click()
        page.wait_for_selector('#dlgConfirm[open]')
        if 'will fail' not in page.inner_text('#dlgConfirm'):
            page.click('#cfYes'); page.wait_for_timeout(300)
            page.wait_for_selector('#dlgConfirm[open]')
        check('SAVE_CONFIG gate warns before Klipper refuses', 'will fail' in page.inner_text('#dlgConfirm'), page.inner_text('#dlgConfirm'))
        page.click('#cfNo')
        page.wait_for_timeout(300)
        check('cancelled gate sends nothing', not any('SAVE_CONFIG' in g for g in log()['gcode'][n0:]))
        sendg_err = page.evaluate("() => KTC.app.S.rpc.call('printer.gcode.script', {script: 'SAVE_CONFIG'}).then(() => 'ok', e => e.message)")
        check('mock Klipper refuses like the real one', 'conflicts with included value' in sendg_err, sendg_err)
        restart(page, configs='clean')
        page.click('#tabs [data-tab=cal]')
        page.wait_for_function('() => document.getElementById("cfBox") && document.getElementById("cfBox").classList.contains("ok")', timeout=8000)
        check('clean configs: no banner, box ok', page.locator('[data-banner=cfg]').count() == 0, page.inner_text('#cfBox'))

        print('rev 2: Mark loaded command (item 8) + active printing tool (item 7)')
        page.click('#tabs [data-tab=fil]')
        page.wait_for_timeout(200)
        n0 = len(log()['gcode'])
        mk = page.locator('#tabBody table.ftab').first.locator('tr').nth(3).get_by_role('button', name='Mark loaded')
        mk.click()
        page.wait_for_timeout(600)
        sent = log()['gcode'][n0:]
        check('Mark loaded sends SET_FILAMENT_LOADED TOOL=2 LOADED=1', any(g.strip() == 'SET_FILAMENT_LOADED TOOL=2 LOADED=1' for g in sent), sent)
        check('T2 now loaded', log()['variables'].get('t2_loaded') in (True, 1), log()['variables'].get('t2_loaded'))
        page.evaluate("() => KTC.app.S.rpc.call('printer.gcode.script', {script: 'SET_FILAMENT_LOADED TOOL=0 LOADED=0'})")
        api('/mock/set', {'state': 'printing'})
        page.wait_for_timeout(600)
        s0 = page.locator('.col[data-tool="0"] .spool .state').first.inner_text()
        check('active tool in a print shows loaded', s0 == 'printing' and page.locator('.col[data-tool="0"]').get_attribute('data-fil') == 'loaded', s0)
        api('/mock/set', {'state': 'standby'})
        page.wait_for_timeout(400)

        print('rev 2: usage + Spoolman (items 9, 13)')
        restart(page)
        page.click('#tabs [data-tab=fil]')
        page.wait_for_selector('#usagePanel')
        check('usage panel, Local selected', page.locator('#usageSeg .sel').get_attribute('data-v') == 'local'
              and 'm used' in page.locator('#usagePanel').inner_text())
        api('/mock/set', {'state': 'printing'})
        page.wait_for_timeout(2500)
        api('/mock/set', {'state': 'standby'})
        page.wait_for_timeout(1500)
        used = log()['variables'].get('t0_used_mm', 0)
        check('local mode counts T0 use in variables', used and used > 0, used)
        check('spool card shows the usage line', 'm used' in page.locator('.col[data-tool="0"] .spool .use').inner_text())
        sb = page.locator('#usageSeg [data-v=spoolman]')
        check('Spoolman selectable (Moonraker reports it)', sb.is_enabled())
        sb.click()
        page.wait_for_function('() => document.querySelector("#usageSeg .sel") && document.querySelector("#usageSeg .sel").dataset.v == "spoolman"', timeout=6000)
        check('mode sent', 'KTC_USAGE_MODE MODE=SPOOLMAN' in gcode())
        page.wait_for_selector('#usagePanel select[data-tool="1"]')
        page.select_option('#usagePanel select[data-tool="1"]', '7')
        page.wait_for_selector('#dlgConfirm[open]')
        check('assign shows the macro', 'SET_TOOL_SPOOL TOOL=1 ID=7' in page.inner_text('#cfCmd'), page.inner_text('#cfCmd'))
        page.click('#cfYes')
        page.wait_for_timeout(800)
        check('T1 linked to spool 7', log()['variables'].get('t1_spool_id') == 7, log()['variables'].get('t1_spool_id'))
        page.locator('.col[data-tool="1"] .headwrap').click()
        page.wait_for_function('() => document.querySelectorAll(".head")[1].classList.contains("active")', timeout=15000)
        for _ in range(40):                     # change takes 2 s in the mock, then the 1 s watcher
            if log()['spoolman']['active'] == 7:
                break
            page.wait_for_timeout(200)
        check('tool change sets the Moonraker active spool', log()['spoolman']['active'] == 7, log()['spoolman']['active'])
        rem0 = next(s['remaining_weight'] for s in log()['spoolman']['spools'] if s['id'] == 7)
        api('/mock/set', {'state': 'printing'})
        page.wait_for_timeout(2500)
        api('/mock/set', {'state': 'standby'})
        rem1 = next(s['remaining_weight'] for s in log()['spoolman']['spools'] if s['id'] == 7)
        check('printing books use to spool 7', rem1 < rem0, (rem0, rem1))
        page.click('#usagePanel button:has-text("Add spool")')
        page.wait_for_selector('#dlgSpool[open]')
        page.wait_for_function('() => document.getElementById("spFil").options.length > 1', timeout=5000)
        page.select_option('#spFil', 'new')
        page.fill('#spVendor', 'Overture'); page.fill('#spName', 'PETG Formula Test'); page.fill('#spMat', 'PETG'); page.fill('#spHex', '1E88E5')
        page.fill('#spRem', '950'); page.fill('#spInit', '1000')
        if a.shots:
            shot(page, 'desktop_1400_spoolman_add.png')
        nsp = len(log()['spoolman']['spools'])
        page.click('#spSave')
        page.wait_for_function('() => !document.getElementById("dlgSpool").open', timeout=8000)
        page.wait_for_timeout(600)
        L = log()['spoolman']
        newsp = L['spools'][-1] if len(L['spools']) > nsp else None
        check('new spool + filament + vendor created in Spoolman', newsp and newsp['remaining_weight'] == 950
              and newsp['filament']['material'] == 'PETG' and (newsp['filament'].get('vendor') or {}).get('name') == 'Overture', newsp)
        page.wait_for_timeout(500)
        check('new spool offered in the tool picker', page.locator('#usagePanel select[data-tool="2"] option[value="%s"]' % (newsp or {}).get('id')).count() == 1)
        page.locator('#usagePanel tr[data-tool="1"] button:has-text("Edit spool")').click()
        page.wait_for_selector('#dlgSpool[open]')
        page.fill('#spRem', '500')
        page.click('#spSave')
        page.wait_for_function('() => !document.getElementById("dlgSpool").open', timeout=8000)
        page.wait_for_timeout(400)
        check('edit spool PATCHes remaining weight', next(s['remaining_weight'] for s in log()['spoolman']['spools'] if s['id'] == 7) == 500)
        if a.shots:
            page.locator('#usagePanel').scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            page.screenshot(path=os.path.join(SHOTS, 'desktop_1400_usage_spoolman.png'))
        restart(page, spoolman='off')
        page.click('#tabs [data-tab=fil]')
        page.wait_for_selector('#usagePanel')
        page.wait_for_timeout(500)
        check('no [spoolman] in Moonraker: Spoolman not selectable', page.locator('#usageSeg [data-v=spoolman]').is_disabled()
              and 'not set up' in page.inner_text('#usagePanel'))
        restart(page, no_usage=True)
        page.click('#tabs [data-tab=fil]')
        page.wait_for_timeout(300)
        check('without the add-on: install hint, no panel', page.locator('#usagePanel').count() == 0 and 'usage_addon.cfg' in page.inner_text('#tabBody'))
        restart(page)

        print('rev 2: halo (item 12)')
        halo = page.evaluate("""() => [0,1,2,3,4,5].map(n => { const u = KTC.app.S.ui[n]; const c = u && u.spool && u.spool.parentNode;
            const cs = c ? getComputedStyle(c) : null;
            return [cs ? cs.getPropertyValue('--halo').trim() : '', cs ? cs.getPropertyValue('--fil').trim().replace('#', '') : '']; })""")
        bg = (0x17, 0x19, 0x1c)   # rack background behind the spools
        def rgb(sx):
            import re
            m = re.findall(r'[\d.]+', sx)
            return tuple(float(x) for x in m[:3]) if len(m) >= 3 else None
        def lum(c):
            def ch(v):
                v /= 255.0
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
        ratios = []
        for hv, col in halo:
            c = rgb(hv)
            ratios.append(round((lum(c) + 0.05) / (lum(bg) + 0.05), 2) if c else None)
        check('every halo is at least 3:1 on the card', all(r and r >= 3 for r in ratios), list(zip(halo, ratios)))
        black = [h for h, col in halo if col.upper() in ('000000', '111111', '1A1A1A', '222222')]
        check('black spool gets a light neutral glow', black and all(abs(rgb(h)[0] - rgb(h)[1]) < 12 and abs(rgb(h)[1] - rgb(h)[2]) < 12 for h in black), halo)

        print('rev 2: tool change animation (item 14)')
        api('/mock/set', {'tc_status': 'changing'})
        page.wait_for_function('() => document.querySelectorAll(".head")[0].classList.contains("dropping")', timeout=4000)
        page.wait_for_timeout(700)
        api('/mock/set', {'tc_status': 'ready'})
        page.wait_for_timeout(250)
        mid = page.evaluate('() => document.querySelectorAll(".head")[0].classList.contains("dropping")')
        page.wait_for_function('() => !document.querySelectorAll(".head")[0].classList.contains("dropping")', timeout=4000)
        eased = page.evaluate('() => document.querySelectorAll(".head")[0].dataset.anim')
        check('lift cycle finishes before stopping, then eases', mid and eased == 'eased', (mid, eased))

        print('rev 2: phone header (item 11)')
        for w in (430, 375):
            ph = b.new_context(viewport={'width': w, 'height': 900}, device_scale_factor=2)
            pp = ph.new_page()
            pp.on('pageerror', lambda e: errors.append('pageerror: %s' % e))
            pp.goto(BASE + '/')
            pp.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=10000)
            pp.wait_for_timeout(1000)
            m = pp.evaluate("""() => { const t = document.querySelector('.topbar'), r = t.getBoundingClientRect();
                return [Math.round(r.height), t.scrollWidth - t.clientWidth, document.documentElement.scrollWidth,
                        [...t.children].every(c => c.getBoundingClientRect().right <= innerWidth + 0.5)]; }""")
            check('%dpx: one compact header row, nothing off screen' % w, m[0] <= 48 and m[1] <= 0 and m[2] <= w and m[3], m)
            pp.click('#tabs [data-tab=fil]')
            pp.wait_for_timeout(300)
            over = pp.evaluate("""() => [...document.querySelectorAll('table.ftab button, table.ftab select')]
                .filter(e => e.offsetParent && e.getBoundingClientRect().right > innerWidth + 0.5).map(e => e.textContent.trim() || e.tagName)""")
            check('%dpx: filament tables fit the screen' % w, not over, over)
            if a.shots:
                pp.evaluate('window.scrollTo(0, 0)')
                pp.wait_for_timeout(200)
                pp.screenshot(path=os.path.join(SHOTS, 'phone_%d.png' % w))
                pp.click('#tabs [data-tab=fil]')
                pp.wait_for_timeout(300)
                pp.evaluate('document.getElementById("controls").scrollIntoView()')
                pp.wait_for_timeout(300)
                pp.screenshot(path=os.path.join(SHOTS, 'phone_%d_filament.png' % w))
            ph.close()

        print('repo: installed version shown (Update Manager)')
        page.evaluate('window.KTC.app.loadRepoVersion()')
        page.wait_for_timeout(300)
        vt = page.locator('#verTag')
        gv = page.evaluate('window.KTC.app.GUI_VERSION')
        check('mock Update Manager version matches the GUI', ('v' + gv) in vt.get_attribute('title'), (gv, vt.get_attribute('title')))
        check('version tag shows GUI version and commit', vt.inner_text() == 'v%s \u00b7 0123abc' % gv, vt.inner_text())
        check('version tooltip names the git version', ('v%s-0-g0123abcd' % gv) in vt.get_attribute('title')
              and 'Up to date' in vt.get_attribute('title'), vt.get_attribute('title'))
        api('/mock/set', {'update': {'remote_hash': 'feedbeef' * 5, 'remote_version': 'v0.2.1', 'commits_behind_count': 2}})
        page.evaluate('window.KTC.app.loadRepoVersion()')
        page.wait_for_timeout(300)
        check('update available is flagged', 'warn' in vt.get_attribute('class') and '\u2191' in vt.inner_text()
              and 'v0.2.1' in vt.get_attribute('title'), (vt.inner_text(), vt.get_attribute('title')))
        api('/mock/set', {'update': {'version': 'v0.3.0', 'full_version_string': 'v0.3.0-0-gfeedbeef',
                                     'current_hash': 'feedbeef' * 5, 'remote_hash': 'feedbeef' * 5, 'commits_behind_count': 0}})
        page.evaluate('window.KTC.app.loadRepoVersion()')
        page.wait_for_timeout(300)
        page.evaluate('window.KTC.app.checkServed("test", true)')
        page.wait_for_timeout(500)
        check('Update Manager newer than the served files: says the GUI folder is not linked',
              'setup.sh' in vt.get_attribute('title') and 'Ctrl+F5' not in vt.get_attribute('title'), vt.get_attribute('title'))
        page.click('#btnSettings')
        page.wait_for_timeout(400)
        check('settings shows the installed commit', 'feedbee' in page.locator('#repoVersion').inner_text(),
              page.locator('#repoVersion').inner_text())
        page.keyboard.press('Escape')
        api('/mock/set', {'update': None})
        page.evaluate('window.KTC.app.loadRepoVersion()')
        page.wait_for_timeout(300)
        check('no update_manager entry: plain GUI version', vt.inner_text() == 'v' + gv, vt.inner_text())
        api('/mock/set', {'update': {}})

        check('no JS console errors', not errors, errors)
        b.close()

if __name__ == '__main__':
    main()
