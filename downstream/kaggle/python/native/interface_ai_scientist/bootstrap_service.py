"""Idempotent Kaggle bootstrap orchestration, independent of its caller."""
from types import SimpleNamespace
from pathlib import Path
import uuid
from . import session


def start(account: str, ttl_seconds: int = 480, accelerator: str = 'cpu',
          competition_sources: list[str] | None = None,
          dataset_sources: list[str] | None = None, wait_seconds: int = 240,
          session_id: str | None = None, request_id: str | None = None) -> dict:
    """Submit one private Kaggle bootstrap, wait for SSH, and return its terminal command.

    Accelerators: cpu, NvidiaTeslaT4 (T4 x2), TpuV5E8, TpuV6E8.
    gpu/NvidiaT4 select T4; tpu selects v5e-8. Tokyo is the selected relay.
    With session_id, ONLY wait/reconnect to that saved session, never submit.
    Its accelerator, TTL and sources remain the saved values. Account must match.
    On PENDING or UNKNOWN, reuse the returned session_id in this operation.
    request_id makes backend admission idempotent, including interrupted calls.
    A new call without session_id/request_id starts a new session.
    Code, logs, files and STOP marker are handled in the persistent SSH terminal.
    """
    if type(ttl_seconds) is not int or ttl_seconds < 60:
        raise ValueError('Choose a bootstrap TTL of at least 60 seconds')
    if type(wait_seconds) is not int or wait_seconds < 1:
        raise ValueError('SSH wait must be a positive number of seconds')
    accelerator = session.accelerator_name(accelerator)
    for sources in (competition_sources or [], dataset_sources or []):
        if not isinstance(sources, list) or any(not isinstance(item, str) or not item
                or len(item) > 500 or any(c in item for c in '\r\n') for item in sources):
            raise ValueError('Invalid Kaggle source references')
    if request_id is not None:
        import re
        if not re.fullmatch('[0-9a-f]{32}', request_id):
            raise ValueError('Expected a UUID request ID')
        if session_id is not None and session_id != request_id:
            raise ValueError('Request and session IDs differ')
        saved = session.DEFAULT_STATE / request_id / 'state.json'
        if saved.is_file():
            session_id = request_id
    if session_id is not None:
        state = session.state_for(session_id)
        selected_account, _ = session.account_store.resolve(account)
        if state['account'] != selected_account:
            raise ValueError('Saved SSH session belongs to another account')
        if not (Path(state['root']) / 'submit-intent.json').is_file():
            return {**session.descriptor(state), 'submission_status': 'NOT_SUBMITTED',
                    'ssh_status': 'NOT_STARTED', 'required_action': 'This saved session has not been submitted.'}
    else:
        request_id = request_id or uuid.uuid4().hex
        with session.request_lock(request_id):
            root = session.DEFAULT_STATE / request_id
            if (root / 'cancel-intent.json').is_file() and not (root / 'state.json').is_file():
                return session.cancelled_descriptor(account, request_id, ttl_seconds)
            if (root / 'state.json').is_file():
                state = session.state_for(request_id)
                selected_account, _ = session.account_store.resolve(account)
                if state['account'] != selected_account:
                    raise ValueError('Saved SSH session belongs to another account')
                if not (root / 'submit-intent.json').is_file():
                    return {**session.descriptor(state), 'submission_status': 'NOT_SUBMITTED', 'ssh_status': 'NOT_STARTED'}
            else:
                args = SimpleNamespace(account=account, config=None, state_root=session.DEFAULT_STATE,
                                       ttl=ttl_seconds, accelerator=accelerator,
                                       competition_sources=competition_sources or [], dataset_sources=dataset_sources or [],
                                       request_id=request_id)
                state = session.prepare(args)
                try:
                    session.push(state)
                except Exception as exc:
                    attempted = (Path(state['root']) / 'submit-intent.json').is_file()
                    return {**session.descriptor(state),
                            'submission_status': 'UNKNOWN' if attempted else 'NOT_SUBMITTED',
                            'ssh_status': 'NOT_CHECKED', 'error_type': type(exc).__name__,
                            'required_action': 'Reconnect with this session_id; do not submit it again.'}
    result = {**session.descriptor(state),
              'submission_status': state.get('submission_status', 'UNKNOWN')}
    try:
        return {**result, **session.connect(state, wait_seconds)}
    except (TimeoutError, OSError, RuntimeError) as exc:
        return {**result, 'ssh_status': 'PENDING' if isinstance(exc, TimeoutError) else 'UNAVAILABLE',
                'error_type': type(exc).__name__,
            'required_action': 'Call start with this session_id to check SSH without submitting again.'}
