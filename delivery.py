"""Durable result queue for streamed research levels."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal


ChunkState = Literal["pending", "speaking", "spoken", "interrupted", "stale"]


def sanitize_for_speech(text: str) -> str:
    """Remove written-channel syntax without making another LLM call."""
    value = text or ""
    value = re.sub(r"```(?:[^\n]*)\n?(.*?)```", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"!?(?:\[([^\]]+)\])\([^)]*\)", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*>\s?", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"[`*_~]", "", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True)
class ResultChunk:
    run_id: str
    level_id: str
    position: int
    objective: str
    text: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    reference_node_ids: list[str] = field(default_factory=list)
    complete: bool = True
    latency_ms: int = 0
    queries: list[dict[str, Any]] = field(default_factory=list)
    state: ChunkState = "pending"
    spoken_upto: int = 0
    arrived_at: float = field(default_factory=time.monotonic)
    turns_since_arrival: int = 0

    @classmethod
    def from_event(cls, run_id: str, event: dict[str, Any]) -> "ResultChunk":
        return cls(
            run_id=run_id,
            level_id=str(event["level_id"]),
            position=int(event.get("position", 0)),
            objective=str(event.get("objective", "調査結果")),
            text=str(event.get("voice_text") or event.get("text") or ""),
            facts=list(event.get("facts") or []),
            reference_node_ids=[str(value) for value in event.get("reference_node_ids") or []],
            complete=bool(event.get("complete", True)),
            latency_ms=int(event.get("latency_ms") or 0),
            queries=list(event.get("queries") or []),
        )

    @property
    def should_hedge(self) -> bool:
        if not self.complete:
            return True
        for query in self.queries:
            if isinstance(query, dict) and query.get("enough") is False:
                return True
            if isinstance(query, dict) and int(query.get("search_result_count") or 0) < 3:
                return True
        return False

    @property
    def remaining_text(self) -> str:
        return self.text[min(self.spoken_upto, len(self.text)) :].lstrip()


class DeliveryQueue:
    """Retains every result, including spoken, interrupted, and stale chunks."""

    def __init__(self) -> None:
        self._chunks: list[ResultChunk] = []
        self._keys: dict[tuple[str, str], ResultChunk] = {}
        self._fingerprints: set[tuple[str, str]] = set()

    @property
    def chunks(self) -> tuple[ResultChunk, ...]:
        return tuple(self._chunks)

    def enqueue_event(self, run_id: str, event: dict[str, Any]) -> ResultChunk | None:
        chunk = ResultChunk.from_event(run_id, event)
        if not chunk.text.strip():
            return None
        key = (chunk.run_id, chunk.level_id)
        fingerprint = (chunk.level_id, chunk.text)
        if key in self._keys:
            return self._keys[key]
        # A reconnect receives a new run id and replays completed levels. Do not
        # speak the same level body twice.
        if fingerprint in self._fingerprints:
            return None
        self._keys[key] = chunk
        self._fingerprints.add(fingerprint)
        self._chunks.append(chunk)
        return chunk

    def pending(self) -> list[ResultChunk]:
        return sorted(
            (chunk for chunk in self._chunks if chunk.state == "pending"),
            key=lambda chunk: (chunk.position, chunk.arrived_at),
        )

    def next_pending(self) -> ResultChunk | None:
        chunks = self.pending()
        return chunks[0] if chunks else None

    def mark_speaking(self, chunk: ResultChunk) -> None:
        if chunk.state != "pending":
            raise ValueError(f"cannot speak chunk in state {chunk.state}")
        chunk.state = "speaking"

    def mark_spoken(self, chunk: ResultChunk) -> None:
        chunk.spoken_upto = len(chunk.text)
        chunk.state = "spoken"

    def mark_interrupted(self, chunk: ResultChunk, spoken_upto: int) -> None:
        chunk.spoken_upto = max(chunk.spoken_upto, min(spoken_upto, len(chunk.text)))
        chunk.state = "interrupted"

    def resume_interrupted(self) -> ResultChunk | None:
        for chunk in reversed(self._chunks):
            if chunk.state == "interrupted" and chunk.remaining_text:
                chunk.state = "pending"
                return chunk
        return None

    def replay_last(self) -> ResultChunk | None:
        for chunk in reversed(self._chunks):
            if chunk.state == "spoken":
                chunk.spoken_upto = 0
                chunk.state = "pending"
                return chunk
        return None

    def advance_turn(self) -> None:
        for chunk in self._chunks:
            if chunk.state in ("pending", "interrupted"):
                chunk.turns_since_arrival += 1
                if chunk.turns_since_arrival > 2:
                    chunk.state = "stale"

    def compact_backpressure(self, threshold: int = 4) -> ResultChunk | None:
        pending = self.pending()
        if len(pending) < threshold:
            return None
        selected = pending[:threshold]
        summaries = []
        for chunk in selected:
            first_sentence = re.split(r"(?<=[。！？!?])", sanitize_for_speech(chunk.text), maxsplit=1)[0]
            summaries.append(f"{chunk.objective}では、{first_sentence}")
            chunk.state = "stale"
        merged = ResultChunk(
            run_id=selected[0].run_id,
            level_id=f"merged:{time.monotonic_ns()}",
            position=min(chunk.position for chunk in selected),
            objective="調査結果の要点",
            text=" ".join(summaries),
            facts=[fact for chunk in selected for fact in chunk.facts],
            reference_node_ids=list(dict.fromkeys(
                node for chunk in selected for node in chunk.reference_node_ids
            )),
            complete=all(chunk.complete for chunk in selected),
            latency_ms=max(chunk.latency_ms for chunk in selected),
        )
        self._chunks.append(merged)
        self._keys[(merged.run_id, merged.level_id)] = merged
        return merged

    def read_result(self, handle: str) -> str:
        normalized = handle.strip().lower()
        for chunk in reversed(self._chunks):
            if normalized in (chunk.level_id.lower(), chunk.objective.lower()):
                return chunk.text
        for chunk in reversed(self._chunks):
            if normalized and normalized in chunk.objective.lower():
                return chunk.text
        return ""

    def state_block(self) -> str:
        lines = ["[research state]", "handle\tstate\tnote"]
        for chunk in self._chunks[-12:]:
            note = ""
            if chunk.state == "interrupted":
                note = f"{len(chunk.remaining_text)} chars remain"
            elif chunk.state == "stale":
                note = "screen only"
            elif not chunk.complete:
                note = "partial evidence"
            lines.append(f"{chunk.objective}\t{chunk.state}\t{note}")
        return "\n".join(lines)
