#!/usr/bin/env python3
"""Lemon News daily collector and static report generator."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "history"
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
HOT_KEYWORDS = ["发布", "推出", "突破", "重磅", "首次", "开源", "融资"]
FUN_KEYWORDS = ["黄仁勋", "马斯克", "LeCun", "华为", "阿里", "字节", "腾讯", "离职", "创业", "融资"]
TAG_RULES = [
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        values = {key: data[key] for key in ("title", "source", "url", "publish_time", "category", "track", "summary", "importance_score", "keywords", "tags") if key in data}
        values.setdefault("title", "")
        values.setdefault("source", "未知来源")
        values.setdefault("url", "")
        values.setdefault("publish_time", "")
        values.setdefault("category", "AI 综合")
        values["track"] = infer_track(values["title"], values.get("summary", ""), values["category"], values.get("track", ""))
        values.setdefault("summary", "")
        values.setdefault("importance_score", 5)
        values.setdefault("keywords", [])
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


def normalize_title(title: str) -> str:
    """Reduce clickbait framing while preserving the factual title body."""
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
    return result[:68].rstrip("，,：:")


def classify_news(title: str, summary: str = "", category: str = "", track: str = "") -> list[str]:
    text = f"{title} {summary} {category}".lower()
    tags = [label for label, keywords in TAG_RULES if any(keyword.lower() in text for keyword in keywords)]
    if track == "finance" and "AI × 金融" not in tags:
        tags.insert(0, "AI × 金融")
    return tags[:3] or [category or "AI 综合"]


def tag_tone(tag: str) -> str:
    return TAG_TONES.get(tag, "general")


def archive_id(item: NewsItem) -> str:
    return hashlib.sha1(f"{item.url}\x00{item.title}".encode("utf-8")).hexdigest()[:12]


def calc_score(title: str, source: str, track: str = "industry", summary: str = "") -> tuple[int, int]:
    source_weights = {"量子位": 9, "机器之心": 9, "InfoQ": 8, "界面新闻": 8, "虎嗅网": 8, "财联社": 8}
    score = max(6, source_weights.get(source, 5))
    score += 0.5 * sum(keyword in title for keyword in HOT_KEYWORDS)
    finance_hits = sum(keyword.lower() in f"{title} {summary}".lower() for keyword in FINANCE_KEYWORDS)
    if track == "finance" or finance_hits:
        score += min(2, 1 + 0.25 * max(finance_hits - 1, 0))
    fun_score = min(10, 2 * sum(keyword in title for keyword in FUN_KEYWORDS))
    return min(int(score), 10), fun_score


def select_top_items(
    items: Iterable[NewsItem],
    limit: int = 10,
    industry_target: int = 6,
    finance_target: int = 4,
) -> list[NewsItem]:
    """Select a diverse 6/4 track mix, filling shortages from the other track."""
    ranked = sorted(items, key=lambda item: item.importance_score, reverse=True)
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


def summarize_content(content: str, max_len: int = 160) -> str:
    """Deterministic fallback summary; M3 can replace this with an LLM call."""
    if not content or len(content.strip()) < 40:
        return "暂无详细内容，请点击阅读原文查看。"
    sentences = [part.strip() for part in re.split(r"[.!?。！？]", content) if part.strip()]
    useful = [sentence for sentence in sentences[:12] if 18 < len(sentence) < 120 and not any(noise in sentence.lower() for noise in ("广告", "扫码", "公众号", "关注"))]
    result = " | ".join(useful[:3] or sentences[:2])
    return result[:max_len - 3] + "..." if len(result) > max_len else result


class AIDailyScraper:
    def __init__(self, config_path: Path | None = None) -> None:
        config_path = config_path or ROOT / "skill.json"
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.sources = self.config.get("config", {}).get("sources", [])
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
                    print(f"  ⚠️ HTTP {response.status}: {url}")
                    return ""
                return await response.text("utf-8", errors="ignore")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"  ⚠️ 请求失败 {url}: {exc}")
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
        track = source.get("track") if source.get("track") in VALID_TRACKS else "industry"
        page = await self.fetch_html(source.get("url", ""))
        if not page:
            print(f"  [WARN] {name} 暂无可用页面，跳过并继续其他来源")
            return []
        soup = BeautifulSoup(page, "lxml")
        items: list[NewsItem] = []
        for link in soup.select(source.get("selector", "h2 a, h3 a"))[:25]:
            raw_title = link.get_text(" ", strip=True)
            related = is_ai_related(raw_title) or (track == "finance" and is_finance_related(raw_title))
            if not (8 <= len(raw_title) <= 140) or not related:
                continue
            title = normalize_title(raw_title)
            article_url = urljoin(source.get("url", ""), link.get("href", ""))
            content, publish_time = await self.fetch_article_content(article_url)
            category = source.get("category", "AI 综合")
            summary = summarize_content(content)
            score, _ = calc_score(raw_title, name, track, summary)
            matched_keywords = [keyword for keyword in (*AI_KEYWORDS, *FINANCE_KEYWORDS) if keyword.lower() in f"{title} {summary}".lower()][:8]
            items.append(NewsItem(title=title, source=name, url=article_url or source.get("url", ""), publish_time=publish_time or datetime.now().strftime("%Y-%m-%d"), category=category, track=track, summary=summary, importance_score=score, keywords=matched_keywords, tags=classify_news(title, summary, category, track)))
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
        unique: dict[str, NewsItem] = {}
        for item in all_items:
            unique.setdefault(item.title, item)
        items = sorted(unique.values(), key=lambda item: item.importance_score, reverse=True)
        print(f"\n[OK] 共抓取 {len(items)} 条不重复新闻")
        return items


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def history_dates() -> list[str]:
    return sorted((path.stem.removeprefix("report_") for path in HISTORY_DIR.glob("report_*.json")), reverse=True)


def generate_html(items: Iterable[NewsItem], output_path: Path, updated_at: str | None = None) -> None:
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
        item_id = archive_id(item)
        cards.append(f'''<article class="news-item" data-news-id="{item_id}" data-track="{html.escape(item.track, quote=True)}"><div class="news-number">{number:02d}</div><div class="news-content"><div class="news-title-row"><h2><a href="{safe_url}" target="_blank" rel="noopener">{html.escape(item.title)}</a></h2><a class="read-more" href="{safe_url}" target="_blank" rel="noopener">阅读原文 ↗</a></div><div class="news-meta"><div class="meta-primary"><span class="score {score_class}">热度 {item.importance_score}</span><span class="source">{html.escape(item.source)}</span></div><time>{html.escape(item.publish_time)}</time></div><div class="tag-row"><div class="tags">{tag_html}</div><button class="archive-btn" type="button" data-archive-id="{item_id}" data-title="{html.escape(item.title, quote=True)}" data-url="{safe_url}" data-source="{html.escape(item.source, quote=True)}" title="归档新闻" aria-label="归档新闻">☆</button></div><p>{html.escape(item.summary or "暂无摘要")}</p></div></article>''')
    links = "".join(f'<a href="{history_href}{html.escape(date)}.html">{html.escape(date)}</a>' for date in history_dates()[:15]) or '<span class="muted">暂无历史日报</span>'
    tag_names = sorted({tag for item in items for tag in (item.tags or classify_news(item.title, item.summary, item.category, item.track))})
    tag_filters = '<div class="tag-filters" aria-label="按标签筛选"><button class="tag-filter is-active" type="button" data-tag="*">全部</button>' + ''.join(f'<button class="tag-filter tone-{tag_tone(tag)}" type="button" data-tag="{html.escape(tag, quote=True)}">{html.escape(tag)}</button>' for tag in tag_names) + '</div>'
    track_filters = '<div class="track-filters" role="tablist" aria-label="内容轨道"><button class="track-filter is-active" type="button" data-track="*" role="tab" aria-selected="true">全部</button><button class="track-filter tone-industry" type="button" data-track="industry" role="tab" aria-selected="false">AI 产业要闻</button><button class="track-filter tone-finance" type="button" data-track="finance" role="tab" aria-selected="false">AI × 金融</button></div>'
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lemon News · {html.escape(date_label)}</title><style>
:root{{--navy:#18324b;--blue:#2d5a87;--lemon:#f4d35e;--paper:#f7f9fc;--ink:#172333;--muted:#6b7c8f;--line:#dce5ed}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}a{{color:inherit}}button{{font:inherit}}.nav{{background:var(--navy);color:#fff}}.nav-inner{{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;gap:28px}}.brand{{font-size:21px;font-weight:750;text-decoration:none;display:inline-flex;align-items:center}}.brand-mark{{position:relative;display:inline-block;width:30px;height:23px;margin:0 9px 0 1px;background:var(--lemon);border-radius:54% 46% 50% 48%;transform:rotate(-18deg);box-shadow:inset -4px -3px 0 rgba(190,145,0,.16)}}.brand-mark::before{{content:"";position:absolute;left:7px;top:4px;width:7px;height:4px;border-radius:50%;background:rgba(255,255,255,.52);transform:rotate(-18deg)}}.brand-mark::after{{content:"";position:absolute;right:-3px;top:-7px;width:10px;height:6px;border-radius:90% 10% 90% 10%;background:#79a83b;transform:rotate(24deg)}}.nav-note{{color:#b9cada;font-size:13px}}.layout{{max-width:1180px;margin:30px auto;padding:0 24px;display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:24px}}.hero{{margin-bottom:20px;text-align:center}}.eyebrow{{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.12em}}h1{{font-size:32px;line-height:1.2;margin:7px 0;color:var(--navy)}}.date{{color:var(--muted);margin:0}}.news-item{{position:relative;display:flex;gap:18px;padding:20px 0;border-top:1px solid var(--line)}}.news-number{{flex:none;width:42px;height:42px;border-radius:50%;background:var(--lemon);color:var(--navy);display:grid;place-items:center;font-weight:800;font-size:15px}}.news-content{{min-width:0;flex:1}}.news-title-row{{position:relative;text-align:center}}h2{{font-size:19px;line-height:1.4;margin:0;padding:0 96px;text-align:center}}h2 a{{text-decoration:none}}h2 a:hover{{color:var(--blue)}}.news-meta{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:10px 0 5px;color:var(--muted);font-size:12px;white-space:nowrap}}.meta-primary{{display:flex;align-items:center;gap:8px;min-width:0}}.news-meta time{{margin-left:auto}}.news-actions{{display:flex;flex-direction:column;align-items:flex-end;gap:2px;min-height:48px}}.read-more{{position:absolute;right:0;top:0;display:inline-block;color:var(--blue);font-size:13px;white-space:nowrap;text-decoration:none}}.score,.source,.tag{{padding:2px 8px;border-radius:999px;font-weight:700}}.score-high{{background:#ffe6df;color:#b34124}}.score-mid{{background:#fff1c2;color:#886a00}}.score-low{{background:#e5eef7;color:var(--blue)}}.source{{background:#e9eef3;color:var(--blue);font-weight:600}}.tag-row{{position:relative;min-height:30px;display:flex;align-items:center;justify-content:center}}.tags{{display:flex;justify-content:center;gap:6px;flex-wrap:nowrap;overflow-x:auto;margin:0 auto}}.tag-row .archive-btn{{position:absolute;right:0;top:0}}.news-item[hidden],.news-item.is-filtered-out{{display:none}}.track-filters,.tag-filters{{display:flex;justify-content:center;gap:7px;flex-wrap:wrap;margin-top:16px}}.track-filter,.tag-filter{{border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--blue);cursor:pointer;padding:5px 11px;font-size:12px}}.track-filter.is-active,.tag-filter.is-active{{box-shadow:inset 0 0 0 2px currentColor}}.track-filter:hover,.tag-filter:hover{{filter:brightness(.97)}}.tag{{font-size:11px;font-weight:650}}.tone-general{{background:#edf5d0;color:#56701d;border-color:#d8e8a6}}.tone-industry{{background:#edf1f8;color:#48627d;border-color:#d5deea}}.tone-model{{background:#e4efff;color:#2e5e94;border-color:#c5daf5}}.tone-agent{{background:#f1e8ff;color:#6c4b9a;border-color:#ddccf5}}.tone-app{{background:#e7f7ef;color:#2e7657;border-color:#c5e9d5}}.tone-company{{background:#fff0df;color:#99602f;border-color:#f2d4b2}}.tone-finance{{background:#e9ebff;color:#515c9e;border-color:#d0d5f6}}.tone-compute{{background:#e4f5f5;color:#2f7779;border-color:#c6e6e6}}.tone-embodied{{background:#ffe8ec;color:#a24e68;border-color:#f5cbd5}}.archive-btn{{border:0;background:transparent;color:#8ca0b3;font-size:22px;line-height:1;cursor:pointer;padding:0 2px}}.archive-btn.is-archived{{color:#d69c00}}.news-content p{{margin:0;color:#526273}}aside{{position:sticky;top:20px;height:max-content;background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px;text-align:center}}aside h3{{margin:0 0 12px;color:var(--navy);font-size:15px}}aside a{{display:block;padding:7px 0;color:var(--blue);text-decoration:none;border-top:1px solid #edf1f5;font-size:13px}}.archive-panel{{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}}.archive-panel h3{{margin-bottom:9px}}.archive-controls{{display:flex;gap:6px;margin-bottom:10px}}.archive-controls input,.archive-controls select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:6px;padding:6px 7px;color:var(--ink);background:#fff}}.archive-controls button,.archive-remove{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--blue);cursor:pointer;padding:5px 8px;white-space:nowrap}}.archive-group{{border-top:1px solid #edf1f5;text-align:left}}.archive-group summary{{display:flex;justify-content:space-between;align-items:center;padding:9px 2px;cursor:pointer;color:var(--navy);font-weight:700;list-style:none}}.archive-group summary::-webkit-details-marker{{display:none}}.archive-group summary::after{{content:"+";color:var(--muted);font-size:16px}}.archive-group[open] summary::after{{content:"−"}}.archive-group summary small{{color:var(--muted);font-weight:500;margin-left:auto;margin-right:8px}}.archive-group-items{{padding:0 0 8px}}dialog{{width:min(340px,calc(100vw - 36px));border:1px solid var(--line);border-radius:10px;padding:18px;color:var(--ink)}}dialog::backdrop{{background:rgba(24,50,75,.32)}}dialog h4{{margin:0 0 12px;color:var(--navy)}}dialog input{{width:100%;border:1px solid var(--line);border-radius:6px;padding:8px}}.dialog-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}}.dialog-actions button{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--blue);cursor:pointer;padding:6px 12px}}.dialog-actions button[type=submit]{{background:var(--navy);border-color:var(--navy);color:#fff}}.archive-list{{display:grid;gap:8px}}.archive-entry{{padding:8px;background:var(--paper);border-radius:7px;font-size:12px;text-align:left}}.archive-entry a{{border:0;padding:0;font-weight:650}}.archive-entry small{{display:block;color:var(--muted);margin:3px 0 5px}}.archive-entry-row{{display:flex;gap:5px}}.archive-entry select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:5px;font-size:11px}}.archive-remove{{color:#a34b3d;font-size:11px}}.muted{{color:var(--muted);font-size:13px}}footer{{max-width:1180px;margin:12px auto 36px;padding:0 24px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px;align-items:center}}footer .footer-note{{text-align:left}}footer .footer-brand{{text-align:right;white-space:nowrap}}@media(max-width:800px){{.layout{{display:block;margin-top:22px}}aside{{position:static;margin-top:28px}}.nav-inner{{padding:14px 18px;flex-wrap:wrap;gap:8px 18px}}.news-meta{{overflow-x:auto;justify-content:flex-start}}.tag-row .archive-btn{{right:0}}.tags{{justify-content:center}}h2{{padding:0 72px}}.read-more{{top:0}}.tag-filters,.track-filters{{justify-content:flex-start;overflow-x:auto;flex-wrap:nowrap;padding-bottom:3px}}.layout,footer{{padding-left:18px;padding-right:18px}}h1{{font-size:27px}}.news-title-row{{display:block}}.read-more{{top:0;display:inline-block;margin:0}}footer{{display:block}}footer .footer-brand{{display:block;margin-top:4px;text-align:left}}}}
</style></head><body><nav class="nav"><div class="nav-inner"><a class="brand" href="{home_href}"><span class="brand-mark" aria-hidden="true"></span>Lemon News</a><span class="nav-note">AI × 金融 · 每日情报站</span></div></nav><main class="layout"><section><header class="hero"><div class="eyebrow">DAILY INTELLIGENCE</div><h1>今日 AI 情报</h1><p class="date">{html.escape(date_label)}</p>{tag_filters}</header>{''.join(cards) or '<p class="muted">今日暂无可用新闻。</p>'}</section><aside><h3>历史日报</h3>{links}<section class="archive-panel" id="archive-panel"><h3>我的归档</h3><div class="archive-controls"><select id="archive-filter" aria-label="归档分组"><option value="*">全部分组</option></select><button id="archive-add-group" type="button" title="新建分组">+ 新建</button></div><dialog id="archive-group-dialog"><form method="dialog" id="archive-group-form"><h4>新建归档分组</h4><input id="archive-group-input" maxlength="24" placeholder="分组名称" aria-label="分组名称" required><div class="dialog-actions"><button type="button" id="archive-group-cancel">取消</button><button type="submit">创建</button></div></form></dialog><div class="archive-list" id="archive-list"><span class="muted">暂无归档新闻</span></div></section></aside></main><footer><span class="footer-note">内容来自公开媒体，仅作信息整理与产品研究；请点击原文核验。</span><span class="footer-brand">Lemon News · AI × 金融每日情报站</span></footer><script>
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
  const applyFilters = () => {{
    document.querySelectorAll('.news-item').forEach(item => {{
      const trackMatches = activeTrack === '*' || item.dataset.track === activeTrack;
      const tagMatches = activeTag === '*' || Array.from(item.querySelectorAll('.tag')).some(node => node.textContent.trim() === activeTag);
      const matches = trackMatches && tagMatches;
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
  document.getElementById('archive-add-group').addEventListener('click', () => {{ groupInput.value = ''; if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', ''); groupInput.focus(); }});
  document.getElementById('archive-group-cancel').addEventListener('click', () => {{ if (dialog.close) dialog.close(); else dialog.removeAttribute('open'); }});
  document.getElementById('archive-group-form').addEventListener('submit', event => {{ event.preventDefault(); const group = groupInput.value.trim(); if (group && !state.groups.includes(group)) {{ state.groups.push(group); persist(); render(); }} if (dialog.close) dialog.close(); else dialog.removeAttribute('open'); }});
  render();
  applyFilters();
}})();
</script></body></html>'''
    document = document.replace('<div class="tag-filters"', f'{track_filters}<div class="tag-filters"', 1)
    output_path.write_text(document, encoding="utf-8")


def render_history() -> int:
    count = 0
    for json_path in sorted(HISTORY_DIR.glob("report_*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        generate_html((NewsItem.from_dict(item) for item in data.get("top_items", [])), json_path.with_suffix(".html"), data.get("update_time"))
        count += 1
    return count


async def run() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    async with AIDailyScraper() as scraper:
        items = await scraper.scrape_all()
    if not items:
        existing_path = ROOT / "news_data.json"
        if existing_path.exists():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            items = [NewsItem.from_dict(item) for item in existing.get("top_items", [])]
            print("  [WARN] 所有来源暂不可用，沿用最近一次日报，避免生成空日报")
    today = datetime.now().strftime("%Y-%m-%d")
    output_config = AIDailyScraper().config.get("config", {}).get("output", {})
    top_items = select_top_items(
        items,
        limit=int(output_config.get("daily_limit", 10)),
        industry_target=int(output_config.get("industry_target", 6)),
        finance_target=int(output_config.get("finance_target", 4)),
    )
    now = datetime.now().isoformat()
    save_json(ROOT / "news_data.json", {"update_time": now, "total_items": len(items), "top_items": [item.to_dict() for item in top_items]})
    save_json(HISTORY_DIR / f"report_{today}.json", {"date": today, "update_time": now, "total_items": len(top_items), "top_items": [item.to_dict() for item in top_items]})
    generate_html(top_items, ROOT / "index.html", now.replace("T", " ").split(".")[0])
    render_history()
    print(f"[OK] 已生成 Lemon News：{len(top_items)} 条精选，历史页面已重渲染")


if __name__ == "__main__":
    asyncio.run(run())
