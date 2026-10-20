"""Kaggle session-list parsing adapted from interface_old/kaggle_pool.

Only the parser used by account readiness is kept; the donor's account CRUD,
quota and cookie commands have no caller in OpenResearch.
"""


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
