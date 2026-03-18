import os
import re
import uuid
import sqlite3
import hashlib
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import pandas as pd
import requests
import streamlit as st
import extra_streamlit_components as stx

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None


st.set_page_config(page_title="个人交易中心", page_icon="📈", layout="wide")

st.markdown(
    """
    <style>
    .block-container {padding-top: 1rem; padding-bottom: 2rem; max-width: 1500px;}
    .big-title {font-size: 2.3rem; font-weight: 800; margin-bottom: 0.2rem;}
    .subtle {color: #6b7280; font-size: 1rem; margin-bottom: 1rem;}
    .card {
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 18px;
        padding: 18px 18px 14px 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.98));
        box-shadow: 0 8px 20px rgba(15,23,42,0.05);
        margin-bottom: 12px;
    }
    .hero {
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 22px;
        padding: 22px;
        background: linear-gradient(135deg, rgba(255,255,255,0.98), rgba(240,249,255,0.95));
        box-shadow: 0 10px 24px rgba(15,23,42,0.06);
        margin-bottom: 12px;
    }
    .kpi-label {font-size: 0.95rem; color: #6b7280; margin-bottom: 6px;}
    .kpi-value {font-size: 2rem; font-weight: 800; line-height: 1.15;}
    .kpi-small {font-size: 1.18rem; font-weight: 700; line-height: 1.25;}

    /* A股风格：红涨绿跌 */
    .profit { color: #dc2626; }
    .loss { color: #16a34a; }
    .neutral { color: #111827; }

    .section-title {font-size: 1.15rem; font-weight: 760; margin: 0 0 10px 0;}
    .hint {font-size: 0.88rem; color: #6b7280;}
    .pill-green, .pill-red, .pill-gray, .pill-blue {
        display: inline-block; padding: 6px 10px; border-radius: 999px; font-size: 0.88rem; font-weight: 700;
    }

    /* A股风格：盈利红、亏损绿 */
    .pill-green {background: rgba(220,38,38,0.12); color: #b91c1c;}
    .pill-red {background: rgba(22,163,74,0.12); color: #15803d;}
    .pill-gray {background: rgba(100,116,139,0.12); color: #475569;}
    .pill-blue {background: rgba(37,99,235,0.12); color: #1d4ed8;}

    div[data-testid="stMetric"] {
        background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.98));
        border: 1px solid rgba(0,0,0,0.08);
        padding: 14px 16px;
        border-radius: 18px;
        box-shadow: 0 8px 20px rgba(15,23,42,0.04);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path.home())))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "stock_margin_tracker.db"
AUTH_COOKIE_NAME = "ytc_auth_token"

DEFAULT_USERS = [
    {"login_name": "admin", "display_name": "管理员", "password": "060913yu", "role": "admin", "mode": "admin"},
    {"login_name": "俞", "display_name": "俞", "password": "060913", "role": "user", "mode": "margin"},
    {"login_name": "俞（普通账户）", "display_name": "俞（普通账户）", "password": "060913yu", "role": "user", "mode": "normal"},
    {"login_name": "俞（小账户）", "display_name": "俞（小账户）", "password": "060913yu", "role": "user", "mode": "normal"},
    {"login_name": "管", "display_name": "管", "password": "123456", "role": "user", "mode": "normal"},
]

SESSION_MINUTES_MAP = {
    "30分钟": 30,
    "60分钟": 60,
    "120分钟": 120,
}


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


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mode_label(mode: str) -> str:
    if mode == "margin":
        return "融资模式"
    if mode == "normal":
        return "普通模式"
    return "管理员"


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


def render_big_card(label: str, value: str, class_name: str = "neutral", badge: Optional[str] = None):
    badge_html = f'<div style="margin-top:8px">{badge}</div>' if badge else ""
    st.markdown(
        f'''
        <div class="card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {class_name}">{value}</div>
            {badge_html}
        </div>
        ''',
        unsafe_allow_html=True,
    )


def render_small_card(label: str, value: str, class_name: str = "neutral"):
    st.markdown(
        f'''
        <div class="card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-small {class_name}">{value}</div>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def render_login_hero():
    st.markdown(
        '''
        <div class="hero">
            <div class="big-title">📈 个人交易中心</div>
            <div class="subtle">支持多账户登录、普通账户、融资账户、管理员后台、多账户总览和实时股票行情。</div>
            <div style="margin-top:8px;"><span class="pill-blue">普通模式</span> <span class="pill-red">融资模式</span> <span class="pill-gray">管理员后台</span></div>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_name TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            mode TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS normal_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            buy_price REAL NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            buy_time TEXT,
            total_cost REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS margin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            buy_price REAL NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            buy_time TEXT,
            total_cost REAL NOT NULL DEFAULT 0,
            financed_principal REAL NOT NULL DEFAULT 0,
            leverage REAL NOT NULL DEFAULT 10,
            fee_daily_rate_pct REAL NOT NULL DEFAULT 0.3,
            held_days REAL NOT NULL DEFAULT 1,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_login TEXT NOT NULL,
            action TEXT NOT NULL,
            target_login TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    conn.commit()

    for u in DEFAULT_USERS:
        cur.execute("SELECT id FROM users WHERE login_name = ?", (u["login_name"],))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO users (login_name, display_name, password_hash, role, mode, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    u["login_name"],
                    u["display_name"],
                    hash_password(u["password"]),
                    u["role"],
                    u["mode"],
                    now_str(),
                    now_str(),
                ),
            )

    conn.commit()
    conn.close()


def log_action(actor_login: str, action: str, target_login: str = "", details: str = "") -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO audit_logs (actor_login, action, target_login, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor_login, action, target_login, details, now_str()),
    )
    conn.commit()
    conn.close()


def get_all_users() -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_by_login(login_name: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE login_name = ? AND is_active = 1", (login_name,))
    row = cur.fetchone()
    conn.close()
    return row


def create_user(login_name: str, display_name: str, password: str, role: str, mode: str, actor: str) -> Tuple[bool, str]:
    if not login_name.strip() or not password.strip():
        return False, "用户名和密码不能为空。"
    if get_user_by_login(login_name.strip()):
        return False, "该用户名已存在。"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (login_name, display_name, password_hash, role, mode, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            login_name.strip(),
            display_name.strip() or login_name.strip(),
            hash_password(password.strip()),
            role,
            mode,
            now_str(),
            now_str(),
        ),
    )
    conn.commit()
    conn.close()
    log_action(actor, "新增用户", login_name.strip(), f"role={role}, mode={mode}")
    return True, "新增用户成功。"


def update_user_password(login_name: str, new_password: str, actor: str) -> Tuple[bool, str]:
    if not new_password.strip():
        return False, "新密码不能为空。"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE login_name = ?",
        (hash_password(new_password.strip()), now_str(), login_name),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        log_action(actor, "修改密码", login_name, "")
        return True, "密码修改成功。"
    return False, "未找到该用户。"


def update_user_mode(login_name: str, mode: str, actor: str) -> Tuple[bool, str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET mode = ?, updated_at = ? WHERE login_name = ?", (mode, now_str(), login_name))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        log_action(actor, "修改用户模式", login_name, f"mode={mode}")
        return True, "用户模式修改成功。"
    return False, "未找到该用户。"


def delete_user(login_name: str, actor: str) -> Tuple[bool, str]:
    if login_name == "admin":
        return False, "不能删除管理员账户。"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_active = 0, updated_at = ? WHERE login_name = ?", (now_str(), login_name))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    if changed:
        log_action(actor, "停用用户", login_name, "")
        return True, "用户已停用。"
    return False, "未找到该用户。"


def get_user_positions_normal(user_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM normal_positions WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_positions_margin(user_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM margin_positions WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def save_normal_position(user_id: int, stock_code: str, stock_name: str, buy_price: float, shares: int, buy_time: str, total_cost: float, note: str, actor: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO normal_positions (user_id, stock_code, stock_name, buy_price, shares, buy_time, total_cost, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, normalize_code(stock_code), stock_name, buy_price, shares, buy_time, total_cost, note, now_str(), now_str()),
    )
    conn.commit()
    conn.close()
    log_action(actor, "新增普通持仓", str(user_id), f"{stock_code} / {shares}股")


def save_margin_position(
    user_id: int,
    stock_code: str,
    stock_name: str,
    buy_price: float,
    shares: int,
    buy_time: str,
    total_cost: float,
    financed_principal: float,
    leverage: float,
    fee_daily_rate_pct: float,
    held_days: float,
    note: str,
    actor: str,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO margin_positions (user_id, stock_code, stock_name, buy_price, shares, buy_time, total_cost, financed_principal, leverage, fee_daily_rate_pct, held_days, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            normalize_code(stock_code),
            stock_name,
            buy_price,
            shares,
            buy_time,
            total_cost,
            financed_principal,
            leverage,
            fee_daily_rate_pct,
            held_days,
            note,
            now_str(),
            now_str(),
        ),
    )
    conn.commit()
    conn.close()
    log_action(actor, "新增融资持仓", str(user_id), f"{stock_code} / {shares}股")


def update_normal_position(
    position_id: int,
    stock_code: str,
    stock_name: str,
    buy_price: float,
    shares: int,
    buy_time: str,
    total_cost: float,
    note: str,
    actor: str,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE normal_positions
        SET stock_code = ?, stock_name = ?, buy_price = ?, shares = ?, buy_time = ?, total_cost = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            normalize_code(stock_code),
            stock_name,
            buy_price,
            shares,
            buy_time,
            total_cost,
            note,
            now_str(),
            position_id,
        ),
    )
    conn.commit()
    conn.close()
    log_action(actor, "修改普通持仓", str(position_id), f"{stock_code} / {shares}股")


def update_margin_position(
    position_id: int,
    stock_code: str,
    stock_name: str,
    buy_price: float,
    shares: int,
    buy_time: str,
    total_cost: float,
    financed_principal: float,
    leverage: float,
    fee_daily_rate_pct: float,
    held_days: float,
    note: str,
    actor: str,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE margin_positions
        SET stock_code = ?, stock_name = ?, buy_price = ?, shares = ?, buy_time = ?, total_cost = ?,
            financed_principal = ?, leverage = ?, fee_daily_rate_pct = ?, held_days = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            normalize_code(stock_code),
            stock_name,
            buy_price,
            shares,
            buy_time,
            total_cost,
            financed_principal,
            leverage,
            fee_daily_rate_pct,
            held_days,
            note,
            now_str(),
            position_id,
        ),
    )
    conn.commit()
    conn.close()
    log_action(actor, "修改融资持仓", str(position_id), f"{stock_code} / {shares}股")


def delete_position(table_name: str, position_id: int, actor: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table_name} WHERE id = ?", (position_id,))
    conn.commit()
    conn.close()
    log_action(actor, "删除持仓", str(position_id), table_name)


def get_audit_logs(limit: int = 100) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def create_session(user_id: int, remember_minutes: int) -> Tuple[str, str]:
    token = str(uuid.uuid4())
    expires_at = (datetime.now() + timedelta(minutes=remember_minutes)).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_sessions (user_id, session_token, expires_at, is_active, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        """,
        (user_id, token, expires_at, now_str(), now_str()),
    )
    conn.commit()
    conn.close()
    return token, expires_at


def get_valid_session(token: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.*, u.login_name, u.display_name, u.role, u.mode
        FROM user_sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.session_token = ? AND s.is_active = 1 AND u.is_active = 1
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        if datetime.now() >= datetime.fromisoformat(row["expires_at"]):
            return None
    except Exception:
        return None
    return row


def invalidate_session(token: str) -> None:
    if not token:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE user_sessions SET is_active = 0, updated_at = ? WHERE session_token = ?", (now_str(), token))
    conn.commit()
    conn.close()


def _quote_from_eastmoney(code: str) -> Tuple[str, float, float]:
    code = normalize_code(code)
    secid = f"1.{code}" if code.startswith(("60", "68", "90", "51", "58")) else f"0.{code}"
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f57,f58,f43,f46",
        "invt": "2",
        "fltt": "2",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    r = requests.get(url, params=params, headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json().get("data") or {}

    stock_name = str(data.get("f58") or code)

    raw_current = safe_float(data.get("f43"), 0.0)
    raw_open = safe_float(data.get("f46"), 0.0)

    current_price = raw_current / 100 if raw_current > 1000 else raw_current
    open_price = raw_open / 100 if raw_open > 1000 else raw_open

    if current_price <= 0:
        raise RuntimeError("东方财富接口当前价格无效")

    return stock_name, current_price, open_price


def _quote_from_tencent(code: str) -> Tuple[str, float, float]:
    code = normalize_code(code)
    symbol = f"{get_market_prefix(code)}{code}"
    url = f"https://qt.gtimg.cn/q={symbol}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    r = requests.get(url, headers=headers, timeout=8)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text.strip()
    if "~" not in text:
        raise RuntimeError("腾讯接口返回格式异常")
    parts = text.split("~")
    if len(parts) < 6:
        raise RuntimeError("腾讯接口字段不足")
    stock_name = parts[1].strip() or code
    current_price = safe_float(parts[3], 0.0)
    open_price = safe_float(parts[5], 0.0)
    if current_price <= 0:
        raise RuntimeError("腾讯接口当前价格无效")
    return stock_name, current_price, open_price


def _quote_from_sina(code: str) -> Tuple[str, float, float]:
    code = normalize_code(code)
    symbol = f"{get_market_prefix(code)}{code}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
    r = requests.get(url, headers=headers, timeout=8)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text.strip()
    if "=" not in text:
        raise RuntimeError("新浪接口返回格式异常")
    raw = text.split("=", 1)[1].strip().rstrip(";").strip().strip('"')
    if not raw:
        raise RuntimeError("新浪接口未返回有效行情数据")
    fields = raw.split(",")
    if len(fields) < 4:
        raise RuntimeError("新浪接口字段不足")
    stock_name = fields[0].strip() or code
    open_price = safe_float(fields[1], 0.0)
    current_price = safe_float(fields[3], 0.0)
    if current_price <= 0:
        raise RuntimeError("新浪接口当前价格无效")
    return stock_name, current_price, open_price


def get_realtime_quote(code: str) -> Tuple[str, float, float]:
    errors = []
    for fn in (_quote_from_eastmoney, _quote_from_tencent, _quote_from_sina):
        try:
            return fn(code)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError("；".join(errors))


@st.cache_data(ttl=2)
def cached_quote(code: str) -> Tuple[str, float, float]:
    return get_realtime_quote(code)


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


def calculate_risk_line_info(total_cost: float, current_price: float, shares: int, financed_principal: float, line_pct: float, current_profit_loss: float) -> dict:
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
    additional_drop_pct = max(0.0, additional_drop_price / current_price * 100) if current_price > 0 else 0.0
    triggered = current_loss_amount >= threshold_loss_amount and threshold_loss_amount > 0
    return {
        "target_price": target_price,
        "additional_drop_price": additional_drop_price,
        "additional_drop_pct": additional_drop_pct,
        "remaining_loss_amount": remaining_loss_amount,
        "triggered": triggered,
    }


def get_cookie_manager():
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager


def bootstrap_session_state():
    if "auth" not in st.session_state:
        st.session_state.auth = None
    if "cookie_retry" not in st.session_state:
        st.session_state.cookie_retry = 0


def try_restore_login_from_cookie():
    bootstrap_session_state()

    if st.session_state.auth:
        return

    cookie_manager = get_cookie_manager()
    token = cookie_manager.get(AUTH_COOKIE_NAME)

    if token is None:
        if st.session_state.cookie_retry < 2:
            st.session_state.cookie_retry += 1
            st.stop()
        return

    st.session_state.cookie_retry = 0

    if not token:
        return

    session_row = get_valid_session(token)
    if not session_row:
        try:
            cookie_manager.delete(AUTH_COOKIE_NAME, key=f"delete_invalid_{uuid.uuid4()}")
        except Exception:
            pass
        return

    st.session_state.auth = {
        "user_id": session_row["user_id"],
        "login_name": session_row["login_name"],
        "display_name": session_row["display_name"],
        "role": session_row["role"],
        "mode": session_row["mode"],
        "expires_at": session_row["expires_at"],
    }


def is_auth_valid() -> bool:
    auth = st.session_state.get("auth")
    if not auth:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(auth["expires_at"])
    except Exception:
        return False


def login_user(user_row: sqlite3.Row, remember_minutes: int):
    token, expires_at = create_session(user_row["id"], remember_minutes)
    cookie_manager = get_cookie_manager()
    expire_dt = datetime.now() + timedelta(minutes=remember_minutes)

    try:
        cookie_manager.set(
            AUTH_COOKIE_NAME,
            token,
            expires_at=expire_dt,
            key=f"set_cookie_{token}",
        )
    except Exception:
        pass

    st.session_state.auth = {
        "user_id": user_row["id"],
        "login_name": user_row["login_name"],
        "display_name": user_row["display_name"],
        "role": user_row["role"],
        "mode": user_row["mode"],
        "expires_at": expires_at,
    }


def logout_user():
    cookie_manager = get_cookie_manager()
    token = cookie_manager.get(AUTH_COOKIE_NAME)
    if token:
        invalidate_session(token)
    try:
        cookie_manager.delete(AUTH_COOKIE_NAME, key=f"delete_cookie_{uuid.uuid4()}")
    except Exception:
        pass
    st.session_state.auth = None
    st.rerun()


def current_user() -> Optional[dict]:
    if not is_auth_valid():
        return None
    return st.session_state.auth


def check_login() -> bool:
    try_restore_login_from_cookie()
    if is_auth_valid():
        return True

    st.session_state.auth = None
    render_login_hero()

    users = get_all_users()
    user_choices = [u["login_name"] for u in users]

    c1, c2, c3 = st.columns([1.1, 1.2, 1.1])
    with c2:
        st.markdown('<div class="card">', unsafe_allow_html=True)

        with st.form("login_form", clear_on_submit=False):
            selected_user = st.selectbox("选择用户", user_choices, index=0 if user_choices else None)
            password = st.text_input("密码", type="password", placeholder="请输入密码")
            remember_choice = st.radio("免重复验证", list(SESSION_MINUTES_MAP.keys()), horizontal=True, index=1)
            submitted = st.form_submit_button("登录", use_container_width=True, type="primary")

        if submitted:
            user_row = get_user_by_login(selected_user)
            if not user_row:
                st.error("该用户不存在或已停用。")
            elif hash_password(password) == user_row["password_hash"]:
                remember_minutes = SESSION_MINUTES_MAP[remember_choice]
                login_user(user_row, remember_minutes)
                log_action(user_row["login_name"], "登录系统", user_row["login_name"], f"记住{remember_minutes}分钟")
                st.success("登录成功")
                st.rerun()
            else:
                st.error("密码错误")

        st.markdown("</div>", unsafe_allow_html=True)
    return False


def topbar(user: dict):
    c1, c2 = st.columns([5, 1])
    with c1:
        st.markdown('<div class="big-title">📈 个人交易中心</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="subtle">当前登录：{user["display_name"]} · {mode_label(user["mode"])}</div>', unsafe_allow_html=True)
    with c2:
        if st.button("退出登录", use_container_width=True):
            logout_user()


def sidebar_common(user: dict):
    with st.sidebar:
        st.markdown('<div class="section-title">系统设置</div>', unsafe_allow_html=True)
        if st.button("清缓存并刷新价格", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.markdown("---")
        st.markdown(f'<div class="hint">数据库路径：{DB_PATH}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="hint">当前用户：{user["display_name"]}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="hint">当前模式：{mode_label(user["mode"])}</div>', unsafe_allow_html=True)


def render_normal_position_card(row: sqlite3.Row):
    stock_name = row["stock_name"] or row["stock_code"]
    current_price = 0.0
    open_price = 0.0
    quote_ok = False
    try:
        stock_name, current_price, open_price = cached_quote(row["stock_code"])
        quote_ok = True
    except Exception:
        pass

    buy_price = safe_float(row["buy_price"], 0.0)
    shares = int(row["shares"] or 0)
    total_cost = safe_float(row["total_cost"], buy_price * shares)
    market_value = current_price * shares if quote_ok else 0.0
    total_profit = market_value - total_cost if quote_ok else 0.0
    total_profit_pct = (total_profit / total_cost * 100) if total_cost > 0 and quote_ok else 0.0
    daily_profit = (current_price - open_price) * shares if quote_ok and open_price > 0 else 0.0
    daily_profit_pct = ((current_price - open_price) / open_price * 100) if quote_ok and open_price > 0 else 0.0

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown(f'**{stock_name}（{row["stock_code"]}）**')
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("现价", f"{current_price:.4f}" if quote_ok else "--")
    c2.metric("总盈亏", money(total_profit), delta=pct(total_profit_pct))
    c3.metric("当日浮盈亏", money(daily_profit), delta=pct(daily_profit_pct))
    c4.metric("总成本", money(total_cost))
    st.caption(f'买入价：{buy_price:.4f} ｜ 股数：{shares:,} ｜ 买入时间：{row["buy_time"] or "--"}')
    st.markdown("</div>", unsafe_allow_html=True)


def render_margin_position_card(row: sqlite3.Row):
    stock_name = row["stock_name"] or row["stock_code"]
    current_price = 0.0
    try:
        stock_name, current_price, _ = cached_quote(row["stock_code"])
    except Exception:
        pass

    metrics = calculate_metrics(
        stock_code=row["stock_code"],
        stock_name=stock_name,
        buy_price=row["buy_price"],
        current_price=current_price,
        shares=int(row["shares"] or 0),
        override_total_cost_enabled=True,
        override_total_cost=row["total_cost"],
        financed_principal=row["financed_principal"],
        leverage=row["leverage"],
        fee_daily_rate_pct=row["fee_daily_rate_pct"],
        held_days=row["held_days"],
    )

    warning_70 = calculate_risk_line_info(metrics.total_cost, metrics.current_price, metrics.shares, metrics.financed_principal, 0.70, metrics.profit_loss)
    liq_80 = calculate_risk_line_info(metrics.total_cost, metrics.current_price, metrics.shares, metrics.financed_principal, 0.80, metrics.profit_loss)

    if warning_70["triggered"]:
        warning_70_text = "已触发"
    else:
        warning_70_text = f"还需下跌 {pct(warning_70['additional_drop_pct'])} / {money(warning_70['additional_drop_price'])}"

    if liq_80["triggered"]:
        liq_80_text = "已触发"
    else:
        liq_80_text = f"还需下跌 {pct(liq_80['additional_drop_pct'])} / {money(liq_80['additional_drop_price'])}"

    used_vs_financed_principal_pct = (
        metrics.used_position_amount / metrics.financed_principal * 100
        if metrics.financed_principal > 0 else 0.0
    )

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown(f'**{stock_name}（{row["stock_code"]}）**')
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("现价", f"{metrics.current_price:.4f}" if metrics.current_price > 0 else "--")
    c2.metric("浮盈亏", money(metrics.profit_loss), delta=pct(metrics.profit_loss_pct))
    c3.metric("占用总本金", pct(used_vs_financed_principal_pct))
    c4.metric("累计利息", money(metrics.accumulated_fee))
    c5, c6 = st.columns(2)
    with c5:
        st.caption(f"70%线：{warning_70_text}")
    with c6:
        st.caption(f"80%线：{liq_80_text}")
    st.caption(f'买入价：{metrics.buy_price:.4f} ｜ 股数：{metrics.shares:,} ｜ 杠杆：{metrics.leverage:.2f} ｜ 买入时间：{row["buy_time"] or "--"}')
    st.markdown("</div>", unsafe_allow_html=True)


def multi_account_overview(user: dict):
    if st_autorefresh is not None:
        st_autorefresh(interval=2000, key="admin_overview_refresh")

    st.markdown('<div class="section-title">多账户总览</div>', unsafe_allow_html=True)
    users = [u for u in get_all_users() if u["role"] != "admin"]
    selected_users = st.multiselect(
        "选择要查看的账户",
        [u["login_name"] for u in users],
        default=[u["login_name"] for u in users],
    )

    if not selected_users:
        st.info("请选择至少一个账户。")
        return

    selected_rows = [u for u in users if u["login_name"] in selected_users]
    for u in selected_rows:
        st.markdown(f'<div class="section-title">{u["display_name"]} · {mode_label(u["mode"])}</div>', unsafe_allow_html=True)
        if u["mode"] == "margin":
            rows = get_user_positions_margin(u["id"])
            if rows:
                for row in rows:
                    render_margin_position_card(row)
            else:
                st.info("暂无融资持仓。")
        else:
            rows = get_user_positions_normal(u["id"])
            if rows:
                for row in rows:
                    render_normal_position_card(row)
            else:
                st.info("暂无普通持仓。")


def admin_panel(user: dict):
    tabs = st.tabs(["多账户总览", "用户管理", "普通账户持仓管理", "融资账户持仓管理", "操作日志"])

    with tabs[0]:
        multi_account_overview(user)

    with tabs[1]:
        left, right = st.columns([1, 1.2])
        with left:
            st.markdown('<div class="section-title">新增用户</div>', unsafe_allow_html=True)
            new_login = st.text_input("登录名")
            new_display = st.text_input("显示名")
            new_password = st.text_input("初始密码", type="password")
            new_role = st.selectbox("角色", ["user", "admin"], index=0)
            new_mode = st.selectbox("账户模式", ["normal", "margin"], format_func=lambda x: "普通模式" if x == "normal" else "融资模式")
            if st.button("新增用户", type="primary"):
                ok, msg = create_user(new_login, new_display, new_password, new_role, new_mode, user["login_name"])
                st.success(msg) if ok else st.error(msg)
                if ok:
                    st.rerun()

        with right:
            users = get_all_users()
            if users:
                df = pd.DataFrame(
                    [{"登录名": u["login_name"], "显示名": u["display_name"], "角色": u["role"], "模式": mode_label(u["mode"]), "创建时间": u["created_at"]} for u in users]
                )
                st.dataframe(df, use_container_width=True, hide_index=True)

                selected = st.selectbox("选择要编辑的用户", [u["login_name"] for u in users])
                c1, c2, c3 = st.columns(3)
                with c1:
                    new_pwd = st.text_input("新密码", type="password", key="admin_new_pwd")
                    if st.button("修改密码"):
                        ok, msg = update_user_password(selected, new_pwd, user["login_name"])
                        st.success(msg) if ok else st.error(msg)
                with c2:
                    mode_val = st.selectbox("切换模式", ["normal", "margin"], format_func=lambda x: "普通模式" if x == "normal" else "融资模式")
                    if st.button("修改模式"):
                        ok, msg = update_user_mode(selected, mode_val, user["login_name"])
                        st.success(msg) if ok else st.error(msg)
                with c3:
                    if st.button("停用用户"):
                        ok, msg = delete_user(selected, user["login_name"])
                        st.success(msg) if ok else st.error(msg)
                        if ok:
                            st.rerun()

    with tabs[2]:
        normal_users = [u for u in get_all_users() if u["mode"] == "normal"]
        if normal_users:
            target_login = st.selectbox("选择普通账户用户", [u["login_name"] for u in normal_users], key="normal_target")
            target_user = get_user_by_login(target_login)

            st.markdown('<div class="section-title">新增普通持仓</div>', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            with c1:
                n_code = st.text_input("股票代码", key="n_code")
                n_buy_price = st.number_input("买入价格", min_value=0.0, step=0.01, format="%.4f", key="n_buy_price")
                n_buy_time = st.text_input("买入时间", value=now_str(), key="n_buy_time")
            with c2:
                n_shares = st.number_input("买入股数", min_value=0, step=100, key="n_shares")
                n_total_cost = n_buy_price * int(n_shares)
                st.number_input("总成本（自动计算）", min_value=0.0, value=float(n_total_cost), step=100.0, format="%.2f", disabled=True, key="n_total_cost_display")
                n_note = st.text_input("备注", key="n_note")
            with c3:
                st.write("")
                st.write("")
                if st.button("保存普通持仓", type="primary"):
                    stock_name = normalize_code(n_code)
                    try:
                        stock_name, _, _ = cached_quote(n_code)
                    except Exception:
                        pass
                    save_normal_position(target_user["id"], n_code, stock_name, n_buy_price, int(n_shares), n_buy_time, n_total_cost, n_note, user["login_name"])
                    st.success("普通账户持仓已保存。")
                    st.rerun()

            rows = get_user_positions_normal(target_user["id"])
            if rows:
                df = pd.DataFrame(
                    [{"ID": r["id"], "股票代码": r["stock_code"], "股票名称": r["stock_name"], "买入价格": r["buy_price"], "股数": r["shares"], "买入时间": r["buy_time"], "总成本": r["total_cost"], "备注": r["note"]} for r in rows]
                )
                st.dataframe(df, use_container_width=True, hide_index=True)

                st.markdown('<div class="section-title">编辑普通持仓</div>', unsafe_allow_html=True)
                edit_id = st.selectbox("选择普通持仓ID", [r["id"] for r in rows], key="edit_normal_id")
                selected_row = next(r for r in rows if r["id"] == edit_id)

                e1, e2, e3 = st.columns(3)
                with e1:
                    en_code = st.text_input("股票代码", value=selected_row["stock_code"], key="en_code")
                    en_buy_price = st.number_input("买入价格", min_value=0.0, step=0.01, format="%.4f", value=float(selected_row["buy_price"]), key="en_buy_price")
                    en_buy_time = st.text_input("买入时间", value=str(selected_row["buy_time"] or ""), key="en_buy_time")
                with e2:
                    en_shares = st.number_input("买入股数", min_value=0, step=100, value=int(selected_row["shares"]), key="en_shares")
                    en_total_cost = en_buy_price * int(en_shares)
                    st.number_input("总成本（自动计算）", min_value=0.0, value=float(en_total_cost), step=100.0, format="%.2f", disabled=True, key="en_total_cost")
                    en_note = st.text_input("备注", value=str(selected_row["note"] or ""), key="en_note")
                with e3:
                    st.write("")
                    st.write("")
                    if st.button("保存普通持仓修改", type="primary"):
                        stock_name = normalize_code(en_code)
                        try:
                            stock_name, _, _ = cached_quote(en_code)
                        except Exception:
                            pass
                        update_normal_position(edit_id, en_code, stock_name, en_buy_price, int(en_shares), en_buy_time, en_total_cost, en_note, user["login_name"])
                        st.success("普通持仓修改成功。")
                        st.rerun()

                delete_id = st.selectbox("删除普通持仓ID", [r["id"] for r in rows], key="delete_normal_id")
                if st.button("删除该普通持仓"):
                    delete_position("normal_positions", int(delete_id), user["login_name"])
                    st.success("普通持仓已删除。")
                    st.rerun()

    with tabs[3]:
        margin_users = [u for u in get_all_users() if u["mode"] == "margin"]
        if margin_users:
            target_login = st.selectbox("选择融资账户用户", [u["login_name"] for u in margin_users], key="margin_target")
            target_user = get_user_by_login(target_login)

            st.markdown('<div class="section-title">新增融资持仓</div>', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            with c1:
                m_code = st.text_input("股票代码", key="m_code")
                m_buy_price = st.number_input("买入价格", min_value=0.0, step=0.01, format="%.4f", key="m_buy_price")
                m_buy_time = st.text_input("买入时间", value=now_str(), key="m_buy_time")
                m_shares = st.number_input("买入股数", min_value=0, step=100, key="m_shares")
            with c2:
                m_total_cost = m_buy_price * int(m_shares)
                st.number_input("总成本（自动计算）", min_value=0.0, value=float(m_total_cost), step=100.0, format="%.2f", disabled=True, key="m_total_cost_display")
                m_financed = st.number_input("配资本金", min_value=0.0, step=1000.0, format="%.2f", key="m_financed")
                m_leverage = st.number_input("杠杆", min_value=0.0, step=1.0, format="%.2f", key="m_leverage")
            with c3:
                m_fee = st.number_input("日利息(%)", min_value=0.0, step=0.01, format="%.4f", key="m_fee")
                m_days = st.number_input("持仓天数", min_value=0.0, step=1.0, format="%.2f", key="m_days")
                m_note = st.text_input("备注", key="m_note")
                if st.button("保存融资持仓", type="primary"):
                    stock_name = normalize_code(m_code)
                    try:
                        stock_name, _, _ = cached_quote(m_code)
                    except Exception:
                        pass
                    save_margin_position(target_user["id"], m_code, stock_name, m_buy_price, int(m_shares), m_buy_time, m_total_cost, m_financed, m_leverage, m_fee, m_days, m_note, user["login_name"])
                    st.success("融资持仓已保存。")
                    st.rerun()

            rows = get_user_positions_margin(target_user["id"])
            if rows:
                df = pd.DataFrame(
                    [{"ID": r["id"], "股票代码": r["stock_code"], "股票名称": r["stock_name"], "买入价格": r["buy_price"], "股数": r["shares"], "总成本": r["total_cost"], "配资本金": r["financed_principal"], "杠杆": r["leverage"], "日利息": r["fee_daily_rate_pct"], "持仓天数": r["held_days"]} for r in rows]
                )
                st.dataframe(df, use_container_width=True, hide_index=True)

                st.markdown('<div class="section-title">编辑融资持仓</div>', unsafe_allow_html=True)
                edit_id = st.selectbox("选择融资持仓ID", [r["id"] for r in rows], key="edit_margin_id")
                selected_row = next(r for r in rows if r["id"] == edit_id)

                e1, e2, e3 = st.columns(3)
                with e1:
                    em_code = st.text_input("股票代码", value=selected_row["stock_code"], key="em_code")
                    em_buy_price = st.number_input("买入价格", min_value=0.0, step=0.01, format="%.4f", value=float(selected_row["buy_price"]), key="em_buy_price")
                    em_buy_time = st.text_input("买入时间", value=str(selected_row["buy_time"] or ""), key="em_buy_time")
                    em_shares = st.number_input("买入股数", min_value=0, step=100, value=int(selected_row["shares"]), key="em_shares")
                with e2:
                    em_total_cost = em_buy_price * int(em_shares)
                    st.number_input("总成本（自动计算）", min_value=0.0, value=float(em_total_cost), step=100.0, format="%.2f", disabled=True, key="em_total_cost")
                    em_financed = st.number_input("配资本金", min_value=0.0, step=1000.0, format="%.2f", value=float(selected_row["financed_principal"]), key="em_financed")
                    em_leverage = st.number_input("杠杆", min_value=0.0, step=1.0, format="%.2f", value=float(selected_row["leverage"]), key="em_leverage")
                with e3:
                    em_fee = st.number_input("日利息(%)", min_value=0.0, step=0.01, format="%.4f", value=float(selected_row["fee_daily_rate_pct"]), key="em_fee")
                    em_days = st.number_input("持仓天数", min_value=0.0, step=1.0, format="%.2f", value=float(selected_row["held_days"]), key="em_days")
                    em_note = st.text_input("备注", value=str(selected_row["note"] or ""), key="em_note")
                    if st.button("保存融资持仓修改", type="primary"):
                        stock_name = normalize_code(em_code)
                        try:
                            stock_name, _, _ = cached_quote(em_code)
                        except Exception:
                            pass
                        update_margin_position(edit_id, em_code, stock_name, em_buy_price, int(em_shares), em_buy_time, em_total_cost, em_financed, em_leverage, em_fee, em_days, em_note, user["login_name"])
                        st.success("融资持仓修改成功。")
                        st.rerun()

                delete_id = st.selectbox("删除融资持仓ID", [r["id"] for r in rows], key="delete_margin_id")
                if st.button("删除该融资持仓"):
                    delete_position("margin_positions", int(delete_id), user["login_name"])
                    st.success("融资持仓已删除。")
                    st.rerun()

    with tabs[4]:
        logs = get_audit_logs(200)
        if logs:
            df = pd.DataFrame(
                [{"时间": r["created_at"], "操作者": r["actor_login"], "动作": r["action"], "目标": r["target_login"], "详情": r["details"]} for r in logs]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("暂无日志。")


def normal_system(user: dict):
    if st_autorefresh is not None:
        st_autorefresh(interval=2000, key=f'normal_auto_{user["user_id"]}')

    st.markdown('<div class="section-title">普通股票系统</div>', unsafe_allow_html=True)
    positions = get_user_positions_normal(user["user_id"])
    if not positions:
        st.warning("当前没有持仓数据。请让管理员先录入普通账户持仓。")
        return

    options = [f'{p["stock_name"] or p["stock_code"]}（{p["stock_code"]}）- ID {p["id"]}' for p in positions]
    idx = st.selectbox("选择持仓", range(len(options)), format_func=lambda i: options[i])
    row = positions[idx]

    stock_name = row["stock_name"] or row["stock_code"]
    current_price = 0.0
    open_price = 0.0
    quote_ok = False
    try:
        stock_name, current_price, open_price = cached_quote(row["stock_code"])
        quote_ok = True
    except Exception as e:
        st.error(f"实时价格获取失败：{e}")

    buy_price = safe_float(row["buy_price"], 0.0)
    shares = int(row["shares"] or 0)
    total_cost = safe_float(row["total_cost"], buy_price * shares)
    market_value = current_price * shares if quote_ok else 0.0
    total_profit = market_value - total_cost if quote_ok else 0.0
    total_profit_pct = (total_profit / total_cost * 100) if total_cost > 0 and quote_ok else 0.0
    daily_profit = (current_price - open_price) * shares if quote_ok and open_price > 0 else 0.0
    daily_profit_pct = ((current_price - open_price) / open_price * 100) if quote_ok and open_price > 0 else 0.0

    c1, c2 = st.columns([1, 1])
    with c1:
        render_big_card("股票名称", f"{stock_name}（{row['stock_code']}）")
    with c2:
        render_big_card("总盈亏", f"{money(total_profit)}  ({pct(total_profit_pct)})", pnl_class(total_profit), pnl_badge(total_profit))

    r1, r2, r3 = st.columns(3)
    r1.metric("现价", f"{current_price:.4f}" if quote_ok else "--")
    r2.metric("总成本", money(total_cost))
    r3.metric("股票市值", money(market_value) if quote_ok else "--")

    r4, r5, r6 = st.columns(3)
    r4.metric("买入价", f"{buy_price:.4f}")
    r5.metric("买入股数", f"{shares:,}")
    r6.metric("买入时间", str(row["buy_time"] or "--"))

    render_big_card("当日浮盈 / 浮亏", f"{money(daily_profit)}  ({pct(daily_profit_pct)})", pnl_class(daily_profit), pnl_badge(daily_profit))

    details = pd.DataFrame(
        [
            ["股票代码", row["stock_code"]],
            ["股票名称", stock_name],
            ["买入价格", buy_price],
            ["当前价格", current_price if quote_ok else None],
            ["买入股数", shares],
            ["买入时间", row["buy_time"]],
            ["总成本", total_cost],
            ["股票市值", market_value if quote_ok else None],
            ["总盈亏", total_profit if quote_ok else None],
            ["当日浮盈/浮亏", daily_profit if quote_ok else None],
            ["备注", row["note"]],
        ],
        columns=["项目", "数值"],
    )
    st.dataframe(details, use_container_width=True, hide_index=True)


def margin_system(user: dict):
    if st_autorefresh is not None:
        st_autorefresh(interval=2000, key=f'margin_auto_{user["user_id"]}')

    st.markdown('<div class="section-title">融资交易系统</div>', unsafe_allow_html=True)
    positions = get_user_positions_margin(user["user_id"])
    if not positions:
        st.warning("当前没有融资持仓数据。请让管理员先录入融资账户持仓。")
        return

    options = [f'{p["stock_name"] or p["stock_code"]}（{p["stock_code"]}）- ID {p["id"]}' for p in positions]
    idx = st.selectbox("选择融资持仓", range(len(options)), format_func=lambda i: options[i])
    row = positions[idx]

    stock_name = row["stock_name"] or row["stock_code"]
    current_price = 0.0
    try:
        stock_name, current_price, _ = cached_quote(row["stock_code"])
    except Exception as e:
        st.error(f"实时价格获取失败：{e}")

    metrics = calculate_metrics(
        stock_code=row["stock_code"],
        stock_name=stock_name,
        buy_price=row["buy_price"],
        current_price=current_price,
        shares=int(row["shares"] or 0),
        override_total_cost_enabled=True,
        override_total_cost=row["total_cost"],
        financed_principal=row["financed_principal"],
        leverage=row["leverage"],
        fee_daily_rate_pct=row["fee_daily_rate_pct"],
        held_days=row["held_days"],
    )

    warning_70 = calculate_risk_line_info(metrics.total_cost, metrics.current_price, metrics.shares, metrics.financed_principal, 0.70, metrics.profit_loss)
    liq_80 = calculate_risk_line_info(metrics.total_cost, metrics.current_price, metrics.shares, metrics.financed_principal, 0.80, metrics.profit_loss)

    c1, c2 = st.columns([1, 1])
    with c1:
        render_big_card("股票名称", f"{metrics.stock_name}（{metrics.stock_code}）")
    with c2:
        render_big_card("浮盈 / 浮亏", f"{money(metrics.profit_loss)}  ({pct(metrics.profit_loss_pct)})", pnl_class(metrics.profit_loss), pnl_badge(metrics.profit_loss))

    r1, r2, r3 = st.columns(3)
    r1.metric("现价", f"{metrics.current_price:.4f}" if metrics.current_price > 0 else "--")
    r2.metric("总成本", money(metrics.total_cost))
    r3.metric("股票市值", money(metrics.market_value))

    r4, r5, r6 = st.columns(3)
    r4.metric("配资本金", money(metrics.financed_principal))
    r5.metric("杠杆", f"{metrics.leverage:.2f}")
    r6.metric("累计手续费", money(metrics.accumulated_fee))

    render_big_card("扣费后净盈亏", f"{money(metrics.net_profit_after_fee)}  ({pct(metrics.net_profit_after_fee_pct_cost)})", pnl_class(metrics.net_profit_after_fee), pnl_badge(metrics.net_profit_after_fee))

    rr1, rr2 = st.columns(2)
    with rr1:
        if warning_70["triggered"]:
            render_big_card("提醒平仓线 70%", f"已触发（目标价 {warning_70['target_price']:.4f}）", "loss", '<span class="pill-red">已到提醒线</span>')
        else:
            render_big_card(
                "提醒平仓线 70%",
                f"下跌 {pct(warning_70['additional_drop_pct'])} / {money(warning_70['additional_drop_price'])}",
                "neutral",
                f'<span class="pill-gray">到价约 {warning_70["target_price"]:.4f}</span>',
            )

    with rr2:
        if liq_80["triggered"]:
            render_big_card("强平线 80%", f"已触发（目标价 {liq_80['target_price']:.4f}）", "loss", '<span class="pill-red">已到强平线</span>')
        else:
            render_big_card(
                "强平线 80%",
                f"下跌 {pct(liq_80['additional_drop_pct'])} / {money(liq_80['additional_drop_price'])}",
                "neutral",
                f'<span class="pill-gray">到价约 {liq_80["target_price"]:.4f}</span>',
            )

    details = pd.DataFrame(
        [
            ["股票代码", metrics.stock_code],
            ["股票名称", metrics.stock_name],
            ["买入价格", metrics.buy_price],
            ["买入时间", row["buy_time"]],
            ["买入股数", metrics.shares],
            ["总成本", metrics.total_cost],
            ["当前价格", metrics.current_price],
            ["股票市值", metrics.market_value],
            ["浮盈/浮亏", metrics.profit_loss],
            ["盈亏比例", metrics.profit_loss_pct],
            ["配资本金", metrics.financed_principal],
            ["杠杆", metrics.leverage],
            ["亏损占配资本金", metrics.loss_vs_financed_principal],
            ["日利息(%)", metrics.fee_daily_rate_pct],
            ["当日手续费", metrics.daily_fee_amount],
            ["累计手续费", metrics.accumulated_fee],
            ["备注", row["note"]],
        ],
        columns=["项目", "数值"],
    )
    st.dataframe(details, use_container_width=True, hide_index=True)


def main():
    init_db()
    bootstrap_session_state()

    if not check_login():
        return

    user = current_user()
    if not user:
        st.rerun()

    topbar(user)
    sidebar_common(user)

    if user["role"] == "admin":
        admin_panel(user)
    elif user["mode"] == "margin":
        margin_system(user)
    else:
        normal_system(user)


if __name__ == "__main__":
    main()
