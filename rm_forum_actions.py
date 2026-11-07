#!/usr/bin/env python3
# coding: utf-8
"""Monitor newly discovered forum articles; persist pending deliveries until acknowledged."""

import argparse
import hashlib
import html
import time
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("RMForum")

# ============================================================
# 状态管理 (forum_state.json)
# ============================================================
STATE_PATH = Path(__file__).parent / "forum_state.json"

DEFAULT_STATE = {
    "seen_ids": [],  # 已见过的文章 ID 列表
}




def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_STATE.copy()


def save_state(state: dict):
    temporary = STATE_PATH.with_suffix(".json.tmp")
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=True)
    temporary.replace(STATE_PATH)


def set_output(name: str, value: str):
    """向 GITHUB_OUTPUT 写入 step output，兼容本地运行"""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    logger.info("output %s=%s", name, value)


# ============================================================
# 配置 (从环境变量读取)
# ============================================================
WEBHOOKS = os.environ.get("FORUM_FEISHU_WEBHOOKS", "").split(",")
WEBHOOKS = [w.strip() for w in WEBHOOKS if w.strip()]

FORUM_URL = "https://bbs.robomaster.com/article"

def parse_articles(document: str) -> list[dict]:
    soup = BeautifulSoup(document, "html.parser")
    articles = {}
    for item in soup.select("a.articleItem"):
        match = re.search(r"/article/(\d+)", item.get("href", ""))
        title_node = item.select_one(".articleItem__title")
        if not match or not title_node:
            continue
        aid = int(match.group(1))
        title = html.unescape(title_node.get_text(" ", strip=True))
        if title:
            articles[aid] = {"id": aid, "title": title,
                             "url": f"https://bbs.robomaster.com/article/{aid}?source=1"}
    if not articles:
        raise RuntimeError("Forum returned no parseable articles; refusing to advance state")
    return list(articles.values())


def fetch_articles() -> list[dict]:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"])))
    response = session.get(FORUM_URL, timeout=30, headers={
        "User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    response.raise_for_status()
    response.encoding = "utf-8"
    articles = parse_articles(response.text)
    logger.info("Fetched %d articles", len(articles))
    return articles


def webhook_key(url: str) -> str:
    # Never persist webhook credentials in a public repository.
    return hashlib.sha256(url.encode()).hexdigest()


def send_feishu_post(url: str, article: dict) -> bool:
    payload = {"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": "RoboMaster 论坛新文章",
        "content": [[
            {"tag": "text", "text": ("[历史补发] " if article.get("backfill") else "[新文章] ") + article["title"] + "\n"},
            {"tag": "a", "text": "点击查看详情", "href": article["url"]}
        ]]
    }}}}
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
        # Missing code must not be treated as success.
        if not isinstance(result, dict):
            logger.error("Unexpected Feishu response for article %s", article["id"])
            return False
        if result.get("code", result.get("StatusCode")) == 0:
            return True
        logger.error("Feishu rejected article %s (code=%s)",
                     article["id"], result.get("code", result.get("StatusCode", "missing")))
    except (requests.RequestException, ValueError):
        # Exception messages can contain the secret webhook URL.
        logger.error("Feishu delivery failed for article %s", article["id"])
    return False


def process_articles(articles, state, webhooks, *, dry_run=False, backfill=None):
    state = json.loads(json.dumps(state))
    seen = set(state.get("seen_ids", []))
    pending = state.setdefault("pending", {})
    recovered = set(state.get("backfilled_ids", []))
    baseline = not seen and not state.get("initialized")
    if baseline:
        seen.update(a["id"] for a in articles)
        logger.info("Initialized baseline with %d articles", len(articles))
    else:
        for article in articles:
            if article["id"] not in seen:
                pending.setdefault(str(article["id"]), {**article, "delivered_to": []})
    for article in backfill or []:
        if article["id"] not in recovered:
            pending.setdefault(str(article["id"]), {**article, "backfill": True, "delivered_to": []})
    failures = 0
    for aid, article in list(pending.items()):
        if dry_run:
            logger.info("[dry-run] %s %s", aid, article["title"])
            continue
        delivered = set(article.get("delivered_to", []))
        for url in dict.fromkeys(webhooks):
            key = webhook_key(url)
            if key in delivered:
                continue
            if send_feishu_post(url, article):
                delivered.add(key)
                article["delivered_to"] = sorted(delivered)
            else:
                failures += 1
            time.sleep(0.4)
        if webhooks and all(webhook_key(url) in delivered for url in webhooks):
            seen.add(article["id"])
            if article.get("backfill"):
                recovered.add(article["id"])
            del pending[aid]
    state.update(seen_ids=sorted(seen), initialized=True,
                 backfilled_ids=sorted(recovered), schema_version=3)
    return state, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-confirmed", action="store_true",
                        help="Replay only the 16 articles proven skipped in the 2026-11-04 log")
    args = parser.parse_args()
    if not WEBHOOKS and not args.dry_run:
        raise RuntimeError("FORUM_FEISHU_WEBHOOKS is not configured")
    state = load_state()
    articles = fetch_articles()
    backfill = None
    if args.backfill_confirmed:
        backfill = json.loads((Path(__file__).parent / "forum_backfill_20261104.json").read_text(encoding="utf-8"))
    updated, failures = process_articles(articles, state, WEBHOOKS,
                                        dry_run=args.dry_run, backfill=backfill)
    if args.dry_run:
        logger.info("Dry run finished; no messages sent and no state written")
        return
    if updated != state:
        save_state(updated)
        set_output("state_changed", "true")
    if failures:
        raise RuntimeError(f"{failures} deliveries failed; pending articles retained for retry")


if __name__ == "__main__":
    main()
