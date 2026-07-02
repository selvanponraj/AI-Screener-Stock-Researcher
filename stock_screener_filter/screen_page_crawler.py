"""Download every paginated page from configured Screener screens."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

from stock_screener_filter.screener_login import (
    DEFAULT_BROWSER_CHANNEL,
    DEFAULT_PROFILE_DIR,
)


DEFAULT_SCREEN_URLS = (
    "https://www.screener.in/screens/2122337/market-cap-to-profit/",
    "https://www.screener.in/screens/2242309/return-less-than-profit-growth/",
    "https://www.screener.in/screens/2402442/dpr/",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Save the HTML of every page in one or more Screener screens."
    )
    parser.add_argument(
        "--screens",
        nargs="+",
        default=list(DEFAULT_SCREEN_URLS),
        help="One or more Screener screen URLs.",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Browser profile containing the saved Screener session.",
    )
    parser.add_argument(
        "--browser-channel",
        default=DEFAULT_BROWSER_CHANNEL,
        help=f"Browser channel to use. Default: {DEFAULT_BROWSER_CHANNEL}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Run output directory. Defaults to data/runs/<timestamp>/screens.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Pause between page requests. Default: 1.0.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser while crawling.",
    )
    return parser.parse_args()


def screen_id(screen_url: str) -> str:
    parts = [part for part in urlparse(screen_url).path.split("/") if part]
    return parts[1] if len(parts) > 1 and parts[0] == "screens" else "screen"


def page_number(url: str) -> int:
    value = parse_qs(urlparse(url).query).get("page", ["1"])[0]
    try:
        return int(value)
    except ValueError:
        return 1


def pagination_links(page: Page) -> list[dict[str, str]]:
    return page.locator(".pagination a").evaluate_all(
        """anchors => anchors.map(anchor => ({
            href: anchor.href,
            text: (anchor.innerText || '').trim(),
            rel: anchor.getAttribute('rel') || '',
            ariaLabel: anchor.getAttribute('aria-label') || ''
        }))"""
    )


def next_page_url(page: Page, current_url: str) -> str | None:
    links = pagination_links(page)

    for link in links:
        label = f"{link['text']} {link['ariaLabel']}".strip().lower()
        if link["rel"].lower() == "next" or label in {"next", "next page", "next >", "next >>"}:
            return link["href"]

    current_number = page_number(current_url)
    later_pages = [
        link
        for link in links
        if page_number(link["href"]) > current_number
    ]
    if not later_pages:
        return None

    return min(later_pages, key=lambda link: page_number(link["href"]))["href"]


def crawl_screen(
    page: Page,
    screen_url: str,
    output_dir: Path,
    delay_seconds: float,
) -> list[dict[str, Any]]:
    identifier = screen_id(screen_url)
    screen_dir = output_dir / identifier
    screen_dir.mkdir(parents=True, exist_ok=True)

    current_url = screen_url
    visited_urls: set[str] = set()
    records: list[dict[str, Any]] = []

    while current_url not in visited_urls:
        visited_urls.add(current_url)
        print(f"Fetching {current_url}", flush=True)
        response = page.goto(current_url, wait_until="domcontentloaded")

        if "/login/" in urlparse(page.url).path.lower():
            raise RuntimeError("Screener redirected to login. Run the login helper first.")

        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except TimeoutError:
            pass

        current_number = page_number(page.url)
        html_path = screen_dir / f"page_{current_number:03d}.html"
        html_path.write_text(page.content(), encoding="utf-8")

        record = {
            "screen_id": identifier,
            "page": current_number,
            "source_url": current_url,
            "final_url": page.url,
            "title": page.title(),
            "status": response.status if response else None,
            "html_file": str(html_path.relative_to(output_dir.parent)),
        }
        records.append(record)
        print(f"Saved {html_path}", flush=True)

        next_url = next_page_url(page, page.url)
        if not next_url:
            break

        current_url = next_url
        if delay_seconds:
            page.wait_for_timeout(delay_seconds * 1_000)

    return records


def main() -> int:
    args = parse_args()
    run_dir = (
        args.output_dir
        if args.output_dir
        else Path("data") / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S") / "screens"
    )
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    try:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(Path(args.profile_dir).resolve()),
            "headless": not args.headed,
            "viewport": {"width": 1366, "height": 850},
        }
        if args.browser_channel.lower() != "chromium":
            launch_kwargs["channel"] = args.browser_channel

        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Error as exc:
        playwright.stop()
        print(f"Could not launch the browser: {exc}", file=sys.stderr)
        return 1

    try:
        page = context.pages[0] if context.pages else context.new_page()
        manifest: list[dict[str, Any]] = []
        for screen_url in args.screens:
            manifest.extend(crawl_screen(page, screen_url, run_dir, args.delay_seconds))

        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Saved manifest: {manifest_path}", flush=True)
        print(f"Downloaded {len(manifest)} screen pages.", flush=True)
        return 0
    finally:
        context.close()
        playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
