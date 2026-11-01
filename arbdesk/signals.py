"""Buy / sell signal alerts for strategies whose backtest is good enough to follow by hand.

When a qualifying (strategy, symbol) trades in the lab, an alert with category "signal" is
raised; the email notifier sends each one as its own email. The text explains the rule that
fired, the indicator values behind it, the backtest it rests on, what to do, and how long the
signal stays valid. Nothing here places orders.

A backtest qualifies when (all configurable in [strategies]):
    return > signal_min_return_pct (default 0) and beats buy-and-hold by > signal_min_excess_pct (0),
    max drawdown <= signal_max_drawdown_pct (25), at least signal_min_trades trades (2),
    and the strategy is in signal_strategies (slow ones only: daily / 6-hour rules can be
    followed by hand, intraday ones can't).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DAY_MS = 86_400_000
# How far price may run from the signal price before the signal counts as stale.
DRIFT_PCT = {"1d": 3.0, "6h": 2.0}
TZ_NAMES = {"America/New_York": "美东时间", "America/Chicago": "美中时间", "America/Denver": "美山地时间",
            "America/Los_Angeles": "美西时间", "Asia/Shanghai": "北京时间", "UTC": "UTC"}
TF_NAMES = {"1d": "日线", "6h": "6 小时 K 线", "1h": "1 小时 K 线"}


def qualifies(bt: dict, cfg) -> tuple[bool, str]:
    if not bt or bt.get("return_pct") is None:
        return False, "还没有回测结果"
    ret, hold = bt["return_pct"], bt.get("buy_hold_pct", 0.0)
    if ret <= cfg.signal_min_return_pct:
        return False, f"回测收益 {ret:+.1f}% 不够"
    if ret - hold <= cfg.signal_min_excess_pct:
        return False, f"回测 {ret:+.1f}% 没有跑赢持有 {hold:+.1f}%"
    if bt.get("max_drawdown_pct", 100) > cfg.signal_max_drawdown_pct:
        return False, f"回测回撤 {bt['max_drawdown_pct']:.1f}% 太大"
    if bt.get("trades", 0) < cfg.signal_min_trades:
        return False, f"回测只有 {bt.get('trades', 0)} 笔交易，样本太少"
    return True, f"回测 {ret:+.1f}% vs 持有 {hold:+.1f}%，回撤 {bt.get('max_drawdown_pct', 0):.1f}%"


def _local(ms: int, tz_name: str) -> str:
    utc = datetime.fromtimestamp(ms / 1000, timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        loc = utc.astimezone(ZoneInfo(tz_name))
        if tz_name == "UTC":
            return f"{utc:%Y-%m-%d %H:%M} UTC"
        return f"{loc:%Y-%m-%d %H:%M} {TZ_NAMES.get(tz_name, tz_name)}（UTC {utc:%m-%d %H:%M}）"
    except Exception:  # no tz database (Windows without the tzdata package)
        return f"{utc:%Y-%m-%d %H:%M} UTC"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


def build(*, label: str, symbol: str, side: str, kind: str, tf: str, why: list[str], signal_ms: int,
          signal_price: float, now_ms: int, price: float, bt: dict, qual: str, action: str,
          stop: float | None, tz_name: str, synced: bool = False) -> tuple[str, str]:
    """(title, detail) for a signal alert. kind: entry | exit | stop."""
    bar_ms = {"1d": DAY_MS, "6h": DAY_MS // 4}.get(tf, DAY_MS)
    age_bars = (now_ms - signal_ms) / bar_ms
    drift = (price / signal_price - 1) * 100 if signal_price else 0.0
    limit = DRIFT_PCT.get(tf, 2.0)
    word = {"entry": "买入信号", "exit": "卖出信号", "stop": "止损卖出"}[kind]
    title = f"【{word}】{label} {symbol} @ {_usd(price)}"

    lines = [f"信号：{word} {symbol}（策略：{label}，{TF_NAMES.get(tf, tf)}）", ""]
    lines += ["一、信号分析"] + [f"  · {w}" for w in why] + [""]
    lines += ["二、回测依据（同一套规则跑最近的历史行情，含手续费）",
              f"  · {qual}；共 {bt.get('trades', 0)} 笔交易，覆盖约 {round(((bt.get('to') or 0) - (bt.get('from') or 0)) / DAY_MS)} 天"]
    if bt.get("win_rate_pct") is not None:
        lines.append(f"  · 胜率 {bt['win_rate_pct']:.0f}%，平均持有 {bt.get('avg_hold_days')} 天")
    lines += ["  · 回测只代表过去，样本很少时偶然性很大。", ""]

    lines += ["三、建议操作", f"  · {action}"]
    if stop:
        lines.append(f"  · 止损价 {_usd(stop)}（距现价 {(stop / price - 1) * 100:+.1f}%）。建议直接在交易所挂好止损单。")
    lines.append("")

    lines += ["四、信号时效",
              f"  · 信号产生：{_local(signal_ms, tz_name)}，信号价 {_usd(signal_price)}",
              f"  · 邮件发出时价格 {_usd(price)}（较信号价 {drift:+.1f}%）"]
    if kind == "stop":
        lines.append("  · 止损信号：立即执行，不要等。价格可能已经继续下跌。")
    elif kind == "exit":
        lines.append("  · 卖出信号：尽快执行。在出现新的买入信号之前一直有效（策略认为此时不该持有）。")
    else:
        lines.append(f"  · 买入信号：到 {_local(signal_ms + bar_ms, tz_name)}（下一根 K 线收盘）前有效，"
                     f"并且价格不高于信号价 {limit:.0f}%（{_usd(signal_price * (1 + limit / 100))}）。")
        lines.append("    超过时间或价格已经跑远，就不要追了，等下一个信号或回面板看策略最新状态。")
    if synced or age_bars > 1.5:
        lines.append(f"  · 注意：这是程序启动时的状态同步，原始信号已出现约 {age_bars * bar_ms / DAY_MS:.0f} 天，"
                     "不是刚发生的信号，按上面的价格条件判断是否还值得执行。")
    elif kind == "entry" and drift > limit:
        lines.append(f"  · 注意：价格已经比信号价高出 {drift:.1f}%，超过 {limit:.0f}% 的追价上限，本次建议不追。")
    lines += ["", "这是模拟盘策略发出的提醒，程序不会自动下任何真实订单；是否交易由你决定。"]
    return title, "\n".join(lines)
