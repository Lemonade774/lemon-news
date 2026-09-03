#!/usr/bin/env python3
"""Lemon News daily collector and static report generator."""

from __future__ import annotations

import asyncio
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
HOT_KEYWORDS = ["发布", "推出", "突破", "重磅", "首次", "开源", "融资"]
FUN_KEYWORDS = ["黄仁勋", "马斯克", "LeCun", "华为", "阿里", "字节", "腾讯", "离职", "创业", "融资"]


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        values = {key: data[key] for key in ("title", "source", "url", "publish_time", "category", "summary", "importance_score", "keywords") if key in data}
        values.setdefault("title", "")
        values.setdefault("source", "未知来源")
        values.setdefault("url", "")
        values.setdefault("publish_time", "")
        values.setdefault("category", "AI 综合")
        values.setdefault("summary", "")
        values.setdefault("importance_score", 5)
        values.setdefault("keywords", [])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_ai_related(text: str) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in AI_KEYWORDS)


def calc_score(title: str, source: str) -> tuple[int, int]:
    source_weights = {"量子位": 9, "机器之心": 9, "InfoQ": 8, "界面新闻": 8, "虎嗅网": 8}
    score = max(6, source_weights.get(source, 5))
    score += 0.5 * sum(keyword in title for keyword in HOT_KEYWORDS)
    fun_score = min(10, 2 * sum(keyword in title for keyword in FUN_KEYWORDS))
    return min(int(score), 10), fun_score


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
        page = await self.fetch_html(source.get("url", ""))
        if not page:
            return []
        soup = BeautifulSoup(page, "lxml")
        items: list[NewsItem] = []
        for link in soup.select(source.get("selector", "h2 a, h3 a"))[:25]:
            title = link.get_text(" ", strip=True)
            if not (8 <= len(title) <= 140) or not is_ai_related(title):
                continue
            article_url = urljoin(source.get("url", ""), link.get("href", ""))
            content, publish_time = await self.fetch_article_content(article_url)
            score, _ = calc_score(title, name)
            items.append(NewsItem(title=title, source=name, url=article_url or source.get("url", ""), publish_time=publish_time or datetime.now().strftime("%Y-%m-%d"), category=source.get("category", "AI 综合"), summary=summarize_content(content), importance_score=score))
        print(f"  ✓ {name}: {len(items)} 条")
        return items

    async def scrape_all(self) -> list[NewsItem]:
        print("📰 开始抓取 Lemon News...\n")
        results = await asyncio.gather(*(self.fetch_source(source) for source in self.sources), return_exceptions=True)
        all_items: list[NewsItem] = []
        for result in results:
            if isinstance(result, list):
                all_items.extend(result)
        unique: dict[str, NewsItem] = {}
        for item in all_items:
            unique.setdefault(item.title, item)
        items = sorted(unique.values(), key=lambda item: item.importance_score, reverse=True)
        print(f"\n✅ 共抓取 {len(items)} 条不重复新闻")
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
    in_history = output_path.parent.name == "history"
    home_href = "../index.html" if in_history else "index.html"
    history_href = "report_" if in_history else "history/report_"
    cards = []
    for number, item in enumerate(items, 1):
        score_class = "score-high" if item.importance_score >= 8 else "score-mid" if item.importance_score >= 6 else "score-low"
        safe_url = html.escape(item.url, quote=True)
        cards.append(f'''<article class="news-item"><div class="news-number">{number:02d}</div><div class="news-content"><div class="news-title-row"><h2><a href="{safe_url}" target="_blank" rel="noopener">{html.escape(item.title)}</a></h2><a class="read-more" href="{safe_url}" target="_blank" rel="noopener">阅读原文 ↗</a></div><div class="news-meta"><span class="score {score_class}">热度 {item.importance_score}</span><span class="source">{html.escape(item.source)}</span><time>{html.escape(item.publish_time)}</time></div><p>{html.escape(item.summary or "暂无摘要")}</p></div></article>''')
    links = "".join(f'<a href="{history_href}{html.escape(date)}.html">{html.escape(date)}</a>' for date in history_dates()[:15]) or '<span class="muted">暂无历史日报</span>'
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lemon News · {html.escape(date_only)}</title><style>
:root{{--navy:#18324b;--blue:#2d5a87;--lemon:#f4d35e;--paper:#f7f9fc;--ink:#172333;--muted:#6b7c8f;--line:#dce5ed}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}a{{color:inherit}}.nav{{background:var(--navy);color:#fff}}.nav-inner{{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;gap:28px}}.brand{{font-size:21px;font-weight:750;text-decoration:none}}.brand-mark{{display:inline-grid;place-items:center;width:27px;height:27px;margin-right:8px;background:var(--lemon);color:var(--navy);border-radius:7px;font-weight:900}}.nav-note{{color:#b9cada;font-size:13px}}.layout{{max-width:1180px;margin:30px auto;padding:0 24px;display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:24px}}.hero{{margin-bottom:20px}}.eyebrow{{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.12em}}h1{{font-size:32px;line-height:1.2;margin:7px 0;color:var(--navy)}}.date{{color:var(--muted);margin:0}}.news-item{{display:flex;gap:18px;padding:20px 0;border-top:1px solid var(--line)}}.news-number{{flex:none;width:52px;height:52px;border-radius:10px;background:var(--navy);color:var(--lemon);display:grid;place-items:center;font-weight:800;font-size:18px}}.news-content{{min-width:0;flex:1}}.news-title-row{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}}h2{{font-size:19px;line-height:1.4;margin:0}}h2 a{{text-decoration:none}}h2 a:hover{{color:var(--blue)}}.read-more{{color:var(--blue);font-size:13px;white-space:nowrap;text-decoration:none}}.news-meta{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:9px 0 7px;color:var(--muted);font-size:12px}}.score,.source{{padding:2px 8px;border-radius:999px;font-weight:700}}.score-high{{background:#ffe6df;color:#b34124}}.score-mid{{background:#fff1c2;color:#886a00}}.score-low{{background:#e5eef7;color:var(--blue)}}.source{{background:#e9eef3;color:var(--blue);font-weight:600}}.news-content p{{margin:0;color:#526273}}aside{{position:sticky;top:20px;height:max-content;background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px}}aside h3{{margin:0 0 12px;color:var(--navy);font-size:15px}}aside a{{display:block;padding:7px 0;color:var(--blue);text-decoration:none;border-top:1px solid #edf1f5;font-size:13px}}.muted{{color:var(--muted);font-size:13px}}footer{{max-width:1180px;margin:12px auto 36px;padding:0 24px;color:var(--muted);font-size:12px}}@media(max-width:800px){{.layout{{display:block;margin-top:22px}}aside{{position:static;margin-top:28px}}.nav-inner{{padding:14px 18px}}.layout,footer{{padding-left:18px;padding-right:18px}}h1{{font-size:27px}}.news-title-row{{display:block}}.read-more{{display:inline-block;margin-top:7px}}}}
</style></head><body><nav class="nav"><div class="nav-inner"><a class="brand" href="{home_href}"><span class="brand-mark">L</span>Lemon News</a><span class="nav-note">AI × 金融 · 每日情报站</span></div></nav><main class="layout"><section><header class="hero"><div class="eyebrow">DAILY INTELLIGENCE</div><h1>今日 AI 情报</h1><p class="date">{html.escape(date_only)} · 更新于 {html.escape(updated_at)}</p></header>{''.join(cards) or '<p class="muted">今日暂无可用新闻。</p>'}</section><aside><h3>历史日报</h3>{links}</aside></main><footer>内容来自公开媒体，仅作信息整理与产品研究；请点击原文核验。<br>Lemon News · AI × 金融每日情报站</footer></body></html>'''
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
    today = datetime.now().strftime("%Y-%m-%d")
    top_items = items[:10]
    now = datetime.now().isoformat()
    save_json(ROOT / "news_data.json", {"update_time": now, "total_items": len(items), "top_items": [item.to_dict() for item in top_items]})
    save_json(HISTORY_DIR / f"report_{today}.json", {"date": today, "update_time": now, "total_items": len(top_items), "top_items": [item.to_dict() for item in top_items]})
    generate_html(top_items, ROOT / "index.html", now.replace("T", " ").split(".")[0])
    render_history()
    print(f"💾 已生成 Lemon News：{len(top_items)} 条精选，历史页面已重渲染")


if __name__ == "__main__":
    asyncio.run(run())
