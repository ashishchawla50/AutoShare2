# UNIVERSE_RECONSTRUCTION_PLAN.md

**Date:** 2026-08-11
**Scope:** Priority #1 — what historical universe data actually exists, and what
can be reconstructed without look-ahead.
**Status:** Findings + proposed method. **Nothing implemented. Awaiting approval.**

---

## 1. What exists today — full inventory

### 1.1 `universe_snapshots` (fundamentals.db) — the fabrication, now proven

```
2019-01-01   500 symbols     <-- the only pre-2026 row set
2026-07-01   200
...          (daily, 200 until 2026-07-13, 500 from 2026-07-14)
2026-08-11   500
```

Overlap between the `2019-01-01` snapshot and today's:

```
2019-01-01 snapshot : 500 symbols
2026-08-11 snapshot : 500 symbols
INTERSECTION        : 499  (99.8%)
only in 2019 list   : ['JBCHEPHARM']
only in 2026 list   : ['PFOCUS']
```

The "2019" universe is the July-2026 Nifty 500 with one symbol swapped. Genuine
snapshots begin **2026-07-01** — after the pilot went live. There is no real
historical membership data, and (per `CURRENT_SYSTEM_AUDIT.md` BLOCKER-1) no code
path capable of producing any.

### 1.2 `market_data.db` — survivor-only prices

502 symbols · 767,124 rows · 2019-06-03 → 2026-08-10.

```
2019  376 symbols     2023  447
2020  385             2024  472
2021  415             2025  502
2022  430             2026  502
```

The rising symbol count is **listing dates**, not universe evolution — every one
of the 502 is a *current* Nifty 500 member. **Zero delisted, merged or demoted
companies are present.**

### 1.3 Measured size of the survivorship hole

Taking NSE's actual bhavcopy for 2022-10-27 and ranking all EQ-series equities by
traded value:

```
top-500-by-turnover on 2022-10-27 : 500
we HAVE price data for            : 320
we have NO data for               : 180   <-- 36% hole
```

Missing names include **HDFC** — the 8th most-traded stock in India that day
(₹9.19bn turnover), merged into HDFCBANK in July 2023 — plus ADANITRANS,
AMARAJABAT, CENTURYTEX, ASTRAZEN, ALKYLAMINE, CSBBANK and 174 others.

This is the survivorship bias measured directly rather than argued: **the backtest
was selecting from 320 of the 500 real candidates, and the 180 absent are
systematically those that merged, delisted, were renamed, or fell out of favour.**

### 1.4 `corporate_events_full.db` — genuinely useful

69,648 rows, 2021-01 → 2026-08: results 59,059 · dividend 10,013 · **split 295** ·
**bonus 281**, with ex-dates and parseable ratio text. Real, usable, PIT-safe.
No demerger event type (see `DATA_INTEGRITY_REPORT.md` §4.2).

### 1.5 Filesystem sweep

No archived index files, no constituent CSVs, no historical universe artifacts.
`data/historical/` is empty. Backups hold 30 days of `corporate_events` only —
no universe history. Disk: 38 GB free.

---

## 2. The better source — found, tested, working

**NSE's daily bhavcopy archive is fully reachable from the VPS.** Verified live
in both formats:

| Format | Period | URL pattern | Test |
|---|---|---|---|
| Legacy | ≤ 2024-07-01 | `.../historical/EQUITIES/2022/OCT/cm27OCT2022bhav.csv.zip` | 2,298 rows ✓ |
| UDiFF | 2024-01-02 → today | `.../cm/BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip` | 3,473 rows ✓ |

The legacy format stops working between 2024-07-01 and 2024-07-31; UDiFF already
works back to at least 2024-01-02, so the two overlap and coverage is continuous.

Each file contains, for **every security that traded that day**:

`SYMBOL · SERIES · OPEN · HIGH · LOW · CLOSE · PREVCLOSE · TOTTRDQTY ·
TOTTRDVAL · TIMESTAMP · TOTALTRADES · ISIN`

This is decisively better than a screen over the existing 502 because it contains
**the companies that later disappeared**. A turnover screen over `market_data.db`
would fix inclusion *timing* but leave survivorship fully intact — it can only ever
select from today's winners. Bhavcopy removes that ceiling.

Cost: ~1,300 trading days for 2021-06 → 2026-08, 85–200 KB each ≈ 150–250 MB
compressed, ~35–50 min with a polite 1.5 s delay. Trivial against 38 GB free.

---

## 3. Proposed method

**Not** the monthly top-N-by-turnover screen over existing data that I originally
suggested — the bhavcopy source makes a materially better reconstruction possible.
Revised proposal:

### Step 1 — Download the bhavcopy archive
2021-06-01 → 2026-08-10, store raw zips plus a parsed
`bhavcopy_daily(isin, symbol, series, date, o,h,l,c, prevclose, qty, turnover)`
table. Raw files retained so every downstream artifact is reproducible.

### Step 2 — Build an ISIN-keyed security master
ISIN is the stable identity; symbols change (IBULHSGFIN→SAMMAANCAP,
SUVENPHAR→COHANCE, CENTURYTEX→ABREL). Track `isin → [(symbol, from, to)]`.
**All joins key on ISIN.** This alone fixes the 5 unverifiable cases in the
integrity report.

### Step 3 — Reconstruct PIT universe membership
For each month-end T, from **data available strictly on or before T**:
1. take all `SERIES = EQ` securities trading in the 3 months ending T;
2. require ≥ 60 trading sessions of history as of T (proxy for listing age);
3. rank by **median daily turnover** over the trailing 3 months;
4. take the top 500 → that is the universe for month T+1.

Median (not mean) resists single-day turnover spikes. The list is applied to the
month *after* it is computed, so no same-month information is used.

### Step 4 — Rebuild adjusted prices for the full reconstructed universe
Raw bhavcopy OHLCV + a corporate-action ledger (splits/bonuses from
`corporate_events_full.db`, demergers derived from residual discontinuities) →
one consistently back-adjusted series per ISIN. This simultaneously resolves
`DATA_INTEGRITY_REPORT.md` items 1–4, which is why the two jobs should run
together.

### Step 5 — Validate
- Reconstructed Nifty-500-proxy vs the real 2026-07 list: expect high but **not
  perfect** overlap. Perfect overlap would mean the reconstruction has leaked.
- Confirm known-delisted names (HDFC, ADANITRANS) appear in the correct periods
  and disappear at the right dates.
- Assert no unexplained >25% move survives.

---

## 4. Why this does not introduce look-ahead

Information used at each historical date T, and its availability at T:

| Input | Available at T? | Why |
|---|---|---|
| Traded value, trailing 3 months | Yes | Published daily in the bhavcopy by 18:00 IST on day T |
| Series (EQ/BE/BZ) | Yes | Same file — a PIT-safe surveillance proxy |
| Sessions traded to date | Yes | Derived from files ≤ T |
| ISIN | Yes | Same file |
| Split/bonus ex-dates | Yes | Announced in advance |

Explicitly **not** used: current index membership, current market cap, current
D/E, pledge, ROE, today's ASM/ESM list, or any knowledge of which companies later
delisted or merged. Ranking uses turnover only, which was fully observable at T.

The critical property: a company that was liquid in 2022 and delisted in 2024
**enters the 2022 universe and can be traded and lost money on**, because the
reconstruction never consults its fate.

---

## 5. Biases that remain — stated honestly

1. **Turnover ≠ Nifty 500 membership.** NSE uses free-float market cap plus
   liquidity and eligibility screens. A turnover-ranked top-500 is a *different*
   universe — broadly overlapping, not identical. Results should be reported as
   "a liquid-500 universe," not "the Nifty 500." This is a change in what is being
   measured and must not be papered over.
2. **The proxy is a strategy choice with real effects.** Turnover ranking tilts
   toward high-velocity, news-driven and speculative names. For a momentum/breakout
   family that plausibly *helps*; it is not a neutral substitute.
3. **Pre-2021 remains unreconstructed** unless the download is extended. The
   corporate-events DB starts 2021-01, so adjustment quality degrades before that.
   Recommendation: start the usable window at **2021-06-01**.
4. **Bhavcopy holidays/gaps.** Missing files must be logged, not silently skipped;
   a silent skip would distort turnover medians.
5. **Delisted-name price quality is untested.** Bhavcopy gives raw prices for
   delisted securities, but their adjustment history is harder to verify — there
   is no vendor series to check against. Expect these to be the noisiest part.
6. **Survivorship is reduced, not eliminated.** Companies that traded, delisted,
   and left no clean bhavcopy trail (suspensions, prolonged halts) may still be
   under-represented.

---

## 6. Effect on validation

- **Every existing performance number is retired**, not adjusted. The universe
  changes, so the numbers are not comparable — they are measurements of a
  different experiment.
- **Expect the vcp_v2 edge to shrink materially.** With 36% of the real candidate
  pool restored — disproportionately weak and failed names — a strategy that
  looked like PF 1.75 may not clear 1.0. That outcome must be reportable as a
  result, not treated as a bug to engineer around.
- **The three rejections deserve a fair re-test.** They were dismissed on a pool
  biased in their favour, which makes those rejections *more* credible, but they
  should still be re-run on identical corrected infrastructure before Phase 4
  concludes anything.
- **A fresh frozen validation window must be declared after the rebuild**, since
  2025-01 → 2026-06 has now been examined repeatedly. Once frozen, it is not
  tuned against.

---

## 7. Decision required

The proposed method is **Step 1–5 above**, not the original top-N-by-turnover
screen over the existing 502. It costs roughly 45–60 minutes of download plus
rebuild time, and it is the only route found that addresses survivorship rather
than merely inclusion timing.

Two things need your approval before I proceed:

1. **Go ahead with the bhavcopy download and rebuild** (writes only to *new*
   tables/files; `market_data.db` copied to `market_data.db.orig-20260811` first,
   originals never overwritten).
2. **Accept "liquid-500 by turnover" as the reconstructed universe definition**,
   with the caveat in §5.1 carried into every subsequent result.
