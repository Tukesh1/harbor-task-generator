# Task Proposal: contaminated-vcf-deconvolution

**Domain:** Bioinformatics / Healthcare genomics

## Overview

A clinical-grade whole-genome variant calling pipeline produced a VCF for what was supposed to be a single human sample. The downstream QC sweep is failing in several ways at once: the Mendelian inheritance check against the trio is flagging a de-novo SNV count an order of magnitude above the biological prior, a handful of clinically meaningful variants disagree with an independent Sanger validation, and the per-chromosome heterozygosity histogram shows a small but reproducible left shoulder that the lab's QC engineer flagged as "unusual." The root cause has not been determined. Candidate causes include sample swap during library prep, mosaicism in the proband, an aligner or variant-caller regression introduced in the last pipeline release, a batch effect from the flowcell, or low-grade cross-sample contamination — the lab cannot rule any of these out from QC alone.

The agent is given: the suspect proband VCF + BAM, the parents' VCFs + BAMs, a panel of allele frequencies from a population reference (e.g., gnomAD subset), the BAMs of the other samples that shared the flowcell run, the pipeline's last-release notes, and the trio's pedigree. It does not have the ground-truth proband genotypes. It is told the lab needs a corrected proband VCF.

## Instruction Crux

Produce a corrected proband VCF such that the precision and recall of the variant calls against a hidden ground-truth set (held by the verifier) both exceed lab-grade thresholds — concretely, F1 ≥ 0.98 on SNVs and ≥ 0.96 on small indels in the high-confidence regions, with the de-novo SNV count against the parents falling within the biological prior (≤ 100 genome-wide for the held-out truth).

How the agent reaches that state is up to it: it may re-call from the BAM, it may filter or genotype-correct the existing VCF, it may downsample reads, it may apply a contamination-aware caller, it may join evidence across the trio, or it may use something else entirely. The deliverable is a single VCF file at a fixed path; nothing about the method is graded.

## Solution Crux

The first non-obvious step is the diagnosis: among the several plausible failure modes the agent has been given, the actual cause is low-grade cross-sample contamination from a sample that shared the flowcell. The right way to discriminate between the candidates is to fit a mixture model on the observed B-allele frequency distribution against population allele frequencies; under a sample swap or a caller regression the BAF distribution would be approximately bimodal around 0.5 and 0/1, while under contamination it shifts toward a mixture-derived shoulder. The other candidate causes (mosaicism, batch effect, aligner regression) leave qualitatively different fingerprints and can be ruled out with the data the agent has. An agent that skips this diagnostic and treats the symptom as generic noise will reach for the wrong fix.

The second non-obvious step is to *estimate* the contamination fraction and identify the contaminant before correcting calls. A naive QC pass — depth filter, GQ filter, low-MAF filter — does not work, because the contamination produces variants that look like genuine low-AF heterozygous calls. The right move is to use the population allele frequencies plus the observed BAF distribution to fit the VerifyBamID-style mixture (maximize the likelihood that the reads come from the proband with probability (1−α) and a contaminant drawn from population frequencies with probability α), then identify the contaminant against the candidate set of co-multiplexed BAMs. Once α and the contaminant identity are pinned, the corrected calls are those where the proband genotype likelihood, computed with the contamination model active, strongly favors a single genotype — variants that look heterozygous only because of contaminant reads collapse back to homozygous reference. Trio-based joint genotyping (using parental genotypes as a strong prior) further removes the residual false positives.

## Difficulty Crux

The unobvious surface is that the data *looks* clean on the standard checks: the BAM is well-aligned, depth is uniform, quality scores are healthy, the caller did not regress. The bug isn't in any single read or in any single component of the pipeline; it's in the mixture. The agent has to first triage between half a dozen plausible explanations using only summary statistics on the data it has — none of which is announced as the answer — and then, having reached the right diagnosis, reframe the corrective work as "two-source mixture, deconvolve it" rather than "noisy single-source data, filter it harder." Both steps require senior clinical bioinformatics judgment; the same task takes a less-experienced analyst weeks because they spend the first week tightening filters on the wrong hypothesis.

The task is not hard due to volume (the contaminated regions are a small fraction of the genome), formatting (the output is a standard VCF), or environment friction. It is hard because the diagnosis is competitive between several reasonable hypotheses and the right corrective model is non-obvious once you settle on the cause.

## Verification Note

The verifier owns the ground-truth proband VCF (generated by an independent technology — e.g., a clean re-sequence of the same individual at high depth, not exposed to the agent). It computes precision/recall/F1 on SNVs and indels separately in high-confidence regions using a standard comparison tool (hap.py or rtg vcfeval) and asserts F1 ≥ 0.98 (SNVs), F1 ≥ 0.96 (indels), and the de-novo-rate cap. The thresholds are set on the basis of the author's oracle solution clearing 0.99/0.98 with margin and the naive "tighten filters" baseline failing on both. The reference is fixed, the comparison tool is deterministic, and the verifier is re-runnable on the same agent output indefinitely with the same verdict. No LLM judge, no string match — just standard bioinformatics F1 numbers against a held-out truth.
