#!/usr/bin/env python3
"""Interactively log in once and export a Playwright storage_state JSON."""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://user.gamer.com.tw/login.php")
    parser.add_argument("--output", type=Path, default=Path("bahamut-session.json"))
    parser.add_argument("--channel", default="chrome", help="installed browser channel; defaults to Google Chrome Stable")
    parser.add_argument("--executable-path", help="explicit path to an installed Chrome executable")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        launch_options = {"headless": False}
        if args.executable_path:
            launch_options["executable_path"] = args.executable_path
        else:
            launch_options["channel"] = args.channel
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        print("在開啟的瀏覽器中完成巴哈登入與必要的站方驗證。完成後回到終端機按 Enter。")
        input()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(args.output))
        browser.close()
    args.output.chmod(0o600)
    print(f"已輸出 {args.output}（請用安全方式傳到服務主機，勿提交 Git）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
