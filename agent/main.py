"""LiveKit worker entrypoint for the Luma Bistro voice agent.

Run modes:
    python -m agent.main download-files   # one-time: turn-detector model weights
    python -m agent.main console          # local terminal voice session (no LiveKit server)
    python -m agent.main dev              # connect to LiveKit (browser/phone demo)
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from livekit import agents
from livekit.agents import AgentSession, MetricsCollectedEvent, metrics
from livekit.plugins import deepgram, silero

from .metrics_logger import LOG_DIR, TurnLatencyTracker
from .restaurant_agent import RestaurantAgent
from .state import CallState

logger = logging.getLogger("luma")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)


def resolve_llm_provider() -> str:
    """Explicit LLM_PROVIDER wins; otherwise infer from whichever key is set."""
    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider in ("openai", "anthropic"):
        return provider
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def build_llm():
    """LLM is provider-pluggable; a voice turn blocks on LLM time-to-first-token,
    so defaults favor the fastest tool-calling tier of each provider
    (gpt-4o-mini / claude-haiku-4-5). Override with LLM_MODEL."""
    if resolve_llm_provider() == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    from livekit.plugins import anthropic

    return anthropic.LLM(model=os.getenv("LLM_MODEL", "claude-haiku-4-5"))


def build_turn_detection():
    """Semantic end-of-turn model; falls back to VAD-only if weights are missing."""
    try:
        from livekit.plugins.turn_detector.english import EnglishModel

        return EnglishModel()
    except Exception as e:  # model files not downloaded yet
        logger.warning("turn detector unavailable (%s); falling back to VAD-only turn taking", e)
        return None


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: agents.JobContext) -> None:
    _setup_logging()
    await ctx.connect()

    turn_detection = build_turn_detection()
    session: AgentSession[CallState] = AgentSession[CallState](
        userdata=CallState(),
        stt=deepgram.STT(model="nova-3", language="en"),
        llm=build_llm(),
        tts=deepgram.TTS(model=os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")),
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
        turn_detection=turn_detection if turn_detection else "vad",
        preemptive_generation=True,  # start LLM/TTS on interim transcript; cancelled on barge-in
    )

    tracker = TurnLatencyTracker()
    usage = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)
        usage.collect(ev.metrics)
        tracker.on_metrics(ev.metrics)

    restaurant_agent = RestaurantAgent()

    async def _shutdown() -> None:
        logger.info("usage summary: %s", usage.get_summary())
        logger.info("latency summary: %s", tracker.summary())
        await restaurant_agent.api.aclose()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(room=ctx.room, agent=restaurant_agent)
    await session.generate_reply(
        instructions="Greet the caller warmly in one short sentence: this is Luma Bistro, ask how you can help."
    )


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
