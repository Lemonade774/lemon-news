# Lemon News

**AI × 金融每日情报站**，面向产品经理、投资研究和 AI 从业者的静态日报产品。

Lemon News 每天从公开媒体抓取 AI 产业与金融科技新闻，进行关键词过滤、来源加权和热度排序，生成可追溯的 JSON 数据、当日 HTML 报告和历史归档。项目保留 GitHub Actions + GitHub Pages 的零成本自动化链路，重点展示信息筛选、内容产品设计和数据敏感度。

## 当前能力

- 异步抓取量子位、InfoQ、界面新闻、虎嗅网和财联社公开页面
- AI 产业与 AI × 金融双轨数据模型，默认按约 6/4 比例混排，单轨失败时从另一轨补齐
- AI 关键词过滤、来源权重、热词和融资等标题信号评分
- 金融关键词（投研、量化、风控、支付、财富管理、合规等）相关性加分，最终热度不超过 10 分
- 可选 DashScope 结构化摘要与实体提取（公司、产品、融资），失败时自动回退规则摘要
- 每日精选 Top 10，并保存 `news_data.json`
- 自动将标题压缩为主题、事件、结果导向的短标题，并生成大模型、Agent、AI 应用、公司动态、融资与投资、具身智能等标签
- 生成 `history/report_YYYY-MM-DD.html` 与对应 JSON，支持历史追溯
- Lemon News 品牌化静态页面，移动端自适应
- 首页支持按内容轨道和新闻标签组合筛选，标题、热度、来源和日期采用居中信息层级
- 支持本地全文搜索、来源/热度/日期组合筛选，以及浏览器记忆的亮色/暗色主题
- 通过 ECharts CDN 展示标签热度图；行情暂无公开数据时显示明确空状态
- 浏览器本地归档：收藏新闻、创建分组、移动分组、删除和按组查看
- 抓取失败时记录日志并继续处理其他来源；所有来源暂不可用时沿用最近日报，不静默提交空日报

## 目录结构

```text
.
├── skill.py                 # 抓取、评分、归档和 HTML 生成
├── skill.json               # 数据源、轨道与输出配置
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

运行后会更新首页、`news_data.json` 和当天历史归档。没有配置 Key 时使用可审计的规则摘要；配置后会通过 DashScope 兼容接口生成结构化摘要。财联社等金融页面可能受反爬或动态渲染影响，失败会记录警告并继续生成产业内容。

页面中的“我的归档”使用浏览器 `localStorage` 保存，适合个人整理和演示；它不会写回 GitHub 仓库。需要团队共享或长期保存时，应把归档数据导出后再纳入仓库或接入后端服务。

## 自动化部署

仓库使用 GitHub Actions 每天 UTC 00:00（北京时间 08:00）更新日报，也支持手动触发。推送到 `main` 后，GitHub Pages 工作流发布静态文件。

预计访问地址：<https://lemonade774.github.io/lemon-news/>

## 环境变量与安全

大模型能力启用后使用以下环境变量或 GitHub Actions Secret：

- `DASHSCOPE_API_KEY`：必填密钥，不写入仓库
- `DASHSCOPE_BASE_URL`：可选，默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `DASHSCOPE_MODEL`：可选，默认 `qwen-turbo`
- `DASHSCOPE_TIMEOUT`：可选，单次请求超时秒数，默认 `12`

任何 API Key、Token、Webhook 都不得写入源码、配置文件、日志或提交历史。

## 数据与版权

项目只保存公开页面的标题、链接和改写后的要点摘要；原文版权归各媒体所有。摘要用于信息整理和产品研究，阅读时请回到来源页面核验。

## 后续路线

1. M2：AI 产业与 AI×金融双轨内容、金融数据源和频道筛选（已完成本地实现）
2. M3：大模型摘要与自动标签（已完成本地实现）
3. M4：全文搜索、筛选、主题切换和 ECharts 可视化（已完成本地实现）
4. M5：GitHub Pages 全链路验证与公开上线
