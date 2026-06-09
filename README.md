# 慧研 · A 股投资分析工作台

> 构建时间：2026-06-10 00:13:33

## 运行环境

| 环境 | 版本 | 下载 |
|------|------|------|
| Node.js | 20.9+ | https://nodejs.org/ |
| Python | 3.10+ | https://www.python.org/downloads/ |
| pnpm | 9.x（`corepack enable` 自动启用） | — |

## 快速启动

```bash
bash start.sh
```

首次启动自动创建 Python venv 并安装依赖，之后直接启动前后端。

按 `Ctrl+C` 停止。

## 选项

```bash
bash start.sh --frontend-only   # 仅前端 :3000
bash start.sh --backend-only    # 仅后端 :8888
```

## 目录

```
├── start.sh          # 一键启动
├── README.md
├── app-dist/         # 前端 (Next.js standalone)
│   ├── server.js
│   └── .next/static/
├── server/           # 后端 (Python FastAPI)
│   ├── app/
│   ├── data/         # SQLite 数据库（启动时自动创建）
│   └── requirements.txt
└── vendor/           # venv（启动时创建）
```

## 端口

| 服务 | 地址 |
|------|------|
| 前端 | http://127.0.0.1:3000 |
| API | http://127.0.0.1:8888 |
| 文档 | http://127.0.0.1:8888/docs |

## 常见问题

**启动报错 "缺少运行环境"？**

按提示去 https://nodejs.org/ 和 https://www.python.org/downloads/ 下载安装对应版本。

**macOS / Windows 能用吗？**

可以。macOS 直接运行 `bash start.sh`；Windows 需要 Git Bash（安装 Git for Windows 自带）。
