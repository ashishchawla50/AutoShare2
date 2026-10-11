# DATA_INTEGRITY_REPORT.md

**Date:** 2026-08-11
**Scope:** Priority #2 — split / corporate-action audit of the VPS price database.
**Data audited:** `~/AutoShare/data/market_data.db` — 502 symbols, 767,124 rows,
2019-06-03 → 2026-08-10.
**Ground truth:** NSE daily bhavcopy archive (raw, unadjusted, as-published).
**Nothing was modified.** All VPS operations were read-only.

---

## 1. Headline verdict

The cache **is** corporate-action adjusted, and adjustment is **correct for
ordinary splits and bonuses**. The failures are concentrated in **demergers** and
in **phantom pre-IPO rows**.

| Test | Result |
|---|---|
| Splits/bonuses with a testable ex-date and price data | 109 |
| …showing an unadjusted price jump | **0** |
| Large single-day moves in the adjusted series (≤−25% or ≥+35%) | 40 |
| …verified **genuine** against raw bhavcopy | **28** |
| …proven **fabricated** (adjustment artifacts) | **7** |
| …unverifiable (symbol renamed before 2021) | 5 |

**Correction to an earlier reading.** An intermediate scan flagged "154 of 501
symbols show an adjustment-basis change." That scan was wrong as a corruption
test: correct back-adjustment *also* changes the raw/stored ratio over time. In a
properly adjusted series the ratio steps down monotonically at each corporate
action and ends at exactly 1.000 — which is what 147 of those 154 do. The
decisive test is the one reported above: does the adjusted series contain a move
the raw series does not?

---

## 2. Confirmed defects

Format per the brief: symbol · date · previous price · suspicious price · % change
· likely cause · adjusted/unadjusted · proposed correction · evidence.

### 2.1 — RAINBOW (Rainbow Children's Medicare)

| Field | Value |
|---|---|
| Date | 2022-05-10 |
| Previous stored price | ₹0.75 (dated **2019-06-04**) |
| Suspicious price | ₹450.20 |
| Change in our data | **+59,926.7%** |
| True change (NSE raw) | 542.00 → 450.20 = **−16.9%** |
| Fabricated movement | **+59,943.6 pp** |
| Likely cause | Phantom pre-IPO rows. RAINBOW listed 2022-05-10; the DB holds 2 rows dated 2019-06-03/04 at ₹0.75–0.85 on ~5–6k volume — almost certainly a recycled/incorrect Kite instrument token. |
| Adjusted? | Series is adjusted; these two rows do not belong to this security. |
| **Proposed correction** | **Delete** the 2 rows before 2022-05-10. |
| Evidence | `>200-day gap` scan found only 2 such symbols repo-wide; NSE bhavcopy has no RAINBOW listing before 2022-05-10. |

### 2.2 — LATENTVIEW (Latent View Analytics)

| Field | Value |
|---|---|
| Date | 2021-11-23 |
| Previous stored price | ₹65.06 (dated **2019-08-05**) |
| Suspicious price | ₹488.75 |
| Change in our data | **+651.2%** |
| True change (NSE raw) | 197.00 → 488.75 = **+148.1%** (listing-day pop) |
| Fabricated movement | **+503.1 pp** |
| Likely cause | Phantom pre-IPO rows. LATENTVIEW listed 2021-11-23; the DB holds 6 rows in Aug 2019, one with **volume = 2 shares**. |
| Adjusted? | As above — rows do not belong to this security. |
| **Proposed correction** | **Delete** the 6 rows before 2021-11-23. |
| Evidence | Volume of 2 shares is not a real listed-equity session; NSE bhavcopy has no LATENTVIEW before 2021-11-23. |

### 2.3 — NMDC

| Field | Value |
|---|---|
| Date | 2022-10-27 |
| Previous stored price | ₹15.75 (2022-10-25) |
| Suspicious price | ₹29.25 |
| Change in our data | **+85.7%** |
| True change (NSE raw) | 130.75 → 104.30 = **−20.2%** |
| Fabricated movement | **+105.9 pp** |
| Likely cause | NMDC Steel demerger (ex-date Oct 2022). The vendor's back-adjustment factor steps 8.29 → 3.57 across the event (ratio = raw/stored), a 2.33× step, while the true price ratio was only 1.25×. Over-adjusted by ~1.85×. |
| Adjusted? | Yes — **incorrectly**. |
| **Proposed correction** | Re-derive the pre-event factor from raw bhavcopy so the adjusted series is continuous across the ex-date; or rebuild NMDC from raw + a correct demerger factor. |
| Evidence | Raw bhavcopy 2022-10-27 `PREVCLOSE=130.75, CLOSE=104.30`. A genuine +85.7% single-day move in a ₹40,000 Cr PSU did not occur. |

### 2.4 — SPLPETRO (Supreme Petrochem)

| Field | Value |
|---|---|
| Date | 2022-06-07 |
| Previous stored price | ₹850.95 |
| Suspicious price | ₹433.10 |
| Change in our data | **−49.1%** |
| True change (NSE raw) | 850.95 → 866.20 = **+1.8%** |
| Fabricated movement | **−50.9 pp** |
| Likely cause | **Off-by-one adjustment date.** A ~2× action (1:1 bonus / 2-for-1 split) applied one session out of alignment, so the adjusted series shows the mechanical halving as a real −49% day. |
| Adjusted? | Yes — **misdated**. |
| **Proposed correction** | Shift the adjustment boundary by one session; verify continuity against raw. |
| Evidence | Raw prev-close and close are within 1.8% of each other — no move occurred. This is the cleanest artifact of the seven. |

### 2.5 — VEDL (Vedanta)

| Field | Value |
|---|---|
| Date | 2026-04-30 |
| Previous stored price | ₹404.90 |
| Suspicious price | ₹271.55 |
| Change in our data | **−32.9%** |
| True change (NSE raw) | 773.60 → 271.55 = **−64.9%** |
| Fabricated movement | **+32.0 pp** (under-adjusted) |
| Likely cause | Vedanta demerger. Pre-event prices were scaled, but not enough to leave the series economically continuous — a holder who received the demerged entities did not lose 32.9%. |
| Adjusted? | Yes — **partially**. |
| **Proposed correction** | Recompute the demerger factor from the listing prices of the resulting entities; rescale pre-event history. |
| Evidence | Raw shows −64.9%; stored shows −32.9%. Neither equals the true economic move. |

### 2.6 — SIEMENS

| Field | Value |
|---|---|
| Date | 2025-04-07 |
| Previous stored price | ₹3,754.25 |
| Suspicious price | ₹2,812.45 |
| Change in our data | **−25.1%** |
| True change (NSE raw) | 4,928.15 → 2,812.45 = **−42.9%** |
| Fabricated movement | **+17.8 pp** (under-adjusted) |
| Likely cause | Siemens Energy India demerger, same failure mode as VEDL. |
| Adjusted? | Yes — **partially**. |
| **Proposed correction** | As 2.5. |
| Evidence | As 2.5. |

### 2.7 — ABFRL (Aditya Birla Fashion & Retail)

| Field | Value |
|---|---|
| Date | 2025-05-22 |
| Previous stored price | ₹203.55 |
| Suspicious price | ₹89.85 |
| Change in our data | **−55.9%** |
| True change (NSE raw) | 268.95 → 89.85 = **−66.6%** |
| Fabricated movement | **+10.7 pp** (under-adjusted) |
| Likely cause | ABLBL demerger, same failure mode. |
| Adjusted? | Yes — **partially**. |
| **Proposed correction** | As 2.5. |
| Evidence | As 2.5. |

---

## 3. Why this matters to the backtest

Direction is **mixed**, so this cannot be dismissed as conservative:

- **Fabricated down-moves** (SPLPETRO −50.9pp, VEDL, SIEMENS, ABFRL) trigger stop
  losses that never economically happened → invented losses, and invented
  `technical_breakdown` exits.
- **Fabricated up-moves** (NMDC +105.9pp, LATENTVIEW, RAINBOW) invent windfall
  gains and, worse, corrupt the VCP prior-uptrend gate.

The phantom-row cases are the most insidious. `detect_vcp` computes
`prior_uptrend_pct = (consolidation_start − prior_low) / prior_low`
(`strategy/vcp_detector.py:108`) using `pre_consolidation_df["low"].min()`. With a
phantom ₹0.75 row, RAINBOW's prior uptrend evaluates to ~+59,900% on **every day
of its history** — the ≥30% gate can never fail for that symbol. LATENTVIEW is
the same. Two symbols permanently and silently exempted from a core entry gate.

---

## 4. Known limits of this audit

State these plainly rather than implying the sweep was exhaustive:

1. **Threshold-limited.** Only moves ≤−25% or ≥+35% were examined. Demerger
   mis-adjustments producing 10–20% artifacts are **not** captured. Given 4 of the
   7 confirmed artifacts are demergers, and the Nifty 500 sees roughly 30–50
   demergers over five years, **the true artifact count is very likely higher than
   seven.** A full re-derivation against raw bhavcopy is the only complete answer.
2. **Demergers are invisible to the events database.** `corporate_events_full.db`
   carries `results / dividend / split / bonus` only — there is no demerger
   event type, so demergers cannot be enumerated from stored data and had to be
   inferred from price behaviour.
3. **5 moves unverifiable — and that is itself a finding.** TATACHEM, TATACOMM,
   SAMMAANCAP (×2), ABREL, COHANCE could not be matched because **the database
   stores today's ticker against historical prices**. SAMMAANCAP traded as
   IBULHSGFIN in 2021; COHANCE as SUVENPHAR; ABREL as CENTURYTEX. Any join to
   historical NSE data by symbol will silently fail for renamed securities.
   **The reconstruction must key on ISIN, not symbol.**
4. **Dividend adjustment is present but unquantified.** Ratios drift slowly
   between corporate actions (e.g. CANBK 5.958 → 5.511 across 2021–2024),
   consistent with dividend back-adjustment. That makes the series total-return-like
   rather than price-only. Not wrong, but it should be a stated assumption, since
   it slightly inflates measured price momentum.

---

## 5. Recommended corrections — for approval, not yet applied

Nothing has been changed. In priority order:

| # | Action | Risk |
|---|---|---|
| 1 | Delete 8 phantom pre-IPO rows (RAINBOW 2, LATENTVIEW 6) | Very low — provably not this security |
| 2 | Rebuild NMDC and SPLPETRO adjustment factors from raw bhavcopy | Low — verifiable against ground truth |
| 3 | Re-derive demerger factors for VEDL, SIEMENS, ABFRL | Medium — requires spun-off entity listing prices |
| 4 | Full re-derivation of all 502 series from raw bhavcopy + a corporate-action ledger | Highest effort, only complete answer |
| 5 | Add a regression test asserting no unexplained >25% move exists | Low — prevents recurrence |

Item 4 is the honest recommendation, and it converges with the universe
reconstruction (see `docs/UNIVERSE_RECONSTRUCTION_PLAN.md`): both need the same
bhavcopy download, so they should be executed as one job.

**Before any write, `market_data.db` will be copied to
`market_data.db.orig-20260811` on the VPS and left untouched**, so ORIGINAL vs
CORRECTED remains comparable as required.
