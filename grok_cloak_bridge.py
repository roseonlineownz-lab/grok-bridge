#!/usr/bin/env python3
"""
NovaMaster Grok Bridge — CloakBrowser edition v3
- OpenAI-compatible /v1/chat/completions endpoint
- /v1/images/generations via Grok Aurora (browser)
- Auto-login via Chrome Default profile (roseonlineownz)
- Persistent CloakBrowser profile at ~/.nova/grok_cloak/
"""

import os, sys, json, signal, argparse, time, base64, shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    from cloakbrowser import launch_persistent_context
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:
    print("[FATAL] cloakbrowser or playwright not found", file=sys.stderr)
    sys.exit(1)

VERSION = "v4-cloak"
GROK_URL = "https://grok.com"
PROFILE_DIR = os.path.expanduser("~/.nova/grok_cloak")
# Chrome Default profile from WSL path (roseonlineownz Google login)
CHROME_PROFILE_SRC = "/mnt/c/Users/roseo/AppData/Local/Google/Chrome/User Data/Default"
os.makedirs(PROFILE_DIR, exist_ok=True)

# Extra Chromium args to prevent crash-recovery dialog (breaks headless login)
EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--restore-last-session=false",
    "--hide-crash-restore-bubble",
]


def _import_chrome_cookies():
    """Copy Chrome cookies on first run only. Windows cookies are DPAPI-encrypted and can't be
    decrypted on Linux — only copy if the profile has no cookies yet (bootstrap only)."""
    src = Path(CHROME_PROFILE_SRC)
    dst = Path(PROFILE_DIR)
    if not src.exists():
        print("[login] Chrome profile not found at expected path, skipping import", file=sys.stderr)
        return False

    cookies_src = src / "Cookies"
    cookies_dst = dst / "Default" / "Cookies"
    cookies_dst.parent.mkdir(parents=True, exist_ok=True)

    # Only copy if destination missing — Linux Chromium session cookies must NOT be overwritten
    if cookies_src.exists() and not cookies_dst.exists():
        try:
            shutil.copy2(str(cookies_src), str(cookies_dst))
            print(f"[login] Bootstrap: copied Chrome cookies → {cookies_dst}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[login] Cookie copy failed: {e}", file=sys.stderr)
    return False


def _clear_crash_recovery():
    """Remove Chromium crash/session files that trigger restore dialog in headless mode."""
    import glob
    for pattern in [
        f"{PROFILE_DIR}/Default/Last Session",
        f"{PROFILE_DIR}/Default/Last Tabs",
        f"{PROFILE_DIR}/Default/Current Session",
        f"{PROFILE_DIR}/Default/Current Tabs",
    ]:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except Exception:
                pass


class GrokCloak:
    def __init__(self, headless=True):
        _clear_crash_recovery()
        _import_chrome_cookies()
        print(f"[CloakBrowser] Launching — headless={headless}", file=sys.stderr)
        self._ctx = launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            humanize=True,
            args=EXTRA_ARGS,
        )
        self._page = self._ctx.new_page()
        self._last_login_check = 0
        self._auto_login_if_needed()

    def _auto_login_if_needed(self):
        """Navigate to grok.com and check login state. React needs time to hydrate — wait properly."""
        try:
            self._page.goto(GROK_URL, wait_until="domcontentloaded", timeout=20000)
            # Wait for React hydration — poll up to 10s in 1s steps
            for _ in range(10):
                self._page.wait_for_timeout(1000)
                if self.is_logged_in():
                    break

            if self.is_logged_in():
                print("[login] Already logged in", file=sys.stderr)
                return

            # Check if we have SSO cookies — if so, treat as logged in (cookie auth)
            try:
                cookies = self._ctx.cookies()
                sso_cookies = [c for c in cookies if c.get("name") in ("sso", "sso-rw") and "x.ai" in c.get("domain", "")]
                if sso_cookies:
                    print("[login] SSO cookies present — treating as logged in (cookie auth)", file=sys.stderr)
                    return
            except Exception:
                pass

            # Try clicking Google sign-in button (only works in headed mode)
            print("[login] No SSO cookies — attempting Google sign-in...", file=sys.stderr)
            for sel in [
                'text=Sign in with Google',
                'text=Continue with Google',
                '[data-provider="google"]',
                'button:has-text("Google")',
            ]:
                btn = self._page.query_selector(sel)
                if btn:
                    btn.click()
                    self._page.wait_for_timeout(3000)
                    for acct_sel in [
                        'text=roseonlineownz',
                        '[data-email*="roseonlineownz"]',
                        'div[data-identifier*="roseonlineownz"]',
                    ]:
                        acct = self._page.query_selector(acct_sel)
                        if acct:
                            acct.click()
                            self._page.wait_for_timeout(4000)
                            break
                    break

            for _ in range(6):
                self._page.wait_for_timeout(2000)
                if self._page.url.startswith(GROK_URL) and self.is_logged_in():
                    print("[login] Sign-in succeeded", file=sys.stderr)
                    return

            # Proceed anyway — headless Google OAuth won't work, but cookies might
            print("[login] Sign-in incomplete — proceeding with existing cookies", file=sys.stderr)
        except Exception as e:
            print(f"[login] Login check error: {e}", file=sys.stderr)

    def _ensure_grok(self):
        if not self._page.url.startswith(GROK_URL):
            self._page.goto(GROK_URL, wait_until="domcontentloaded", timeout=15000)
            self._page.wait_for_timeout(2000)
        # Re-check login every 10 minutes
        now = time.time()
        if now - self._last_login_check > 600:
            self._last_login_check = now
            if not self.is_logged_in():
                print("[login] Session expired — re-attempting login", file=sys.stderr)
                self._auto_login_if_needed()

    def is_logged_in(self):
        try:
            # Multiple fallback checks — Grok UI changes selectors frequently
            url = self._page.url or ""
            if "accounts.x.ai" in url or "login" in url.lower():
                return False
            checks = [
                '[data-testid="userAvatar"]',
                '[aria-label="User menu"]',
                'a[href*="/i/user"]',
                'nav a[href="/"]',  # sidebar nav present = logged in
                'button:has-text("New Chat")',
                'a:has-text("New Chat")',
                'text=New Chat',
                'text=SuperGrok',
                '[data-testid="sidebar"]',
            ]
            for sel in checks:
                try:
                    el = self._page.query_selector(sel)
                    if el:
                        return True
                except Exception:
                    continue
            # Fallback: check page text for logged-in indicators
            try:
                content = self._page.evaluate("() => document.body.innerText.slice(0, 2000)")
                if any(kw in content for kw in ["New Chat", "SuperGrok", "Imagine", "Build"]):
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def chat(self, prompt: str, mode: str = "auto") -> dict:
        self._ensure_grok()

        # Inject fetch interceptor to capture streaming response
        self._page.evaluate("""
            () => {
                window.__grok_response = '';
                window.__grok_done = false;
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const resp = await origFetch(...args);
                    const url = (args[0] || '').toString();
                    if (url.includes('/rest/app-chat') || url.includes('/rest/chat') || url.includes('/api/')) {
                        try {
                            const clone = resp.clone();
                            const reader = clone.body.getReader();
                            const decoder = new TextDecoder();
                            (async () => {
                                while(true) {
                                    const {done, value} = await reader.read();
                                    if (done) { window.__grok_done = true; break; }
                                    const chunk = decoder.decode(value);
                                    chunk.split('\\n').forEach(line => {
                                        line = line.trim();
                                        if (!line) return;
                                        try {
                                            const d = JSON.parse(line);
                                            const t = d?.result?.response?.token
                                                   || d?.result?.token
                                                   || d?.token
                                                   || d?.choices?.[0]?.delta?.content
                                                   || '';
                                            if (t) window.__grok_response += t;
                                        } catch(e) {}
                                    });
                                }
                            })();
                        } catch(e) {}
                    }
                    return resp;
                };
            }
        """)

        box = None
        for sel in ['div[contenteditable="true"]', '[role="textbox"]', 'textarea']:
            try:
                box = self._page.wait_for_selector(sel, timeout=8000)
                if box:
                    break
            except:
                continue
        if not box:
            return {"status": "error", "error": "Input not found — not logged in?"}

        box.click()
        box.type(prompt, delay=25)
        self._page.keyboard.press("Enter")

        self._page.wait_for_timeout(3000)
        try:
            self._page.wait_for_function(
                "() => !document.querySelector('svg.animate-spin, [class*=\"animate-spin\"]')",
                timeout=90000,
            )
        except:
            pass
        self._page.wait_for_timeout(2000)

        text = self._page.evaluate("() => window.__grok_response || ''")
        if not text or len(text) < 5:
            text = self._page.evaluate("""
                (userPrompt) => {
                    const main = document.querySelector('main, [role="main"], .conversation, #chat-content');
                    const root = main || document.body;
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    const texts = [];
                    let node;
                    while(node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length > 20) texts.push(t);
                    }
                    const idx = texts.findIndex(t => t.includes(userPrompt.slice(0, 20)));
                    if (idx >= 0 && idx < texts.length - 1) {
                        return texts.slice(idx + 1).join(' ').slice(0, 4000);
                    }
                    return texts.slice(-30).join(' ').slice(0, 4000);
                }
            """, prompt)

        return {"status": "ok", "response": text or "geen response", "url": self._page.url}

    def generate_image(self, prompt: str, size: str = "1024x1024") -> list[dict]:
        """Generate image via Grok Aurora (browser UI)."""
        self._ensure_grok()

        # Navigate to new chat or image mode
        aurora_btn = self._page.query_selector('[aria-label*="Aurora"], [data-testid*="aurora"], text=Aurora')
        if aurora_btn:
            aurora_btn.click()
            self._page.wait_for_timeout(1500)

        # Inject interceptor for image URLs
        self._page.evaluate("""
            () => {
                window.__grok_images = [];
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const resp = await origFetch(...args);
                    const url = (args[0] || '').toString();
                    if (url.includes('/image') || url.includes('aurora') || url.includes('generate')) {
                        try {
                            const clone = resp.clone();
                            const data = await clone.json();
                            // Capture any image URLs in the response
                            const imgs = JSON.stringify(data).match(/https?:[^"]+\\.(?:png|jpg|webp|jpeg)[^"]*/gi) || [];
                            window.__grok_images.push(...imgs);
                        } catch(e) {}
                    }
                    return resp;
                };
            }
        """)

        img_prompt = f"Generate an image: {prompt}"
        box = None
        for sel in ['div[contenteditable="true"]', '[role="textbox"]', 'textarea']:
            try:
                box = self._page.wait_for_selector(sel, timeout=8000)
                if box:
                    break
            except:
                continue
        if not box:
            return []

        box.click()
        box.type(img_prompt, delay=20)
        self._page.keyboard.press("Enter")

        # Wait for image to appear
        for _ in range(20):
            self._page.wait_for_timeout(3000)
            # Check for generated image in DOM
            img_el = self._page.query_selector('img[src*="grok"], img[src*="aurora"], img[alt*="Generated"]')
            if img_el:
                src = img_el.get_attribute("src") or ""
                if src:
                    return [{"url": src}]
            # Check intercepted URLs
            imgs = self._page.evaluate("() => window.__grok_images || []")
            if imgs:
                return [{"url": u} for u in imgs[:4]]

        # Fallback: screenshot the generated area
        try:
            area = self._page.query_selector('main img, .generated-image, [data-testid*="image"]')
            if area:
                png = area.screenshot()
                b64 = base64.b64encode(png).decode()
                return [{"b64_json": b64}]
        except:
            pass

        return [{"url": "", "error": "image not captured — Aurora may need manual trigger"}]

    def close(self):
        try:
            self._ctx.close()
        except:
            pass


# ── HTTP Server ────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    bridge: GrokCloak = None

    def log_message(self, fmt, *args):
        print(f'[{self.address_string()}] ' + fmt % args, file=sys.stderr)

    def _json(self, code: int, data: dict):
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
            self._json(200, {
                "status": "ok",
                "version": VERSION,
                "url": self.bridge._page.url if self.bridge else "?",
                "logged_in": self.bridge.is_logged_in() if self.bridge else False,
            })
        elif self.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "grok-4.3", "object": "model"},
                {"id": "grok-browser", "object": "model"},
                {"id": "supergrok", "object": "model"},
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        # Legacy /chat endpoint
        if self.path == "/chat":
            prompt = body.get("prompt")
            if not prompt:
                return self._json(400, {"error": "missing 'prompt' field"})
            result = self.bridge.chat(prompt, body.get("mode", "auto"))
            return self._json(200, result)

        # OpenAI-compatible chat
        if self.path in ("/v1/chat/completions", "/chat/completions"):
            messages = body.get("messages", [])
            if not messages:
                return self._json(400, {"error": "missing messages"})
            # Flatten messages to a single prompt
            prompt_parts = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    prompt_parts.append(f"[System]: {content}")
                elif role == "user":
                    prompt_parts.append(f"[User]: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"[Assistant]: {content}")
            prompt = "\n".join(prompt_parts)
            result = self.bridge.chat(prompt)
            if result.get("status") == "error":
                return self._json(502, result)
            text = result.get("response", "")
            return self._json(200, {
                "id": f"chatcmpl-grok-{int(time.time())}",
                "object": "chat.completion",
                "model": body.get("model", "grok-4.3"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        # OpenAI-compatible image generation
        if self.path in ("/v1/images/generations", "/images/generations"):
            prompt = body.get("prompt", "")
            if not prompt:
                return self._json(400, {"error": "missing prompt"})
            size = body.get("size", "1024x1024")
            response_format = body.get("response_format", "url")
            images = self.bridge.generate_image(prompt, size=size)
            if not images:
                return self._json(502, {"error": "image generation failed"})
            return self._json(200, {
                "created": int(time.time()),
                "data": images,
            })

        self._json(404, {"error": "not found"})


# ── Entry points ───────────────────────────────────────────────────────────────

def login_mode():
    """Open headed browser for manual Google login."""
    print(f"Opening browser for manual login (profile: {PROFILE_DIR})")
    print("Log in with Google account (roseonlineownz@gmail.com), then close the window.")
    bridge = GrokCloak(headless=False)
    try:
        bridge._page.wait_for_event("close", timeout=0)
    except:
        pass
    bridge.close()
    print("Session saved to profile.")


def server_mode(host: str, port: int, headless: bool):
    bridge = GrokCloak(headless=headless)
    _Handler.bridge = bridge

    def _shutdown(sig, _):
        print("\n[SIGNAL] Shutting down...", file=sys.stderr)
        bridge.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    srv = HTTPServer((host, port), _Handler)
    print(f"grok-cloak-bridge {VERSION} listening on {host}:{port}", file=sys.stderr)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Grok CloakBrowser Bridge v3")
    ap.add_argument("--login", action="store_true", help="Open browser for manual Google login")
    ap.add_argument("--port", type=int, default=19997)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--headed", action="store_true", help="Run with visible browser")
    args = ap.parse_args()

    if args.login:
        login_mode()
    else:
        server_mode(args.host, args.port, not args.headed)


if __name__ == "__main__":
    main()
