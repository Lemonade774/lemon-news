# Lemon News

**AI × 金融每日情报站**，面向产品经理、投资研究和 AI 从业者的静态日报产品。

Lemon News 每天从公开媒体抓取 AI 产业与金融科技新闻，进行关键词过滤、来源加权和热度排序，生成可追溯的 JSON 数据、当日 HTML 报告和历史归档。项目保留 GitHub Actions + GitHub Pages 的零成本自动化链路，重点展示信息筛选、内容产品设计和数据敏感度。

## 当前能力

- 异步抓取量子位、InfoQ、界面新闻、虎嗅网等公开页面
- AI 关键词过滤、来源权重、热词和融资等标题信号评分
- 每日精选 Top 10，并保存 `news_data.json`
- 生成 `history/report_YYYY-MM-DD.html` 与对应 JSON，支持历史追溯
- Lemon News 品牌化静态页面，移动端自适应
- 抓取失败时记录日志并继续处理其他来源，不静默提交空日报

## 目录结构

```text
.
├── skill.py                 # 抓取、评分、归档和 HTML 生成
├── skill.json               # 数据源与输出配置
├── index.html               # 最新日报
├── news_data.json           # 最新日报数据
├── history/                 # 历史日报 HTML/JSON
└── .github/workflows/       # 每日更新、健康检查和 Pages 部署
```

## 本地运行

需要 Python 3.10+：

```bash
python -m pip install aiohttp beautifulsoup4 lxml
python skill.py
```

运行后会更新首页、`news_data.json` 和当天历史归档。摘要目前使用可审计的规则降级逻辑；后续大模型摘要会通过环境变量接入。

## 自动化部署

仓库使用 GitHub Actions 每天 UTC 00:00（北京时间 08:00）更新日报，也支持手动触发。推送到 `main` 后，GitHub Pages 工作流发布静态文件。

预计访问地址：<https://lemonade774.github.io/lemon-news/>

## 环境变量与安全

大模型能力启用后使用 `DASHSCOPE_API_KEY` 环境变量或 GitHub Actions Secret。任何 API Key、Token、Webhook 都不得写入源码、配置文件、日志或提交历史。

## 数据与版权

项目只保存公开页面的标题、链接和改写后的要点摘要；原文版权归各媒体所有。摘要用于信息整理和产品研究，阅读时请回到来源页面核验。

## 后续路线

1. M2：AI 产业与 AI×金融双轨内容、金融数据源和频道筛选
2. M3：大模型摘要与自动标签
3. M4：全文搜索、筛选、主题切换和 ECharts 可视化
4. M5：GitHub Pages 全链路验证与公开上线
