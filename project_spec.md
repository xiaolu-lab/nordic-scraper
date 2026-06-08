# Nordic 官网新产品/新技术监控代理需求文档

## 1. 项目目标
创建一个自动化脚本，定期监控 Nordic Semiconductor (Nordic 半导体) 官方网站的动态。提取过去两周内发布的新产品（Products）或新技术新闻（News/Press Releases），对其进行简单整理，并通过电子邮件发送给指定用户。

## 2. 技术栈建议
* **编程语言**：Python 3
* **依赖库**：
    * `requests` / `BeautifulSoup4` (用于网页抓取) 或者 `feedparser` (如果 Nordic 有 RSS 源)
    * `smtplib` / `email.mime` (用于构建和发送邮件)
* **自动化部署**：GitHub Actions (使用 `schedule` cron 触发器)

## 3. 核心功能逻辑
1.  **数据获取**：
    * 访问 Nordic 官网的 News 页面 (例如 `https://www.nordicsemi.com/News` 或 press release 页面)。
    * 抓取文章的标题、发布日期、简短摘要和原文链接。要中文,没有中文就自己翻译下.
2.  **数据过滤**：
    * 解析抓取到的发布日期。
    * 只保留发布时间在**过去 14 天内**的条目。
3.  **内容整理 (格式化)**：
    * 如果没有新内容，发送一封写有“过去两周无新动态”的简报。
    * 如果有新内容，使用 HTML 或整洁的纯文本格式将它们拼接起来。
4.  **邮件发送**：
    * 使用 SMTP 发送邮件（例如 Gmail SMTP）。
    * 发件人账号密码、收件人邮箱必须从**环境变量 (Environment Variables)** 中读取，严禁硬编码在代码中。

## 4. 文件结构要求
请为我生成以下文件：
* `scraper.py`: 包含抓取、过滤和邮件发送逻辑的主脚本。
* `requirements.txt`: Python 依赖清单。
* `.github/workflows/schedule.yml`: GitHub Actions 配置文件，设置为每两周执行一次。

## 5. 注意事项
* 代码需要有完善的错误处理（例如：网站结构改变导致抓取失败时，应在日志中报错并尝试发送报警邮件）。
* 抓取时请添加合适的 `User-Agent` 请求头，避免被反爬虫机制拦截。