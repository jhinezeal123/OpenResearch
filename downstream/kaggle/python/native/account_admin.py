"""Selected account/quota/session operations adapted from interface_old/kaggle_pool.

No donor imports, notebook submissions, credential values in responses or implicit
browser recovery. Cookie renewal is an explicit operation.
"""
from datetime import datetime, timezone
import json
import math
import re
import shutil
import urllib.request

import account_store
import account_runtime
import web_session


def now():
    return datetime.now(timezone.utc).isoformat()


def _selected(account):
    key, entry = account_store.resolve(account)
    return key, entry, account_runtime._account(key)


def _quota_from_cookie(account, username, data):
    """Donor quota parser; unknown/malformed durations remain unknown, never zero."""
    item = {'account': account, 'username': username, 'refresh_at': data.get('quotaRefreshTime')}
    for name, quota in (('gpu', data.get('gpuQuota')), ('tpu', data.get('tpuQuota'))):
        if not isinstance(quota, dict):
            continue
        try:
            used = float(str(quota.get('timeUsed', '0s')).removesuffix('s')) / 3600
            total = float(str(quota['totalTimeAllowed']).removesuffix('s')) / 3600
            if not all(math.isfinite(value) for value in (used, total)) or used < 0 or total <= 0:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        item[name] = {'used_h': round(used, 2), 'remaining_h': round(max(0, total-used), 2), 'total_h': round(total, 2)}
    return item


def parse_active_sessions(data, refs=None):
    """Donor's ListKernelSessions parser, preserving every active version."""
    raw = data.get('sessions', [])
    count = data.get('totalCount', len(raw) if isinstance(raw, list) else None)
    if not isinstance(raw, list) or type(count) is not int or count < len(raw):
        raise ValueError('Kaggle chưa xác minh được danh sách session')
    rows = []
    for session in raw:
        kid = int(session.get('kernelId') or 0)
        rows.append({'ref': (refs or {}).get(kid, ''), 'title': session.get('title') or '',
            'kernel_id': kid, 'kernel_session_id': int(session.get('kernelRunId') or 0),
            'kernel_version_number': int(session.get('versionNumber') or 0),
            'status': 'RUNNING', 'version_type': session.get('type'),
            'accelerator': session.get('accelerator'), 'last_run_time': session.get('dateCreated')})
    return {'total_count': count, 'quota_limits': data.get('quotaLimits') or {}, 'sessions': rows}


def overview(account):
    from cookie_client import _client_for
    key, entry, selected = _selected(account)
    status = web_session.cookie_status(selected['cookie_file'])
    cookie = {name: status.get(name) for name in ('expires_iso', 'valid_for_hours', 'expired')}
    cookie['status'] = 'revoked' if status.get('server_invalid') else 'expired' if status['expired'] else 'unverified'
    result = {'account': key, 'username': entry['username'], 'observed_at': now(),
              'cookie': cookie, 'quota': None, 'sessions': None, 'errors': []}
    if status['expired']:
        return result
    if web_session.identify_user(selected['cookie_file']) != entry['username']:
        cookie.update(status='identity_mismatch', expired=True)
        return result
    client = _client_for(key)
    for method, field in [('GetAcceleratorQuotaStatistics', 'quota'), ('ListKernelSessions', 'sessions')]:
        try:
            http, data = client.call('kernels.KernelsService/' + method, {}, timeout=20)
            if http == 200 and isinstance(data, dict):
                cookie['status'] = 'valid'
                if field == 'quota':
                    result[field] = _quota_from_cookie(key, entry['username'], data)
                else:
                    refs = {}
                    for row in data.get('sessions') or []:
                        kid = int(row.get('kernelId') or 0)
                        if kid and kid not in refs:
                            try:
                                code, kernel = client.call('kernels.KernelsService/GetKernel', {'kernelId': kid}, timeout=20)
                            except Exception:
                                # Reference enrichment must not hide an active session.
                                continue
                            if code == 200 and isinstance(kernel, dict):
                                owner = (kernel.get('author') or {}).get('userName')
                                slug = kernel.get('slug')
                                if isinstance(owner, str) and isinstance(slug, str):
                                    refs[kid] = owner + '/' + slug
                    result[field] = parse_active_sessions(data, refs)
            else:
                # Only a verified auth rejection may trigger password login.
                current = web_session.cookie_status(selected['cookie_file'])
                if current.get('server_invalid'):
                    cookie.update(status='revoked', expired=True)
                    break
                result['errors'].append('Không đọc được ' + ('quota' if field == 'quota' else 'session') + f' (HTTP {http})')
        except Exception:
            result['errors'].append('Chưa kết nối được Kaggle để đọc ' + field)
    return result


def check_cookie(account):
    key, entry, selected = _selected(account)
    before = overview(key)
    invalid = before['cookie']['status'] in {'expired', 'revoked', 'identity_mismatch'}
    if not invalid:
        return {'account': key, 'status': 'valid' if before['cookie']['status'] == 'valid' else 'unavailable',
                'was_expired': False, 'auto_login_attempted': False}
    login = account_runtime.auto_login_core(key)
    state = login.get('status')
    if state != 'ok':
        messages = {'no_credentials': 'Chưa có username/mật khẩu để tự đăng nhập.',
                    'needs_manual_verification': 'Kaggle yêu cầu xác minh bổ sung; cần đăng nhập thủ công.',
                    'blocked': 'Kaggle từ chối đăng nhập; kiểm tra mật khẩu hoặc chờ hết khóa tạm.'}
        return {'account': key, 'status': state or 'error', 'was_expired': True,
                'auto_login_attempted': True, 'message': messages.get(state, 'Tự đăng nhập chưa thành công; không thử lại tự động.')}
    after = overview(key)
    valid = after['cookie']['status'] == 'valid' and web_session.identify_user(selected['cookie_file']) == entry['username']
    return {'account': key, 'status': 'renewed' if valid else 'error', 'was_expired': True,
            'auto_login_attempted': True,
            'message': 'Đã tự đăng nhập và xác minh cookie mới.' if valid else 'Cookie mới chưa được xác minh đúng account.'}


def token_username(token):
    request = urllib.request.Request('https://www.kaggle.com/api/v1/oauth2/introspect',
        data=json.dumps({'token': token}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.load(response)
        return data.get('username') if isinstance(data, dict) else None
    except Exception:
        raise ValueError('Không xác minh được token với Kaggle; kiểm tra token và kết nối') from None


def add_account(token, username, password):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', username):
        raise ValueError('Tên tài khoản Kaggle chỉ gồm chữ, số, gạch ngang hoặc gạch dưới')
    if not isinstance(token, str) or not re.fullmatch(r'KGAT_[A-Za-z0-9_.=\-]{8,1000}', token.strip()):
        raise ValueError('Token Kaggle phải có dạng KGAT_...')
    if not isinstance(password, str) or not 1 <= len(password) <= 512:
        raise ValueError('Cần mật khẩu để tự đăng nhập')
    username, token = username.lower(), token.strip()
    registry = account_store.load_registry()
    found = next(((key, value) for key, value in registry['accounts'].items()
                  if value.get('username', '').lower() == username or value.get('alias', '').lower() == username), None)
    if found:
        raise ValueError('Account đã có trong proxy')
    if token_username(token) != username:
        raise ValueError('Token không thuộc tên tài khoản đã nhập')
    key, alias = username + '.txt', username
    profile = account_store.PROFILES / alias
    if profile.is_symlink() or not profile.resolve().is_relative_to(account_store.PROFILES.resolve()):
        raise ValueError('Profile account không hợp lệ')
    if profile.exists():
        raise ValueError('Profile đã tồn tại; không ghi đè')
    profile.mkdir(parents=True, exist_ok=True)
    web_session.protect_acl(str(profile))
    target = profile / 'token.txt'
    if target.is_symlink():
        raise ValueError('Token profile không hợp lệ')
    target.write_text(token + '\n', encoding='utf-8')
    web_session.protect_acl(str(target))
    with account_store.REGISTRY_LOCK:
        registry = account_store.load_registry()
        registry['accounts'][key] = {'alias': alias, 'username': username,
            'token_file': f'profiles/{alias}/token.txt', 'profile_dir': f'profiles/{alias}',
            'cookie_file': f'profiles/{alias}/web-session.json', 'browser': 'coccoc'}
        account_store.write_json(account_store.REGISTRY, registry)
    web_session.protect_acl(str(account_store.REGISTRY))
    account_store.store_credentials(key, username, password)
    return {'account': key, 'username': username, 'created': True}


def remove_account(account):
    key, entry, _ = _selected(account)
    profile = account_store.PROFILES / entry['alias']
    if (profile.is_symlink() or profile.resolve().parent != account_store.PROFILES.resolve()
            or any(path.is_symlink() or not path.resolve().is_relative_to(profile.resolve()) for path in profile.rglob('*'))):
        raise ValueError('Profile account không hợp lệ; không xóa đường dẫn ngoài bundle')
    if profile.exists():
        idle = account_runtime.account_idle(key, recover=False)
        if idle.get('idle') is not True or idle.get('active_session_count') != 0:
            if type(idle.get('active_session_count')) is int and idle['active_session_count'] > 0:
                raise ValueError('Account còn session đang chạy; chưa thể xóa')
            raise ValueError('Chưa xác minh account đã hết session; Check cookie rồi thử xóa lại')
    # The exact resolved profile is checked before recursive permanent deletion.
    if profile.exists():
        shutil.rmtree(profile)
    with account_store.REGISTRY_LOCK, account_store._CREDENTIAL_LOCK:
        credentials = account_store.read_json(account_store.CREDENTIALS, {'version': 1, 'accounts': {}})
        credentials['accounts'].pop(key, None)
        account_store.write_json(account_store.CREDENTIALS, credentials)
        registry = account_store.load_registry()
        registry['accounts'].pop(key)
        account_store.write_json(account_store.REGISTRY, registry)
    web_session.protect_acl(str(account_store.REGISTRY))
    web_session.protect_acl(str(account_store.CREDENTIALS))
    return {'account': key, 'removed': True, 'permanent': True}


def dispatch(action, account, arguments=None):
    try:
        if action == 'account-overview':
            return overview(account)
        if action == 'cookie-check':
            return check_cookie(account)
        if action == 'account-add':
            return add_account(**(arguments or {}))
        if action == 'account-remove':
            return remove_account(account)
        raise ValueError('Thao tác account không hợp lệ')
    except ValueError as exc:
        return {'error': str(exc)}
    except Exception:
        return {'error': 'Không thực hiện được thao tác Kaggle; kiểm tra kết nối và đăng nhập'}
