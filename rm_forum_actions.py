#!/usr/bin/env python3
# coding: utf-8
"""
RoboMaster 论坛新文章监控 - GitHub Actions 版
每次运行抓取论坛文章列表，与上次记录的 ID 对比，发现新文章则推送飞书

监控页面: https://bbs.robomaster.com/article
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

import requests

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
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=True)


# ============================================================
# 配置 (从环境变量读取)
# ============================================================
WEBHOOKS = os.environ.get("FORUM_FEISHU_WEBHOOKS", "").split(",")
WEBHOOKS = [w.strip() for w in WEBHOOKS if w.strip()]

FORUM_URL = "https://bbs.robomaster.com/article"

# ============================================================
# 论坛文章列表解析
# ============================================================
ARTICLE_PATTERN = re.compile(
    r'<a href="/article/(\d+)\?source=1"[^>]*class="articleItem"[^>]*>.*?'
    r'<div class="articleItem__title"[^>]*>(.*?)</div>',
    re.DOTALL,
)

TAG_CLEAN = re.compile(r"<[^>]+>")


def fetch_articles() -> list[dict]:
    """抓取论坛文章列表，返回 [{id, title, url}, ...]"""
    try:
        resp = requests.get(
            FORUM_URL,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
    except requests.RequestException as e:
        logger.error("请求论坛失败: %s", e)
        return []

    if resp.status_code != 200:
        logger.error("论坛返回状态码 %d", resp.status_code)
        return []

    articles = []
    for match in ARTICLE_PATTERN.finditer(resp.text):
        aid = int(match.group(1))
        title = TAG_CLEAN.sub("", match.group(2)).strip()
        if not title:
            continue
        articles.append({
            "id": aid,
            "title": title,
            "url": f"https://bbs.robomaster.com/article/{aid}?source=1",
        })

    logger.info("共抓取到 %d 篇文章", len(articles))
    return articles


# ============================================================
# 飞书 Webhook 发送
# ============================================================
def send_feishu_post(title: str, content_lines: list[list[dict]]):
    """发送飞书富文本消息到所有 webhook"""
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_lines,
                }
            }
        },
    }
    for url in WEBHOOKS:
        try:
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("code", 0) == 0:
                logger.info("消息发送成功")
            else:
                logger.error("飞书错误: %s", result.get("msg", "unknown"))
        except Exception as e:
            logger.error("发送失败: %s", e)


# ============================================================
# 主逻辑
# ============================================================
def main():
    if not WEBHOOKS:
        logger.error("未配置 FORUM_FEISHU_WEBHOOKS 环境变量")
        sys.exit(1)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    articles = fetch_articles()
    if not articles:
        logger.info("未抓取到文章，退出")
        return

    current_ids = {a["id"] for a in articles}

    # 首次运行：记录所有当前文章 ID，不发送通知
    if not seen_ids:
        state["seen_ids"] = sorted(current_ids)
        save_state(state)
        logger.info("首次运行，已记录 %d 篇文章作为基准，不发送通知", len(articles))
        print("::set-output name=state_changed::true")
        return

    # 找出新文章（当前有但之前没见过的）
    new_articles = [a for a in articles if a["id"] not in seen_ids]

    if not new_articles:
        logger.info("没有新文章")
        # 仍更新状态（清理过期 ID，保留最近的）
        state["seen_ids"] = sorted(seen_ids & current_ids)
        if state["seen_ids"] != sorted(seen_ids):
            save_state(state)
            print("::set-output name=state_changed::true")
        return

    # 按 ID 从小到大排序（保证旧的先发）
    new_articles.sort(key=lambda x: x["id"])
    logger.info("发现 %d 篇新文章", len(new_articles))

    for art in new_articles:
        logger.info("  新文章: [%d] %s", art["id"], art["title"])
        content_lines = [[
            {"tag": "text", "text": f"[新文章] {art['title']}\n"},
            {"tag": "a", "text": "点击查看详情", "href": art["url"]},
        ]]
        send_feishu_post("RoboMaster 论坛新文章", content_lines)

    # 更新状态
    state["seen_ids"] = sorted(seen_ids | current_ids)
    save_state(state)
    logger.info("状态已更新，共记录 %d 篇文章", len(state["seen_ids"]))
    print("::set-output name=state_changed::true")


if __name__ == "__main__":
    main()
