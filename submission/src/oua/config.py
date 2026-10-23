"""Runtime settings. Every budget and threshold the agent uses lives here so it can be reviewed and tuned."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # submission/
DEFAULT_PACK = ROOT.parent / "one_user_one_agent_pack"


@dataclass
class Settings:
    pack_dir: Path = field(default_factory=lambda: Path(os.environ.get("OUA_PACK", DEFAULT_PACK)))
    provider: str = os.environ.get("OUA_PROVIDER", "mock")  # mock | anthropic

    # Per-decision budgets (brief section 7).
    max_model_calls: int = 8
    max_tool_calls: int = 20
    latency_target_ms: int = 60_000
    latency_fail_ms: int = 300_000
    # A home page only helps if it is live before the viewer reaches home (3-40 s after open).
    home_page_soft_budget_ms: int = 3_000

    # Dependency timeouts used on the interactive path.
    model_timeout_ms: int = 2_500
    store_timeout_ms: int = 2_000

    # Trigger freshness: an app open older than this is history, not a live session.
    stale_trigger_s: int = 600
    # Queue delay above which the agent skips optional model calls (load shedding).
    shed_model_calls_queue_ms: int = 5_000
    # Simulated decision workers (replay models real concurrency on a simulated clock).
    workers: int = 8

    # Messaging.
    min_confidence_for_message: float = 0.55
    # A purchase this soon after a decision was already in flight; it cannot be credited to the agent.
    purchase_min_attribution_lag_s: int = 600
    # "Recently lapsed" is not defined in goals.json; this is our documented definition.
    recent_lapse_days: int = 30
    # History coverage starts here; facts like "never completed an episode" are only provable after it.
    history_start: str = "2026-08-31T18:30:00.000Z"  # 2026-09-01 00:00 IST
