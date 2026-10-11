"""Telegram notification layer for the trading bot.

All alerts are sent synchronously via the Telegram Bot API.
Every alert in paper trading mode is prefixed with [PAPER].
Failures are logged but never raise exceptions — alerts must not crash the bot.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

import requests

from config.config import config

logger = logging.getLogger("trading.alerts")

_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = config.TELEGRAM_TIMEOUT_SECONDS


def _prefix() -> str:
    return "[PAPER] " if config.PAPER_TRADING_MODE else ""


def _send(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat.

    Returns:
        True on success, False on any failure.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping alert: %s", message[:80])
        return False

    url = _BASE_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram alert failed: %s | message=%s", exc, message[:100])
        return False


# ── Individual alert functions ────────────────────────────────────────────────

def send_login_url(login_url: str, portfolio_value: float) -> None:
    msg = (
        f"{_prefix()}🔐 <b>Daily Login Required</b>\n"
        f"Portfolio: ₹{portfolio_value:,.0f}\n"
        f"<a href=\"{login_url}\">Click to Login (expires in 30 min)</a>\n"
        f"Time: {datetime.now().strftime('%H:%M IST')}"
    )
    _send(msg)


def send_no_token_warning(portfolio_value: float) -> None:
    msg = (
        f"{_prefix()}⚠️ <b>No Auth Token Received</b>\n"
        f"Portfolio: ₹{portfolio_value:,.0f}\n"
        f"No login by 8:45 AM — bot will not trade today unless token is received.\n"
        f"Trading will resume tomorrow automatically."
    )
    _send(msg)


def send_trade_entry(
    symbol: str,
    entry_price: float,
    stop_loss: float,
    shares: int,
    position_value: float,
    rs_score: float,
    regime: str,
    vcp_tightness: float,
    portfolio_value: float,
    strategy: str = "vcp",
) -> None:
    risk_pct = ((entry_price - stop_loss) / entry_price) * 100
    msg = (
        f"{_prefix()}🟢 <b>New Trade Entry</b>\n"
        f"<b>{symbol}</b> [{strategy}]\n"
        f"Entry: ₹{entry_price:.2f} | Stop: ₹{stop_loss:.2f} ({risk_pct:.1f}% risk)\n"
        f"Shares: {shares} | Value: ₹{position_value:,.0f}\n"
        f"RS Score: {rs_score:.1f} | VCP: {vcp_tightness:.0f}\n"
        f"Regime: {regime}\n"
        f"Portfolio: ₹{portfolio_value:,.0f}"
    )
    _send(msg)


def send_trade_exit(
    symbol: str,
    exit_price: float,
    entry_price: float,
    pnl_rupees: float,
    pnl_pct: float,
    holding_days: int,
    exit_reason: str,
    portfolio_value: float,
) -> None:
    emoji = "🔴" if pnl_rupees < 0 else "✅"
    sign = "+" if pnl_rupees >= 0 else ""
    msg = (
        f"{_prefix()}{emoji} <b>Trade Closed</b>\n"
        f"<b>{symbol}</b>\n"
        f"Entry: ₹{entry_price:.2f} → Exit: ₹{exit_price:.2f}\n"
        f"P&L: {sign}₹{pnl_rupees:,.0f} ({sign}{pnl_pct:.1f}%)\n"
        f"Held: {holding_days} days | Reason: {exit_reason}\n"
        f"Portfolio: ₹{portfolio_value:,.0f}"
    )
    _send(msg)


def send_stop_loss_hit(
    symbol: str,
    loss_rupees: float,
    portfolio_impact_pct: float,
    portfolio_value: float,
) -> None:
    msg = (
        f"{_prefix()}🔴 <b>Stop Loss Hit</b>\n"
        f"<b>{symbol}</b>\n"
        f"Loss: ₹{loss_rupees:,.0f} ({portfolio_impact_pct:.2f}% of portfolio)\n"
        f"Portfolio: ₹{portfolio_value:,.0f}"
    )
    _send(msg)


def send_trailing_stop_updated(
    symbol: str,
    old_stop: float,
    new_stop: float,
    current_pnl_pct: float,
) -> None:
    msg = (
        f"{_prefix()}📈 <b>Trailing Stop Updated</b>\n"
        f"<b>{symbol}</b>\n"
        f"Stop: ₹{old_stop:.2f} → ₹{new_stop:.2f}\n"
        f"Current P&L: +{current_pnl_pct:.1f}%"
    )
    _send(msg)


def send_partial_profit(
    symbol: str,
    sold_shares: int,
    sold_value: float,
    pnl_pct: float,
    remaining_shares: int,
) -> None:
    msg = (
        f"{_prefix()}💰 <b>Partial Profit Booked</b>\n"
        f"<b>{symbol}</b>\n"
        f"Sold {sold_shares} shares → ₹{sold_value:,.0f} (+{pnl_pct:.1f}%)\n"
        f"Remaining: {remaining_shares} shares still running"
    )
    _send(msg)


def send_regime_change(
    old_regime: str,
    new_regime: str,
    action: str,
    portfolio_value: float,
) -> None:
    msg = (
        f"{_prefix()}🌡️ <b>Market Regime Changed</b>\n"
        f"{old_regime} → <b>{new_regime}</b>\n"
        f"Action: {action}\n"
        f"Portfolio: ₹{portfolio_value:,.0f}"
    )
    _send(msg)


def send_daily_summary(
    portfolio_value: float,
    day_pnl: float,
    day_pnl_pct: float,
    open_positions: int,
    cash_pct: float,
    regime: str,
    trades_today: int,
) -> None:
    sign = "+" if day_pnl >= 0 else ""
    emoji = "📊"
    msg = (
        f"{_prefix()}{emoji} <b>Daily Summary</b>\n"
        f"Portfolio: ₹{portfolio_value:,.0f}\n"
        f"Day P&L: {sign}₹{day_pnl:,.0f} ({sign}{day_pnl_pct:.2f}%)\n"
        f"Positions: {open_positions} open | Deployable cash: {cash_pct:.0f}% (after 20% reserve)\n"
        f"Regime: {regime} | Trades today: {trades_today}"
    )
    _send(msg)


def send_loss_halt(
    halt_type: str,
    loss_pct: float,
    resume_date: Optional[date],
    portfolio_value: float,
) -> None:
    resume_str = str(resume_date) if resume_date else "Manual resume required"
    msg = (
        f"{_prefix()}🚫 <b>{halt_type} Loss Halt Triggered</b>\n"
        f"Loss: {loss_pct:.2f}% | Portfolio: ₹{portfolio_value:,.0f}\n"
        f"No new trades until: {resume_str}"
    )
    _send(msg)


def send_consecutive_loss_pause(
    loss_count: int,
    pause_days: int,
    resume_date: date,
) -> None:
    msg = (
        f"{_prefix()}⏸️ <b>Consecutive Loss Pause</b>\n"
        f"{loss_count} consecutive losses.\n"
        f"Pausing for {pause_days} day(s).\n"
        f"Next trade allowed: {resume_date}"
    )
    _send(msg)


def send_emergency_halt(
    drawdown_pct: float,
    portfolio_value: float,
    peak_value: float,
) -> None:
    msg = (
        f"{_prefix()}🚨 <b>EMERGENCY HALT — MANUAL RESTART REQUIRED</b>\n"
        f"Drawdown from peak: {drawdown_pct:.1f}%\n"
        f"Current: ₹{portfolio_value:,.0f} | Peak: ₹{peak_value:,.0f}\n"
        f"Delete data/emergency_halt.flag to resume after review."
    )
    _send(msg)


def send_data_anomaly(symbol: str, issue: str) -> None:
    msg = (
        f"{_prefix()}⚠️ <b>Data Anomaly</b>\n"
        f"Symbol: {symbol}\n"
        f"Issue: {issue}"
    )
    _send(msg)


def send_system_error(module: str, error: str, traceback_summary: str = "") -> None:
    msg = (
        f"{_prefix()}🔥 <b>System Error</b>\n"
        f"Module: {module}\n"
        f"Error: {error}"
    )
    if traceback_summary:
        msg += f"\n<code>{traceback_summary[:300]}</code>"
    _send(msg)


def send_gtt_sync_status(active_count: int, expected_count: int) -> None:
    status = "✅" if active_count == expected_count else "⚠️"
    msg = (
        f"{_prefix()}{status} <b>GTT Order Sync</b>\n"
        f"Active GTT orders: {active_count} / {expected_count} expected"
    )
    _send(msg)


def send_heartbeat_dead(last_heartbeat_str: str) -> None:
    msg = (
        f"{_prefix()}💔 <b>Dead Man's Switch Triggered</b>\n"
        f"No heartbeat since: {last_heartbeat_str}\n"
        f"Bot may have crashed. Check VPS immediately."
    )
    _send(msg)


def send_inactivity_warning(days_since_trade: int, cash_in_zerodha: float) -> None:
    msg = (
        f"{_prefix()}⚠️ <b>Account Inactivity Warning</b>\n"
        f"No trades in <b>{days_since_trade} days</b>.\n"
        f"Zerodha may transfer unused funds to your bank at 30 days.\n"
        f"Cash at risk of sweep: ₹{cash_in_zerodha:,.0f}\n\n"
        f"Options:\n"
        f"• Do nothing — re-deposit after Zerodha transfers\n"
        f"• Place any small trade to reset the 30-day clock\n"
        f"Bot will continue running normally either way."
    )
    _send(msg)


def send_text(message: str) -> None:
    """Send a raw text message (for testing or custom notifications)."""
    _send(f"{_prefix()}{message}")
