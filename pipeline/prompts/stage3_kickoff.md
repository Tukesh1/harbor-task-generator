You are doing QA on a Terminal Bench 3 task that is already model-breaking and was
judged fair by the reviewer in Stage 2. Your job is to make it pass `harbor check`
and the QA check suite WITHOUT reducing difficulty or losing the break.

1. Read your playbook in full: `.claude/skills/stage3-qa-task/SKILL.md` — pay
   special attention to the CARDINAL RULE (never reduce difficulty).
2. Read `./inputs/stage1-design-notes.md` so you know the intended difficulty crux
   and which signals must stay hard.

Do an initial self-review of `./task/` against the implementation rubric named in
the playbook and fix obvious metadata / structure / specification gaps (without
hinting the method). Then the conductor will run `harbor check` and the QA check
suite (LLM-judged — no SOTA trial, no oracle/no-op rerun here) and report the
failing criteria and QA issues for you to address round by round.

Remember: satisfy "clarity" criteria by precise outcome specification, never by
adding hints, comments, error messages, or worked steps that would help a
super-strong agent. If a required fix would reduce difficulty, refuse it and
explain. Reply with the files changed and a status line.
