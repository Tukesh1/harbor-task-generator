"""Claude Agent SDK session wrapper.

What we need for this pipeline is a persistent, multi-turn agent session
whose context survives across ``.send()`` calls — that's how the stage
builders and the conductor-owned reviewer work (send a round, get a reply,
send feedback, repeat). Message text comes back to the caller; tool activity
and assistant text are appended to a log file on disk so a second Claude Code
session (or the watch monitor) can tail progress live.

Reliability is the whole game here, since these runs are long and headless —
there's no human to hit Ctrl-C when something hangs. So this module handles:

  * STALLED STREAMS  — if no streamed activity arrives within
    stall_timeout_sec, the dead turn is interrupted and re-issued in the SAME
    session (context preserved), up to stall_retries times.
  * TRANSIENT API ERRORS (429/5xx/529/overloaded) — retried with exponential
    backoff + full jitter, up to api_max_retries. Auth/billing errors are
    NEVER retried; they abort loudly.
  * CONTEXT COMPACTION — when the SDK summarizes the conversation mid-run,
    we detect it and inject a recovery note into the next turn's prompt, so
    the agent recovers its state instead of drifting.

Requires ``pip install claude-agent-sdk`` and ANTHROPIC_API_KEY in the env.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from pathlib import Path

try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    _SDK_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - surfaced at runtime
    ClaudeSDKClient = None
    ClaudeAgentOptions = None
    _SDK_IMPORT_ERROR = exc


CLAUDE_CODE_PRESET = "claude_code"

# Substrings that indicate the turn failed for an auth/billing reason rather than
# the agent legitimately producing a (possibly empty) answer.
AUTH_MARKERS = (
    "invalid api key",
    "fix external api key",
    "authentication_error",
    "could not resolve authentication",
    "invalid x-api-key",
    "x-api-key header is required",
    "oauth token has expired",
    "credit balance is too low",
    "please run /login",
)

# Transient API errors that are SAFE to retry (server-side / capacity / transport), as
# opposed to AUTH_MARKERS (never retry). Matched against an error turn's detail text.
TRANSIENT_MARKERS = (
    "overloaded", "529",
    "rate limit", "rate_limit", "429",
    "500", "502", "503", "504",
    "internal server error", "server_error", "api_error",
    "service unavailable", "temporarily unavailable", "bad gateway", "gateway timeout",
    "timeout", "timed out", "connection error", "connection reset", "connection aborted",
)

# Substring of the SDK auto-compaction banner. When the context window overflows the
# SDK summarizes the conversation and the agent continues off a lossy summary — losing
# the skill it read at turn 1 and tending to redo work / drift to generic behavior.
COMPACTION_MARKERS = (
    "session is being continued from a previous conversation that ran out of context",
)


class SessionError(RuntimeError):
    """An agent turn failed outright (auth/billing/error result) instead of
    doing work. Fatal for the stage — callers abort."""


class StallError(SessionError):
    """The model stream went silent (no activity for the inactivity timeout)
    and never recovered across the configured retries. That's a network or
    streaming hang — not an auth problem. Re-running the stage is safe."""


class TransientAPIExhaustedError(SessionError):
    """A transient API error (429/5xx/529/overloaded) persisted even after all
    backoff retries. Server-side trouble, NOT your keys/config — just resume
    the run later."""


class _Stall(Exception):
    """Internal: one turn's stream produced no activity within the inactivity
    window."""


class _TransientAPIError(Exception):
    """Internal: one turn ended in a retryable API error (overloaded /
    rate-limit / 5xx)."""
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# Sentinel marking a cleanly-exhausted response stream (so StopAsyncIteration never
# has to travel through asyncio.wait_for).
_STREAM_DONE = object()


def session_error_hint(exc: Exception) -> str:
    """An actionable one-liner for a failed session, tailored to the cause —
    printed next to the error so whoever is on call knows what to do next."""
    if isinstance(exc, TransientAPIExhaustedError):
        return ("The Anthropic API kept erroring (e.g. 529 Overloaded) even after backoff "
                "retries — a server-side blip, NOT your keys/config. Just resume the stage "
                "(the run dir is preserved); check status.claude.com if it persists.")
    if isinstance(exc, StallError):
        return ("The model stream stalled (a network/streaming hang, NOT auth). "
                "Re-run the stage — the run dir is preserved.")
    return "Almost always auth/billing. Verify: python pipeline/preflight.py --live"


def _short(val, n: int = 200) -> str:
    s = repr(val)
    return s if len(s) <= n else s[: n - 1] + "…"


class AgentSession:
    """Long-lived ClaudeSDKClient session with disk logging.

    Use it as an async context manager:

        async with AgentSession(...) as sess:
            reply = await sess.send("do X")
            reply = await sess.send("now do Y")   # still remembers X
    """

    def __init__(
        self,
        *,
        cwd: Path | str,
        model: str,
        system_append: str,
        log_path: Path | str,
        label: str = "agent",
        permission_mode: str = "bypassPermissions",
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        max_turns: int = 250,
        stall_timeout_sec: float = 600.0,
        stall_retries: int = 2,
        stall_settle_sec: float = 5.0,
        api_max_retries: int = 5,
        api_backoff_base_sec: float = 2.0,
        api_backoff_max_sec: float = 60.0,
        compaction_recovery_note: str | None = None,
        setting_sources: tuple[str, ...] = ("project",),
    ) -> None:
        if ClaudeSDKClient is None:
            raise RuntimeError(
                "claude-agent-sdk is not importable. `pip install claude-agent-sdk`. "
                f"Original error: {_SDK_IMPORT_ERROR}"
            )
        self.cwd = Path(cwd)
        self.label = label
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stall_timeout_sec = float(stall_timeout_sec)
        self.stall_retries = int(stall_retries)
        self.stall_settle_sec = float(stall_settle_sec)
        self.api_max_retries = int(api_max_retries)
        self.api_backoff_base_sec = float(api_backoff_base_sec)
        self.api_backoff_max_sec = float(api_backoff_max_sec)
        self._last_activity = time.monotonic()
        self.compaction_recovery_note = compaction_recovery_note
        self._compacted_pending = False

        opts_kwargs: dict = dict(
            model=model,
            cwd=str(self.cwd),
            system_prompt={"type": "preset", "preset": CLAUDE_CODE_PRESET, "append": system_append},
            permission_mode=permission_mode,
            max_turns=max_turns,
            setting_sources=list(setting_sources),
        )
        if allowed_tools is not None:
            opts_kwargs["allowed_tools"] = allowed_tools
        if disallowed_tools is not None:
            opts_kwargs["disallowed_tools"] = disallowed_tools
        self._options = ClaudeAgentOptions(**opts_kwargs)
        self._client = None
        self._turn = 0
        # --- cost/usage accounting -------------------------------------
        # The SDK's ResultMessage reports CUMULATIVE session totals (verified from
        # the logs: total_cost_usd climbs monotonically across turns, e.g. 2.39 →
        # 6.14 → … → 34.56). So we take the LATEST value each time, NOT a sum —
        # summing cumulative snapshots inflated cost/tokens ~Nx. num_turns is also
        # cumulative (kept via max). See lib/costs.py for how this is surfaced.
        self.cost_usd = 0.0
        self.num_turns = 0
        self.api_ms = 0
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    # -- logging -----------------------------------------------------
    def _write(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self.log_path.open("a") as f:
            f.write(f"[{ts}][{self.label}] {line}\n")

    def _drain_message(self, msg, texts: list[str]) -> None:
        content = getattr(msg, "content", None)
        if content:
            for block in content:
                thinking = getattr(block, "thinking", None)
                if isinstance(thinking, str):
                    snippet = thinking.strip().replace("\n", " ")
                    if snippet:
                        self._write(f"think: {snippet[:400]}")
                    continue
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    texts.append(text)
                    if any(m in text.lower() for m in COMPACTION_MARKERS):
                        self._compacted_pending = True
                        self._write("⚠️ CONTEXT COMPACTION DETECTED — recovery note will be "
                                    "injected at the next turn")
                    snippet = text.strip().replace("\n", " ")
                    if snippet:
                        self._write(f"say: {snippet}")
                    continue
                name = getattr(block, "name", None)
                if name:  # ToolUseBlock
                    self._write(f"tool {name} {_short(getattr(block, 'input', {}))}")
        # ResultMessage carries per-turn cost/usage/turns (AssistantMessage etc. do
        # not have num_turns) — accumulate it for the per-stage cost report.
        if hasattr(msg, "num_turns") or getattr(msg, "total_cost_usd", None) is not None:
            cost = getattr(msg, "total_cost_usd", None)
            if isinstance(cost, (int, float)):
                self.cost_usd = float(cost)          # cumulative session total, not a delta
            nt = getattr(msg, "num_turns", None)
            if isinstance(nt, int):
                self.num_turns = max(self.num_turns, nt)
            ap = getattr(msg, "duration_api_ms", None)
            if isinstance(ap, (int, float)):
                self.api_ms = max(self.api_ms, int(ap))   # cumulative too
            self._set_usage(getattr(msg, "usage", None))  # latest cumulative, not a sum
            self._write(f"turn done (cost_usd~{cost}, session_total~{self.cost_usd:.4f}, "
                        f"turns~{self.num_turns})")

    def _set_usage(self, usage) -> None:
        # ResultMessage.usage is a CUMULATIVE session total, so replace (don't add) —
        # summing it across turns double-counts the same way cost_usd did.
        if not isinstance(usage, dict):
            return
        m = {"input": ("input_tokens", "prompt_tokens"),
             "output": ("output_tokens", "completion_tokens"),
             "cache_read": ("cache_read_input_tokens", "cache_read_tokens"),
             "cache_creation": ("cache_creation_input_tokens", "cache_creation_tokens")}
        for dst, keys in m.items():
            for k in keys:
                v = usage.get(k)
                if isinstance(v, (int, float)):
                    self.tokens[dst] = int(v)
                    break

    async def _heartbeat(self, interval: int = 60) -> None:
        while True:
            await asyncio.sleep(interval)
            waited = time.monotonic() - self._last_activity
            self._write(f"(waiting for API response... {waited:.0f}s since last activity; "
                        f"stall at {self.stall_timeout_sec:.0f}s)")

    # -- lifecycle ---------------------------------------------------
    async def __aenter__(self) -> "AgentSession":
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.__aenter__()
        self._write(f"session started (cwd={self.cwd})")
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._client is not None:
                await self._client.__aexit__(*exc)
        finally:
            self._write("session closed")
            self._client = None

    async def _next_msg(self, agen):
        """One message from the response stream, or _STREAM_DONE when exhausted.
        Keeps StopAsyncIteration from having to pass through asyncio.wait_for."""
        try:
            return await agen.__anext__()
        except StopAsyncIteration:
            return _STREAM_DONE

    async def _consume_once(self) -> str:
        """Drain one turn's response, enforcing a per-message inactivity timeout.

        Returns the assistant text. Raises _Stall if no message arrives within
        stall_timeout_sec (a hung stream), or SessionError on an error/auth
        result.
        """
        texts: list[str] = []
        is_error = False
        subtype = None
        err_detail = None
        got_any = False
        agen = self._client.receive_response().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(self._next_msg(agen),
                                             timeout=self.stall_timeout_sec)
            except asyncio.TimeoutError:
                raise _Stall()
            if msg is _STREAM_DONE:
                break
            got_any = True
            self._last_activity = time.monotonic()
            self._drain_message(msg, texts)
            if getattr(msg, "is_error", False):
                is_error = True
                subtype = getattr(msg, "subtype", None)
                errs = getattr(msg, "errors", None)
                if errs:
                    err_detail = str(errs)
            am_err = getattr(msg, "error", None)
            if am_err:
                err_detail = str(am_err)
        full = "\n".join(texts).strip()
        low = full.lower()
        # Only treat an auth marker as fatal when the turn is itself an error or is
        # essentially empty (a real SDK auth failure is short / flagged is_error).
        # NEVER kill a long, substantive assistant turn just because it *mentions* an
        # auth phrase — e.g. the builder quoting a trial's "401 invalid api key".
        matched = None
        if is_error or len(full) < 400:
            matched = next((m for m in AUTH_MARKERS if m in low), None)
        if matched:                                   # auth/billing → fatal, NEVER retry
            detail = err_detail or matched or subtype or "auth error"
            self._write(f"ERROR turn (auth, is_error={is_error}, marker={matched!r}): {detail}")
            raise SessionError(f"[{self.label}] agent turn failed (auth/billing): {detail}")
        if is_error:
            detail = err_detail or subtype or (full[:300] or "unknown error")
            signal = " ".join(str(x) for x in (subtype, err_detail) if x).lower() + " " + low
            if any(m in signal for m in TRANSIENT_MARKERS):   # 429/5xx/529/overloaded → retry
                self._write(f"TRANSIENT API error turn: {detail}")
                raise _TransientAPIError(detail)
            self._write(f"ERROR turn (is_error=True, non-retryable): {detail}")
            raise SessionError(f"[{self.label}] agent turn failed: {detail}")
        if not got_any:                               # no messages at all → dropped conn, retry
            self._write(f"[{self.label}] no response this turn (connection issue?) — retrying as transient")
            raise _TransientAPIError("no response (connection issue?)")
        return full

    async def _drain_residual(self, budget_sec: float = 15.0) -> None:
        """After an interrupt, consume and throw away whatever is left of the
        killed turn (its '[Request interrupted]' result, a trailing
        tool_result, etc.). Without this, the next query() starts against a
        dirty conversation tail and collides with the dangling tool_use."""
        deadline = time.monotonic() + budget_sec
        try:
            agen = self._client.receive_response().__aiter__()
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    msg = await asyncio.wait_for(self._next_msg(agen), timeout=max(0.5, remaining))
                except asyncio.TimeoutError:
                    break
                if msg is _STREAM_DONE:
                    break
        except Exception as e:
            self._write(f"residual drain skipped: {e!r}")

    async def send(self, prompt: str) -> str:
        """Send a user turn; return the concatenated assistant text.

        Raises SessionError if the turn errored (ResultMessage.is_error,
        AssistantMessage.error) or matched an auth/billing failure marker —
        the pipeline aborts loudly instead of looping forever against an
        empty task.

        Stalls get special treatment: a silent stream (no activity for
        stall_timeout_sec — the classic headless-SDK failure mode, with no
        human around to hit Ctrl-C) is caught by the watchdog. The dead turn
        is interrupted and re-issued in the SAME session (all prior context
        preserved), up to stall_retries times; after that we raise StallError
        rather than hang forever.
        """
        if self._client is None:
            raise RuntimeError("AgentSession used outside its async context")
        # If the SDK compacted the context since the last turn, the agent is now
        # running off a lossy summary — re-inject the core directives so it recovers
        # state instead of redoing work / drifting to generic behavior.
        if self._compacted_pending and self.compaction_recovery_note:
            prompt = self.compaction_recovery_note.rstrip() + "\n\n" + prompt
            self._compacted_pending = False
            self._write("injected compaction-recovery note into this turn's prompt")
        self._turn += 1
        self._write(f"--- turn {self._turn} (prompt {len(prompt)} chars) ---")
        # Two INDEPENDENT retry budgets so neither failure mode eats the other's:
        #   stalls (silent stream)     -> interrupt + re-issue, up to stall_retries
        #   transient API errors (529) -> backoff + re-issue, up to api_max_retries
        stall_count = 0
        api_err_count = 0
        max_iters = self.stall_retries + self.api_max_retries + 2   # safety backstop
        hb = asyncio.create_task(self._heartbeat())
        try:
            for _ in range(max_iters):
                self._last_activity = time.monotonic()
                try:
                    await self._client.query(prompt)
                except Exception as e:  # transport gone, etc.
                    raise SessionError(f"[{self.label}] failed to send turn: {e!r}")
                try:
                    return await self._consume_once()
                except _Stall:
                    stall_count += 1
                    self._write(f"STALL: no API activity for {self.stall_timeout_sec:.0f}s "
                                f"(stall {stall_count}/{self.stall_retries}) — interrupting the dead turn.")
                    try:
                        await asyncio.wait_for(self._client.interrupt(), timeout=20)
                        self._write("interrupt acknowledged.")
                    except Exception as ie:
                        self._write(f"interrupt failed/timed out: {ie!r}")
                    if stall_count > self.stall_retries:
                        raise StallError(
                            f"[{self.label}] model stream stalled — no response for "
                            f"{self.stall_timeout_sec:.0f}s across {stall_count} attempt(s).")
                    # Let the interrupt FULLY flush before re-issuing: an immediate re-query
                    # collides with the half-finished tool_use the interrupt left behind
                    # (the `stop_reason=tool_use` error). Settle, then drain the residual.
                    if self.stall_settle_sec > 0:
                        self._write(f"settling {self.stall_settle_sec:.0f}s for the interrupt to flush "
                                    "before re-issuing…")
                        await asyncio.sleep(self.stall_settle_sec)
                    await self._drain_residual()
                    # loop and re-issue the turn
                except _TransientAPIError as te:
                    api_err_count += 1
                    if api_err_count > self.api_max_retries:
                        raise TransientAPIExhaustedError(
                            f"[{self.label}] transient API error persisted after "
                            f"{self.api_max_retries} retries: {te.detail}")
                    # Exponential backoff with FULL jitter. The error turn already ENDED
                    # (stream fully drained) — not hung — so no interrupt/drain is needed.
                    delay = random.uniform(0, min(self.api_backoff_max_sec,
                                                  self.api_backoff_base_sec * 2 ** (api_err_count - 1)))
                    self._write(f"TRANSIENT API error ({te.detail[:80]}) — retry "
                                f"{api_err_count}/{self.api_max_retries} in {delay:.1f}s (context preserved).")
                    await asyncio.sleep(delay)
                    # loop and re-issue the turn
        finally:
            hb.cancel()
            with suppress(asyncio.CancelledError):
                await hb
        # Reached only if the iteration backstop is hit (counters normally return/raise first).
        raise SessionError(f"[{self.label}] send() exhausted retries unexpectedly "
                           f"(stalls={stall_count}, api_errors={api_err_count}).")


def sdk_available() -> bool:
    """True when claude-agent-sdk actually imported — cheap guard for preflight."""
    return ClaudeSDKClient is not None


def run_live_auth_check(model: str | None = None) -> tuple[bool, str]:
    """One-shot SDK round-trip to prove auth genuinely works (not just that the
    env var exists). Cheap by design: a single 'OK' reply. Returns (ok, detail)."""
    import asyncio

    if ClaudeSDKClient is None:
        return False, f"claude-agent-sdk not importable: {_SDK_IMPORT_ERROR}"
    from claude_agent_sdk import query

    async def _go() -> tuple[bool, str]:
        texts: list[str] = []
        is_error = False
        detail = None
        opts_kwargs = {"max_turns": 1}
        if model:
            opts_kwargs["model"] = model
        async for msg in query(prompt="Reply with exactly: OK", options=ClaudeAgentOptions(**opts_kwargs)):
            for b in (getattr(msg, "content", None) or []):
                t = getattr(b, "text", None)
                if isinstance(t, str):
                    texts.append(t)
            if getattr(msg, "is_error", False):
                is_error = True
                detail = str(getattr(msg, "errors", None) or getattr(msg, "subtype", None))
            am = getattr(msg, "error", None)
            if am:
                detail = str(am)
        full = " ".join(texts).strip()
        marker = next((m for m in AUTH_MARKERS if m in full.lower()), None)
        if is_error or marker:
            return False, (detail or marker or full[:200] or "error")
        return True, (full[:80] or "(empty reply)")

    try:
        return asyncio.run(_go())
    except Exception as e:  # pragma: no cover
        return False, repr(e)
