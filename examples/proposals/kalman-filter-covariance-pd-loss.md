# Task Proposal: kalman-filter-covariance-pd-loss

**Domain:** Estimation / Control systems / Numerical methods

## Overview

A Kalman filter is deployed on a small navigation system that fuses an IMU with a periodic position fix (think: GPS-denied indoor localization or a low-cost UAV in dead-reckoning mode). The state is a textbook 9-dimensional inertial state (position, velocity, attitude error). The filter works perfectly in simulation — on synthetic data it tracks the truth state to within centimeters indefinitely — but in production, after the system has run continuously for around 30–90 minutes, the filter starts producing wildly incorrect estimates and the innovation covariance blows up or the Cholesky factorization of the state covariance fails outright. Restarting the filter clears the symptom for another 30–90 minutes.

The agent is given the filter source (in C or Python), a recorded sensor log from a production run that reproduces the failure at ~58 minutes, the discrete-time model (F, H, Q, R) the system uses, a held-out set of additional production logs the verifier will replay, and the system's runtime constraints (the filter must execute in under 200 µs per step on the embedded target, ruling out arbitrary precision arithmetic). It is not told that the issue is numerical.

## Instruction Crux

Modify the filter so that, on every held-out production log (which span tens of minutes to several hours each), the state-estimation error against the ground-truth recorded by an independent reference system stays within 1.5× the system's design RMS error envelope for the entire log, no Cholesky factorization fails, and the symmetric positive-definiteness of the state covariance — measured as `min_eigenvalue(P) > 0` — is preserved at every filter step. Per-step compute must remain under 200 µs on the verifier's reference hardware.

The agent is free to change the update equations, change the representation of the covariance, change the order of operations, introduce factorizations, or anything else — so long as the filter still produces equivalent state estimates and meets the runtime bound. The interface (input sensor samples, output state + covariance) is fixed.

## Solution Crux

The filter uses the textbook update `P = (I - K H) P_prior`. This form is mathematically equivalent to the Joseph form `P = (I - K H) P_prior (I - K H)ᵀ + K R Kᵀ`, but in floating-point it is *not* numerically equivalent. The textbook form does not preserve symmetry exactly (rounding makes `P` slightly non-symmetric every step), and crucially, it does not preserve positive-definiteness under finite precision when the Kalman gain is poorly conditioned — which happens on this system whenever a high-quality position fix arrives after a long IMU-only stretch, because `H P Hᵀ` has become small relative to `R` and the gain saturates. Over thousands of steps, the asymmetric drift accumulates, eigenvalues that should remain positive cross zero, and the filter collapses.

The right fix is to switch to the Joseph form for the covariance update, which is the textbook recommendation for filters that must run for long horizons under finite precision and which provably preserves symmetry and positive-definiteness regardless of how badly conditioned `K H` becomes. Alternatives that also pass: a square-root filter (UDU or Cholesky-factored covariance, updated via stabilized rank-one updates), or a UD-factored implementation in the Bierman/Thornton style. Wrong-but-tempting fixes: (a) periodically resetting the covariance — kicks the problem down the road and produces estimator discontinuities the held-out logs catch; (b) projecting `P` onto the symmetric cone via `(P + Pᵀ)/2` each step — restores symmetry but does not restore positive-definiteness once eigenvalues have already crossed zero; (c) raising the process noise `Q` — keeps the filter healthy but blows the RMS error envelope on the held-out logs because the filter no longer trusts the IMU. (d) Using double-precision instead of single-precision — works in simulation but doesn't fit the embedded compute budget.

## Difficulty Crux

The bug is invisible in simulation because synthetic data does not exercise the long-horizon conditioning regime that production data does — the position-fix gap distribution in production is heavier-tailed than the simulator's, so the gain spends more time saturated. The agent has to first recognize this is a numerical-conditioning failure rather than a modeling failure (wrong `Q`, wrong `R`, wrong model order, sensor bias) — all of which are reasonable competing hypotheses given the symptom, and most of which are what an estimation engineer reaches for first. Then they have to know the right reformulation, which is textbook to anyone with a graduate-level estimation background but is decidedly not undergrad material — Joseph form, UD factorization, square-root filtering all live in the same numerical-stability chapter that most ML/SLAM practitioners skip past.

The task is not hard from volume — the filter is on the order of a few hundred lines — or from format. It is hard because diagnosing "filter loses positive-definiteness because of finite-precision rounding under saturated gain" is competing against half a dozen other plausible hypotheses, and the right fix is not in the standard EKF tutorials.

## Verification Note

The verifier replays each held-out log through the agent's filter, compares each filter state to the ground-truth state recorded by the reference system, computes per-step error norms, and asserts the time-windowed RMS stays within the design envelope. It also (a) checks at every step that the smallest eigenvalue of the covariance is strictly positive, (b) catches any Cholesky/LDLT factorization failure from the filter, (c) wall-clocks the per-step compute and asserts < 200 µs median + < 500 µs max. Three held-out logs are run; all three must pass. Deterministic given the pinned sensor logs and reference truth. No LLM judge.
