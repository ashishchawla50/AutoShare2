# The Whole Strategy — AutoShare Explained From Zero

> **Living document.** Plain-language explanation of everything the bot does,
> written for a reader with zero share-market knowledge. Update this file
> whenever the strategy, filters, sizing, or live status change (see the
> changelog at the bottom). Last updated: **2026-10-19**.

---

## 1. The one-line idea

The bot copies what a professional human trader does every morning: **look at
India's 500 largest companies, throw out the risky ones, find stocks that are
"resting" after a strong run, wait for them to wake up with force, buy a small
amount, and always keep an automatic emergency exit ready.**

**The thumb rule every change is judged by:** *less percentage profit in fewer
days is much better than more percentage profit in more days.* So strategies are
ranked by **speed of profit**: net profit per day the money is in a trade (shown as
"% per 30 days invested"), alongside yearly account growth. On 2026-10-11, ten
faster alternatives (shorter time stops, maximum holds of 5–10 days, take-profits
at +3/+5/+10%, and the 4-day mean-reversion strategy) were tested on this rule.
**None beat the current exits.** Selling sooner cut each trade's profit by more
than it saved in days. With today's settings (1.5× volume, −10% first stop) the
strategy makes about +2.0–2.8% per 30 days invested, holding ~18–20 days per trade
(medians over many start dates, 2021–24 and 2025–26).

---

## 2. The dictionary — every technical word, explained once

**Share / Stock** — a tiny piece of ownership in a company. If XYZ company is
divided into 1 crore pieces and you buy 10 pieces, you own 10 shares.

**Price** — what one share costs right now. It changes every second while the
market is open (9:15 AM – 3:30 PM).

**Volume** — how many shares of a company got bought/sold in a day. High
volume = lots of people interested. Think of it like footfall in a shop. If
XYZ normally trades 1 lakh shares a day and today 3 lakh traded, something big
is happening.

**Market cap** — the total value of the whole company. Price × total number of
shares. XYZ at ₹100 with 10 crore shares = ₹1,000 crore market cap. Big market
cap = big, stable company.

**Moving average (the "EMA" word)** — the average price of the last N days,
drawn as a smooth line. Daily prices jump around and confuse you; the average
shows the real direction.
- Example: if XYZ's last 5 closing prices were 98, 99, 100, 101, 102, the
  5-day average is 100.
- **EMA** (Exponential Moving Average) is the same thing but gives more
  importance to recent days — yesterday matters more than 50 days ago.
- **EMA200** = average of roughly the last 200 trading days (about 10 months).
  It shows the *long-term* direction. Price above EMA200 = the stock has been
  healthy for months. Below = long-term sickness.

**52-week high / low** — the highest and lowest price in the last one year. A
stock near its 52-week high is a winner; near its low is a loser. This bot
only buys winners (sounds backwards, but strong stocks tend to stay strong —
the most tested finding in trading research).

**RSI** — a 0-to-100 "speedometer" of how fast a stock has been rising or
falling recently. Above ~70 = rising very fast (maybe overheated). Below ~30 =
falling fast. Around 50 = calm.

**VIX (India VIX)** — the market's "fear meter", published by NSE. Low
(10–15) = calm market. High (25+) = people are scared, prices swing wildly.

**Nifty** — the average of India's 50 biggest companies. When people say
"market is up", they usually mean Nifty is up. The bot uses Nifty's health to
judge the whole market's mood.

**Pivot** — the "ceiling" price a stock has bumped against several times but
not crossed. If XYZ touched ₹100 three times over two months and dropped back
each time, ₹100 is the pivot. Crossing a ceiling that held for months is a big
event.

**Breakout** — the moment the price finally smashes through that ceiling. The
bot only buys breakouts.

**Stop-loss** — a pre-decided "I was wrong, get me out" price. Buy at ₹100
with stop-loss ₹90: if the price falls to ₹90, you sell automatically and take
a 10% loss instead of hoping and riding it down 40%. (The bot's first stop is
10% below the buy price since 2026-10-11; it was 7% before.)

**GTT order** — "Good Till Triggered." A stop-loss instruction stored **on
Zerodha's servers**, not in the bot. Even if the bot's server catches fire,
Zerodha itself will sell your share at the stop price. The bot's seatbelt.

**Drawdown** — how far your account has fallen from its highest point. Started
with ₹50,000, grew to ₹55,000, fell to ₹49,500 → drawdown is 10% (from 55k).
Big drawdowns are what destroy traders.

**Backtest** — a "time machine" test: run the strategy's rules on old price
data (2021–2026) and see what *would have* happened. Cheap way to find out if
an idea is garbage before betting real money.

**Profit Factor (PF)** — total money won ÷ total money lost across all trades.
PF 1.61 means: for every ₹100 lost on bad trades, ₹161 was earned on good
ones. Above 1 = profitable system. Below 1 = losing system.

**Trading equity** — the bot's OWN money: its ₹50,000 starting capital plus
whatever its own trades have won or lost. The Zerodha account also holds the
owner's personal investments — those are walled off and never counted (see
the 2026-07-31 incident in the changelog).

---

## 3. How 500 shares become 1 trade — the funnel

The bot scans the **Nifty 500** — India's 500 largest listed companies — the
same universe the strategy was validated on. The list is refreshed from NSE
every morning at 5:30 AM.

Every morning the funnel runs (real numbers from 2026-08-11):

```
 500 stocks (Nifty 500)
   ↓  9 safety filters          → 442 survive
   ↓  8 trend checks            → 151 survive   (291 failed today)
   ↓  VCP pattern check         → 23 qualify    (128 failed today)
   ↓  goes on WATCHLIST         → currently 23 stocks being watched
   ↓  waits for confirmed breakout (up to 30 days)
   ↓  7 final risk approvals
   ↓  BUY (only between 9:30–10:30 AM)
```

Note the last two stages are where the patience lives: 23 stocks are sitting
in a valid resting pattern right now, and on 2026-08-11 **none** of them broke
out on the required volume (2× then; 1.5× since 2026-10-11) — so the bot bought nothing. That is the normal
state, not a fault.

---

## 4. Stage 1 — The 9 safety filters (file: `scanner/stock_filter.py`)

These remove stocks that are dangerous *regardless* of their chart:

1. **filter_price** — price must be above ₹100. Very cheap stocks are often
   manipulated.
2. **filter_market_cap** — company must be worth at least ₹5,000 crore. Big
   companies can't be easily rigged.
3. **filter_liquidity** — at least **₹10 crore worth of the stock must change
   hands daily**, so you can sell whenever *you* want. (Until 2026-07-29 there
   was also a "5 lakh shares/day" rule — removed because it double-counted
   liquidity and wrongly rejected expensive-but-liquid stocks: a ₹3,000 stock
   trading 3 lakh shares moves ₹90 crore a day.)
4. **filter_surveillance** — NSE keeps naughty-lists (ASM/ESM) of suspicious
   stocks. Anything on those lists is out.
5. **filter_corporate_events** — no buying within 7 days of quarterly results
   or 3 days of a dividend/bonus/split date. Results day is a coin flip —
   prices can gap 10% either way overnight. *(Validated 2026-08-08: adding
   this rule to the historical test improved profit factor 1.75 → 2.22, so it
   stays. **But live's version is half-broken** — the daily data feed only
   fetches dividend/bonus/split dates, never results dates, so the "7 days
   around results" half has never actually fired. Fixing that needs the NSE
   board-meetings feed added to the daily job; pending sign-off because it
   makes the bot stricter.)*
6. **filter_circuit_breaker** — stocks that hit their daily maximum-move limit
   more than 3 times in 60 days are too wild. Out.
7. **filter_fundamentals** — company health check: debt below 2× equity (not
   drowning in loans) and promoters must not have pledged more than 50% of
   their shares as loan collateral. *(An extra "ROE ≥ 15%" profitability rule
   existed until 2026-07-29 — it was disabled because it was never part of the
   validated ruleset and was silently removing ~160 of the 500 stocks from the
   pond the strategy was proven on.)*
8. **filter_ipo_age** — the stock must be listed at least 1 year. Newly listed
   stocks have no history to analyze.
9. **filter_data_quality** — if our price data has holes (more than 5 missing
   days in 90), we can't trust the analysis. Out.

Today (2026-08-11): **442 of 500 passed.** The ~58 removed are mostly stocks
under ₹100, a handful near results dates, and a few genuinely illiquid ones.
The exact split moves daily with earnings season.

---

## 5. Stage 2 — The 8 trend checks (file: `strategy/trend_template.py`)

The famous "Trend Template" by **Mark Minervini** (US trading champion — the
whole strategy is his method). One question: *is this stock in a genuine
long-term uptrend?* **All 8 must pass — 7/8 is a fail.**

Using XYZ at ₹100:

| # | Check | Plain meaning |
|---|---|---|
| 1 | Price > EMA50 | above the ~2.5-month average → recently strong |
| 2 | Price > EMA150 | above the ~7-month average → medium-term strong |
| 3 | Price > EMA200 | above the ~10-month average → long-term strong |
| 4 | EMA50 > EMA150 | recent average above older → speeding up, not slowing |
| 5 | EMA150 > EMA200 | averages stacked like a staircase going up |
| 6 | EMA200 rising ~1 month | the long-term line itself points upward |
| 7 | Price ≥ 25% above 52-week low | yearly low ₹70, price ₹100 = 43% above ✓ — clearly left its worst days |
| 8 | Price within 25% of 52-week high | yearly high ₹110, ₹100 is 9% below ✓ — near its best, not recovering from a crash |

Today (2026-08-11) **291 of 442 failed** this, leaving 151. Normal — most
stocks are not in a clean long-term uptrend at any given moment.

---

## 6. Stage 3 — The VCP pattern (file: `strategy/vcp_detector.py`)

**VCP = Volatility Contraction Pattern.** Simple idea: **after a strong run, a
good stock "rests" — and each wave of selling gets smaller.**

XYZ's last 2 months: ran to ₹100, dropped to ₹88 (12% dip), bounced to ₹99,
dropped to ₹94 (5% dip), bounced to ₹100, dropped to ₹97 (3% dip). The swings
shrink: 12% → 5% → 3%. Sellers are running out. Buyers quietly hold. The
spring is compressed.

The bot scores this "tightness" 0–100 and demands above 50. The ceiling of the
resting zone — ₹100 — is the **pivot**.

A stock passing all this is **NOT bought**. It goes on the **watchlist**
(`data/.pivot_watchlist.json`) for up to **30 days** with its pivot recorded.

**Currently watched (23 as of 2026-08-11 — a new record):** BAJAJ-AUTO,
BIOCON, BOSCHLTD, CCL, CHOLAFIN, EICHERMOT, EMCURE, GLENMARK, HAL, HINDALCO,
INDIANB, LALPATHLAB, MARICO, NETWEB, NYKAA, OLECTRA, POLYCAB, PTCIL,
SONACOMS, TITAN, TRITURBINE, USHAMART, VIJAYA.

The list has grown from 15 to 23 as the market strengthened — more stocks are
forming valid resting patterns. That is the pipeline filling up, which is what
should happen before breakouts start firing.

---

## 7. Stage 4 — The buy trigger: when XYZ at ₹100 actually gets bought

XYZ is on the watchlist with pivot ₹100. Every day the bot checks yesterday's
completed candle against **6 conditions** (file:
`strategy/breakout_detector.py`). ALL must pass on the same day.

Say XYZ opens at ₹100.50, high ₹104, low ₹100, **closes ₹103.60** on 2.3×
normal volume:

1. **price_above_pivot** — ₹103.60 > ₹100 ✓ *(broke the ceiling)*
2. **strong_close** — close in the top 4% of the day's range ✓ *(finished
   strong, didn't break out and collapse back)*
3. **bullish_body** — close above open ✓ *(a genuine green day)*
4. **volume_surge** — 2.3× ≥ **1.5×** ✓ *(big institutions are buying, not
   just small traders. THE critical check. Lowered from 2.0× to 1.5× on
   2026-10-11 after a pre-registered test: more trades, and better profit factor
   and return in both test periods. Under 2.0× it rejected every pivot crossing
   in the pilot's first weeks: TITAN, EICHERMOT and others crossed their
   ceilings on sleepy 0.4–1.5× volume)*
5. **healthy_range** — the day moved more than 0.5% ✓ *(not a dead day)*
6. **not_extended** — ₹103.60 is only 3.6% past the pivot, limit 5% ✓ *(not
   chasing a bus that already left)*

All six green → **next morning, 9:30–10:30 AM only**, the bot places the buy.
One final morning check: if XYZ *opens* gapped up more than 5%, the trade is
skipped — too expensive now; there will be other buses.

How close can it get? On 2026-07-31, SONACOMS closed above its pivot but the
candle was red by **55 paise** (close ₹769.40 vs open ₹769.95) and volume was
1.2×. Two conditions failed → no trade. Brutal, but enforcing that line *is*
the strategy.

### The 7 final approvals (`RiskManager.approve_entry`)

1. **Regime not BEAR/EXTREME** (market weather — below)
2. **Not on the personal blocklist** (owner's own holdings are protected)
3. **Fewer than 8 positions open**
4. **Not already holding it**
5. **Sector cap 30%** — if 30% of the money is already in banks, no more banks
6. **Enough cash** (20% of bot capital always stays in reserve)
7. **No halt flags** — no emergency-halt file, daily loss under 3%, not paused
   after 3 consecutive losses

If a valid signal is blocked here (say, not enough cash), the pivot goes
**back on the watchlist** instead of being thrown away — so the setup can
still fire later. (Added 2026-08-01 after the SONACOMS incident.)

### How much does it buy? (position sizing)

Rule: **risk only 2% of the BOT's capital per trade** — and "capital" means
the bot's own ₹50,000 + its own P&L, never the whole Zerodha account. (Until
2026-08-01 a bug counted the owner's personal holdings too, making the bot
think it managed ~₹5 lakh — its one real signal was sized 10× too big and
blocked. Fixed and verified.)

- Bot capital ₹50,000 → max acceptable loss per trade = **₹1,000**
- Buy at ₹103.60, first stop-loss 10% lower = ₹93.25 → risking ₹10.35/share
- ₹1,000 ÷ ₹10.35 ≈ 96 shares, and one position ≤ 20% of capital = ₹10,000 →
  96 shares. The two limits meet exactly at a 10% stop (2% ÷ 10% = 20%), so a
  stopped-out trade loses about ₹1,000 (2% of capital).
- Regime CAUTIOUS → everything halved again → ~48 shares (~₹5,000), risk ~₹460.
  *(Regime is BULL as of 2026-08-11, so sizing is currently full — the ~96
  share / ₹10,000 line above is what would actually be bought today.)*

The moment the buy fills, a **GTT stop-loss** goes onto Zerodha's servers.
Every trade has a small, pre-known, survivable worst case.

### After buying — the exits (all automatic)

- **Stop-loss:** price hits the stop (first stop 10% below the buy price) →
  sold, loss of about 2% of capital, move on. The 10-day-average rule below
  usually lifts the stop within a day or two, so few trades lose the full 10%.
- **Trailing stop:** as price climbs, the stop ratchets up behind it — UP
  only, never back down. At ~+10% profit the stop reaches your buy price (the
  trade can no longer lose).
- **Partial profit booking: OFF since 2026-08-10.** The bot used to sell a
  third at +20% and another third at +30%. The validated test never did this —
  it rode every winner to the trailing stop — so live was running an exit the
  ₹50k result (PF 1.75) never measured. Positions now ride whole, exactly as
  tested. The code is still there behind a switch
  (`config.ENABLE_PARTIAL_PROFIT`) if it is ever validated properly.
- **Time stop:** going nowhere for weeks → exit, free the money.

---

## 8. The "market weather" system (file: `regime/regime_detector.py`)

Checked before everything, using Nifty vs its EMA200, market **breadth** (what
% of stocks are above their own EMA200), and the VIX fear meter:

| Regime | Meaning | Bot behavior |
|---|---|---|
| **BULL** | Nifty healthy, most stocks rising | Full size ← *we are here* |
| **CAUTIOUS** | Nifty near/below its long-term average | **Half** size |
| **BEAR** | Market clearly falling | No new buys |
| **HIGH_VOLATILITY** | VIX 25+ | Reduced size |
| **EXTREME_VOLATILITY** | VIX 35+, panic | No buys, start exiting |

The bot turns **pessimistic instantly, optimistic slowly** (upgrades need 2
consecutive days of confirmation). Fear fast, greed slow.

*Status 2026-08-11: the upgrade confirmed — regime is now **BULL**. Nifty
24,584 vs its 200-day average of 24,376 (+0.9%), breadth 65.7%, VIX 12.2 (very
calm). Positions are sized at full, not half. This is the tape strengthening
that the 2026-08-03 note anticipated; breakouts historically cluster after this
kind of turn, though "historically" is not a promise about any given week.*

---

## 9. The strategies — 4 built, 3 fired, 1 kept

| Strategy | Idea in one line | Test result | Verdict |
|---|---|---|---|
| Momentum | Buy when short average crosses above long average | PF 0.55–0.69 — lost money | ❌ Benched |
| Breakout (20-day) | Buy anything making a new 20-day high with volume | PF 0.69 — lost money; **re-tested 2026-08-10 with better exits, still 0.68–0.70** | ❌ Benched permanently |
| Mean Reversion | Buy sharp dips in uptrends, sell the bounce | PF 0.61 — lost money | ❌ Benched |
| **VCP v2 (Minervini)** | Everything described above | **PF 1.61 on unseen data; 1.75 when re-tested at ₹50k** | ✅ **The only one running** |

**Was it the exits that killed Breakout?** Worth asking, because Breakout used
a rigid "sell at +10%" rule that caps winners. On 2026-08-10 it was re-tested
with VCP's patient exits instead (no fixed target, trailing stop, longer time
stop) — the *rules for buying* left completely untouched, so only the selling
changed. The answer was no. Average win did roughly double and the best single
trade went from +10.9% to +97.8%, but profit factor only moved 0.65 → 0.70 on
the practice paper and 0.51 → 0.68 on the sealed one — nowhere near the 1.50
needed. Reason: **82% of Breakout's trades hit the stop-loss.** When four of
five buys are simply wrong, no selling rule can save it. The fault is in what
it buys. Question closed — see `breakout_validation_plan.md`.

### How the validation worked (this part matters most)

Two exam papers:

1. **Dev window (2021–2024):** build and tune here — like practicing with a
   question bank. Score: PF 1.11 across 224 simulated trades.
2. **Validation window (2025–2026):** data the strategy was NEVER tuned on — a
   sealed exam paper opened once. Score: **PF 1.61 across 43 trades**.

Why two papers? Any strategy can be twisted until it looks great on past data
(memorizing last year's answer key). Only unseen data proves a real edge.
Momentum scored 0.99 on the practice paper and **0.55 on the sealed paper** —
its "edge" was memorization. Fired.

Extra rigor:
- **Honest costs:** every simulated trade paid real brokerage, taxes, and
  slippage (~0.5% per round trip). This is what killed the three losers.
- **Regime replay:** the time machine simulated the weather system too (no
  buys in BEAR periods).
- **Bootstrap test:** the 43 validation trades were statistically reshuffled
  thousands of times — 85.6% confident the profit is a real edge, not luck.
- **Sizing study:** 1.5% vs 2.0% vs 2.5% risk per trade. 2.0% doubled returns
  for barely more drawdown; 2.5% breached the 20% emergency-halt line in a
  2023 crash simulation → disqualified. Hence 2%/20%.

---

## 10. Live pilot scoreboard (updated 2026-08-11)

> **2026-10-19 status: running normally again.** Zerodha login has worked every trading day
> since 2026-10-12, so data refreshes and the regime is read every 30 minutes. (The bot had been
> offline from 2026-08-31 to 2026-10-12 because Zerodha rejected its Kite Connect app.) The
> regime has been **BEAR** every day since: Nifty is about 6.6% below its 200-day average and only
> about 39% of stocks are in uptrends, so no new entries. **Watchlist: 7 stocks** (AARTIIND, ACE,
> AIIL, BALRAMCHIN, COFORGE, LAURUSLABS, SOLARINDS), each watched for 30 days. Still 0 real trades,
> 0 open positions. Live settings: 1.5× breakout volume, −10% first stop, 2% risk / 20% cap.
> The bullets below are from 2026-08-11.

- **LIVE since 2026-07-20** with ₹50,000. Success metric for this phase:
  *does the machine execute faithfully* — not P&L.
- **Trades: 0. P&L: ₹0. Drawdown: 0.** (The database holds 19 closed trades,
  but all 19 are *paper* trades from 10–16 July, before go-live, made by the
  now-benched strategies. Real-money trades: zero.)
- **Watchlist: 23 stocks armed. Regime: BULL. Slots free: 8 of 8.** The bot is
  fully loaded and waiting for a trigger, not stuck or broken.
- **One real signal so far:** SONACOMS, 2026-07-31 — a valid breakout that
  reached the final gate and was blocked by the sizing bug (now fixed). Its
  pivot is restored and watched until Aug 31. Every stage of the pipeline has
  now run on live money except the final order.
- **Expected pace: about 1.7 trades/month.** The sealed-paper test at ₹50,000
  took **31 trades over 18 months**. (An earlier run at larger capital took 43
  trades — ~2.4/month — because more cash means fewer entries get skipped for
  lack of funds. At pilot size, 1.7 is the honest number.) This is a long-run
  average containing long droughts, not a monthly quota. Being 3 weeks into
  live with zero trades is well inside normal.
- **How long do winners take?** From the validated ₹50k trades: winners are
  held a **median of 22 days** (longest 69), losers are cut in ~11 days. The
  median winner exits at only **+4.6%**; just 1 trade in 31 ever exceeded +20%.
  The money comes from rare large winners, which is exactly why the bot no
  longer trims them (see exits).
- **Four real defects found and fixed by the pilot so far** (exactly its
  purpose): nightly report crash, backup purge crash, over-narrow scan pond,
  sizing-off-account-value bug.
- **Date to watch:** ~Aug 19 — Zerodha sweeps idle funds after 30 days without
  a trade (harmless; bot warns via Telegram first).

---

## Changelog

- **2026-10-19** — **Bot back online.** Zerodha login has succeeded every trading day since
  2026-10-12 with the existing app key, so data and regime readings are flowing again. The
  market regime has been BEAR all week, so the bot correctly made no trades. 7 stocks are on the
  watchlist. No code or rule changes.
- **2026-10-12** — **Live bot switched to the AutoShare2 code.** The old program was stopped
  and archived. The new one runs on the same server with the same capital (₹50,000), the same
  blocklist and the same records. It brings the 1.5× breakout volume, the −10% first stop and
  the three live-safety fixes to real money for the first time. **Found during the switch:** the
  bot had not been able to log in to Zerodha since 31 August, because Zerodha stopped accepting
  its API app. It was never in a trade during that time (0 positions), so nothing was at risk,
  but it also could not trade. It restarts once the Zerodha app is renewed.

- **2026-10-11** — **First stop widened from −7% to −10%, keeping 1.5× breakout volume
  (owner decision).** All four combinations were tested (₹50k, full 2021-09 → 2026-08, real
  costs):

  | | 1.5× / −7% | **1.5× / −10% (chosen)** | 2.0× / −7% (old live) | 2.0× / −10% |
  |---|---|---|---|---|
  | Trades | 224 | **218** | 177 | 175 |
  | PF / return | 1.04 / +4.3% | **1.26 / +26.5%** | 1.20 / +18.0% | 1.30 / +26.7% |
  | Max drawdown | −15.7% | **−13.3%** | −19.1% | −16.3% |
  | Speed per 30 days invested (2021–24 / 2025–26) | +1.75 / +1.99% | **+2.78 / +1.96%** | +1.70 / +1.77% | +2.12 / +1.74% |
  | Days with no position | 42% | **41%** | 49% | 48% |

  −10% was better than −7% at both volume levels and tied in 2025–26. Strictly it fails the
  pre-registered "better in both periods" rule (test A1), so this is a deliberate override, made
  on consistent evidence. Position size is unchanged (₹10,000 at ₹50k). A stopped trade now
  loses about ₹1,000 instead of about ₹700. The edge is still fragile: bootstrap 79.9%, just
  under the 80% bar.
- **2026-10-11** — **Live-safety fixes from the Phase 1 audit (no strategy rule changed).**
  (1) The exit check crashed the first time a live trailing stop needed raising, because two
  share counts were only set when partial profits were on; the stop would have stayed at −7%
  and later positions went unchecked. Fixed, and one position's error no longer stops the others.
  (2) When raising a stop, the new GTT is now placed before the old one is cancelled, so a failed
  placement never leaves a position without a stop. (3) Live "available cash" is now capped at
  the bot's own ₹50k-based cash minus the 20% reserve; before, the owner's personal cash in the
  same Zerodha account counted, so live could hold more and larger positions than the backtest.
  *Deployed to the live bot on 2026-10-12. None of the bugs had fired, because the pilot had made zero trades.*
- **2026-10-11** — **Thumb rule adopted as the ranking rule:** less % profit in fewer
  days beats more % in more days, measured as profit per day invested. A pre-registered
  test of 11 faster alternatives (docs/TEST_PLAN_2026_10C.md) found **the current exits
  already fastest**: +1.75% (2021–24) and +1.99% (2025–26) per 30 days invested, median
  over rolling start dates. Take-profits at +3/+5/+10%, 5- and 10-day maximum holds, and
  the fast strategies (mean reversion at 4 days per trade: −4.7% per 30 days) were all
  slower or lost money. No rule changed. Every backtest now prints its speed of profit.
- **2026-10-11** — **Breakout volume requirement lowered from 2.0× to 1.5×**, in the
  AutoShare2 research code. It was the only change to pass a pre-registered batch of
  14 tests (docs/TEST_RESULTS_2026_10.md). On ₹50k it went from 130 → 161 trades with
  PF 1.29 → 1.32 in 2021–24, and 39 → 50 trades with PF 1.78 → 1.95 in 2025–26. The same
  batch found the edge **fragile**: only 2 of 5 years profitable, and a 28% chance of a
  20% drawdown at ₹50k. Partial profits, the RS gate and a −10% first stop all failed
  and stay as they are. **Caveat found afterwards:** one continuous 2021–2026 run is
  *worse* at 1.5× (PF 1.04 vs 1.20). Medians over 15 start dates are slightly better
  (speed of profit +1.99% vs +1.77% per 30 days in 2025–26), so it stays, but the
  margin is thin. Live since 2026-10-12.
- **2026-08-11** — Document refreshed against the live bot: funnel numbers
  (442/500 pond, 151 past trend checks, 23 watched), regime now BULL, watchlist
  list rewritten, scoreboard re-checked directly against the database.
- **2026-08-10** — **Live now matches the validated test** in two places where
  it had quietly drifted, and the bot was restarted the same evening to load
  the changes. (1) A "relative strength must be in the top 30%" filter was
  dropping confirmed breakouts in live; the validated test has no such filter
  anywhere, so it is gone — strength still counts for 35% of a setup's score,
  it just no longer disqualifies. (2) Partial profit booking off (see exits) —
  measured on the real validated trades, only 1 trade in 31 ever reached +20%,
  and the one dev trade that reached +110% would have been cut roughly in half
  by the old rule. Separately, the benched Range Breakout strategy was
  re-tested to see whether its fixed +10% target was what killed it: four
  pre-registered runs said no (see §9). It stays benched permanently.
- **2026-08-08** — **Re-validation at pilot scale passed.** Tested at ₹50k on
  the untouched 2025–26 window: profit factor **1.75**, confidence **84.7%**,
  worst drawdown −4.4% — against **1.77** at ₹5L. In plain terms, trading
  ₹6,000 positions instead of ₹64,000 ones costs almost nothing. ₹1L scores
  better still (1.84 / 89.3%) and is now the reference for the next capital
  step. Two flaws in the *testing* code were found and fixed first: the 20%
  emergency-stop, once triggered, could never lift (it blocks buying, so the
  account can't recover, so the stop stays on) which had silently killed 3 of
  the 3.5 test years; and the test charged ₹40 per round trip in brokerage
  that Zerodha does not actually charge on delivery trades — which mattered
  hugely on small positions and almost nothing on large ones. Caveat: on the
  older 2021–24 window the ₹50k drawdown reaches −19.8%, a whisker from the
  20% halt. See `VALIDATION_2026_08.md`.
- **2026-08-07** — Small-position rules unified. The historical test refused
  any position under ₹30,000 while the live bot had no minimum at all — so
  at ₹50k capital (max position ₹10k) the validated test could never trade,
  and live would take positions the validated method refused. Real Zerodha
  delivery cost math (see `DUST_FLOOR_DECISION.md`) shows costs only exceed
  0.5% of a position below ~₹5,700, so both engines now share one ₹6,000
  floor. The corporate-events rule (filter 5) was also added to the
  historical test (it was live-only); it **improved** results when tested
  (profit factor 1.75 → 2.22) so it stays in both — but note live's version
  of that rule is currently only half-working (see filter 5 above).
- **2026-08-06** — Live-order safety hardening (no strategy change): the bot
  now confirms every sell order actually went through before recording an
  exit; a buy whose stop-loss (GTT) cannot be placed is immediately sold back
  rather than held unprotected; the ledger records the broker's real fill
  prices (so slippage is now measurable, which the pilot exists to measure);
  the morning reconcile also detects shares left in the account by a failed
  exit; and the backup stop monitor sells the correct remaining quantity and
  retries failed sells instead of silently giving up.
- **2026-08-03** — Created this document. Numbers as of that day: pond 449/500,
  watchlist 15, regime CAUTIOUS with possible BULL upgrade pending.
- **2026-08-01** — Sizing now uses bot trading equity only (personal holdings
  excluded); risk-blocked entries restore their pivot; SONACOMS pivot restored.
- **2026-07-29** — Universe truth corrected (live scans Nifty 500, since
  07-14); ROE gate disabled; share-count liquidity floor removed → scan pond
  141 → ~449. Entry rules untouched.
- **2026-07-20** — Live pilot began (₹50k).
- **2026-07-16** — vcp_v2 validated out-of-sample (PF 1.61); other three
  strategies rejected and benched.
