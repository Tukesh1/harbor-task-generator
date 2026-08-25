# Task Proposal: symplectic-energy-drift

**Domain:** Scientific computing / Numerical analysis

## Overview

The agent inherits a small molecular-dynamics-style integrator that simulates a Hamiltonian N-body system (e.g., a chain of 64 coupled oscillators or a small solar-system-like configuration). The integrator is a textbook-correct velocity-Verlet (symplectic) method and produces excellent energy conservation over short runs of ~10^5 steps. Over the long horizons the production users actually care about — 10^7 steps and beyond — the total energy drifts by several percent in a clearly non-physical direction, even though velocity-Verlet is *provably* energy-bounded for a true symplectic flow at infinite precision.

The agent is given the source, a reproducible run script, and a set of held-out initial conditions. They are told the simulation must conserve energy to a tight relative tolerance over the full long horizon, but they are not told why the current code fails to do so.

## Instruction Crux

Modify the integrator so that, on a battery of held-out initial conditions (which the agent does not see during development), the relative energy error |E(t) − E(0)| / |E(0)| stays below 10⁻⁸ over 10⁷ integration steps, and so that wall-clock runtime stays within a fixed budget set at roughly 2× the unmodified integrator's wall-clock — a real SLO on the production users' simulation runs, which already take many hours. Time-step, step count, and the form of the Hamiltonian are fixed inputs; what the agent changes is how the arithmetic is structured inside the integrator and force evaluation.

The end state is a binary that, when fed each held-out initial condition, prints the energy trace and finishes within the wall-clock budget, and whose energy trace satisfies the bound. The agent is free to reformulate force evaluation, change the order in which terms are summed, change variable representations, introduce compensation, or anything else — as long as the result on the held-out cases is what it is. The author's oracle solution clears the energy bound by 2–3 orders of magnitude (typical residual relative drift ~10⁻¹¹), so the 10⁻⁸ threshold is comfortable for a correct fix and clearly unreachable for the unmodified integrator (whose drift is ~10⁻² over the same horizon).

## Solution Crux

The integrator is mathematically symplectic, so in exact arithmetic it would conserve energy. The drift comes entirely from floating-point: each force evaluation computes a difference of nearly-equal positions (catastrophic cancellation) and then accumulates many such forces by naive summation. Over 10^7 steps the rounding bias is not zero-mean — it correlates with the geometry of the orbit — and the energy random-walks (and biases) out of bound.

The fix has two non-obvious moves. First, restructure the force terms to avoid the cancellation: instead of computing `x_i - x_j` directly when those positions are close, work in difference variables / relative coordinates carried across steps, or use a Kahan-style compensated update for the position itself so that the dominant rounding error is bounded rather than accumulating. Second, use compensated summation (Kahan, Neumaier, or pairwise) when summing forces across particles, so that the O(N) sum doesn't shed log(N) bits into the integrator. The wrong-looking-right fix is to increase the precision globally (use `long double` or `__float128`) — this works numerically but blows the wall-clock SLO on the held-out runs, which is the same constraint the production users have on their own hardware: they cannot afford to make every hours-long simulation 5–10× slower to paper over a roundoff bug.

## Difficulty Crux

The integrator is "correct" in the textbook sense. There is no algorithmic bug to find with a debugger. The bug is in the floating-point structure of an otherwise faithful implementation, and the fix requires the agent to (a) form the right hypothesis (this is not an integrator-order problem, it is a roundoff-accumulation problem), (b) localize *which* arithmetic step is leaking precision, and (c) restructure the math so that the bound is provably tight. This is the bread and butter of computational scientists working in long-horizon dynamical systems, orbital mechanics, and molecular dynamics — and it consistently humbles people who reach for "use higher precision" as a first instinct.

Difficulty is not from corner cases, formatting, or volume. The code is short. What's hard is the diagnosis and the reformulation; both require domain knowledge of floating-point and numerical methods that an undergrad does not typically have.

## Verification Note

The verifier runs the agent's integrator on 5 held-out initial conditions (different masses, coupling constants, and starting configurations than the development set), each for 10⁷ steps. For each run it records the energy trace, computes max relative drift, and asserts < 10⁻⁸. It also enforces a wall-clock budget, set at approximately 2× the unmodified integrator's wall-clock on the verifier's hardware — the same SLO the production users have. Re-running the verifier on the same agent output is deterministic — the integrator and the initial conditions are bit-reproducible — so the pass/fail line is sharp. No LLM judge.
