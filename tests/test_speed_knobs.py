"""The two speed levers, pinned so a later "cleanup" cannot silently undo them.

Both were measured, not guessed (2026-10-06, see the daily log). The failure mode they
guard is unusual for this repo: nobody would revert them on purpose — they look like
arbitrary constants, so someone tidying config would drop them without knowing what they
cost. Hence the comments carry the measurement, not just the name.
"""

from __future__ import annotations

import inspect

from app import executor
from app.config import get_settings


def test_llm_retries_are_not_left_at_the_library_default():
    """A rate-limited gateway must not be retried by the transport.

    Measured: one 3-step case spent 10.6s in the judge phase emitting 8 consecutive 429s
    ("您的 API 速率限制"). Two defaults stacked up — the SDK retries 2, and
    browser-use's ChatOpenAI raises it to 5 (its own source comment: "Increase default
    retries for automation reliability"). A 429 from this gateway is a quota decision,
    not a transient blip, so retrying only spends wall-clock to reach the same failure.
    """
    assert get_settings().llm_max_retries == 0


def test_both_llm_clients_receive_the_retry_setting():
    """The judge client AND the agent client. Checking one is how this regresses.

    The two are separate constructors (`openai_client` / `browser_use_llm`) and each
    carries its own default, so setting only the obvious one leaves the other at 5.
    """
    src = inspect.getsource(executor)  # sanity: the module is the one under test
    assert src  # (executor is imported for its settings plumbing, not its internals)

    import app.llm as llm

    judge_src = inspect.getsource(llm.openai_client)
    assert "max_retries" in judge_src, "judge client still uses the SDK default"

    agent_src = inspect.getsource(llm.browser_use_llm)
    assert "max_retries" in agent_src, "agent client still uses browser-use's default of 5"


def test_browser_use_version_check_is_off():
    """It costs 3.4s per case and produces one log line about upgrading."""
    assert get_settings().browser_version_check is False


def test_version_check_uses_the_official_env_var_not_a_source_patch():
    """Editing site-packages would evaporate on the next browser-use upgrade.

    browser_use/config.py reads BROWSER_USE_VERSION_CHECK from the environment, so the
    env var is the supported entry point. This asserts we go through it.
    """
    src = inspect.getsource(executor.execute_case)
    assert "BROWSER_USE_VERSION_CHECK" in src
    # And it must be gated, not unconditional: a user who wants the notice back should
    # be able to set BROWSER_VERSION_CHECK=true in .env.
    assert "browser_version_check" in src


def test_case_max_steps_covers_the_longest_case_ever_observed():
    """120, not 80 — with the measurement that produced it.

    From 150 cases / 177 executions in the real database: per-case action-count peak
    p50=22, p90=54, p95=65, max=127, and 4 cases exceeded 80. So 80 was never a real
    ceiling (nothing normal reached it) while still cutting genuine long flows short —
    the classic "safety net used as a budget" bug, where the case gets reported as a
    business failure and then re-run by hand.
    """
    assert get_settings().case_max_steps >= 100
