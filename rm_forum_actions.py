#!/usr/bin/env python3
# coding: utf-8
"""
RoboMaster 论坛新文章监控 - GitHub Actions 版
每次运行抓取论坛文章列表，与上次记录的 ID 对比，发现新文章则推送飞书

监控页面: https://bbs.robomaster.com/article

v2 变更：
1. 增加发布时间过滤：只推送 MAX_AGE_HOURS 小时内发布的文章，
   避免论坛列表里旧文章重新出现（置顶/审核/编辑）时被当成新文章推送
2. 不再把 seen_ids 裁剪成“当前列表页的文章”，避免旧文章掉出列表后
   再次出现时被重复推送
3. 用 GITHUB_OUTPUT 替代已弃用的 ::set-output
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
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

MAX_SEEN_IDS = 1000  # seen_ids 保留上限


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_STATE.copy()


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=True)


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

# 只推送该时间窗口内发布的文章（小时）
MAX_AGE_HOURS = float(os.environ.get("FORUM_MAX_AGE_HOURS", "48"))

DRY_RUN = "--dry-run" in sys.argv

# ============================================================
# 论坛文章列表解析
# ============================================================
ITEM_PATTERN = re.compile(
    r'<a href="/article/(\d+)\?source=1"[^>]*class="articleItem"[^>]*>([\s\S]*?)</a>'
)
TITLE_PATTERN = re.compile(r'articleItem__title"[^>]*>([\s\S]*?)</div>')
DATETIME_PATTERN = re.compile(
    r'articleItem__datetime[\s\S]*?articleItem__number"[^>]*>\s*([\s\S]*?)\s*</div>'
)
TAG_CLEAN = re.compile(r"<[^>]+>")

# 相对时间：X分钟前 / X小时前 / 昨天 / X天前 / X周前 / X个月前
REL_PATTERN = re.compile(r"(\d+)\s*(分钟|小时|天|周|个月|月)前")
ABS_PATTERN = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def parse_age_hours(text: str) -> float | None:
    """把列表页的时间文案转换成“距今多少小时”，无法解析返回 None"""
    if not text:
        return None
    text = text.strip()

    if "刚刚" in text or "秒前" in text:
        return 0.0
    if "昨天" in text:
        return 24.0

    m = REL_PATTERN.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        factor = {"分钟": 1 / 60, "小时": 1, "天": 24, "周": 168, "个月": 720, "月": 720}[unit]
        return n * factor

    m = ABS_PATTERN.search(text)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (datetime.now() - d).total_seconds() / 3600
        except ValueError:
            return None

    return None


def fetch_articles() -> list[dict]:
    """抓取论坛文章列表，返回 [{id, title, url, age_hours}, ...]"""
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
    for match in ITEM_PATTERN.finditer(resp.text):
        aid = int(match.group(1))
        block = match.group(2)

        title_match = TITLE_PATTERN.search(block)
        if not title_match:
            continue
        title = TAG_CLEAN.sub("", title_match.group(1)).strip()
        if not title:
            continue

        dt_match = DATETIME_PATTERN.search(block)
        age_hours = parse_age_hours(TAG_CLEAN.sub("", dt_match.group(1)) if dt_match else "")

        articles.append({
            "id": aid,
            "title": title,
            "url": f"https://bbs.robomaster.com/article/{aid}?source=1",
            "age_hours": age_hours,
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
    if not WEBHOOKS and not DRY_RUN:
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
        set_output("state_changed", "true")
        return

    # 找出新出现的文章
    fresh = [a for a in articles if a["id"] not in seen_ids]

    # 再按发布时间过滤：太旧的文章（置顶/审核通过/被编辑后重新出现在列表）不推送
    new_articles = []
    for art in fresh:
        age = art["age_hours"]
        if age is None:
            logger.warning("文章 [%d] 无法解析发布时间，跳过: %s", art["id"], art["title"])
        elif age > MAX_AGE_HOURS:
            logger.info("跳过过期文章 [%d]（约 %.0f 小时前发布）: %s",
                        art["id"], age, art["title"])
        else:
            new_articles.append(art)

    if not new_articles:
        if fresh:
            logger.info("有 %d 篇新出现但已过期的文章，已加入已见列表不再推送", len(fresh))
        else:
            logger.info("没有新文章")

        # 只把本次看到的 ID 并入已见列表，不做裁剪（避免旧文章重复推送）
        state["seen_ids"] = sorted(seen_ids | current_ids)
        if state["seen_ids"] != sorted(seen_ids):
            trim_seen_ids(state)
            save_state(state)
            set_output("state_changed", "true")
        return

    # 按发布时间从旧到新排序，保证推送顺序
    new_articles.sort(key=lambda x: (x["age_hours"] or 0), reverse=True)
    logger.info("发现 %d 篇新文章", len(new_articles))

    for art in new_articles:
        logger.info("  新文章: [%d] %s", art["id"], art["title"])
        content_lines = [[
            {"tag": "text", "text": f"[新文章] {art['title']}\n"},
            {"tag": "a", "text": "点击查看详情", "href": art["url"]},
        ]]
        if DRY_RUN:
            logger.info("  [dry-run] 不实际发送")
        else:
            send_feishu_post("RoboMaster 论坛新文章", content_lines)

    # 更新状态
    state["seen_ids"] = sorted(seen_ids | current_ids)
    trim_seen_ids(state)
    save_state(state)
    logger.info("状态已更新，共记录 %d 篇文章", len(state["seen_ids"]))
    set_output("state_changed", "true")


def trim_seen_ids(state: dict):
    """限制 seen_ids 长度，保留 ID 较大的（较新的）文章"""
    if len(state["seen_ids"]) > MAX_SEEN_IDS:
        state["seen_ids"] = sorted(state["seen_ids"])[-MAX_SEEN_IDS:]


if __name__ == "__main__":
    main()
