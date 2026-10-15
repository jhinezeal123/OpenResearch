# Kaggle API proxy: route requests qua nhiều account token, fallback khi token hết quota
# Cách dùng: python proxy.py [port] (mặc định 8013 cho bản đóng gói).
# SDK trỏ trực tiếp http://127.0.0.1:<port>/api/v1/... qua session.client().
#
# HIỆU NĂNG: giữ một pool kết nối HTTPS persistent tới upstream (kaggle.com) để TÁI SỬ
# DỤNG TCP+TLS giữa các request (trước đây mỗi request mở 1 kết nối mới -> tốn ~0.3-1s
# handshake mỗi lần). Upstream response luôn được đọc đủ body rồi trả về client qua
# Content-Length (không còn Connection: close) nên client <-> proxy cũng giữ kết nối.
import http.server, urllib.request, urllib.parse, urllib.error, http.client, ssl, threading, sys, time, os, re

import account_store
from provider_metrics import record as record_metrics

UPSTREAM = os.environ.get('KAGGLE_PROXY_UPSTREAM', 'https://www.kaggle.com')
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8013
BASE = os.path.dirname(os.path.abspath(__file__))

# Windows console cp1252 không in được ký tự Unicode (vd tiếng Việt trong message lỗi
# upstream) -> print() ném UnicodeEncodeError làm crash handler. Ép replace + utf-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# kagglesdk 2.2.3 crash khi duration dạng "108000s" (không có phần thập phân):
# normalize thành "108000.000s" cho mọi JSON response
_DUR = re.compile(rb'"(-?\d+)s"')

def _fix_json(body):
    return _DUR.sub(rb'"\1.000s"', body)


# ---------------------------------------------------------------- upstream pool
class _UpstreamPool:
    """Pool kết nối HTTPS(UPSTREAM) persistent. Acquire 1 conn, request + đọc đủ body,
    release lại để tái dùng. Khi kết nối đã đóng (stale) -> đóng và thử lại 1 lần."""

    def __init__(self, url, maxsize=8, timeout=300):
        u = urllib.parse.urlsplit(url)
        self.host = u.hostname
        self.port = u.port or ({'http': 80, 'https': 443}.get(u.scheme, 443))
        self.is_https = u.scheme == 'https'
        self.maxsize = maxsize
        self.timeout = timeout
        self._idle = []
        self._lock = threading.Lock()
        self._reused = 0
        self._created = 0

    def _new(self):
        self._created += 1
        if self.is_https:
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def acquire(self):
        """Lấy 1 conn sẵn sàng; trả (conn, reused_bool)."""
        with self._lock:
            if self._idle:
                self._reused += 1
                return self._idle.pop(), True
        return self._new(), False

    def release(self, conn, healthy=True):
        if healthy:
            with self._lock:
                if len(self._idle) < self.maxsize:
                    self._idle.append(conn)
                    return
        try:
            conn.close()
        except Exception:
            pass

    def _drop_idle(self):
        """Vứt hết kết nối đang rảnh.

        Gọi khi một kết nối phát hiện đã chết: các kết nối khác trong cùng chồng
        thường cũng bị Kaggle đóng, vì chúng nằm không cùng một khoảng thời gian.
        Không dọn thì lần thử lại sẽ vớ phải kết nối chết thứ hai (đúng lỗi
        2026-09-13: SaveKernel -> 502 dù token còn tốt)."""
        with self._lock:
            idle, self._idle = self._idle, []
        for c in idle:
            try:
                c.close()
            except Exception:
                pass

    def request(self, method, path, body, headers):
        """Gửi request lên upstream, đọc đủ body. Trả (conn, status, resp_headers, body).
        Conn được release vào pool (hoặc đóng nếu stale)."""
        # Saving a notebook can launch a paid/quota-consuming session. A lost
        # response does not prove rejection: never replay that POST in the pool.
        save = method == 'POST' and urllib.parse.urlsplit(path).path in {
            '/api/v1/kernels.KernelsApiService/SaveKernel', '/api/v1/kernels/push',
            '/v1/kernels.KernelsApiService/SaveKernel'}
        attempts = 1 if save else 2
        for attempt in range(attempts):
            # Avoid stale idle connections for the single allowed save attempt.
            conn, reused = (self._new(), False) if save else self.acquire()
            started, response_bytes, failed = time.perf_counter(), 0, True
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                response_bytes = len(data)
                rheaders = resp.getheaders()
                status = resp.status
                failed = status >= 400
                self.release(conn, healthy=True)
                return status, rheaders, data, reused
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionResetError, BrokenPipeError, ssl.SSLError) as exc:
                self.release(conn, healthy=False)
                if attempt == attempts - 1:
                    raise
                self._log_upstream('stale conn, reconnect 1 lần: %s' % exc)
                self._drop_idle()  # chồng còn lại có thể cũng chết -> lần 2 mở MỚI
            except Exception:
                self.release(conn, healthy=False)
                raise
            finally:
                auth = headers.get('Authorization', '')
                selected = next((tk[0] for tk in tokens if auth == 'Bearer ' + tk[1]), 'external')
                record_metrics(selected, len(body or b''), response_bytes, started, failed)

    def _log_upstream(self, msg):
        print('[%s] [pool] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


POOL = _UpstreamPool(UPSTREAM)

# tokens: [name, token, n_requests, blocked_until]
tokens = []
token_errors = []
for f, token_path in account_store.token_files():
    try:
        t = token_path.read_text(encoding='utf-8-sig').strip()
    except OSError:
        token_errors.append(f + ' (unreadable profile token)')
        continue
    if t.startswith('KGAT_'):
        tokens.append([f, t, 0, 0.0])

def pick_token(pool=None):
    now = time.time()
    avail = [tk for tk in (pool or tokens) if tk[3] <= now]
    if not avail:
        return None
    return min(avail, key=lambda tk: tk[2])  # token dùng ít nhất


_token_lock = threading.Lock()
_token_signature = None


def refresh_tokens():
    """Pick up account additions/removals and token rotation without restarting."""
    global tokens, token_errors, _token_signature
    with _token_lock:
        paths = list(account_store.token_files())
        signature = []
        for name, path in paths:
            try:
                info = path.stat()
                signature.append((name, info.st_mtime_ns, info.st_size))
            except OSError:
                signature.append((name, None, None))
        if signature == _token_signature:
            return
        existing = {(row[0], row[1]): row for row in tokens}
        loaded, errors = [], []
        for name, path in paths:
            try:
                value = path.read_text(encoding='utf-8-sig').strip()
                if value.startswith('KGAT_'):
                    loaded.append(existing.get((name, value), [name, value, 0, 0.0]))
            except OSError:
                errors.append(name + ' (unreadable profile token)')
        tokens, token_errors, _token_signature = loaded, errors, signature

HOP = {'connection', 'transfer-encoding', 'keep-alive', 'accept-encoding', 'host', 'authorization', 'content-length', 'set-cookie', 'proxy-connection'}

def _build_headers(client_headers, auth):
    headers = {}
    for k, v in client_headers.items():
        if k.lower() not in HOP:
            headers[k] = v
    headers['Authorization'] = auth
    return headers


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _log(self, msg):
        print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)

    def _do(self):
        refresh_tokens()
        if self.command == 'GET' and self.path == '/_kaggle_proxy_health':
            import json
            body = json.dumps({'service': 'ai-scientist-kaggle-proxy', 'root': BASE, 'token_count': len(tokens)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        # Nếu client tự chọn token (Authorization Bearer KGAT_...): dùng đúng token đó,
        # không round-robin/fallback. Token ngoài pool thì forward thẳng với token client.
        candidates = tokens
        req_auth = self.headers.get('Authorization', '')
        explicit = None
        if req_auth.startswith('Bearer KGAT_'):
            t = req_auth[len('Bearer '):].strip()
            explicit = next((tk for tk in tokens if tk[1] == t), None)
            candidates = [explicit] if explicit else []
        if req_auth.startswith('Bearer KGAT_') and not candidates:
            self._forward_once(body, req_auth)  # token ngoài pool: forward thẳng
            return
        last_err = None
        for _ in range(len(candidates)):
            tk = pick_token(candidates)
            if tk is None:
                break
            headers = _build_headers(self.headers, 'Bearer ' + tk[1])
            try:
                status, rheaders, rbody, reused = POOL.request(self.command, self.path, body or None, headers)
                tk[2] += 1
                # http.client không raise HTTPError — kiểm tra status trực tiếp.
                quota = status == 429 or (status == 403 and b'quota' in (rbody or b'').lower())
                if quota:
                    retry = dict(rheaders).get('Retry-After')
                    try:
                        retry_v = float(retry)
                    except (TypeError, ValueError):
                        retry_v = 3600 if status == 403 else 300
                    tk[3] = time.time() + retry_v
                    last_err = (status, rbody)
                    self._log('%s QUOTA-BLOCKED token %s code=%d' % (self.path, tk[0], status))
                    continue  # thử token khác
                resp_headers = dict(rheaders)
                if 'application/json' in resp_headers.get('Content-Type', ''):
                    rbody = _fix_json(rbody)
                self._respond_bytes(status, resp_headers, rbody)
                self._log('%s %s -> %d (token %s, %d reqs%s)' %
                          (self.command, self.path, status, tk[0], tk[2], ', reuse' if reused else ''))
                return
            except urllib.error.URLError as ex:
                self._log('UPSTREAM FAIL %s: %s' % (tk[0], ex))
                last_err = (502, ('upstream fail (token %s): %s' % (tk[0], ex)).encode('utf-8', 'replace'))
                continue
            except Exception as ex:
                self._log('UPSTREAM FAIL %s: %s' % (tk[0], ex))
                last_err = (502, ('upstream fail (token %s): %s' % (tk[0], ex)).encode('utf-8', 'replace'))
                continue
        code, body = last_err if last_err else (502, b'no token available')
        self._respond_err(code, body)
        self._log('%s -> %d (ALL TOKENS EXHAUSTED)' % (self.path, code))

    def _forward_once(self, body, auth):
        """Forward 1 request với Authorization client cho sẵn (token ngoài pool)."""
        headers = _build_headers(self.headers, auth)
        try:
            status, rheaders, rbody, reused = POOL.request(self.command, self.path, body or None, headers)
            resp_headers = dict(rheaders)
            if 'application/json' in resp_headers.get('Content-Type', ''):
                rbody = _fix_json(rbody)
            self._respond_bytes(status, resp_headers, rbody)
            self._log('%s %s -> %d (external token%s)' %
                      (self.command, self.path, status, ', reuse' if reused else ''))
        except Exception as ex:
            self._respond_err(502, str(ex).encode())

    def _respond_bytes(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in HOP and k.lower() != 'content-length':
                self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_err(self, code, body):
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *a):
        pass  # bỏ log mặc định, dùng _log

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _do

def main():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), H)
    print('kaggle-proxy: http://127.0.0.1:%d  tokens=%d -> %s (upstream keep-alive pool)' % (PORT, len(tokens), UPSTREAM), flush=True)
    srv.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
