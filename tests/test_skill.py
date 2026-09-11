import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
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
        topic=skill.infer_topic(title, "金融科技投研与量化风控进展" if track == "finance" else "AI 产品和技术进展", track),
    )


class SkillTests(unittest.TestCase):
    def test_daily_selection_prefers_today_over_hotter_old_news(self):
        today = item("AI application launches today", "InfoQ", "industry", 6)
        today.publish_time = "2026-09-10"
        old = item("OpenAI major model release", "OpenAI", "industry", 10)
        old.publish_time = "2026-09-09"
        selected = skill.select_daily_items([old, today], date(2026, 9, 10), limit=1)
        self.assertEqual(selected, [today])

    def test_daily_selection_excludes_recent_history(self):
        current = item("OpenAI releases GPT model", "OpenAI", "industry", 10)
        current.publish_time = "2026-09-10"
        current.url = "https://openai.com/news/gpt?utm_source=feed"
        previous = item("OpenAI releases GPT model", "AIHub", "industry", 9)
        previous.publish_time = "2026-09-09"
        previous.url = "https://openai.com/news/gpt"
        self.assertEqual(skill.select_daily_items([current], date(2026, 9, 10), [previous]), [])

    def test_daily_selection_rejects_unknown_and_older_than_72_hours(self):
        unknown = item("AI news without a date", "InfoQ", "industry", 10)
        unknown.publish_time = ""
        old = item("AI news from last week", "InfoQ", "industry", 10)
        old.publish_time = "2026-09-06"
        self.assertEqual(skill.select_daily_items([unknown, old], date(2026, 9, 10)), [])

    def test_daily_selection_marks_recent_backfill(self):
        recent = item("Agent platform update", "InfoQ", "industry", 8)
        recent.publish_time = "2026-09-08"
        selected = skill.select_daily_items([recent], date(2026, 9, 10))
        self.assertTrue(selected[0].is_backfill)
        self.assertEqual(selected[0].selection_reason, "recent_backfill")

    def test_daily_selection_fills_to_ten_from_previous_report_top(self):
        fresh = []
        for index in range(3):
            news = item(f"今日 AI 事件 {index}", f"today-{index}", "industry", 7)
            news.url = f"https://example.com/today/{index}"
            news.publish_time = "2026-09-10"
            fresh.append(news)
        previous = []
        for index in range(10):
            news = item(f"前日 AI 事件 {index}", f"previous-{index}", "industry", 10 - index // 3)
            news.url = f"https://example.com/previous/{index}"
            news.publish_time = "2026-09-09"
            previous.append(news)
        selected = skill.select_daily_items(fresh, date(2026, 9, 10), previous_day_items=previous, limit=10)
        self.assertEqual(len(selected), 10)
        self.assertEqual(selected[:3], fresh)
        self.assertTrue(all(news.selection_reason == "previous_day_top" for news in selected[3:]))
        self.assertEqual([news.importance_score for news in selected[3:]], sorted((news.importance_score for news in selected[3:]), reverse=True))

    def test_daily_selection_rejects_stale_items_from_previous_report(self):
        stale = item("一个月前的 AI 新闻", "InfoQ", "industry", 10)
        stale.publish_time = "2026-08-08"
        selected = skill.select_daily_items([], date(2026, 9, 10), previous_day_items=[stale], limit=10)
        self.assertEqual(selected, [])

    def test_english_headline_is_localized_but_model_name_is_preserved(self):
        title = skill.normalize_title("How GPT-5.6 Sol helps run quantum computing experiments")
        self.assertEqual(title, "GPT-5.6 Sol 如何辅助量子计算实验")

    def test_ai_finance_workflow_bad_case_gets_finance_track_and_high_score(self):
        title = "蚂蚁的首个金融增强模型进入AI投研工作流"
        self.assertEqual(skill.infer_track(title), "finance")
        self.assertEqual(skill.infer_topic(title, track="finance"), "ai_finance")
        score, _ = skill.calc_score(title, "量子位", "finance")
        self.assertGreaterEqual(score, 8)

    def test_substantive_version_update_can_reappear(self):
        previous = item("Model V1.2 release", "OpenAI", "industry", 9)
        previous.url = "https://example.com/model"
        previous.publish_time = "2026-09-09"
        current = item("Model V1.3 release", "OpenAI", "industry", 9)
        current.url = previous.url
        current.publish_time = "2026-09-10"
        self.assertFalse(skill.appeared_in_history(current, [previous]))

    def test_three_daily_runs_have_no_adjacent_duplicates(self):
        previous = []
        reports = []
        start = date(2026, 9, 8)
        for day_index in range(3):
            report_date = start + timedelta(days=day_index)
            candidates = []
            for item_index in range(12):
                news = item(f"AI daily event {day_index}-{item_index}", f"source-{item_index % 4}", "industry", 9 - item_index % 3)
                news.url = f"https://example.com/{day_index}/{item_index}"
                news.publish_time = report_date.isoformat()
                candidates.append(news)
            selected = skill.select_daily_items(candidates, report_date, previous, limit=10)
            self.assertEqual(len(selected), 10)
            reports.append(selected)
            previous.extend(selected)
        for left, right in zip(reports, reports[1:]):
            self.assertFalse({news.url for news in left} & {news.url for news in right})

    def test_history_loader_reads_only_previous_seven_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"top_items": [item("AI previous event", "InfoQ", "industry").to_dict()]}
            (root / "report_2026-09-09.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            (root / "report_2026-09-10.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            (root / "report_2026-09-02.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            loaded = skill.load_recent_history(date(2026, 9, 10), history_dir=root)
        self.assertEqual(len(loaded), 1)

    def test_previous_day_loader_does_not_fall_back_to_older_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = item("旧日报里的 AI 新闻", "InfoQ", "industry")
            stale.publish_time = "2026-08-08"
            (root / "report_2026-09-03.json").write_text(
                json.dumps({"top_items": [stale.to_dict()]}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(skill.load_previous_day_items(date(2026, 9, 10), root), [])

    def test_previous_day_loader_keeps_recent_publications_from_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yesterday = item("昨天发布的 AI 新闻", "InfoQ", "industry")
            yesterday.publish_time = "2026-09-09"
            delayed = item("前日报延迟的 AI 新闻", "量子位", "industry")
            delayed.publish_time = "2026-09-08"
            stale = item("日报中的更早新闻", "InfoQ", "industry")
            stale.publish_time = "2026-08-08"
            (root / "report_2026-09-09.json").write_text(
                json.dumps({"top_items": [stale.to_dict(), yesterday.to_dict(), delayed.to_dict()]}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(skill.load_previous_day_items(date(2026, 9, 10), root), [yesterday, delayed])

    def test_aihub_parser_attributes_external_original_source(self):
        page = '''<article class="news-item"><h2><a href="/news/123">OpenAI 发布新模型</a></h2><p>模型能力和 API 同步开放，开发者可立即使用。</p><span class="source">OpenAI</span><time datetime="2026-09-10"></time><a class="original" href="https://openai.com/news/model">原文</a></article>'''
        result = skill.parse_aihub_page(page)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "OpenAI")
        self.assertEqual(result[0]["url"], "https://openai.com/news/model")
        self.assertEqual(result[0]["publish_time"], "2026-09-10")

    def test_rss_parser_supports_atom(self):
        feed = '''<feed><entry><title>Claude model update improves coding</title><link href="https://anthropic.com/news/update"/><summary>Model capability update.</summary><updated>2026-09-10T08:00:00Z</updated></entry></feed>'''
        result = skill.parse_rss_feed(feed, "Anthropic")
        self.assertEqual(result[0]["source"], "Anthropic")
        self.assertEqual(result[0]["url"], "https://anthropic.com/news/update")
        self.assertEqual(result[0]["publish_time"], "2026-09-10")

    def test_topic_quota_selects_expected_mix(self):
        topics = ["model_release"] * 4 + ["agent"] * 3 + ["ai_application"] * 3 + ["ai_finance"] * 3 + ["compute"] * 2
        candidates = []
        for index, topic in enumerate(topics):
            track = "finance" if topic == "ai_finance" else "industry"
            news = item(f"AI topic news {index}", f"source-{index % 5}", track, 10 - index % 3)
            news.topic = topic
            candidates.append(news)
        selected = skill.select_top_items(candidates, topic_targets={"models": 3, "agent": 2, "applications": 2, "finance": 2, "company_compute": 1})
        self.assertEqual(len(selected), 10)
        self.assertEqual(sum(news.topic == "model_release" for news in selected), 3)
        self.assertEqual(sum(news.topic == "agent" for news in selected), 2)
        self.assertEqual(sum(news.topic == "ai_finance" for news in selected), 2)

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
            official = item("产业新闻", "OpenAI", "industry")
            official.source_type = "official"
            discovered = item("金融新闻", "财联社", "finance")
            discovered.discovery_source = "AIHub"
            discovered.is_backfill = True
            skill.generate_html([official, discovered], output, selection_summary={"today_count": 1, "backfill_count": 1})
            html = output.read_text(encoding="utf-8")
            self.assertIn('data-track="industry"', html)
            self.assertIn('data-track="finance"', html)
            self.assertIn("AI × 金融", html)
            self.assertIn("const runGlobalFilters", html)
            self.assertIn('id="news-search"', html)
            self.assertIn('id="source-filter"', html)
            self.assertIn('id="score-filter"', html)
            self.assertIn('id="date-filter"', html)
            self.assertIn('id="theme-toggle"', html)
            self.assertIn('cdn.jsdelivr.net/npm/echarts@5', html)
            self.assertIn('id="keyword-chart"', html)
            self.assertIn('data-topic=', html)
            self.assertIn('class="source-context">官方', html)
            self.assertIn('class="source-context">via AIHub', html)
            self.assertIn('class="source-context">近期补位', html)
            self.assertIn("今日新增 1 条", html)
            self.assertIn("搜索全部历史日报", html)
            self.assertIn('id="history-search-results"', html)
            self.assertIn("searchIndexHref", html)
            self.assertIn("populateGlobalControls", html)
            self.assertIn("body[data-theme=\"dark\"] .news-content p", html)
            self.assertIn("body[data-theme=\"dark\"] h1", html)
            subprocess.run(["node", "-e", "const fs=require('fs'); const t=fs.readFileSync(process.argv[1],'utf8'); for (const s of t.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)) new Function(s[1]);", str(output)], check=True)

    def test_search_index_covers_all_reports_and_deduplicates_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = item("AI 历史事件", "InfoQ", "industry")
            first.url = "https://example.com/event?utm_source=old"
            newer = item("AI 历史事件更新", "InfoQ", "industry")
            newer.url = "https://example.com/event"
            other = item("金融 Agent 落地", "量子位", "finance")
            other.url = "https://example.com/finance"
            (root / "report_2026-09-09.json").write_text(json.dumps({"date": "2026-09-09", "top_items": [newer.to_dict(), other.to_dict()]}, ensure_ascii=False), encoding="utf-8")
            (root / "report_2026-09-08.json").write_text(json.dumps({"date": "2026-09-08", "top_items": [first.to_dict()]}, ensure_ascii=False), encoding="utf-8")
            index = skill.build_search_index(root)
            self.assertEqual(index["total_items"], 2)
            self.assertEqual(index["items"][0]["report_date"], "2026-09-09")

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

    def test_auto_track_source_recalls_ai_finance_workflow_and_deduplicates_links(self):
        scraper = skill.AIDailyScraper.__new__(skill.AIDailyScraper)
        scraper.session = object()
        scraper.fetch_html = AsyncMock(return_value='''
            <a href="/2026/09/486288.html">蚂蚁的首个金融增强模型进入AI投研工作流</a>
            <a href="/2026/09/486288.html">蚂蚁的首个金融增强模型进入AI投研工作流</a>
            <a href="/2026/09/other.html">OpenAI 发布另一款 AI 模型</a>
        ''')
        scraper.fetch_article_content = AsyncMock(return_value=("该模型进入真实金融投研工作流，支持机构研究和风控场景。", "2026-09-10"))
        result = asyncio.run(scraper.fetch_source({
            "name": "量子位", "url": "https://www.qbitai.com", "track_mode": "auto",
            "category": "AI 产业", "selector": "a[href*='/2026/']", "max_candidates": 50,
        }))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].track, "finance")
        self.assertEqual(result[0].topic, "ai_finance")
        self.assertGreaterEqual(result[0].importance_score, 8)

    def test_finance_source_rejects_non_ai_finance_news(self):
        scraper = skill.AIDailyScraper.__new__(skill.AIDailyScraper)
        scraper.session = object()
        scraper.fetch_html = AsyncMock(return_value='<h2><a href="/detail/123">传统银行季度报表发布</a></h2>')
        scraper.fetch_article_content = AsyncMock(return_value=("银行公布季度经营数据和利润情况。", "2026-09-10"))
        result = asyncio.run(scraper.fetch_source({"name": "财联社", "url": "https://www.cls.cn/telegraph", "track": "finance", "category": "AI × 金融", "selector": "h2 a"}))
        self.assertEqual(result, [])

    def test_same_event_normalizes_tracking_and_title(self):
        left = item("OpenAI发布新模型", "AIHub", "industry")
        right = item("OpenAI 发布新模型", "OpenAI", "industry")
        left.url = "https://openai.com/news/model?utm_source=aihub"
        right.url = "https://openai.com/news/model"
        self.assertTrue(skill.same_event(left, right))

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
