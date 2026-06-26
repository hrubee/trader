# PLAYBOOK — SHARED rulebook (ALL accounts). STRATEGY: MARKET-WIDE VOLUME-SPIKE momentum.

> You self-evolve this from your OWN net-of-fee outcomes. Read it FIRST, obey it, and after each closed
> trade update the scoreboard + refine ONE rule. Keep it SHORT. This is the CURRENT strategy — it
> SUPERSEDES any older archetype (rejection-short / RSL-breakout / mother-son) you may remember; for
> ENTRIES, the volume spike below is your ONLY trigger. Safety rails (always-bracketed, sizing, no-naked,
> MAX-2-per-coin) are FIXED in the tool — never weaken them.

## STRATEGY — VOLUME-SPIKE DIRECTIONAL MOMENTUM (demo, HIGHEST AGGRESSION)
- PRIMARY (and only) entry trigger = a 1-MINUTE VOLUME SPIKE, traded on the 15m chart. Run
  `volspike --spike-tf 1m --tf 15m` — it scans every liquid USDT-perp for 1m volume spikes. Take coins with
  **spike_vol_ratio ≥ 30** (latest 1m bar volume ≥ 5× its 60-min avg (60 1m candles)). Higher = better.
- DIRECTION + bracket = from the 15m context: 15m up/di+ → LONG; 15m down/di- → SHORT.
- TAKE every qualifying 5×+ directional spike, multiple concurrent (highest-aggression). Do NOT wait for "perfect".
- SKIP: vol_ratio < 5; a spike bar with no clear direction (doji/tiny body); already-parabolic coins (>25% over ema200 = too late, reverses).

## SETUP ARCHETYPE SCOREBOARD  (net-R per type — BUILD THIS from your own volume-spike trades)
| archetype | trades | avg net-R | verdict |
|---|---|---|---|
| volume-spike-long  (5×+ up bar)   | 0 | — | UNTESTED — gather data |
| volume-spike-short (5×+ down bar) | 0 | — | UNTESTED — gather data |
(populate from real closed trades; learn which vol_ratio band / trend-alignment / direction actually pays)

## SELECTION RULES (volume-spike)
1. Only trade vol_ratio ≥ 5 (hard floor). The bigger the spike, the stronger the signal.
2. Bet the spike bar's direction; require a clear directional body (skip dojis/indecision).
3. Skip parabolic extension (>25% over ema200) — joining late gets reversed.
4. Whole-market: the spiker can be ANY liquid coin, not just majors — that's the edge of scanning everything.

## MANAGEMENT RULES
1. Every entry is bracketed via the FORMULA: LONG → stop = price − 1.5×atr, tp = price + 3×atr ; SHORT → stop = price + 1.5×atr, tp = price − 3×atr. (Correct sides + ~2:1.) NEVER naked.
2. MAX 2 entries per coin/direction per session.
3. Let the bracket work — don't micro-exit. Time-stop: if a spike trade hasn't progressed in ~4h and momentum died, cut it.
4. After a stop, wait for a FRESH spike — no revenge re-entry.

## OPEN QUESTIONS / HYPOTHESES
- Does volume-spike momentum pay NET? Which works better — longs or shorts on the spike? Which vol_ratio band?
- Does requiring trend-alignment (spike WITH the 1h trend) beat taking every spike? Build the scoreboard to find out.
