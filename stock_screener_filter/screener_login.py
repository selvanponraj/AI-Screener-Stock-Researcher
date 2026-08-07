"""Open Screener and make sure a browser profile is logged in."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

from stock_screener_filter.config import load_env

load_env()
DEFAULT_URL = "https://www.screener.in/"
ACCOUNT_URL = "https://www.screener.in/user/account/"
DEFAULT_BROWSER_CHANNEL = os.getenv("SCREENER_BROWSER_CHANNEL", "msedge").strip() or "msedge"
DEFAULT_PROFILE_DIR = Path(
    os.getenv(
        "SCREENER_PROFILE_DIR",
        str(Path(".browser") / f"screener-profile-{DEFAULT_BROWSER_CHANNEL.lower().replace('msedge', 'edge')}"),
    )
)

LOGIN_SELECTORS = (
    "a[href*='/login']",
    "a:has-text('Login')",
    "button:has-text('Login')",
    "input[name='username']",
    "input[name='password']",
)

LOGGED_IN_SELECTORS = (
    "a[href*='/logout']",
    "button:has-text('Logout')",
    "text=Logout",
    "a[href*='/user/']",
    "a[href*='/user/account/']",
    "a[href*='/watchlist']",
    "a[href*='/alerts/']",
    "a[href*='/notebook/']",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open Screener in a persistent browser and wait for login when needed."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Screener URL to open after launching the browser. Default: {DEFAULT_URL}",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Directory where the browser session/cookies are stored.",
    )
    parser.add_argument(
        "--browser-channel",
        default=DEFAULT_BROWSER_CHANNEL,
        help=(
            "Installed browser channel to use, for example chrome or msedge. "
            f"Use 'chromium' to use Playwright's bundled browser. Default: {DEFAULT_BROWSER_CHANNEL}"
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="How long to wait for Screener to accept login before failing.",
    )
    parser.add_argument(
        "--email",
        default=os.getenv("SCREENER_EMAIL"),
        help="Screener email. Defaults to SCREENER_EMAIL or an interactive prompt.",
    )
    parser.add_argument(
        "--password-env",
        default="SCREENER_PASSWORD",
        help="Environment variable containing the Screener password.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the browser window open after login is confirmed.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Open the login page and wait for manual login instead of filling credentials.",
    )
    return parser.parse_args()


def first_visible(page: Page, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=1_000):
                return selector
        except (Error, TimeoutError):
            continue
    return None


def first_present(page: Page, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return selector
        except (Error, TimeoutError):
            continue
    return None


def looks_logged_in(page: Page) -> bool:
    parsed_url = urlparse(page.url)
    host = parsed_url.netloc.lower()
    path = parsed_url.path.lower()

    if not (host == "screener.in" or host.endswith(".screener.in")):
        return False

    if first_present(page, LOGGED_IN_SELECTORS):
        return True

    return False


def verify_logged_in_session(page: Page, url: str = DEFAULT_URL) -> bool:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1_000)
    if looks_logged_in(page):
        return True

    page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1_000)
    current = urlparse(page.url)
    if current.netloc.lower().endswith("screener.in") and "/login/" not in current.path.lower():
        return True

    return looks_logged_in(page)


def get_credentials(args: argparse.Namespace) -> tuple[str, str]:
    email = args.email or input("Screener email: ").strip()
    password = os.getenv(args.password_env)

    if not email or email.strip().lower().startswith("your-"):
        raise ValueError("Set SCREENER_EMAIL in .env before running automatic login.")

    if password is None:
        password = getpass.getpass(f"Screener password ({args.password_env}): ")

    if not password or password.strip().lower().startswith("your-"):
        raise ValueError("Set SCREENER_PASSWORD in .env before running automatic login.")

    return email, password


def login_with_credentials(
    page: Page,
    login_url: str,
    email: str,
    password: str,
    timeout_seconds: int,
) -> None:
    print(f"Opening login page: {login_url}", flush=True)
    page.goto(login_url, wait_until="domcontentloaded")

    username_input = page.locator("input[name='username']").first
    password_input = page.locator("input[name='password']").first

    username_input.wait_for(state="visible", timeout=timeout_seconds * 1_000)
    username_input.fill(email)
    password_input.fill(password)

    print("Submitting Screener email login...", flush=True)
    page.locator("button[type='submit'], input[type='submit']").first.click()

    for elapsed_seconds in range(timeout_seconds):
        page.wait_for_timeout(1_000)
        if looks_logged_in(page):
            print("Login confirmed.", flush=True)
            return

        if elapsed_seconds >= 2 and elapsed_seconds % 5 == 0:
            try:
                if verify_logged_in_session(page):
                    print("Login confirmed.", flush=True)
                    return
            except (Error, TimeoutError):
                continue

    raise RuntimeError(
        "Screener login did not complete. Check the email/password or any on-page prompt."
    )


def wait_for_manual_login(page: Page, login_url: str, timeout_seconds: int) -> None:
    print(f"Opening login page for manual login: {login_url}", flush=True)
    page.goto(login_url, wait_until="domcontentloaded")
    print("Complete login in the opened browser window.", flush=True)

    elapsed = 0
    while elapsed < timeout_seconds:
        if looks_logged_in(page):
            print("Manual login confirmed.", flush=True)
            return
        page.wait_for_timeout(1_000)
        elapsed += 1

    raise RuntimeError("Manual login did not complete before the timeout.")


def main() -> int:
    args = parse_args()
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    try:
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "viewport": {"width": 1366, "height": 850},
        }
        if args.browser_channel and args.browser_channel.lower() != "chromium":
            launch_kwargs["channel"] = args.browser_channel

        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Error as exc:
        playwright.stop()
        print(
            "Could not launch the browser.\n"
            "If Chrome is not installed, run with --browser-channel msedge or install "
            "Playwright's browser with: python -m playwright install chromium\n"
            f"\nDetails: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        page = context.pages[0] if context.pages else context.new_page()
        print(f"Opening {args.url}", flush=True)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)

        if verify_logged_in_session(page, args.url):
            print("Already logged in to Screener.", flush=True)
        else:
            login_url = urljoin(args.url, "/login/")
            if args.manual:
                wait_for_manual_login(page, login_url, args.timeout_seconds)
            else:
                email, password = get_credentials(args)
                login_with_credentials(page, login_url, email, password, args.timeout_seconds)
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_timeout(1_000)

        print(f"Ready for Screener automation. Profile saved at: {profile_dir}", flush=True)

        if args.keep_open:
            print("Browser will stay open. Press Ctrl+C in this terminal to stop.", flush=True)
            while True:
                page.wait_for_timeout(10_000)

        return 0
    finally:
        context.close()
        playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
