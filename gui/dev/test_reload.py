#!/usr/bin/env python3
"""Self-reload tests: after an update the page must load the new files by itself (no Ctrl+F5).

The mock serves the GUI like an old nginx site (Last-Modified/ETag, no Cache-Control, so the
browser caches index.html heuristically) and /mock/release pretends a new version was pulled.

    venv/bin/python gui/dev/test_reload.py [--browser chromium|webkit|all] [--chrome /usr/bin/google-chrome]
"""
import argparse, json, os, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8098
BASE = 'http://127.0.0.1:%d' % PORT

def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

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

def ver(page):
    try:
        return page.evaluate('window.KTC && window.KTC.app ? window.KTC.app.GUI_VERSION : null')
    except Exception:
        return None     # navigating

def wait_ver(page, v, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if ver(page) == v:
            try:
                page.wait_for_function('() => window.KTC.app.S.ready', timeout=10000)
                return True
            except Exception:
                pass
        page.wait_for_timeout(200)
    return False

def notify_update():
    api('/mock/notify', {'method': 'notify_update_refreshed', 'params': [{'version_info': {}}]})

def banner(page):
    try:
        return page.evaluate('(document.querySelector(\'[data-banner="reload"]\') || {}).textContent || ""')
    except Exception:
        return ''

def run_browser(p, name, a, gui):
    print('\n== %s ==' % name)
    if name == 'chromium':
        kw = {'executable_path': a.chrome, 'args': ['--no-sandbox']} if a.chrome else {}
        b = p.chromium.launch(**kw)
    else:
        b = p.webkit.launch()
    api('/mock/release', {'reset': True, 'clear_hits': True})
    api('/mock/set', {'state': 'standby', 'update': {}})
    ctx = b.new_context(viewport={'width': 1200, 'height': 900})
    page = ctx.new_page()
    navs, errors = [], []
    page.on('framenavigated', lambda f: navs.append(f.url) if f == page.main_frame else None)
    page.on('pageerror', lambda e: errors.append('pageerror: %s' % e))

    page.goto(BASE + '/')
    page.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=15000)
    page.wait_for_timeout(800)
    hits = api('/mock/release', {})['hits']
    check('loads v%s with ?v= on every file' % gui, ver(page) == gui and all(
        any(h == '/%s?v=%s' % (f, gui) for h in hits) for f in ('style.css', 'js/rpc.js', 'js/commands.js', 'js/art.js', 'js/app.js')), hits)
    check('up to date: no reload notice', banner(page) == '', banner(page))

    # 1. new release pulled, user just opens the page again (index.html may come from cache)
    api('/mock/release', {'version': '9.9.1', 'clear_hits': True})
    n0 = len(navs)
    page.goto(BASE + '/')
    check('reopening after an update ends on the new version', wait_ver(page, '9.9.1'), ver(page))
    page.wait_for_timeout(500)
    check('address bar is tidy (no _v)', '_v=' not in page.url, page.url)
    check('header shows the new version', page.locator('#verTag').inner_text().startswith('v9.9.1'), page.locator('#verTag').inner_text())
    check('loop record cleared after success', page.evaluate('localStorage.getItem("ktc_gui_reload")') is None)
    print('     (navigations: %d; index.html from cache first: %s)' % (len(navs) - n0, len(navs) - n0 > 1))
    n0 = len(navs)
    page.goto(BASE + '/')
    page.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=15000)
    page.wait_for_timeout(1500)
    check('next plain open loads the new version straight away', ver(page) == '9.9.1' and len(navs) - n0 == 1, (ver(page), navs[n0:]))

    # 2. Update Manager notice while the page is open and idle
    api('/mock/release', {'version': '9.9.2'})
    notify_update()
    check('open page reloads itself after the update notice', wait_ver(page, '9.9.2'), ver(page))

    # 3. dialog open: wait, then reload once it is closed
    page.click('#btnSettings')
    page.wait_for_timeout(300)
    api('/mock/release', {'version': '9.9.3'})
    api('/mock/notify', {'method': 'notify_update_response', 'params': [{'application': 'stealthchanger-gui', 'message': 'done', 'complete': True}]})
    page.wait_for_timeout(3500)
    check('dialog open: no reload', ver(page) == '9.9.2', ver(page))
    check('dialog open: notice with Reload now', 'close the dialog' in banner(page) and page.locator('#btnReloadNow').count() == 1, banner(page))
    page.keyboard.press('Escape')
    check('dialog closed: reloads by itself', wait_ver(page, '9.9.3'), ver(page))

    # 4. printing: wait for the print to end
    api('/mock/set', {'state': 'printing'})
    page.wait_for_timeout(800)
    api('/mock/release', {'version': '9.9.4'})
    notify_update()
    page.wait_for_timeout(4000)
    check('printing: no reload', ver(page) == '9.9.3', ver(page))
    check('printing: notice says it waits for the print', 'print is finished' in banner(page), banner(page))
    api('/mock/set', {'state': 'paused'})
    page.wait_for_timeout(3500)
    check('paused: still no reload', ver(page) == '9.9.3', ver(page))
    api('/mock/set', {'state': 'complete'})
    check('print done: reloads by itself', wait_ver(page, '9.9.4'), ver(page))

    # 5. typing in a form field: wait until idle
    page.evaluate('window.KTC.app.RL.idleMs = 1500')
    page.click('#conInput')
    page.keyboard.type('G28')
    api('/mock/release', {'version': '9.9.5'})
    notify_update()
    page.wait_for_timeout(3500)
    check('console line typed: no reload', ver(page) == '9.9.4' and page.locator('#conInput').input_value() == 'G28', ver(page))
    page.fill('#conInput', '')
    page.click('#printerName')
    check('console cleared: reloads once idle', wait_ver(page, '9.9.5'), ver(page))

    # 6. Reload now button
    page.click('#btnSettings')
    page.wait_for_timeout(300)
    api('/mock/release', {'version': '9.9.6'})
    notify_update()
    page.wait_for_timeout(1500)
    page.evaluate('document.getElementById("btnReloadNow").click()')
    check('Reload now works with a dialog open', wait_ver(page, '9.9.6'), ver(page))

    # 7. loop guard: index.html says 9.9.7 but app.js still says 9.9.6
    api('/mock/release', {'index': '9.9.7'})
    n0 = len(navs)
    notify_update()
    page.wait_for_timeout(9000)
    try:
        page.wait_for_function('() => window.KTC && window.KTC.app.S.ready', timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    reloads = len(navs) - n0
    check('mismatched files: at most two reload tries', 1 <= reloads <= 2, navs[n0:])
    check('mismatched files: then asks for a manual reload', 'keeps loading' in banner(page), banner(page))
    page.evaluate('window.KTC.app.checkServed("test", true)')
    page.wait_for_timeout(4000)
    check('no further reloads', len(navs) - n0 == reloads, navs[n0:])

    # 8. back to a consistent release (also a rollback): reloads again
    api('/mock/release', {'version': gui})
    notify_update()
    check('consistent release after the guard: reloads', wait_ver(page, gui), ver(page))
    page.wait_for_timeout(1000)
    check('no %s page errors' % name, not errors, errors)
    b.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--browser', choices=['chromium', 'webkit', 'all'], default='all')
    ap.add_argument('--chrome', default=None)
    ap.add_argument('--klipper', default=None)
    a = ap.parse_args()
    cmd = [sys.executable, os.path.join(HERE, 'mock_moonraker.py'), '--port', str(PORT)]
    if a.klipper:
        cmd += ['--klipper', a.klipper]
    mock = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                gui = api('/mock/release', {})['gui_version']; break
            except Exception:
                time.sleep(0.1)
        with sync_playwright() as p:
            for name in (['chromium', 'webkit'] if a.browser == 'all' else [a.browser]):
                try:
                    run_browser(p, name, a, gui)
                except Exception as e:
                    if name == 'webkit' and 'launch' in str(e).lower() and a.browser == 'all':
                        print('  skip  webkit not installed (venv/bin/playwright install webkit): %s' % str(e).splitlines()[0])
                    else:
                        raise
    finally:
        mock.terminate()
        out = mock.communicate(timeout=5)[0].decode(errors='replace')
        if T.failed and out:
            print('--- mock output ---\n' + out[-3000:])
    print('\n%d passed, %d failed' % (T.passed, len(T.failed)))
    if T.failed:
        print('FAILED: ' + ', '.join(T.failed))
    sys.exit(1 if T.failed else 0)

if __name__ == '__main__':
    main()
