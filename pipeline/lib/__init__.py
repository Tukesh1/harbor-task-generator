"""Shared library for the TB3 task-generation pipeline.

Everything the three stage scripts (stage1/stage2/stage3) and the conductor
(run_pipeline.py) rely on lives here — config loading, env/key handling,
harbor wrappers, the SDK agent session, orchestration helpers, cost
accounting, and so on. Stage scripts should stay thin; if a piece of logic
is needed by more than one stage, it belongs in this package.
"""
