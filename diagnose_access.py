#!/usr/bin/env python3
"""Record what this host sees on Bahamut, to tell an IP block from a headless block or an expired session.

Run it on the service host with the service's config. It saves a screenshot,
HTML and a report for every step into --output; cookie values and the account
password are never written.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from bahamut_auto_bump import _first_visible, chrome_page, load_config

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
HEADERS = ("server", "cf-ray", "cf-mitigated", "cf-cache-status", "retry-after", "location")
LOGIN_TRIGGER = "a[onclick*='requireLoginIframe'], a:has-text('登入')"


def plain_http(url: str) -> dict[str, Any]:
    """Fetch without a browser: a challenge here means the IP itself is flagged."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, headers, body = exc.code, exc.headers, exc.read()
    except Exception as exc:
        return {"url": url, "error": repr(exc)}
    text = body.decode("utf-8", "replace")
    title = text.split("<title>", 1)[1].split("</title>", 1)[0].strip() if "<title>" in text else ""
    return {
        "url": url,
        "status": status,
        "title": title,
        "bytes": len(body),
        "headers": {name: headers.get(name) for name in HEADERS if headers.get(name)},
        "has_login_trigger": "requireLoginIframe" in text,
    }


def session_summary(path: str) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {"path": path, "exists": False}
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    cookies = []
    for cookie in state.get("cookies", []):
        expires = cookie.get("expires", -1)
        cookies.append({
            "name": cookie.get("name"),
            "domain": cookie.get("domain"),
            "expires": datetime.fromtimestamp(expires).isoformat() if expires and expires > 0 else "session",
            "expired": bool(expires and 0 < expires < time.time()),
        })
    modified = datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat()
    return {"path": path, "exists": True, "modified": modified, "cookies": cookies}


def browser_visit(config: dict[str, Any], url: str, label: str, output: Path, *, use_session: bool) -> dict[str, Any]:
    """Open url in the service's Chrome and snapshot it on load and again after a challenge would have finished."""
    result: dict[str, Any] = {"label": label, "url": url, "use_session": use_session}
    with chrome_page(config, use_session=use_session) as page:
        result["user_agent"] = page.evaluate("navigator.userAgent")
        result["webdriver"] = page.evaluate("navigator.webdriver")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            result["status"] = response.status if response else None
            result["headers"] = {name: response.headers.get(name) for name in HEADERS if response and response.headers.get(name)}
        except Exception as exc:
            result["goto_error"] = repr(exc)
        for moment, wait_ms in (("on_load", 0), ("after_15s", 15000)):
            page.wait_for_timeout(wait_ms)
            snapshot: dict[str, Any] = {"final_url": page.url}
            try:
                snapshot["title"] = page.title()
                snapshot["login_trigger_visible"] = _first_visible(page, LOGIN_TRIGGER) is not None
                snapshot["body_start"] = page.locator("body").inner_text(timeout=5000)[:300]
            except Exception as exc:
                snapshot["error"] = repr(exc)
            stem = output / f"{label}-{moment}"
            try:
                page.screenshot(path=f"{stem}.png", full_page=False)
                Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
            except Exception as exc:
                snapshot["capture_error"] = repr(exc)
            result[moment] = snapshot
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path, default=Path("diagnostics") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--headed", action="store_true", help="also run a visible Chrome (run under xvfb-run on a server)")
    args = parser.parse_args()
    config = load_config(args.config)
    args.output.mkdir(parents=True, exist_ok=True)
    homepage = str(config["browser"].get("homepage_url", "https://www.gamer.com.tw/"))
    thread = str(config["thread"]["url"])

    report: dict[str, Any] = {"time": datetime.now().astimezone().isoformat(), "headless": config["browser"].get("headless", True)}
    report["session_file"] = session_summary(str(config["browser"].get("storage_state", "")).strip())
    report["plain_http"] = [plain_http(homepage), plain_http(thread)]
    report["browser"] = []
    modes = [bool(config["browser"].get("headless", True))]
    if args.headed and False not in modes:
        modes.append(False)
    for headless in modes:
        config["browser"]["headless"] = headless
        tag = "headless" if headless else "headed"
        for label, url, use_session in (
            (f"{tag}-thread-with-session", thread, True),
            (f"{tag}-homepage-no-session", homepage, False),
        ):
            print(f"Visiting {label} ...", flush=True)
            try:
                report["browser"].append(browser_visit(config, url, label, args.output, use_session=use_session))
            except Exception as exc:
                report["browser"].append({"label": label, "error": repr(exc)})
            time.sleep(5)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    (args.output / "report.json").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved report, screenshots and HTML to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
