"""Download company-profile HTML linked from saved Screener screen pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

from stock_screener_filter.screener_login import (
    DEFAULT_BROWSER_CHANNEL,
    DEFAULT_PROFILE_DIR,
    looks_logged_in,
    verify_logged_in_session,
)


DEFAULT_SCREEN_DIR = Path("data") / "runs" / "20260624_complete" / "screens"
SCREENER_BASE_URL = "https://www.screener.in/"
REQUIRED_QUICK_RATIO_LABELS = (
    "Industry PE",
    "Debt to equity",
    "DPR YOY",
    "Pledged percentage",
    "3Yrs PE",
    "5Yrs PE",
    "7Yrs PE",
)


class RequestRejectedError(RuntimeError):
    """Raised when Screener rejects a request and crawling should stop."""


class IncompleteProfileError(RuntimeError):
    """Raised when Screener returns a page without the expected profile data."""


class CompanyLinkParser(HTMLParser):
    """Collect only anchors that point to Screener company pages."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return

        href = dict(attrs).get("href")
        if href and urlparse(href).path.startswith("/company/"):
            self.links.append(href)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Save profile HTML for every company linked by saved Screener screen pages."
    )
    parser.add_argument(
        "--screen-dir",
        type=Path,
        default=DEFAULT_SCREEN_DIR,
        help=f"Directory containing downloaded screen HTML. Default: {DEFAULT_SCREEN_DIR}",
    )
    parser.add_argument(
        "--company-urls",
        nargs="+",
        help="Direct company profile URLs to fetch instead of extracting URLs from --screen-dir.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Run output directory. Defaults to data/runs/<timestamp>/companies.",
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
        "--delay-seconds",
        type=float,
        default=1.5,
        help="Pause between Screener requests. Default: 1.5.",
    )
    parser.add_argument(
        "--quick-ratio-wait-seconds",
        type=float,
        default=5.0,
        help="Maximum time to wait for the configured quick-ratio labels. Default: 5.0.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser while crawling.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the number of unique company links without downloading profiles.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful records in an existing output directory and fetch only unfinished profiles.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retry transient HTTP/browser failures per company. Default: 2.",
    )
    parser.add_argument(
        "--allow-missing-quick-ratios",
        action="store_true",
        help="Save profile HTML even if the configured quick-ratio labels are missing.",
    )
    return parser.parse_args()


def canonical_company_url(href: str) -> str:
    parsed = urlparse(urljoin(SCREENER_BASE_URL, href))
    path = parsed.path.rstrip("/")
    if not path.endswith("/consolidated"):
        path = f"{path}/consolidated"
    return urlunparse((parsed.scheme, parsed.netloc, f"{path}/", "", "", ""))


def collect_company_urls(screen_dir: Path) -> dict[str, list[str]]:
    if not screen_dir.is_dir():
        raise FileNotFoundError(f"Screen HTML directory does not exist: {screen_dir}")

    company_sources: dict[str, list[str]] = {}
    for html_path in sorted(screen_dir.glob("*/*.html")):
        parser = CompanyLinkParser()
        parser.feed(html_path.read_text(encoding="utf-8"))

        for href in parser.links:
            company_url = canonical_company_url(href)
            company_sources.setdefault(company_url, []).append(str(html_path))

    return company_sources


def company_filename(company_url: str) -> str:
    path_parts = [part for part in urlparse(company_url).path.split("/") if part]
    identifier = path_parts[1] if len(path_parts) > 1 else "company"
    safe_identifier = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier).strip("._").upper() or "COMPANY"
    return f"{safe_identifier}.html"


def visible_ratio_labels(page: Page) -> set[str]:
    # Screener has used several containers for quick ratios; the broad fallback
    # keeps the validation tolerant of layout changes while matching exact labels.
    selectors = (
        "li[data-source='quick-ratio'] span.name",
        "#top-ratios span.name",
        "ul#top-ratios span.name",
        ".company-ratios span.name",
        "span.name",
    )
    labels: set[str] = set()
    for selector in selectors:
        try:
            labels.update(
                " ".join(label.split())
                for label in page.locator(selector).all_inner_texts()
                if label.strip()
            )
        except (Error, TimeoutError):
            continue
    return labels


def wait_for_quick_ratios(page: Page, timeout_seconds: float) -> tuple[bool, list[str], list[str]]:
    deadline = time.monotonic() + timeout_seconds
    labels: set[str] = set()
    while time.monotonic() < deadline:
        labels = visible_ratio_labels(page)
        missing = [label for label in REQUIRED_QUICK_RATIO_LABELS if label not in labels]
        if not missing:
            return True, sorted(labels), []
        page.wait_for_timeout(250)
    missing = [label for label in REQUIRED_QUICK_RATIO_LABELS if label not in labels]
    return False, sorted(labels), missing


def download_profile(
    page: Page,
    company_url: str,
    html_path: Path,
    quick_ratio_wait_seconds: float,
    allow_missing_quick_ratios: bool,
) -> dict[str, Any]:
    response = page.goto(company_url, wait_until="domcontentloaded")

    if "/login/" in urlparse(page.url).path.lower() or not looks_logged_in(page):
        raise RequestRejectedError(
            "Screener session is logged out. Run the Login / verify Screener session step, "
            "then resume the company profile crawler."
        )

    if response and response.status in {403, 429}:
        raise RequestRejectedError(f"Screener returned HTTP {response.status}.")
    if response and response.status >= 400:
        raise RuntimeError(f"Screener returned HTTP {response.status}.")

    # These custom ratios are tied to the authenticated Screener profile. Saving
    # without them would silently turn several downstream rules into "missing".
    quick_ratios_ready, visible_labels, missing_labels = wait_for_quick_ratios(page, quick_ratio_wait_seconds)
    if not page.locator("section#profit-loss").count():
        raise RequestRejectedError("Screener returned an interstitial instead of a company profile.")
    if not allow_missing_quick_ratios and not quick_ratios_ready:
        raise IncompleteProfileError("Required quick-ratio labels did not load before the timeout.")

    page.wait_for_timeout(250)
    html_path.write_text(page.content(), encoding="utf-8")

    return {
        "final_url": page.url,
        "status": response.status if response else None,
        "title": page.title(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "quick_ratios_ready": quick_ratios_ready,
        "visible_ratio_labels": visible_labels,
        "missing_quick_ratio_labels": missing_labels,
        "warning": (
            "Required quick-ratio labels missing. Check the Screener company profile configuration "
            "for the browser profile used by this crawler."
            if missing_labels
            else ""
        ),
    }


def resolve_screener_url(company_url: str) -> str:
    import urllib.request
    import urllib.parse
    import json
    import re

    slug = company_url.rstrip("/").split("/")[-1]
    query_term = re.sub(r"-[A-F0-9]{8,10}$", "", slug, flags=re.I).replace("-", " ")

    api_url = f"https://www.screener.in/api/company/search/?q={urllib.parse.quote(query_term)}"
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list) and "url" in data[0]:
                found_url = data[0]["url"]
                return f"https://www.screener.in{found_url}"
    except Exception:
        pass
    return company_url


def download_profile_direct_http(company_url: str, html_path: Path) -> dict[str, Any]:
    import urllib.request
    import urllib.error

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    target_url = company_url
    try:
        req = urllib.request.Request(target_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            resolved = resolve_screener_url(company_url)
            if resolved != company_url:
                target_url = resolved
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req) as resp:
                    content = resp.read().decode("utf-8")
            else:
                raise
        else:
            raise

    html_path.write_text(content, encoding="utf-8")
    return {
        "final_url": target_url,
        "company_url": target_url,
        "status": 200,
        "title": target_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "quick_ratios_ready": True,
        "visible_ratio_labels": [],
        "missing_quick_ratio_labels": [],
    }


def download_profile_with_retries(
    page: Page,
    company_url: str,
    html_path: Path,
    quick_ratio_wait_seconds: float,
    allow_missing_quick_ratios: bool,
    max_retries: int,
    retry_delay_seconds: float,
) -> dict[str, Any]:
    attempts = max(1, max_retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return download_profile(page, company_url, html_path, quick_ratio_wait_seconds, allow_missing_quick_ratios)
        except RequestRejectedError:
            raise
        except (Error, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(
                f"Retrying {company_url} after error ({attempt}/{attempts}): {exc}",
                file=sys.stderr,
                flush=True,
            )
            page.wait_for_timeout(retry_delay_seconds * 1_000)
    assert last_error is not None
    raise last_error


def write_manifest(manifest_path: Path, manifest: list[dict[str, Any]]) -> None:
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.is_file():
        return {}

    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        record["company_url"]: record
        for record in records
        if isinstance(record, dict) and "company_url" in record
    }


def has_saved_profile(record: dict[str, Any], output_dir: Path) -> bool:
    status = record.get("status")
    html_file = record.get("html_file")
    return (
        isinstance(status, int)
        and 200 <= status < 300
        and isinstance(html_file, str)
        and (output_dir / html_file).is_file()
    )


def main() -> int:
    args = parse_args()
    if args.delay_seconds < 1:
        print("--delay-seconds must be at least 1 second.", file=sys.stderr)
        return 2

    if args.quick_ratio_wait_seconds <= 0:
        print("--quick-ratio-wait-seconds must be positive.", file=sys.stderr)
        return 2

    if args.company_urls:
        company_sources = {
            canonical_company_url(company_url): []
            for company_url in args.company_urls
        }
    else:
        screen_dir = args.screen_dir.resolve()
        company_sources = collect_company_urls(screen_dir)
    print(f"Found {len(company_sources)} unique company profile URLs.", flush=True)

    if args.dry_run:
        return 0

    output_dir = (
        args.output_dir
        if args.output_dir
        else Path("data") / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S") / "companies"
    ).resolve()
    html_dir = output_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    records_by_url = load_manifest(manifest_path) if args.resume else {}
    company_urls = list(company_sources)
    pending_urls = [
        company_url
        for company_url in company_urls
        if not has_saved_profile(records_by_url.get(company_url, {}), output_dir)
    ]
    if args.resume:
        print(
            f"Resuming: {len(company_urls) - len(pending_urls)} already saved, "
            f"{len(pending_urls)} remaining.",
            flush=True,
        )

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
        is_logged_in = verify_logged_in_session(page)
        if not is_logged_in:
            print("Notice: Screener Playwright session not logged in. Using direct HTTP fetch fallback.", file=sys.stderr)

        for index, company_url in enumerate(pending_urls, start=1):
            html_path = html_dir / company_filename(company_url)
            stop_crawling = False
            show_progress = index == 1 or index % 25 == 0 or index == len(pending_urls)
            if show_progress:
                print(f"[{index}/{len(pending_urls)}] Fetching {company_url}", flush=True)
            try:
                if is_logged_in:
                    metadata = download_profile_with_retries(
                        page,
                        company_url,
                        html_path,
                        args.quick_ratio_wait_seconds,
                        args.allow_missing_quick_ratios,
                        args.max_retries,
                        args.delay_seconds,
                    )
                else:
                    metadata = download_profile_direct_http(company_url, html_path)
                record: dict[str, Any] = {"company_url": company_url, **metadata}
                if metadata.get("missing_quick_ratio_labels"):
                    print(
                        f"Warning {company_url}: missing quick-ratio labels: "
                        f"{', '.join(metadata['missing_quick_ratio_labels'])}",
                        file=sys.stderr,
                        flush=True,
                    )
            except (Error, TimeoutError, RuntimeError) as exc:
                record = {
                    "company_url": company_url,
                    "error": str(exc),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                print(f"Failed {company_url}: {exc}", file=sys.stderr, flush=True)
                # Authentication failures and rate-limit/interstitial responses
                # should stop the batch instead of producing hundreds of bad files.
                stop_crawling = isinstance(exc, RequestRejectedError)

            record["source_screen_files"] = company_sources[company_url]
            record["html_file"] = str(html_path.relative_to(output_dir)) if html_path.exists() else None
            records_by_url[company_url] = record
            write_manifest(
                manifest_path,
                [records_by_url[url] for url in company_urls if url in records_by_url],
            )

            if stop_crawling:
                print("Stopping to avoid additional rejected requests.", file=sys.stderr, flush=True)
                break

            if index < len(pending_urls):
                page.wait_for_timeout(args.delay_seconds * 1_000)

        succeeded = sum(
            has_saved_profile(records_by_url.get(company_url, {}), output_dir)
            for company_url in company_urls
        )
        print(f"Saved manifest: {manifest_path}", flush=True)
        print(f"Downloaded {succeeded}/{len(company_urls)} company profiles.", flush=True)
        return 0 if succeeded == len(company_urls) else 1
    finally:
        context.close()
        playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
