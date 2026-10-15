"""Original persistent cookie client and mtime cache for idle verification."""
import os,json,threading,http.client,urllib.parse,time
from provider_metrics import record as record_metrics
import web_session
from account_runtime import _account

class _IClient:
    """Client /api/i/ PERSISTENT cho 1 account (cookie auth) — tái dùng 1 kết nối HTTPS
    thay vì mở mới mỗi request (tiết kiệm TCP+TLS handshake). Cookie header được build
    từ cookie jar của account; X-XSRF-TOKEN lấy từ cookie XSRF-TOKEN/CSRF-TOKEN."""

    def __init__(self, cookie_file, account='external'):
        self._account = account
        self._cookie_file = cookie_file  # để invalidate_cookie() khi server từ chối cookie
        jar = web_session.cookie_jar(cookie_file)
        self._cookie_header = '; '.join('%s=%s' % (c.name, c.value) for c in jar if c.value)
        # QUAN TRỌNG: phải ưu tiên cookie XSRF-TOKEN (CSRF-TOKEN là giá trị KHÁC —
        # dùng nhầm sẽ bị server trả 400).
        xsrf = next((c.value for c in jar if c.name == 'XSRF-TOKEN' and c.value), None)
        if not xsrf:
            xsrf = next((c.value for c in jar if c.name == 'CSRF-TOKEN' and c.value), None)
        self._xsrf = urllib.parse.unquote(xsrf) if xsrf else None
        self._conn = None
        self._lock = threading.Lock()

    def _drop(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def call(self, method, body, timeout=60):
        """POST /api/i/<method>. Trả (status, parsed_json_or_str). Reconnect 1 lần khi stale."""
        data = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        if self._cookie_header:
            headers['Cookie'] = self._cookie_header
        if self._xsrf:
            headers['X-XSRF-TOKEN'] = self._xsrf
        for attempt in range(2):
            with self._lock:
                if self._conn is None:
                    self._conn = http.client.HTTPSConnection('www.kaggle.com', 443, timeout=timeout)
                conn = self._conn
                started, response_bytes, failed = time.perf_counter(), 0, True
                try:
                    conn.request('POST', '/api/i/' + method, body=data, headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    response_bytes = len(raw)
                    status = resp.status
                    failed = status >= 400
                    if getattr(resp, 'will_close', False):
                        self._drop()
                    text = raw.decode('utf-8', 'ignore')
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        parsed = text
                    if web_session.cookie_rejected(status, text):
                        web_session.invalidate_cookie(self._cookie_file, 'HTTP %s in _IClient (%s)' % (status, method))
                    return status, parsed
                except (http.client.HTTPException, OSError) as exc:
                    self._drop()
                    if attempt == 1:
                        raise RuntimeError('_IClient %s thất bại: %s' % (method, exc)) from exc
                finally:
                    record_metrics(self._account, len(data), response_bytes, started, failed)
        raise RuntimeError('_IClient unreachable')


_iclient_cache = {}  # account key -> (cookie_file_mtime, _IClient)
_iclient_lock = threading.Lock()


def _client_for(account):
    """Lấy (và cache) _IClient theo account. QUAN TRỌNG: cookie file có thể bị thay bằng
    cookie MỚI (recovery/login viết đè) — cache phải rebuild khi mtime file thay đổi,
    nếu không sẽ mãi dùng cookie cũ bị thu hồi (401) dù đã đăng nhập lại."""
    a = _account(account)
    try:
        mtime = os.path.getmtime(a['cookie_file'])
    except OSError:
        mtime = 0
    with _iclient_lock:
        entry = _iclient_cache.get(a['account'])
        if entry is None or entry[0] != mtime:
            entry = (mtime, _IClient(a['cookie_file'], a['account']))
            _iclient_cache[a['account']] = entry
        return entry[1]
