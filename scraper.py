"""Nordic Semiconductor news monitor.

Scrapes recent press releases from nordicsemi.com, filters to the last N days,
translates titles + summaries to Chinese, and emails the digest via SMTP.

All secrets are read from environment variables:
  SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL  (required)
  LOOKBACK_DAYS                                    (optional, default 14)
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

BASE_URL = "https://www.nordicsemi.com"
NEWS_BASE = f"{BASE_URL}/Nordic-news"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Match article URLs like /Nordic-news/2026/03/Nordic-accelerates-...
ARTICLE_PATH_RE = re.compile(r"^/Nordic-news/(\d{4})/(\d{2})/([^/?#]+)/?$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nordic-monitor")


@dataclass
class Article:
    url: str
    title_en: str
    title_zh: str
    summary_en: str
    summary_zh: str
    published: datetime


def http_get(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def listing_urls_for_window(today: datetime, lookback_days: int) -> list[str]:
    earliest = today - timedelta(days=lookback_days)
    years = {today.year, earliest.year}
    return [f"{NEWS_BASE}/{year}" for year in sorted(years, reverse=True)]


def discover_article_urls(listing_html: str) -> set[str]:
    soup = BeautifulSoup(listing_html, "html.parser")
    found: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        path = href if href.startswith("/") else _strip_origin(href)
        if path and ARTICLE_PATH_RE.match(path):
            found.add(urljoin(BASE_URL, path).rstrip("/"))
    return found


def _strip_origin(href: str) -> str:
    if href.startswith(BASE_URL):
        return href[len(BASE_URL):]
    return ""


def parse_iso_date(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    # Python <3.11 doesn't accept trailing "Z"
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_article_metadata(html: str, url: str) -> Article | None:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    summary = ""
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    meta_desc = soup.find("meta", attrs={"name": "description"})
    for tag in (og_desc, meta_desc):
        if tag and tag.get("content"):
            summary = tag["content"].strip()
            break

    published: datetime | None = None
    meta_pub = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_pub and meta_pub.get("content"):
        published = parse_iso_date(meta_pub["content"])
    if not published:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            published = parse_iso_date(time_tag["datetime"])

    if not title or not published:
        log.warning("Skipping %s — missing title or published date", url)
        return None

    return Article(
        url=url,
        title_en=title,
        title_zh="",
        summary_en=summary,
        summary_zh="",
        published=published,
    )


def translate_text(translator: GoogleTranslator, text: str) -> str:
    if not text:
        return ""
    try:
        result = translator.translate(text)
        return result or text
    except Exception as exc:
        log.warning("Translation failed (%s); keeping original text", exc)
        return text


def collect_recent_articles(lookback_days: int) -> list[Article]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    article_urls: set[str] = set()
    for listing_url in listing_urls_for_window(now, lookback_days):
        log.info("Fetching listing: %s", listing_url)
        try:
            html = http_get(listing_url)
        except requests.RequestException as exc:
            log.warning("Listing %s failed: %s", listing_url, exc)
            continue
        urls = discover_article_urls(html)
        log.info("  found %d article links", len(urls))
        article_urls.update(urls)

    if not article_urls:
        raise RuntimeError(
            "No article links found on Nordic news listing pages — "
            "the page structure may have changed."
        )

    # Quick filter by year/month from URL before fetching full pages.
    candidate_urls = []
    for url in article_urls:
        path = url[len(BASE_URL):] if url.startswith(BASE_URL) else url
        m = ARTICLE_PATH_RE.match(path)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        # Allow articles whose (year, month) overlap the lookback window.
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        # last day of that month — add 32 days then snap to first of next month
        next_month = month_start + timedelta(days=32)
        month_end = datetime(next_month.year, next_month.month, 1, tzinfo=timezone.utc)
        if month_end < cutoff:
            continue
        if month_start > now:
            continue
        candidate_urls.append(url)

    log.info("Inspecting %d candidate articles", len(candidate_urls))

    translator = GoogleTranslator(source="auto", target="zh-CN")
    articles: list[Article] = []
    for url in sorted(candidate_urls):
        try:
            html = http_get(url)
        except requests.RequestException as exc:
            log.warning("Article %s failed: %s", url, exc)
            continue

        article = extract_article_metadata(html, url)
        if not article:
            continue
        if article.published < cutoff:
            continue

        article.title_zh = translate_text(translator, article.title_en)
        article.summary_zh = translate_text(translator, article.summary_en)
        articles.append(article)
        log.info(
            "Kept (%s): %s",
            article.published.date().isoformat(),
            article.title_en[:80],
        )

    articles.sort(key=lambda a: a.published, reverse=True)
    return articles


def render_html(articles: list[Article], lookback_days: int) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not articles:
        body = (
            f"<p>过去 {lookback_days} 天内（截至 {today_str} UTC），"
            "Nordic Semiconductor 官网未发布新闻或新产品动态。</p>"
        )
    else:
        rows = []
        for art in articles:
            date_str = art.published.strftime("%Y-%m-%d")
            rows.append(
                f"""
                <div style="margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #eee;">
                  <div style="color:#888;font-size:12px;">{escape(date_str)}</div>
                  <h3 style="margin:4px 0 6px;font-size:16px;">
                    <a href="{escape(art.url)}" style="color:#0066cc;text-decoration:none;">
                      {escape(art.title_zh)}
                    </a>
                  </h3>
                  <div style="color:#666;font-size:13px;margin-bottom:6px;">
                    原文：{escape(art.title_en)}
                  </div>
                  <p style="margin:6px 0;font-size:14px;line-height:1.5;">
                    {escape(art.summary_zh) or "（无摘要）"}
                  </p>
                  <div style="font-size:12px;">
                    <a href="{escape(art.url)}" style="color:#0066cc;">查看原文 →</a>
                  </div>
                </div>
                """
            )
        body = (
            f"<p>过去 {lookback_days} 天内 Nordic Semiconductor 共发布 "
            f"<b>{len(articles)}</b> 条动态（截至 {today_str} UTC）：</p>"
            + "".join(rows)
        )

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                   max-width:680px;margin:0 auto;padding:16px;color:#222;">
<h2 style="margin-top:0;">Nordic Semiconductor 新动态简报</h2>
{body}
<hr style="border:none;border-top:1px solid #eee;margin-top:32px;">
<p style="font-size:12px;color:#999;">
  自动生成 · 数据来源 <a href="{NEWS_BASE}">{NEWS_BASE}</a>
</p>
</body></html>"""


def render_plain(articles: list[Article], lookback_days: int) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not articles:
        return (
            f"过去 {lookback_days} 天内（截至 {today_str} UTC），"
            "Nordic Semiconductor 官网未发布新闻或新产品动态。\n"
        )
    lines = [
        f"过去 {lookback_days} 天内 Nordic Semiconductor 共发布 "
        f"{len(articles)} 条动态（截至 {today_str} UTC）：\n"
    ]
    for art in articles:
        lines.append(f"[{art.published.strftime('%Y-%m-%d')}] {art.title_zh}")
        lines.append(f"  原文标题：{art.title_en}")
        if art.summary_zh:
            lines.append(f"  摘要：{art.summary_zh}")
        lines.append(f"  链接：{art.url}\n")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, plain_body: str) -> None:
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_PASSWORD"]
    recipients = [
        addr.strip()
        for addr in os.environ["RECIPIENT_EMAIL"].split(",")
        if addr.strip()
    ]
    if not recipients:
        raise RuntimeError("RECIPIENT_EMAIL is empty")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Nordic Monitor", sender))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("Sending email to %s via %s:%d", recipients, SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, msg.as_string())
    log.info("Email sent.")


def send_alert(reason: str) -> None:
    # Best-effort alert; swallow secondary failures so we still exit non-zero.
    try:
        subject = "[Nordic Monitor] 抓取失败告警"
        body = (
            "Nordic Semiconductor 监控任务执行失败。\n\n"
            f"原因：\n{reason}\n\n"
            "请检查 GitHub Actions 日志或网站结构是否变化。\n"
        )
        html = f"<pre style='font-family:monospace;'>{escape(body)}</pre>"
        send_email(subject, html, body)
    except Exception as exc:
        log.error("Failed to send alert email: %s", exc)


def main() -> int:
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "14"))

    for required in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL"):
        if not os.environ.get(required):
            log.error("Missing required env var: %s", required)
            return 2

    try:
        articles = collect_recent_articles(lookback_days)
    except Exception:
        tb = traceback.format_exc()
        log.error("Scrape failed:\n%s", tb)
        send_alert(tb)
        return 1

    subject = (
        f"Nordic 新动态（{len(articles)} 条）"
        if articles
        else f"Nordic 新动态（过去 {lookback_days} 天无更新）"
    )
    html_body = render_html(articles, lookback_days)
    plain_body = render_plain(articles, lookback_days)

    try:
        send_email(subject, html_body, plain_body)
    except Exception as exc:
        log.error("Email send failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
