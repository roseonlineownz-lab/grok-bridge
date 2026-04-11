#!/usr/bin/env python3
"""grok-bridge: Bridge Grok (grok.com) to a local REST API via Playwright + Chrome."""

import argparse
import http.server
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout, Error as PwError

VERSION = "v1"
GROK_URL = "https://grok.com"
USER_DATA_DIR = str(Path.home() / ".grok-bridge" / "chrome-data")

# Selectors in priority order
INPUT_SELECTORS = [
    "textarea",
    'div[contenteditable="true"]',
    '[data-testid="text-input"]',
    '[role="textbox"]',
]
SEND_SELECTORS = [
    'button[aria-label="Send"]',
    'button[data-testid="send-button"]',
]
VALID_MODES = {"auto", "fast", "expert", "heavy"}

# UI artifacts to strip from responses
TRAILING_MARKERS = [
    "\nAsk anything",
    "\nDeepSearch",
    "\nThink Harder",
    "\nThink\n",
    "\nAttach",
    "\nGrok",
    "\nFast\n",
    "\nFast",
    "\nAuto\n",
    "\nAuto",
    "\nExpert\n",
    "\nExpert",
    "\nHeavy\n",
    "\nHeavy",
    "\nUpgrade to",
]


def _clean_response(text: str) -> str:
    """Remove Grok UI artifacts from extracted text."""
    for marker in TRAILING_MARKERS:
        idx = text.rfind(marker)
        if idx != -1:
            text = text[:idx]
    # Timing indicators like \n3.2s\n or \n717ms\n
    text = re.sub(r"\n[0-9]+(\.[0-9]+)?s\n", "\n", text)
    text = re.sub(r"\n[0-9]+ms\n", "\n", text)
    text = re.sub(r"\n[0-9]+(\.[0-9]+)?s$", "", text)
    text = re.sub(r"\n[0-9]+ms$", "", text)
    # "Thought for Xs" prefix from Expert/Heavy mode
    text = re.sub(r"^\s*Thought for [0-9]+s\s*\n*", "", text)
    # Action buttons
    text = re.sub(r"\n(Share|Compare|Make it|Explain|Toggle|Like|Dislike).*", "", text)
    # Collapse excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class GrokBridge:
    """Manages a persistent Chrome browser session against grok.com."""

    def __init__(self, headless: bool = False):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                channel="chrome",
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            # Remove navigator.webdriver flag that Cloudflare detects
            for page in self._context.pages:
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception as exc:
            self._pw.stop()
            print(
                f"ERROR: Could not launch Chrome. Is google-chrome installed?\n{exc}",
                file=sys.stderr,
            )
            raise
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_grok(self) -> None:
        """Navigate to grok.com if not already there."""
        url = self._page.url
        if not url.startswith(GROK_URL):
            self._page.goto(GROK_URL, wait_until="domcontentloaded")
            self._page.wait_for_timeout(2000)

    def _select_mode(self, mode: str) -> None:
        """Select a Grok mode (auto/fast/expert/heavy) via the Model select dropdown."""
        # Open the model select dropdown with pointer events (click alone doesn't work)
        self._page.evaluate("""() => {
            const btn = document.querySelector('button[aria-label="Model select"]');
            if (!btn) return;
            btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
            btn.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
            btn.click();
        }""")
        self._page.wait_for_timeout(500)
        # Click the matching menu item
        mode_lower = mode.lower()
        clicked = self._page.evaluate("""(mode) => {
            const items = Array.from(document.querySelectorAll('[role=menu] [role=menuitem]'));
            const target = items.find(i => i.textContent.trim().toLowerCase().startsWith(mode));
            if (target) { target.click(); return true; }
            return false;
        }""", mode_lower)
        if not clicked:
            raise RuntimeError(f"Could not find mode '{mode}' in dropdown")
        self._page.wait_for_timeout(300)

    def _find_input(self):
        """Locate the message input element. Returns (element, selector)."""
        # First pass: 2s per selector
        for sel in INPUT_SELECTORS:
            try:
                el = self._page.wait_for_selector(sel, timeout=2000)
                if el:
                    return el, sel
            except PwTimeout:
                continue
        # Retry pass: 5s per selector
        for sel in INPUT_SELECTORS:
            try:
                el = self._page.wait_for_selector(sel, timeout=5000)
                if el:
                    return el, sel
            except PwTimeout:
                continue
        raise RuntimeError("Could not find Grok input element with any known selector")

    def _type_and_send(self, selector: str, prompt: str) -> None:
        """Type the prompt and press send."""
        # Type via fill(), fall back to execCommand
        try:
            self._page.fill(selector, prompt)
        except PwError:
            try:
                self._page.evaluate(
                    """([sel, text]) => {
                        const el = document.querySelector(sel);
                        el.focus();
                        document.execCommand('insertText', false, text);
                    }""",
                    [selector, prompt],
                )
            except Exception as exc:
                raise RuntimeError(f"Both fill() and execCommand failed: {exc}") from exc

        # Click send button
        for sel in SEND_SELECTORS:
            try:
                btn = self._page.wait_for_selector(sel, timeout=2000)
                if btn:
                    btn.click()
                    return
            except PwTimeout:
                continue

        # Fallback: find button matching /send|submit/i
        try:
            btn = self._page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const b = btns.find(b =>
                        /send|submit/i.test(b.textContent || '') ||
                        /send|submit/i.test(b.getAttribute('aria-label') || '')
                    );
                    if (b) { b.click(); return true; }
                    return false;
                }"""
            )
            if btn:
                return
        except Exception:
            pass

        # Last resort: dispatch Enter on input
        self._page.press(selector, "Enter")

    def _poll_response(self, prompt: str, timeout: float) -> dict:
        """Poll page text until stable (3 consecutive identical readings)."""
        marker = prompt[:60]
        start = time.time()
        prev_text = ""
        stable_count = 0

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                body = self._page.evaluate("() => document.body.innerText")
                partial = self._extract(body, marker)
                sources, source_count = self._extract_sources(body)
                return {"status": "timeout", "response": partial, "elapsed": round(elapsed, 1),
                        "sources": sources, "source_count": source_count}

            self._page.wait_for_timeout(2000)
            body = self._page.evaluate("() => document.body.innerText")

            if body == prev_text:
                stable_count += 1
            else:
                stable_count = 1
                prev_text = body

            if stable_count >= 3:
                response = self._extract(body, marker)
                sources, source_count = self._extract_sources(body)
                elapsed = time.time() - start
                return {"status": "ok", "response": response, "elapsed": round(elapsed, 1),
                        "sources": sources, "source_count": source_count}

    @staticmethod
    def _extract(body: str, marker: str) -> str:
        """Extract the response text after the prompt marker and clean it."""
        parts = body.split(marker, 1)
        raw = parts[-1] if len(parts) > 1 else body
        return _clean_response(raw)

    def _extract_sources(self, body: str) -> tuple[list[dict], int]:
        """Extract source links from DOM and parse source count from text."""
        # Get links from DOM
        raw_links = self._page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            return links
                .filter(a => {
                    const h = a.href;
                    return h.startsWith('http')
                        && !h.includes('grok.com')
                        && !h.includes('cookiepedia')
                        && !h.includes('onetrust');
                })
                .map(a => ({url: a.href, text: (a.textContent || '').trim().substring(0, 80)}));
        }""")
        # Deduplicate by URL
        seen = set()
        sources = []
        for link in raw_links:
            if link["url"] not in seen:
                seen.add(link["url"])
                sources.append(link)
        # Parse source count from body text
        source_count = 0
        m = re.search(r"(\d+)\s*sources?", body, re.IGNORECASE)
        if m:
            source_count = int(m.group(1))
        return sources, source_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(self, prompt: str, timeout: float = 120, mode: str | None = None) -> dict:
        """Send a prompt and return the response."""
        self._ensure_grok()
        if mode:
            self._select_mode(mode)
        _el, selector = self._find_input()
        self._type_and_send(selector, prompt)
        result = self._poll_response(prompt, timeout)
        result["query"] = prompt
        result["mode"] = mode
        return result

    def health(self) -> dict:
        """Return current browser state."""
        url = self._page.url
        return {
            "status": "ok",
            "url": url,
            "on_grok": url.startswith(GROK_URL),
            "version": VERSION,
        }

    def history(self) -> dict:
        """Return the current page text content."""
        body = self._page.evaluate("() => document.body.innerText")
        return {"status": "ok", "content": _clean_response(body), "raw_length": len(body)}

    def new_conversation(self) -> dict:
        """Navigate to grok.com for a fresh conversation."""
        self._page.goto(GROK_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(2000)
        return {"status": "ok"}

    def evaluate(self, js: str) -> dict:
        """Run arbitrary JS in the page and return the result."""
        result = self._page.evaluate(js)
        return {"status": "ok", "result": result}

    def close(self) -> None:
        """Shut down browser and Playwright."""
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


# ======================================================================
# HTTP Server
# ======================================================================


class _Handler(http.server.BaseHTTPRequestHandler):
    bridge: GrokBridge  # set on the class before serving

    def log_message(self, fmt, *args):  # noqa: D401
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {fmt % args}", file=sys.stderr)

    def _json_response(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET -----------------------------------------------------------

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            try:
                self._json_response(200, self.bridge.health())
            except Exception as exc:
                self._json_response(500, {"status": "error", "error": str(exc)})
        elif self.path == "/history":
            try:
                self._json_response(200, self.bridge.history())
            except Exception as exc:
                self._json_response(500, {"status": "error", "error": str(exc)})
        else:
            self._json_response(404, {"status": "error", "error": "not found"})

    # --- POST ----------------------------------------------------------

    def do_POST(self):  # noqa: N802
        if self.path == "/chat":
            self._handle_chat()
        elif self.path == "/new":
            self._handle_new()
        elif self.path == "/eval":
            self._handle_eval()
        else:
            self._json_response(404, {"status": "error", "error": "not found"})

    def _handle_chat(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._json_response(400, {"status": "error", "error": "invalid JSON"})
            return
        prompt = data.get("prompt")
        if not prompt or not isinstance(prompt, str):
            self._json_response(400, {"status": "error", "error": "missing or invalid 'prompt' field"})
            return
        timeout = data.get("timeout", 120)
        mode = data.get("mode")
        if mode and mode not in VALID_MODES:
            self._json_response(400, {"status": "error", "error": f"invalid mode '{mode}', must be one of: {', '.join(sorted(VALID_MODES))}"})
            return
        try:
            result = self.bridge.chat(prompt, timeout=timeout, mode=mode)
            self._json_response(200, result)
        except Exception as exc:
            self._json_response(500, {"status": "error", "error": str(exc)})

    def _handle_eval(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._json_response(400, {"status": "error", "error": "invalid JSON"})
            return
        js = data.get("js")
        if not js or not isinstance(js, str):
            self._json_response(400, {"status": "error", "error": "missing 'js' field"})
            return
        try:
            result = self.bridge.evaluate(js)
            self._json_response(200, result)
        except Exception as exc:
            self._json_response(500, {"status": "error", "error": str(exc)})

    def _handle_new(self) -> None:
        try:
            result = self.bridge.new_conversation()
            self._json_response(200, result)
        except Exception as exc:
            self._json_response(500, {"status": "error", "error": str(exc)})


class _Server(http.server.HTTPServer):
    allow_reuse_address = True


# ======================================================================
# CLI
# ======================================================================


def _login_mode() -> None:
    """Launch headed Chrome for manual login, block until closed."""
    print("Launching headed Chrome for login…")
    print(f"User data: {USER_DATA_DIR}")
    print("Log into your X/Twitter account on grok.com, then close the browser.")
    bridge = GrokBridge(headless=False)
    bridge._page.goto(GROK_URL, wait_until="domcontentloaded")
    try:
        # Block until the browser context is closed by the user
        bridge._page.wait_for_event("close", timeout=0)
    except Exception:
        pass
    finally:
        bridge.close()
    print("Login session saved.")


def _server_mode(host: str, port: int, headless: bool) -> None:
    """Launch browser and start HTTP server."""
    bridge = GrokBridge(headless=headless)

    # Signal handling for clean shutdown
    def _shutdown(sig, _frame):
        name = signal.Signals(sig).name
        print(f"\n[{name}] Shutting down…", file=sys.stderr)
        bridge.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _Handler.bridge = bridge
    server = _Server((host, port), _Handler)
    print(f"grok-bridge {VERSION} listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except Exception:
        bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Grok ↔ REST API bridge")
    parser.add_argument("--login", action="store_true", help="Launch headed Chrome for manual login")
    parser.add_argument("--port", type=int, default=19998, help="HTTP server port (default: 19998)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP server bind address (default: 0.0.0.0)")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    args = parser.parse_args()

    if args.login:
        _login_mode()
    else:
        _server_mode(args.host, args.port, args.headless)


if __name__ == "__main__":
    main()
