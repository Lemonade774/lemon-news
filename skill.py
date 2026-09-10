#!/usr/bin/env python3
"""Lemon News daily collector and static report generator."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "history"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
AI_KEYWORDS = [
    "人工智能", "大模型", "LLM", "生成式 AI", "AIGC", "机器学习", "深度学习", "神经网络",
    "自然语言处理", "计算机视觉", "强化学习", "AI", "AGI", "GPT", "Claude", "Gemini",
    "OpenAI", "Anthropic", "DeepSeek", "Agent", "智能体", "具身智能", "机器人", "自动驾驶",
    "AI 芯片", "GPU", "NPU", "算力",
]
FINANCE_KEYWORDS = [
    "投研", "量化", "风控", "支付", "财富管理", "合规", "金融大模型", "融资", "投资方",
    "银行", "证券", "保险", "基金", "资管", "智能投顾", "金融科技", "数字人民币", "交易",
]
VALID_TRACKS = {"industry", "finance"}
VALID_TOPICS = {
    "model_release", "model_update", "agent", "ai_application", "ai_finance",
    "company", "compute", "embodied_ai",
}
TOPIC_LABELS = {
    "model_release": "模型发布", "model_update": "模型更新", "agent": "Agent",
    "ai_application": "AI 应用", "ai_finance": "AI × 金融", "company": "公司动态",
    "compute": "算力与芯片", "embodied_ai": "具身智能",
}
HOT_KEYWORDS = ["发布", "推出", "突破", "重磅", "首次", "开源", "融资"]
FUN_KEYWORDS = ["黄仁勋", "马斯克", "LeCun", "华为", "阿里", "字节", "腾讯", "离职", "创业", "融资"]
TAG_RULES = [
    ("模型发布", ("发布模型", "模型发布", "推出模型", "开源模型", "正式发布")),
    ("模型更新", ("模型更新", "版本更新", "升级模型", "新版模型", "能力提升")),
    ("大模型", ("大模型", "LLM", "GPT", "Claude", "Gemini", "DeepSeek", "模型")),
    ("Agent", ("agent", "智能体", "工作流", "自动化")),
    ("AI 应用", ("应用", "产品", "工具", "视频", "音乐", "办公", "插件")),
    ("公司动态", ("公司", "发布", "推出", "收购", "员工", "人才", "创始人", "创投")),
    ("融资与投资", ("融资", "投资", "IPO", "估值", "基金", "收购")),
    ("算力与芯片", ("GPU", "NPU", "芯片", "算力", "显卡", "服务器")),
    ("具身智能", ("机器人", "具身", "自动驾驶", "无人机")),
    ("AI × 金融", ("金融", "投研", "量化", "风控", "支付", "财富", "证券", "银行")),
]
TAG_TONES = {
    "模型发布": "model",
    "模型更新": "model",
    "大模型": "model",
    "Agent": "agent",
    "AI 应用": "app",
    "公司动态": "company",
    "融资与投资": "finance",
    "算力与芯片": "compute",
    "具身智能": "embodied",
    "AI × 金融": "finance",
    "AI 综合": "general",
}


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    publish_time: str
    category: str
    summary: str = ""
    importance_score: int = 5
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    track: str = "industry"
    topic: str = "ai_application"
    source_type: str = "media"
    discovery_source: str = ""
    corroboration_count: int = 1
    publish_time_source: str = "unknown"
    selection_reason: str = ""
    is_backfill: bool = False
    companies: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    financing: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        values = {key: data[key] for key in ("title", "source", "url", "publish_time", "category", "track", "topic", "source_type", "discovery_source", "corroboration_count", "publish_time_source", "selection_reason", "is_backfill", "summary", "importance_score", "keywords", "tags", "companies", "products", "financing") if key in data}
        values.setdefault("title", "")
        values.setdefault("source", "未知来源")
        values.setdefault("url", "")
        values.setdefault("publish_time", "")
        values.setdefault("category", "AI 综合")
        values["track"] = infer_track(values["title"], values.get("summary", ""), values["category"], values.get("track", ""))
        values["topic"] = infer_topic(values["title"], values.get("summary", ""), values["track"], values.get("topic", ""))
        values.setdefault("source_type", "media")
        values.setdefault("discovery_source", "")
        values.setdefault("corroboration_count", 1)
        values.setdefault("publish_time_source", "legacy" if values["publish_time"] else "unknown")
        values.setdefault("selection_reason", "")
        values.setdefault("is_backfill", False)
        values.setdefault("summary", "")
        values.setdefault("importance_score", 5)
        values.setdefault("keywords", [])
        values.setdefault("companies", [])
        values.setdefault("products", [])
        values.setdefault("financing", "")
        values["title"] = normalize_title(values["title"])
        values.setdefault("tags", classify_news(values["title"], values["summary"], values["category"], values["track"]))
        if not values["tags"]:
            values["tags"] = classify_news(values["title"], values["summary"], values["category"], values["track"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_ai_related(text: str) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in AI_KEYWORDS)


def is_finance_related(text: str) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in FINANCE_KEYWORDS)


def infer_track(title: str, summary: str = "", category: str = "", explicit: str = "") -> str:
    if explicit in VALID_TRACKS:
        return explicit
    text = f"{title} {summary} {category}"
    return "finance" if "金融" in category or (is_ai_related(text) and is_finance_related(text)) else "industry"


def infer_topic(title: str, summary: str = "", track: str = "industry", explicit: str = "") -> str:
    if explicit in VALID_TOPICS:
        return explicit
    text = f"{title} {summary}".lower()
    if track == "finance" and is_ai_related(text) and is_finance_related(text):
        return "ai_finance"
    model_signal = any(word in text for word in ("模型", "llm", "gpt", "claude", "gemini", "deepseek"))
    if any(word in text for word in ("发布模型", "模型发布", "推出模型", "开源模型", "正式发布")) or (model_signal and any(word in text for word in (" release", " launch", "announce"))):
        return "model_release"
    if any(word in text for word in ("模型更新", "版本更新", "升级模型", "新版模型", "能力提升")) or (model_signal and any(word in text for word in (" update", "upgrade", "new version"))):
        return "model_update"
    if any(word in text for word in ("agent", "智能体", "工作流", "多智能体")):
        return "agent"
    if any(word in text for word in ("gpu", "npu", "芯片", "算力", "服务器")):
        return "compute"
    if any(word in text for word in ("机器人", "具身", "自动驾驶", "无人机")):
        return "embodied_ai"
    if any(word in text for word in ("收购", "融资", "加盟", "离职", "创始人", "公司")):
        return "company"
    return "ai_application"


def translate_english_title(title: str) -> str:
    """Translate common English headline constructions without touching product names."""
    text = title.strip()
    if not text or sum(char.isalpha() for char in text) < 8:
        return text
    latin = sum(char.isascii() and char.isalpha() for char in text)
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    if latin <= cjk * 1.4:
        return text
    exact = {
        "The AI policy window is open. We need to act.": "AI 政策窗口开启，亟需行动",
        "GPT-6 Astra: The next generation in intelligence for work": "GPT-6 Astra：面向工作的新一代智能模型",
        "Paul Christiano joins OpenAI Foundation Board": "Paul Christiano 加入 OpenAI 基金会董事会",
        "How GPT-5.6 Sol helps run quantum computing experiments": "GPT-5.6 Sol 如何辅助量子计算实验",
        "Research acceleration: The view inside OpenAI": "研究加速：OpenAI 内部观察",
        "Give Your Coding Agents a Memory You Own": "为你的 Coding Agent 配置自主记忆",
        "The latest AI news we announced in August 2026": "OpenAI 2026 年 8 月 AI 动态汇总",
    }
    if text in exact:
        return exact[text]
    phrase_map = (
        (r"\bjoins\b", "加入"), (r"\bannounces\b", "宣布"), (r"\blaunches\b", "推出"),
        (r"\breleases\b", "发布"), (r"\blaunch\b", "发布"), (r"\bupdate\b", "更新"),
        (r"\bupdates\b", "更新"), (r"\bimproves\b", "提升"), (r"\bintroduces\b", "推出"),
        (r"\bjoins\b", "加入"), (r"\bBoard\b", "董事会"), (r"\bFoundation\b", "基金会"),
        (r"\bpolicy\b", "政策"), (r"\bwindow\b", "窗口"), (r"\bopen\b", "开启"),
        (r"\bwork\b", "工作"), (r"\bmodel\b", "模型"), (r"\bmodels\b", "模型"),
        (r"\bmemory\b", "记忆"), (r"\bquantum computing experiments\b", "量子计算实验"),
    )
    translated = text
    for pattern, replacement in phrase_map:
        translated = re.sub(pattern, replacement, translated, flags=re.I)
    translated = re.sub(r"^How\s+(.+?)\s+helps\s+(.+)$", r"\1 如何辅助\2", translated, flags=re.I)
    translated = re.sub(r"^(.+?)\s+joins\s+(.+)$", r"\1 加入\2", translated, flags=re.I)
    translated = translated.replace("： The ", "：").replace(": The ", "：")
    translated = re.sub(r"\bthe\b", "", translated, flags=re.I)
    translated = re.sub(r"\s+", " ", translated).strip(" .")
    return translated if any("\u4e00" <= char <= "\u9fff" for char in translated) else text


def normalize_title(title: str) -> str:
    """Reduce clickbait framing and keep English headlines readable for Chinese readers."""
    result = re.sub(r"[🔥🚀💥✨]+", "", title)
    result = re.sub(r"^(刚刚|重磅|独家|突发|速看|最新消息|好消息)[：:、，, ]*", "", result, flags=re.I)
    # Remove rhetorical openings while keeping the factual subject after them.
    result = re.sub(r"^还在[^？?！!]{0,24}[？?！!]\s*", "", result)
    result = result.replace("今年最难的机器人Demo，", "机器人Demo：")
    match = re.match(r"^今年最难的[^，,：:]{0,24}[，,：:]\s*(.+)$", result)
    if match and len(match.group(1)) >= 12:
        result = match.group(1)
    result = re.sub(r"^([^：:，,]{1,16})(急了|慌了|坐不住了)[：:]\s*", r"\1：", result)
    result = re.sub(r"^([^：:]{1,16})：(.+?)全给我搬回(.+?)坐班$", r"\1要求\2回\3办公", result)
    result = re.sub(r"^(企业级[^！!]{0,24}[！!])\s*", "", result)
    result = re.sub(r"^(\d+秒出片比播放还快[，,])\s*", "", result)
    # Tone down hype words without rewriting the reported fact.
    replacements = {
        "很炸的": "",
        "一连串很炸的": "",
        "效率狂飙": "效率提升",
        "全给我搬回": "回",
        "进入iPhone时刻": "上市",
        "配置拉满": "配置升级",
        "真正跑起来": "落地",
        "打开了": "探索",
        "商业化路径": "商业化",
        "神秘": "",
        "一连串": "",
        "直接起飞": "",
        "杀疯了": "",
        "太能卷": "",
        "这波有点绝": "",
        "绝了": "",
        "脑瓜子贼灵光的": "",
    }
    for source, replacement in replacements.items():
        result = result.replace(source, replacement)
    result = re.sub(r"[！!？?。]+$", "", result).strip()
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"([，,：:])\s*([，,：:])", r"\1", result)
    result = translate_english_title(result)
    return result[:68].rstrip("，,：:")


def classify_news(title: str, summary: str = "", category: str = "", track: str = "") -> list[str]:
    text = f"{title} {summary} {category}".lower()
    tags = [label for label, keywords in TAG_RULES if any(keyword.lower() in text for keyword in keywords)]
    if track == "finance" and "AI × 金融" not in tags:
        tags.insert(0, "AI × 金融")
    return tags[:3] or [category or "AI 综合"]


def normalize_url(url: str) -> str:
    """Normalize URLs for cross-source deduplication without changing destinations."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return url.strip()
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"spm", "from", "source"}]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", urlencode(query), ""))


def title_fingerprint(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalize_title(title).lower())


def same_event(left: NewsItem, right: NewsItem) -> bool:
    if normalize_url(left.url) and normalize_url(left.url) == normalize_url(right.url):
        return True
    left_title, right_title = title_fingerprint(left.title), title_fingerprint(right.title)
    if not left_title or not right_title:
        return False
    left_numbers = set(re.findall(r"\d+(?:\.\d+)*", left.title))
    right_numbers = set(re.findall(r"\d+(?:\.\d+)*", right.title))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    return SequenceMatcher(None, left_title, right_title).ratio() >= 0.86


def source_name_from_url(url: str, fallback: str = "AIHub") -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    known = {
        "openai.com": "OpenAI", "anthropic.com": "Anthropic", "deepmind.google": "Google DeepMind",
        "blog.google": "Google", "huggingface.co": "Hugging Face", "github.com": "GitHub",
        "qbitai.com": "量子位", "infoq.cn": "InfoQ", "cls.cn": "财联社", "aihub.cn": "AIHub",
    }
    return next((name for domain, name in known.items() if host == domain or host.endswith(f".{domain}")), fallback)


def parse_date(value: str) -> str:
    value = value.strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if match:
        return "-".join(f"{int(part):02d}" for part in match.groups())
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def parse_aihub_page(page: str, base_url: str = "https://www.aihub.cn/news/") -> list[dict[str, str]]:
    """Parse public AIHub cards without bypassing access controls."""
    soup = BeautifulSoup(page, "lxml")
    containers = soup.select("article, .news-item, .post-item, .article-item, .news-list li")
    if not containers:
        containers = [link.parent for link in soup.select("h2 a, h3 a") if link.parent]
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for container in containers[:50]:
        link = container.select_one("h1 a, h2 a, h3 a, .title a, a[href]")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        if not 8 <= len(title) <= 140:
            continue
        article_url = urljoin(base_url, link.get("href", ""))
        external = next((urljoin(base_url, node.get("href", "")) for node in container.select("a[href]") if urlparse(urljoin(base_url, node.get("href", ""))).netloc and "aihub.cn" not in urlparse(urljoin(base_url, node.get("href", ""))).netloc), "")
        final_url = external or article_url
        key = normalize_url(final_url)
        if not key or key in seen:
            continue
        seen.add(key)
        source_node = container.select_one(".source, .from, [class*='source'], [class*='author']")
        date_node = container.select_one("time, .date, [class*='time'], [class*='date']")
        summary_node = container.select_one("p, .summary, .excerpt, [class*='desc']")
        source = source_node.get_text(" ", strip=True) if source_node else source_name_from_url(final_url)
        source = re.sub(r"^(?:来源|source)\s*[：:]\s*", "", source, flags=re.I).strip()
        if not source or len(source) > 30:
            source = source_name_from_url(final_url)
        parsed.append({
            "title": title, "url": final_url, "source": source,
            "summary": summary_node.get_text(" ", strip=True) if summary_node else "",
            "publish_time": parse_date(date_node.get("datetime", "") or date_node.get_text(" ", strip=True)) if date_node else "",
            "publish_time_source": "listing" if date_node else "unknown",
        })
    return parsed


def parse_rss_feed(page: str, fallback_source: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page, "xml")
    entries = soup.find_all(["item", "entry"])
    parsed: list[dict[str, str]] = []
    for entry in entries[:30]:
        title_node = entry.find("title")
        link_node = entry.find("link")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        url = (link_node.get("href") or link_node.get_text(" ", strip=True)) if link_node else ""
        summary_node = entry.find(["description", "summary", "content"])
        date_node = entry.find(["pubDate", "published", "updated"])
        summary = BeautifulSoup(summary_node.get_text(" ", strip=True), "lxml").get_text(" ", strip=True) if summary_node else ""
        if title and url:
            parsed.append({"title": title, "url": url, "source": fallback_source, "summary": summary, "publish_time": parse_date(date_node.get_text(" ", strip=True)) if date_node else "", "publish_time_source": "rss" if date_node else "unknown"})
    return parsed


def tag_tone(tag: str) -> str:
    return TAG_TONES.get(tag, "general")


def archive_id(item: NewsItem) -> str:
    return hashlib.sha1(f"{item.url}\x00{item.title}".encode("utf-8")).hexdigest()[:12]


def calc_score(title: str, source: str, track: str = "industry", summary: str = "", source_type: str = "media", priority: int = 0, publish_time: str = "", corroboration_count: int = 1) -> tuple[int, int]:
    """Return an explainable content-value score; freshness is handled before ranking."""
    source_weights = {"量子位": 9, "机器之心": 9, "InfoQ": 8, "界面新闻": 8, "虎嗅网": 8, "财联社": 8}
    source_score = max(6, source_weights.get(source, 6), 10 if source_type == "official" else 0)
    source_score = max(1, min(10, source_score + max(-1, min(1, priority)) * 0.5))
    text = f"{title} {summary}"
    hot_hits = sum(keyword.lower() in text.lower() for keyword in HOT_KEYWORDS)
    impact_hits = sum(keyword.lower() in text.lower() for keyword in ("突破", "开源", "收购", "融资", "上线", "降价", "合作", "监管", "投研", "风控", "工作流", "生产落地", "实际应用"))
    event_score = min(10, 5 + hot_hits + impact_hits)
    relevance_score = min(10, 5 + (2 if is_ai_related(text) else 0) + (3 if track == "finance" and is_finance_related(text) else 0))
    corroboration_score = min(10, 5 + max(0, corroboration_count - 1) * 2.5)
    completeness_score = min(10, 4 + (3 if len(summary.strip()) >= 40 else 0) + (2 if parse_date(publish_time) else 0))
    weighted = 0.30 * source_score + 0.25 * event_score + 0.20 * relevance_score + 0.15 * corroboration_score + 0.10 * completeness_score
    fun_score = min(10, 2 * sum(keyword in title for keyword in FUN_KEYWORDS))
    return max(1, min(10, int(weighted + 0.5))), fun_score


def publish_age(item: NewsItem, report_date: date) -> int | None:
    parsed = parse_date(item.publish_time)
    if not parsed:
        return None
    try:
        return (report_date - date.fromisoformat(parsed)).days
    except ValueError:
        return None


def is_substantive_update(current: NewsItem, previous: NewsItem) -> bool:
    """Allow a repeated URL only when the headline contains a materially new version/result."""
    if current.publish_time == previous.publish_time:
        return False
    update_words = ("更新", "升级", "新版", "发布", "上线", "完成", "获批", "结果", "update", "release", "launch")
    if not any(word in current.title.lower() for word in update_words):
        return False
    version_pattern = re.compile(r"\b(?:v|gpt-|claude\s*)?\d+(?:\.\d+)+\b", re.I)
    current_versions = set(version_pattern.findall(current.title))
    previous_versions = set(version_pattern.findall(previous.title))
    return bool(current_versions != previous_versions or SequenceMatcher(None, title_fingerprint(current.title), title_fingerprint(previous.title)).ratio() < 0.78)


def appeared_in_history(item: NewsItem, previous_items: Iterable[NewsItem]) -> bool:
    for previous in previous_items:
        if same_event(item, previous) and not is_substantive_update(item, previous):
            return True
    return False


def load_recent_history(report_date: date, days: int = 7, history_dir: Path = HISTORY_DIR) -> list[NewsItem]:
    previous: list[NewsItem] = []
    for offset in range(1, days + 1):
        path = history_dir / f"report_{(report_date - timedelta(days=offset)).isoformat()}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            previous.extend(NewsItem.from_dict(item) for item in data.get("top_items", []))
        except (OSError, json.JSONDecodeError, TypeError):
            print(f"  [WARN] 历史日报无法读取，已跳过：{path.name}")
    return previous


def load_previous_day_items(report_date: date, history_dir: Path = HISTORY_DIR) -> list[NewsItem]:
    """Load the prior report for hard-fill fallback, preferring the closest date."""
    exact = history_dir / f"report_{(report_date - timedelta(days=1)).isoformat()}.json"
    paths = [exact] if exact.exists() else sorted(history_dir.glob("report_*.json"), reverse=True)
    for path in paths:
        try:
            report = date.fromisoformat(path.stem.removeprefix("report_"))
        except ValueError:
            continue
        if report >= report_date:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [NewsItem.from_dict(item) for item in data.get("top_items", [])]
        except (OSError, json.JSONDecodeError, TypeError):
            print(f"  [WARN] 前日报无法读取，已跳过：{path.name}")
    return []


def select_top_items(
    items: Iterable[NewsItem],
    limit: int = 10,
    industry_target: int = 6,
    finance_target: int = 4,
    topic_targets: dict[str, int] | None = None,
) -> list[NewsItem]:
    """Select a topic-balanced daily list, filling shortages by score and source diversity."""
    ranked = sorted(items, key=lambda item: item.importance_score, reverse=True)
    if topic_targets:
        groups = {
            "models": {"model_release", "model_update"}, "agent": {"agent"},
            "applications": {"ai_application"}, "finance": {"ai_finance"},
            "company_compute": {"company", "compute"},
        }
        selected: list[NewsItem] = []
        used_sources: set[str] = set()
        for group, count in topic_targets.items():
            topics = groups.get(group, {group})
            candidates = [item for item in ranked if item.topic in topics and item not in selected]
            while candidates and sum(item.topic in topics for item in selected) < count and len(selected) < limit:
                choice = next((item for item in candidates if item.source not in used_sources), candidates[0])
                candidates.remove(choice)
                selected.append(choice)
                used_sources.add(choice.source)
        while len(selected) < limit:
            remaining = [item for item in ranked if item not in selected]
            if not remaining:
                break
            choice = next((item for item in remaining if item.source not in used_sources), remaining[0])
            selected.append(choice)
            used_sources.add(choice.source)
        return selected
    buckets = {track: [item for item in ranked if item.track == track] for track in VALID_TRACKS}
    used_sources: set[str] = set()

    def take(track: str, count: int) -> list[NewsItem]:
        candidates = buckets[track][:]
        chosen: list[NewsItem] = []
        while candidates and len(chosen) < count:
            choice = next((item for item in candidates if item.source not in used_sources), candidates[0])
            candidates.remove(choice)
            chosen.append(choice)
            used_sources.add(choice.source)
        return chosen

    industry = take("industry", min(industry_target, limit))
    finance = take("finance", min(finance_target, max(0, limit - len(industry))))
    selected_ids = {id(item) for item in industry + finance}
    while len(industry) + len(finance) < limit:
        remaining = [item for item in ranked if id(item) not in selected_ids]
        if not remaining:
            break
        choice = next((item for item in remaining if item.source not in used_sources), remaining[0])
        (finance if choice.track == "finance" else industry).append(choice)
        selected_ids.add(id(choice))
        used_sources.add(choice.source)

    mixed: list[NewsItem] = []
    industry_index = finance_index = 0
    total = len(industry) + len(finance)
    for position in range(total):
        expected_finance = round((position + 1) * len(finance) / total) if total else 0
        if finance_index < expected_finance and finance_index < len(finance):
            mixed.append(finance[finance_index])
            finance_index += 1
        elif industry_index < len(industry):
            mixed.append(industry[industry_index])
            industry_index += 1
        else:
            mixed.append(finance[finance_index])
            finance_index += 1
    return mixed


def select_daily_items(
    items: Iterable[NewsItem],
    report_date: date,
    previous_items: Iterable[NewsItem] = (),
    previous_day_items: Iterable[NewsItem] = (),
    limit: int = 10,
    topic_targets: dict[str, int] | None = None,
    max_backfill_days: int = 3,
) -> list[NewsItem]:
    """Select unseen news by calendar freshness first, then content value."""
    previous_items = list(previous_items)
    eligible: list[tuple[int, NewsItem]] = []
    for item in items:
        age = publish_age(item, report_date)
        if age is None or age < 0 or age > max_backfill_days:
            continue
        if appeared_in_history(item, previous_items):
            continue
        eligible.append((age, item))

    groups = {
        "models": {"model_release", "model_update"}, "agent": {"agent"},
        "applications": {"ai_application"}, "finance": {"ai_finance"},
        "company_compute": {"company", "compute"},
    }
    targets = topic_targets or {}
    selected: list[NewsItem] = []
    selected_ids: set[int] = set()
    used_sources: set[str] = set()

    def choose(candidates: list[NewsItem]) -> NewsItem:
        return next((candidate for candidate in candidates if candidate.source not in used_sources), candidates[0])

    # Exhaust each freshness tier before considering older content. Topic quotas guide
    # the first pass, but same-day surplus is preferable to an older quota match.
    for ages in ({0}, {1}, {2, 3}):
        tier = sorted((item for age, item in eligible if age in ages), key=lambda item: item.importance_score, reverse=True)
        for group, target in targets.items():
            topics = groups.get(group, {group})
            have = sum(item.topic in topics for item in selected)
            candidates = [item for item in tier if id(item) not in selected_ids and item.topic in topics]
            while candidates and have < target and len(selected) < limit:
                choice = choose(candidates)
                candidates.remove(choice)
                selected.append(choice)
                selected_ids.add(id(choice))
                used_sources.add(choice.source)
                have += 1
        candidates = [item for item in tier if id(item) not in selected_ids]
        while candidates and len(selected) < limit:
            choice = choose(candidates)
            candidates.remove(choice)
            selected.append(choice)
            selected_ids.add(id(choice))
            used_sources.add(choice.source)
        if len(selected) >= limit:
            break

    # A daily report is intentionally full. If fresh candidates are scarce,
    # append the previous report's highest-value items as an explicit fallback.
    if len(selected) < limit:
        fallback = sorted(previous_day_items, key=lambda item: item.importance_score, reverse=True)
        for item in fallback:
            if len(selected) >= limit:
                break
            if any(same_event(item, chosen) for chosen in selected):
                continue
            item.is_backfill = True
            item.selection_reason = "previous_day_top"
            selected.append(item)

    for item in selected:
        if item.selection_reason == "previous_day_top":
            continue
        age = publish_age(item, report_date) or 0
        item.is_backfill = age > 0
        item.selection_reason = "recent_backfill" if item.is_backfill else "today"
    return selected


def summarize_content(content: str, max_len: int = 160) -> str:
    """Deterministic fallback summary; M3 can replace this with an LLM call."""
    if not content or len(content.strip()) < 40:
        return "暂无详细内容，请点击阅读原文查看。"
    sentences = [part.strip() for part in re.split(r"[.!?。！？]", content) if part.strip()]
    useful = [sentence for sentence in sentences[:12] if 18 < len(sentence) < 120 and not any(noise in sentence.lower() for noise in ("广告", "扫码", "公众号", "关注"))]
    result = " | ".join(useful[:3] or sentences[:2])
    return result[:max_len - 3] + "..." if len(result) > max_len else result


KNOWN_COMPANIES = (
    "OpenAI", "Anthropic", "Google", "谷歌", "微软", "Meta", "阿里巴巴", "阿里", "腾讯", "字节跳动",
    "华为", "蚂蚁集团", "蚂蚁", "百度", "DeepSeek", "MiniMax", "百融", "理想汽车", "英伟达", "商汤",
)
KNOWN_PRODUCTS = ("ChatGPT", "Gemini", "Claude", "DeepSeek", "Copilot", "文心一言", "通义千问", "TwinDex", "SkyProduction")
FINANCING_PATTERN = re.compile(r"(?:融资|募资|投资|估值)[^。；;，,]{0,30}?(\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?(?:美元|人民币|元)?)?[^。；;]{0,18}?((?:Pre[- ]?[A-Z]|[A-Z]轮|天使轮|种子轮|战略投资))?", re.I)


def extract_rule_metadata(item: NewsItem) -> NewsItem:
    """Extract conservative entities before optionally asking an LLM to enrich them."""
    text = f"{item.title} {item.summary}"
    companies = [name for name in KNOWN_COMPANIES if name.lower() in text.lower()]
    products = [name for name in KNOWN_PRODUCTS if name.lower() in text.lower()]
    financing = ""
    match = FINANCING_PATTERN.search(text)
    if match and (match.group(1) or match.group(2)):
        financing = " ".join(part for part in match.groups() if part).strip()
    item.companies = list(dict.fromkeys(companies))[:5]
    item.products = list(dict.fromkeys(products))[:5]
    item.financing = financing[:80]
    return item


def parse_llm_json(content: str) -> dict[str, Any] | None:
    """Parse strict JSON or a fenced JSON object returned by a chat model."""
    candidate = content.strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", candidate)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def apply_llm_enrichment(item: NewsItem, payload: dict[str, Any]) -> NewsItem:
    summary = payload.get("summary")
    if isinstance(summary, str) and 12 <= len(summary.strip()) <= 240:
        item.summary = summary.strip()
    title = payload.get("title")
    if isinstance(title, str) and 8 <= len(title.strip()) <= 68:
        item.title = normalize_title(title.strip())
    category = payload.get("category")
    if isinstance(category, str) and 2 <= len(category.strip()) <= 24:
        item.category = category.strip()
    tags = payload.get("tags")
    if isinstance(tags, list):
        clean_tags = [str(tag).strip() for tag in tags if isinstance(tag, str) and 1 <= len(tag.strip()) <= 16]
        if clean_tags:
            item.tags = clean_tags[:3]
    for key in ("companies", "products"):
        values = payload.get(key)
        if isinstance(values, list):
            clean_values = [str(value).strip() for value in values if isinstance(value, str) and value.strip()]
            setattr(item, key, list(dict.fromkeys(clean_values))[:5])
    financing = payload.get("financing")
    if isinstance(financing, str):
        item.financing = financing.strip()[:80]
    if item.track == "finance" and "AI × 金融" not in item.tags:
        item.tags = ["AI × 金融", *item.tags][:3]
    return item


class DashScopeEnricher:
    """Optional DashScope-compatible enrichment with deterministic fallback."""

    def __init__(self) -> None:
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.model = os.getenv("DASHSCOPE_MODEL", "qwen-turbo")
        try:
            self.timeout_seconds = max(3, int(os.getenv("DASHSCOPE_TIMEOUT", "12")))
        except ValueError:
            self.timeout_seconds = 12

    async def enrich(self, item: NewsItem, session: aiohttp.ClientSession) -> NewsItem:
        item = extract_rule_metadata(item)
        if not self.api_key or session is None:
            return item
        prompt = {
            "title": item.title,
            "summary": item.summary,
            "category": item.category,
            "track": item.track,
            "instruction": "用中文提炼主题、事情和结果。只返回 JSON，不要营销措辞。字段为 title, summary, category, tags, companies, products, financing。tags 最多 3 个。",
        }
        body = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是新闻编辑，输出客观、短、可核验的结构化摘要。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        for attempt in range(2):
            try:
                async with session.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout) as response:
                    if response.status >= 400:
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                        return item
                    data = await response.json(content_type=None)
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    parsed = parse_llm_json(content) if isinstance(content, str) else None
                    return apply_llm_enrichment(item, parsed) if parsed else item
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError):
                if attempt == 0:
                    await asyncio.sleep(0.5)
        return item


class AIDailyScraper:
    def __init__(self, config_path: Path | None = None) -> None:
        config_path = config_path or ROOT / "skill.json"
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.sources = self.config.get("config", {}).get("sources", [])
        self.enricher = DashScopeEnricher()
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AIDailyScraper":
        self.session = aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def fetch_html(self, url: str) -> str:
        if not self.session:
            raise RuntimeError("HTTP session is not initialized")
        try:
            async with self.session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    print(f"  [WARN] HTTP {response.status}: {url}")
                    return ""
                return await response.text("utf-8", errors="ignore")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"  [WARN] 请求失败 {url}: {exc}")
            return ""

    async def fetch_article_content(self, url: str) -> tuple[str, str]:
        page = await self.fetch_html(url)
        if not page:
            return "", ""
        soup = BeautifulSoup(page, "lxml")
        content = ""
        for selector in ("article", ".article-content", ".article-body", ".content", ".post-content", "main"):
            element = soup.select_one(selector)
            if element:
                paragraphs = [p.get_text(" ", strip=True) for p in element.select("p")]
                content = " ".join(text for text in paragraphs if len(text) > 20)
                if content:
                    break
        if not content:
            description = soup.find("meta", attrs={"name": "description"})
            content = description.get("content", "") if description else ""
        date_match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", page)
        publish_time = "-".join(f"{int(part):02d}" for part in date_match.groups()) if date_match else ""
        return content[:1200], publish_time

    async def fetch_source(self, source: dict[str, Any]) -> list[NewsItem]:
        name = source.get("name", "未知来源")
        if not source.get("enabled", True):
            return []
        track = source.get("track") if source.get("track") in VALID_TRACKS else "industry"
        page = await self.fetch_html(source.get("url", ""))
        if not page:
            print(f"  [WARN] {name} 暂无可用页面，跳过并继续其他来源")
            return []
        source_type = source.get("source_type", "media")
        if source_type == "aggregator" and source.get("adapter") == "aihub":
            candidates = parse_aihub_page(page, source.get("url", ""))
        elif source_type in {"official", "ecosystem"} and source.get("format") == "rss":
            candidates = parse_rss_feed(page, name)
        else:
            soup = BeautifulSoup(page, "lxml")
            candidates = []
            seen_urls: set[str] = set()
            max_candidates = int(source.get("max_candidates", 40))
            for link in soup.select(source.get("selector", "h2 a, h3 a")):
                url = normalize_url(urljoin(source.get("url", ""), link.get("href", "")))
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append({"title": link.get_text(" ", strip=True), "url": url, "source": name, "summary": "", "publish_time": "", "publish_time_source": "unknown"})
                if len(candidates) >= max_candidates:
                    break
        items: list[NewsItem] = []
        for candidate in candidates:
            raw_title = candidate.get("title", "")
            title_signal = is_ai_related(raw_title)
            finance_signal = is_finance_related(raw_title)
            if not (8 <= len(raw_title) <= 140) or (track != "finance" and not title_signal):
                continue
            title = normalize_title(raw_title)
            article_url = normalize_url(candidate.get("url", "") or source.get("url", ""))
            content = candidate.get("summary", "")
            publish_time = candidate.get("publish_time", "")
            publish_time_source = candidate.get("publish_time_source", "unknown")
            if len(content) < 40 and article_url:
                article_content, article_date = await self.fetch_article_content(article_url)
                content = article_content or content
                if not publish_time and article_date:
                    publish_time = article_date
                    publish_time_source = "article"
            category = source.get("category", "AI 综合")
            summary = summarize_content(content)
            full_text = f"{raw_title} {content}"
            if track == "finance" and not (is_ai_related(full_text) and is_finance_related(full_text)) and not source.get("ai_finance_whitelist", False):
                continue
            explicit_track = track if source.get("track") in VALID_TRACKS and source.get("track_mode") != "auto" else ""
            item_track = infer_track(title, summary, category, explicit_track)
            topic = infer_topic(title, summary, item_track, source.get("topic", ""))
            item_source = candidate.get("source", "") or name
            discovery_source = name if source_type == "aggregator" and item_source != name else source.get("discovery_source", "")
            score, _ = calc_score(raw_title, item_source, item_track, summary, source_type, int(source.get("priority", 0)), publish_time)
            matched_keywords = [keyword for keyword in (*AI_KEYWORDS, *FINANCE_KEYWORDS) if keyword.lower() in f"{title} {summary}".lower()][:8]
            tags = classify_news(title, summary, category, item_track)
            topic_label = TOPIC_LABELS[topic]
            tags = [topic_label, *[tag for tag in tags if tag != topic_label]][:3]
            item = NewsItem(title=title, source=item_source, url=article_url, publish_time=publish_time, publish_time_source=publish_time_source, category=category, track=item_track, topic=topic, source_type=source_type, discovery_source=discovery_source, summary=summary, importance_score=score, keywords=matched_keywords, tags=tags)
            item = extract_rule_metadata(item)
            items.append(item)
        print(f"  [OK] {name}: {len(items)} 条")
        return items

    async def scrape_all(self) -> list[NewsItem]:
        print("[INFO] 开始抓取 Lemon News...\n")
        results = await asyncio.gather(*(self.fetch_source(source) for source in self.sources), return_exceptions=True)
        all_items: list[NewsItem] = []
        for result in results:
            if isinstance(result, list):
                all_items.extend(result)
            elif isinstance(result, Exception):
                print(f"  [WARN] 来源任务失败，已降级继续：{result}")
        ranked = sorted(all_items, key=lambda item: (item.source_type == "official", item.importance_score), reverse=True)
        items: list[NewsItem] = []
        for item in ranked:
            duplicate_index = next((index for index, existing in enumerate(items) if same_event(item, existing)), None)
            if duplicate_index is None:
                items.append(item)
            elif item.source_type == "official" and items[duplicate_index].source_type != "official":
                item.corroboration_count = items[duplicate_index].corroboration_count + 1
                item.importance_score = min(10, item.importance_score + 1)
                items[duplicate_index] = item
            else:
                items[duplicate_index].corroboration_count += 1
                items[duplicate_index].importance_score = min(10, items[duplicate_index].importance_score + 1)
        print(f"\n[OK] 共抓取 {len(items)} 条不重复新闻")
        return items


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def history_dates() -> list[str]:
    return sorted((path.stem.removeprefix("report_") for path in HISTORY_DIR.glob("report_*.json")), reverse=True)


def build_search_index(history_dir: Path = HISTORY_DIR) -> dict[str, Any]:
    """Build a compact, deduplicated index covering every saved daily report."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(history_dir.glob("report_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        report_date = str(payload.get("date") or path.stem.removeprefix("report_"))
        for raw in payload.get("top_items", []):
            item = NewsItem.from_dict(raw)
            key = normalize_url(item.url) or title_fingerprint(item.title)
            if not key or key in seen:
                continue
            seen.add(key)
            records.append({
                "title": item.title, "summary": item.summary, "source": item.source,
                "url": item.url, "publish_time": item.publish_time or report_date,
                "report_date": report_date, "track": item.track, "topic": item.topic,
                "importance_score": item.importance_score, "tags": item.tags,
            })
    return {"generated_at": datetime.now(LOCAL_TZ).isoformat(), "total_items": len(records), "items": records}


def generate_html(items: Iterable[NewsItem], output_path: Path, updated_at: str | None = None, selection_summary: dict[str, int] | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    items = list(items)[:10]
    date_only = output_path.stem.removeprefix("report_") or datetime.now().strftime("%Y-%m-%d")
    updated_at = updated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_label = updated_at[:10] if date_only == "index" else date_only
    in_history = output_path.parent.name == "history"
    home_href = "../index.html" if in_history else "index.html"
    history_href = "report_" if in_history else "history/report_"
    cards = []
    for number, item in enumerate(items, 1):
        score_class = "score-high" if item.importance_score >= 8 else "score-mid" if item.importance_score >= 6 else "score-low"
        safe_url = html.escape(item.url, quote=True)
        tags = item.tags or classify_news(item.title, item.summary, item.category, item.track)
        tag_html = "".join(f'<span class="tag tone-{tag_tone(tag)}">{html.escape(tag)}</span>' for tag in tags)
        source_context = []
        if item.source_type == "official":
            source_context.append('<span class="source-context">官方</span>')
        if item.discovery_source:
            source_context.append(f'<span class="source-context">via {html.escape(item.discovery_source)}</span>')
        if item.selection_reason == "previous_day_top":
            source_context.append('<span class="source-context">前日精选</span>')
        elif item.is_backfill:
            source_context.append('<span class="source-context">近期补位</span>')
        source_context_html = "".join(source_context)
        item_id = archive_id(item)
        cards.append(f'''<article class="news-item" data-news-id="{item_id}" data-track="{html.escape(item.track, quote=True)}" data-topic="{html.escape(item.topic, quote=True)}"><div class="news-number">{number:02d}</div><div class="news-content"><div class="news-title-row"><h2><a href="{safe_url}" target="_blank" rel="noopener">{html.escape(item.title)}</a></h2><a class="read-more" href="{safe_url}" target="_blank" rel="noopener">阅读原文 ↗</a></div><div class="news-meta"><div class="meta-primary"><span class="score {score_class}">热度 {item.importance_score}</span><span class="source">{html.escape(item.source)}</span>{source_context_html}</div><time>{html.escape(item.publish_time)}</time></div><div class="tag-row"><div class="tags">{tag_html}</div><button class="archive-btn" type="button" data-archive-id="{item_id}" data-title="{html.escape(item.title, quote=True)}" data-url="{safe_url}" data-source="{html.escape(item.source, quote=True)}" title="归档新闻" aria-label="归档新闻">☆</button></div><p>{html.escape(item.summary or "暂无摘要")}</p></div></article>''')
    links = "".join(f'<a href="{history_href}{html.escape(date)}.html">{html.escape(date)}</a>' for date in history_dates()[:15]) or '<span class="muted">暂无历史日报</span>'
    tag_names = sorted({tag for item in items for tag in (item.tags or classify_news(item.title, item.summary, item.category, item.track))})
    tag_filters = '<div class="tag-filters" aria-label="按标签筛选"><button class="tag-filter is-active" type="button" data-tag="*">全部</button>' + ''.join(f'<button class="tag-filter tone-{tag_tone(tag)}" type="button" data-tag="{html.escape(tag, quote=True)}">{html.escape(tag)}</button>' for tag in tag_names) + '</div>'
    track_filters = '<div class="track-filters" role="tablist" aria-label="内容轨道"><button class="track-filter is-active" type="button" data-track="*" role="tab" aria-selected="true">全部</button><button class="track-filter tone-industry" type="button" data-track="industry" role="tab" aria-selected="false">AI 产业要闻</button><button class="track-filter tone-finance" type="button" data-track="finance" role="tab" aria-selected="false">AI × 金融</button></div>'
    source_names = sorted({item.source for item in items})
    date_names = sorted({item.publish_time for item in items if item.publish_time})
    source_options = ''.join(f'<option value="{html.escape(source, quote=True)}">{html.escape(source)}</option>' for source in source_names)
    date_options = ''.join(f'<option value="{html.escape(date, quote=True)}">{html.escape(date)}</option>' for date in date_names)
    control_html = f'''<div class="content-controls" aria-label="日报筛选"><label class="search-control"><span>搜索全部历史日报</span><input id="news-search" type="search" placeholder="输入关键词，搜索全部日期" autocomplete="off"></label><label><span>来源</span><select id="source-filter"><option value="*">全部来源</option>{source_options}</select></label><label><span>热度</span><select id="score-filter"><option value="*">全部热度</option><option value="9">9 分及以上</option><option value="8">8 分及以上</option><option value="6">6 分及以上</option></select></label><label><span>日期</span><select id="date-filter"><option value="*">全部日期</option>{date_options}</select></label></div><p id="search-status" class="search-status" aria-live="polite"></p>'''
    data_json_href = "news_data.json" if not in_history else f"{output_path.stem}.json"
    search_index_href = "search_index.json" if not in_history else "../search_index.json"
    insight_html = '<section class="insight-panel" aria-label="数据洞察"><div class="insight-heading"><h3>关键词热度</h3><span class="muted">来自当前日报</span></div><div id="keyword-chart" class="keyword-chart"><span class="muted">正在加载图表...</span></div><div class="market-card"><div><h3>AI 概念股行情</h3><p class="muted">暂无行情数据，等待公开行情源接入。</p></div><span class="market-status">未接入</span></div></section>'
    theme_toggle = '<button id="theme-toggle" class="theme-toggle" type="button" title="切换主题" aria-label="切换主题">◐</button>'
    selection_summary = selection_summary or {}
    today_count = int(selection_summary.get("today_count", 0))
    backfill_count = int(selection_summary.get("backfill_count", 0))
    previous_day_top_count = int(selection_summary.get("previous_day_top_count", 0))
    summary_parts = [f"今日新增 {today_count} 条"]
    if backfill_count:
        summary_parts.append(f"近 72 小时补位 {backfill_count} 条")
    if previous_day_top_count:
        summary_parts.append(f"前日精选 {previous_day_top_count} 条")
    selection_summary_html = f'<p class="selection-summary">{" · ".join(summary_parts)}</p>' if selection_summary else ""
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lemon News · {html.escape(date_label)}</title><style>
:root{{--navy:#18324b;--blue:#2d5a87;--lemon:#f4d35e;--paper:#f7f9fc;--ink:#172333;--muted:#6b7c8f;--line:#dce5ed}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}a{{color:inherit}}button{{font:inherit}}.nav{{background:var(--navy);color:#fff}}.nav-inner{{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;gap:28px}}.brand{{font-size:21px;font-weight:750;text-decoration:none;display:inline-flex;align-items:center}}.brand-mark{{position:relative;display:inline-block;width:30px;height:23px;margin:0 9px 0 1px;background:var(--lemon);border-radius:54% 46% 50% 48%;transform:rotate(-18deg);box-shadow:inset -4px -3px 0 rgba(190,145,0,.16)}}.brand-mark::before{{content:"";position:absolute;left:7px;top:4px;width:7px;height:4px;border-radius:50%;background:rgba(255,255,255,.52);transform:rotate(-18deg)}}.brand-mark::after{{content:"";position:absolute;right:-3px;top:-7px;width:10px;height:6px;border-radius:90% 10% 90% 10%;background:#79a83b;transform:rotate(24deg)}}.nav-note{{color:#b9cada;font-size:13px}}.layout{{max-width:1180px;margin:30px auto;padding:0 24px;display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:24px}}.hero{{margin-bottom:20px;text-align:center}}.eyebrow{{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.12em}}h1{{font-size:32px;line-height:1.2;margin:7px 0;color:var(--navy)}}.date{{color:var(--muted);margin:0}}.news-item{{position:relative;display:flex;gap:18px;padding:20px 0;border-top:1px solid var(--line)}}.news-number{{flex:none;width:42px;height:42px;border-radius:50%;background:var(--lemon);color:var(--navy);display:grid;place-items:center;font-weight:800;font-size:15px}}.news-content{{min-width:0;flex:1}}.news-title-row{{position:relative;text-align:center}}h2{{font-size:19px;line-height:1.4;margin:0;padding:0 96px;text-align:center}}h2 a{{text-decoration:none}}h2 a:hover{{color:var(--blue)}}.news-meta{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:10px 0 5px;color:var(--muted);font-size:12px;white-space:nowrap}}.meta-primary{{display:flex;align-items:center;gap:8px;min-width:0}}.news-meta time{{margin-left:auto}}.news-actions{{display:flex;flex-direction:column;align-items:flex-end;gap:2px;min-height:48px}}.read-more{{position:absolute;right:0;top:0;display:inline-block;color:var(--blue);font-size:13px;white-space:nowrap;text-decoration:none}}.score,.source,.tag{{padding:2px 8px;border-radius:999px;font-weight:700}}.score-high{{background:#ffe6df;color:#b34124}}.score-mid{{background:#fff1c2;color:#886a00}}.score-low{{background:#e5eef7;color:var(--blue)}}.source{{background:#e9eef3;color:var(--blue);font-weight:600}}.tag-row{{position:relative;min-height:30px;display:flex;align-items:center;justify-content:center}}.tags{{display:flex;justify-content:center;gap:6px;flex-wrap:nowrap;overflow-x:auto;margin:0 auto}}.tag-row .archive-btn{{position:absolute;right:0;top:0}}.news-item[hidden],.news-item.is-filtered-out{{display:none}}.track-filters,.tag-filters{{display:flex;justify-content:center;gap:7px;flex-wrap:wrap;margin-top:16px}}.track-filter,.tag-filter{{border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--blue);cursor:pointer;padding:5px 11px;font-size:12px}}.track-filter.is-active,.tag-filter.is-active{{box-shadow:inset 0 0 0 2px currentColor}}.track-filter:hover,.tag-filter:hover{{filter:brightness(.97)}}.tag{{font-size:11px;font-weight:650}}.tone-general{{background:#edf5d0;color:#56701d;border-color:#d8e8a6}}.tone-industry{{background:#edf1f8;color:#48627d;border-color:#d5deea}}.tone-model{{background:#e4efff;color:#2e5e94;border-color:#c5daf5}}.tone-agent{{background:#f1e8ff;color:#6c4b9a;border-color:#ddccf5}}.tone-app{{background:#e7f7ef;color:#2e7657;border-color:#c5e9d5}}.tone-company{{background:#fff0df;color:#99602f;border-color:#f2d4b2}}.tone-finance{{background:#e9ebff;color:#515c9e;border-color:#d0d5f6}}.tone-compute{{background:#e4f5f5;color:#2f7779;border-color:#c6e6e6}}.tone-embodied{{background:#ffe8ec;color:#a24e68;border-color:#f5cbd5}}.archive-btn{{border:0;background:transparent;color:#8ca0b3;font-size:22px;line-height:1;cursor:pointer;padding:0 2px}}.archive-btn.is-archived{{color:#d69c00}}.news-content p{{margin:0;color:#526273}}aside{{position:sticky;top:20px;height:max-content;background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px;text-align:center}}aside h3{{margin:0 0 12px;color:var(--navy);font-size:15px}}aside a{{display:block;padding:7px 0;color:var(--blue);text-decoration:none;border-top:1px solid #edf1f5;font-size:13px}}.archive-panel{{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}}.archive-panel h3{{margin-bottom:9px}}.archive-controls{{display:flex;gap:6px;margin-bottom:10px}}.archive-controls input,.archive-controls select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:6px;padding:6px 7px;color:var(--ink);background:#fff}}.archive-controls button,.archive-remove{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--blue);cursor:pointer;padding:5px 8px;white-space:nowrap}}.archive-group{{border-top:1px solid #edf1f5;text-align:left}}.archive-group summary{{display:flex;justify-content:space-between;align-items:center;padding:9px 2px;cursor:pointer;color:var(--navy);font-weight:700;list-style:none}}.archive-group summary::-webkit-details-marker{{display:none}}.archive-group summary::after{{content:"+";color:var(--muted);font-size:16px}}.archive-group[open] summary::after{{content:"−"}}.archive-group summary small{{color:var(--muted);font-weight:500;margin-left:auto;margin-right:8px}}.archive-group-items{{padding:0 0 8px}}dialog{{width:min(340px,calc(100vw - 36px));border:1px solid var(--line);border-radius:10px;padding:18px;color:var(--ink)}}dialog::backdrop{{background:rgba(24,50,75,.32)}}dialog h4{{margin:0 0 12px;color:var(--navy)}}dialog input{{width:100%;border:1px solid var(--line);border-radius:6px;padding:8px}}.dialog-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}}.dialog-actions button{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--blue);cursor:pointer;padding:6px 12px}}.dialog-actions button[type=submit]{{background:var(--navy);border-color:var(--navy);color:#fff}}.archive-list{{display:grid;gap:8px}}.archive-entry{{padding:8px;background:var(--paper);border-radius:7px;font-size:12px;text-align:left}}.archive-entry a{{border:0;padding:0;font-weight:650}}.archive-entry small{{display:block;color:var(--muted);margin:3px 0 5px}}.archive-entry-row{{display:flex;gap:5px}}.archive-entry select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:5px;font-size:11px}}.archive-remove{{color:#a34b3d;font-size:11px}}.muted{{color:var(--muted);font-size:13px}}footer{{max-width:1180px;margin:12px auto 36px;padding:0 24px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px;align-items:center}}footer .footer-note{{text-align:left}}footer .footer-brand{{text-align:right;white-space:nowrap}}@media(max-width:800px){{.layout{{display:block;margin-top:22px}}aside{{position:static;margin-top:28px}}.nav-inner{{padding:14px 18px;flex-wrap:wrap;gap:8px 18px}}.news-meta{{overflow-x:auto;justify-content:flex-start}}.tag-row .archive-btn{{right:0}}.tags{{justify-content:center}}h2{{padding:0 72px}}.read-more{{top:0}}.tag-filters,.track-filters{{justify-content:flex-start;overflow-x:auto;flex-wrap:nowrap;padding-bottom:3px}}.layout,footer{{padding-left:18px;padding-right:18px}}h1{{font-size:27px}}.news-title-row{{display:block}}.read-more{{top:0;display:inline-block;margin:0}}footer{{display:block}}footer .footer-brand{{display:block;margin-top:4px;text-align:left}}}}
</style></head><body><nav class="nav"><div class="nav-inner"><a class="brand" href="{home_href}"><span class="brand-mark" aria-hidden="true"></span>Lemon News</a><span class="nav-note">AI × 金融 · 每日情报站</span></div></nav><main class="layout"><section><header class="hero"><div class="eyebrow">DAILY INTELLIGENCE</div><h1>今日 AI 情报</h1><p class="date">{html.escape(date_label)}</p>{selection_summary_html}{tag_filters}</header><div id="history-search-results" class="history-search-results" hidden></div>{''.join(cards) or '<p class="muted">今日暂无可用新闻。</p>'}</section><aside><h3>历史日报</h3>{links}<section class="archive-panel" id="archive-panel"><h3>我的归档</h3><div class="archive-controls"><select id="archive-filter" aria-label="归档分组"><option value="*">全部分组</option></select><button id="archive-add-group" type="button" title="新建分组">+ 新建</button></div><dialog id="archive-group-dialog"><form method="dialog" id="archive-group-form"><h4>新建归档分组</h4><input id="archive-group-input" maxlength="24" placeholder="分组名称" aria-label="分组名称" required><div class="dialog-actions"><button type="button" id="archive-group-cancel">取消</button><button type="submit">创建</button></div></form></dialog><div class="archive-list" id="archive-list"><span class="muted">暂无归档新闻</span></div></section></aside></main><footer><span class="footer-note">内容来自公开媒体，仅作信息整理与产品研究；请点击原文核验。</span><span class="footer-brand">Lemon News · AI × 金融每日情报站</span></footer><script>
(() => {{
  const storageKey = 'lemon-news-archive-v1';
  const load = () => {{ try {{ return JSON.parse(localStorage.getItem(storageKey)) || {{groups:['默认'],items:{{}}}}; }} catch (_) {{ return {{groups:['默认'],items:{{}}}}; }} }};
  let state = load();
  if (!Array.isArray(state.groups) || !state.groups.length) state.groups = ['默认'];
  if (!state.items || typeof state.items !== 'object') state.items = {{}};
  const persist = () => localStorage.setItem(storageKey, JSON.stringify(state));
  const escapeHtml = value => String(value ?? '').replace(/[&<>\"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[char]));
  const safeUrl = value => /^https?:\\/\\//i.test(String(value || '')) ? String(value) : '#';
  const list = document.getElementById('archive-list');
  const filter = document.getElementById('archive-filter');
  const dialog = document.getElementById('archive-group-dialog');
  const groupInput = document.getElementById('archive-group-input');
  const render = () => {{
    const selected = filter.value || '*';
    filter.innerHTML = '<option value="*">全部分组</option>' + state.groups.map(group => `<option value="${{escapeHtml(group)}}">${{escapeHtml(group)}}</option>`).join('');
    filter.value = state.groups.includes(selected) ? selected : '*';
    const groups = filter.value === '*' ? state.groups : state.groups.filter(group => group === filter.value);
    list.innerHTML = groups.map(group => {{
      const entries = Object.entries(state.items).filter(([, item]) => item.group === group);
      const entryHtml = entries.map(([id, item]) => `<div class="archive-entry" data-entry-id="${{escapeHtml(id)}}"><a href="${{escapeHtml(safeUrl(item.url))}}" target="_blank" rel="noopener">${{escapeHtml(item.title)}}</a><small>${{escapeHtml(item.source)}}</small><div class="archive-entry-row"><select class="archive-entry-group" aria-label="归档分组">${{state.groups.map(option => `<option value="${{escapeHtml(option)}}" ${{option === item.group ? 'selected' : ''}}>${{escapeHtml(option)}}</option>`).join('')}}</select><button class="archive-remove" type="button" title="移除归档">移除</button></div></div>`).join('');
      return `<details class="archive-group" ${{filter.value !== '*' ? 'open' : ''}}><summary><span>${{escapeHtml(group)}}</span><small>${{entries.length}} 条</small></summary><div class="archive-group-items">${{entryHtml || '<span class="muted">暂无归档新闻</span>'}}</div></details>`;
    }}).join('') || '<span class="muted">暂无归档分组</span>';
    document.querySelectorAll('.archive-btn').forEach(button => {{ const active = Boolean(state.items[button.dataset.archiveId]); button.classList.toggle('is-archived', active); button.textContent = active ? '★' : '☆'; }});
  }};
  document.querySelectorAll('.archive-btn').forEach(button => button.addEventListener('click', () => {{
    const id = button.dataset.archiveId;
    if (state.items[id]) delete state.items[id]; else state.items[id] = {{title: button.dataset.title, url: button.dataset.url, source: button.dataset.source, group: state.groups[0]}};
    persist(); render();
  }}));
  list.addEventListener('click', event => {{ if (event.target.classList.contains('archive-remove')) {{ delete state.items[event.target.closest('[data-entry-id]').dataset.entryId]; persist(); render(); }} }});
  list.addEventListener('change', event => {{ if (event.target.classList.contains('archive-entry-group')) {{ state.items[event.target.closest('[data-entry-id]').dataset.entryId].group = event.target.value; persist(); render(); }} }});
  filter.addEventListener('change', render);
  let activeTrack = '*';
  let activeTag = '*';
  let activeSearch = '';
  let activeSource = '*';
  let activeScore = '*';
  let activeDate = '*';
  const applyFilters = () => {{
    document.querySelectorAll('.news-item').forEach(item => {{
      const scopeMatches = !activeSearch || item.classList.contains('search-result');
      const trackMatches = activeTrack === '*' || item.dataset.track === activeTrack;
      const tagMatches = activeTag === '*' || Array.from(item.querySelectorAll('.tag')).some(node => node.textContent.trim() === activeTag);
      const textMatches = !activeSearch || item.textContent.toLowerCase().includes(activeSearch);
      const sourceMatches = activeSource === '*' || item.querySelector('.source')?.textContent.trim() === activeSource;
      const score = Number((item.querySelector('.score')?.textContent.match(/\\d+/) || [0])[0]);
      const scoreMatches = activeScore === '*' || score >= Number(activeScore);
      const dateMatches = activeDate === '*' || item.querySelector('time')?.textContent.trim() === activeDate;
      const matches = scopeMatches && trackMatches && tagMatches && textMatches && sourceMatches && scoreMatches && dateMatches;
      item.hidden = !matches;
      item.classList.toggle('is-filtered-out', !matches);
    }});
    document.querySelectorAll('.track-filter').forEach(button => {{
      const active = button.dataset.track === activeTrack;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
    }});
    document.querySelectorAll('.tag-filter').forEach(button => button.classList.toggle('is-active', button.dataset.tag === activeTag));
  }};
  document.querySelectorAll('.track-filter').forEach(button => button.addEventListener('click', () => {{ activeTrack = button.dataset.track; applyFilters(); }}));
  document.querySelectorAll('.tag-filter').forEach(button => button.addEventListener('click', () => {{ activeTag = button.dataset.tag; applyFilters(); }}));
  const searchInput = document.getElementById('news-search');
  const sourceFilter = document.getElementById('source-filter');
  const scoreFilter = document.getElementById('score-filter');
  const dateFilter = document.getElementById('date-filter');
  const searchResults = document.getElementById('history-search-results');
  const searchStatus = document.getElementById('search-status');
  const searchIndexHref = {json.dumps(search_index_href, ensure_ascii=False)};
  let searchIndex = null;
  let searchTimer = 0;
  const tagTone = tag => ({{'模型发布':'model','模型更新':'model','大模型':'model','Agent':'agent','AI 应用':'app','公司动态':'company','融资与投资':'finance','算力与芯片':'compute','具身智能':'embodied','AI × 金融':'finance','AI 综合':'general'}}[tag] || 'general');
  const renderHistoryResults = records => {{
    searchResults.innerHTML = records.map((item, index) => {{
      const tags = (item.tags || []).map(tag => `<span class="tag tone-${{tagTone(tag)}}">${{escapeHtml(tag)}}</span>`).join('');
      return `<article class="news-item search-result" data-track="${{escapeHtml(item.track || 'industry')}}"><div class="news-number">${{String(index + 1).padStart(2, '0')}}</div><div class="news-content"><div class="news-title-row"><h2><a href="${{escapeHtml(safeUrl(item.url))}}" target="_blank" rel="noopener">${{escapeHtml(item.title)}}</a></h2><a class="read-more" href="${{escapeHtml(safeUrl(item.url))}}" target="_blank" rel="noopener">阅读原文 ↗</a></div><div class="news-meta"><div class="meta-primary"><span class="score">热度 ${{Number(item.importance_score || 0)}}</span><span class="source">${{escapeHtml(item.source)}}</span><span class="source-context">历史日报 ${{escapeHtml(item.report_date)}}</span></div><time>${{escapeHtml(item.publish_time || item.report_date)}}</time></div><div class="tags">${{tags}}</div><p>${{escapeHtml(item.summary || '暂无摘要')}}</p></div></article>`;
    }}).join('');
    searchResults.hidden = false;
    searchStatus.textContent = `在全部历史日报中找到 ${{records.length}} 条结果`;
  }};
  const runHistorySearch = async () => {{
    activeSearch = searchInput.value.trim().toLowerCase();
    if (!activeSearch) {{ searchResults.hidden = true; searchResults.innerHTML = ''; searchStatus.textContent = ''; applyFilters(); return; }}
    searchStatus.textContent = '正在搜索全部历史日报...';
    try {{
      if (!searchIndex) {{ const response = await fetch(searchIndexHref); if (!response.ok) throw new Error('search index unavailable'); searchIndex = (await response.json()).items || []; }}
      const records = searchIndex.filter(item => [item.title, item.summary, item.source, ...(item.tags || [])].join(' ').toLowerCase().includes(activeSearch)).slice(0, 100);
      renderHistoryResults(records);
      applyFilters();
    }} catch (_) {{ searchStatus.textContent = '历史搜索索引暂不可用'; searchResults.hidden = true; applyFilters(); }}
  }};
  searchInput?.addEventListener('input', () => {{ window.clearTimeout(searchTimer); searchTimer = window.setTimeout(runHistorySearch, 180); }});
  sourceFilter?.addEventListener('change', () => {{ activeSource = sourceFilter.value; applyFilters(); }});
  scoreFilter?.addEventListener('change', () => {{ activeScore = scoreFilter.value; applyFilters(); }});
  dateFilter?.addEventListener('change', () => {{ activeDate = dateFilter.value; applyFilters(); }});
  document.getElementById('archive-add-group').addEventListener('click', () => {{ groupInput.value = ''; if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', ''); groupInput.focus(); }});
  document.getElementById('archive-group-cancel').addEventListener('click', () => {{ if (dialog.close) dialog.close(); else dialog.removeAttribute('open'); }});
  document.getElementById('archive-group-form').addEventListener('submit', event => {{ event.preventDefault(); const group = groupInput.value.trim(); if (group && !state.groups.includes(group)) {{ state.groups.push(group); persist(); render(); }} if (dialog.close) dialog.close(); else dialog.removeAttribute('open'); }});
  render();
  applyFilters();
}})();
</script></body></html>'''
    extra_css = """
body[data-theme="dark"] { --paper:#101923; --ink:#e6edf3; --muted:#9cafc0; --line:#2a3b4c; --blue:#9cc7ed; --navy:#0b1724; }
body[data-theme="dark"] .tag-filter, body[data-theme="dark"] .track-filter, body[data-theme="dark"] .archive-controls select, body[data-theme="dark"] .archive-controls button { background:#172636; color:var(--blue); border-color:var(--line); }
body[data-theme="dark"] aside, body[data-theme="dark"] dialog { background:#142230; }
.theme-toggle { margin-left:auto; border:1px solid rgba(255,255,255,.25); border-radius:999px; background:transparent; color:#fff; cursor:pointer; width:34px; height:30px; }
.content-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:end; justify-content:center; margin:16px 0 4px; }
.content-controls label { display:flex; flex-direction:column; gap:3px; color:var(--muted); font-size:11px; text-align:left; }
.content-controls label span { padding-left:2px; }
.content-controls input, .content-controls select { border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--ink); padding:6px 8px; min-width:112px; }
.content-controls select { width:100%; max-width:100%; }
.news-title-row h2, .news-content p { overflow-wrap:anywhere; }
.content-controls .search-control { min-width:min(300px,100%); flex:1 1 240px; }
.content-controls .search-control input { width:100%; }
.search-status { min-height:20px; margin:5px 0 0; color:var(--muted); font-size:12px; text-align:center; }
.history-search-results { border-top:1px solid var(--line); margin-top:14px; }
.history-search-results .news-item:first-child { border-top:0; }
.history-search-results .tags { margin:4px auto 7px; }
.source-context { color:var(--muted); font-size:10px; padding:1px 5px; border:1px solid var(--line); border-radius:4px; font-weight:600; }
.selection-summary { margin:5px 0 0; color:var(--muted); font-size:12px; }
.insight-panel { border-top:1px solid var(--line); margin-top:18px; padding-top:16px; text-align:left; }
.insight-heading { display:flex; justify-content:space-between; align-items:baseline; }
.insight-panel h3 { margin:0 0 8px; }
.keyword-chart { width:100%; height:248px; min-height:248px; display:grid; place-items:center; overflow:hidden; }
.market-card { display:flex; justify-content:space-between; gap:10px; align-items:center; border-top:1px solid var(--line); padding-top:12px; margin-top:8px; }
.market-card p { margin:0; }
.market-status { color:var(--muted); font-size:11px; white-space:nowrap; }
@media(max-width:800px) { .content-controls { justify-content:stretch; } .content-controls label { flex:1 1 calc(50% - 8px); min-width:0; } .content-controls .search-control { flex-basis:100%; } }
"""
    enhancement_js = f"""
(() => {{
  const themeKey = 'lemon-news-theme-v1';
  const themeButton = document.getElementById('theme-toggle');
  const applyTheme = theme => {{ document.body.dataset.theme = theme; if (themeButton) themeButton.textContent = theme === 'dark' ? '☀' : '◐'; }};
  applyTheme(localStorage.getItem(themeKey) || 'light');
  themeButton?.addEventListener('click', () => {{ const next = document.body.dataset.theme === 'dark' ? 'light' : 'dark'; localStorage.setItem(themeKey, next); applyTheme(next); }});
  const chartRoot = document.getElementById('keyword-chart');
  const jsonHref = {json.dumps(data_json_href, ensure_ascii=False)};
  fetch(jsonHref).then(response => {{ if (!response.ok) throw new Error('data unavailable'); return response.json(); }}).then(data => {{
    const counts = {{}};
    (data.top_items || []).forEach(item => (item.tags || []).forEach(tag => {{ counts[tag] = (counts[tag] || 0) + 1; }}));
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 8);
    if (!entries.length || !window.echarts) {{ chartRoot.textContent = '暂无足够数据生成关键词图表'; return; }}
    const chart = window.echarts.init(chartRoot);
    chart.setOption({{ grid: {{ left: 6, right: 12, top: 4, bottom: 4, containLabel: true }}, xAxis: {{ type: 'value', minInterval: 1, axisLabel: {{ color: getComputedStyle(document.body).getPropertyValue('--muted') }} }}, yAxis: {{ type: 'category', inverse: true, data: entries.map(entry => entry[0]), axisLabel: {{ color: getComputedStyle(document.body).getPropertyValue('--muted'), width: 76, overflow: 'truncate' }} }}, series: [{{ type: 'bar', data: entries.map(entry => entry[1]), barMaxWidth: 18, itemStyle: {{ color: '#f4d35e', borderRadius: [0, 4, 4, 0] }} }}] }});
    window.addEventListener('resize', () => chart.resize());
  }}).catch(() => {{ chartRoot.textContent = '关键词数据暂不可用'; }});
}})();
"""
    document = document.replace("</style>", extra_css + "</style>", 1)
    document = document.replace('<div class="tag-filters"', f'{control_html}{track_filters}<div class="tag-filters"', 1)
    document = document.replace('</div></nav>', f'{theme_toggle}</div></nav>', 1)
    document = document.replace('<section class="archive-panel"', f'{insight_html}<section class="archive-panel"', 1)
    document = document.replace('</head>', '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script></head>', 1)
    document = document.replace('</body>', f'<script>{enhancement_js}</script></body>', 1)
    output_path.write_text(document, encoding="utf-8")


def render_history() -> int:
    count = 0
    for json_path in sorted(HISTORY_DIR.glob("report_*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        generate_html((NewsItem.from_dict(item) for item in data.get("top_items", [])), json_path.with_suffix(".html"), data.get("update_time"), data.get("selection_summary"))
        count += 1
    return count


async def run() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    report_date = datetime.now(LOCAL_TZ).date()
    scraper = AIDailyScraper()
    async with scraper:
        items = await scraper.scrape_all()
        output_config = scraper.config.get("config", {}).get("output", {})
        previous_items = load_recent_history(report_date, int(output_config.get("dedupe_window_days", 7)))
        previous_day_items = load_previous_day_items(report_date)
        if not items:
            print("  [WARN] 所有来源暂不可用，使用上一期日报 Top 内容降级生成")
        top_items = select_daily_items(
            items,
            report_date,
            previous_items,
            previous_day_items,
            limit=int(output_config.get("daily_limit", 10)),
            topic_targets=output_config.get("topic_targets"),
            max_backfill_days=int(output_config.get("max_backfill_days", 3)),
        )
        if not top_items:
            print("  [WARN] 没有可用候选或上一期日报，保留最近一次日报")
            return
        if top_items and scraper.enricher.api_key and scraper.session:
            top_items = await asyncio.gather(*(scraper.enricher.enrich(item, scraper.session) for item in top_items))
    today = report_date.isoformat()
    now = datetime.now(LOCAL_TZ).isoformat()
    selection_summary = {
        "today_count": sum(item.selection_reason == "today" for item in top_items),
        "backfill_count": sum(item.selection_reason == "recent_backfill" for item in top_items),
        "previous_day_top_count": sum(item.selection_reason == "previous_day_top" for item in top_items),
        "selected_count": len(top_items),
        "candidate_count": len(items),
        "dedupe_window_days": int(output_config.get("dedupe_window_days", 7)),
        "max_backfill_days": int(output_config.get("max_backfill_days", 3)),
    }
    payload = {"date": today, "update_time": now, "total_items": len(items), "selection_summary": selection_summary, "top_items": [item.to_dict() for item in top_items]}
    save_json(ROOT / "news_data.json", payload)
    save_json(HISTORY_DIR / f"report_{today}.json", payload)
    save_json(ROOT / "search_index.json", build_search_index())
    generate_html(top_items, ROOT / "index.html", now, selection_summary)
    generate_html(top_items, HISTORY_DIR / f"report_{today}.html", now, selection_summary)
    print(f"[OK] 已生成 Lemon News：{len(top_items)} 条精选（今日 {selection_summary['today_count']}，补位 {selection_summary['backfill_count']}）")


if __name__ == "__main__":
    asyncio.run(run())
