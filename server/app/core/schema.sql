-- ============================================================================
-- 慧研 · A 股投资分析工作台 — 数据库表结构定义
-- ============================================================================
-- 本文件是所有 SQLite 表结构的唯一权威来源。
-- 数据库文件: server/data/stockbench.db
-- 迁移策略: CREATE TABLE IF NOT EXISTS（幂等建表，可多次执行）
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. 股票池 (stock_universe)
--    从 mootdx 拉取的全量 A 股列表，每天刷新一次
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_universe (
    code        TEXT PRIMARY KEY,          -- 6 位股票代码，如 600519
    name        TEXT NOT NULL,             -- 股票名称
    market      TEXT NOT NULL DEFAULT 'sh', -- sh/sz/cyb/kcb
    market_name TEXT NOT NULL DEFAULT '',  -- 沪市主板/深市主板/创业板/科创板
    industry    TEXT NOT NULL DEFAULT '',  -- 行业分类
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_universe_market ON stock_universe(market);
CREATE INDEX IF NOT EXISTS idx_universe_name ON stock_universe(name);

-- ----------------------------------------------------------------------------
-- 2. 自选股 (watchlist)
--    替代 server/data/watchlist.json
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    code       TEXT PRIMARY KEY,           -- 6 位股票代码
    name       TEXT NOT NULL,              -- 股票名称（冗余，便于快速展示）
    market     TEXT NOT NULL DEFAULT 'sh', -- sh/sz/cyb/kcb
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ----------------------------------------------------------------------------
-- 2a. 自选分类 (watchlist_categories)
--     用户自定义分类，如「短线」「长线」「关注」等
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);

-- ----------------------------------------------------------------------------
-- 2b. 自选股-分类关联 (watchlist_category_map)
--     多对多：一只股票可属于多个分类
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_category_map (
    code        TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    PRIMARY KEY (code, category_id),
    FOREIGN KEY (code) REFERENCES watchlist(code) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES watchlist_categories(id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- 3. 日K线缓存 (daily_kline)
--    每只股票每个交易日的 OHLCV 数据，替代重复调用 mootdx
--    复权方式固定为前复权 (qfq)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_kline (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,              -- 6 位股票代码
    trade_date TEXT NOT NULL,              -- 交易日 YYYYMMDD
    open       REAL NOT NULL DEFAULT 0,
    high       REAL NOT NULL DEFAULT 0,
    low        REAL NOT NULL DEFAULT 0,
    close      REAL NOT NULL DEFAULT 0,
    vol        REAL NOT NULL DEFAULT 0,    -- 成交量（股）
    amount     REAL NOT NULL DEFAULT 0,    -- 成交额（元）
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_kline_code_date ON daily_kline(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(trade_date);

-- ----------------------------------------------------------------------------
-- 4. 实时行情快照缓存 (stock_quotes_cache)
--    替代 server/data/cache/stocks.json
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_quotes_cache (
    code        TEXT PRIMARY KEY,          -- 6 位股票代码
    name        TEXT NOT NULL DEFAULT '',
    price       REAL NOT NULL DEFAULT 0,
    change      REAL NOT NULL DEFAULT 0,
    change_pct  REAL NOT NULL DEFAULT 0,
    open        REAL NOT NULL DEFAULT 0,
    high        REAL NOT NULL DEFAULT 0,
    low         REAL NOT NULL DEFAULT 0,
    volume      REAL NOT NULL DEFAULT 0,   -- 成交量（万手）
    amount      REAL NOT NULL DEFAULT 0,   -- 成交额（元）
    market_cap  REAL NOT NULL DEFAULT 0,   -- 总市值（亿）
    industry    TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_quotes_updated ON stock_quotes_cache(updated_at);

-- ----------------------------------------------------------------------------
-- 5. 市场概况缓存 (market_overview_cache)
--    替代 server/data/cache/market_overview_real.json
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_overview_cache (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行缓存
    data_json   TEXT NOT NULL,              -- 完整 market overview JSON
    cached_at   REAL NOT NULL DEFAULT 0,    -- time.time() 时间戳
    source      TEXT NOT NULL DEFAULT 'mootdx'
);

-- ----------------------------------------------------------------------------
-- 6. 策略筛选结果 (strategy_results)
--    每次策略扫描的结果持久化存储
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       TEXT NOT NULL,            -- 扫描批次 ID (YYYYMMDD_HHMMSS)
    strategy      TEXT NOT NULL,            -- s1/s2/s3
    code          TEXT NOT NULL,            -- 股票代码
    name          TEXT NOT NULL,            -- 股票名称
    score         REAL NOT NULL DEFAULT 0,  -- 综合得分 0-100
    rank          INTEGER NOT NULL DEFAULT 0, -- 排名
    factors_json  TEXT NOT NULL DEFAULT '{}', -- 各因子得分明细 JSON
    signals_json  TEXT NOT NULL DEFAULT '[]', -- 触发信号列表 JSON
    metrics_json  TEXT NOT NULL DEFAULT '{}', -- 关键指标值 JSON
    trade_date    TEXT NOT NULL,            -- 筛选日期 YYYYMMDD
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scan_id, strategy, code)
);

CREATE INDEX IF NOT EXISTS idx_strategy_scan ON strategy_results(scan_id, strategy);
CREATE INDEX IF NOT EXISTS idx_strategy_code ON strategy_results(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_strategy_date ON strategy_results(trade_date, strategy);

-- ----------------------------------------------------------------------------
-- 7. 策略扫描日志 (strategy_scan_log)
--    记录每次扫描的执行情况
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_scan_log (
    scan_id       TEXT PRIMARY KEY,         -- YYYYMMDD_HHMMSS
    strategy      TEXT NOT NULL,            -- s1/s2/s3 或 all
    trade_date    TEXT NOT NULL,            -- 筛选日期
    status        TEXT NOT NULL DEFAULT 'running', -- running/completed/failed
    total_stocks  INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_scan_log_date ON strategy_scan_log(trade_date);

-- ----------------------------------------------------------------------------
-- 8. 复盘笔记 (review_notes)
--    用户每日复盘记录
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL DEFAULT '',
    market_obs   TEXT NOT NULL DEFAULT '',     -- 市场观察
    trade_review TEXT NOT NULL DEFAULT '',     -- 操作复盘
    next_plan    TEXT NOT NULL DEFAULT '',     -- 明日计划
    tags_json    TEXT NOT NULL DEFAULT '[]',   -- 标签列表 JSON
    word_count   INTEGER NOT NULL DEFAULT 0,
    trade_date   TEXT NOT NULL,                -- 关联交易日 YYYY-MM-DD
    created_at   TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);

CREATE INDEX IF NOT EXISTS idx_review_notes_date ON review_notes(trade_date);
CREATE INDEX IF NOT EXISTS idx_review_notes_created ON review_notes(created_at);

-- ----------------------------------------------------------------------------
-- 8a. 复盘笔记-标的关联 (review_note_stocks)
--     多对多：一篇笔记可关联多只股票
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_note_stocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    UNIQUE(note_id, stock_code),
    FOREIGN KEY (note_id) REFERENCES review_notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rns_note ON review_note_stocks(note_id);
CREATE INDEX IF NOT EXISTS idx_rns_stock ON review_note_stocks(stock_code);

-- ----------------------------------------------------------------------------
-- 9. 策略复盘记录 (strategy_reviews)
--    记录每次 AI 策略复盘的内容，可关联到复盘笔记
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     TEXT NOT NULL,
    cli_tool    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    note_id     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    FOREIGN KEY (note_id) REFERENCES review_notes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sr_scan ON strategy_reviews(scan_id);

-- ----------------------------------------------------------------------------
-- 10. 股票池刷新状态 (universe_refresh_status)
--    记录股票池的最后刷新时间和状态
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS universe_refresh_status (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    count       INTEGER NOT NULL DEFAULT 0,
    cached_at   REAL NOT NULL DEFAULT 0,    -- time.time() 时间戳
    updated_at  TEXT NOT NULL DEFAULT '',   -- 可读时间
    source      TEXT NOT NULL DEFAULT 'mootdx'
);

-- ----------------------------------------------------------------------------
-- 11. 市场总览刷新状态 (market_overview_status)
--     单行表，追踪 get_market_overview() 后台刷新任务的运行状态
--     前端通过 /api/market/overview/status 轮询此表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_overview_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行
    refreshing   INTEGER NOT NULL DEFAULT 0,          -- 0/1：是否有后台刷新任务
    started_at   REAL    NOT NULL DEFAULT 0,          -- time.time()：本次刷新开始时间
    last_success REAL    NOT NULL DEFAULT 0,          -- time.time()：上一次成功完成时间
    last_error   TEXT    NOT NULL DEFAULT ''          -- 最近一次失败信息（成功时清空）
);

-- ----------------------------------------------------------------------------
-- 12. 策略参数配置 (strategy_params)
--     存储各策略的因子阈值、权重、区间等可调参数
--     current_value 使用 JSON 字符串存储，支持 float / int / list 等多种类型
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_params (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,              -- s1 / s2 / s3
    param_name      TEXT NOT NULL,              -- 参数标识，如 trend_health_center
    param_label     TEXT NOT NULL DEFAULT '',    -- 中文名称
    param_type      TEXT NOT NULL DEFAULT 'float', -- float / int / bool / range
    current_value   TEXT NOT NULL,              -- 当前值 (JSON)
    default_value   TEXT NOT NULL,              -- 默认值 (JSON)
    min_value       REAL,                       -- 最小值 (数值型参数)
    max_value       REAL,                       -- 最大值 (数值型参数)
    step            REAL,                       -- 步长 (数值型参数)
    last_tuned      TEXT,                       -- 上次自动调整时间
    tune_history_json TEXT NOT NULL DEFAULT '[]', -- 调整历史 [{ts, old_value, new_value, reason}]
    UNIQUE(strategy, param_name)
);

-- ----------------------------------------------------------------------------
-- 13. 精筛记录 (precision_picks)
--     每日精筛引擎选出的 Top N 标的，追踪后续表现
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS precision_picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,              -- 选股日期 YYYYMMDD
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    pick_price      REAL NOT NULL,              -- 入选时价格
    precision_score REAL NOT NULL DEFAULT 0,    -- 精筛综合得分 0-100
    rank            INTEGER NOT NULL,           -- 当日排名
    reasons_json    TEXT NOT NULL DEFAULT '[]', -- 入选理由 [{type, desc, value}]
    feature_scores_json TEXT NOT NULL DEFAULT '{}', -- 各维度得分明细
    signal_weights_json TEXT NOT NULL DEFAULT '{}', -- 选股时使用的权重快照
    latest_price    REAL,                       -- 最新价 (每日更新)
    return_pct      REAL,                       -- 至今涨跌幅%
    outcome         TEXT NOT NULL DEFAULT 'pending', -- pending / win / loss / breakeven
    outcome_days    INTEGER,                    -- 判定持仓天数
    outcome_verified_at TEXT,                   -- 结果确认时间
    created_at      TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    UNIQUE(trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_pp_date ON precision_picks(trade_date);
CREATE INDEX IF NOT EXISTS idx_pp_outcome ON precision_picks(outcome);
CREATE INDEX IF NOT EXISTS idx_pp_code ON precision_picks(code);

-- ----------------------------------------------------------------------------
-- 14. 信号权重 (signal_weights)
--     精筛引擎每个信号的当前权重及历史表现统计
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_weights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name     TEXT NOT NULL UNIQUE,        -- 信号唯一标识
    category        TEXT NOT NULL,              -- strategy_score / consensus / factor_detail / persistence / market_env
    weight          REAL NOT NULL DEFAULT 1.0,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    positive_count  INTEGER NOT NULL DEFAULT 0,
    win_rate        REAL NOT NULL DEFAULT 0,
    avg_return      REAL NOT NULL DEFAULT 0,
    information_coef REAL NOT NULL DEFAULT 0,   -- IC: 信号值与后续收益的 Pearson 相关系数
    ic_stability    REAL NOT NULL DEFAULT 0,    -- 多时间窗口 IC 的标准差倒数
    last_updated    TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_sw_category ON signal_weights(category);

-- ----------------------------------------------------------------------------
-- 15. 精筛每日日志 (precision_daily_log)
--     记录每日精筛执行的元信息，含完整权重/参数快照用于回测追溯
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS precision_daily_log (
    trade_date      TEXT PRIMARY KEY,           -- YYYYMMDD
    total_candidates INTEGER NOT NULL DEFAULT 0,
    picks_count     INTEGER NOT NULL DEFAULT 0,
    model_version   TEXT NOT NULL DEFAULT '1.0',
    weights_snapshot_json TEXT NOT NULL DEFAULT '{}',
    params_snapshot_json  TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);

-- ----------------------------------------------------------------------------
-- 16. 因子有效性快照 (factor_effectiveness)
--     各策略各因子的历史预测能力统计，用于评估策略科学性
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_effectiveness (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,              -- s1 / s2 / s3
    factor_name     TEXT NOT NULL,              -- 如 ma_alignment / rsi_divergence
    sample_count    INTEGER NOT NULL DEFAULT 0,
    positive_count  INTEGER NOT NULL DEFAULT 0,
    win_rate        REAL NOT NULL DEFAULT 0,
    avg_return      REAL NOT NULL DEFAULT 0,
    ic              REAL NOT NULL DEFAULT 0,    -- 因子得分 vs 后续收益的相关性
    optimal_center  REAL,                       -- 数据驱动的最优质心 (高斯型因子)
    optimal_sigma   REAL,                       -- 数据驱动的最优sigma (高斯型因子)
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    UNIQUE(strategy, factor_name)
);
