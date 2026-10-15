"""Account resolution and account-wide idle guard, without the old tool suite."""
from pathlib import Path
from datetime import datetime, timezone
import account_store
import web_session
from auto_login.recovery import (
    _web_cli, _launch_login_detached, _ensure_cookie,
    auto_login_core, login_account_core, get_web_session_core,
)
from sdk_version import _assert_pinned_sdk

_COOKIE_LOGIN_MSG = 'Cookie của %s chưa dùng được. Đã mở đăng nhập cho %s. %s'


def _account(account):
    key, entry = account_store.resolve(account)
    alias = entry.get('alias') or Path(key).stem
    profile = account_store.PROFILES / alias
    return {'account': key, 'username': entry.get('username'), 'alias': alias,
            'browser': entry.get('browser', 'coccoc'), 'profile_dir': str(profile),
            'cookie_file': str(profile / 'web-session.json')}


def _get_registry_entry(account):
    key, entry = account_store.resolve(account)
    return {**entry, **account_store.credentials(key)}


def _set_registry(account_key, **fields):
    return web_session.update_registry(account_key, **fields)


def _probe_cookie_liveness(account):
    from cookie_client import _client_for
    status, body = _client_for(account).call('kernels.KernelsService/ListKernelSessions', {})
    return status == 200 and isinstance(body, dict)


def account_idle(account, recover=True):
    from cookie_client import _client_for
    _assert_pinned_sdk()
    selected = _ensure_cookie(_account(account), headless=True) if recover else _account(account)
    if not selected['username'] or web_session.identify_user(selected['cookie_file']) != selected['username']:
        raise RuntimeError('Cookie identity differs from selected account')
    status, data = _client_for(account).call('kernels.KernelsService/ListKernelSessions', {})
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError('Cannot verify active Kaggle sessions')
    sessions = data.get('sessions', [])
    count = data.get('totalCount', len(sessions) if isinstance(sessions, list) else None)
    if not isinstance(sessions, list) or type(count) is not int or count < len(sessions):
        raise RuntimeError('Active session response is unverified')
    # Reuse Settings' session parser, including versions, to reconcile local
    # reservations with sessions already visible at Kaggle (count each once).
    from account_admin import parse_active_sessions
    refs = {}
    for session in sessions:
        kid = int(session.get('kernelId') or 0)
        if kid and kid not in refs:
            try:
                code, kernel = _client_for(account).call('kernels.KernelsService/GetKernel', {'kernelId': kid}, timeout=15)
                if code == 200 and isinstance(kernel, dict):
                    owner, slug = (kernel.get('author') or {}).get('userName'), kernel.get('slug')
                    if isinstance(owner, str) and isinstance(slug, str):
                        refs[kid] = owner + '/' + slug
            except Exception:
                pass  # Missing identity stays occupied; never infer a free slot.
    return {'account': selected['account'], 'username': selected['username'],
            'active_session_count': count, 'idle': count == 0 and not sessions,
            'active_sessions': parse_active_sessions(data, refs)['sessions'],
            'observed_at': datetime.now(timezone.utc).isoformat()}


def account_readiness(account):
    """Observe readiness without opening a login window or refreshing a browser."""
    selected = _account(account)
    def needs_login():
        return {'account': selected['account'], 'username': selected['username'],
                'readiness': 'needs_login', 'idle': False, 'active_session_count': None,
                'observed_at': datetime.now(timezone.utc).isoformat()}
    if (web_session.cookie_status(selected['cookie_file'])['expired']
            or web_session.identify_user(selected['cookie_file']) != selected['username']):
        return needs_login()
    try:
        observation = account_idle(account, recover=False)
    except RuntimeError:
        # A server auth rejection marks the cookie invalid in cookie_client.
        # Network failures alone must never trigger a password login.
        if web_session.cookie_status(selected['cookie_file'])['expired']:
            return needs_login()
        raise
    return {**observation, 'readiness': 'verified_idle' if observation['idle'] else 'busy'}
