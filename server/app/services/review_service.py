"""AI 策略复盘服务。

通过调用本地 CLI 工具（如 claude、tgpt）对策略扫描结果进行 AI 分析，
支持 SSE 流式输出，并可将结果保存为复盘笔记。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncGenerator

from app.core.sqlite_storage import get_storage
from app.services.strategy_engine import STRATEGIES

# AI 响应验证标记 — 提示词要求 AI 在回复开头原封不动包含此标记，
# 解析时检查是否存在，缺失表示 AI 返回了错误/拒绝/格式异常。
REVIEW_VALIDATION_PREFIX = "<!--REVIEW_VALID:"

# scan_id → 完整验证标记 (<!--REVIEW_VALID:<uuid>-->)
_pending_tokens: dict[str, str] = {}


def _make_validation_token() -> str:
    """生成唯一的响应验证标记。"""
    return f"<!--REVIEW_VALID:{uuid.uuid4().hex}-->"


def get_validation_token(scan_id: str) -> str | None:
    """获取指定 scan 的验证标记，用于响应校验。"""
    return _pending_tokens.get(scan_id)


def clear_validation_token(scan_id: str) -> None:
    """清除验证标记。"""
    _pending_tokens.pop(scan_id, None)

# ---------------------------------------------------------------------------
# CLI 白名单及命令模板
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI 注册表 — 每个 CLI 定义 label 和命令行参数模板
# args 中 {prompt} 会被替换为实际 prompt 文本，作为单独参数传入（无 shell 注入风险）
# ---------------------------------------------------------------------------

# ---- CLI 注册表 — mode 决定 I/O 格式 ----
#   "stream_json": 逐 token 流式（claude 专用，使用 stream-json + include-partial-messages）
#   "text":        按行流式（其他 CLI，prompt 通过 stdin 传入）
CLI_REGISTRY: dict[str, dict] = {
    "claude":    {"label": "Claude (Anthropic CLI)",     "mode": "stream_json",
                  "args": ["--output-format", "stream-json", "--verbose",
                           "--input-format", "stream-json",
                           "--include-partial-messages"]},
    "codex":     {"label": "Codex (OpenAI CLI)",         "mode": "text", "args": ["exec"]},
    "opencode":  {"label": "OpenCode CLI",               "mode": "text", "args": ["run"]},
    "reasonix":  {"label": "Reasonix CLI",               "mode": "text", "args": []},
    "tgpt":      {"label": "tgpt (Terminal GPT)",        "mode": "text", "args": []},
    "llm":       {"label": "llm (Simon Willison)",       "mode": "text", "args": ["-p", "-"]},
    "aider":     {"label": "Aider AI",                   "mode": "text", "args": ["--message", "-"]},
    "gemini":    {"label": "Gemini CLI (Google)",         "mode": "text", "args": []},
    "copilot":   {"label": "GitHub Copilot CLI",         "mode": "text", "args": ["suggest"]},
    "qwen":      {"label": "Qwen CLI (Alibaba)",         "mode": "text", "args": []},
    "deepseek":  {"label": "DeepSeek CLI",               "mode": "text", "args": []},
    "ollama":    {"label": "Ollama CLI",                 "mode": "text", "args": ["run"]},
}


def available_clis() -> list[dict[str, str]]:
    """返回支持的 CLI 列表（label + key），仅包含本地已安装的。"""
    import shutil
    result: list[dict[str, str]] = []
    for key, info in CLI_REGISTRY.items():
        if shutil.which(key) is not None:
            result.append({"key": key, "label": info["label"]})
    return result


def scan_clis() -> list[dict[str, str]]:
    """扫描本地已安装的 CLI 工具（每次调用实时扫描 PATH）。"""
    return available_clis()


# ---------------------------------------------------------------------------
# 策略详情（与前端 STRATEGY_REGISTRY 保持同步）
# ---------------------------------------------------------------------------

STRATEGY_DETAILS: dict[str, dict[str, str]] = {
    "s1": {
        "factors": "8 因子加权评分：多头排列强度 · 趋势健康度 · 回调到支撑 · MACD 趋势 · 量价配合 · 布林方向 · 相对强度 · 板块加分",
        "scenario": "趋势确立后，回调不破支撑",
        "hold_period": "5-20 个交易日",
    },
    "s2": {
        "factors": "7 因子加权评分：RSI 底背离 · 超卖程度 · 缩量止跌 · 价格企稳 · MACD 拐头 · 跌幅充分 · 支撑验证",
        "scenario": "超卖区域出现反转信号",
        "hold_period": "3-10 个交易日",
    },
    "s3": {
        "factors": "8 因子加权评分：突破强度 · 放量确认 · 金叉信号 · RSI 动能 · MACD 金叉 · 蓄势质量 · 布林扩张 · 板块加分",
        "scenario": "蓄势后放量突破关键位",
        "hold_period": "3-15 个交易日",
    },
}


# ---------------------------------------------------------------------------
# Prompt 构建
# ---------------------------------------------------------------------------


def _get_previous_review_summary(scan_id: str) -> str:
    """获取该扫描上次复盘 AI 总结的内容，用于提供给 AI 作为参考基线。

    返回空字符串表示没有上次复盘内容。
    """
    storage = get_storage()

    # 获取该 scan 的所有历史复盘（按时间倒序，只包含已关联笔记的）
    reviews = storage.get_reviews_by_scan(scan_id)
    if not reviews:
        return ""

    # 取最近的一条
    latest = reviews[0]
    note_id = latest.get("note_id")
    if not note_id:
        return ""

    note = storage.get_note(note_id)
    if not note:
        return ""

    # 组装 AI 上次的总结内容
    parts: list[str] = []

    market_obs = (note.get("marketObs") or "").strip()
    if market_obs:
        parts.append(f"### 上次「市场观察」\n\n{market_obs}")

    trade_review = (note.get("tradeReview") or "").strip()
    if trade_review:
        parts.append(f"### 上次「操作复盘」\n\n{trade_review}")

    next_plan = (note.get("nextPlan") or "").strip()
    if next_plan:
        parts.append(f"### 上次「明日计划」\n\n{next_plan}")

    if not parts:
        return ""

    # 添加时间信息
    created_at = note.get("createdAt", "")
    time_info = ""
    if created_at:
        time_info = f"\n\n> 以上总结生成于 {created_at[:10]}，由 {latest.get('cli_tool', 'AI')} 生成。"

    return "\n\n".join(parts) + time_info


def build_review_prompt(scan_id: str, user_operations: str = "") -> str | None:
    """根据 scan_id 组装 AI 复盘的完整 prompt。返回 None 表示数据不可用。

    user_operations: 用户今日操作记录（可选），若提供则要求 AI 一并分析。
    """
    from datetime import date

    storage = get_storage()

    # ---- 获取 scan 状态和结果 ----
    scan = storage.get_scan_status(scan_id)
    if not scan:
        return None

    strategy = scan["strategy"]
    trade_date = scan["trade_date"]
    strategy_name = STRATEGIES.get(strategy, ("未知策略",))[0]
    details = STRATEGY_DETAILS.get(strategy, {})

    results = storage.get_strategy_results(scan_id, strategy)
    analysis = storage.get_scan_analysis(scan_id)

    # ---- 日期计算 ----
    today_str = date.today().strftime("%Y%m%d")

    def _fmt_date(d: str) -> str:
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d

    formatted_trade_date = _fmt_date(trade_date)
    formatted_today = _fmt_date(today_str)

    days_passed: int | None = None
    if len(trade_date) == 8:
        td = date(int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:8]))
        days_passed = (date.today() - td).days

    # ---- 判断是否有绩效数据 ----
    has_performance = bool(analysis and analysis.get("summary"))

    # ---- 组装 Prompt ----
    lines: list[str] = []
    lines.append("# 策略复盘分析任务")
    lines.append("")
    lines.append("你是一位专业的量化策略分析师。请对以下 A 股策略扫描结果进行复盘分析，")
    lines.append("输出「市场观察」和「明日计划」两部分内容。")
    lines.append("")

    # ---- 日期与数据状态（关键上下文） ----
    lines.append("## 日期与数据状态")
    lines.append("")
    lines.append(f"- **策略扫描日期**: {formatted_trade_date}")
    lines.append(f"- **当前复盘日期**: {formatted_today}")
    if days_passed is not None:
        if days_passed == 0:
            lines.append(f"- **交易日验证**: ⚠️ 策略今天刚生成，**尚未经过任何交易日验证**，没有实际收益数据")
        else:
            lines.append(f"- **交易日验证**: 已过去 {days_passed} 个自然日")
    lines.append("")

    if not has_performance:
        lines.append("### ⚠️ 重要：绩效数据不可用")
        lines.append("")
        lines.append("本策略目前**没有策略绩效数据**（平均收益、胜率、最佳/最差标的等均不可用）。")
        if days_passed == 0:
            lines.append("这是因为策略今天刚生成，尚未经过交易日验证，而非策略表现不佳。")
        else:
            lines.append("这可能是因为尚未到绩效计算节点，并非策略表现不佳。")
        lines.append("")
        lines.append("**请注意以下事项：**")
        lines.append("- 扫描结果中的「得分」是策略公式的**技术评分**，代表技术面符合程度，**不是实际收益**")
        lines.append("- **不要**假设策略得分为 0 或策略表现不佳")
        lines.append("- **不要**编造或猜测收益数据")
        lines.append("- 你的分析应聚焦于：技术面逻辑、市场环境适配性、标的个体特征")
        lines.append("")

    # ---- 策略基本信息 ----
    lines.append("## 策略信息")
    lines.append("")
    lines.append(f"- **策略名称**: {strategy_name}")
    lines.append(f"- **扫描日期**: {formatted_trade_date}")
    lines.append(f"- **入围标的**: {scan['matched_count']} 只 (共筛选 {scan['total_stocks']} 只)")
    if details:
        lines.append(f"- **因子体系**: {details.get('factors', '')}")
        lines.append(f"- **适用场景**: {details.get('scenario', '')}")
        lines.append(f"- **持仓周期**: {details.get('hold_period', '')}")
    lines.append("")

    # ---- 扫描结果 Top 20 ----
    lines.append("## 扫描结果 (Top 20)")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 得分 | 关键信号 |")
    lines.append("|------|------|------|------|----------|")
    for r in (results or [])[:20]:
        signals = ", ".join(r.get("signals", [])[:3]) if r.get("signals") else "—"
        lines.append(f"| {r['rank']} | {r['code']} | {r['name']} | {r['score']:.0f} | {signals} |")
    lines.append("")

    # ---- 绩效分析（仅当可用时） ----
    if has_performance:
        s = analysis["summary"]
        lines.append("## 策略绩效数据（历史验证）")
        lines.append("")
        lines.append(f"- **平均收益**: {s['avg_return']:+.2f}%")
        lines.append(f"- **收益中位数**: {s['median_return']:+.2f}%")
        lines.append(f"- **胜率**: {s['win_rate']}% ({s['total']} 只标的)")
        lines.append(f"- **最佳标的**: {s['best']['name']} ({s['best']['code']}) +{s['best']['return_pct']}%")
        lines.append(f"- **最差标的**: {s['worst']['name']} ({s['worst']['code']}) {s['worst']['return_pct']}%")
        lines.append("")

        groups = analysis.get("score_groups", [])
        if groups:
            lines.append("### 得分-收益相关性")
            lines.append("")
            lines.append("| 得分区间 | 数量 | 平均收益 | 胜率 |")
            lines.append("|----------|------|----------|------|")
            for g in groups:
                avg = f"{g['avg_return']:+.2f}%" if g.get("avg_return") is not None else "—"
                wr = f"{g['win_rate']}%" if g.get("win_rate") is not None else "—"
                lines.append(f"| {g['label']} | {g['count']} | {avg} | {wr} |")
            lines.append("")

    # ---- 上次复盘 AI 总结（如有） ----
    prev_note_content = _get_previous_review_summary(scan_id)
    if prev_note_content:
        lines.append("## 上次复盘 AI 总结（你需要回顾自己的上次分析）")
        lines.append("")
        lines.append("以下是你**本人（同一个 AI）**上次对该策略的复盘分析总结。")
        lines.append("请务必作为本次复盘的参考基线，考虑以下要点：")
        lines.append("- 对照上次的分析逻辑，这次的扫描结果是否支持或修正了上次的判断？")
        lines.append("- 上次「明日计划」中提到的重点观察标的，这次是否仍在 Top 榜单中？表现如何？")
        lines.append("- 上次的市场观察结论与本次数据是否一致？如有变化请说明原因")
        lines.append("- 如果上次的计划执行效果不好，请反思原因并调整建议")
        lines.append("")
        lines.append(prev_note_content)
        lines.append("")

    # ---- 输出要求 ----
    lines.append("## 输出要求")
    lines.append("")
    lines.append("请严格按照以下格式输出 Markdown 内容（中文），控制在 600-1200 字。")
    lines.append("**只输出下面示例中的两个二级标题，不要输出其他任何标题、前言或结尾语。**")
    lines.append("")
    lines.append("```")
    lines.append("## 市场观察")
    lines.append("")
    lines.append("（结合大盘环境、板块热点和策略选出的个股做总体分析）")
    lines.append("- 大盘环境：当前主要指数走势、量能、市场情绪")
    lines.append("- 板块分析：策略标的集中在哪些板块、板块热度")
    lines.append("- 标的整体评估：技术面质量、共同特征")
    lines.append("- 亮点与风险：值得关注的个股或需要警惕的信号")
    lines.append("")
    lines.append("## 明日计划")
    lines.append("")
    lines.append("（基于策略信号和市场观察，给出明日操作计划）")
    lines.append("- 重点观察标的：从 Top 榜单中挑选 3-5 只，逐一说明入选理由")
    lines.append("- 观察要点：每只标的的关键价位、技术信号、确认条件")
    lines.append("- 风险提示：仓位建议和风险因素")
    lines.append("```")
    lines.append("")
    lines.append("注意：")
    lines.append("- 上面代码块中的「（…）」是内容说明，实际输出时替换为你的分析内容")
    if prev_note_content:
        lines.append("- 请在分析中适当引用或对比上次复盘总结的内容，说明本次分析的延续性或变化")
    if not user_operations.strip():
        lines.append("- 操作复盘部分**请留空，不需要输出**")
    if not has_performance:
        lines.append("- **不要编造收益数据**，聚焦于技术面逻辑和市场环境的分析")

    # ---- 用户今日操作（如有） ----
    if user_operations.strip():
        lines.append("")
        lines.append("## 用户今日操作（需要一并分析）")
        lines.append("")
        lines.append("以下为用户今日的实际操作记录，请在复盘分析时结合策略信号进行评估：")
        lines.append("")
        lines.append("```")
        lines.append(user_operations.strip())
        lines.append("```")
        lines.append("")
        lines.append("请在输出中额外包含「## 操作复盘」章节（放在「市场观察」之后、「明日计划」之前），")
        lines.append("对照用户的操作记录进行逐一分析：")
        lines.append("- 操作与策略信号的匹配度：用户的买卖是否与策略选股方向一致")
        lines.append("- 操作逻辑评估：操作背后的逻辑是否合理，有哪些值得肯定的地方")
        lines.append("- 改进建议：针对操作中的不足之处给出具体的改进方向")
        lines.append("- 总结：对用户今日整体操作的简要评价")
        lines.append("")
        lines.append("如果用户的操作与策略当前筛选标的无关，也请如实说明，")
        lines.append("并从通用交易原则角度给出参考意见。")

    # ---- 验证标记 ----
    token = _make_validation_token()
    _pending_tokens[scan_id] = token
    lines.append("")
    lines.append("## ⚠️ 响应验证（必须遵守）")
    lines.append("")
    lines.append(f"请在回复的**最开头**原封不动地输出以下验证标记（单独一行，不要加任何其他字符）：")
    lines.append("")
    lines.append(f"    {token}")
    lines.append("")
    lines.append("然后紧接上面的输出格式开始你的分析。如果你因为任何原因无法完成分析，")
    lines.append("也必须在回复开头输出该验证标记，然后再说明原因。")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI CLI 调用（流式）
# ---------------------------------------------------------------------------


async def run_ai_review(
    scan_id: str,
    cli_tool: str,
    user_operations: str = "",
) -> AsyncGenerator[str, None]:
    """调用本地 AI CLI，逐 chunk yield 输出。

    - stream_json 模式（claude）：逐 token 流式，真正实时输出
    - text 模式（其他 CLI）：按行流式，prompt 通过 stdin 传入
    - user_operations: 用户今日操作记录（可选），用于 AI 结合分析
    """
    info = CLI_REGISTRY.get(cli_tool)
    if info is None:
        yield f"错误：不支持的 CLI 工具 '{cli_tool}'。"
        return

    prompt = build_review_prompt(scan_id, user_operations)
    if prompt is None:
        yield "错误：无法获取策略扫描数据，请确认 scan_id 有效。"
        return

    mode: str = info.get("mode", "text")
    cmd = [cli_tool] + info["args"]

    # ---- stream_json 模式：包装 input 为 stream-json 格式 ----
    if mode == "stream_json":
        stdin_payload = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }, ensure_ascii=False) + "\n"
    else:
        stdin_payload = prompt

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if process.stdin:
            process.stdin.write(stdin_payload.encode())
            await process.stdin.drain()
            process.stdin.close()

        if not process.stdout:
            yield "错误：无法读取 CLI 输出。"
            return

        if mode == "stream_json":
            # 解析 stream-json 事件，提取 text_delta
            async for line in process.stdout:
                decoded = line.decode().rstrip("\n")
                if not decoded:
                    continue
                try:
                    evt = json.loads(decoded)
                except json.JSONDecodeError:
                    continue

                if evt.get("type") != "stream_event":
                    continue

                event = evt.get("event", {})
                if event.get("type") != "content_block_delta":
                    continue

                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text
        else:
            # text 模式：按行 yield，保留换行符供前端 Markdown 渲染
            async for line in process.stdout:
                decoded = line.decode().rstrip("\n")
                if decoded:
                    yield decoded + "\n"

        return_code = await process.wait()

        if return_code != 0 and process.stderr:
            stderr_output = (await process.stderr.read()).decode()
            if stderr_output.strip():
                import re
                clean = re.sub(r"\x1b\[[0-9;]*m", "", stderr_output).strip()
                yield f"\n\n---\n⚠️ {cli_tool} 返回非零退出码 ({return_code}): {clean[:500]}"

    except FileNotFoundError:
        yield f"错误：未找到 CLI 工具 '{cli_tool}'，请确认已安装并在 PATH 中。"
    except Exception as e:
        yield f"错误：调用 CLI 异常: {e}"


# ---------------------------------------------------------------------------
# 保存复盘为笔记
# ---------------------------------------------------------------------------


def save_review_as_note(
    scan_id: str,
    cli_tool: str,
    content: str,
    user_operations: str = "",
) -> dict | None:
    """将 AI 复盘结果保存为复盘笔记 + 策略复盘记录。

    user_operations: 用户原始今日操作记录（可选），存到 trade_review 顶部。
    返回 {"note": {...}, "review": {...}} 或 None。
    """
    storage = get_storage()

    # ---- 获取 scan 数据用于标题和关联 ----
    scan = storage.get_scan_status(scan_id)
    if not scan:
        return None

    strategy = scan["strategy"]
    strategy_name = STRATEGIES.get(strategy, ("未知策略",))[0]
    trade_date = scan["trade_date"]

    # 格式化日期
    formatted_date = trade_date
    if len(trade_date) == 8:
        formatted_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"

    # ---- 解析 AI 内容为笔记字段 ----
    # AI prompt 输出格式：市场观察 / [操作复盘] / 明日计划
    market_obs = _extract_section(content, "市场观察")
    trade_review = _extract_section(content, "操作复盘")
    next_plan = _extract_section(content, "明日计划")

    # 如果用户提供了原始操作记录，前置到 trade_review 顶部
    if user_operations.strip():
        ops_header = "## 今日操作\n\n"
        ops_block = ops_header + user_operations.strip()
        if trade_review.strip():
            trade_review = ops_block + "\n\n---\n\n## AI 操作分析\n\n" + trade_review
        else:
            trade_review = ops_block

    # 兼容旧版 prompt 输出格式（市场适配性评估 / 因子有效性分析 / 下一步关注）
    if not market_obs.strip():
        market_obs = _extract_section(content, "市场适配性评估")
    if not next_plan.strip():
        next_plan = (
            _extract_section(content, "风险与改进建议")
            + "\n\n"
            + _extract_section(content, "下一步关注")
        ).strip()

    # 如果所有字段都提取为空，将完整内容存入 market_obs 作为兜底
    if not market_obs.strip() and not next_plan.strip():
        market_obs = content

    # 获取 scan 结果的 Top 标的用于关联
    results = storage.get_strategy_results(scan_id, strategy)
    stocks = [
        {"code": r["code"], "name": r["name"]}
        for r in (results or [])[:10]
    ]

    # ---- 创建笔记 ----
    title = f"{formatted_date}-{strategy_name}-复盘笔记"
    tags = ["AI复盘", strategy_name, "策略分析"]

    note_id = storage.create_note(
        title=title,
        trade_date=formatted_date,
        market_obs=market_obs,
        trade_review=trade_review,
        next_plan=next_plan,
        tags=tags,
        stock_codes=stocks,
    )

    if note_id is None:
        return None

    # ---- 创建策略复盘记录 ----
    review_id = storage.create_review(scan_id, cli_tool, content)
    storage.link_review_note(review_id, note_id)

    return {
        "review": storage.get_review(review_id),
        "note": storage.get_note(note_id),
    }


def _extract_section(content: str, section_name: str) -> str:
    """从 Markdown 内容中提取指定 section 的文本。

    支持多种标题格式:
      - ### 1. section_name  /  ### 1、section_name
      - ### 一、section_name (中文数字)
      - ## section_name / ## 1. section_name
    """
    import re

    escaped = re.escape(section_name)
    chinese_nums = "一二三四五六七八九十"

    patterns = [
        # ### N. name 或 ### N、name (阿拉伯数字)
        rf"###\s+\d+[\.、]\s*{escaped}",
        # ### 中文数字、name
        rf"###\s+[{chinese_nums}][、]\s*{escaped}",
        # ## N. name 或 ## name
        rf"##\s+(?:\d+[\.、]\s*)?{escaped}",
        # ### name (无编号)
        rf"###\s+{escaped}",
        # 粗体标题: **1. name**
        rf"\*\*\d+[\.、]\s*{escaped}\*\*",
    ]

    for pat in patterns:
        # 匹配标题后的内容，直到遇到下一个同级标题或文档末尾
        full_pat = rf"{pat}\s*\n(.*?)(?=\n(?:###|\*\*\d+[\.、])|\n##\s|\Z)"
        match = re.search(full_pat, content, re.DOTALL)
        if match:
            text = match.group(1).strip()
            if text:
                return text

    return ""


def get_reviews_for_scan(scan_id: str) -> list[dict]:
    """获取指定扫描的所有复盘记录列表。"""
    return get_storage().get_reviews_by_scan(scan_id)


def get_review_detail(review_id: int) -> dict | None:
    """获取单条复盘记录详情。"""
    return get_storage().get_review(review_id)


def delete_review(review_id: int) -> bool:
    """删除复盘记录及其关联笔记。"""
    return get_storage().delete_review(review_id)
