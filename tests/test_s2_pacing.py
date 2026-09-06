import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _run_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_independent_provider_processes_share_s2_dispatch_gate(tmp_path):
    arrivals = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            arrivals.append(time.monotonic())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            pass

    server, thread = _run_server(Handler)
    url = f"http://127.0.0.1:{server.server_port}/paper"
    script = (
        "import httpx; from bibcite.sources import _s2_get; "
        f"c=httpx.Client(); _s2_get(c, {url!r}, {{}}); c.close()"
    )
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path),
        "S2_API_KEY": "shared-test-key",
    }
    command = ["uv", "run", sys.executable, "-c", script]
    processes = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    try:
        results = [process.communicate(timeout=10) for process in processes]
    finally:
        server.shutdown()
        thread.join()

    assert [process.returncode for process in processes] == [0, 0], results
    assert len(arrivals) == 2
    assert abs(arrivals[1] - arrivals[0]) >= 1.09


def test_provider_process_does_not_retry_real_429(tmp_path):
    calls = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal calls
            calls += 1
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server, thread = _run_server(Handler)
    url = f"http://127.0.0.1:{server.server_port}/paper"
    script = (
        "import httpx; from bibcite.sources import _s2_get; "
        f"c=httpx.Client(); _s2_get(c, {url!r}, {{}})"
    )
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path), "S2_API_KEY": "test-key"}
    try:
        result = subprocess.run(
            ["uv", "run", sys.executable, "-c", script],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode != 0
    assert calls == 1
