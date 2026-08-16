"""X (Twitter) API 客户端：搜索 Polymarket 聪明钱讨论。

用 app-only Bearer Token 调 /2/tweets/search/recent 搜索推文，
提取提到的@用户名，用于发现被社区推荐的 Polymarket 交易者。
"""
import base64
import logging
import time
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

API = "https://api.twitter.com/2"


class XClient:
    def __init__(self, bearer_token: str):
        self.bearer = bearer_token
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {bearer_token}"

    @staticmethod
    def app_only_bearer(consumer_key: str, consumer_secret: str) -> str:
        """用 Consumer Key/Secret 申请 app-only Bearer Token。"""
        key = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
        r = requests.post(
            "https://api.twitter.com/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {key}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15)
        r.raise_for_status()
        return r.json()["access_token"]

    def search_recent(self, query: str, max_results: int = 10) -> list[dict]:
        """搜索最近推文。返回 tweets 列表。"""
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "author_id,text,created_at,lang,public_metrics",
            "user.fields": "username,name",
        }
        url = f"{API}/tweets/search/recent?{urlencode(params)}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 429:
                logger.warning("X 限流(429): %s", r.text[:100])
                return []
            if r.status_code == 403:
                # 免费层可能没用 recent search，尝试 excludes="retweets"
                logger.warning("X 403 搜索受限（免费层无 recent search?）")
                return []
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            logger.warning("X 搜索失败: %s", e)
            return []

    def extract_mentions(self, tweets: list[dict]) -> list[str]:
        """从推文文本提取 @用户名。"""
        import re
        users = set()
        for t in tweets:
            text = t.get("text", "")
            for m in re.finditer(r"@([A-Za-z0-9_]+)", text):
                users.add(m.group(1).lower())
        return list(users)
