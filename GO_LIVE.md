# Go-Live Runbook — ₹50k Live Pilot

> First live run: Monday 2026-07-20. This is a **plumbing pilot**: success is
> measured by orders, GTTs, fills, ledger, and reconcile behaving — not P&L.
> Strategy: vcp_v2 (sole validated engine, OOS PF 1.61). Sizing: 2.0% risk /
> 20% cap (sizing study 2026-07-17; 2.5%/25% disqualified — breached the 20%
> drawdown halt in the dev window).
>
> **Update 2026-08-08 — the pilot is no longer only a plumbing exercise.**
> vcp_v2 was re-validated *at pilot capital* (`VALIDATION_2026_08.md`): ₹50k
> OOS PF **1.75**, bootstrap **84.7%**, MaxDD −4.35%, versus 1.77 at ₹5L.
> Trading ₹6k positions costs ~0.02 PF. A ₹6,000 minimum-position floor
> (`config.MIN_POSITION_VALUE`) now applies in live as well as backtest, so
> live can no longer take dust positions the validated method would refuse.
> P&L at ₹50k is still mostly noise, but the ruleset live runs is now covered
> by a validation at its own capital level.
>
> **Standing risk:** the ₹50k dev window reaches −19.78% drawdown, a whisker
> from the 20% emergency halt — and that halt is **absorbing** (it blocks new
> entries, so equity cannot recover, so it never lifts). If it ever fires,
> the bot will not resume on its own; treat it as a manual-intervention
> event, and prefer reaching ₹1L (OOS PF 1.84 / bootstrap 89.3%) promptly.

## Pre-flight checklist

- [x] ₹50,101 in the Zerodha account, verified via the bot's own Kite session; TOTP login rehearsed successfully *(2026-07-19 evening)*
- [x] Personal holdings blocklisted: JIOFIN, SILVERBEES, SILVERIETF, HDFCGOLD *(2026-07-17; full order-path audit passed 2026-07-19)*
- [x] TOTP auto-login confirmed ON by Ashish *(2026-07-19)*
- [x] `validate_setup.py` — ALL CHECKS PASSED on VPS *(2026-07-19; Python floor set to the proven 3.10 runtime)*
- [x] Latest `main` deployed AND bot restarted

## The flip — EXECUTED 2026-07-19 ~16:40 IST (log: `paper=False`, pid 310381)

```bash
ssh -i ssh-key-2026-06-28.key ubuntu@152.67.10.33
sed -i 's/^PAPER_TRADING_MODE=.*/PAPER_TRADING_MODE=false/' ~/AutoShare/.env
sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare
grep -m1 'paper=' ~/AutoShare/logs/trading.log | tail -1   # must say paper=False
```

Telegram messages lose the `[PAPER]` prefix — that is the visible confirmation.

## What happens on a live morning

| IST | Event |
|---|---|
| 08:00 | TOTP login (fallback: manual URL via Telegram) |
| 08:20 | Holdings reconcile (DB vs Kite; counts T+1 shares) |
| 08:30–09:15 | Data refresh → filters → **two-stage VCP scan** (pivot watch) |
| 09:30–10:30 | Entry window — only pivot-breakout candidates, real orders + real GTTs |
| all day | Exit checks every 5 min; GTT monitor every 60s |
| 15:45 | Daily summary |

## First-trade watch protocol

When the first entry alert arrives: verify in Kite (a) the buy order exists and
filled, (b) a GTT sell trigger exists at the alerted stop price for the full
quantity, (c) fill price ≈ alert price (note slippage). Then check
`logs/orders.log` matches. Any mismatch → emergency halt (below) and diagnose
before the next window.

## Emergency controls (no restart needed)

- **Stop new entries:** `touch ~/AutoShare/data/emergency_halt.flag` (delete to resume)
- **Exit one position cleanly:** `echo "SYMBOL" >> ~/AutoShare/data/manual_exit.txt`
  (acted on within 5 min: order + GTT cancel + ledger + alert)
- **Never** sell bot positions from the Kite app (rule #11); the 08:20
  reconcile is the backstop if it happens anyway
- **Full rollback to paper:** set `PAPER_TRADING_MODE=true` in `.env`, restart
  `autoshare`. Open live positions remain in the ledger (`is_paper=0`) with
  GTT stops standing in the account — exit them via `manual_exit.txt` before
  rolling back, or manage them manually in Kite afterwards.

## Expectations for the pilot

- ~2–4 entries/month; days or weeks with zero trades are **normal and correct**
- Position size ~₹4–5k (CAUTIOUS halves the 20% cap), risk ~₹300–350/trade
- P&L over the pilot is statistical noise; the promotion gate to ₹1L stays
  ≥3 months / ≥30 trades with live behavior matching the backtest logic
- Live fills will be slightly worse than paper (slippage) — expected
- Zerodha sweeps idle funds after ~30 days without a trade (first deadline
  ~2026-08-19). The bot warns via Telegram; harmless — re-deposit, or place
  any small manual trade outside the bot to reset the clock.

## Pilot log

- **2026-07-20 → 07-26:** zero entries (correct: no valid breakouts). EOD
  daily report crashed nightly (`today_ist().date()` AttributeError) — fixed
  `8167e8b`, deployed with the 07-27 restart.
- **2026-07-29:** "no trades" investigation found the scan pond filtered to
  ~141 of the Nifty 500 vs the ~500-stock pond the validation traded. ROE
  gate disabled + share-count liquidity floor removed (`5816651`) → pond
  ~432. Entry rules untouched. Nightly backup purge TypeError also fixed
  (`4a3ec5e`).
- **2026-07-31 — first live signal, lost to a sizing bug:** SONACOMS fired a
  valid vcp_v2 breakout (rank 1, VCP 80). Sizing used the whole Zerodha
  account value (~₹5L incl. personal blocklisted holdings) instead of the
  bot's ₹50k, computed a ₹49.9k position, and the cash gate blocked it; the
  consumed pivot was then dropped from the watch. **Fixed `a8e5d90`
  (2026-08-01):** `get_portfolio_value()` = bot trading equity only
  (mode-filtered P&L; account view moved to `get_account_value()`), and
  risk-blocked vcp entries restore their pivot to the watch. SONACOMS pivot
  (741.65) manually restored, watch until 2026-08-31.
- **2026-08-03:** pond 449, watchlist a record 15 pivots, still zero valid
  breakouts — CAUTIOUS regime, VIX ~12; 2x-volume breakouts cluster in
  strong tapes. Machinery verified end-to-end; waiting is the strategy.
