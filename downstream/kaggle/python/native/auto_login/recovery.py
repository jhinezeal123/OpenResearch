"""Original cookie recovery orchestration adapted to the bundled account core."""
import hashlib
import os, json, sys, subprocess, time
from pathlib import Path
import web_session


def get_web_session_core(account, headless=True, timeout=120):
    from account_runtime import _account, _set_registry, _web_cli
    a = _account(account)
    result = _web_cli(['--account', a['account'], ('--headless' if headless else '--headed')], timeout=timeout)
    if isinstance(result, dict) and result.get('expires_iso'):
        _set_registry(a['account'], cookie_expires=result['expires_iso'])
    out = dict(result) if isinstance(result, dict) else {'raw': str(result)}
    out['account'] = a['account']
    out['saved_to'] = a['cookie_file']
    return out


def _web_cli(args, timeout=120, env=None):
    """playwright/headless work chạy trong subprocess riêng (web_session.py standalone):
    Playwright Sync API không chạy được bên trong asyncio loop của MCP server.
    env: dict biến môi trường bổ sung (vd credential auto-login — KHÔNG đưa vào argv)."""
    cmd = [sys.executable, web_session.__file__] + args
    # env=... replaces the whole environment on Windows.  Auto-login used to
    # pass only KG_AUTO_* here, which could leave Playwright/Chromium without
    # PATH/SystemRoot/TEMP and make the browser subprocess fail mysteriously.
    child_env = None
    if env is not None:
        child_env = os.environ.copy()
        child_env.update({str(k): str(v) for k, v in env.items()})
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('web_session.py bị timeout sau %ss (%s). '
                           'Kiểm tra browser/profile lock và kết nối Kaggle.'
                           % (timeout, ' '.join(args))) from exc
    if res.returncode != 0:
        err = (res.stderr or res.stdout or '').strip()
        raise RuntimeError('web_session.py %s thất bại (rc=%s): %s'
                           % (' '.join(args), res.returncode, err[-400:]))
    stdout = res.stdout.strip()
    try:
        return json.loads(stdout)
    except Exception:
        return stdout


def _launch_login_detached(account, timeout_minutes, label=None):
    from account_runtime import _account
    """Mở cửa sổ headed đăng nhập (subprocess tách rời — không chặn caller).
    label = username thật của account (hiện banner đỏ trên cửa sổ để khỏi nhầm)."""
    if label is None:
        try:
            label = _account(account).get('username')
        except Exception:
            label = os.path.splitext(os.path.basename(account))[0]
    cmd = [sys.executable, web_session.__file__,
           '--login', '--account', account, '--timeout-minutes', str(timeout_minutes)]
    if label:
        cmd += ['--label', str(label)]
    creationflags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen(cmd, creationflags=creationflags, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cmd


def _ensure_cookie(a, headless=True):
    from account_runtime import _COOKIE_LOGIN_MSG, _account, _get_registry_entry, _set_registry, _web_cli, auto_login_core, login_account_core
    """Đảm bảo web-session cookie sẵn sàng cho /api/i/ trước khi resolve/cancel.
    Deterministic if-else & Self-Healing:
      (1) cookie còn hạn và hợp lệ server -> dùng ngay;
      (2) hết hạn / server_invalid -> refresh headless (hồi phục im lặng nếu browser còn login);
      (3) vẫn hết hạn & có credential -> thử auto_login bằng username/password;
      (4) không có credential hoặc auto-login thất bại -> tự động MỞ CỬA SỔ HEADED LOGIN cho user."""
    method = a.get('login_method')

    def usable_cookie(selected):
        path = selected['cookie_file']
        return not web_session.cookie_status(path)['expired'] and (
            method not in web_session.GOOGLE_LOGIN_METHODS or web_session.same_user(web_session.identify_user(path), selected['username']))

    if usable_cookie(a):
        return a
    # (2) Refresh headless (profile còn login)
    try:
        result = _web_cli(['--account', a['account'], '--headless' if headless else '--headed'])
        if isinstance(result, dict) and result.get('expires_iso'):
            _set_registry(a['account'], cookie_expires=result['expires_iso'])
    except Exception:
        pass
    a2 = _account(a['account'])
    if usable_cookie(a2):
        return a2
    # (3) Vẫn hết hạn -> auto_login bằng credential (nếu account có lưu password)
    reg = {} if method == 'google_passkey' else _get_registry_entry(a['account'])
    if reg.get('kaggle_username') and reg.get('kaggle_password'):
        try:
            auto = auto_login_core(a['account'])
            if auto.get('status') == 'ok':
                a3 = _account(a['account'])
                if usable_cookie(a3):
                    return a3
        except Exception:
            pass
    # (4) Vẫn không được (hoặc account không có password) -> mở cửa sổ headed login
    login = login_account_core(a['account'])
    raise RuntimeError(_COOKIE_LOGIN_MSG % (a['account'], a['account'], login['note']))


def recover_cookie_core(account, headless_timeout=120):
    from account_runtime import _account
    from processes import lock

    a = _account(account)
    with lock(a['cookie_file'] + '.recovery.lock') as acquired:
        if not acquired:
            return {'account': a['account'], 'stage': 'retry', 'note': 'This Kaggle session is already being recovered. Try again in a moment.'}
        return _recover_cookie(a, headless_timeout)


def _recover_cookie(a, headless_timeout):
    from account_runtime import _account, _get_registry_entry, _probe_cookie_liveness, auto_login_core, get_web_session_core, login_account_core
    """Recovery cookie 1 click.

    Có credential thì ưu tiên auto-login (tự điền username + password) trong
    browser context riêng, để không bị khóa bởi cửa sổ Chrome đang mở. Chỉ khi
    auto-login không dùng được mới thử lấy lại session từ profile hiện tại rồi
    mới mở cửa sổ login tay.
    """
    account = a['account']
    errors = []
    auto = None

    # (1) Click Recovery phải thực sự chạy auto-login trước nếu đã lưu credential.
    method = a.get('login_method')
    reg = {} if method == 'google_passkey' else _get_registry_entry(account)
    if reg.get('kaggle_username') and reg.get('kaggle_password'):
        auto = auto_login_core(account)
        if auto.get('status') == 'ok':
            try:
                if _probe_cookie_liveness(account):
                    st = web_session.cookie_status(a['cookie_file'])
                    return {'account': a['account'], 'stage': 'auto_login',
                            'expires_iso': st['expires_iso'], 'via': auto.get('via', 'password'),
                            'note': 'Đã tự điền username/mật khẩu, đăng nhập lại và kiểm tra cookie thành công.'}
                errors.append('auto-login tạo cookie nhưng probe liveness không thành công')
            except Exception as exc:
                errors.append('probe sau auto-login: %s' % str(exc)[:200])
        else:
            errors.append('auto-login: %s' % (auto.get('error') or auto.get('note')
                                               or auto.get('raw') or auto.get('status')))

    # (2) Fallback: profile hiện tại có thể vẫn còn session Kaggle hợp lệ.
    try:
        get_web_session_core(account, headless=True, timeout=headless_timeout)
    except Exception as exc:
        errors.append('headless refresh: %s' % str(exc)[:200])
    try:
        if (method not in web_session.GOOGLE_LOGIN_METHODS or web_session.same_user(web_session.identify_user(a['cookie_file']), a['username'])) and _probe_cookie_liveness(account):
            st = web_session.cookie_status(a['cookie_file'])
            return {'account': a['account'], 'stage': 'refreshed',
                    'expires_iso': st['expires_iso'],
                    'note': 'Cookie đã làm mới từ profile và hoạt động trở lại.'}
    except Exception as exc:
        errors.append('probe cookie: %s' % str(exc)[:200])

    # (3) Vẫn chưa được -> mở headed login, nhưng trả luôn kết quả auto-login để UI
    # không biến lỗi thật thành thông báo mơ hồ.
    login = login_account_core(account)
    login['stage'] = 'login_window_opened'
    if auto:
        login['auto_login'] = auto
    if errors:
        login['note'] = (login.get('note') or '') + ' | ' + ' | '.join(errors)
    return login


def recover_notebook_cookie(account, profile_path):
    """Refresh the selected VS Code Chrome profile before the editor opens it."""
    import account_store
    from account_runtime import _account
    from processes import lock

    if account not in account_store.load_registry()['accounts']:
        return {'stage': 'unavailable', 'note': 'Import this Kaggle account into OpenResearch to recover its session.'}
    a = _account(account)
    username = a['username']
    if not username:
        return {'stage': 'unavailable', 'note': 'Set the Kaggle username for this account in OpenResearch.'}

    profile = Path(profile_path)
    digest = hashlib.sha256(account.encode()).hexdigest()
    if not profile.is_absolute() or profile.name != digest or profile.parent.name != 'kaggle-chrome':
        raise ValueError('Invalid Chrome profile for the selected Kaggle account.')
    # A junction or symlink in VS Code's storage path is fine; the real
    # directory must still be this account's folder inside kaggle-chrome.
    resolved = profile.resolve()
    if resolved.name != digest or resolved.parent.name != 'kaggle-chrome':
        raise ValueError('The selected Chrome profile resolves outside its account directory.')
    profile_dir = str(resolved)

    def result(stage, note):
        return {'account': account, 'stage': stage, 'note': note}

    def verify(record):
        from cookie_client import _IClient
        status, body = _IClient(account=account, cookies=record['cookies']).call('kernels.KernelsService/ListKernelSessions', {}, timeout=8)
        if status == 200 and isinstance(body, dict):
            web_session.save_session(record, a['cookie_file'])
            return True
        if web_session.cookie_rejected(status, body):
            return False
        raise RuntimeError('Kaggle could not verify the recovered Chrome session (HTTP %s).' % status)

    # Finish well inside the bridge's 180 s limit, so Chrome is always closed before a kill.
    deadline = time.monotonic() + 150

    with lock(a['cookie_file'] + '.recovery.lock') as acquired:
        if not acquired:
            return result('retry', 'This account’s Chrome session is already being recovered. Try again in a moment.')
        # Chrome locks and encrypts an open profile, so its session cannot be checked.
        # Open the notebook in that window; Kaggle asks there if it needs sign-in.
        if web_session._chrome_profile_open(profile_dir):
            return result('browser_open', 'Chrome is already open for this account. The notebook opens in that window.')

        try:
            record = web_session.extract_cookies(profile_dir, 'chrome', expected_username=username, timeout_s=30)
            if verify(record):
                return result('refreshed', 'Kaggle session refreshed from Chrome.')
        except web_session.KaggleSignInRequired:
            pass
        except web_session.KaggleAccountMismatch as exc:
            return result('sign_in_required', str(exc))

        method = a['login_method']
        sign_in = ('Choose Sign in with Google and confirm your passkey.' if method == 'google_passkey'
                   else 'Choose Sign in with Google and finish signing in.' if method == 'google_password'
                   else 'Sign in to the selected Kaggle account.')
        if method == 'google_passkey':
            return result('sign_in_required', sign_in)

        credentials = account_store.credentials(account)
        user, password = credentials['kaggle_username'], credentials['kaggle_password']
        if not user or not password:
            return result('sign_in_required', sign_in)
        # Keep 30 s to close Chrome and verify the session.
        login_seconds = min(60, deadline - time.monotonic() - 30)
        if login_seconds < 15:
            return result('sign_in_required', f'Recovery ran out of time. {sign_in}')
        try:
            if method == 'google_password':
                record = web_session.auto_login_google(profile_dir, user, password, username, login_seconds, None)
            else:
                record = web_session.auto_login(profile_dir, 'chrome', user, password, timeout_s=login_seconds,
                                                isolated=False, expected_username=username)
        except (web_session.AutoLoginNeedsVerification, web_session.AutoLoginBlocked) as exc:
            return result('sign_in_required', f'{exc} {sign_in}')
        if verify(record):
            return result('auto_login', 'Kaggle session recovered in Chrome.')
        return result('sign_in_required', sign_in)


def login_account_core(account, timeout_minutes=15):
    from account_runtime import _account, _launch_login_detached
    a = _account(account)
    method = a.get('login_method')
    if method in web_session.GOOGLE_LOGIN_METHODS:
        if not a['username']:
            raise RuntimeError('Set the expected Kaggle username before using Google sign-in recovery.')
        web_session.google_login_chrome()
    os.makedirs(a['profile_dir'], exist_ok=True)
    _launch_login_detached(a['account'], timeout_minutes)
    if method in web_session.GOOGLE_LOGIN_METHODS:
        action = 'dùng passkey' if method == 'google_passkey' else 'hoàn tất đăng nhập Google'
        return {'account': a['account'], 'login_window_opened': True, 'via': method,
                'note': f'Chọn Sign in with Google, {action}, rồi đóng các cửa sổ Chrome của tài khoản này để lưu cookie.'}
    return {'account': a['account'], 'login_window_opened': True,
            'note': 'Đăng nhập trong cửa sổ vừa mở; sau đó chạy get_web_session để lưu cookie.'}


def auto_login_core(account, timeout_minutes=3):
    from account_runtime import _account, _get_registry_entry, _set_registry, _web_cli, login_account_core
    """Khôi phục đăng nhập bằng mật khẩu hoặc mở Chrome để xác nhận passkey.
    - ok: đã login + lưu cookie.
    - needs_manual_verification: hoàn tất trong cửa sổ login.
    - no_credentials / error: tương ứng."""
    a = _account(account)
    method = a.get('login_method')
    if method == 'google_passkey':
        return {**login_account_core(a['account'], timeout_minutes), 'status': 'needs_manual_verification'}
    reg = _get_registry_entry(account)
    user = reg.get('kaggle_username')
    pwd = reg.get('kaggle_password')
    if not user or not pwd:
        return {'account': a['account'], 'status': 'no_credentials',
                'note': 'Chưa lưu credential. Chạy store_credentials trước.'}
    env = None if method == 'google_password' else {'KG_AUTO_USER': user, 'KG_AUTO_PASS': pwd}
    cli_args = ['--auto-login', '--account', a['account'], '--timeout-minutes', str(timeout_minutes)]
    try:
        result = _web_cli(cli_args, timeout=timeout_minutes * 60 + 60, env=env)
    except Exception as exc:
        return {'account': a['account'], 'status': 'error', 'error': str(exc)[:300]}
    if not isinstance(result, dict):
        return {'account': a['account'], 'status': 'error',
                'error': 'auto-login trả về kết quả không hợp lệ: %s' % str(result)[:300]}
    status = result.get('status')
    if status == 'ok':
        if result.get('expires_iso'):
            _set_registry(a['account'], cookie_expires=result['expires_iso'])
        return {'account': a['account'], 'status': 'ok',
                'via': 'google_password' if method == 'google_password' else 'password',
                'expires_iso': result.get('expires_iso'), 'username': result.get('username'),
                'note': 'Đã tự đăng nhập lại và lưu cookie.'}
    if status == 'needs_manual_verification':
        return {'account': a['account'], 'status': 'needs_manual_verification',
                'note': 'Đã tự điền username/mật khẩu nhưng Kaggle yêu cầu xác minh thêm. '
                        'Hãy dùng Login để hoàn tất trong trình duyệt.'}
    return {'account': a['account'], 'status': status or 'error', 'raw': str(result)[:300]}
