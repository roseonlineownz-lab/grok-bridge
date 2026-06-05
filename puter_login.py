#!/usr/bin/env python3
"""
Puter login — one-time headed CloakBrowser login to obtain a Puter auth token.
After you log in / sign up (solving any captcha) and close the window, the auth
token is extracted from localStorage and written to ~/.config/novamaster/puter.env
(mode 600). The token is then used headlessly by puter_bridge for free AI calls.
"""
import os, sys, json, stat
from pathlib import Path

try:
    from cloakbrowser import launch_persistent_context
except ImportError:
    print("[FATAL] cloakbrowser not found", file=sys.stderr)
    sys.exit(1)

PROFILE_DIR = os.path.expanduser("~/.nova/puter_cloak")
ENV_PATH = os.path.expanduser("~/.config/novamaster/puter.env")
PUTER_URL = "https://puter.com"
os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)


def _extract_token(page):
    """Pull the Puter auth token from localStorage (key varies across versions)."""
    try:
        data = page.evaluate("""
            () => {
                const out = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out[k] = localStorage.getItem(k);
                }
                return out;
            }
        """)
    except Exception as e:
        print(f"[token] localStorage read failed: {e}", file=sys.stderr)
        return None
    # Common keys Puter uses for the auth token
    for key in ("puter.auth.token", "auth_token", "puter_auth_token", "token"):
        if data.get(key):
            return data[key]
    # Fallback: any value that looks like a JWT/long token
    for k, v in data.items():
        if isinstance(v, str) and v.count(".") >= 2 and len(v) > 60:
            print(f"[token] using heuristic key '{k}'", file=sys.stderr)
            return v
    return None


def main():
    print(f"Opening Puter login (profile: {PROFILE_DIR})")
    print("Log in or sign up at puter.com (solve any captcha), then CLOSE the window.")
    ctx = launch_persistent_context(PROFILE_DIR, headless=False, humanize=True,
                                    args=["--no-first-run", "--disable-session-crashed-bubble"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(PUTER_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:
        pass

    # Window closed — try to read token from any remaining page, else reopen briefly
    token = None
    try:
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        if pg.url == "about:blank":
            pg.goto(PUTER_URL, wait_until="domcontentloaded", timeout=20000)
            pg.wait_for_timeout(2000)
        token = _extract_token(pg)
    except Exception as e:
        print(f"[token] extraction error: {e}", file=sys.stderr)

    try:
        ctx.close()
    except Exception:
        pass

    if not token:
        print("[FAIL] No Puter token found — did the login complete?", file=sys.stderr)
        sys.exit(2)

    with open(ENV_PATH, "w") as f:
        f.write(f"PUTER_AUTH_TOKEN={token}\n")
    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600
    print(f"[OK] Puter token saved (mode 600) → {ENV_PATH}")
    print("secret-like value present; not printed.")


if __name__ == "__main__":
    main()
