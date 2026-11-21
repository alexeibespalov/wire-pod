"""Tiny OpenAI-compatible pass-through proxy for wire-pod's "custom" knowledge provider.

wire-pod -> http://127.0.0.1:8091/v1/... -> UPSTREAM_BASE/...

What it adds:
  * injects the real API key (wire-pod only holds a dummy key)
  * forces `enable_thinking: false` (Qwen3 thinks by default: ~5x slower answers,
    and wire-pod has no setting for it)
  * streams SSE responses straight through, chunk by chunk

Later: this is the place to add tool calls for personal data (calendar, HA, notes).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ["UPSTREAM_BASE"].rstrip("/")
UPSTREAM_KEY = os.environ["UPSTREAM_KEY"]
FORCE_MODEL = os.environ.get("FORCE_MODEL", "").strip()
DEBUG = os.environ.get("PROXY_DEBUG", "") == "1"
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8091"))
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
              "proxy-authorization", "proxy-authenticate", "content-length", "content-encoding"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        log("%s %s" % (self.address_string(), fmt % args))

    def _upstream_url(self):
        path = self.path
        if path.startswith("/v1"):
            path = path[3:]
        return UPSTREAM_BASE + path

    def _forward(self, method):
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        if body and "json" in (self.headers.get("Content-Type") or ""):
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and "messages" in payload:
                    payload["enable_thinking"] = False
                    if FORCE_MODEL:
                        payload["model"] = FORCE_MODEL
                    log(f"-> {payload.get('model')} stream={payload.get('stream')} "
                        f"msgs={len(payload['messages'])} last={str(payload['messages'][-1].get('content'))[:80]!r}")
                    if DEBUG:
                        dbg = {k: v for k, v in payload.items() if k != "messages"}
                        log("[debug] params:", json.dumps(dbg))
                        for m in payload["messages"]:
                            log(f"[debug] {m.get('role')}: {str(m.get('content'))}")
                body = json.dumps(payload).encode()
            except ValueError:
                pass

        req = urllib.request.Request(self._upstream_url(), data=body, method=method)
        req.add_header("Authorization", f"Bearer {UPSTREAM_KEY}")
        req.add_header("Content-Type", self.headers.get("Content-Type") or "application/json")
        req.add_header("Accept", self.headers.get("Accept") or "*/*")
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:  # network error
            msg = json.dumps({"error": {"message": f"proxy upstream error: {e}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            log("upstream error:", e)
            return

        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        captured = []
        try:
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                if DEBUG:
                    captured.append(chunk)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")
        finally:
            resp.close()
            if DEBUG and captured:
                text, finish = [], None
                for line in b"".join(captured).decode(errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("data:") and "[DONE]" not in line:
                        try:
                            d = json.loads(line[5:])
                        except ValueError:
                            continue
                        for c in d.get("choices", []):
                            text.append((c.get("delta") or {}).get("content") or "")
                            finish = c.get("finish_reason") or finish
                log(f"[debug] response (finish={finish}): {''.join(text)!r}")
        if resp.status >= 400:
            log(f"<- HTTP {resp.status}")

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            msg = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self._forward("GET")


if __name__ == "__main__":
    log(f"kg-proxy listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_BASE}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
