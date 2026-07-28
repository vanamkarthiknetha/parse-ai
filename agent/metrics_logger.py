"""Latency measurement for the voice pipeline.

Aggregates LiveKit per-speech metrics into a single per-turn number the
assessment asks for: end-of-speech -> first audio, which is
    end_of_utterance_delay (VAD/turn detector) + LLM TTFT + TTS TTFB.
Each completed turn is appended to logs/metrics.jsonl for later analysis.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livekit.agents import metrics as lk_metrics

logger = logging.getLogger("luma.metrics")

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


@dataclass
class TurnLatencyTracker:
    """Correlates EOU / LLM / TTS metrics by speech_id and logs per-turn totals."""

    log_file: Path = field(default_factory=lambda: LOG_DIR / "metrics.jsonl")
    _turns: dict[str, dict[str, float]] = field(default_factory=dict)
    totals: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def on_metrics(self, m: Any) -> None:
        speech_id = getattr(m, "speech_id", None)
        if not speech_id:
            return
        turn = self._turns.setdefault(speech_id, {})

        if isinstance(m, lk_metrics.EOUMetrics):
            turn["eou_delay_s"] = m.end_of_utterance_delay
            turn["transcription_delay_s"] = m.transcription_delay
        elif isinstance(m, lk_metrics.LLMMetrics):
            turn["llm_ttft_s"] = m.ttft
        elif isinstance(m, lk_metrics.TTSMetrics):
            turn["tts_ttfb_s"] = m.ttfb

        if {"eou_delay_s", "llm_ttft_s", "tts_ttfb_s"} <= turn.keys() and "logged" not in turn:
            turn["logged"] = 1.0
            total = turn["eou_delay_s"] + turn["llm_ttft_s"] + turn["tts_ttfb_s"]
            self.totals.append(total)
            record = {
                "event": "turn_latency",
                "ts": time.time(),
                "speech_id": speech_id,
                "eou_delay_ms": round(turn["eou_delay_s"] * 1000, 1),
                "llm_ttft_ms": round(turn["llm_ttft_s"] * 1000, 1),
                "tts_ttfb_ms": round(turn["tts_ttfb_s"] * 1000, 1),
                "eos_to_first_audio_ms": round(total * 1000, 1),
            }
            logger.info(json.dumps(record))
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def summary(self) -> dict[str, float]:
        if not self.totals:
            return {}
        s = sorted(self.totals)
        p = lambda q: s[min(len(s) - 1, int(q * len(s)))]
        return {
            "turns": len(s),
            "p50_ms": round(p(0.50) * 1000, 1),
            "p95_ms": round(p(0.95) * 1000, 1),
            "max_ms": round(s[-1] * 1000, 1),
        }
