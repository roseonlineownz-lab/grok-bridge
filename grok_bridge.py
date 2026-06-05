#!/usr/bin/env python3
"""grok-bridge v2: Async Playwright + stealth + humanized input + SSE interception + session pool.

Drop-in replacement for v1. Same REST API on :19998, same --login mode.
"""

import argparse
import asyncio
import json
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from playwright.async_api import (
    async_playwright,
    TimeoutError as PwTimeout,
    Error as PwError,
)

VERSION = "v2"
GROK_URL = "https://grok.com"
BASE_DIR = str(Path.home() / ".grok-bridge")
USER_DATA_DIR = str(Path.home() / ".grok-bridge" / "chrome-data")  # legacy compat
PROFILES_DIR = str(Path.home() / ".grok-bridge" / "profiles")
POOL_SIZE = 3

# ── Selectors (multiple fallbacks) ────────────────────────────────────
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

# ── UI artifacts to strip ─────────────────────────────────────────────
TRAILING_MARKERS = [
    "\nAsk anything", "\nDeepSearch", "\nThink Harder", "\nThink\n",
    "\nAttach", "\nGrok", "\nFast\n", "\nFast", "\nAuto\n", "\nAuto",
    "\nExpert\n", "\nExpert", "\nHeavy\n", "\nHeavy", "\nUpgrade to",
]

# ── Stealth patches (injected at context level) ──────────────────────
STEALTH_SCRIPT = """
// webdriver — mimic native descriptor
Object.defineProperty(Navigator.prototype, 'webdriver', {
    get: () => false,
    configurable: true,
    enumerable: true,
});
// chrome object
Object.defineProperty(window, 'chrome', {
    value: { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} },
    configurable: false,
    enumerable: true,
});
// permissions
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(params);
// plugins (headless indicator)
Object.defineProperty(Navigator.prototype, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
    ],
    configurable: true,
    enumerable: true,
});
// languages
Object.defineProperty(Navigator.prototype, 'languages', {
    get: () => ['en-US', 'en', 'nl'],
    configurable: true,
    enumerable: true,
});
// hardware concurrency (avoid headless=1)
Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
    enumerable: true,
});

// ── SSE interception: capture Grok stream responses ───────────────────
window.__GROK_CAPTURED = '';
window.__GROK_CAPTURED_DONE = false;

const _origFetch = window.fetch;
window.fetch = async function(resource, init) {
    const url = typeof resource === 'string' ? resource : (resource.url || '');
    const resp = await _origFetch.apply(this, arguments);

    // Intercept Grok chat API calls
    if (url && (url.includes('/rest/app-chat') || url.includes('/api/chat') ||
                (url.includes('grok.com') && url.includes('/chat')))) {
        try {
            const clone = resp.clone();
            const reader = clone.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            const pump = async () => {
                try {
                    const {done, value} = await reader.read();
                    if (done) {
                        window.__GROK_CAPTURED = buf;
                        window.__GROK_CAPTURED_DONE = true;
                        return;
                    }
                    buf += decoder.decode(value, {stream: true});
                    return pump();
                } catch(e) {
                    window.__GROK_CAPTURED_DONE = true;
                }
            };
            pump();
        } catch(e) {
            window.__GROK_CAPTURED_DONE = true;
        }
    }
    return resp;
};
"""


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _clean_response(text: str) -> str:
    for marker in TRAILING_MARKERS:
        idx = text.rfind(marker)
        if idx != -1:
            text = text[:idx]
    text = re.sub(r"\n[0-9]+(\.[0-9]+)?s\n", "\n", text)
    text = re.sub(r"\n[0-9]+ms\n", "\n", text)
    text = re.sub(r"\n[0-9]+(\.[0-9]+)?s$", "", text)
    text = re.sub(r"\n[0-9]+ms$", "", text)
    text = re.sub(r"^\s*Thought for [0-9]+s\s*\n*", "", text)
    text = re.sub(r"\n(Share|Compare|Make it|Explain|Toggle|Like|Dislike).*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _snowflake_to_iso(status_id: str) -> str | None:
    try:
        sid = int(status_id)
        ts_ms = (sid >> 22) + 1288834974657
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return None


_X_STATUS_RE = re.compile(r"x\.com/.+/status/(\d+)")


def _parse_sse_response(raw: str) -> str:
    """Parse SSE/JSON stream from Grok into clean text."""
    if not raw:
        return ""
    chunks = []
    for line in raw.split("\n"):
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
                if "text" in obj:
                    chunks.append(obj["text"])
                elif "choices" in obj and "delta" in obj["choices"][0]:
                    chunks.append(obj["choices"][0]["delta"].get("content", ""))
                elif "result" in obj:
                    chunks.append(obj["result"])
                elif "message" in obj and "content" in obj["message"]:
                    chunks.append(obj["message"]["content"])
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        elif line.startswith("{"):
            try:
                obj = json.loads(line)
                if "text" in obj:
                    chunks.append(obj["text"])
                elif "choices" in obj:
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    chunks.append(delta.get("content", ""))
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
    return "".join(chunks).strip()


# ═══════════════════════════════════════════════════════════════════════
# Session Pool
# ═══════════════════════════════════════════════════════════════════════

class SessionPool:
    """Manages multiple Chrome profiles with health scoring for anti-burnout."""

    def __init__(self, pool_size: int = POOL_SIZE):
        os.makedirs(PROFILES_DIR, exist_ok=True)
        self.profiles = [
            {
                "id": i,
                "path": os.path.join(PROFILES_DIR, f"profile_{i}"),
                "health": 100,
                "in_use": False,
                "context": None,
                "page": None,
            }
            for i in range(pool_size)
        ]
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def start(self, headless: bool = False):
        self._playwright = await async_playwright().start()
        brave_path = "/snap/bin/brave"
        channel = "chromium" if os.path.exists(brave_path) else "chrome"
        executable = brave_path if os.path.exists(brave_path) else None
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            channel=channel,
            executable_path=executable,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

    async def get_healthy_session(self) -> dict:
        async with self._lock:
            available = [p for p in self.profiles if not p["in_use"] and p["health"] > 30]
            if not available:
                best = max(self.profiles, key=lambda p: p["health"])
                print(f"[pool] All profiles burned — forcing profile {best['id']} (health={best['health']})", file=sys.stderr)
                available = [best]
            profile = max(available, key=lambda p: p["health"])
            profile["in_use"] = True

        if profile["context"] is None:
            # Load saved storage state if available (enables cookie persistence)
            state_file = os.path.join(profile["path"], "state.json")
            storage_state = None
            if os.path.exists(state_file):
                try:
                    with open(state_file) as f:
                        storage_state = json.load(f)
                except Exception:
                    pass
            ctx = await self._browser.new_context(
                storage_state=storage_state,
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            await ctx.add_init_script(STEALTH_SCRIPT)
            page = await ctx.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            profile["context"] = ctx
            profile["page"] = page

        return profile

    async def _save_profile_state(self, profile: dict):
        """Persist cookies/session to disk so they survive restarts."""
        if profile["context"]:
            try:
                state = await profile["context"].storage_state()
                os.makedirs(profile["path"], exist_ok=True)
                with open(os.path.join(profile["path"], "state.json"), "w") as f:
                    json.dump(state, f)
            except Exception:
                pass

    def penalize(self, profile_id: int, penalty: int = 30):
        for p in self.profiles:
            if p["id"] == profile_id:
                p["health"] = max(0, p["health"] - penalty)
                p["in_use"] = False
                print(f"[pool] Profile {profile_id} penalized {penalty} → health={p['health']}", file=sys.stderr)
                return

    async def _save_profile_state(self, profile: dict):
        """Persist cookies/session to disk so they survive restarts."""
        if profile["context"]:
            try:
                state = await profile["context"].storage_state()
                os.makedirs(profile["path"], exist_ok=True)
                with open(os.path.join(profile["path"], "state.json"), "w") as f:
                    json.dump(state, f)
            except Exception:
                pass

    async def reward(self, profile_id: int, reward: int = 5):
        for p in self.profiles:
            if p["id"] == profile_id:
                p["health"] = min(100, p["health"] + reward)
                p["in_use"] = False
                await self._save_profile_state(p)
                return

    async def close_all(self):
        for p in self.profiles:
            if p["context"]:
                try:
                    await p["context"].close()
                except Exception:
                    pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
# Grok Bridge
# ═══════════════════════════════════════════════════════════════════════

class GrokBridge:
    """Async bridge to grok.com using Playwright + stealth + humanized input."""

    def __init__(self, headless: bool = False):
        self._headless = headless
        self._pool = SessionPool()
        self._profile = None  # current active profile

    async def start(self):
        await self._pool.start(headless=self._headless)

    async def close(self):
        await self._pool.close_all()

    # ── Humanized mouse (quadratic Bézier) ────────────────────────────

    async def _human_mouse_move(self, page, target_x: float, target_y: float,
                                  duration_ms: float = 500):
        """Move mouse along a quadratic Bézier curve with jitter."""
        try:
            pos = await page.evaluate("() => ({x: window.__mx || 0, y: window.__my || 0})")
        except Exception:
            pos = {"x": 960, "y": 540}  # viewport center fallback
        start_x, start_y = pos["x"], pos["y"]

        steps = max(10, int(duration_ms / 10))
        # Random control point offset for natural curvature
        ctrl_x = start_x + (target_x - start_x) * 0.4 + random.uniform(-60, 60)
        ctrl_y = start_y + (target_y - start_y) * 0.3 + random.uniform(-40, 40)

        for i in range(steps + 1):
            t = i / steps
            # Quadratic Bézier: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
            bx = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * target_x
            by = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * target_y
            bx += random.uniform(-2, 2)
            by += random.uniform(-2, 2)
            await page.mouse.move(bx, by)
            await asyncio.sleep((duration_ms / 1000) / steps * random.uniform(0.5, 1.5))

        # Track position for next move
        await page.evaluate(
            f"() => {{ window.__mx = {target_x}; window.__my = {target_y}; }}"
        )

    # ── Humanized typing ──────────────────────────────────────────────

    async def _human_type(self, page, text: str):
        """Type text with variable inter-keystroke delays and thinking pauses."""
        for char in text:
            await asyncio.sleep(random.uniform(0.03, 0.15))
            if char in (" ", ".", ",", "!", "?", "\n"):
                await asyncio.sleep(random.uniform(0.15, 0.5))
            await page.keyboard.type(char)

    # ── Navigation ────────────────────────────────────────────────────

    async def _ensure_grok(self, page):
        url = page.url
        if not url.startswith(GROK_URL):
            await page.goto(GROK_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

    # ── Mode selection ────────────────────────────────────────────────

    async def _select_mode(self, page, mode: str):
        try:
            await page.evaluate("""() => {
                const btn = document.querySelector('button[aria-label="Model select"]');
                if (!btn) return;
                btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
                btn.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
                btn.click();
            }""")
            await page.wait_for_timeout(500)
            mode_lower = mode.lower()
            clicked = await page.evaluate(
                """(m) => {
                    const items = Array.from(
                        document.querySelectorAll('[role=menu] [role=menuitem]')
                    );
                    const tgt = items.find(i =>
                        i.textContent.trim().toLowerCase().startsWith(m)
                    );
                    if (tgt) { tgt.click(); return true; }
                    return false;
                }""",
                mode_lower,
            )
            if not clicked:
                raise RuntimeError(f"Mode '{mode}' not found in dropdown")
            await page.wait_for_timeout(300)
        except Exception as e:
            print(f"[bridge] Mode selection failed: {e}", file=sys.stderr)

    # ── Input detection ───────────────────────────────────────────────

    async def _find_input(self, page):
        for sel in INPUT_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=2000)
                if el:
                    return el, sel
            except PwTimeout:
                continue
        for sel in INPUT_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=5000)
                if el:
                    return el, sel
            except PwTimeout:
                continue
        raise RuntimeError("Could not find Grok input element")

    # ── Turnstile detection ───────────────────────────────────────────

    async def _handle_turnstile(self, page):
        """Detect and attempt to resolve Cloudflare Turnstile challenges."""
        try:
            iframe = await page.wait_for_selector(
                'iframe[src*="challenges.cloudflare.com"]', timeout=3000
            )
            if iframe:
                box = await iframe.bounding_box()
                if box:
                    # Click near the center of the iframe
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await asyncio.sleep(random.uniform(2, 4))
                    print("[bridge] Turnstile detected — attempted click", file=sys.stderr)
        except PwTimeout:
            pass  # No Turnstile — good

    # ── Thinking detection ────────────────────────────────────────────

    async def _wait_thinking_done(self, page, timeout: float = 120) -> bool:
        """Wait until 'Thinking...' disappears. Returns False on timeout."""
        start = time.time()
        while time.time() - start < timeout:
            thinking = await page.query_selector('div:has-text("Thinking")')
            if not thinking:
                return True
            await asyncio.sleep(1)
        return False

    # ── SSE-based response capture ────────────────────────────────────

    async def _capture_sse(self, page, timeout: float = 30) -> str:
        """Wait for the in-browser fetch interceptor to capture Grok's SSE stream."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                done = await page.evaluate("() => window.__GROK_CAPTURED_DONE")
                if done:
                    raw = await page.evaluate("() => window.__GROK_CAPTURED")
                    # Reset for next call
                    await page.evaluate("""() => {
                        window.__GROK_CAPTURED = '';
                        window.__GROK_CAPTURED_DONE = false;
                    }""")
                    return _parse_sse_response(raw)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return ""

    # ── DOM-based response capture (fallback) ─────────────────────────

    async def _poll_dom_response(self, page, prompt: str, timeout: float = 120) -> dict:
        """Poll body.innerText until stable (3 consecutive identical readings)."""
        marker = prompt[:60]
        start = time.time()
        prev_text = ""
        stable_count = 0

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                body = await page.evaluate("() => document.body.innerText")
                partial = self._dom_extract(body, marker)
                sources, sc = await self._extract_sources(page, body)
                return {
                    "status": "timeout",
                    "response": partial,
                    "elapsed": round(elapsed, 1),
                    "sources": sources,
                    "source_count": sc,
                }

            await asyncio.sleep(1.5)
            body = await page.evaluate("() => document.body.innerText")

            if body == prev_text:
                stable_count += 1
            else:
                stable_count = 1
                prev_text = body

            if stable_count >= 3:
                response = self._dom_extract(body, marker)
                sources, sc = await self._extract_sources(page, body)
                return {
                    "status": "ok",
                    "response": response,
                    "elapsed": round(time.time() - start, 1),
                    "sources": sources,
                    "source_count": sc,
                }

    def _dom_extract(self, body: str, marker: str) -> str:
        parts = body.split(marker, 1)
        return _clean_response(parts[-1] if len(parts) > 1 else body)

    async def _extract_sources(self, page, body: str) -> tuple:
        raw = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            return links
                .filter(a => {
                    const h = a.href;
                    return h.startsWith('http') && !h.includes('grok.com')
                        && !h.includes('cookiepedia') && !h.includes('onetrust');
                })
                .map(a => ({url: a.href, text: (a.textContent || '').trim().substring(0, 80)}));
        }""")
        seen = set()
        sources = []
        for link in raw:
            clean_url = re.sub(r"\?referrer=grok-com$", "", link["url"])
            if clean_url in seen:
                continue
            seen.add(clean_url)
            entry = {"url": clean_url, "text": link["text"]}
            m = _X_STATUS_RE.search(clean_url)
            if m:
                ts = _snowflake_to_iso(m.group(1))
                if ts:
                    entry["timestamp"] = ts
            sources.append(entry)
        sc = 0
        m = re.search(r"(\d+)\s*sources?", body, re.IGNORECASE)
        if m:
            sc = int(m.group(1))
        return sources, sc

    # ── Public API ────────────────────────────────────────────────────

    async def chat(self, prompt: str, timeout: float = 120,
                   mode: str | None = None) -> dict:
        """Send a chat prompt and return the response."""
        profile = await self._pool.get_healthy_session()
        pid = profile["id"]
        page = profile["page"]

        try:
            await self._ensure_grok(page)

            # Handle any Turnstile challenge
            await self._handle_turnstile(page)

            # Select mode if specified
            if mode:
                await self._select_mode(page, mode)

            # Find input and interact
            _el, selector = await self._find_input(page)
            box = await _el.bounding_box()
            if box:
                await self._human_mouse_move(
                    page, box["x"] + 10, box["y"] + box["height"] / 2
                )

            await self._human_type(page, prompt)

            # Click send button
            sent = False
            for sel in SEND_SELECTORS:
                try:
                    btn = await page.wait_for_selector(sel, timeout=2000)
                    if btn:
                        await btn.click()
                        sent = True
                        break
                except PwTimeout:
                    continue
            if not sent:
                await page.keyboard.press("Enter")

            # Try SSE capture first (short timeout, immune to UI changes)
            sse_text = await self._capture_sse(page, timeout=30)
            if sse_text and len(sse_text) > 10:
                await self._pool.reward(pid)
                return {
                    "status": "ok",
                    "response": sse_text,
                    "query": prompt,
                    "mode": mode,
                    "capture": "sse",
                }

            # Fall back to DOM polling
            result = await self._poll_dom_response(page, prompt, timeout=timeout)
            result["query"] = prompt
            result["mode"] = mode
            result["capture"] = "dom"
            await self._pool.reward(pid)
            return result

        except (PwTimeout, RuntimeError) as e:
            self._pool.penalize(pid, penalty=40)
            return {"status": "error", "error": str(e), "query": prompt, "mode": mode}
        except Exception as e:
            self._pool.penalize(pid, penalty=20)
            return {"status": "error", "error": str(e), "query": prompt, "mode": mode}

    async def health(self) -> dict:
        profile = await self._pool.get_healthy_session()
        pid = profile["id"]
        try:
            url = profile["page"].url
            await self._pool.reward(pid)
            return {
                "status": "ok",
                "url": url,
                "on_grok": url.startswith(GROK_URL),
                "version": VERSION,
            }
        except Exception as e:
            self._pool.penalize(pid, penalty=10)
            return {"status": "error", "error": str(e), "version": VERSION}

    async def history(self) -> dict:
        profile = await self._pool.get_healthy_session()
        pid = profile["id"]
        try:
            body = await profile["page"].evaluate("() => document.body.innerText")
            await self._pool.reward(pid)
            return {"status": "ok", "content": _clean_response(body), "raw_length": len(body)}
        except Exception as e:
            self._pool.penalize(pid, penalty=10)
            return {"status": "error", "error": str(e)}

    async def new_conversation(self) -> dict:
        profile = await self._pool.get_healthy_session()
        pid = profile["id"]
        try:
            await profile["page"].goto(GROK_URL, wait_until="domcontentloaded")
            await profile["page"].wait_for_timeout(2000)
            await self._pool.reward(pid)
            return {"status": "ok"}
        except Exception as e:
            self._pool.penalize(pid, penalty=10)
            return {"status": "error", "error": str(e)}

    async def evaluate(self, js: str) -> dict:
        profile = await self._pool.get_healthy_session()
        pid = profile["id"]
        try:
            result = await profile["page"].evaluate(js)
            await self._pool.reward(pid)
            return {"status": "ok", "result": result}
        except Exception as e:
            self._pool.penalize(pid, penalty=10)
            return {"status": "error", "error": str(e)}

    async def pool_stats(self) -> dict:
        return {
            "profiles": [
                {"id": p["id"], "health": p["health"], "in_use": p["in_use"]}
                for p in self._pool.profiles
            ]
        }


# ═══════════════════════════════════════════════════════════════════════
# aiohttp HTTP Server
# ═══════════════════════════════════════════════════════════════════════

def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


class BridgeServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 19998, headless: bool = False):
        self._host = host
        self._port = port
        self._headless = headless
        self._bridge = GrokBridge(headless=headless)
        self._app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/history", self._handle_history)
        self._app.router.add_get("/pool", self._handle_pool)
        self._app.router.add_post("/chat", self._handle_chat)
        self._app.router.add_post("/new", self._handle_new)
        self._app.router.add_post("/eval", self._handle_eval)

    async def start(self):
        await self._bridge.start()
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        print(f"grok-bridge {VERSION} listening on {self._host}:{self._port}", file=sys.stderr)

    async def close(self):
        await self._bridge.close()

    # ── Handlers ──────────────────────────────────────────────────────

    async def _handle_health(self, request):
        return _json(await self._bridge.health())

    async def _handle_history(self, request):
        return _json(await self._bridge.history())

    async def _handle_pool(self, request):
        return _json(await self._bridge.pool_stats())

    async def _handle_chat(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return _json({"status": "error", "error": "invalid JSON"}, 400)
        prompt = data.get("prompt")
        if not prompt or not isinstance(prompt, str):
            return _json({"status": "error", "error": "missing 'prompt'"}, 400)
        timeout = data.get("timeout", 120)
        mode = data.get("mode")
        if mode and mode not in VALID_MODES:
            return _json(
                {"status": "error", "error": f"invalid mode '{mode}'"}, 400
            )
        result = await self._bridge.chat(prompt, timeout=timeout, mode=mode)
        return _json(result)

    async def _handle_new(self, request):
        return _json(await self._bridge.new_conversation())

    async def _handle_eval(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return _json({"status": "error", "error": "invalid JSON"}, 400)
        js = data.get("js")
        if not js or not isinstance(js, str):
            return _json({"status": "error", "error": "missing 'js'"}, 400)
        return _json(await self._bridge.evaluate(js))


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

async def _login_mode():
    """Launch headed Chrome for manual login, block until closed."""
    print("Launching headed Chrome for login…")
    print(f"User data: {USER_DATA_DIR}")
    print("Log into your Grok account, then close the browser.")
    pw = await async_playwright().start()
    brave_path = "/snap/bin/brave"
    channel = "chromium" if os.path.exists(brave_path) else "chrome"
    executable = brave_path if os.path.exists(brave_path) else None
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        channel=channel,
        executable_path=executable,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    await ctx.add_init_script(STEALTH_SCRIPT)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    await page.goto(GROK_URL, wait_until="domcontentloaded")
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass
    finally:
        # Export session to all pool profiles so the bridge can use them
        try:
            state = await ctx.storage_state()
            for i in range(POOL_SIZE):
                pdir = os.path.join(PROFILES_DIR, f"profile_{i}")
                os.makedirs(pdir, exist_ok=True)
                with open(os.path.join(pdir, "state.json"), "w") as f:
                    json.dump(state, f)
            print(f"Login session exported to {POOL_SIZE} profiles.")
        except Exception as e:
            print(f"Warning: could not export session state: {e}")
        await ctx.close()
        await pw.stop()
    print("Login session saved.")


async def _server_mode(host: str, port: int, headless: bool):
    server = BridgeServer(host=host, port=port, headless=headless)
    await server.start()
    # Keep running until signal
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()
    print("\nShutting down…", file=sys.stderr)
    await server.close()


def main():
    parser = argparse.ArgumentParser(description="Grok ↔ REST API bridge v2")
    parser.add_argument("--login", action="store_true", help="Launch headed Chrome for manual login")
    parser.add_argument("--port", type=int, default=19998, help="HTTP server port (default: 19998)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    args = parser.parse_args()

    if args.login:
        asyncio.run(_login_mode())
    else:
        asyncio.run(_server_mode(args.host, args.port, args.headless))


if __name__ == "__main__":
    main()
