import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import skill


def item(title: str, source: str, track: str, score: int = 8) -> skill.NewsItem:
    return skill.NewsItem(
        title=title,
        source=source,
        url=f"https://example.com/{len(title)}",
        publish_time="2026-09-03",
        category="AI × 金融" if track == "finance" else "AI 产业",
        track=track,
        summary="金融科技投研与量化风控进展" if track == "finance" else "AI 产品和技术进展",
        importance_score=score,
        tags=skill.classify_news(title, "", "AI × 金融" if track == "finance" else "AI 产业", track),
    )


class SkillTests(unittest.TestCase):
    def test_finance_keywords_raise_score(self):
        plain, _ = skill.calc_score("AI 产品发布", "InfoQ", "industry", "")
        finance, _ = skill.calc_score("AI 投研产品发布", "InfoQ", "finance", "量化风控")
        self.assertGreater(finance, plain)
        self.assertLessEqual(finance, 10)

    def test_select_top_items_targets_mix_and_diversity(self):
        candidates = [
            item(f"产业新闻 {index}", "量子位" if index < 4 else "InfoQ", "industry", 10 - index % 3)
            for index in range(8)
        ] + [
            item(f"金融新闻 {index}", "财联社" if index < 3 else "界面新闻", "finance", 10 - index % 2)
            for index in range(6)
        ]
        selected = skill.select_top_items(candidates)
        self.assertEqual(len(selected), 10)
        self.assertEqual(sum(item.track == "industry" for item in selected), 6)
        self.assertEqual(sum(item.track == "finance" for item in selected), 4)
        self.assertGreaterEqual(len({item.source for item in selected}), 3)

    def test_track_filter_markup_is_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report_2026-09-03.html"
            skill.generate_html([item("产业新闻", "量子位", "industry"), item("金融新闻", "财联社", "finance")], output)
            html = output.read_text(encoding="utf-8")
            self.assertIn('data-track="industry"', html)
            self.assertIn('data-track="finance"', html)
            self.assertIn("AI × 金融", html)
            self.assertIn("const applyFilters", html)
            self.assertIn('id="news-search"', html)
            self.assertIn('id="source-filter"', html)
            self.assertIn('id="score-filter"', html)
            self.assertIn('id="date-filter"', html)
            self.assertIn('id="theme-toggle"', html)
            self.assertIn('cdn.jsdelivr.net/npm/echarts@5', html)
            self.assertIn('id="keyword-chart"', html)
            subprocess.run(["node", "-e", "const fs=require('fs'); const t=fs.readFileSync(process.argv[1],'utf8'); for (const s of t.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)) new Function(s[1]);", str(output)], check=True)

    def test_finance_source_failure_is_empty_and_non_fatal(self):
        scraper = skill.AIDailyScraper.__new__(skill.AIDailyScraper)
        scraper.session = object()
        scraper.fetch_html = AsyncMock(return_value="")
        result = asyncio.run(scraper.fetch_source({"name": "财联社", "url": "https://www.cls.cn/telegraph", "track": "finance"}))
        self.assertEqual(result, [])

    def test_finance_source_builds_finance_items(self):
        scraper = skill.AIDailyScraper.__new__(skill.AIDailyScraper)
        scraper.session = object()
        scraper.fetch_html = AsyncMock(return_value='<h2><a href="/detail/123">AI 投研平台上线，支持量化风控</a></h2>')
        scraper.fetch_article_content = AsyncMock(return_value=("平台为机构提供量化投研和风控工具，首批客户已经接入。", "2026-09-03"))
        result = asyncio.run(scraper.fetch_source({"name": "财联社", "url": "https://www.cls.cn/telegraph", "track": "finance", "category": "AI × 金融", "selector": "h2 a"}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].track, "finance")
        self.assertIn("AI × 金融", result[0].tags)

    def test_old_json_defaults_to_industry_track(self):
        restored = skill.NewsItem.from_dict({"title": "AI 产品发布", "source": "InfoQ", "url": "https://example.com"})
        self.assertEqual(restored.track, "industry")
        self.assertTrue(restored.tags)

    def test_dashscope_without_key_uses_rule_metadata(self):
        news = item("OpenAI 投资方公布新产品", "量子位", "industry")
        with patch.dict(os.environ, {}, clear=True):
            enricher = skill.DashScopeEnricher()
            enriched = asyncio.run(enricher.enrich(news, None))
        self.assertEqual(enriched.companies, ["OpenAI"])
        self.assertEqual(enriched.products, [])

    def test_parse_llm_json_accepts_fenced_response(self):
        parsed = skill.parse_llm_json('```json\n{"summary":"客观摘要","tags":["大模型"]}\n```')
        self.assertEqual(parsed["summary"], "客观摘要")
        self.assertEqual(parsed["tags"], ["大模型"])

    def test_llm_enrichment_keeps_finance_track_tag(self):
        news = item("AI 投研平台", "财联社", "finance")
        enriched = skill.apply_llm_enrichment(news, {"summary": "机构接入量化风控平台", "tags": ["产品动态"]})
        self.assertIn("AI × 金融", enriched.tags)


if __name__ == "__main__":
    unittest.main()
