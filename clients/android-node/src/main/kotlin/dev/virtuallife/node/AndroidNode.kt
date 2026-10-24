package dev.virtuallife.node

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.random.Random
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.decodeFromJsonElement

data class NodeSessionConfig(
    val nodeId: String,
    val label: String,
    val heartbeatMillis: Long = 15_000,
    val leasePollMillis: Long = 100,
    val leaseRetryMillis: Long = 1_000,
    val pingMillis: Long = 10_000,
    val pongTimeoutMillis: Long = 5_000,
    val welcomeTimeoutMillis: Long = 5_000,
    val reconnectInitialMillis: Long = 250,
    val reconnectMaxMillis: Long = 15_000,
) {
    init {
        require(nodeId.isNotBlank() && nodeId.length <= 128 && label.isNotBlank())
        require(listOf(heartbeatMillis, leasePollMillis, leaseRetryMillis, pingMillis, pongTimeoutMillis,
            welcomeTimeoutMillis, reconnectInitialMillis, reconnectMaxMillis).all { it > 0 })
        require(reconnectMaxMillis >= reconnectInitialMillis)
    }
}

/**
 * A host-owned coroutine runs the session. Cancellation closes the socket and awaits
 * output cleanup. Native overlay, permission prompts and service lifecycle stay in host.
 */
class AndroidNode(
    config: NodeSessionConfig,
    api: NodeApi,
    private val events: NodeEventConnector,
    bridge: AndroidNodeBridge,
    cursorStore: CursorStore,
    clock: MonotonicClock,
    val gate: LeaseGate = LeaseGate(config.nodeId, clock),
) {
    private val config = config
    private val bridge = bridge
    private val controller = MigrationController(config, api, bridge, clock, gate)
    private val router = ReplayEventRouter(config.nodeId, cursorStore, controller::onEvent)
    private val running = AtomicBoolean(false)
    val companions: StateFlow<Map<String, CompanionSnapshot>> = controller.companions

    init { require(gate.nodeId == config.nodeId) { "Gate and session must use the same node" } }

    suspend fun savedCursor(): Long = router.cursor()

    suspend fun run(): Unit = coroutineScope {
        check(running.compareAndSet(false, true)) { "This node session is already running" }
        // This watchdog never waits on the network/transition mutex. Slow HTTP calls
        // or render callbacks cannot prevent it from revoking a lease and stopping audio.
        val watchdog = launch {
            while (isActive) {
                controller.enforceDeadlines()
                delay(config.leasePollMillis)
            }
        }
        val leaseRenewal = launch {
            while (isActive) {
                controller.maintainLeases()
                delay(config.leasePollMillis)
            }
        }
        val heartbeat = launch {
            while (isActive) {
                delay(config.heartbeatMillis)
                try {
                    controller.heartbeat()
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Exception) {
                    controller.failClosed(StopReason.CONNECTION_LOST)
                    report(error)
                }
            }
        }
        var backoff = config.reconnectInitialMillis
        try {
            while (currentCoroutineContext().isActive) {
                var connection: NodeEventConnection? = null
                try {
                    controller.register()
                    connection = events.connect(config.nodeId, router.cursor())
                    try {
                        withTimeout(config.welcomeTimeoutMillis) { connection.awaitWelcome() }
                    } catch (_: TimeoutCancellationException) {
                        throw NodeSocketException("Node event welcome timed out")
                    }
                    controller.connected()
                    backoff = config.reconnectInitialMillis
                    consume(connection)
                    throw NodeSocketException("Node event stream ended")
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Exception) {
                    controller.disconnected(StopReason.CONNECTION_LOST)
                    report(error)
                } finally {
                    connection?.close()
                }
                val jitter = Random.nextLong(0, maxOf(1, backoff / 4))
                delay(backoff + jitter)
                backoff = minOf(config.reconnectMaxMillis, backoff * 2)
            }
        } finally {
            withContext(NonCancellable) {
                heartbeat.cancelAndJoin()
                leaseRenewal.cancelAndJoin()
                watchdog.cancelAndJoin()
                try {
                    controller.disconnected(StopReason.SHUTDOWN)
                } finally {
                    running.set(false)
                }
            }
        }
    }

    private suspend fun consume(connection: NodeEventConnection) = coroutineScope {
        val pongs = Channel<Unit>(Channel.CONFLATED)
        val pinger = launch {
            while (isActive) {
                delay(config.pingMillis)
                pongs.tryReceive()
                if (!connection.ping()) throw NodeSocketException("Cannot send node ping")
                try {
                    withTimeout(config.pongTimeoutMillis) { pongs.receive() }
                } catch (_: TimeoutCancellationException) {
                    throw NodeSocketException("Node pong timed out")
                }
            }
        }
        try {
            for (frame in connection.frames) {
                when (frame) {
                    is NodeStreamFrame.Event -> router.route(frame.event)
                    is NodeStreamFrame.Cursor -> router.scanCursor(frame.sequence)
                    NodeStreamFrame.Pong -> pongs.trySend(Unit)
                }
            }
        } finally {
            pinger.cancelAndJoin()
            pongs.close()
        }
    }

    private suspend fun report(error: Throwable) {
        try { bridge.onFailure(error) } catch (cancelled: CancellationException) { throw cancelled } catch (_: Exception) { }
    }
}

/** Internal transitions are serialized, while lease expiry cleanup remains independent. */
internal class MigrationController(
    private val config: NodeSessionConfig,
    private val api: NodeApi,
    private val bridge: AndroidNodeBridge,
    private val clock: MonotonicClock,
    private val gate: LeaseGate,
) {
    private val transitions = Mutex()
    private val lifecycle = Any()
    private val pending = mutableMapOf<String, TransferSnapshot>()
    private val preparedEpoch = ConcurrentHashMap<String, Long>()
    private val mayOutput = ConcurrentHashMap<String, Long>()
    private val stopRequired = ConcurrentHashMap<String, StopReason>()
    private val stopLocks = ConcurrentHashMap<String, Mutex>()
    private val nextLeaseAttempt = mutableMapOf<String, Long>()
    private val state = MutableStateFlow<Map<String, CompanionSnapshot>>(emptyMap())
    val companions = state.asStateFlow()
    private var registeredCapabilities = emptySet<String>()
    @Volatile private var registered = false
    @Volatile private var socketConnected = false
    @Volatile private var outputsEnabled = false
    @Volatile private var outputCapable = false

    suspend fun register() = transitions.withLock {
        val capabilities = bridge.capabilities()
        api.registerNode(NodeRegistration(config.nodeId, config.label, capabilities = capabilities.sorted()))
        registeredCapabilities = capabilities
        outputCapable = capabilities.any { it in OUTPUT_CAPABILITIES }
        registered = true
        api.heartbeat(config.nodeId, NodeHeartbeat(bridge.heartbeatContext()))
    }

    suspend fun connected() = transitions.withLock {
        socketConnected = true
        outputsEnabled = outputCapable
        refreshAll()
    }

    suspend fun heartbeat() = transitions.withLock {
        if (!registered) return@withLock
        val capabilities = bridge.capabilities()
        outputCapable = capabilities.any { it in OUTPUT_CAPABILITIES }
        if (!outputCapable) failClosed(StopReason.HOST_CAPABILITY_LOST)
        if (capabilities != registeredCapabilities) {
            api.registerNode(NodeRegistration(config.nodeId, config.label, capabilities = capabilities.sorted()))
            registeredCapabilities = capabilities
        }
        api.heartbeat(config.nodeId, NodeHeartbeat(bridge.heartbeatContext()))
        if (!outputCapable) {
            failClosed(StopReason.HOST_CAPABILITY_LOST)
        } else if (socketConnected && !outputsEnabled) {
            outputsEnabled = true
            refreshAll()
        }
    }

    suspend fun onEvent(event: ProtocolEvent) = transitions.withLock {
        when (event.type) {
            "transfer.prepared" -> {
                val payload = NodeJson.decodeFromJsonElement<PreparedTransferPayload>(event.payload)
                require(event.companionId == payload.transfer.companionId)
                handlePrepared(payload)
            }
            "companion.transferred" -> {
                val payload = NodeJson.decodeFromJsonElement<TransferredPayload>(event.payload)
                require(event.companionId == payload.transfer.companionId && payload.companion.companionId == event.companionId)
                handleTerminal(payload.transfer.transferId)
            }
            "transfer.aborted", "transfer.expired" -> {
                val payload = NodeJson.decodeFromJsonElement<TransferPayload>(event.payload)
                require(event.companionId == payload.transfer.companionId)
                handleTerminal(payload.transfer.transferId)
            }
            "companion.created", "companion.context_updated" -> {
                val payload = NodeJson.decodeFromJsonElement<CompanionPayload>(event.payload)
                require(event.companionId == payload.companion.companionId)
                refreshOne(payload.companion.companionId)
            }
        }
        bridge.onEvent(event)
    }

    private suspend fun handlePrepared(payload: PreparedTransferPayload) {
        var transfer = api.getTransfer(payload.transfer.transferId)
        require(transfer.companionId == payload.transfer.companionId)
        // A stored prepared frame may describe a transfer that committed or aborted
        // while this host was offline. Never stop a newer owner on that old frame.
        if (transfer.status != TransferStatus.PREPARED) {
            settleTerminal(transfer)
            return
        }
        val expectedRole = when (config.nodeId) {
            transfer.sourceNodeId -> TransferRole.SOURCE
            transfer.targetNodeId -> TransferRole.TARGET
            else -> return
        }
        require(payload.role == expectedRole) { "Transfer recipient role mismatch" }
        val current = api.getCompanion(transfer.companionId)
        if (current.revision != transfer.baseRevision || current.epoch != transfer.snapshot.epoch ||
            current.activeNodeId != transfer.sourceNodeId
        ) {
            transfer = api.getTransfer(transfer.transferId)
            if (transfer.status != TransferStatus.PREPARED) {
                settleTerminal(transfer)
                return
            }
            throw NodeSocketException("Prepared transfer no longer matches authoritative state")
        }
        if (!gate.observe(current)) return
        state.value = state.value + (current.companionId to current)
        pending[transfer.companionId] = transfer
        gate.pause(transfer.companionId)
        when (expectedRole) {
            TransferRole.SOURCE -> {
                requireStopped(transfer.companionId, StopReason.TRANSFER_RELEASE)
                // Only after stopOutput completed successfully can released be sent.
                val latest = api.getTransfer(transfer.transferId)
                transfer = if (latest.status == TransferStatus.PREPARED) {
                    api.acknowledgeTransfer(transfer.transferId, TransferAck(config.nodeId, AckPhase.RELEASED))
                } else latest
            }
            TransferRole.TARGET -> {
                bridge.prepareRender(transfer.snapshot)
                preparedEpoch[transfer.companionId] = transfer.snapshot.epoch + 1
                // Rendering preparation creates no permit and emits no output.
                val latest = api.getTransfer(transfer.transferId)
                transfer = if (latest.status == TransferStatus.PREPARED) {
                    api.acknowledgeTransfer(transfer.transferId, TransferAck(config.nodeId, AckPhase.READY))
                } else latest
            }
        }
        if (transfer.status == TransferStatus.PREPARED) pending[transfer.companionId] = transfer else settleTerminal(transfer)
    }

    private suspend fun handleTerminal(transferId: String) {
        val latest = api.getTransfer(transferId)
        if (latest.status == TransferStatus.PREPARED) {
            throw NodeSocketException("Terminal event precedes terminal transfer state")
        }
        settleTerminal(latest)
    }

    private suspend fun settleTerminal(transfer: TransferSnapshot) {
        if (pending[transfer.companionId]?.transferId == transfer.transferId) pending.remove(transfer.companionId)
        if (!pending.containsKey(transfer.companionId)) gate.resume(transfer.companionId)
        refreshOne(transfer.companionId)
    }

    private suspend fun refreshAll() {
        for (snapshot in api.listCompanions()) {
            val transfer = pending[snapshot.companionId]
            if (transfer != null) {
                val latest = api.getTransfer(transfer.transferId)
                if (latest.status != TransferStatus.PREPARED) {
                    pending.remove(snapshot.companionId)
                    gate.resume(snapshot.companionId)
                }
            }
            adopt(snapshot)
        }
    }

    private suspend fun refreshOne(companionId: String) { adopt(api.getCompanion(companionId)) }

    private suspend fun adopt(snapshot: CompanionSnapshot) {
        if (!gate.observe(snapshot)) return
        state.value = state.value + (snapshot.companionId to snapshot)
        if (mayOutput[snapshot.companionId]?.let { it != snapshot.epoch } == true) {
            requireStopped(snapshot.companionId, StopReason.OWNERSHIP_CHANGED)
        }
        if (snapshot.activeNodeId != config.nodeId || !outputsEnabled || !outputCapable) {
            gate.revoke(snapshot.companionId)
            if (mayOutput.containsKey(snapshot.companionId) || preparedEpoch.containsKey(snapshot.companionId) ||
                stopRequired.containsKey(snapshot.companionId)
            ) requireStopped(snapshot.companionId, StopReason.OWNERSHIP_CHANGED)
            return
        }
        if (pending.containsKey(snapshot.companionId)) return
        gate.resume(snapshot.companionId)
        activate(snapshot, forceRenew = true)
    }

    private suspend fun activate(snapshot: CompanionSnapshot, forceRenew: Boolean) {
        val id = snapshot.companionId
        if (stopRequired.containsKey(id)) performStop(id)
        if (preparedEpoch[id] != snapshot.epoch) {
            bridge.prepareRender(snapshot)
            preparedEpoch[id] = snapshot.epoch
        }
        if (forceRenew || gate.permit(id, snapshot.epoch) == null) {
            val sample = try {
                api.renewLease(id, LeaseRenew(config.nodeId, snapshot.epoch))
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                leaseFailed(id, error)
                return
            }
            val accepted = synchronized(lifecycle) {
                outputsEnabled && outputCapable && !pending.containsKey(id) && !stopRequired.containsKey(id) && gate.grant(sample)
            }
            if (!accepted) {
                leaseFailed(id, NodeSocketException("Output lease was stale, expired, or locally suspended"))
                return
            }
            nextLeaseAttempt.remove(id)
        }
        // Expiry cleanup may have run while the renewal HTTP request was in flight.
        // A fresh lease does not recreate resources that stopOutput just disposed.
        if (preparedEpoch[id] != snapshot.epoch) {
            bridge.prepareRender(snapshot)
            preparedEpoch[id] = snapshot.epoch
        }
        val canRender = synchronized(lifecycle) {
            if (!outputsEnabled || stopRequired.containsKey(id) || gate.permit(id, snapshot.epoch) == null) {
                false
            } else {
            // Mark before invoking host code so the independent expiry watchdog can
            // interrupt long running playback initiated by renderState.
                mayOutput[id] = snapshot.epoch
                true
            }
        }
        if (!canRender) {
            requireStopped(id, StopReason.LEASE_EXPIRED)
            return
        }
        try {
            bridge.renderState(snapshot, OutputHandle(id, snapshot.epoch, gate))
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            requireStopped(id, StopReason.RENDER_FAILED)
            throw error
        }
    }

    private suspend fun leaseFailed(companionId: String, error: Throwable) {
        nextLeaseAttempt[companionId] = clock.nowNanos() + config.leaseRetryMillis * 1_000_000
        requireStopped(companionId, StopReason.LEASE_RENEWAL_FAILED)
        report(error)
    }

    suspend fun maintainLeases() = transitions.withLock {
        if (!registered || !outputsEnabled || !outputCapable) return@withLock
        for (snapshot in state.value.values) {
            val id = snapshot.companionId
            if (snapshot.activeNodeId != config.nodeId || pending.containsKey(id)) continue
            val now = clock.nowNanos()
            if ((nextLeaseAttempt[id] ?: Long.MIN_VALUE) > now) continue
            val permit = gate.permit(id, snapshot.epoch)
            if (permit == null || now >= permit.renewAtNanos) {
                try {
                    activate(snapshot, forceRenew = true)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (error: Exception) {
                    nextLeaseAttempt[id] = now + config.leaseRetryMillis * 1_000_000
                    markStop(id, StopReason.RENDER_FAILED)
                    report(error)
                }
            }
        }
    }

    /** Independent of transitions: expiry must stop already playing speech/audio. */
    suspend fun enforceDeadlines() {
        synchronized(lifecycle) {
            for ((id, epoch) in mayOutput) {
                if (gate.permit(id, epoch) == null) markStop(id, StopReason.LEASE_EXPIRED)
            }
        }
        for (id in stopRequired.entries.map { it.key }) {
            try { performStop(id) } catch (error: Exception) {
                currentCoroutineContext().ensureActive()
                report(error)
            }
        }
    }

    suspend fun failClosed(reason: StopReason) {
        synchronized(lifecycle) {
            outputsEnabled = false
            gate.revokeAll()
            // Use entrySet rather than ConcurrentHashMap's Java 8 KeySetView,
            // which is unavailable before Android API 24.
            for (id in mayOutput.entries.map { it.key } + preparedEpoch.entries.map { it.key }) markStop(id, reason)
        }
        for (id in stopRequired.entries.map { it.key }) {
            try { performStop(id) } catch (error: Exception) {
                currentCoroutineContext().ensureActive()
                report(error)
            }
        }
    }

    suspend fun disconnected(reason: StopReason) {
        socketConnected = false
        failClosed(reason)
    }

    private fun markStop(companionId: String, reason: StopReason) {
        synchronized(lifecycle) {
            gate.revoke(companionId)
            stopRequired.putIfAbsent(companionId, reason)
        }
    }

    private suspend fun requireStopped(companionId: String, reason: StopReason) {
        markStop(companionId, reason)
        performStop(companionId)
    }

    private suspend fun performStop(companionId: String) {
        val newMutex = Mutex()
        val mutex = stopLocks.putIfAbsent(companionId, newMutex) ?: newMutex
        mutex.withLock {
            val reason = stopRequired[companionId] ?: return@withLock
            bridge.stopOutput(companionId, reason)
            synchronized(lifecycle) {
                mayOutput.remove(companionId)
                preparedEpoch.remove(companionId)
                stopRequired.remove(companionId)
            }
        }
    }

    private suspend fun report(error: Throwable) {
        try { bridge.onFailure(error) } catch (_: Exception) { currentCoroutineContext().ensureActive() }
    }

    private companion object {
        val OUTPUT_CAPABILITIES = setOf("avatar.render", "text.display", "audio.output")
    }
}
