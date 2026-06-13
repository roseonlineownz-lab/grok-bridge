#!/usr/bin/env python3
"""
NovaMaster Grok Bridge — CloakBrowser edition v3
- OpenAI-compatible /v1/chat/completions endpoint
- /v1/images/generations via Grok Aurora (browser)
- Auto-login via Chrome Default profile (roseonlineownz)
- Persistent CloakBrowser profile at ~/.nova/grok_cloak/
"""

import os, sys, json, signal, argparse, time, base64, shutil, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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

# Extra Chromium args: prevent crash-recovery dialog + reduce renderer memory (OOM-resilient)
EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--restore-last-session=false",
    "--hide-crash-restore-bubble",
    # Memory pressure mitigation — host RAM is tight, keep renderer from OOM-crashing
    "--js-flags=--max-old-space-size=512",
    "--renderer-process-limit=1",
    "--disable-background-timer-throttling",
    "--memory-pressure-off",
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
        self._headless = headless
        self._last_login_check = 0
        self._lock = threading.RLock()
        self._busy_since = None
        self._last_error = None
        self._last_url = "about:blank"
        self._cookie_session_ready = False
        self._launch()

    def _launch(self):
        """Launch (or relaunch) the persistent browser context and primary page."""
        _clear_crash_recovery()
        _import_chrome_cookies()
        print(f"[CloakBrowser] Launching — headless={self._headless}", file=sys.stderr)
        # Optional egress proxy (e.g. residential SOCKS exit to pass Cloudflare on a
        # datacenter host). Set GROK_PROXY=socks5://127.0.0.1:1080 to route browser traffic.
        proxy = os.environ.get("GROK_PROXY") or None
        if proxy:
            print(f"[proxy] Routing browser via {proxy}", file=sys.stderr)
        self._ctx = launch_persistent_context(
            PROFILE_DIR,
            headless=self._headless,
            humanize=True,
            args=EXTRA_ARGS,
            proxy=proxy,
        )
        self._page = self._ctx.new_page()
        self._page.set_default_timeout(int(os.environ.get("GROK_PLAYWRIGHT_TIMEOUT_MS", "12000")))
        self._page.set_default_navigation_timeout(int(os.environ.get("GROK_NAVIGATION_TIMEOUT_MS", "20000")))
        self._last_login_check = 0
        # Lightweight init: verify SSO cookies WITHOUT loading the heavy grok.com SPA.
        # Loading grok.com at startup OOM-crashes the renderer on RAM-tight hosts;
        # the actual navigation is deferred to the first chat() via _ensure_grok().
        self._verify_session_cookies()

    def _verify_session_cookies(self):
        """Cheap login check: inspect persisted cookies, no page navigation."""
        try:
            cookies = self._ctx.cookies("https://grok.com")
            sso = [c for c in cookies if c.get("name") in ("sso", "sso-rw")]
            self._cookie_session_ready = bool(sso)
            if sso:
                print("[login] SSO cookies present — session ready (deferred nav)", file=sys.stderr)
            else:
                print("[login] No SSO cookies — manual --login may be needed", file=sys.stderr)
        except Exception as e:
            self._cookie_session_ready = False
            print(f"[login] cookie check skipped: {e}", file=sys.stderr)

    def _has_sso_cookie(self) -> bool:
        try:
            cookies = self._ctx.cookies("https://grok.com")
            return any(c.get("name") in ("sso", "sso-rw") for c in cookies)
        except Exception:
            return False

    def _is_alive(self):
        """Return True if the page/context is still usable."""
        try:
            if self._page.is_closed():
                return False
            # Cheap probe — raises if renderer is dead
            self._page.evaluate("() => 1")
            return True
        except Exception:
            return False

    def _recover(self):
        """Rebuild the browser after a renderer crash. Best-effort cleanup then relaunch."""
        print("[recover] Page crashed — rebuilding browser", file=sys.stderr)
        try:
            self._ctx.close()
        except Exception:
            pass
        # Kill any stale chrome holding the profile, then relaunch
        try:
            import subprocess
            subprocess.run(
                [os.path.join(os.path.dirname(__file__), "cleanup_grok_profile.sh")],
                timeout=15,
            )
        except Exception as e:
            print(f"[recover] cleanup failed: {e}", file=sys.stderr)
        time.sleep(2)
        self._launch()
        print("[recover] Browser rebuilt", file=sys.stderr)

    def acquire(self, timeout_s: float = 2.0, mark_busy: bool = True) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._lock.acquire(blocking=False):
                if mark_busy:
                    self._busy_since = time.time()
                return True
            time.sleep(0.05)
        return False

    def release(self, mark_busy: bool = True):
        if mark_busy:
            self._busy_since = None
        self._lock.release()

    def health_snapshot(self) -> dict:
        return {
            "status": "busy" if self._busy_since else "ok",
            "version": VERSION,
            "code_rev": 4,
            "url": self._last_url,
            "logged_in": self._cookie_session_ready,
            "busy_since": self._busy_since,
            "last_error": self._last_error,
        }

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
        print(f"[ensure] current_url={self._page.url}", file=sys.stderr, flush=True)
        if not self._page.url.startswith(GROK_URL):
            print("[ensure] navigating to grok.com", file=sys.stderr, flush=True)
            self._page.goto(GROK_URL, wait_until="domcontentloaded", timeout=15000)
            print(f"[ensure] navigated_url={self._page.url}", file=sys.stderr, flush=True)
            self._page.wait_for_timeout(2000)
        self._last_url = self._page.url
        # Re-check login every 10 minutes
        now = time.time()
        if now - self._last_login_check > 600:
            self._last_login_check = now
            print("[ensure] checking login", file=sys.stderr, flush=True)
            if not self.is_logged_in():
                print("[login] Session expired — re-attempting login", file=sys.stderr)
                self._auto_login_if_needed()
            print(f"[ensure] login_checked url={self._page.url}", file=sys.stderr, flush=True)

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
        """Crash-resilient wrapper: detect renderer crash, rebuild browser, retry once."""
        for attempt in range(2):
            try:
                if not self._is_alive():
                    self._recover()
                return self._chat_impl(prompt, mode)
            except Exception as e:
                msg = str(e)
                crashed = "Target crashed" in msg or "Target closed" in msg or "has been closed" in msg
                if crashed and attempt == 0:
                    print(f"[chat] crash detected ({msg[:80]}) — recovering", file=sys.stderr)
                    self._recover()
                    continue
                return {"status": "error", "error": msg[:300]}
        return {"status": "error", "error": "chat failed after recovery"}

    def _chat_impl(self, prompt: str, mode: str = "auto") -> dict:
        self._ensure_grok()

        # Inject fetch interceptor to capture streaming response
        self._page.evaluate("""
            () => {
                window.__grok_response = '';
                window.__grok_done = false;
                window.__grok_urls = [];  // debug
                window.__grok_raw = [];   // debug: raw stream samples
                if (window.__grok_hooked) { return; }
                window.__grok_hooked = true;
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const resp = await origFetch(...args);
                    const url = (args[0] || '').toString();
                    const hostname = (() => { try { return new URL(url, location.href).hostname; } catch(e) { return ''; } })();
                    const isGrokAPI = hostname === 'grok.com' || hostname.endsWith('.grok.com')
                        || hostname === 'x.ai' || hostname.endsWith('.x.ai')
                        || (url.startsWith('/') && (url.includes('chat') || url.startsWith('/rest/') || url.startsWith('/api/')));
                    if (isGrokAPI) {
                        window.__grok_urls.push(url);  // debug log
                        // Skip suggestions stream — that's UI chips, not the real response
                        const isSuggestion = url.includes('/suggestions/') || url.includes('/typeahead');
                        // Target: only the response-node streaming endpoint
                        const isResponseStream = url.includes('response-node');
                        const contentType = resp.headers.get('content-type') || '';
                        const isStream = contentType.includes('text/event-stream')
                            || contentType.includes('application/x-ndjson')
                            || contentType.includes('text/plain');
                        if (!isSuggestion && (isResponseStream || isStream || url.includes('chat'))) {
                            try {
                                const clone = resp.clone();
                                const reader = clone.body.getReader();
                                const decoder = new TextDecoder();
                                let buf = '';
                                let tokenCount = 0;
                                (async () => {
                                    try {
                                        while(tokenCount < 5000) {
                                            const {done, value} = await reader.read();
                                            if (done) {
                                                // Flush remaining buffer
                                                const lines = buf.split('\\n');
                                                for (const line of lines) {
                                                    const trimmed = line.trim();
                                                    if (!trimmed || !trimmed.startsWith('{')) continue;
                                                    try {
                                                        const d = JSON.parse(trimmed);
                                                        // Grok response-node token formats
                                                        const t = d?.result?.response?.token
                                                               || d?.result?.token
                                                               || d?.token
                                                               || d?.choices?.[0]?.delta?.content
                                                               || d?.data
                                                               || '';
                                                        if (t && typeof t === 'string' && t !== window.__grok_last && t.length > 0
                                                              && t.indexOf('Thinking') === -1) {
                                                            window.__grok_response += t;
                                                            window.__grok_last = t;
                                                            tokenCount++;
                                                        }
                                                    } catch(e) {}
                                                }
                                                window.__grok_done = true;
                                                break;
                                            }
                                            buf += decoder.decode(value, {stream: true});
                                            // Save raw chunk sample for debugging JSON format (only first 5 samples)
                                            if (window.__grok_raw.length < 5 && buf.length > 50) {
                                                window.__grok_raw.push({url: url, sample: buf.slice(0, 3000)});
                                            }
                                            const lines = buf.split('\\n');
                                            buf = lines.pop() || '';
                                            for (const line of lines) {
                                                const trimmed = line.trim();
                                                if (!trimmed || !trimmed.startsWith('{')) continue;                                                    try {
                                                        const d = JSON.parse(trimmed);
                                                        const t = d?.result?.response?.token
                                                               || d?.result?.token
                                                               || d?.token
                                                               || d?.choices?.[0]?.delta?.content
                                                               || d?.data
                                                               || '';
                                                        if (t && typeof t === 'string' && t !== window.__grok_last && t.length > 0
                                                              && t.indexOf('Thinking') === -1) {
                                                            window.__grok_response += t;
                                                            window.__grok_last = t;
                                                            tokenCount++;
                                                        }
                                                    } catch(e) {}
                                            }
                                        }
                                        window.__grok_done = true;
                                    } catch(e) { window.__grok_done = true; }
                                })();
                            } catch(e) {}
                        }
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

        # Dismiss any overlay/banner that may cover the input (e.g. "Grok Build" promo)
        try:
            for close_sel in ['button[aria-label="Close"]', 'button[aria-label="Dismiss"]',
                              '[data-testid="dismiss"]', 'button:has-text("×")']:
                ov = self._page.query_selector(close_sel)
                if ov:
                    ov.click()
                    self._page.wait_for_timeout(300)
        except Exception:
            pass

        # Robust focus+type: force-click past overlays, fall back to JS focus
        try:
            box.click(force=True, timeout=5000)
        except Exception:
            try:
                box.scroll_into_view_if_needed(timeout=3000)
                self._page.evaluate("(el) => el.focus()", box)
            except Exception:
                pass
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

        # Wait briefly for SSE capture, but don't block if it's not working
        try:
            self._page.wait_for_function(
                "() => window.__grok_done === true || (window.__grok_response || '').length > 50",
                timeout=5000,
            )
        except:
            pass
        self._page.wait_for_timeout(500)

        text = self._page.evaluate("() => window.__grok_response || ''")
        if not text or len(text) < 5:
            text = self._page.evaluate("""
                (userPrompt) => {
                    // Try to find the last assistant message bubble first
                    const msgBlocks = document.querySelectorAll('[data-testid="message"], [data-message-role="assistant"], .message-assistant, [class*="assistant"]');
                    if (msgBlocks.length > 0) {
                        const lastMsg = msgBlocks[msgBlocks.length - 1].textContent.trim();
                        if (lastMsg.length > 5) return lastMsg.slice(0, 4000);
                    }
                    // Fallback: TreeWalker but filter out Grok UI suggestions
                    const main = document.querySelector('main, [role="main"], .conversation, #chat-content');
                    const root = main || document.body;
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    const suggestionPatterns = [
                        'Explore ', 'Learn about ', 'Discover ', 'Try asking',
                        'Explain how', 'Find out', 'See also', 'Suggested',
                        'Compare ', 'Summarize', 'Ask a follow', 'DeepSearch',
                        'Think Harder', 'Attach', 'Ask anything', 'Search the web'
                    ];
                    const texts = [];
                    let node;
                    while(node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length < 20) continue;
                        if (suggestionPatterns.some(p => t.startsWith(p))) continue;
                        texts.push(t);
                    }
                    const idx = texts.findIndex(t => t.includes(userPrompt.slice(0, 20)));
                    if (idx >= 0 && idx < texts.length - 1) {
                        // Only take the first few texts after the prompt (the actual response)
                        const after = texts.slice(idx + 1);
                        // Stop at the first suggestion-like text
                        const cutoff = after.findIndex(t => suggestionPatterns.some(p => t.startsWith(p)));
                        const response = cutoff > 0 ? after.slice(0, cutoff) : after.slice(0, 5);
                        return response.join(' ').slice(0, 4000);
                    }
                    return texts.slice(-5).join(' ').slice(0, 4000);
                }
            """, prompt)

        return {"status": "ok", "response": text or "geen response", "url": self._page.url}

    def generate_image(self, prompt: str, size: str = "1024x1024") -> list[dict]:
        """Generate image via Grok Aurora (browser UI)."""
        deadline = time.monotonic() + int(os.environ.get("GROK_IMAGE_TIMEOUT_S", "45"))
        try:
            self._ensure_grok()
        except Exception as e:
            self._last_error = f"ensure_grok: {e}"
            return [{"url": "", "error": self._last_error}]

        # Navigate to image mode if a stable button is exposed. Do not click a
        # broad text locator here: in headless Grok it can resolve to hidden
        # navigation text and hang until Playwright's long default timeout.
        aurora_btn = None
        for sel in ('[aria-label*="Aurora"]', '[data-testid*="aurora"]'):
            try:
                aurora_btn = self._page.query_selector(sel)
                if aurora_btn:
                    break
            except Exception:
                pass
        if aurora_btn:
            try:
                aurora_btn.click(timeout=2000)
                self._page.wait_for_timeout(1500)
            except Exception:
                pass

        # Inject interceptor for image URLs
        try:
            self._page.evaluate("""
            () => {
                if (window.__grok_image_interceptor_installed) return;
                window.__grok_image_interceptor_installed = true;
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
        except Exception as e:
            self._last_error = f"image_interceptor: {e}"
            return [{"url": "", "error": self._last_error}]

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
            self._last_error = "textbox not found"
            return [{"url": "", "error": self._last_error}]

        try:
            box.click(timeout=3000)
            box.type(img_prompt, delay=20)
            self._page.keyboard.press("Enter")
        except Exception as e:
            self._last_error = f"submit_image_prompt: {e}"
            return [{"url": "", "error": self._last_error}]

        # Wait for image to appear
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(1500)
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

        self._last_error = "image generation timed out before capture"
        return [{"url": "", "error": self._last_error}]

    def close(self):
        try:
            self._ctx.close()
        except:
            pass


# ── HTTP Server ────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    bridge: GrokCloak = None
    bridge_status = {"state": "starting", "error": None, "started_at": None}

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
            if not self.bridge:
                return self._json(200, {
                    "status": self.bridge_status.get("state", "starting"),
                    "version": VERSION,
                    "code_rev": 5,
                    "url": "about:blank",
                    "logged_in": False,
                    "busy_since": None,
                    "started_at": self.bridge_status.get("started_at"),
                    "last_error": self.bridge_status.get("error"),
                })
            self._json(200, self.bridge.health_snapshot())
        elif self.path == "/health-debug":
            if self.bridge:
                if not self.bridge.acquire(timeout_s=0.25):
                    return self._json(423, {"error": "browser busy", "busy_since": self.bridge._busy_since})
                try:
                    raw = self.bridge._page.evaluate("() => (window.__grok_raw || []).slice(0, 3)")
                    urls = self.bridge._page.evaluate("() => (window.__grok_urls || []).slice(-10)")
                    self._json(200, {"raw_samples": raw, "recent_urls": urls})
                except Exception as e:
                    self._json(500, {"error": str(e)})
                finally:
                    self.bridge.release()
            else:
                self._json(503, {"error": "bridge not ready"})
        elif self.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "grok-browser", "object": "model"},
                {"id": "grok-4.3", "object": "model"},
                {"id": "supergrok", "object": "model"},
                {"id": "grok-build", "object": "model"},
                {"id": "grok-code-fast-1", "object": "model"},
                {"id": "grok-composer-2.5-fast", "object": "model"},
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path in ("/chat", "/v1/chat/completions", "/chat/completions", "/v1/images/generations", "/images/generations") and not self.bridge:
            return self._json(503, {
                "error": "bridge not ready",
                "status": self.bridge_status.get("state", "starting"),
                "detail": self.bridge_status.get("error"),
            })

        # Legacy /chat endpoint
        if self.path == "/chat":
            prompt = body.get("prompt")
            if not prompt:
                return self._json(400, {"error": "missing 'prompt' field"})
            if not self.bridge.acquire(timeout_s=2):
                return self._json(423, {"error": "browser busy", "busy_since": self.bridge._busy_since})
            try:
                result = self.bridge.chat(prompt, body.get("mode", "auto"))
                return self._json(200, result)
            finally:
                self.bridge.release()

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
            if not self.bridge.acquire(timeout_s=2):
                return self._json(423, {"error": "browser busy", "busy_since": self.bridge._busy_since})
            try:
                result = self.bridge.chat(prompt)
                if result.get("status") == "error":
                    return self._json(502, result)
            finally:
                self.bridge.release()
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
            if not self.bridge.acquire(timeout_s=5):
                return self._json(423, {"error": "browser busy", "busy_since": self.bridge._busy_since})
            try:
                images = self.bridge.generate_image(prompt, size=size)
            finally:
                self.bridge.release()
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
    _Handler.bridge = None
    _Handler.bridge_status = {"state": "starting", "error": None, "started_at": time.time()}

    def _launch_bridge():
        try:
            _Handler.bridge = GrokCloak(headless=headless)
            _Handler.bridge_status = {"state": "ready", "error": None, "started_at": _Handler.bridge_status["started_at"]}
        except Exception as e:
            _Handler.bridge = None
            _Handler.bridge_status = {"state": "error", "error": str(e), "started_at": _Handler.bridge_status["started_at"]}
            print(f"[FATAL] bridge launch failed: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_launch_bridge, daemon=True, name="grok-bridge-launch").start()

    def _shutdown(sig, _):
        print("\n[SIGNAL] Shutting down...", file=sys.stderr)
        if _Handler.bridge:
            _Handler.bridge.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ThreadingHTTPServer.allow_reuse_address = True  # survive fast restarts (TIME_WAIT)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), _Handler)
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
