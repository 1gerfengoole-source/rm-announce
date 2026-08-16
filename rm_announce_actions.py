#!/usr/bin/env python3
# coding: utf-8
"""
RoboMaster 官方公告监控 - GitHub Actions 版
每次运行检查一次，状态保存在 state.json 中并提交回仓库

灵感来源: https://github.com/scutrobotlab/RMAnnounce
"""

import hashlib
import json
import logging
import os
import sys
from html.parser import HTMLParser
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
logger = logging.getLogger("RMAnnounce")

# ============================================================
# 状态管理 (state.json)
# ============================================================
STATE_PATH = Path(__file__).parent / "state.json"

DEFAULT_STATE = {
    "last_id": 0,
    "monitored_pages": {},  # {page_id: hash}
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
WEBHOOKS = os.environ.get("FEISHU_WEBHOOKS", "").split(",")
WEBHOOKS = [w.strip() for w in WEBHOOKS if w.strip()]

LAST_ID_INIT = int(os.environ.get("LAST_ID_INIT", "0"))
MONITORED_PAGES = [
    int(x.strip())
    for x in os.environ.get("MONITORED_PAGES", "").split(",")
    if x.strip()
]

# ============================================================
# RoboMaster 公告页解析
# ============================================================
BASE_URL = "https://www.robomaster.com/zh-CN/resource/pages/announcement/{}"


class AnnounceParser(HTMLParser):
    """解析公告页 HTML，提取标题和正文"""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.content_text = ""
        self._in_title = False
        self._in_content = False
        self._content_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        if tag == "p" and "main-title" in cls:
            self._in_title = True
        elif tag == "div" and "main-context" in cls:
            self._in_content = True
            self._content_depth = 1
        elif self._in_content:
            self._content_depth += 1

    def handle_endtag(self, tag):
        if self._in_title and tag == "p":
            self._in_title = False
        if self._in_content:
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._in_content = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._in_content:
            self.content_text += data.strip()


def fetch_announce_page(announce_id: int) -> dict | None:
    """拉取公告页，返回 {title, url, content_hash, content_empty} 或 None"""
    url = BASE_URL.format(announce_id)
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (RMAnnounce Bot)"
        })
    except requests.RequestException as e:
        logger.error("请求公告 %d 失败: %s", announce_id, e)
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        logger.error("公告 %d 状态码 %d", announce_id, resp.status_code)
        return None

    body = resp.text
    if "您访问的页面不存在" in body:
        return None

    parser = AnnounceParser()
    parser.feed(body)

    if not parser.title:
        parser.title = f"公告 #{announce_id}"

    content_hash = hashlib.sha256(parser.content_text.encode()).hexdigest()
    return {
        "title": parser.title,
        "url": url,
        "content_hash": content_hash,
        "content_empty": len(parser.content_text.strip()) == 0,
    }


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


def send_feishu_text(text: str):
    """发送纯文本消息"""
    payload = {"msg_type": "text", "content": {"text": text}}
    for url in WEBHOOKS:
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error("发送失败: %s", e)


# ============================================================
# 主逻辑
# ============================================================
def main():
    if not WEBHOOKS:
        logger.error("未配置 FEISHU_WEBHOOKS 环境变量")
        sys.exit(1)

    state = load_state()

    # 首次运行：用环境变量初始化
    if state["last_id"] == 0 and LAST_ID_INIT > 0:
        state["last_id"] = LAST_ID_INIT
        logger.info("初始化 last_id = %d", LAST_ID_INIT)

    state_changed = False
    new_announcements = []

    # --------------------------------------------------------
    # 任务 1: 增量拉取新公告 (检查 last_id+1, last_id+2, ...)
    # --------------------------------------------------------
    if state["last_id"] > 0:
        check_id = state["last_id"] + 1
        consecutive_404 = 0
        max_check = 10  # 单次最多向前检查 10 个 ID，防止漏掉批量发布

        while consecutive_404 < 3 and max_check > 0:
            result = fetch_announce_page(check_id)
            if result is None:
                consecutive_404 += 1
                logger.info("公告 %d 暂未发布 (404)", check_id)
                check_id += 1
                max_check -= 1
                continue

            # 发现新公告
            logger.info("发现新公告: %d - %s", check_id, result["title"])
            state["last_id"] = check_id
            state_changed = True

            prefix = "[空白] " if result["content_empty"] else "[新增] "
            content_lines = [[
                {"tag": "text", "text": f"{prefix}{result['title']}\n"},
                {"tag": "a", "text": "点击查看详情", "href": result["url"]},
            ]]
            new_announcements.append((check_id, content_lines))

            check_id += 1
            max_check -= 1
            consecutive_404 = 0  # 重置，继续检查下一个

    # --------------------------------------------------------
    # 任务 2: 监控已知页面内容变化
    # --------------------------------------------------------
    updated_pages = []
    for page_id in MONITORED_PAGES:
        result = fetch_announce_page(page_id)
        if result is None:
            logger.warning("监控页面 %d 无法访问", page_id)
            continue

        old_hash = state["monitored_pages"].get(str(page_id), "")
        new_hash = result["content_hash"]

        if not old_hash:
            state["monitored_pages"][str(page_id)] = new_hash
            state_changed = True
            logger.info("页面 %d 初始化 hash", page_id)
            continue

        if new_hash != old_hash:
            state["monitored_pages"][str(page_id)] = new_hash
            state_changed = True
            logger.info("页面 %d 内容有更新", page_id)
            content_lines = [[
                {"tag": "text", "text": f"[更新] {result['title']}\n"},
                {"tag": "a", "text": "点击查看详情", "href": result["url"]},
            ]]
            updated_pages.append(content_lines)

    # --------------------------------------------------------
    # 发送消息
    # --------------------------------------------------------
    for announce_id, content_lines in new_announcements:
        send_feishu_post("RoboMaster 资料站新公告", content_lines)

    for content_lines in updated_pages:
        send_feishu_post("RoboMaster 资料站公告更新", content_lines)

    # --------------------------------------------------------
    # 保存状态
    # --------------------------------------------------------
    if state_changed:
        save_state(state)
        logger.info("状态已更新并保存")
    else:
        logger.info("无新公告，无更新")

    # 输出标记，供 GitHub Actions 判断是否需要提交
    if state_changed:
        print("::set-output name=state_changed::true")


if __name__ == "__main__":
    main()
