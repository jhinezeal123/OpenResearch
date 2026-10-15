"""Profile-owned tokens and one editable login configuration."""
import json
import os
from pathlib import Path
import threading

BASE = Path(os.environ["ORX_KAGGLE_HOME"]).resolve()
PROFILES = BASE / 'profiles'
REGISTRY = PROFILES / 'accounts.json'
CREDENTIALS = PROFILES / 'credentials.json'
REGISTRY_LOCK = threading.RLock()
_CREDENTIAL_LOCK = threading.RLock()


def read_json(path, default):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise RuntimeError(f'Cannot read account configuration: {path}') from None
    if not isinstance(value, dict) or not isinstance(value.get('accounts'), dict):
        raise RuntimeError(f'Invalid account configuration: {path}')
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def load_registry():
    return read_json(REGISTRY, {'version': 1, 'accounts': {}})


def resolve(account):
    records = load_registry()['accounts']
    name = str(account)
    stem = Path(name).stem
    for key, entry in records.items():
        if name == key or name == entry.get('alias') or stem == Path(key).stem:
            return key, entry
    for key, path in token_files():
        if name in {key, path.parent.name} or stem == Path(key).stem:
            return key, {'alias': path.parent.name, 'token_file': path.relative_to(BASE).as_posix()}
    raise ValueError(f'Unknown Kaggle account: {name}')


def token_path(key, entry=None):
    if entry is None:
        _, entry = resolve(key)
    path = BASE / entry.get('token_file', f'profiles/{entry.get("alias") or Path(key).stem}/token.txt')
    path = path.resolve()
    if not path.is_relative_to(PROFILES.resolve()) or path.name != 'token.txt':
        raise ValueError('Token path must be inside its account profile')
    return path


def token_files():
    registered = load_registry()['accounts']
    seen = set()
    for key, entry in registered.items():
        path = token_path(key, entry)
        seen.add(path)
        yield key, path
    if PROFILES.is_dir():
        for profile in sorted(PROFILES.iterdir()):
            if profile.is_dir() and not profile.is_symlink():
                path = (profile / 'token.txt').resolve()
                if path.is_file() and path not in seen:
                    yield profile.name + '.txt', path


def read_token(account):
    key, entry = resolve(account)
    token = token_path(key, entry).read_text(encoding='utf-8-sig').strip()
    if not token.startswith('KGAT_'):
        raise ValueError('Selected profile has an invalid Kaggle API token')
    return token


def credentials(account):
    key, _ = resolve(account)
    value = read_json(CREDENTIALS, {'version': 1, 'accounts': {}})['accounts'].get(key, {})
    return {'kaggle_username': value.get('username'), 'kaggle_password': value.get('password')}


def store_credentials(account, username=None, password=None):
    key, _ = resolve(account)
    with _CREDENTIAL_LOCK:
        document = read_json(CREDENTIALS, {'version': 1, 'accounts': {}})
        entry = document['accounts'].setdefault(key, {})
        if username is not None:
            entry['username'] = username
        if password is not None:
            entry['password'] = password
        write_json(CREDENTIALS, document)
    from auto_login.web_session import protect_acl
    protect_acl(str(CREDENTIALS))
    return credentials(key)
