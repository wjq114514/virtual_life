"""REST and replayable WebSocket transport for the companion runtime."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, webui_session
from .auto_memory import AutoMemoryService, GraphAliasUpdate
from .coordinator import OUTPUT_CAPABILITIES, Coordinator, DomainError, RoutingPolicy
from .delegation import DelegationExecutor, DelegationService
from .dialogue import (
    DialogueCommitRequest,
    DialogueError,
    DialogueRequest,
    DialogueResult,
    DialogueService,
)
from .dialogue_stream import stream_response
from .dist_api import build_dist_router
from .embedding_memory import EmbeddingMemoryService
from .emotion import (
    EmotionCorrection,
    EmotionExperiencePage,
    EmotionInvalidation,
    EmotionService,
    EmotionSnapshot,
)
from .focus import FocusSnapshot
from .inner_life import (
    InnerActivityPage,
    InnerLifePolicy,
    InnerLifeService,
    InnerLifeSnapshot,
)
from .linux_bundle import get_linux_bundle
from .linux_deployment import (
    LinuxDeploymentJob,
    LinuxDeploymentRequest,
    LinuxDeploymentService,
    LinuxInspection,
    SSHAccess,
    SSHHost,
    SSHHostKey,
    SSHSession,
    probe_host_key,
)
from .log import RequestLogMiddleware, get_flow_log_buffer
from .memory import (
    MemoryCorrection,
    MemoryError,
    MemoryMetadataUpdate,
    MemoryPolicy,
    MemoryRecall,
    MemoryService,
    MemoryWrite,
)
from .model_provider import (
    CompatibleChatProvider,
    CompatibleEmbeddingProvider,
    CompatibleRerankProvider,
)
from .models import (
    CompanionCreate,
    ContextUpdate,
    Identifier,
    LeaseRenew,
    NodeHeartbeat,
    NodeRegister,
    ObservationCreate,
    RoomFollow,
    TransferAck,
    TransferPrepare,
    WebAccessHeartbeat,
    WebAccessRequest,
)
from .node_credentials import TOKEN_PREFIX, NodeCredentialService
from .personality import (
    PersonalityCardImport,
    PersonalityError,
    PersonalityHistoryPage,
    PersonalityService,
    PersonalitySnapshot,
    PersonalityStateSnapshot,
    PersonalityStateUpdate,
    PersonalityUpdate,
)
from .personality_evolution import (
    EvolutionPolicy,
    PersonalityEvolutionDecision,
    PersonalityEvolutionDecisionResult,
    PersonalityEvolutionPage,
    PersonalityEvolutionProposal,
    PersonalityEvolutionRefresh,
    PersonalityEvolutionRefreshResult,
    PersonalityEvolutionRejection,
    PersonalityEvolutionService,
)
from .personality_prompt import (
    PersonalityPolicy,
    PersonalityPreview,
    PersonalityPreviewResult,
    PersonalityPromptBuilder,
)
from .ports import EmbeddingProvider, ModelProvider, RerankProvider, SpeechProvider, TokenEstimator
from .presentations import (
    NativeOutputPage,
    PresentationOpen,
    PresentationService,
    PresentationSnapshot,
)
from .proactivity import (
    AttentionObservation,
    AttentionSnapshot,
    ProactivityDecisionPage,
    ProactivityPolicySnapshot,
    ProactivityPolicyUpdate,
    ProactivityService,
    TaskStatusFilter,
)
from .proactivity_output import ProactivityOutputPage, ProactivityOutputService
from .rerank import RerankService
from .responses import (
    CompanionSnapshot,
    DialogueHistoryPage,
    EventPage,
    FlowLogPage,
    GraphSnapshot,
    KnowledgeGraphEntity,
    KnowledgeGraphSnapshot,
    MemoryLogPage,
    MemoryRecord,
    NodeSnapshot,
    OutputLease,
    RecallResult,
    TransferSnapshot,
    WebAccessSnapshot,
)
from .scene_classifier.runtime import SceneClassifier
from .screen_injection import build_injection_guard
from .screen_models import (
    ScreenAnalysis,
    ScreenCommand,
    ScreenFrameReceipt,
    ScreenFrameSnapshot,
    ScreenFrameSubmit,
    ScreenPurgeReceipt,
    ScreenSessionCreate,
    ScreenSessionReceipt,
    ScreenSessionSnapshot,
    ScreenState,
    ScreenUploadReceipt,
    ScreenUploadRegister,
)
from .screen_ports import ScreenVisionProvider
from .screen_provider import CompatibleVisionProvider
from .screen_service import ScreenVisionService
from .screen_worker import ScreenWorker
from .settings import Settings
from .slow_variables import (
    SlowVariablePolicy,
    SlowVariableService,
    SlowVariableSnapshot,
)
from .speech_provider import SpeechProviderRegistry, build_speech_provider
from .tools import (
    CancelScheduledTaskTool,
    DelegationTool,
    ListScheduledTasksTool,
    ScheduleTaskTool,
    ScheduleTool,
    ToolPlugin,
    ToolPluginContext,
    ToolRegistry,
    build_tool_plugins,
)
from .voice_api import build_voice_router
from .voice_service import VoiceService

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class DeploymentBodyLimit:
    """Bound secret-bearing requests before JSON parsing or validation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/deployments/linux"):
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 131072:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": "Deployment request exceeds the size limit",
                        }
                    },
                )
                return await response(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _device_peer(request: Request) -> str | None:
    # Do not merge devices using a proxy's shared address or untrusted forwarded
    # values. Explicit device/browser bindings still work through reverse proxies.
    if "forwarded" in request.headers or "x-forwarded-for" in request.headers:
        return None
    return request.client.host if request.client is not None else None


def _json_safe(value: object) -> object:
    """Return a JSON-compliant copy of *value*.

    JSONResponse renders with ``allow_nan=False``, so a NaN or Infinity that
    survives into a response raises out of the renderer. Request bodies can carry
    those values because the transport parses them before validation runs, and
    pydantic echoes them under ``"input"``. A ``value_error`` context can also
    hold the raised exception itself. Anything json.dumps cannot encode is
    downgraded to its text form instead of being allowed to escape the handler.
    """
    if isinstance(value, float):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value


class MemoryAutomationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    force: bool = True


class MemoryEmbeddingRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int | None = Field(default=None, ge=1, le=64)


class WebUISessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=1, max_length=512)


class WebUISessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool
    expires_at: str | None = None


def create_app(
    settings: Settings | None = None,
    *,
    routing_policy: RoutingPolicy | None = None,
    memory_policy: MemoryPolicy | None = None,
    personality_policy: PersonalityPolicy | None = None,
    evolution_policy: EvolutionPolicy | None = None,
    slow_variable_policy: SlowVariablePolicy | None = None,
    inner_life_policy: InnerLifePolicy | None = None,
    token_estimator: TokenEstimator | None = None,
    model_provider: ModelProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    rerank_provider: RerankProvider | None = None,
    vision_provider: ScreenVisionProvider | None = None,
    speech_provider: SpeechProvider | None = None,
    delegation_executor: DelegationExecutor | None = None,
    tool_plugins: Sequence[ToolPlugin] | None = None,
    linux_ssh_connector: Callable | None = None,
    linux_bundle_factory: Callable | None = None,
    scene_classifier: SceneClassifier | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    if tool_plugins and not settings.tools_enabled:
        raise ValueError("Injected tool plugins require tools.enabled")
    settings.prepare_directory()
    database_path = str(Path(settings.database_path).expanduser().resolve())
    coordinator = Coordinator(
        database_path,
        node_ttl_seconds=settings.node_ttl_seconds,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        transfer_ttl_seconds=settings.transfer_ttl_seconds,
        routing_policy=routing_policy,
        auto_follow_rooms=settings.auto_follow_rooms,
        auto_follow_quiet_seconds=settings.auto_follow_quiet_seconds,
    )
    node_credentials = NodeCredentialService(coordinator)
    presentations = PresentationService(coordinator)
    linux_deployments = LinuxDeploymentService(
        coordinator,
        node_credentials,
        connector=linux_ssh_connector or SSHSession,
        bundle_factory=linux_bundle_factory or get_linux_bundle,
    )
    memory = MemoryService(
        database_path,
        policy=memory_policy or settings.memory_policy,
        token_estimator=token_estimator,
    )
    personality = PersonalityService(database_path, clock=coordinator.clock)
    emotion = EmotionService(personality, memory, policy=settings.emotion_policy)
    slow_variables = SlowVariableService(
        personality, emotion, policy=slow_variable_policy or settings.slow_variable_policy
    )
    inner_life = InnerLifeService(
        personality,
        emotion,
        slow_variables,
        coordinator,
        policy=inner_life_policy or settings.inner_life_policy,
        memory_service=memory,
    )
    provider = model_provider
    if provider is None and settings.model_base_url:
        provider = CompatibleChatProvider(
            settings.model_base_url,
            settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            extra_body=settings.model_extra_body,
        )
    inner_life.attach_model(provider, settings.model_name)
    personality_evolution = PersonalityEvolutionService(
        personality,
        emotion,
        memory,
        policy=evolution_policy or settings.evolution_policy,
        provider=provider,
        model_name=settings.model_name,
        token_estimator=token_estimator,
    )
    personality_prompts = PersonalityPromptBuilder(
        memory,
        policy=personality_policy or settings.personality_policy,
        token_estimator=token_estimator,
    )
    automatic_memory = AutoMemoryService(
        memory,
        provider=provider,
        model_name=settings.model_name,
        enabled=settings.auto_memory_enabled,
        batch_size=settings.auto_memory_batch_size,
        max_wait_seconds=settings.auto_memory_max_wait_seconds,
        output_token_limit=settings.auto_memory_output_tokens,
        graph_max_hops=settings.graph_max_hops,
        graph_candidate_limit=settings.graph_candidate_limit,
    )
    memory.automatic_memory = automatic_memory
    vector_provider = embedding_provider
    if vector_provider is None and settings.embedding_base_url:
        vector_provider = CompatibleEmbeddingProvider(
            settings.embedding_base_url,
            settings.embedding_api_key,
            timeout_seconds=settings.embedding_timeout_seconds,
            dimensions=settings.embedding_dimensions,
            extra_body=settings.embedding_extra_body,
        )
    if rerank_provider is None and settings.rerank_base_url:
        rerank_provider = CompatibleRerankProvider(
            settings.rerank_base_url,
            settings.rerank_api_key,
            timeout_seconds=settings.rerank_timeout_seconds,
        )
    rerank_service = RerankService(
        rerank_provider,
        model_name=settings.rerank_model_name,
        enabled=settings.rerank_enabled,
        candidate_limit=settings.rerank_candidate_limit,
        min_candidates=settings.rerank_min_candidates,
        rrf_weight=settings.rerank_rrf_weight,
        timeout_seconds=settings.rerank_timeout_seconds,
    )
    embeddings = EmbeddingMemoryService(
        memory,
        provider=vector_provider,
        model_name=settings.embedding_model_name,
        enabled=settings.embedding_enabled,
        batch_size=settings.embedding_batch_size,
        max_input_tokens=settings.embedding_max_input_tokens,
        dimensions=settings.embedding_dimensions,
        space_identity=settings.embedding_space_identity,
        candidate_limit=settings.embedding_candidate_limit,
        similarity_threshold=settings.embedding_similarity_threshold,
        query_cache_size=settings.embedding_query_cache_size,
        query_cache_ttl_seconds=settings.embedding_query_cache_ttl_seconds,
        local=settings.embedding_local,
        rerank=rerank_service,
    )
    screen_vision = ScreenVisionService(coordinator, settings.screen_vision)
    if scene_classifier is None:
        scene_classifier = SceneClassifier(settings.scene_classifier)
    dialogue = DialogueService(
        coordinator,
        personality,
        memory,
        personality_prompts,
        provider=provider,
        model_name=settings.model_name,
        automatic_memory=automatic_memory,
        embedding_memory=embeddings if settings.embedding_dialogue_recall else None,
        emotion_service=emotion,
        slow_variables=slow_variables,
        inner_life=inner_life,
        screen_service=screen_vision,
        scene_classifier=scene_classifier,
    )
    if speech_provider is not None:
        speech_registry = SpeechProviderRegistry(speech_provider)
    else:
        speech_registry = build_speech_provider(settings.speech)
    voice = VoiceService(
        coordinator,
        dialogue,
        memory,
        presentations,
        speech_registry,
        voice_config=settings.voice,
        speech_config=settings.speech,
        audio_dir=Path(settings.database_path).expanduser().resolve().parent / "voice-audio",
    )
    proactivity = ProactivityService(
        dialogue,
        recovery_seconds=max(300, settings.model_timeout_seconds + 60),
        inner_life=inner_life,
        schedule_policy=settings.proactive_schedule,
    )
    proactivity_output = ProactivityOutputService(proactivity)
    # Birthday facts extracted by automatic memory schedule a durable reminder
    # through the same channel a model reminder uses, so delivery honours the
    # quiet, attention, budget, lease and cooldown gates.
    automatic_memory.birthday_reminder = (
        lambda companion_id, command_id, title, details, delay_seconds: proactivity.schedule_task(
            companion_id,
            command_id,
            "birthday",
            title,
            details,
            delay_seconds,
        )
    )
    delegation = DelegationService(
        coordinator,
        config=settings.delegation,
        executor=delegation_executor,
    )
    tool_runtime = None
    if settings.tools_enabled:
        tool_runtime = ToolRegistry(max_calls_per_turn=settings.tools_max_calls_per_turn)
        tool_runtime.register(ScheduleTool(proactivity))
        tool_runtime.register(ScheduleTaskTool(proactivity))
        tool_runtime.register(ListScheduledTasksTool(proactivity))
        tool_runtime.register(CancelScheduledTaskTool(proactivity))
        if settings.delegation.enabled:
            tool_runtime.register(DelegationTool(delegation))
        plugin_context = ToolPluginContext(
            coordinator=coordinator,
            memory=memory,
            personality=personality,
            proactivity=proactivity,
            delegation=delegation,
            dialogue=dialogue,
            settings=settings,
        )
        for plugin in build_tool_plugins(settings.tools_plugins, plugin_context):
            tool_runtime.register(plugin)
        for plugin in tool_plugins or ():
            tool_runtime.register(plugin)
    dialogue.tool_runtime = tool_runtime
    model_configured = provider is not None and bool(settings.model_name)
    screen_provider = vision_provider
    if screen_provider is None and settings.screen_vision.provider_configured:
        screen_provider = CompatibleVisionProvider(
            settings.screen_vision.base_url,
            settings.screen_vision.api_key,
            model=settings.screen_vision.model_name,
            timeout_seconds=settings.screen_vision.timeout_seconds,
            extra_body=dict(settings.screen_vision.extra_body),
            json_mode=settings.screen_vision.json_mode,
        )
    screen_injection_guard = build_injection_guard(settings.screen_vision)
    screen_worker = ScreenWorker(
        screen_vision,
        memory,
        provider=screen_provider,
        injection_guard=screen_injection_guard,
    )

    def token_matches(candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), settings.api_token.encode("utf-8"))

    def webui_session_expiry() -> int:
        return int(time.time()) + int(settings.webui_session_days * webui_session.SECONDS_PER_DAY)

    def remember_webui_session(response: Response, request: Request, expires_at: int) -> None:
        # HttpOnly keeps the token out of JavaScript; SameSite=Strict keeps it out
        # of cross-site requests, so the browser is the only place it ever lives.
        response.set_cookie(
            webui_session.COOKIE_NAME,
            value=webui_session.sign_session_cookie(settings.api_token, expires_at),
            max_age=max(expires_at - int(time.time()), 1),
            expires=datetime.fromtimestamp(expires_at, UTC),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )

    def forget_webui_session(response: Response, request: Request) -> None:
        response.delete_cookie(
            webui_session.COOKIE_NAME,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )

    async def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is not None:
            token = credentials.credentials
            if token_matches(token):
                yield
                return
            if token.startswith(TOKEN_PREFIX):
                segments = request.url.path.split("/")
                if "screen" in segments or "voice" in segments:
                    # The first screen-vision routes accept only the administrative
                    # token; never let the JSON-oriented reporter guard parse images.
                    raise DomainError(
                        403, "forbidden", "Screen and voice require an administrative token"
                    )
                body = await request.json() if request.method == "POST" else None
                with node_credentials.mutation_guard(token, request.method, request.url.path, body):
                    yield
                return
        # The WebUI keeps the administrative token in an HttpOnly session cookie so a
        # reload does not ask for it again. The cookie is only consulted when the
        # request presents no bearer credential text: a wrong one is a 401, never a
        # fallback to a remembered token. A zero lifetime disables this even for
        # cookies minted before the setting changed.
        authorization = request.headers.get("authorization", "")
        presented = authorization.rsplit(" ", 1)[-1].strip() if authorization else ""
        if (
            settings.webui_session_days > 0
            and not presented
            and webui_session.verify_session_cookie(
                settings.api_token, request.cookies.get(webui_session.COOKIE_NAME, "")
            )
        ):
            yield
            return
        raise DomainError(401, "unauthorized", "A valid runtime bearer token is required")

    async def reconciliation_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(coordinator.reconcile)
            except Exception:
                logger.exception("Transfer reconciliation failed")
            await asyncio.sleep(settings.reconcile_seconds)

    async def automatic_memory_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(automatic_memory.process_next)
            except Exception:
                logger.exception("Automatic memory processing failed")
            # Sweep derivatives whose sources are no longer live so the table
            # does not accumulate rows that recall already hides. Stale
            # summaries are rebuilt from their surviving sources rather than
            # only deleted; relations are simply dropped.
            try:
                await asyncio.to_thread(automatic_memory.prune_derivatives, limit=500, rebuild=True)
            except Exception:
                logger.exception("Automatic memory derivative pruning failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.auto_memory_poll_seconds)
            except TimeoutError:
                pass

    async def embedding_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(embeddings.index_pending)
            except MemoryError as exc:
                if exc.code != "embedding_busy":
                    logger.warning("Embedding indexing paused: %s", exc.code)
            except Exception:
                logger.exception("Embedding indexing failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.embedding_poll_seconds)
            except TimeoutError:
                pass

    async def inner_life_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(inner_life.tick)
            except Exception:
                logger.exception("Inner life evaluation failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.inner_life_poll_seconds)
            except TimeoutError:
                pass

    async def proactivity_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(proactivity.process_next)
            except Exception:
                logger.exception("Proactive participation failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.proactivity_poll_seconds)
            except TimeoutError:
                pass

    async def delegation_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(delegation.process_next)
            except Exception:
                logger.exception("Delegated task failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def screen_worker_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await asyncio.to_thread(screen_worker.process_next)
            except Exception:
                logger.exception("Screen vision task failed")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.screen_vision.worker_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def screen_cleanup_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(screen_vision.cleanup)
            except Exception:
                logger.exception("Screen vision cleanup failed")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.screen_vision.cleanup_interval_seconds
                )
            except TimeoutError:
                pass

    async def voice_worker_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await asyncio.to_thread(voice.process_next)
            except Exception:
                logger.exception("Voice turn execution failed")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.2)
                except TimeoutError:
                    pass

    async def voice_maintenance_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(voice.maintain)
            except Exception:
                logger.exception("Voice maintenance failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    dialogue_workers = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "Runtime %s starting (database=%s, model=%s, embeddings=%s, tools=%s)",
            __version__,
            database_path,
            settings.model_name or "local",
            "on" if settings.embedding_enabled else "off",
            "on" if settings.tools_enabled else "off",
        )
        task = asyncio.create_task(reconciliation_loop(), name="transfer-reconciliation")
        stop = asyncio.Event()
        memory_task = (
            asyncio.create_task(automatic_memory_loop(stop), name="automatic-memory")
            if settings.auto_memory_enabled
            else None
        )
        embedding_task = (
            asyncio.create_task(embedding_loop(stop), name="memory-embeddings")
            if settings.embedding_enabled
            else None
        )
        proactive_task = asyncio.create_task(proactivity_loop(stop), name="proactive-participation")
        inner_life_task = asyncio.create_task(inner_life_loop(stop), name="inner-life")
        voice_tasks = []
        if settings.voice.enabled:
            await asyncio.to_thread(voice.recover)
            voice_tasks = [
                asyncio.create_task(voice_worker_loop(stop), name=f"voice-worker-{index}")
                for index in range(2)
            ]
            voice_tasks.append(
                asyncio.create_task(voice_maintenance_loop(stop), name="voice-maintenance")
            )
        screen_tasks = []
        if settings.screen_vision.enabled:
            screen_tasks.append(
                asyncio.create_task(screen_cleanup_loop(stop), name="screen-vision-cleanup")
            )
            if not settings.screen_vision.single_pass:
                await asyncio.to_thread(screen_worker.recover)
                for index in range(settings.screen_vision.global_concurrency):
                    screen_tasks.append(
                        asyncio.create_task(
                            screen_worker_loop(stop), name=f"screen-vision-worker-{index}"
                        )
                    )
        delegation_task = None
        if settings.delegation.enabled:
            await asyncio.to_thread(delegation.recover)
            delegation_task = asyncio.create_task(delegation_loop(stop), name="delegation-worker")
        logger.info("Runtime ready")
        try:
            yield
        finally:
            logger.info("Runtime stopping")
            stop.set()
            if settings.voice.enabled:
                await asyncio.to_thread(voice.close)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if memory_task is not None:
                # Finish an in-flight provider call and its durable receipt before exit.
                await asyncio.gather(memory_task, return_exceptions=True)
            if embedding_task is not None:
                await asyncio.gather(embedding_task, return_exceptions=True)
            await asyncio.gather(proactive_task, return_exceptions=True)
            await asyncio.gather(inner_life_task, return_exceptions=True)
            if delegation_task is not None:
                # A running subprocess finishes and writes its receipt before exit.
                await asyncio.gather(delegation_task, return_exceptions=True)
            await asyncio.gather(*voice_tasks, return_exceptions=True)
            if screen_tasks:
                # Let an in-flight vision call finish and persist its receipt.
                await asyncio.gather(*screen_tasks, return_exceptions=True)
            if dialogue_workers:
                await asyncio.gather(*tuple(dialogue_workers), return_exceptions=True)
            await asyncio.to_thread(scene_classifier.close)
            await asyncio.to_thread(linux_deployments.close)
            logger.info("Runtime stopped")

    app = FastAPI(
        title="Virtual Life Node Runtime",
        version=__version__,
        description=(
            "A persistent companion travels between capability-described device nodes. "
            "Local prototype with versioned state, output leases, replayable events "
            "and shared personality with budgeted long-term memory and optional text dialogue. "
            "WebSocket protocol: docs/protocol.md."
        ),
        lifespan=lifespan,
    )
    app.state.coordinator = coordinator
    app.state.node_credentials = node_credentials
    app.state.presentations = presentations
    app.state.linux_deployments = linux_deployments
    app.state.memory = memory
    app.state.automatic_memory = automatic_memory
    app.state.embeddings = embeddings
    app.state.personality = personality
    app.state.emotion = emotion
    app.state.slow_variables = slow_variables
    app.state.inner_life = inner_life
    app.state.personality_evolution = personality_evolution
    app.state.personality_prompt_builder = personality_prompts
    app.state.dialogue = dialogue
    app.state.scene_classifier = scene_classifier
    app.state.proactivity = proactivity
    app.state.delegation = delegation
    app.state.tools = tool_runtime
    app.state.screen_vision = screen_vision
    app.state.screen_worker = screen_worker
    app.state.voice = voice
    app.state.speech = speech_registry
    app.state.settings = settings

    async def runtime_error_handler(
        _, exc: DomainError | MemoryError | PersonalityError | DialogueError
    ) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {}
        headers.update(getattr(exc, "headers", {}) or {})
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers or None,
        )

    app.add_exception_handler(DomainError, runtime_error_handler)
    app.add_exception_handler(MemoryError, runtime_error_handler)
    app.add_exception_handler(PersonalityError, runtime_error_handler)
    app.add_exception_handler(DialogueError, runtime_error_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/v1/deployments/linux"):
            # Pydantic's default errors may echo raw passwords/private keys.
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "validation_error", "message": "Invalid deployment request"}
                },
            )
        # Pydantic keeps the offending value under "input", so a NaN or Infinity
        # in the request body survives all the way into this response. Starlette
        # renders JSON with allow_nan=False and raises there, which escapes this
        # handler as an HTTP 500 with a truncated non-JSON body and tears down
        # the keep-alive connection mid-response.
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe(exc.errors())},
        )

    app.add_middleware(DeploymentBodyLimit)
    app.add_middleware(RequestLogMiddleware, access=settings.log_access)
    router = APIRouter(prefix="/v1", dependencies=[Depends(authorize)])
    router.include_router(build_voice_router(voice))
    router.include_router(build_dist_router(settings))

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/ui/")

    app.mount(
        "/ui", StaticFiles(directory=Path(__file__).parent / "webui", html=True), name="webui"
    )

    @app.get("/healthz", tags=["runtime"])
    def health() -> dict:
        return {"status": "ok", "version": __version__, "protocol_version": "1.0"}

    @app.post("/v1/session", response_model=WebUISessionStatus, tags=["runtime"])
    def create_webui_session(
        request: Request, response: Response, body: WebUISessionCreate
    ) -> WebUISessionStatus:
        """Remember the administrative token in an HttpOnly session cookie."""
        if settings.webui_session_days <= 0:
            raise DomainError(
                503, "session_disabled", "Session cookies are disabled on this runtime"
            )
        if not token_matches(body.token):
            raise DomainError(401, "unauthorized", "A valid runtime bearer token is required")
        expires_at = webui_session_expiry()
        remember_webui_session(response, request, expires_at)
        return WebUISessionStatus(
            active=True, expires_at=datetime.fromtimestamp(expires_at, UTC).isoformat()
        )

    @app.delete("/v1/session", response_model=WebUISessionStatus, tags=["runtime"])
    def delete_webui_session(request: Request, response: Response) -> WebUISessionStatus:
        """Forget the token for this browser."""
        forget_webui_session(response, request)
        return WebUISessionStatus(active=False)

    @router.get("/capabilities", tags=["runtime"])
    def capabilities() -> dict:
        return {
            "protocol_version": "1.0",
            "runtime_mode": "single_owner_local",
            "travel": "ready_released_with_output_lease",
            "events": "durable_sequence_replay",
            "memory": {
                "policy": memory.policy_snapshot(),
                "external_models_configured": model_configured,
                "automatic_memory_enabled": settings.auto_memory_enabled,
                "automatic_batch_size": settings.auto_memory_batch_size,
                "automatic_max_wait_seconds": settings.auto_memory_max_wait_seconds,
                "derived_summaries": True,
                "knowledge_graph": "sqlite_canonical_evidence_graph",
                "graph_max_hops": settings.graph_max_hops,
                "graph_candidate_limit": settings.graph_candidate_limit,
                "entity_aliases": "explicit_versioned",
                "embeddings_enabled": settings.embedding_enabled,
                "embeddings_configured": vector_provider is not None
                and bool(settings.embedding_model_name),
                "embedding_model": settings.embedding_model_name or None,
                "embedding_local": settings.embedding_local,
                "semantic_recall": True,
                "diagnostic_logs": True,
                "dialogue_semantic_recall": settings.embedding_enabled
                and settings.embedding_dialogue_recall,
                "rerank_enabled": settings.rerank_enabled,
                "rerank_model": settings.rerank_model_name or None,
            },
            "personality": {
                "persistent_profiles": True,
                "expiring_states": True,
                "character_card_import": "v2_json_subset",
                "prompt_preview": True,
                "policy": personality_prompts.policy.model_dump(mode="json"),
                "external_models_configured": model_configured,
                "autonomous_dialogue": True,
                "evolution": {
                    "enabled": personality_evolution.policy.enabled,
                    "generators": ["rules_v1"]
                    + (["model_v1"] if personality_evolution.policy.model_assisted else []),
                    "model_assisted": personality_evolution.policy.model_assisted,
                    "model_available": provider is not None and bool(settings.model_name),
                    "owner_adoption_required": True,
                    "revision_history": True,
                    "policy": personality_evolution.policy.model_dump(mode="json"),
                },
                "slow_variables": {
                    "enabled": slow_variables.policy.enabled,
                    "read_only": True,
                    "affects": "prompt_and_participation",
                    "policy": slow_variables.policy.model_dump(mode="json"),
                },
                "inner_life": {
                    "enabled": inner_life.policy.enabled,
                    "generators": ["rules_v1"]
                    + (["model_v1"] if inner_life.policy.model_assisted else []),
                    "model_assisted": inner_life.policy.model_assisted,
                    "model_available": provider is not None and bool(settings.model_name),
                    "affects": "prompt_and_proactive_trigger",
                    "proactive_trigger": True,
                    "policy": inner_life.policy.model_dump(mode="json"),
                },
            },
            "emotion": {
                "policy": emotion.policy.model_dump(mode="json"),
                "persistent": True,
                "appraisal_version": "rules_v2",
                "correction_kinds": ["direction", "intensity", "attribution"],
                "dimensional_recovery": True,
                "model_calls": 0,
            },
            "dialogue": {
                "configured": model_configured,
                "model": settings.model_name if model_configured else None,
                "max_output_tokens": 1024,
                "autonomous_dialogue": True,
                "streaming": True,
                "provider_streaming": callable(getattr(provider, "stream", None)),
            },
            "scene_classifier": scene_classifier.capabilities(),
            "voice": voice.capabilities(),
            "native_presentation": {
                "supported": True,
                "kinds": ["terminal", "android"],
                "presentation_ttl_seconds": presentations.ttl_seconds,
                "native_output": True,
                "dialogue_history": True,
                "transfer_presentation_binding": True,
            },
            "proactivity": {
                "configured": model_configured,
                "enabled_by_default": False,
                "attention_ingestion": True,
                "poll_seconds": settings.proactivity_poll_seconds,
                "boundaries": ["task_completed", "break_started", "user_returned"],
                "origins": ["attention_boundary", "inner_activity"],
                "audio": False,
            },
            "perception": {"observation_ingestion": True, "recognition_configured": False},
            "screen_vision": screen_vision.capabilities(),
            "devices": {
                "physical_nodes": True,
                "web_access_sessions": True,
                "private_ip_matching": True,
                "explicit_binding": True,
                "access_ttl_seconds": coordinator.node_ttl_seconds,
            },
            "linux_deployment": {
                "ssh": True,
                "host_key_pinning": True,
                "scoped_reporter_credentials": True,
                "attention_collection": {
                    "enabled_by_default": False,
                    "sources": ["logind", "x11_application_category"],
                    "content_capture": False,
                },
                "python_minimum": "3.12",
                "systemd": True,
                "privilege": "root_or_passwordless_sudo",
                "deployment_mode": "direct",
            },
        }

    @router.get("/deployments/linux/bundle", tags=["deployment"])
    def linux_bundle() -> Response:
        bundle = linux_deployments.bundle_factory()
        return Response(
            bundle.data,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="virtual-life-linux-{bundle.version}.tar.gz"',
                "X-Content-SHA256": bundle.sha256,
                "Cache-Control": "no-store",
            },
        )

    @router.post("/deployments/linux/host-key", response_model=SSHHostKey, tags=["deployment"])
    def linux_host_key(body: SSHHost):
        return probe_host_key(body)

    @router.post("/deployments/linux/inspect", response_model=LinuxInspection, tags=["deployment"])
    def inspect_linux(body: SSHAccess):
        return linux_deployments.inspect(body)

    @router.post(
        "/deployments/linux",
        response_model=LinuxDeploymentJob,
        status_code=202,
        tags=["deployment"],
    )
    def deploy_linux(body: LinuxDeploymentRequest):
        return linux_deployments.submit(body)

    @router.get(
        "/deployments/linux/{command_id}", response_model=LinuxDeploymentJob, tags=["deployment"]
    )
    def linux_deployment(command_id: str):
        return linux_deployments.get(command_id)

    @router.delete(
        "/deployments/linux/{command_id}/credential",
        response_model=LinuxDeploymentJob,
        tags=["deployment"],
    )
    def revoke_linux_credential(command_id: str):
        return linux_deployments.revoke(command_id)

    @router.post("/nodes", response_model=NodeSnapshot, status_code=201, tags=["nodes"])
    def register_node(body: NodeRegister, request: Request):
        return coordinator.register_node(
            body.model_dump(mode="json"), peer_ip=_device_peer(request)
        )

    @router.post(
        "/nodes/access/web", response_model=WebAccessSnapshot, status_code=201, tags=["nodes"]
    )
    def open_web_access(body: WebAccessRequest, request: Request):
        return coordinator.devices.open_web(
            body.model_dump(mode="json"), peer_ip=_device_peer(request)
        )

    @router.post(
        "/nodes/{node_id}/access/{access_id}/heartbeat",
        response_model=WebAccessSnapshot,
        tags=["nodes"],
    )
    def heartbeat_web_access(
        node_id: str, access_id: str, body: WebAccessHeartbeat, request: Request
    ):
        return coordinator.devices.heartbeat(
            node_id, access_id, body.model_dump(mode="json"), peer_ip=_device_peer(request)
        )

    @router.delete(
        "/nodes/{node_id}/access/{access_id}", response_model=NodeSnapshot, tags=["nodes"]
    )
    def release_web_access(node_id: str, access_id: str):
        return coordinator.devices.release(node_id, access_id)

    @router.get("/nodes", response_model=list[NodeSnapshot], tags=["nodes"])
    def list_nodes():
        return coordinator.list_nodes()

    @router.get("/nodes/{node_id}", response_model=NodeSnapshot, tags=["nodes"])
    def get_node(node_id: str):
        return coordinator.get_node(node_id)

    @router.post("/nodes/{node_id}/heartbeat", response_model=NodeSnapshot, tags=["nodes"])
    def heartbeat(node_id: str, body: NodeHeartbeat, request: Request):
        return coordinator.heartbeat(
            node_id, body.model_dump(mode="json"), peer_ip=_device_peer(request)
        )

    @router.post("/nodes/{node_id}/observations", status_code=201, tags=["perception"])
    def observe(node_id: str, body: ObservationCreate):
        return coordinator.observe(node_id, body.model_dump(mode="json"))

    @router.post(
        "/nodes/{node_id}/attention", response_model=AttentionSnapshot, tags=["perception"]
    )
    def observe_attention(node_id: str, body: AttentionObservation):
        return proactivity.observe(node_id, body.model_dump(mode="json"))

    @router.get("/nodes/{node_id}/attention", response_model=AttentionSnapshot, tags=["perception"])
    def get_attention(node_id: str):
        return proactivity.get_attention(node_id)

    @router.get("/nodes/{node_id}/focus", response_model=FocusSnapshot, tags=["perception"])
    def get_focus(node_id: str):
        return proactivity.focus.get(node_id)

    @router.post(
        "/nodes/{node_id}/screen/sessions",
        response_model=ScreenSessionReceipt,
        status_code=201,
        tags=["screen_vision"],
    )
    def create_screen_session(node_id: str, body: ScreenSessionCreate):
        return screen_vision.create_session(node_id, body.model_dump(mode="json"))

    @router.get(
        "/nodes/{node_id}/screen/sessions/{session_id}",
        response_model=ScreenSessionSnapshot,
        tags=["screen_vision"],
    )
    def get_screen_session(node_id: str, session_id: str):
        return screen_vision.get_session(node_id, session_id)

    @router.post(
        "/nodes/{node_id}/screen/sessions/{session_id}/stop",
        response_model=ScreenSessionReceipt,
        tags=["screen_vision"],
    )
    def stop_screen_session(node_id: str, session_id: str, body: ScreenCommand):
        return screen_vision.stop_session(node_id, session_id, body.model_dump(mode="json"))

    @router.post(
        "/nodes/{node_id}/screen/sessions/{session_id}/purge",
        response_model=ScreenPurgeReceipt,
        status_code=202,
        tags=["screen_vision"],
    )
    def purge_screen_session(node_id: str, session_id: str, body: ScreenCommand):
        return screen_vision.purge_session(node_id, session_id, body.model_dump(mode="json"))

    @router.post(
        "/nodes/{node_id}/screen/uploads",
        response_model=ScreenUploadReceipt,
        status_code=201,
        tags=["screen_vision"],
    )
    def register_screen_upload(node_id: str, body: ScreenUploadRegister):
        return screen_vision.register_upload(node_id, body.model_dump(mode="json"))

    @router.put(
        "/nodes/{node_id}/screen/uploads/{upload_id}/content",
        response_model=ScreenUploadReceipt,
        tags=["screen_vision"],
    )
    async def upload_screen_content(node_id: str, upload_id: str, request: Request):
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and content_type not in ("image/png", "image/jpeg"):
            raise DomainError(
                415, "screen_image_type_unsupported", "Only PNG and JPEG uploads are accepted"
            )
        plan = await asyncio.to_thread(screen_vision.prepare_upload_write, node_id, upload_id)
        if plan["mode"] == "ready":
            return plan["receipt"]
        tmp_path = Path(plan["tmp_path"])
        max_bytes = plan["max_bytes"]
        writer_token = plan["writer_token"]
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp_path, "wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise DomainError(
                            413, "screen_image_too_large", "Image bytes exceed the server limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise DomainError(422, "screen_image_invalid", "Uploaded content is empty")
        except BaseException:
            await asyncio.to_thread(screen_vision.abort_upload, node_id, upload_id, writer_token)
            raise
        return await asyncio.to_thread(
            screen_vision.commit_upload,
            node_id,
            upload_id,
            writer_token=writer_token,
            byte_length=size,
            sha256=digest.hexdigest(),
        )

    @router.post(
        "/nodes/{node_id}/screen/frames",
        response_model=ScreenFrameReceipt,
        status_code=202,
        tags=["screen_vision"],
    )
    def submit_screen_frame(node_id: str, body: ScreenFrameSubmit):
        return screen_vision.submit_frame(node_id, body.model_dump(mode="json"))

    @router.get(
        "/nodes/{node_id}/screen/frames/{frame_id}",
        response_model=ScreenFrameSnapshot,
        tags=["screen_vision"],
    )
    def get_screen_frame(node_id: str, frame_id: str):
        return screen_vision.get_frame(node_id, frame_id)

    @router.get(
        "/nodes/{node_id}/screen/analyses/{analysis_id}",
        response_model=ScreenAnalysis,
        tags=["screen_vision"],
    )
    def get_screen_analysis(node_id: str, analysis_id: str):
        return screen_vision.get_analysis(node_id, analysis_id)

    @router.get(
        "/nodes/{node_id}/screen/state",
        response_model=ScreenState,
        tags=["screen_vision"],
    )
    def get_screen_state(
        node_id: str,
        companion_id: Identifier,
        display_id: Identifier = "default",
    ):
        return screen_vision.get_state(node_id, companion_id, display_id)

    @router.post(
        "/nodes/{node_id}/presentations",
        response_model=PresentationSnapshot,
        status_code=201,
        tags=["perception"],
    )
    def open_presentation(node_id: str, body: PresentationOpen):
        return presentations.open(node_id, body.model_dump(mode="json"))

    @router.post(
        "/nodes/{node_id}/presentations/{presentation_id}/heartbeat",
        response_model=PresentationSnapshot,
        tags=["perception"],
    )
    def heartbeat_presentation(node_id: str, presentation_id: str):
        return presentations.heartbeat(node_id, presentation_id)

    @router.delete(
        "/nodes/{node_id}/presentations/{presentation_id}",
        response_model=PresentationSnapshot,
        tags=["perception"],
    )
    def release_presentation(node_id: str, presentation_id: str):
        return presentations.release(node_id, presentation_id)

    @router.get(
        "/companions/{companion_id}/proactivity",
        response_model=ProactivityPolicySnapshot,
        tags=["proactivity"],
    )
    def get_proactivity_policy(companion_id: str):
        return proactivity.get_policy(companion_id)

    @router.post(
        "/companions/{companion_id}/proactivity",
        response_model=ProactivityPolicySnapshot,
        tags=["proactivity"],
    )
    def update_proactivity_policy(companion_id: str, body: ProactivityPolicyUpdate):
        return proactivity.update_policy(companion_id, body.model_dump(mode="json"))

    @router.get(
        "/companions/{companion_id}/proactivity/decisions",
        response_model=ProactivityDecisionPage,
        tags=["proactivity"],
    )
    def get_proactivity_decisions(
        companion_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        latest: bool = False,
    ):
        return proactivity.decisions(companion_id, after_sequence, limit, latest=latest)

    @router.get(
        "/companions/{companion_id}/proactivity/schedule",
        tags=["proactivity"],
    )
    def get_proactivity_schedule(companion_id: str):
        return proactivity.snapshot_schedule(companion_id)

    @router.get(
        "/companions/{companion_id}/scheduled-tasks",
        tags=["tools"],
    )
    def get_scheduled_tasks(
        companion_id: str,
        status: TaskStatusFilter | None = None,
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ):
        return proactivity.list_tasks(companion_id, status=status, limit=limit, offset=offset)

    @router.delete(
        "/companions/{companion_id}/scheduled-tasks/{task_id}",
        tags=["tools"],
    )
    def cancel_scheduled_task(companion_id: str, task_id: str):
        return proactivity.cancel_task(companion_id, task_id)

    @router.get("/companions/{companion_id}/tools", tags=["tools"])
    def get_tools(
        companion_id: str,
        purpose: Literal["dialogue", "proactive"] = "dialogue",
    ):
        coordinator.get_companion(companion_id)
        return {
            "companion_id": companion_id,
            "enabled": tool_runtime is not None,
            "purpose": purpose,
            "tools": list(tool_runtime.snapshot(purpose)) if tool_runtime is not None else [],
            "max_calls_per_turn": settings.tools_max_calls_per_turn,
            "delegation_enabled": settings.delegation.enabled,
        }

    @router.get("/companions/{companion_id}/delegations", tags=["tools"])
    def list_delegations(
        companion_id: str,
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ):
        return delegation.list(companion_id, limit=limit, offset=offset)

    @router.get(
        "/companions/{companion_id}/proactivity/output",
        response_model=ProactivityOutputPage,
        tags=["proactivity"],
    )
    def poll_proactivity_output(
        companion_id: str,
        response: Response,
        node_id: Identifier,
        access_id: Identifier,
        epoch: int = Query(ge=0, le=2**63 - 1),
        after_sequence: int | None = Query(default=None, ge=0, le=2**63 - 1),
    ):
        response.headers["Cache-Control"] = "no-store"
        return proactivity_output.poll(companion_id, node_id, access_id, epoch, after_sequence)

    @router.get(
        "/companions/{companion_id}/proactivity/native-output",
        response_model=NativeOutputPage,
        tags=["proactivity"],
    )
    def poll_native_output(
        companion_id: str,
        response: Response,
        node_id: Identifier,
        presentation_id: Identifier,
        epoch: int = Query(ge=0, le=2**63 - 1),
        after_sequence: int | None = Query(default=None, ge=0, le=2**63 - 1),
    ):
        response.headers["Cache-Control"] = "no-store"
        return proactivity_output.poll_native(
            companion_id, node_id, presentation_id, epoch, after_sequence
        )

    @router.get("/graph", response_model=GraphSnapshot, tags=["nodes"])
    def graph():
        return coordinator.graph()

    @router.post(
        "/companions", response_model=CompanionSnapshot, status_code=201, tags=["companions"]
    )
    def create_companion(body: CompanionCreate):
        return coordinator.create_companion(body.model_dump(mode="json"))

    @router.get("/companions", response_model=list[CompanionSnapshot], tags=["companions"])
    def list_companions():
        return coordinator.list_companions()

    @router.get("/companions/{companion_id}", response_model=CompanionSnapshot, tags=["companions"])
    def get_companion(companion_id: str):
        return coordinator.get_companion(companion_id)

    @router.post(
        "/companions/{companion_id}/context", response_model=CompanionSnapshot, tags=["companions"]
    )
    def update_context(companion_id: str, body: ContextUpdate):
        return coordinator.patch_context(
            companion_id, body.model_dump(mode="json", exclude_unset=True)
        )

    @router.get(
        "/companions/{companion_id}/personality",
        response_model=PersonalitySnapshot,
        tags=["personality"],
    )
    def get_personality(companion_id: str):
        return personality.get(companion_id)

    @router.post(
        "/companions/{companion_id}/personality",
        response_model=PersonalitySnapshot,
        tags=["personality"],
    )
    def update_personality(companion_id: str, body: PersonalityUpdate):
        return personality.update(companion_id, body.model_dump(mode="json"))

    @router.post(
        "/companions/{companion_id}/personality/import",
        response_model=PersonalitySnapshot,
        tags=["personality"],
    )
    def import_personality(companion_id: str, body: PersonalityCardImport):
        return personality.import_card(companion_id, body.model_dump(mode="json"))

    @router.get(
        "/companions/{companion_id}/personality/state",
        response_model=PersonalityStateSnapshot,
        tags=["personality"],
    )
    def get_personality_state(companion_id: str):
        return personality.get_state(companion_id)

    @router.post(
        "/companions/{companion_id}/personality/state",
        response_model=PersonalityStateSnapshot,
        tags=["personality"],
    )
    def update_personality_state(companion_id: str, body: PersonalityStateUpdate):
        return personality.update_state(companion_id, body.model_dump(mode="json"))

    @router.get(
        "/companions/{companion_id}/personality/history",
        response_model=PersonalityHistoryPage,
        tags=["personality"],
    )
    def get_personality_history(
        companion_id: str,
        before_revision: int | None = Query(default=None, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        return personality.history(companion_id, before_revision=before_revision, limit=limit)

    @router.get(
        "/companions/{companion_id}/inner-life",
        response_model=InnerLifeSnapshot,
        tags=["personality"],
    )
    def get_inner_life(companion_id: str):
        return inner_life.get(companion_id)

    @router.get(
        "/companions/{companion_id}/inner-life/activities",
        response_model=InnerActivityPage,
        tags=["personality"],
    )
    def get_inner_life_activities(
        companion_id: str,
        after_sequence: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return inner_life.activities(companion_id, after_sequence, limit)

    @router.get(
        "/companions/{companion_id}/slow-variables",
        response_model=SlowVariableSnapshot,
        tags=["personality"],
    )
    def get_slow_variables(companion_id: str):
        return slow_variables.get(companion_id)

    @router.get(
        "/companions/{companion_id}/personality/proposals",
        response_model=PersonalityEvolutionPage,
        tags=["personality"],
    )
    def list_personality_proposals(
        companion_id: str,
        status: str | None = Query(default=None),
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        return personality_evolution.list(
            companion_id, status=status, after_sequence=after_sequence, limit=limit
        )

    @router.post(
        "/companions/{companion_id}/personality/proposals/refresh",
        response_model=PersonalityEvolutionRefreshResult,
        tags=["personality"],
    )
    def refresh_personality_proposals(companion_id: str, body: PersonalityEvolutionRefresh):
        return personality_evolution.refresh(companion_id, body.model_dump(mode="json"))

    @router.get(
        "/companions/{companion_id}/personality/proposals/{proposal_id}",
        response_model=PersonalityEvolutionProposal,
        tags=["personality"],
    )
    def get_personality_proposal(companion_id: str, proposal_id: str):
        return personality_evolution.get(companion_id, proposal_id)

    @router.post(
        "/companions/{companion_id}/personality/proposals/{proposal_id}/accept",
        response_model=PersonalityEvolutionDecisionResult,
        tags=["personality"],
    )
    def accept_personality_proposal(
        companion_id: str, proposal_id: str, body: PersonalityEvolutionDecision
    ):
        return personality_evolution.accept(companion_id, proposal_id, body.model_dump(mode="json"))

    @router.post(
        "/companions/{companion_id}/personality/proposals/{proposal_id}/reject",
        response_model=PersonalityEvolutionDecisionResult,
        tags=["personality"],
    )
    def reject_personality_proposal(
        companion_id: str, proposal_id: str, body: PersonalityEvolutionRejection
    ):
        return personality_evolution.reject(companion_id, proposal_id, body.model_dump(mode="json"))

    @router.get(
        "/companions/{companion_id}/emotion", response_model=EmotionSnapshot, tags=["emotion"]
    )
    def get_emotion(companion_id: str):
        return emotion.get(companion_id)

    @router.get(
        "/companions/{companion_id}/emotion/experiences",
        response_model=EmotionExperiencePage,
        tags=["emotion"],
    )
    def emotion_experiences(
        companion_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return emotion.experiences(companion_id, after_sequence=after_sequence, limit=limit)

    @router.post(
        "/companions/{companion_id}/emotion/experiences/{event_id}/invalidate",
        response_model=EmotionSnapshot,
        tags=["emotion"],
    )
    def invalidate_emotion(companion_id: str, event_id: str, body: EmotionInvalidation):
        return emotion.invalidate(companion_id, event_id, body.model_dump(mode="json"))

    @router.post(
        "/companions/{companion_id}/emotion/experiences/{event_id}/correct",
        response_model=EmotionSnapshot,
        tags=["emotion"],
    )
    def correct_emotion(companion_id: str, event_id: str, body: EmotionCorrection):
        return emotion.correct(
            companion_id, event_id, body.model_dump(mode="json", exclude_none=True)
        )

    @router.post(
        "/companions/{companion_id}/personality/preview",
        response_model=PersonalityPreviewResult,
        tags=["personality"],
    )
    def preview_personality(companion_id: str, body: PersonalityPreview):
        profile = personality.get(companion_id)
        state = personality.get_state(companion_id)
        state["emotion"] = emotion.get(companion_id)
        companion = coordinator.get_companion(companion_id)
        node_id = companion["active_node_id"]
        node = coordinator.get_node(node_id) if node_id is not None else None
        # A migration between these reads makes this suggestion conservative.
        # Preview never grants output ownership or extends a lease.
        current = coordinator.get_companion(companion_id)
        now = coordinator.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        lease = current["lease_expires_at"]
        output_permitted = bool(
            node is not None
            and current["active_node_id"] == node_id
            and current["epoch"] == companion["epoch"]
            and current["epoch"] > 0
            and node["online"]
            and OUTPUT_CAPABILITIES.intersection(node["capabilities"])
            and lease is not None
            and datetime.fromisoformat(lease) > now
        )
        host_allows_audio = bool(node is not None and "audio.output" in node["capabilities"])
        return personality_prompts.preview(
            current,
            profile,
            state,
            body.model_dump(mode="json"),
            output_permitted=output_permitted,
            host_allows_audio=host_allows_audio,
            slow_variables=slow_variables.get(companion_id),
            inner_activity=inner_life.current(companion_id),
        )

    @router.post(
        "/companions/{companion_id}/dialogue", response_model=DialogueResult, tags=["dialogue"]
    )
    def send_dialogue(companion_id: str, body: DialogueRequest):
        return dialogue.send(companion_id, body.model_dump(mode="json"))

    @router.post(
        "/companions/{companion_id}/dialogue/stream",
        tags=["dialogue"],
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "SSE status, snapshot and provisional cumulative-text delta events, followed "
                    "by one durable DialogueResult in result, or a structured error event. "
                    "Both dialogue transports share command IDs and receipt recovery."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
        response_model=None,
    )
    def stream_dialogue(companion_id: str, body: DialogueRequest):
        return stream_response(
            dialogue, companion_id, body.model_dump(mode="json"), dialogue_workers
        )

    @router.get(
        "/companions/{companion_id}/dialogue/history",
        response_model=DialogueHistoryPage,
        tags=["dialogue"],
    )
    def dialogue_history(
        companion_id: str,
        before_sequence: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return dialogue.history(companion_id, before_sequence, limit)

    @router.get(
        "/companions/{companion_id}/dialogue/{command_id}",
        response_model=DialogueResult,
        tags=["dialogue"],
    )
    def get_dialogue_result(companion_id: str, command_id: str):
        return dialogue.get_result(companion_id, command_id)

    @router.post(
        "/companions/{companion_id}/dialogue/{command_id}/commit",
        response_model=DialogueResult,
        tags=["dialogue"],
    )
    def commit_dialogue(companion_id: str, command_id: str, body: DialogueCommitRequest):
        """Commit an externally generated reply to a turn parked by the runtime.

        `generator: "external"` on the dialogue route returns the assembled
        prompt without calling a model; this route writes the reply back under
        the same context, reference, emotion, and memory gates.
        """
        return dialogue.commit_turn(companion_id, command_id, body.model_dump(mode="json"))

    @router.post(
        "/companions/{companion_id}/transfers",
        response_model=TransferSnapshot,
        status_code=201,
        tags=["travel"],
    )
    def prepare_transfer(companion_id: str, body: TransferPrepare):
        return coordinator.prepare_transfer(companion_id, body.model_dump(mode="json"))

    @router.get("/transfers/{transfer_id}", response_model=TransferSnapshot, tags=["travel"])
    def get_transfer(transfer_id: str):
        return coordinator.get_transfer(transfer_id)

    @router.post("/transfers/{transfer_id}/ack", response_model=TransferSnapshot, tags=["travel"])
    def acknowledge(transfer_id: str, body: TransferAck):
        return coordinator.ack_transfer(transfer_id, body.model_dump(mode="json"))

    @router.post("/transfers/{transfer_id}/abort", response_model=TransferSnapshot, tags=["travel"])
    def abort_transfer(transfer_id: str):
        return coordinator.abort_transfer(transfer_id)

    @router.post(
        "/companions/{companion_id}/follow",
        response_model=CompanionSnapshot,
        tags=["travel"],
    )
    def follow_room(companion_id: str, body: RoomFollow):
        """Move the lifeform to the room that just reported activity.

        Single-party authoritative migration: no target handshake is required.
        The abandoned room becomes `room_state: "empty"` but keeps its tools.
        """
        return coordinator.follow_room(companion_id, body.model_dump(mode="json"))

    @router.post("/companions/{companion_id}/lease", response_model=OutputLease, tags=["travel"])
    def renew_lease(companion_id: str, body: LeaseRenew):
        return coordinator.renew_lease(companion_id, body.model_dump(mode="json"))

    @router.get("/events", response_model=EventPage, tags=["events"])
    def events(
        after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        node_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        if node_id is not None:
            coordinator.get_node(node_id)
        return coordinator.events(after_sequence=after_sequence, node_id=node_id, limit=limit)

    @router.get("/flow/logs", response_model=FlowLogPage, tags=["flow"])
    def flow_logs(
        after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ):
        """Tail the in-memory live log ring for the WebUI (AstrBot-style)."""
        return get_flow_log_buffer().read(after_sequence=after_sequence, limit=limit)

    @router.post(
        "/companions/{companion_id}/memories",
        response_model=MemoryRecord,
        status_code=201,
        tags=["memory"],
    )
    def write_memory(companion_id: str, body: MemoryWrite):
        coordinator.get_companion(companion_id)
        return memory.write(companion_id, body.model_dump(mode="json", exclude_unset=True))

    @router.get("/companions/{companion_id}/memories/automation", tags=["memory"])
    def memory_automation_status(companion_id: str) -> dict:
        coordinator.get_companion(companion_id)
        return automatic_memory.status(companion_id)

    @router.get(
        "/companions/{companion_id}/memories/logs",
        response_model=MemoryLogPage,
        tags=["memory"],
        description="Read scoped, redacted memory diagnostics using a sequence cursor.",
    )
    def memory_logs(
        companion_id: str,
        after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        operation: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        status: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        command_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ):
        coordinator.get_companion(companion_id)
        try:
            return memory.logs.list(
                companion_id,
                after_sequence=after_sequence,
                limit=limit,
                operation=operation,
                status=status,
                command_id=command_id,
            )
        except ValueError as exc:
            raise MemoryError(422, "validation_error", str(exc)) from exc

    @router.get("/companions/{companion_id}/memories/automation/jobs", tags=["memory"])
    def memory_automation_jobs(
        companion_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        coordinator.get_companion(companion_id)
        return automatic_memory.list_jobs(companion_id, limit=limit, offset=offset)

    @router.post("/companions/{companion_id}/memories/automation/run", tags=["memory"])
    def run_memory_automation(companion_id: str, body: MemoryAutomationRun) -> dict:
        coordinator.get_companion(companion_id)
        return automatic_memory.process_next(companion_id, force=body.force) or {"status": "idle"}

    @router.get("/companions/{companion_id}/memories/summaries", tags=["memory"])
    def memory_summaries(
        companion_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        coordinator.get_companion(companion_id)
        return automatic_memory.summaries(companion_id, limit=limit, offset=offset)

    @router.get(
        "/companions/{companion_id}/memories/knowledge-graph",
        response_model=KnowledgeGraphSnapshot,
        tags=["memory"],
    )
    def memory_knowledge_graph(
        companion_id: str,
        query: Annotated[str, Query(max_length=2048)] = "",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        entity_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        hops: Annotated[int, Query(ge=1, le=3)] = 1,
    ) -> dict:
        coordinator.get_companion(companion_id)
        return automatic_memory.graph(
            companion_id, query=query, limit=limit, entity_id=entity_id, hops=hops
        )

    @router.put(
        "/companions/{companion_id}/memories/knowledge-graph/entities/{entity_id}/aliases",
        response_model=KnowledgeGraphEntity,
        tags=["memory"],
    )
    def update_graph_aliases(companion_id: str, entity_id: str, body: GraphAliasUpdate):
        coordinator.get_companion(companion_id)
        return automatic_memory.update_entity_aliases(
            companion_id, entity_id, body.model_dump(mode="json")
        )

    @router.get("/companions/{companion_id}/memories/embeddings", tags=["memory"])
    def memory_embedding_status(companion_id: str) -> dict:
        coordinator.get_companion(companion_id)
        return embeddings.status(companion_id)

    @router.get("/companions/{companion_id}/memories/embeddings/jobs", tags=["memory"])
    def memory_embedding_jobs(
        companion_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        coordinator.get_companion(companion_id)
        return embeddings.jobs(companion_id, limit=limit, offset=offset)

    @router.post("/companions/{companion_id}/memories/embeddings/run", tags=["memory"])
    def run_memory_embeddings(companion_id: str, body: MemoryEmbeddingRun) -> dict:
        coordinator.get_companion(companion_id)
        return embeddings.run(companion_id, limit=body.limit)

    @router.post(
        "/companions/{companion_id}/memories/{memory_id}/corrections",
        response_model=MemoryRecord,
        status_code=201,
        tags=["memory"],
    )
    def correct_memory(companion_id: str, memory_id: str, body: MemoryCorrection):
        coordinator.get_companion(companion_id)
        return memory.correct(
            companion_id, memory_id, body.model_dump(mode="json", exclude_unset=True)
        )

    @router.patch(
        "/companions/{companion_id}/memories/{memory_id}",
        response_model=MemoryRecord,
        tags=["memory"],
    )
    def update_memory_metadata(companion_id: str, memory_id: str, body: MemoryMetadataUpdate):
        coordinator.get_companion(companion_id)
        return memory.update_metadata(
            companion_id, memory_id, body.model_dump(mode="json", exclude_unset=True)
        )

    @router.get("/companions/{companion_id}/memories", tags=["memory"])
    def list_memories(
        companion_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        coordinator.get_companion(companion_id)
        return memory.list(companion_id, limit=limit, offset=offset)

    @router.post(
        "/companions/{companion_id}/memories/recall", response_model=RecallResult, tags=["memory"]
    )
    def recall_memory(companion_id: str, body: MemoryRecall):
        coordinator.get_companion(companion_id)
        return memory.recall(companion_id, body.model_dump(mode="json", exclude_unset=True))

    @router.post(
        "/companions/{companion_id}/memories/semantic-recall",
        response_model=RecallResult,
        tags=["memory"],
        description=(
            "May call the embedding provider once; local embeddings skip the paid quota. "
            "An enabled reranker may reorder the fused candidate head and fails open."
        ),
    )
    def semantic_recall_memory(companion_id: str, body: MemoryRecall):
        coordinator.get_companion(companion_id)
        return embeddings.semantic_recall(
            companion_id, body.model_dump(mode="json", exclude_unset=True)
        )

    @router.get("/companions/{companion_id}/memories/stats", tags=["memory"])
    def memory_stats(companion_id: str):
        coordinator.get_companion(companion_id)
        return memory.stats(companion_id)

    @router.get("/companions/{companion_id}/memories/consolidation-plan", tags=["memory"])
    def consolidation_plan(companion_id: str):
        coordinator.get_companion(companion_id)
        return memory.plan_consolidation(companion_id)

    @router.delete("/companions/{companion_id}/memories/{memory_id}", tags=["memory"])
    def delete_memory(companion_id: str, memory_id: str):
        coordinator.get_companion(companion_id)
        return memory.delete(companion_id, memory_id)

    @router.delete("/companions/{companion_id}/memory", tags=["memory"])
    def erase_companion_memory(companion_id: str, command_id: str):
        coordinator.get_companion(companion_id)
        return memory.erase_companion(companion_id, command_id)

    @router.get("/memory/policy", tags=["memory"])
    def memory_policy():
        return memory.policy_snapshot()

    app.include_router(router)

    @app.websocket("/v1/nodes/{node_id}/events")
    async def node_events(websocket: WebSocket, node_id: str):
        await websocket.accept()

        async def deny(code: int, event_type: str, payload: dict) -> None:
            # Auth-stage rejections become durable connection log events.
            await asyncio.to_thread(
                coordinator.connection_event, event_type, payload, node_id=node_id
            )
            await websocket.close(code=code)

        try:
            first = await asyncio.wait_for(
                websocket.receive_json(), timeout=settings.websocket_auth_timeout_seconds
            )
            if not isinstance(first, dict) or first.get("type") != "auth":
                await deny(4401, "node.auth_failed", {"code": 4401, "stage": "auth"})
                return
            token = first.get("token")
            cursor = first.get("after_sequence", 0)
            if not isinstance(token, str) or not token_matches(token):
                await deny(4401, "node.auth_failed", {"code": 4401, "stage": "auth"})
                return
            if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
                await deny(4400, "node.auth_failed", {"code": 4400, "stage": "auth"})
                return
            await asyncio.to_thread(coordinator.get_node, node_id)
        except DomainError:
            await deny(4404, "node.auth_failed", {"code": 4404, "stage": "auth"})
            return
        except (TimeoutError, ValueError):
            await deny(4401, "node.auth_failed", {"code": 4401, "stage": "auth"})
            return
        except WebSocketDisconnect:
            return

        send_lock = asyncio.Lock()

        async def send(frame: dict) -> None:
            async with send_lock:
                await websocket.send_json(frame)

        await send({"type": "welcome", "protocol_version": "1.0", "node_id": node_id})

        async def replay() -> None:
            nonlocal cursor
            while True:
                page = await asyncio.to_thread(
                    coordinator.events, after_sequence=cursor, node_id=node_id, limit=100
                )
                for event in page["events"]:
                    await send({"type": "event", "event": event})
                next_cursor = page["next_sequence"]
                if next_cursor > cursor:
                    cursor = next_cursor
                    await send({"type": "cursor", "sequence": cursor})
                else:
                    await asyncio.sleep(settings.event_poll_seconds)

        async def receive() -> None:
            while True:
                frame = await websocket.receive_json()
                if isinstance(frame, dict) and frame.get("type") == "ping":
                    await send({"type": "pong"})
                else:
                    await send({"type": "error", "code": "unsupported_frame"})

        tasks = [asyncio.create_task(replay()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # The peer is gone or the stream broke. Audit the disconnect inline,
            # before yielding control again: session teardown cancels this coroutine
            # on the next await and a detached audit task could be dropped before
            # its write commits. The audit is one row in a single transaction, so it
            # does not hold the loop for long.
            coordinator.connection_event("node.disconnected", {}, node_id=node_id)
            for task in done:
                try:
                    task.result()
                except WebSocketDisconnect:
                    pass
                except Exception:
                    logger.exception("Node event stream failed")
                    await websocket.close(code=1011)
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                # Session teardown may cancel this coroutine while the stream tasks
                # are still winding down. They are cancelled above, so the
                # cancellation is the teardown signal, not a stream failure; swallow
                # it so the connection closes cleanly.
                pass

    return app
