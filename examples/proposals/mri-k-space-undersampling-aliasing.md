# Task Proposal: mri-k-space-undersampling-aliasing

**Domain:** Medical imaging / Signal processing

## Overview

An MRI reconstruction pipeline for accelerated scans is producing images with a band of clinically unacceptable aliasing artifact near the spine in a knee-protocol acquisition. The scanner team has shipped the same pipeline for years on cardiac and brain protocols where it works fine; the knee protocol uses a 4× acceleration factor with a different k-space sampling pattern (a variable-density Poisson-disc), and that's where the artifact shows up. The artifact is sharp, reproducible, only on the knee protocol, and only at acceleration factors above 3×. It manifests as a coherent ghost of the high-signal region (the femoral marrow) projected ~30 pixels away from its true location — the kind of artifact a radiologist will reject the scan over.

The agent has the reconstruction pipeline (a SENSE/PI variant with iterative SENSE in image space), the k-space sampling masks, the coil sensitivity maps, the raw multi-coil k-space data for a held-out set of knee scans, and a reference set of clinical-quality reconstructions of the same scans produced by an independent ground-truth pipeline that the verifier holds (a fully sampled acquisition reconstructed without any parallel-imaging machinery). It is told to fix the pipeline so reconstructed images meet a clinical fidelity bar against the reference, but it is not told what's broken.

## Instruction Crux

Produce a reconstruction such that, on every held-out knee scan, the reconstructed image meets all of the following against the reference image: (a) NRMSE ≤ 0.05 inside the prescribed FOV, (b) SSIM ≥ 0.95 over the same region, (c) PSNR ≥ 36 dB, and (d) the maximum local error in a 7×7 neighborhood anywhere in the marrow-bearing slices is below a fixed threshold the verifier supplies (this is what catches the aliasing ghost; it doesn't show up in global NRMSE but is fatal clinically). Reconstruction wall-clock per slice must remain under 2× the original pipeline's wall-clock on the verifier's hardware.

The agent is free to change the regularization, the iterative scheme, the coil-combination step, the density compensation, the algorithm entirely (e.g., move to SPIRiT or to an L1-wavelet compressed-sensing formulation), or to fix bugs in any of those. The deliverable is a pipeline that takes raw multi-coil k-space + sensitivity maps and produces an image at a fixed Python entry point.

## Solution Crux

The bug is in the density compensation weights for the variable-density Poisson-disc mask. The pipeline computes a single density-compensation function from the mask, but the Poisson-disc pattern in question has *spatially correlated* gaps near the high-spatial-frequency edges of the central disc — the gaps coherently undersample a specific spatial frequency band, which under iterative SENSE produces coherent aliasing rather than the diffuse, noise-like aliasing the algorithm assumes and that NRMSE happily averages over. On the cardiac and brain protocols, the sampling pattern is different enough that this regime never arises; on the knee protocol it does.

The right fix has two parts. First, replace the analytic density-compensation function with one estimated directly from the mask (Pipe-Menon iterative DCF or a Voronoi-based DCF), so the compensation actually matches the realized sample distribution rather than the intended distribution. Second, augment the iterative SENSE objective with a coherence-suppressing regularizer — either an L1 penalty in the wavelet domain (compressed sensing) or a low-rank prior on a Casorati-style coil/slice matrix (LORAKS / PI-SENSE-CS hybrid). Either one breaks the coherent-aliasing pattern; iterative SENSE alone cannot. Wrong-but-tempting fixes: (a) increasing iterations of the existing SENSE solver — reduces global NRMSE but the ghost persists (it's not a convergence problem, it's a null-space problem under that specific mask); (b) tuning the Tikhonov regularizer — same story; (c) reducing the acceleration factor — fixes the artifact but the gate is at 4× because that's the clinical protocol; (d) using a wholly external deep-learning reconstruction model — possible but typically fails the wall-clock budget and may not generalize to the held-out scans.

## Difficulty Crux

The difficulty is that the artifact pattern *looks* like a parallel-imaging g-factor problem (it would respond to better coil maps) or a phase-encoding problem (it would respond to motion correction), and both are far more common explanations for ghosts in MRI. An MR physicist's first instinct is usually to inspect the sensitivity maps and the prescan, not the density compensation. The agent has to recognize that the artifact is mask-coherent — it ghosts in a direction set by the under-sampling lattice — and then know that variable-density Poisson-disc patterns are a known failure mode for analytic density compensation in iterative SENSE, and that the textbook fix is either an empirical DCF or an explicit incoherence regularizer. This is graduate-level MR-physics + numerical reconstruction work; the failure mode is documented in the literature but no single tutorial walks you from "ghost in knee scans" to "DCF + sparsity prior."

The task is not hard from volume (the pipeline is moderate-sized) or from format (image in, image out). It is hard because the diagnostic chain crosses physics, sampling theory, and iterative reconstruction.

## Verification Note

The verifier runs the agent's pipeline on each held-out knee scan, compares the output to the reference reconstruction produced by the held-out fully-sampled pipeline, and asserts NRMSE, SSIM, PSNR, and max-local-error gates. It additionally runs an aliasing detector that correlates the reconstruction error map with the expected aliasing direction implied by the k-space mask and asserts the correlation falls below a threshold set by the oracle solution's margin. Wall-clock is bounded. Held-out scans (≥ 5 cases) are pinned, the reference reconstructions are pinned, and the metrics are deterministic. No LLM judge.
