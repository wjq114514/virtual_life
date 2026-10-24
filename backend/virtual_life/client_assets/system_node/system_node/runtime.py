"""Fresh heartbeat samples, bounded retries, and significant observations."""

import logging
import random
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .attention import AttentionCollector
from .collectors import LocalCollector, collect_target
from .config import Config, Node
from .json_utils import JsonError, encode_json
from .notify import notify_ready
from .operations import OperationReader, utc_timestamp
from .transport import ApiClient, TransportError, node_path

LOGGER = logging.getLogger(__name__)
MAX_OBSERVATIONS_PER_CYCLE = 32
MAX_OPERATION_DEDUP_ENTRIES = 4096
CATEGORY_FIELDS = ("reachability.status", "source.quality")
PROVENANCE_FIELDS = (
    "schema_version",
    "subject_node_id",
    "observer_node_id",
    "session_id",
    "sequence",
    "sampled_at",
)


def _categories(state: dict[str, Any]) -> dict[str, str]:
    return {
        "reachability.status": state["reachability"]["status"],
        "source.quality": state["source"]["quality"],
    }


def _provenance(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state[key] for key in PROVENANCE_FIELDS}


@dataclass
class CategoryTracker:
    baseline: dict[str, str] | None = None
    pending_command_id: str | None = None
    last_sent_at: float | None = None


@dataclass(frozen=True)
class PendingObservation:
    command_id: str
    subject_node_id: str
    modality: str
    body: bytes
    categories: dict[str, str] | None = None


class Backoff:
    def __init__(self, initial: float, maximum: float):
        self._initial = initial
        self._maximum = maximum
        self._next = initial

    def reset(self) -> None:
        self._next = self._initial

    def delay(self) -> float:
        delay = random.uniform(self._next / 2, self._next)
        self._next = min(self._maximum, self._next * 2)
        return delay


class Runtime:
    def __init__(self, config: Config, api: ApiClient, stop: threading.Event):
        self._config = config
        self._api = api
        self._stop = stop
        self._session_id = str(uuid.uuid4())
        self._sequence = 0
        self._registered: set[str] = set()
        self._local = LocalCollector(config.disk_path)
        self._operations = OperationReader(
            config.operation_file, {node.node_id for node in config.managed_nodes}
        )
        self._trackers = {node.node_id: CategoryTracker() for node in config.managed_nodes}
        self._pending: deque[PendingObservation] = deque()
        self._operation_seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._dropped = 0
        self._ready_sent = False
        self._attention = (
            AttentionCollector(config.attention.session_id) if config.attention.enabled else None
        )
        self._attention_sequence = 0
        self._attention_session = None
        self._attention_diagnostics = None

    def run(self, *, once: bool) -> int:
        backoff = Backoff(self._config.backoff_initial_seconds, self._config.backoff_max_seconds)
        LOGGER.info("reporter_started managed_nodes=%d", len(self._config.managed_nodes))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._cycle()
            except TransportError as exc:
                if not exc.retryable:
                    raise
                LOGGER.warning(
                    "report_retry reason=%s status=%s pending_observations=%d",
                    exc.code,
                    exc.status,
                    len(self._pending),
                )
                if once:
                    return 4
                delay = backoff.delay()
            else:
                backoff.reset()
                LOGGER.info(
                    "report_cycle_complete managed_nodes=%d pending_observations=%d",
                    len(self._config.managed_nodes),
                    len(self._pending),
                )
                if once:
                    return 0
                if not self._ready_sent and not self._stop.is_set():
                    self._ready_sent = notify_ready()
                delay = max(0, self._config.interval_seconds - (time.monotonic() - started))
            self._stop.wait(delay)
        LOGGER.info("reporter_stopped pending_observations=%d", len(self._pending))
        return 0

    def _cycle(self) -> None:
        for node in self._config.managed_nodes:
            if self._stop.is_set():
                return
            if node.node_id not in self._registered:
                self._register(node)
        for node in self._config.managed_nodes:
            if self._stop.is_set():
                return
            self._heartbeat(node)
            if (
                node == self._config.node
                and self._attention is not None
                and not self._stop.is_set()
            ):
                self._report_attention()
        self._flush()

    def _register(self, node: Node) -> None:
        capabilities = ["device.state.report"]
        if node == self._config.node and self._config.deployment_mode == "gateway":
            capabilities.append("device.state.observe")
        if node == self._config.node and self._attention is not None:
            capabilities.append("attention.observe")
        registration = {
            "node_id": node.node_id,
            "label": node.label,
            "kind": "other",
            "capabilities": capabilities,
        }
        if node == self._config.node:
            # Managed gateway targets must not inherit the reporter's IP identity.
            registration["device"] = {"device_id": node.node_id}
        self._api.post("/v1/nodes", registration)
        self._registered.add(node.node_id)

    def _report_attention(self) -> None:
        reading = self._attention.collect()
        self._attention_diagnostics = reading.diagnostics
        if self._attention_session != reading.session_id:
            self._attention_session = reading.session_id
            self._attention_sequence = 0
        self._attention_sequence += 1
        if self._attention_sequence > 2**63 - 1:
            raise TransportError("attention_sequence_exhausted")
        sample = {
            **reading.sample,
            "schema_version": "1.0",
            "session_id": reading.session_id,
            "sequence": self._attention_sequence,
            "sampled_at": reading.diagnostics["sampled_at"],
            "ttl_seconds": 90,
        }
        # No pending queue or replay: a failed request is replaced by a fresh sample.
        self._api.post(
            node_path(self._config.node.node_id, "attention"),
            {
                "command_id": str(uuid.uuid4()),
                "sample": sample,
            },
        )

    def _sample(self, node: Node) -> dict[str, Any]:
        if node == self._config.node:
            source, reachability, metrics = self._local.collect()
        else:
            source, reachability, metrics = collect_target(node)
        now = datetime.now(UTC)
        operation = self._operations.read(now)
        if operation is not None and operation["subject_node_id"] != node.node_id:
            operation = None
        self._sequence += 1
        if self._sequence > 2**63 - 1:
            raise TransportError("sample_sequence_exhausted")
        return {
            "schema_version": "1.0",
            "subject_node_id": node.node_id,
            "device_class": node.device_class,
            "deployment_mode": self._config.deployment_mode,
            "observer_node_id": self._config.node.node_id,
            "session_id": self._session_id,
            "sequence": self._sequence,
            "sampled_at": utc_timestamp(now),
            "ttl_seconds": self._config.ttl_seconds,
            "source": source,
            "reachability": reachability,
            "health": {"status": "unknown", "reason": "not_evaluated"},
            "metrics": metrics,
            "operation": operation,
        }

    def _heartbeat(self, node: Node) -> None:
        for attempt in range(2):
            # Heartbeats are not replayed: each attempt collects a new complete state.
            state = self._sample(node)
            self._track_categories(state)
            self._track_operation(state)
            context = {"device_state": state}
            if node == self._config.node and self._attention_diagnostics is not None:
                context["attention_collection"] = self._attention_diagnostics
            try:
                self._api.post(node_path(node.node_id, "heartbeat"), {"context": context})
                return
            except TransportError as exc:
                if exc.status != 404:
                    raise
                self._registered.discard(node.node_id)
                if attempt == 1:
                    raise TransportError(
                        "node_registration_lost", retryable=True, status=404
                    ) from exc
                self._register(node)

    def _observation(
        self,
        state: dict[str, Any],
        modality: str,
        data: dict[str, Any],
        *,
        user_initiated: bool = False,
        categories: dict[str, str] | None = None,
    ) -> PendingObservation:
        command_id = str(uuid.uuid4())
        try:
            body = encode_json(
                {
                    "command_id": command_id,
                    "modality": modality,
                    "data": data,
                    "user_initiated": user_initiated,
                }
            )
        except JsonError as exc:
            raise TransportError("observation_payload_invalid") from exc
        return PendingObservation(command_id, state["subject_node_id"], modality, body, categories)

    def _track_categories(self, state: dict[str, Any]) -> None:
        tracker = self._trackers[state["subject_node_id"]]
        current = _categories(state)
        if tracker.baseline is None:
            tracker.baseline = current
            return
        if tracker.pending_command_id is not None:
            # Retain the immutable observation for retry. A subsequent fresh sample
            # coalesces later category changes against its acknowledged baseline.
            return
        now = time.monotonic()
        if (
            tracker.last_sent_at is not None
            and now - tracker.last_sent_at < self._config.observation_min_interval_seconds
        ):
            return
        changes = [
            {"field": field, "previous": tracker.baseline[field], "current": current[field]}
            for field in CATEGORY_FIELDS
            if tracker.baseline[field] != current[field]
        ]
        if not changes:
            return
        pending = self._observation(
            state, "device_state", {**_provenance(state), "changes": changes}, categories=current
        )
        self._enqueue(pending)
        tracker.pending_command_id = pending.command_id

    def _track_operation(self, state: dict[str, Any]) -> None:
        operation = state["operation"]
        if operation is None:
            return
        key = (operation["operation_id"], operation["phase"])
        if key in self._operation_seen:
            self._operation_seen.move_to_end(key)
            return
        self._enqueue(
            self._observation(
                state,
                "operation",
                {**_provenance(state), "operation": operation},
                user_initiated=operation["user_initiated"],
            )
        )
        self._operation_seen[key] = None
        if len(self._operation_seen) > MAX_OPERATION_DEDUP_ENTRIES:
            self._operation_seen.popitem(last=False)

    def _enqueue(self, pending: PendingObservation) -> None:
        if len(self._pending) >= self._config.pending_observations_limit:
            dropped = self._pending.popleft()
            if dropped.modality == "device_state":
                tracker = self._trackers[dropped.subject_node_id]
                if tracker.pending_command_id == dropped.command_id:
                    tracker.pending_command_id = None
            self._dropped += 1
            LOGGER.warning("observation_queue_overflow dropped_total=%d", self._dropped)
        self._pending.append(pending)

    def _flush(self) -> None:
        deadline = time.monotonic() + self._config.observation_budget_seconds
        for _ in range(min(len(self._pending), MAX_OBSERVATIONS_PER_CYCLE)):
            # The budget limits starting requests; an in-progress request may overrun.
            if self._stop.is_set() or time.monotonic() >= deadline:
                return
            pending = self._pending[0]
            try:
                self._api.post_encoded(
                    node_path(self._config.node.node_id, "observations"), pending.body
                )
            except TransportError as exc:
                if exc.status == 404:
                    self._registered.discard(self._config.node.node_id)
                    raise TransportError(
                        "observer_registration_lost", retryable=True, status=404
                    ) from exc
                raise
            self._pending.popleft()
            if pending.modality == "device_state":
                tracker = self._trackers[pending.subject_node_id]
                tracker.baseline = pending.categories
                tracker.pending_command_id = None
                tracker.last_sent_at = time.monotonic()
