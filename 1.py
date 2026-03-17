import re
import sqlite3
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None


st.set_page_config(page_title="个人交易中心", page_icon="📈", layout="wide")

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2rem;
        max-width: 1400px;
    }
    .big-title {
        font-size: 2.4rem;
        font-weight: 800;
        margin-bottom: 0.2rem;
    }
    .subtle {
        color: #6b7280;
        font-size: 1rem;
        margin-bottom: 1rem;
    }
    .card {
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 18px;
        padding: 18px 18px 14px 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,250,252,0.96));
        box-shadow: 0 6px 18px rgba(15,23,42,0.05);
        margin-bottom: 12px;
    }
    .kpi-label {
        font-size: 0.95rem;
        color: #6b7280;
        margin-bottom: 6px;
    }
    .kpi-value {
        font-size: 2rem;
        font-weight: 800;
        line-height: 1.15;
    }
    .kpi-small {
        font-size: 1.3rem;
        font-weight: 700;
        line-height: 1.2;
    }
    .profit { color: #16a34a; }
    .loss { color: #dc2626; }
    .neutral { color: #111827; }
    .section-title {
        font-size: 1.15rem;
        font-weight: 750;
        margin: 0 0 10px 0;
    }
    .hint {
        font-size: 0.88rem;
        color: #6b7280;
    }
    .pill-green, .pill-red, .pill-gray {
        display: inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        font-size: 0.88rem;
        font-weight: 700;
    }
    .pill-green {
        background: rgba(22,163,74,0.12);
        color: #15803d;
    }
    .pill-red {
        background: rgba(220,38,38,0.12);
        color: #b91c1c;
    }
    .pill-gray {
        background: rgba(100,116,139,0.12);
        color: #475569;
    }
    div[data-testid="stMetric"] {
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,250,252,0.96));
        border: 1px solid rgba(0,0,0,0.08);
        padding: 14px 16px;
        border-radius: 18px;
        box-shadow: 0 6px 18px rgba(15,23,42,0.04);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

DB_PATH = Path.home() / "stock_margin_tracker.db"

APP_USERNAME = "admin"
APP_PASSWORD_HASH = hashlib.sha256("060913".encode("utf-8")).hexdigest()


@dataclass
class PositionMetrics:
    stock_code: str
    stock_name: str
    buy_price: float
    current_price: float
    shares: int
    total_cost: float
    market_value: float
    profit_loss: float
    profit_loss_pct: float
    financed_principal: float
    leverage: float
    max_trading_capital: float
    used_position_amount: float
    position_usage_pct: float
    loss_vs_financed_principal: float
    fee_daily_rate_pct: float
    daily_fee_base: float
    daily_fee_amount: float
    held_days: float
    accumulated_fee: float
    net_profit_after_fee: float
    net_profit_after_fee_pct_cost: float


def safe_float(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def money(x: float) -> str:
    return f"¥{x:,.2f}"


def pct(x: float) -> str:
    return f"{x:,.2f}%"


def normalize_code(code: str) -> str:
    digits = re.sub(r"\D", "", str(code).strip())
    return digits.zfill(6)[:6]


def get_market_prefix(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("60", "68", "90", "51", "58")):
        return "sh"
    return "sz"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            stock_code TEXT,
            price_mode TEXT,
            manual_current_price REAL,
            buy_price REAL,
            shares INTEGER,
            override_total_cost_enabled INTEGER,
            override_total_cost REAL,
            financed_principal REAL,
            leverage REAL,
            fee_daily_rate_pct REAL,
            held_days REAL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_state_to_db(state: dict) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO app_state (
            id, stock_code, price_mode, manual_current_price, buy_price, shares,
            override_total_cost_enabled, override_total_cost,
            financed_principal, leverage, fee_daily_rate_pct, held_days, updated_at
        ) VALUES (
            1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            stock_code=excluded.stock_code,
            price_mode=excluded.price_mode,
            manual_current_price=excluded.manual_current_price,
            buy_price=excluded.buy_price,
            shares=excluded.shares,
            override_total_cost_enabled=excluded.override_total_cost_enabled,
            override_total_cost=excluded.override_total_cost,
            financed_principal=excluded.financed_principal,
            leverage=excluded.leverage,
            fee_daily_rate_pct=excluded.fee_daily_rate_pct,
            held_days=excluded.held_days,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            state.get("stock_code", "688479"),
            state.get("price_mode", "自动获取实时价"),
            safe_float(state.get("manual_current_price", 0.0)),
            safe_float(state.get("buy_price", 0.0)),
            int(state.get("shares", 0)),
            1 if state.get("override_total_cost_enabled", False) else 0,
            safe_float(state.get("override_total_cost", 0.0)),
            safe_float(state.get("financed_principal", 0.0)),
            safe_float(state.get("leverage", 0.0)),
            safe_float(state.get("fee_daily_rate_pct", 0.0)),
            safe_float(state.get("held_days", 0.0)),
        ),
    )
    conn.commit()
    conn.close()


def load_state_from_db() -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM app_state WHERE id = 1")
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "stock_code": row["stock_code"],
        "price_mode": row["price_mode"],
        "manual_current_price": safe_float(row["manual_current_price"], 0.0),
        "buy_price": safe_float(row["buy_price"], 0.0),
        "shares": int(row["shares"] or 0),
        "override_total_cost_enabled": bool(row["override_total_cost_enabled"]),
        "override_total_cost": safe_float(row["override_total_cost"], 0.0),
        "financed_principal": safe_float(row["financed_principal"], 0.0),
        "leverage": safe_float(row["leverage"], 0.0),
        "fee_daily_rate_pct": safe_float(row["fee_daily_rate_pct"], 0.0),
        "held_days": safe_float(row["held_days"], 0.0),
    }


def get_quote_from_sina(code: str) -> Tuple[str, float]:
    code = normalize_code(code)
    symbol = f"{get_market_prefix(code)}{code}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    }

    r = requests.get(url, headers=headers, timeout=8)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text.strip()

    if "=" not in text:
        raise RuntimeError("行情接口返回格式异常")

    raw = text.split("=", 1)[1].strip().rstrip(";").strip().strip('"')
    if not raw:
        raise RuntimeError("未取到有效行情数据，请检查代码")

    fields = raw.split(",")
    if len(fields) < 4:
        raise RuntimeError("行情字段不足")

    stock_name = fields[0].strip() or code
    current_price = safe_float(fields[3], 0.0)

    if current_price <= 0:
        raise RuntimeError("当前价格无效，可能休市、停牌或代码错误")

    return stock_name, current_price


@st.cache_data(ttl=5)
def cached_quote(code: str) -> Tuple[str, float]:
    return get_quote_from_sina(code)


def calculate_metrics(
    stock_code: str,
    stock_name: str,
    buy_price: float,
    current_price: float,
    shares: int,
    override_total_cost_enabled: bool,
    override_total_cost: Optional[float],
    financed_principal: float,
    leverage: float,
    fee_daily_rate_pct: float,
    held_days: float,
) -> PositionMetrics:
    buy_price = max(safe_float(buy_price), 0.0)
    current_price = max(safe_float(current_price), 0.0)
    shares = max(int(shares), 0)
    financed_principal = max(safe_float(financed_principal), 0.0)
    leverage = max(safe_float(leverage, 1.0), 0.0)
    fee_daily_rate_pct = max(safe_float(fee_daily_rate_pct), 0.0)
    held_days = max(safe_float(held_days), 0.0)

    raw_cost = buy_price * shares
    total_cost = safe_float(override_total_cost, raw_cost) if override_total_cost_enabled else raw_cost
    if total_cost <= 0 and raw_cost > 0:
        total_cost = raw_cost

    market_value = current_price * shares
    profit_loss = market_value - total_cost
    profit_loss_pct = (profit_loss / total_cost * 100) if total_cost > 0 else 0.0

    max_trading_capital = financed_principal * leverage
    used_position_amount = min(total_cost, max_trading_capital) if max_trading_capital > 0 else total_cost
    position_usage_pct = (used_position_amount / max_trading_capital * 100) if max_trading_capital > 0 else 0.0

    loss_vs_financed_principal = 0.0
    if financed_principal > 0 and profit_loss < 0:
        loss_vs_financed_principal = abs(profit_loss) / financed_principal * 100

    daily_fee_base = used_position_amount
    daily_fee_amount = daily_fee_base * (fee_daily_rate_pct / 100)
    accumulated_fee = daily_fee_amount * held_days

    net_profit_after_fee = profit_loss - accumulated_fee
    net_profit_after_fee_pct_cost = (net_profit_after_fee / total_cost * 100) if total_cost > 0 else 0.0

    return PositionMetrics(
        stock_code=stock_code,
        stock_name=stock_name,
        buy_price=buy_price,
        current_price=current_price,
        shares=shares,
        total_cost=total_cost,
        market_value=market_value,
        profit_loss=profit_loss,
        profit_loss_pct=profit_loss_pct,
        financed_principal=financed_principal,
        leverage=leverage,
        max_trading_capital=max_trading_capital,
        used_position_amount=used_position_amount,
        position_usage_pct=position_usage_pct,
        loss_vs_financed_principal=loss_vs_financed_principal,
        fee_daily_rate_pct=fee_daily_rate_pct,
        daily_fee_base=daily_fee_base,
        daily_fee_amount=daily_fee_amount,
        held_days=held_days,
        accumulated_fee=accumulated_fee,
        net_profit_after_fee=net_profit_after_fee,
        net_profit_after_fee_pct_cost=net_profit_after_fee_pct_cost,
    )


def calculate_risk_line_info(
    total_cost: float,
    current_price: float,
    shares: int,
    financed_principal: float,
    line_pct: float,
    current_profit_loss: float,
) -> dict:
    shares = max(int(shares), 0)
    financed_principal = max(safe_float(financed_principal), 0.0)
    total_cost = max(safe_float(total_cost), 0.0)
    current_price = max(safe_float(current_price), 0.0)

    threshold_loss_amount = financed_principal * line_pct
    current_loss_amount = abs(current_profit_loss) if current_profit_loss < 0 else 0.0
    remaining_loss_amount = max(0.0, threshold_loss_amount - current_loss_amount)

    if shares > 0:
        target_price = max(0.0, (total_cost - threshold_loss_amount) / shares)
        additional_drop_price = max(0.0, current_price - target_price)
    else:
        target_price = 0.0
        additional_drop_price = 0.0

    if current_price > 0:
        additional_drop_pct = max(0.0, additional_drop_price / current_price * 100)
    else:
        additional_drop_pct = 0.0

    triggered = current_loss_amount >= threshold_loss_amount and threshold_loss_amount > 0

    return {
        "line_pct": line_pct,
        "threshold_loss_amount": threshold_loss_amount,
        "current_loss_amount": current_loss_amount,
        "remaining_loss_amount": remaining_loss_amount,
        "target_price": target_price,
        "additional_drop_price": additional_drop_price,
        "additional_drop_pct": additional_drop_pct,
        "triggered": triggered,
    }


def render_big_card(label: str, value: str, class_name: str = "neutral", badge: Optional[str] = None) -> None:
    badge_html = ""
    if badge:
        badge_html = f'<div style="margin-top:8px">{badge}</div>'

    st.markdown(
        f"""
        <div class="card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {class_name}">{value}</div>
            {badge_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_small_card(label: str, value: str, class_name: str = "neutral") -> None:
    st.markdown(
        f"""
        <div class="card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-small {class_name}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def pnl_class(v: float) -> str:
    if v > 0:
        return "profit"
    if v < 0:
        return "loss"
    return "neutral"


def pnl_badge(v: float) -> str:
    if v > 0:
        return '<span class="pill-green">盈利中</span>'
    if v < 0:
        return '<span class="pill-red">亏损中</span>'
    return '<span class="pill-gray">持平</span>'


def check_login() -> bool:
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if st.session_state.logged_in:
        return True

    st.markdown('<div class="big-title">🔐 登录</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtle">请输入用户名和密码后进入数据系统。</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        username = st.text_input("用户名", placeholder="请输入用户名")
        password = st.text_input("密码", type="password", placeholder="请输入密码")

        if st.button("登录", use_container_width=True, type="primary"):
            password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            if username == APP_USERNAME and password_hash == APP_PASSWORD_HASH:
                st.session_state.logged_in = True
                st.success("登录成功")
                st.rerun()
            else:
                st.error("用户名或密码错误")

        st.markdown("</div>", unsafe_allow_html=True)

    return False


def main() -> None:
    init_db()

    if not check_login():
        return

    saved = load_state_from_db()

    defaults = {
        "stock_code": "688479",
        "buy_price": 0.0,
        "shares": 1000,
        "override_total_cost_enabled": False,
        "override_total_cost": 0.0,
        "financed_principal": 30000.0,
        "leverage": 10.0,
        "fee_daily_rate_pct": 0.30,
        "held_days": 1.0,
        "price_mode": "自动获取实时价",
        "manual_current_price": 0.0,
        "show_left_panel": True,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = saved.get(key, value) if saved else value

    st.markdown('<div class="big-title">📈 个人交易中心</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="subtle">俞总专属交易系统。</div>',
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown('<div class="section-title">系统设置</div>', unsafe_allow_html=True)

        auto_refresh = st.checkbox("开启自动刷新", value=False)
        refresh_seconds = st.slider("自动刷新间隔（秒）", 5, 60, 10)

        if auto_refresh and st_autorefresh is not None:
            st_autorefresh(interval=refresh_seconds * 1000, key="auto_refresh_quotes")
        elif auto_refresh and st_autorefresh is None:
            st.info("未安装 streamlit-autorefresh，自动刷新不可用")

        if st.button("清缓存并刷新价格", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        if st.button("退出登录", use_container_width=True):
            st.session_state.logged_in = False
            st.rerun()

        st.markdown("---")

        if st.button("保存当前数据到本地数据库", use_container_width=True, type="primary"):
            save_state_to_db(
                {
                    "stock_code": st.session_state.stock_code,
                    "price_mode": st.session_state.price_mode,
                    "manual_current_price": st.session_state.manual_current_price,
                    "buy_price": st.session_state.buy_price,
                    "shares": st.session_state.shares,
                    "override_total_cost_enabled": st.session_state.override_total_cost_enabled,
                    "override_total_cost": st.session_state.override_total_cost,
                    "financed_principal": st.session_state.financed_principal,
                    "leverage": st.session_state.leverage,
                    "fee_daily_rate_pct": st.session_state.fee_daily_rate_pct,
                    "held_days": st.session_state.held_days,
                }
            )
            st.success(f"已保存到本地数据库：{DB_PATH}")

        if st.button("从本地数据库读取保存数据", use_container_width=True):
            loaded = load_state_from_db()
            if loaded:
                for k, v in loaded.items():
                    st.session_state[k] = v
                st.success("已从本地数据库读取保存数据")
                st.rerun()
            else:
                st.warning("数据库里还没有保存记录")

        st.markdown("---")
        st.markdown(
            '<div class="hint">手续费只按实际买入金额收费，不按全部配资额度收费。</div>',
            unsafe_allow_html=True,
        )

    btn_col1, btn_col2 = st.columns([1, 5])
    with btn_col1:
        if st.button("隐藏参数栏" if st.session_state.show_left_panel else "显示参数栏", use_container_width=True):
            st.session_state.show_left_panel = not st.session_state.show_left_panel
            st.rerun()

    stock_name = st.session_state.stock_code
    current_price = 0.0

    if st.session_state.show_left_panel:
        left, right = st.columns([1, 1.25], gap="large")

        with left:
            st.markdown('<div class="section-title">参数输入</div>', unsafe_allow_html=True)

            st.session_state.price_mode = st.radio(
                "价格来源",
                ["自动获取实时价", "手动输入现价"],
                horizontal=True,
                index=0 if st.session_state.price_mode == "自动获取实时价" else 1,
            )

            st.session_state.stock_code = normalize_code(
                st.text_input(
                    "股票代码（6位）",
                    value=st.session_state.stock_code,
                    placeholder="如 600519 / 000001 / 688479",
                )
            )

            stock_name = st.session_state.stock_code

            if st.session_state.price_mode == "自动获取实时价":
                try:
                    stock_name, current_price = cached_quote(st.session_state.stock_code)
                    st.success(f"已获取 {stock_name}（{st.session_state.stock_code}）最新价：{current_price:.2f}")

                    if st.session_state.buy_price == 0:
                        st.session_state.buy_price = current_price
                    if st.session_state.manual_current_price == 0:
                        st.session_state.manual_current_price = current_price
                except Exception as e:
                    st.error(f"实时价格获取失败：{e}")
                    st.info("请切换为手动输入现价，或稍后再试。")
                    current_price = st.number_input(
                        "当前价格（备用手动输入）",
                        min_value=0.0,
                        step=0.01,
                        format="%.4f",
                        key="manual_current_price",
                    )
            else:
                current_price = st.number_input(
                    "当前价格",
                    min_value=0.0,
                    step=0.01,
                    format="%.4f",
                    key="manual_current_price",
                )

            c1, c2 = st.columns(2)
            with c1:
                st.number_input(
                    "买入价格",
                    min_value=0.0,
                    step=0.01,
                    format="%.4f",
                    key="buy_price",
                    help="这里填买入价，修改后立即重新计算盈亏。",
                )
            with c2:
                st.number_input(
                    "买入股数",
                    min_value=0,
                    step=100,
                    key="shares",
                )

            st.checkbox("手动编辑总成本", key="override_total_cost_enabled")
            auto_cost_preview = st.session_state.buy_price * st.session_state.shares

            if st.session_state.override_total_cost_enabled:
                st.number_input(
                    "总成本",
                    min_value=0.0,
                    step=100.0,
                    format="%.2f",
                    key="override_total_cost",
                    help="勾选后，总成本以这里为准，不再使用 买入价 × 股数。",
                )
            else:
                st.info(f"自动总成本 = 买入价 × 股数 = {money(auto_cost_preview)}")

            st.markdown('<div class="section-title" style="margin-top:12px">配资参数</div>', unsafe_allow_html=True)

            p1, p2 = st.columns(2)
            with p1:
                st.number_input(
                    "配资本金 / 保证金",
                    min_value=0.0,
                    step=1000.0,
                    format="%.2f",
                    key="financed_principal",
                )
                st.number_input(
                    "每日利息 / 手续费（%）",
                    min_value=0.0,
                    step=0.01,
                    format="%.4f",
                    key="fee_daily_rate_pct",
                )
            with p2:
                st.number_input(
                    "配资杠杆",
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key="leverage",
                )
                st.number_input(
                    "持仓天数",
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key="held_days",
                )
    else:
        right = st.container()
        if st.session_state.price_mode == "自动获取实时价":
            try:
                stock_name, current_price = cached_quote(st.session_state.stock_code)
            except Exception:
                current_price = st.session_state.manual_current_price
        else:
            current_price = st.session_state.manual_current_price

    metrics = calculate_metrics(
        stock_code=st.session_state.stock_code,
        stock_name=stock_name,
        buy_price=st.session_state.buy_price,
        current_price=current_price,
        shares=int(st.session_state.shares),
        override_total_cost_enabled=st.session_state.override_total_cost_enabled,
        override_total_cost=st.session_state.override_total_cost,
        financed_principal=st.session_state.financed_principal,
        leverage=st.session_state.leverage,
        fee_daily_rate_pct=st.session_state.fee_daily_rate_pct,
        held_days=st.session_state.held_days,
    )

    warning_70 = calculate_risk_line_info(
        total_cost=metrics.total_cost,
        current_price=metrics.current_price,
        shares=metrics.shares,
        financed_principal=metrics.financed_principal,
        line_pct=0.70,
        current_profit_loss=metrics.profit_loss,
    )

    liquidation_80 = calculate_risk_line_info(
        total_cost=metrics.total_cost,
        current_price=metrics.current_price,
        shares=metrics.shares,
        financed_principal=metrics.financed_principal,
        line_pct=0.80,
        current_profit_loss=metrics.profit_loss,
    )

    with right:
        st.markdown('<div class="section-title">实时结果</div>', unsafe_allow_html=True)

        r1, r2 = st.columns([1, 1])
        with r1:
            render_big_card("股票名称", f"{metrics.stock_name}（{metrics.stock_code}）")
        with r2:
            render_big_card(
                "浮盈 / 浮亏",
                f"{money(metrics.profit_loss)}  ({pct(metrics.profit_loss_pct)})",
                pnl_class(metrics.profit_loss),
                pnl_badge(metrics.profit_loss),
            )

        r3, r4, r5 = st.columns(3)
        with r3:
            render_small_card("当前价格", f"{metrics.current_price:.4f}")
        with r4:
            render_small_card("股票市值", money(metrics.market_value))
        with r5:
            render_small_card("总成本", money(metrics.total_cost))

        r6, r7, r8 = st.columns(3)
        with r6:
            render_small_card("配资本金", money(metrics.financed_principal))
        with r7:
            render_small_card("总操盘资金", money(metrics.max_trading_capital))
        with r8:
            render_small_card("已用仓位", f"{money(metrics.used_position_amount)} ({pct(metrics.position_usage_pct)})")

        r9, r10, r11 = st.columns(3)
        with r9:
            render_small_card(
                "亏损占配资本金",
                pct(metrics.loss_vs_financed_principal),
                "loss" if metrics.loss_vs_financed_principal > 0 else "neutral",
            )
        with r10:
            render_small_card("当日手续费", money(metrics.daily_fee_amount))
        with r11:
            render_small_card("累计手续费", money(metrics.accumulated_fee))

        render_big_card(
            "扣费后净盈亏",
            f"{money(metrics.net_profit_after_fee)}  ({pct(metrics.net_profit_after_fee_pct_cost)})",
            pnl_class(metrics.net_profit_after_fee),
            pnl_badge(metrics.net_profit_after_fee),
        )

        st.markdown('<div class="section-title" style="margin-top:10px">风险线提示</div>', unsafe_allow_html=True)

        rr1, rr2 = st.columns(2)
        with rr1:
            if warning_70["triggered"]:
                render_big_card(
                    "提醒平仓线 70%",
                    f"已触发（目标价 {warning_70['target_price']:.4f}）",
                    "loss",
                    '<span class="pill-red">已到提醒线</span>'
                )
            else:
                render_big_card(
                    "提醒平仓线 70%",
                    f"下跌 {pct(warning_70['additional_drop_pct'])} / {money(warning_70['additional_drop_price'])}",
                    "neutral",
                    f'<span class="pill-gray">到价约 {warning_70["target_price"]:.4f}</span>'
                )
                st.caption(f"按当前仓位计算，再亏 {money(warning_70['remaining_loss_amount'])}， 70%警示线。")

        with rr2:
            if liquidation_80["triggered"]:
                render_big_card(
                    "强平线 80%",
                    f"已触发（目标价 {liquidation_80['target_price']:.4f}）",
                    "loss",
                    '<span class="pill-red">已到强平线</span>'
                )
            else:
                render_big_card(
                    "强平线 80%",
                    f"下跌 {pct(liquidation_80['additional_drop_pct'])} / {money(liquidation_80['additional_drop_price'])}",
                    "neutral",
                    f'<span class="pill-gray">到价约 {liquidation_80["target_price"]:.4f}</span>'
                )
                st.caption(f"按当前仓位计算，再亏 {money(liquidation_80['remaining_loss_amount'])}， 80%强平线。")

        st.markdown('<div class="section-title" style="margin-top:10px">计算明细</div>', unsafe_allow_html=True)
        details = pd.DataFrame(
            [
                ["股票代码", metrics.stock_code],
                ["股票名称", metrics.stock_name],
                ["买入价格", metrics.buy_price],
                ["当前价格", metrics.current_price],
                ["买入股数", metrics.shares],
                ["总成本", metrics.total_cost],
                ["股票市值", metrics.market_value],
                ["浮盈/浮亏", metrics.profit_loss],
                ["盈亏比例", metrics.profit_loss_pct],
                ["配资本金", metrics.financed_principal],
                ["杠杆", metrics.leverage],
                ["总操盘资金", metrics.max_trading_capital],
                ["已用仓位", metrics.used_position_amount],
                ["已用仓位占比", metrics.position_usage_pct],
                ["亏损占配资本金", metrics.loss_vs_financed_principal],
                ["每日费率(%)", metrics.fee_daily_rate_pct],
                ["手续费计费基数", metrics.daily_fee_base],
                ["当日手续费", metrics.daily_fee_amount],
                ["持仓天数", metrics.held_days],
                ["累计手续费", metrics.accumulated_fee],
                ["扣费后净盈亏", metrics.net_profit_after_fee],
            ],
            columns=["项目", "数值"],
        )
        st.dataframe(details, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.info(f"数据库路径：{DB_PATH}")


if __name__ == "__main__":
    main()