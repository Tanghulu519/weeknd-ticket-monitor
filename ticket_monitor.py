#!/usr/bin/env python3
"""Monitor HK Ticketing pages and push an alert to WeChat via ServerChan.

The monitor is intentionally dependency-free so it can run in GitHub Actions.
It detects common on-sale markers in HTML/JSON, persists the previous state,
and only sends a release notification on a transition to available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import sys
import time
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# Phrases commonly used by HK Ticketing and localized event pages.
POSITIVE_PATTERNS = re.compile(
    r"""(?ix)
    \b(?:buy|purchase|book|get)\s+(?:your\s+)?tickets?\b
    | \b(?:on[\s-]?sale|tickets?\s+available|available\s+now)\b
    | \u7acb\u5373 (?:\u8d2d\u4e70|\u8d2d\u7968|\u9884\u8ba2|\u8ba2\u7968)
    | \u9a6c\u4e0a (?:\u8d2d\u4e70|\u8d2d\u7968|\u9884\u8ba2|\u8ba2\u7968)
    | \u73b0\u5728 (?:\u8d2d\u4e70|\u8d2d\u7968|\u9884\u8ba2|\u8ba2\u7968)
    | \u516c\u5f00\u53d1\u552e | \u6b63\u5f0f\u5f00\u552e | \u73b0\u5df2\u5f00\u552e | \u5f00\u59cb\u53d1\u552e | \u73b0\u5df2\u53d1\u552e
    | \u8d2d\u4e70\u95e8\u7968 | \u7acb\u5373\u62a2\u7968 | \u53ef\u8d2d\u7968 | \u53ef\u552e
    """
)

NEGATIVE_PATTERNS = re.compile(
    r"""(?ix)
    \bcoming\s+soon\b | \bnot\s+(?:yet\s+)?on\s+sale\b
    | \bsold\s+out\b | \bunavailable\b | \bnot\s+available\b
    | \u6682\u672a\u5f00\u552e | \u5c1a\u672a\u5f00\u552e | \u5373\u5c06\u5f00\u552e | \u656c\u8bf7\u671f\u5f85
    | \u552e\u7f44 | \u5df2\u552e\u5b8c | \u6682\u65f6\u552e\u7f44 | \u7f3a\u8d27 | \u65e0\u7968
    | \u6682\u65e0\u53ef\u552e | \u65e0\u53ef\u552e | \u6682\u65e0\u53ef\u552e\u7968 | \u65e0\u53ef\u552e\u7968
    | \u6682\u65e0\u53ef\u8d2d\u7968 | \u65e0\u53ef\u8d2d\u7968
    | \u9884\u7ea6\u767b\u8bb0 | \u767b\u8bb0\u63d0\u9192 | \u5f00\u552e\u63d0\u9192 | \u9884\u552e | \u5019\u8865
    """
)

STRUCTURED_POSITIVE = re.compile(
    r"""(?ix)
    ["']?(?:onSale|isAvailable|available|ticketStatus|salesStatus|status)
    ["']?\s*[:=]\s*["']?(?:true|on_sale|on-sale|available|sale|active|public_on_sale)
    ["']?
    """
)

STRUCTURED_NEGATIVE = re.compile(
    r"""(?ix)
    ["']?(?:onSale|isAvailable|available|ticketStatus|salesStatus|status)
    ["']?\s*[:=]\s*["']?(?:false|off_sale|off-sale|unavailable|sold_out|sold-out|coming_soon|coming-soon|inactive)
    ["']?
    """
)

BUTTON_TEXT = re.compile(
    r"""(?is)<(?:button|a)\b[^>]*>(.*?)</(?:button|a)>"""
)


@dataclass
class CheckResult:
    url: str
    status: str  # available, unavailable, or unknown
    reason: str
    content_hash: str
    fetched_at: str


@dataclass
class UrlState:
    status: str = "unknown"
    reason: str = ""
    content_hash: str = ""
    last_checked_at: str = ""
    last_notified_hash: str = ""
    last_notified_at: str = ""
    error_count: int = 0


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_urls(env_value: str, urls_file: Optional[Path]) -> List[str]:
    urls: List[str] = []
    if env_value.strip():
        urls.extend(env_value.splitlines())
    if urls_file and urls_file.exists():
        urls.extend(urls_file.read_text(encoding="utf-8").splitlines())

    result: List[str] = []
    seen = set()
    for raw in urls:
        url = raw.strip().lstrip("\ufeff")
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"URL 必须以 http:// 或 https:// 开头: {url}")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def decode_response(data: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "big5", "gb18030"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def find_browser() -> Optional[str]:
    configured = os.getenv("TICKET_BROWSER", "").strip()
    if configured:
        return configured
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    return next((path for path in candidates if path and Path(path).exists()), None)


def fetch_rendered_html(url: str, timeout: int = 30) -> str:
    """Render the HK Ticketing SPA and return its final DOM."""
    browser = find_browser()

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch_options = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            }
            if browser:
                launch_options["executable_path"] = browser
            rendered = playwright.chromium.launch(**launch_options)
            try:
                page = rendered.new_page(locale="zh-HK")
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(8000)
                return page.content()
            finally:
                rendered.close()
    except ImportError:
        pass
    except Exception:
        if os.getenv("TICKET_REQUIRE_PLAYWRIGHT", "").lower() in {"1", "true", "yes"}:
            raise

    if not browser:
        raise RuntimeError("No Edge/Chrome browser found; install Edge or set TICKET_BROWSER")

    command = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--disable-extensions",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--virtual-time-budget=8000",
        "--dump-dom",
        url,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Browser render failed with exit code {completed.returncode}: {stderr}")
    return completed.stdout.decode("utf-8", errors="replace")


def fetch_html(url: str, timeout: int = 25) -> str:
    if "hkt.hkticketing.com" in url or os.getenv("TICKET_USE_BROWSER", "").lower() in {"1", "true", "yes"}:
        return fetch_rendered_html(url, timeout=max(timeout, 30))

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-HK,zh-TW;q=0.9,zh-CN;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return decode_response(response.read(), response.headers.get("Content-Type", ""))


def normalize_text(html: str) -> str:
    # Entity-decoding and whitespace normalization make localized page text easier to match.
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


def detect_availability(html: str) -> tuple[str, str]:
    text = normalize_text(html)
    structured_positive = bool(STRUCTURED_POSITIVE.search(html))
    structured_negative = bool(STRUCTURED_NEGATIVE.search(html))
    button_text = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)) for m in BUTTON_TEXT.finditer(html))
    body = f"{text} {button_text}"

    positive = structured_positive or bool(POSITIVE_PATTERNS.search(body))
    negative = structured_negative or bool(NEGATIVE_PATTERNS.search(body))

    # Structured flags take precedence over free text, which often contains
    # future-sale copy alongside a disabled “Buy” button.
    if structured_positive and not structured_negative:
        return "available", "structured availability flag"
    if structured_negative and not structured_positive:
        return "unavailable", "structured unavailable flag"
    if negative:
        return "unavailable", "coming-soon/sold-out wording detected"
    if positive:
        return "available", "on-sale wording detected"
    if positive and negative:
        return "unknown", "mixed on-sale and unavailable wording"
    return "unknown", "no recognizable sale status"


def check_url(url: str) -> CheckResult:
    html = fetch_html(url)
    status, reason = detect_availability(html)
    digest = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
    return CheckResult(url=url, status=status, reason=reason, content_hash=digest, fetched_at=utc_now())


class Notifier:
    def __init__(self) -> None:
        self.serverchan_sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
        self.smtp_host = os.getenv("SMTP_HOST", "").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "587") or "587")
        self.smtp_user = os.getenv("SMTP_USER", "").strip()
        self.smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        self.email_to = os.getenv("EMAIL_TO", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.serverchan_sendkey or (self.smtp_host and self.smtp_user and self.smtp_password and self.email_to))

    def send_serverchan(self, title: str, description: str) -> None:
        if not self.serverchan_sendkey:
            raise RuntimeError("缺少 SERVERCHAN_SENDKEY")
        payload = urlencode({"title": title, "desp": description}).encode("utf-8")
        request = Request(
            f"https://sctapi.ftqq.com/{self.serverchan_sendkey}.send",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Server 酱返回无法解析: {body[:300]}") from exc
        if parsed.get("code") not in (0, 200):
            raise RuntimeError(f"Server 酱发送失败: {body[:300]}")

    def send_email(self, title: str, description: str) -> None:
        if not (self.smtp_host and self.smtp_user and self.smtp_password and self.email_to):
            raise RuntimeError("邮件通知配置不完整")
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.smtp_user
        message["To"] = self.email_to
        message.set_content(description)
        context = ssl.create_default_context()
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=25) as smtp:
            smtp.starttls(context=context)
            smtp.login(self.smtp_user, self.smtp_password)
            smtp.send_message(message)

    def send(self, title: str, description: str) -> List[str]:
        sent: List[str] = []
        errors: List[str] = []
        if self.serverchan_sendkey:
            try:
                self.send_serverchan(title, description)
                sent.append("Server 酱/微信")
            except Exception as exc:  # noqa: BLE001 - one channel must not block the backup
                errors.append(f"Server 酱: {exc}")
        if self.smtp_host and self.smtp_user and self.smtp_password and self.email_to:
            try:
                self.send_email(title, description)
                sent.append("邮件")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"邮件: {exc}")
        if not sent:
            raise RuntimeError("；".join(errors) if errors else "没有配置任何通知渠道")
        return sent


def load_state(path: Path) -> Dict[str, UrlState]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: Dict[str, UrlState] = {}
    for url, values in raw.get("urls", {}).items():
        result[url] = UrlState(**{k: v for k, v in values.items() if k in UrlState.__annotations__})
    return result


def save_state(path: Path, states: Dict[str, UrlState]) -> None:
    payload = {"version": 1, "updated_at": utc_now(), "urls": {url: asdict(state) for url, state in states.items()}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_release_message(result: CheckResult) -> tuple[str, str]:
    title = "🎫 Weeknd 香港站已放票"
    description = (
        f"检测到快达票页面已经进入可购票状态。\n\n"
        f"- 页面：[{result.url}]({result.url})\n"
        f"- 检测时间：{result.fetched_at}\n"
        f"- 判断依据：{result.reason}\n\n"
        f"请立即打开购票页并完成登录/选票。"
    )
    return title, description


def make_test_message() -> tuple[str, str]:
    return "✅ 盯票脚本测试成功", "这条消息来自 Weeknd 香港站盯票脚本，微信通知渠道已经配置成功。"


def should_notify(state: UrlState, result: CheckResult, cooldown_minutes: int) -> bool:
    if result.status != "available":
        return False
    if state.status != "available":
        return True
    if state.last_notified_hash == result.content_hash:
        return False
    if state.last_notified_at:
        notified = dt.datetime.fromisoformat(state.last_notified_at.replace("Z", "+00:00"))
        age_minutes = (dt.datetime.now(dt.timezone.utc) - notified).total_seconds() / 60
        return age_minutes >= cooldown_minutes
    return True


def run_monitor(urls: Iterable[str], state_file: Path, cooldown_minutes: int, notifier: Notifier) -> int:
    states = load_state(state_file)
    failures = 0
    for url in urls:
        state = states.setdefault(url, UrlState())
        try:
            result = check_url(url)
        except Exception as exc:  # noqa: BLE001 - keep the cloud loop alive on transient failures
            state.error_count += 1
            state.last_checked_at = utc_now()
            print(f"[ERROR] {url}: {exc}", file=sys.stderr)
            failures += 1
            continue

        print(f"[{result.fetched_at}] {url} -> {result.status} ({result.reason})")
        if should_notify(state, result, cooldown_minutes):
            title, description = make_release_message(result)
            channels = notifier.send(title, description)
            state.last_notified_hash = result.content_hash
            state.last_notified_at = utc_now()
            print(f"[NOTIFY] {', '.join(channels)}")

        state.status = result.status
        state.reason = result.reason
        state.content_hash = result.content_hash
        state.last_checked_at = result.fetched_at
        state.error_count = 0

    save_state(state_file, states)
    return 1 if failures == len(list(urls)) and failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="快达票 / HK Ticketing 盯票并推送到微信")
    parser.add_argument("--urls-file", type=Path, default=Path("ticket_urls.txt"))
    parser.add_argument("--state-file", type=Path, default=Path("ticket-monitor-state.json"))
    parser.add_argument("--cooldown-minutes", type=int, default=int(os.getenv("COOLDOWN_MINUTES", "360")))
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.getenv("TICKET_INTERVAL_SECONDS", "0")),
        help="interval in seconds for continuous mode; 0 runs once",
    )
    parser.add_argument("--test-notification", action="store_true", help="send one test notification and exit")
    args = parser.parse_args()

    notifier = Notifier()
    if not notifier.configured:
        print(
            "错误：请先配置 SERVERCHAN_SENDKEY（推荐），或配置 SMTP_HOST/SMTP_USER/SMTP_PASSWORD/EMAIL_TO。",
            file=sys.stderr,
        )
        return 2

    if args.test_notification:
        title, description = make_test_message()
        channels = notifier.send(title, description)
        print(f"测试通知已发送：{', '.join(channels)}")
        return 0

    urls = parse_urls(os.getenv("TICKET_URLS", ""), args.urls_file)
    if not urls:
        print("错误：没有找到监控 URL。请填写 ticket_urls.txt，或设置 TICKET_URLS 环境变量。", file=sys.stderr)
        return 2

    interval = max(args.interval_seconds, 0)
    if not interval:
        return run_monitor(urls, args.state_file, max(args.cooldown_minutes, 0), notifier)

    while True:
        started = time.monotonic()
        code = run_monitor(urls, args.state_file, max(args.cooldown_minutes, 0), notifier)
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
