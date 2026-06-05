#!/usr/bin/env python3
"""
Puter Bridge — OpenAI-compatible endpoint backed by Puter's free AI (User-Pays).
Gives free access to 400+ models (GPT, Claude, Grok-3, Gemini, ...) over plain HTTP.

Auth: PUTER_AUTH_TOKEN from ~/.config/novamaster/puter.env (obtained via puter_google_login.py).
Endpoints: /health, /v1/models, /v1/chat/completions (and /chat/completions).
"""
import os, sys, json, time, signal, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

VERSION = "v1-puter"
ENV_PATH = os.path.expanduser("~/.config/novamaster/puter.env")
PUTER_API = "https://api.puter.com/drivers/call"
DEFAULT_MODEL = os.environ.get("PUTER_DEFAULT_MODEL", "gpt-4o-mini")


def _load_token():
    tok = os.environ.get("PUTER_AUTH_TOKEN")
    if tok:
        return tok
    try:
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith("PUTER_AUTH_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


TOKEN = _load_token()


def puter_complete(messages, model, temperature=None, max_tokens=None):
    """Call Puter's chat-completion driver. Returns (content, error)."""
    args = {"messages": messages, "model": model or DEFAULT_MODEL}
    if temperature is not None:
        args["temperature"] = temperature
    if max_tokens is not None:
        args["max_tokens"] = max_tokens
    payload = json.dumps({
        "interface": "puter-chat-completion",
        "method": "complete",
        "args": args,
    }).encode()
    req = urllib.request.Request(PUTER_API, data=payload, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Origin": "https://puter.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, str(e)[:300]
    if not data.get("success"):
        return None, json.dumps(data.get("error", data))[:300]
    msg = (data.get("result") or {}).get("message") or {}
    content = msg.get("content", "")
    # Anthropic-style models return content as a list of typed blocks — flatten to text.
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content, None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print(f"[{self.address_string()}] " + fmt % a, file=sys.stderr)

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "version": VERSION,
                             "token": bool(TOKEN), "default_model": DEFAULT_MODEL})
        elif self.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model"} for m in
                ("gpt-4o-mini", "gpt-4o", "claude-haiku-4-5-20251001",
                 "claude-3-7-sonnet-20250219", "grok-3", "grok-3-fast", "gemini-2.5-flash")
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "invalid json"})

        if self.path in ("/v1/chat/completions", "/chat/completions"):
            messages = body.get("messages", [])
            if not messages:
                return self._json(400, {"error": "missing messages"})
            content, err = puter_complete(
                messages, body.get("model"),
                body.get("temperature"), body.get("max_tokens"))
            if err:
                return self._json(502, {"error": err})
            return self._json(200, {
                "id": f"chatcmpl-puter-{int(time.time())}",
                "object": "chat.completion",
                "model": body.get("model", DEFAULT_MODEL),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
        self._json(404, {"error": "not found"})


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19998)
    args = ap.parse_args()

    if not TOKEN:
        print(f"[FATAL] no PUTER_AUTH_TOKEN — run puter_google_login.py first", file=sys.stderr)
        sys.exit(1)

    def _shutdown(*_):
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    HTTPServer.allow_reuse_address = True
    srv = HTTPServer((args.host, args.port), _Handler)
    print(f"puter-bridge {VERSION} listening on {args.host}:{args.port}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
