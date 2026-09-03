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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        values = {key: data[key] for key in ("title", "source", "url", "publish_time", "category", "summary", "importance_score", "keywords", "tags") if key in data}
        values.setdefault("title", "")
        values.setdefault("source", "未知来源")
        values.setdefault("url", "")
        values.setdefault("publish_time", "")
        values.setdefault("category", "AI 综合")
        values.setdefault("summary", "")
        values.setdefault("importance_score", 5)
        values.setdefault("keywords", [])
        values["title"] = normalize_title(values["title"])
        values.setdefault("tags", classify_news(values["title"], values["summary"], values["category"]))
        if not values["tags"]:
            values["tags"] = classify_news(values["title"], values["summary"], values["category"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_ai_related(text: str) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in AI_KEYWORDS)


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


def classify_news(title: str, summary: str = "", category: str = "") -> list[str]:
    text = f"{title} {summary} {category}".lower()
    tags = [label for label, keywords in TAG_RULES if any(keyword.lower() in text for keyword in keywords)]
    return tags[:3] or [category or "AI 综合"]


def archive_id(item: NewsItem) -> str:
    return hashlib.sha1(f"{item.url}\x00{item.title}".encode("utf-8")).hexdigest()[:12]


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
            raw_title = link.get_text(" ", strip=True)
            if not (8 <= len(raw_title) <= 140) or not is_ai_related(raw_title):
                continue
            title = normalize_title(raw_title)
            article_url = urljoin(source.get("url", ""), link.get("href", ""))
            content, publish_time = await self.fetch_article_content(article_url)
            score, _ = calc_score(raw_title, name)
            category = source.get("category", "AI 综合")
            summary = summarize_content(content)
            items.append(NewsItem(title=title, source=name, url=article_url or source.get("url", ""), publish_time=publish_time or datetime.now().strftime("%Y-%m-%d"), category=category, summary=summary, importance_score=score, tags=classify_news(title, summary, category)))
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
        tags = item.tags or classify_news(item.title, item.summary, item.category)
        tag_html = "".join(f'<span class="tag">{html.escape(tag)}</span>' for tag in tags)
        item_id = archive_id(item)
        cards.append(f'''<article class="news-item" data-news-id="{item_id}"><div class="news-number">{number:02d}</div><div class="news-content"><div class="news-title-row"><h2><a href="{safe_url}" target="_blank" rel="noopener">{html.escape(item.title)}</a></h2><a class="read-more" href="{safe_url}" target="_blank" rel="noopener">阅读原文 ↗</a></div><div class="news-meta"><span class="score {score_class}">热度 {item.importance_score}</span><span class="source">{html.escape(item.source)}</span><time>{html.escape(item.publish_time)}</time><button class="archive-btn" type="button" data-archive-id="{item_id}" data-title="{html.escape(item.title, quote=True)}" data-url="{safe_url}" data-source="{html.escape(item.source, quote=True)}" title="归档新闻" aria-label="归档新闻">☆</button></div><div class="tags">{tag_html}</div><p>{html.escape(item.summary or "暂无摘要")}</p></div></article>''')
    links = "".join(f'<a href="{history_href}{html.escape(date)}.html">{html.escape(date)}</a>' for date in history_dates()[:15]) or '<span class="muted">暂无历史日报</span>'
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lemon News · {html.escape(date_only)}</title><style>
:root{{--navy:#18324b;--blue:#2d5a87;--lemon:#f4d35e;--paper:#f7f9fc;--ink:#172333;--muted:#6b7c8f;--line:#dce5ed}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}a{{color:inherit}}button{{font:inherit}}.nav{{background:var(--navy);color:#fff}}.nav-inner{{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;gap:28px}}.brand{{font-size:21px;font-weight:750;text-decoration:none;display:inline-flex;align-items:center}}.brand-mark{{position:relative;display:inline-block;width:30px;height:23px;margin:0 9px 0 1px;background:var(--lemon);border-radius:54% 46% 50% 48%;transform:rotate(-18deg);box-shadow:inset -4px -3px 0 rgba(190,145,0,.16)}}.brand-mark::before{{content:"";position:absolute;left:7px;top:4px;width:7px;height:4px;border-radius:50%;background:rgba(255,255,255,.52);transform:rotate(-18deg)}}.brand-mark::after{{content:"";position:absolute;right:-3px;top:-7px;width:10px;height:6px;border-radius:90% 10% 90% 10%;background:#79a83b;transform:rotate(24deg)}}.nav-note{{color:#b9cada;font-size:13px}}.layout{{max-width:1180px;margin:30px auto;padding:0 24px;display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:24px}}.hero{{margin-bottom:20px;text-align:center}}.eyebrow{{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.12em}}h1{{font-size:32px;line-height:1.2;margin:7px 0;color:var(--navy)}}.date{{color:var(--muted);margin:0}}.news-item{{display:flex;gap:18px;padding:20px 0;border-top:1px solid var(--line)}}.news-number{{flex:none;width:52px;height:52px;border-radius:10px;background:var(--navy);color:var(--lemon);display:grid;place-items:center;font-weight:800;font-size:18px}}.news-content{{min-width:0;flex:1}}.news-title-row{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}}h2{{font-size:19px;line-height:1.4;margin:0}}h2 a{{text-decoration:none}}h2 a:hover{{color:var(--blue)}}.read-more{{color:var(--blue);font-size:13px;white-space:nowrap;text-decoration:none}}.news-meta{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:9px 0 5px;color:var(--muted);font-size:12px}}.score,.source,.tag{{padding:2px 8px;border-radius:999px;font-weight:700}}.score-high{{background:#ffe6df;color:#b34124}}.score-mid{{background:#fff1c2;color:#886a00}}.score-low{{background:#e5eef7;color:var(--blue)}}.source{{background:#e9eef3;color:var(--blue);font-weight:600}}.tags{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}}.tag{{background:#edf5d0;color:#56701d;font-size:11px;font-weight:650}}.archive-btn{{margin-left:auto;border:0;background:transparent;color:#8ca0b3;font-size:22px;line-height:1;cursor:pointer;padding:0 2px}}.archive-btn.is-archived{{color:#d69c00}}.news-content p{{margin:0;color:#526273}}aside{{position:sticky;top:20px;height:max-content;background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px}}aside h3{{margin:0 0 12px;color:var(--navy);font-size:15px}}aside a{{display:block;padding:7px 0;color:var(--blue);text-decoration:none;border-top:1px solid #edf1f5;font-size:13px}}.archive-panel{{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}}.archive-panel h3{{margin-bottom:9px}}.archive-controls{{display:flex;gap:6px;margin-bottom:10px}}.archive-controls input,.archive-controls select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:6px;padding:6px 7px;color:var(--ink);background:#fff}}.archive-controls button,.archive-remove{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--blue);cursor:pointer;padding:5px 8px}}.archive-list{{display:grid;gap:8px}}.archive-entry{{padding:8px;background:var(--paper);border-radius:7px;font-size:12px}}.archive-entry a{{border:0;padding:0;font-weight:650}}.archive-entry small{{display:block;color:var(--muted);margin:3px 0 5px}}.archive-entry-row{{display:flex;gap:5px}}.archive-entry select{{min-width:0;flex:1;border:1px solid var(--line);border-radius:5px;font-size:11px}}.archive-remove{{color:#a34b3d;font-size:11px}}.muted{{color:var(--muted);font-size:13px}}footer{{max-width:1180px;margin:12px auto 36px;padding:0 24px;color:var(--muted);font-size:12px}}@media(max-width:800px){{.layout{{display:block;margin-top:22px}}aside{{position:static;margin-top:28px}}.nav-inner{{padding:14px 18px;flex-wrap:wrap;gap:8px 18px}}.layout,footer{{padding-left:18px;padding-right:18px}}h1{{font-size:27px}}.news-title-row{{display:block}}.read-more{{display:inline-block;margin-top:7px}}}}
</style></head><body><nav class="nav"><div class="nav-inner"><a class="brand" href="{home_href}"><span class="brand-mark" aria-hidden="true"></span>Lemon News</a><span class="nav-note">AI × 金融 · 每日情报站</span></div></nav><main class="layout"><section><header class="hero"><div class="eyebrow">DAILY INTELLIGENCE</div><h1>今日 AI 情报</h1><p class="date">{html.escape(date_only)} · 更新于 {html.escape(updated_at)}</p></header>{''.join(cards) or '<p class="muted">今日暂无可用新闻。</p>'}</section><aside><h3>历史日报</h3>{links}<section class="archive-panel" id="archive-panel"><h3>我的归档</h3><div class="archive-controls"><select id="archive-filter" aria-label="归档分组"><option value="*">全部分组</option></select></div><form class="archive-controls" id="archive-group-form"><input id="archive-group-input" maxlength="24" placeholder="新建分组" aria-label="新建分组"><button type="submit" title="新建分组">+</button></form><div class="archive-list" id="archive-list"><span class="muted">暂无归档新闻</span></div></section></aside></main><footer>内容来自公开媒体，仅作信息整理与产品研究；请点击原文核验。<br>Lemon News · AI × 金融每日情报站</footer><script>
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
  const render = () => {{
    filter.innerHTML = '<option value="*">全部分组</option>' + state.groups.map(group => `<option value="${{escapeHtml(group)}}">${{escapeHtml(group)}}</option>`).join('');
    const selected = filter.value || '*';
    const entries = Object.entries(state.items).filter(([, item]) => selected === '*' || item.group === selected);
    list.innerHTML = entries.length ? entries.map(([id, item]) => `<div class="archive-entry" data-entry-id="${{escapeHtml(id)}}"><a href="${{escapeHtml(safeUrl(item.url))}}" target="_blank" rel="noopener">${{escapeHtml(item.title)}}</a><small>${{escapeHtml(item.source)}}</small><div class="archive-entry-row"><select class="archive-entry-group" aria-label="归档分组">${{state.groups.map(group => `<option value="${{escapeHtml(group)}}" ${{group === item.group ? 'selected' : ''}}>${{escapeHtml(group)}}</option>`).join('')}}</select><button class="archive-remove" type="button" title="移除归档">移除</button></div></div>`).join('') : '<span class="muted">暂无归档新闻</span>';
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
  document.getElementById('archive-group-form').addEventListener('submit', event => {{ event.preventDefault(); const input = document.getElementById('archive-group-input'); const group = input.value.trim(); if (group && !state.groups.includes(group)) {{ state.groups.push(group); input.value = ''; persist(); render(); }} }});
  render();
}})();
</script></body></html>'''
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
