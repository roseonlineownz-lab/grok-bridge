#!/usr/bin/env python3
"""
Automated Puter login via existing Google session (roseonlineownz).
The puter_cloak profile already holds the Google session cookies, so the
Google account picker auto-completes. Extracts the Puter auth token to
~/.config/novamaster/puter.env (mode 600).
"""
import os, sys, stat

from cloakbrowser import launch_persistent_context

PROFILE_DIR = os.path.expanduser("~/.nova/puter_cloak")
ENV_PATH = os.path.expanduser("~/.config/novamaster/puter.env")
PUTER_URL = "https://puter.com"
EMAIL = "roseonlineownz"
os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)


def _extract_token(page):
    try:
        data = page.evaluate("""() => {
            const out = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i); out[k] = localStorage.getItem(k);
            }
            return out;
        }""")
    except Exception as e:
        print(f"[token] read failed: {e}", file=sys.stderr); return None
    for key in ("puter.auth.token", "auth_token", "puter_auth_token", "token"):
        if data.get(key):
            return data[key]
    for k, v in data.items():
        if isinstance(v, str) and v.count(".") >= 2 and len(v) > 60:
            print(f"[token] heuristic key '{k}'", file=sys.stderr); return v
    return None


def _click_any(page, selectors, timeout=4000):
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el:
                el.click()
                return True
        except Exception:
            continue
    return False


def main():
    headless = "--headed" not in sys.argv
    print(f"[puter] launching (headless={headless})", file=sys.stderr)
    ctx = launch_persistent_context(PROFILE_DIR, headless=headless, humanize=True,
                                    args=["--no-first-run", "--disable-session-crashed-bubble",
                                          "--disable-gpu", "--disable-software-rasterizer",
                                          "--disable-dev-shm-usage", "--disable-features=Vulkan,UseSkiaRenderer",
                                          "--js-flags=--max-old-space-size=1024"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(PUTER_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)

    # Already logged in?
    token = _extract_token(page)
    if token:
        print("[puter] already logged in", file=sys.stderr)
    else:
        # Open login, choose Google
        _click_any(page, ['button:has-text("Log In")', 'a:has-text("Log In")',
                           'button:has-text("Login")', 'text=Sign in'], timeout=6000)
        page.wait_for_timeout(1500)
        _click_any(page, ['button:has-text("Continue with Google")',
                           'text=Continue with Google', 'text=Sign in with Google',
                           '[aria-label*="Google"]', 'button:has-text("Google")'], timeout=6000)
        page.wait_for_timeout(3000)

        # Google account picker (session present → pick roseonlineownz)
        _click_any(page, [f'div[data-identifier*="{EMAIL}"]', f'text={EMAIL}',
                          f'[data-email*="{EMAIL}"]'], timeout=8000)
        # Consent / continue if shown
        page.wait_for_timeout(3000)
        _click_any(page, ['button:has-text("Continue")', 'button:has-text("Allow")',
                          '#submit_approve_access'], timeout=6000)

        # Wait for redirect back to puter + token
        for _ in range(15):
            page.wait_for_timeout(2000)
            if PUTER_URL.split("//")[1] in page.url:
                token = _extract_token(page)
                if token:
                    break

    try:
        ctx.close()
    except Exception:
        pass

    if not token:
        print("[FAIL] no Puter token — Google login may need manual step", file=sys.stderr)
        sys.exit(2)
    with open(ENV_PATH, "w") as f:
        f.write(f"PUTER_AUTH_TOKEN={token}\n")
    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)
    print(f"[OK] Puter token saved (600) → {ENV_PATH}; secret-like value present, not printed.")


if __name__ == "__main__":
    main()
