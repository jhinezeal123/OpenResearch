"""web_session.py — nơi duy nhất xử lý browser/cookie web-session của Kaggle.

Mỗi account có một profile browser chuyên dụng (Playwright persistent user-data-dir)
dưới profiles\\<alias>\\ và một file cookie profiles\\<alias>\\web-session.json.
/api/i/ (nguồn duy nhất cho run.id = kernel_session_id) CHỈ chấp nhận web-session
cookie — mọi API bearer token đều bị 400. Module này:

  - extract_cookies()      mở profile (mặc định HEADLESS), đọc cookie kaggle -> JSON
  - login_headed()         mở headed 1 lần, poll tới khi có __Host-KAGGLEID
  - cookie_jar()           build http.cookiejar.CookieJar từ file cookie
  - resolve_session_id()   dùng cookie gọi /api/i/ -> {kernel_id, ..., kernel_session_id}
  - registry load/save     profiles/accounts.json (paths and metadata)
  - login credentials     profiles/credentials.json (editable username/password)

Playwright được lazy-import (bên trong hàm) để các caller không cần browser
(vd mcp_server) vẫn import được module này.
"""
import argparse
import account_store
import base64
import datetime
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Console Windows (cp1252) không in được tiếng Việt -> ép stdout/stderr UTF-8
# để các print JSON của subprocess không crash.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.environ["ORX_KAGGLE_HOME"]
REGISTRY_PATH = os.path.join(BASE, 'profiles', 'accounts.json')
PROFILES_DIR = os.path.join(BASE, 'profiles')

BROWSERS = {
    'coccoc': {
        'exe': r'C:\Program Files\CocCoc\Browser\Application\browser.exe',
    },
    'chrome': {
        'exe': None,  # playwright channel='chrome'
    },
}

_ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')


# ---------------------------------------------------------------- ACL

def protect_acl(path, recursive=False):
    """Thu hẹp ACL: chỉ user hiện tại + SYSTEM + Administrators."""
    if not os.path.exists(path):
        return
    try:
        sid = subprocess.check_output(
            ['powershell.exe', '-NoProfile', '-Command',
             '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
            text=True).strip()
    except Exception:
        return
    rules = ['*%s:(F)' % sid, '*S-1-5-18:(F)', '*S-1-5-32-544:(F)']
    # Keep the original hardening policy, but do not silently ignore a failed
    # ACL update.  A workspace can be reopened under a different Windows SID;
    # in that case the caller must repair the existing file ACL explicitly.
    cmd = ['icacls.exe', path, '/inheritance:r', '/grant:r'] + rules
    if recursive:
        cmd.append('/T')
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except Exception as exc:
        # ACL hardening is best-effort for compatibility with non-Windows
        # callers, but retain a diagnostic for the caller/debugger instead of
        # pretending that the file was protected successfully.
        try:
            print('ACL warning for %s: %s' % (path, str(exc)[:300]),
                  file=sys.stderr, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- registry

def load_registry():
    return account_store.load_registry()


_registry_lock = account_store.REGISTRY_LOCK


def _save_registry_nolock(reg):
    tmp = REGISTRY_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)


def save_registry(reg):
    with _registry_lock:
        _save_registry_nolock(reg)
    protect_acl(REGISTRY_PATH)


def update_registry(account_key=None, **fields):
    """Atomic read-modify-write registry (thread-safe).
    Với account_key: cập nhật 1 entry và lưu. Không account_key: trả reg để caller
    sửa rồi tự gọi save_registry (dùng khi xóa nhiều entry)."""
    with _registry_lock:
        reg = load_registry()
        if account_key is not None:
            entry = reg['accounts'].setdefault(account_key, {})
            entry.update(fields)
            _save_registry_nolock(reg)
            return entry
        return reg


def remove_registry_entry(account_key):
    """Atomic: load, xóa 1 entry, save (thread-safe). Trả entry đã xóa hoặc None."""
    with _registry_lock:
        reg = load_registry()
        entry = reg['accounts'].pop(account_key, None)
        if entry is not None:
            _save_registry_nolock(reg)
        return entry


def resolve_account(account_or_alias):
    """Tra registry theo key token file -> alias -> stem. Trả dict entry (hoặc None)."""
    reg = load_registry()
    accounts = reg.get('accounts', {})
    if account_or_alias in accounts:
        return accounts[account_or_alias]
    for key, entry in accounts.items():
        if entry.get('alias') == account_or_alias:
            return dict(entry, _key=key)
    stem = os.path.splitext(os.path.basename(str(account_or_alias)))[0]
    for key, entry in accounts.items():
        if os.path.splitext(os.path.basename(key))[0] == stem:
            return dict(entry, _key=key)
    return None


def profile_dir_for(alias):
    return os.path.join(PROFILES_DIR, alias)


# ---------------------------------------------------------------- browser launch

# ---------------------------------------------------------------- stealth

STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
    for (const id of ['app', 'cookies', 'management', 'tabs', 'webstore', 'gcm']) {
      if (!window.chrome[id]) { try { window.chrome[id] = {}; } catch (_) {} }
    }
    try { window.chrome.csi = function () { return {}; }; } catch (_) {}
    try { window.chrome.loadTimes = function () { return {}; }; } catch (_) {}
  } catch (_) {}
  try {
    const plugins = [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ];
    Object.defineProperty(navigator, 'plugins', { get: () => plugins });
  } catch (_) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'] }); } catch (_) {}
  try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) {
      navigator.permissions.query = (p) => p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : q(p);
    }
  } catch (_) {}
})();
"""


def _banner_js(label):
    """Init script hiển thị banner đỏ + title để user biết đăng nhập account NÀO
    vào cửa sổ này (tránh nhầm lẫn khi mở nhiều cửa sổ login cùng lúc)."""
    safe = (label or '').replace('\\', '\\\\').replace("'", "\\'")
    return r"""
(() => {
  const label = '%s';
  document.title = 'LOGIN: ' + label;
  try {
    const d = document.createElement('div');
    d.textContent = 'DANG NHAP VAO ACCOUNT: ' + label;
    d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
      + 'background:#c00;color:#fff;font:bold 20px/1.4 sans-serif;'
      + 'padding:10px 16px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.4);';
    document.documentElement.appendChild(d);
  } catch (_) {}
})();
""" % safe


def _launch(pw, profile_dir, browser, headless, label=None, debug_port=None):
    spec = BROWSERS.get(browser or 'coccoc', BROWSERS['coccoc'])
    os.makedirs(profile_dir, exist_ok=True)
    launch = dict(
        user_data_dir=profile_dir,
        headless=headless,
        args=[
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
        ],
    )
    if debug_port:
        launch['args'] = launch['args'] + ['--remote-debugging-port=%d' % debug_port]
    if spec.get('exe'):
        if not os.path.exists(spec['exe']):
            raise RuntimeError('Browser %s không tìm thấy: %s' % (browser, spec['exe']))
        launch['executable_path'] = spec['exe']
    else:
        launch['channel'] = 'chrome'
    try:
        ctx = pw.chromium.launch_persistent_context(**launch)
    except Exception as exc:
        raise RuntimeError(
            'Không mở được profile %s (%s). Trình duyệt dùng profile này đang chạy? '
            'Hãy đóng nó trước.\n  %s' % (profile_dir, browser, str(exc)[:200])
        ) from exc
    ctx.add_init_script(STEALTH_JS)
    if label:
        ctx.add_init_script(_banner_js(label))
    return ctx


def build_record(browser, cookies):
    auth = next((c for c in cookies if c.get('name') == '__Host-KAGGLEID' and c.get('value')), None)
    if not auth:
        raise RuntimeError('Không thấy cookie __Host-KAGGLEID cho kaggle.com. '
                           'Hãy đăng nhập Kaggle trong profile này (login_account).')
    return {
        'extracted_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'browser': browser,
        'username': _session_username(cookies),
        'auth_cookie': {
            'name': auth['name'],
            'expires': auth.get('expires'),
            'expires_iso': (datetime.datetime.fromtimestamp(auth['expires']).isoformat()
                            if isinstance(auth.get('expires'), (int, float))
                            and auth['expires'] > 0 else None),
        },
        'cookies': [{
            'name': c['name'], 'value': c['value'], 'domain': c['domain'],
            'path': c.get('path', '/'), 'httpOnly': c.get('httpOnly', False),
            'secure': c.get('secure', False), 'sameSite': c.get('sameSite', 'Lax'),
            'expires': c.get('expires'),
        } for c in cookies],
    }


def _sync_playwright():
    """Ưu tiên patchright (bản Playwright đã vá: launch KHÔNG có flag --enable-automation,
    hết infobar 'being controlled by automated test software'), fallback playwright thường."""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright
        return sync_playwright


# ---------------------------------------------------------------- extraction

def extract_cookies(profile_dir, browser='coccoc', headless=True, out=None):
    """Đọc cookie kaggle từ profile chuyên dụng. Mặc định HEADLESS.
    Từ chối session khách (guest): Kaggle cấp __Host-KAGGLEID cho cả người chưa
    đăng nhập — chỉ lưu cookie khi có login thật (CLIENT-TOKEN không-anonymous)."""
    with _sync_playwright()() as pw:
        ctx = _launch(pw, profile_dir, browser, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto('https://www.kaggle.com/', wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(2000)
            cookies = [c for c in ctx.cookies() if 'kaggle' in c.get('domain', '')]
        finally:
            ctx.close()
    if not _session_username(cookies):
        raise RuntimeError('Profile này chưa đăng nhập (vẫn là session khách). '
                           'Chạy login_account để đăng nhập lại 1 lần.')
    record = build_record(browser, cookies)
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=1, ensure_ascii=False)
        protect_acl(out)
    return record


def _launch_incognito(pw, browser, label=None, debug_port=None, headless=False):
    """Khởi động browser ẩn danh (không dùng profile trên đĩa; cookies chỉ trong RAM).
    login_headed đọc cookies trực tiếp từ context nên vẫn lưu được web-session.json."""
    spec = BROWSERS.get(browser or 'coccoc', BROWSERS['coccoc'])
    launch = dict(
        headless=headless,
        args=[
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
        ],
    )
    if debug_port:
        launch['args'] = launch['args'] + ['--remote-debugging-port=%d' % debug_port]
    if spec.get('exe'):
        if not os.path.exists(spec['exe']):
            raise RuntimeError('Browser %s không tìm thấy: %s' % (browser, spec['exe']))
        launch['executable_path'] = spec['exe']
    else:
        launch['channel'] = 'chrome'
    try:
        b = pw.chromium.launch(**launch)
    except Exception as exc:
        raise RuntimeError(
            'Không mở được cửa sổ ẩn danh (%s). Trình duyệt đang chạy? '
            'Hãy đóng nó trước.\n  %s' % (browser, str(exc)[:200])
        ) from exc
    ctx = b.new_context()
    ctx.add_init_script(STEALTH_JS)
    if label:
        ctx.add_init_script(_banner_js(label))
    return ctx


def login_headed(profile_dir, browser='coccoc', timeout_minutes=15, out=None, label=None,
                 incognito=False, debug_port=None):
    """Mở headed 1 lần để đăng nhập; poll tới khi có LOGIN THẬT (không phải cookie
    khách — __Host-KAGGLEID có cả khi chưa đăng nhập) rồi tự đóng + lưu.
    label: tên account hiển thị trên banner đỏ (chống đăng nhập nhầm window).
    incognito=True: mở cửa sổ ẩn danh (không dùng profile trên đĩa).
    debug_port: mở thêm --remote-debugging-port để gắn ngoài chụp màn hình/diagnose."""
    deadline = time.time() + timeout_minutes * 60
    with _sync_playwright()() as pw:
        ctx = (_launch_incognito(pw, browser, label=label, debug_port=debug_port) if incognito
               else _launch(pw, profile_dir, browser, headless=False, label=label,
                            debug_port=debug_port))
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto('https://www.kaggle.com/', wait_until='domcontentloaded', timeout=60000)
            last = []
            while time.time() < deadline:
                cookies = []
                try:
                    cookies = [c for c in ctx.cookies() if 'kaggle' in c.get('domain', '')]
                except Exception:
                    break  # browser đã đóng (user tự đóng hoặc crash) -> dùng `last` còn lại
                if cookies:
                    last = cookies
                if _session_username(cookies):  # login THẬT (CLIENT-TOKEN không-anon)
                    time.sleep(2)
                    break
                try:
                    if not ctx.browser.is_connected():
                        break
                except Exception:
                    break
                time.sleep(3)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            if incognito:
                try:
                    ctx.browser.close()
                except Exception:
                    pass
    if not _session_username(last):
        raise RuntimeError('Chưa đăng nhập xong — profile này vẫn là session khách (guest). '
                           'Mở lại với login_account và đăng nhập Kaggle trong cửa sổ.')
    record = build_record(browser, last)
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=1, ensure_ascii=False)
        protect_acl(out)
    return record


# ---------------------------------------------------------------- auto login (headless, bằng credential)

class AutoLoginNeedsVerification(RuntimeError):
    """Kaggle yêu cầu xác minh bổ sung ngoài username/password."""


def _click_button_by_text(page, text, timeout=8000):
    try:
        for b in page.query_selector_all('button'):
            if (b.inner_text() or '').strip().lower() == text.lower():
                b.click()
                return True
    except Exception:
        pass
    try:
        page.keyboard.press('Enter')
    except Exception:
        pass
    return False


def _fill_login_form(page, username, password):
    try:
        page.fill('input[name="email"]', username, timeout=10000)
    except Exception:
        page.fill('input[placeholder*="email" i], input[placeholder*="username" i]',
                  username, timeout=10000)
    page.fill('input[name="password"], input[type="password"]', password, timeout=10000)


def _has_password_field(page):
    try:
        return page.query_selector('input[type="password"]') is not None
    except Exception:
        return False


def _click_signin_with_email(page):
    """Trang chọn phương thức đăng nhập (Welcome!) -> bấm 'Sign in with Email'."""
    try:
        for el in page.query_selector_all('button, a, [role="button"], div'):
            try:
                t = (el.inner_text() or '').strip().lower()
            except Exception:
                continue
            if t == 'sign in with email':
                el.click()
                return True
    except Exception:
        pass
    return False


class AutoLoginBlocked(RuntimeError):
    """Kaggle chặn đăng nhập (khóa tạm / sai credential) — PHẢI dừng, không retry thêm."""


def _detect_login_block(page):
    """Trả message nếu trang login báo lỗi chặn (lockedOut / sai credential), ngược lại None.
    Dùng để dừng auto-login ngay, tránh submit thêm làm account bị khóa nặng hơn."""
    try:
        url = page.url
        txt = (page.inner_text('body') or '').lower()
    except Exception:
        return None
    if 'lockedout' in url or 'locked out' in txt or 'temporarily locked' in txt:
        return ('Account bị KHÓA TẠM do nhiều lần đăng nhập thất bại. '
                'Chờ một lúc rồi thử lại — KHÔNG retry liên tục.')
    if 'login' in url and ('incorrect' in txt or 'wrong password' in txt
                           or 'invalid email' in txt or 'unsuccessful login' in txt):
        return 'Sai email/username hoặc mật khẩu. Kiểm tra lại credential.'
    return None


def auto_login(profile_dir, browser='coccoc', username=None, password=None,
               timeout_s=180, out=None, isolated=True):
    """Tự đăng nhập Kaggle bằng username+password (headless, stealth).
    Mặc định dùng browser context tạm, độc lập với profile đang mở, rồi chỉ
    lưu cookie kết quả ra `out`. Điều này cho phép bấm Recovery khi CocCoc
    profile chính vẫn đang mở.
    - Thành công: trả record cookie (đã lưu ra `out` nếu có).
    - Kaggle yêu cầu xác minh bổ sung: dừng auto-login để caller chuyển sang đăng nhập thủ công.
    Máy trạng thái xử lý luồng đa phase của Kaggle:
      emailSignIn(user+pass) -> [verifyEmail] -> yêu cầu đăng nhập thủ công."""
    if not username or not password:
        raise RuntimeError('auto_login cần username + password')
    deadline = time.time() + timeout_s
    last = []
    with _sync_playwright()() as pw:
        browser_obj = None
        if isolated:
            ctx = _launch_incognito(pw, browser, headless=True)
            browser_obj = ctx.browser
        else:
            ctx = _launch(pw, profile_dir, browser, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # Vào thẳng phase emailSignIn (bỏ qua trang chọn phương thức đăng nhập)
            page.goto('https://www.kaggle.com/account/login?phase=emailSignIn&returnUrl=%2F',
                      wait_until='domcontentloaded', timeout=60000)
            pass_submitted_at = 0.0
            pass_attempts = 0
            MAX_PASS_ATTEMPTS = 2
            while time.time() < deadline:
                cookies = []
                try:
                    cookies = [c for c in ctx.cookies() if 'kaggle' in c.get('domain', '')]
                except Exception:
                    break
                if cookies:
                    last = cookies
                if _session_username(cookies):  # LOGIN THẬT
                    time.sleep(2)
                    break
                # Phát hiện Kaggle CHẶN (khóa tạm / sai credential) -> dừng ngay, tránh khóa nặng hơn
                block_msg = _detect_login_block(page)
                if block_msg:
                    raise AutoLoginBlocked(block_msg)
                try:
                    url = page.url
                except Exception:
                    url = ''
                # Kaggle cần xác minh bổ sung; không đọc mailbox hoặc tự điền mã.
                if 'verifyEmail' in url:
                    raise AutoLoginNeedsVerification(
                        'Kaggle yêu cầu xác minh bổ sung cho %s. Hãy dùng Login để hoàn tất '
                        'trong cửa sổ trình duyệt.' % username)
                # Trang có form mật khẩu (emailSignIn) -> điền user+pass (tối đa 2 lần, tránh khóa)
                if _has_password_field(page):
                    if pass_attempts >= MAX_PASS_ATTEMPTS:
                        raise AutoLoginBlocked(
                            'Đã thử mật khẩu %d lần không thành công — dừng để tránh khóa account.'
                            % MAX_PASS_ATTEMPTS)
                    if (time.time() - pass_submitted_at) > 15:
                        try:
                            _fill_login_form(page, username, password)
                        except Exception:
                            time.sleep(3)
                            continue  # chưa điền được -> KHÔNG submit form trống
                        _click_button_by_text(page, 'Sign In')
                        pass_submitted_at = time.time()
                        pass_attempts += 1
                else:
                    # Trang chọn phương thức đăng nhập -> bấm "Sign in with Email"
                    if _click_signin_with_email(page):
                        time.sleep(3)
                        continue
                time.sleep(2)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            if browser_obj:
                try:
                    browser_obj.close()
                except Exception:
                    pass
    if not _session_username(last):
        raise RuntimeError('auto_login không thành công cho %s sau %ss '
                           '(chưa có login thật — có thể Kaggle yêu cầu xác minh bổ sung hoặc chặn bot).'
                           % (username, timeout_s))
    record = build_record(browser, last)
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=1, ensure_ascii=False)
        protect_acl(out)
    return record


# ---------------------------------------------------------------- /api/i/ resolve

def cookie_jar(cookie_file):
    with open(cookie_file, encoding='utf-8') as f:
        rec = json.load(f)
    jar = http.cookiejar.CookieJar()
    for c in rec.get('cookies', []):
        domain = c['domain'].lstrip('.')
        exp = int(c['expires']) if c.get('expires') and c['expires'] > 0 else None
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=c['name'], value=c['value'], port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=str(c['domain']).startswith('.'),
            path=c.get('path') or '/', path_specified=True,
            secure=bool(c.get('secure')), expires=exp, discard=False,
            comment=None, comment_url=None,
            rest={'HttpOnly': bool(c.get('httpOnly'))}, rfc2109=False))
    return jar


def invalidate_cookie(cookie_file, reason='Session revoked by Kaggle server'):
    """Đánh dấu cookie file bị server Kaggle từ chối (HTTP 400/401/403).
    Ghi cờ server_invalid=True để cookie_status và list_accounts lập tức nhận diện."""
    if not os.path.exists(cookie_file):
        return
    try:
        with open(cookie_file, 'r', encoding='utf-8') as f:
            rec = json.load(f)
        rec['server_invalid'] = True
        rec['invalidated_at'] = datetime.datetime.now().isoformat()
        rec['invalidation_reason'] = str(reason)
        with open(cookie_file, 'w', encoding='utf-8') as f:
            json.dump(rec, f, indent=1)
    except Exception:
        pass


def cookie_status(cookie_file):
    """Trạng thái hạn dùng của web-session cookie.
    Trả {'expires_iso', 'expired', 'valid_for_hours'} — dùng cho auto-refresh:
    valid_for_hours = None chỉ khi không đọc được expiry (cả auth cookie lẫn
    ka_sessionid đều không hạn) -> không auto-refresh được.
    Auth cookie là SESSION cookie (expires=-1) thì fallback theo ka_sessionid
    (Django session Kaggle có hạn thật) để GUI đếm ngược + kích hoạt auto-refresh."""
    if not os.path.exists(cookie_file):
        return {'expires_iso': None, 'expired': True, 'valid_for_hours': None, 'server_invalid': False}
    try:
        with open(cookie_file, encoding='utf-8') as f:
            rec = json.load(f)
        if rec.get('server_invalid'):
            return {
                'expires_iso': None,
                'expired': True,
                'valid_for_hours': 0.0,
                'server_invalid': True,
                'reason': rec.get('invalidation_reason', 'Server rejected cookie')
            }
        expires_iso = (rec.get('auth_cookie') or {}).get('expires_iso')
        if not expires_iso:
            # Session cookie -> fallback theo ka_sessionid (hạn thật của session server)
            for c in rec.get('cookies', []):
                if (c.get('name') == 'ka_sessionid'
                        and isinstance(c.get('expires'), (int, float)) and c['expires'] > 0):
                    expires_iso = datetime.datetime.fromtimestamp(c['expires']).isoformat()
                    break
    except PermissionError as exc:
        raise RuntimeError('Không có quyền đọc web-session %s. '
                           'Hãy cấp quyền cho user chạy MCP hoặc chạy repair ACL.'
                           % cookie_file) from exc
    except Exception:
        return {'expires_iso': None, 'expired': True, 'valid_for_hours': None, 'server_invalid': False}
    if not expires_iso:
        return {'expires_iso': None, 'expired': False, 'valid_for_hours': None, 'server_invalid': False}
    try:
        exp = datetime.datetime.fromisoformat(expires_iso)
        now = datetime.datetime.now(exp.tzinfo) if exp.tzinfo else datetime.datetime.now()
        hours = (exp - now).total_seconds() / 3600
    except Exception:
        return {'expires_iso': expires_iso, 'expired': False, 'valid_for_hours': None, 'server_invalid': False}
    return {'expires_iso': expires_iso, 'expired': hours <= 0, 'valid_for_hours': round(hours, 1), 'server_invalid': False}


def _session_username(cookies):
    """Từ list cookie (dict) xác định username đã đăng nhập. Kaggle cấp __Host-KAGGLEID
    cho CẢ khách (guest) — login thật phải có CLIENT-TOKEN không-anonymous (sub != '').
    Trả None nếu chưa đăng nhập (session khách)."""
    m = {c.get('name'): c.get('value') for c in (cookies or [])}
    if not m.get('__Host-KAGGLEID'):
        return None
    ct = m.get('CLIENT-TOKEN')
    if not ct or ct.count('.') < 1:
        return None
    try:
        payload = ct.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get('anon'):
            return None
        return data.get('sub') or data.get('username')
    except Exception:
        return None


def identify_user(cookie_file):
    """Trả username Kaggle của session từ cookie CLIENT-TOKEN (JWT, claim 'sub').
    Dùng để xác minh account nào đã đăng nhập vào profile nào (tránh nhầm lẫn
    giữa nhiều cửa sổ login). Trả None nếu chưa đăng nhập / không đọc được."""
    if not os.path.exists(cookie_file):
        return None
    try:
        with open(cookie_file, encoding='utf-8') as f:
            rec = json.load(f)
        return _session_username(rec.get('cookies', []))
    except Exception:
        return None


def cookie_rejected(status, body=''):
    """Phản hồi này có nghĩa web-session cookie KHÔNG còn dùng được?
    Đo thực tế trên /api/i/ (2026-09-13): __Host-KAGGLEID rác -> 400 body rỗng; 401 = chưa
    xác thực. Còn 403 kèm 'Permission ... was denied' là lỗi phân quyền THEO TÀI NGUYÊN
    (vd hủy session không thuộc quyền mình) — cookie vẫn tốt, KHÔNG được đánh dấu hỏng."""
    if status in (400, 401):
        return True
    return status == 403 and 'permission' not in str(body).lower()


def _post_i(opener, headers, method, body, cookie_file=None):
    from provider_metrics import record as record_metrics
    selected = 'external'
    if cookie_file:
        try:
            selected = account_store.resolve(os.path.basename(os.path.dirname(cookie_file)))[0]
        except ValueError:
            pass
    req = urllib.request.Request(
        'https://www.kaggle.com/api/i/' + method,
        data=json.dumps(body).encode(),
        method='POST',
        headers=dict(headers, **{'Content-Type': 'application/json'}),
    )
    started, response_bytes, failed = time.perf_counter(), 0, True
    try:
        with opener.open(req, timeout=30) as r:
            raw_response = r.read()
            response_bytes = len(raw_response)
            result = json.loads(raw_response.decode('utf-8', 'ignore'))
            failed = False
            return result
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(500).decode('utf-8', 'ignore')
            response_bytes = len(raw.encode('utf-8'))
        except Exception:
            raw = ''
        if cookie_rejected(exc.code, raw):
            if cookie_file:
                invalidate_cookie(cookie_file, 'HTTP %s in _post_i (%s)' % (exc.code, method))
            raise RuntimeError(
                'Web-session cookie hết hạn hoặc không hợp lệ (HTTP %s). '
                'Chạy get_web_session / login_account để làm mới.' % exc.code) from exc
        elif exc.code == 404:
            raise RuntimeError(
                'Tài nguyên không tìm thấy trên Kaggle (HTTP 404).') from exc
        elif exc.code == 403:
            raise RuntimeError(
                'Kaggle từ chối quyền cho %s (HTTP 403): %s' % (method, raw[:200])) from exc
        raise
    finally:
        record_metrics(selected, len(req.data or b''), response_bytes, started, failed)


def resolve_session_id(cookie_file, kernel_ref, version_number=0, wait_seconds=120):
    """Dùng cookie gọi /api/i/ để lấy kernel_session_id (= run.id) của một version.

    Trả {kernel_id, kernel_version_number, script_version_id, kernel_session_id}.
    """
    jar = cookie_jar(cookie_file)  # build 1 lần
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {'User-Agent': 'Mozilla/5.0'}
    # Cookie file extract bởi web_session đã có XSRF-TOKEN/CSRF-TOKEN -> KHÔNG cần GET
    # nguyên trang kaggle.com (tiết kiệm 1 round-trip full-page mỗi lần resolve).
    xsrf = next((c.value for c in jar if c.name == 'XSRF-TOKEN' and c.value), None)
    if not xsrf:
        xsrf = next((c.value for c in jar if c.name == 'CSRF-TOKEN' and c.value), None)
    if not xsrf:
        # Chỉ khi cookie file thiếu XSRF mới seed qua 1 GET tới trang chủ.
        with opener.open(urllib.request.Request('https://www.kaggle.com/', headers=headers), timeout=30):
            pass
        xsrf = next((c.value for c in jar if c.name == 'XSRF-TOKEN' and c.value), None)
        if not xsrf:
            xsrf = next((c.value for c in jar if c.name == 'CSRF-TOKEN' and c.value), None)
    if not xsrf:
        raise RuntimeError('Kaggle không cấp XSRF cookie (session hết hạn?). Chạy get_web_session.')
    headers['X-XSRF-TOKEN'] = urllib.parse.unquote(xsrf)

    owner, slug = kernel_ref.split('/', 1)
    view = _post_i(opener, headers, 'kernels.LegacyKernelsService/GetKernelViewModel',
                   {'authorUserName': owner, 'kernelSlug': slug, 'tab': 'output'},
                   cookie_file=cookie_file)
    kernel_id = int(view['kernel']['id'])
    if kernel_id <= 0:
        raise RuntimeError('Kaggle không trả kernel id cho %s.' % kernel_ref)
    if version_number <= 0:
        return {'kernel_id': kernel_id, 'kernel_version_number': None,
                'script_version_id': None, 'kernel_session_id': None}

    deadline = time.time() + wait_seconds
    while True:
        versions = _post_i(opener, headers, 'kernels.KernelsService/ListKernelVersions',
                           {'kernelId': kernel_id, 'sortOption': 'VERSION_ID',
                            'pageSize': max(int(view.get('totalVersionCount') or 200), 200)}, cookie_file=cookie_file)
        for item in versions.get('items', []):
            v = item.get('version', {})
            run_id = int((item.get('run') or {}).get('id') or 0)
            if int(v.get('versionNumber')) == version_number and run_id > 0:
                return {'kernel_id': kernel_id, 'kernel_version_number': version_number,
                        'script_version_id': int(v.get('id')), 'kernel_session_id': run_id}
        if time.time() >= deadline:
            raise RuntimeError('Kaggle không lộ run.id cho %s v%s trong %ss.' %
                               (kernel_ref, version_number, wait_seconds))
        time.sleep(3)


# ---------------------------------------------------------------- CLI

def _cli():
    ap = argparse.ArgumentParser(description='Kaggle web-session cookie / /api/i/ resolve')
    ap.add_argument('--account', help='account (key token file) hoặc alias')
    ap.add_argument('--alias', help='alias (tên profile); mặc định = stem của account')
    ap.add_argument('--profile-dir', help='trỏ thẳng profile dir (bỏ qua registry)')
    ap.add_argument('--browser', choices=list(BROWSERS), default='coccoc')
    ap.add_argument('--headless', dest='headless', action='store_true', default=True)
    ap.add_argument('--headed', dest='headless', action='store_false')
    ap.add_argument('--out', help='đường dẫn file cookie đầu ra')
    ap.add_argument('--login', action='store_true', help='mở headed 1 lần để đăng nhập')
    ap.add_argument('--auto-login', action='store_true',
                    help='tự đăng nhập headless bằng credential (env KG_AUTO_USER/KG_AUTO_PASS)')
    ap.add_argument('--incognito', action='store_true', help='login trong cửa sổ ẩn danh (không dùng profile trên đĩa)')
    ap.add_argument('--debug-port', type=int, default=0, help='mở --remote-debugging-port để gắn ngoài (chụp màn hình/diagnose)')
    ap.add_argument('--label', help='tên account hiển thị trên banner đỏ của cửa sổ login')
    ap.add_argument('--timeout-minutes', type=int, default=15)
    ap.add_argument('--resolve', metavar='OWNER/SLUG', help='resolve kernel_session_id cho kernel này')
    ap.add_argument('--version', type=int, default=0)
    ap.add_argument('--wait-seconds', type=int, default=120)
    args = ap.parse_args()

    if not args.account and not args.profile_dir:
        ap.error('cần --account hoặc --profile-dir')

    alias = args.alias
    if args.account:
        entry = resolve_account(args.account)
        alias = alias or (entry.get('alias') if entry else
                          os.path.splitext(os.path.basename(args.account))[0])
        profile = args.profile_dir or (os.path.join(BASE, entry['profile_dir'])
                                       if entry and entry.get('profile_dir') else profile_dir_for(alias))
        out = args.out or (os.path.join(BASE, entry['cookie_file'])
                           if entry and entry.get('cookie_file') else os.path.join(profile, 'web-session.json'))
    else:
        profile = args.profile_dir
        alias = alias or 'default'
        out = args.out or os.path.join(profile, 'web-session.json')

    if args.auto_login:
        auser = os.environ.get('KG_AUTO_USER') or ''
        apass = os.environ.get('KG_AUTO_PASS') or ''
        if not auser or not apass:
            print(json.dumps({'mode': 'auto_login', 'status': 'error',
                              'error': 'thiếu credential (env KG_AUTO_USER/KG_AUTO_PASS)'}, ensure_ascii=False))
            return
        try:
            record = auto_login(profile, args.browser, auser, apass,
                                timeout_s=args.timeout_minutes * 60, out=out)
            print(json.dumps({'mode': 'auto_login', 'status': 'ok', 'saved_to': out,
                              'expires_iso': record['auth_cookie'].get('expires_iso'),
                              'username': record.get('username')}, ensure_ascii=False, indent=1))
        except AutoLoginNeedsVerification as exc:
            print(json.dumps({'mode': 'auto_login', 'status': 'needs_manual_verification',
                              'message': str(exc)}, ensure_ascii=False))
        except AutoLoginBlocked as exc:
            print(json.dumps({'mode': 'auto_login', 'status': 'blocked',
                              'message': str(exc)}, ensure_ascii=False))
        return
    if args.login:
        record = login_headed(profile, args.browser, args.timeout_minutes, out,
                              label=args.label or alias, incognito=args.incognito,
                              debug_port=args.debug_port or None)
        print(json.dumps({'mode': 'login', 'saved_to': out, 'expires_iso': record['auth_cookie'].get('expires_iso'),
                          'username': record.get('username')},
                         ensure_ascii=False, indent=1))
        return
    if args.resolve:
        result = resolve_session_id(out, args.resolve, args.version, args.wait_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    record = extract_cookies(profile, args.browser, headless=args.headless, out=out)
    print(json.dumps({'mode': 'extract', 'headless': args.headless, 'saved_to': out,
                      'expires_iso': record['auth_cookie'].get('expires_iso')},
                     ensure_ascii=False, indent=1))


if __name__ == '__main__':
    sys.exit(_cli())
