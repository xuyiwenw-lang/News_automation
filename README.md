# AI 新闻自动清洗

在 **RSS + n8n** 收集原始新闻（raw news）后，通过本脚本完成 **拆分 → 去重 → 评分**，辅助筛选和产出每日热点 AI 新闻。

使用 Python、飞书多维表格和 DeepSeek，支持 GitHub Actions 每日运行，结果写回飞书。

## 工作流程

```mermaid
flowchart LR
    RAW[RSS + n8n 收集原始新闻]

    subgraph SPLIT[拆分]
        direction TB
        A[读取并拆分待处理新闻] --> B[保存拆分计划]
    end

    subgraph DEDUP[去重]
        direction TB
        C[按清洗键写入或复用目标记录]
        C --> D[跨公司语义去重]
        D --> E[合并内容和历史来源]
        E --> F[确认合并成功后删除重复行]
    end

    subgraph SCORE[评分]
        direction TB
        G[AI 评分] --> H[标记源记录已清洗]
    end

    OUTPUT[按评分筛选每日热点 AI 新闻]
    RAW --> SPLIT --> DEDUP --> SCORE --> OUTPUT

    classDef splitNode fill:#DBEAFE,stroke:#3B82F6,color:#1E3A8A,stroke-width:1.5px,rx:8,ry:8
    classDef dedupNode fill:#D1FAE5,stroke:#10B981,color:#064E3B,stroke-width:1.5px,rx:8,ry:8
    classDef scoreNode fill:#EDE9FE,stroke:#8B5CF6,color:#4C1D95,stroke-width:1.5px,rx:8,ry:8
    classDef endpoint fill:#F1F5F9,stroke:#94A3B8,color:#334155,stroke-width:1.5px,rx:12,ry:12

    class A,B splitNode
    class C,D,E,F dedupNode
    class G,H scoreNode
    class RAW,OUTPUT endpoint

    style SPLIT fill:#EFF6FF,stroke:#93C5FD,color:#1E40AF,stroke-width:2px
    style DEDUP fill:#ECFDF5,stroke:#6EE7B7,color:#065F46,stroke-width:2px
    style SCORE fill:#F5F3FF,stroke:#C4B5FD,color:#5B21B6,stroke-width:2px
    linkStyle default stroke:#64748B,stroke-width:1.5px
```

| 阶段 | 处理内容 |
| --- | --- |
| 拆分 | 将日报、周报等合集拆成独立新闻，补全公司与分类 |
| 去重 | 识别同一事件，合并摘要与来源链接，删除重复记录 |
| 评分 | 按新闻价值评为 1–5 分，供后续筛选热点 |

## 快速开始

### 1. 准备表格与密钥

需要 **Python 3.11+**、**DeepSeek API Key**，以及飞书应用可访问的源表和目标表。

先按 [表格配置指南](docs/configuration.md#配置表格) 创建字段，包含用于中断恢复的辅助字段；目标表的“评分”须为数字类型。

### 2. 安装与配置

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，填写飞书应用凭据、源表与目标表 ID，以及 DeepSeek API Key。完整变量说明见 [配置指南](docs/configuration.md#本地运行)。

### 3. 运行

在 bash / zsh 中加载配置并启动：

```bash
set -a
source .env
set +a
python main.py
```

> 当前已通过离线测试，尚未完成真实表格联调。首次运行请使用有效配置和副本表；原代码中曾明文保存的应用密钥需先更换。

## 每日自动运行

默认每天 **北京时间 09:17（UTC 01:17）** 执行完整流程。

1. 将代码上传到 GitHub 仓库的默认分支。
2. 在 **Settings → Secrets and variables → Actions** 添加 `.env.example` 中的 6 个必填密钥配置。
3. 在 **Actions → Daily news cleaning → Run workflow** 手动运行一次，确认结果。

运行时间在 [daily-clean.yml](.github/workflows/daily-clean.yml) 中调整。Lark 地址、回溯窗口及调度注意事项见 [自动运行配置](docs/configuration.md#github-每日自动运行)。

## 测试与维护

```bash
python -m unittest discover -s tests -v
```

离线测试不需要密钥，也不调用真实服务。失败后可重跑，程序通过保存的计划和清洗键恢复进度；请勿同时从多个环境运行同一张表。

| 想要了解或调整 | 查看位置 |
| --- | --- |
| 分类与评分规则 | [editorial.py](editorial.py) |
| 字段、环境变量与部署 | [配置指南](docs/configuration.md) |
| 失败重试、数据迁移与处理上限 | [中断恢复与限制](docs/configuration.md#中断恢复与限制) |
| 模块职责 | [项目结构](docs/configuration.md#项目结构) |

密钥和本地历史数据已通过 `.gitignore` 排除，不随正常 Git 提交上传。
