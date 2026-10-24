package dev.virtuallife.node

import java.io.File
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient

/** A runnable JVM console host. It creates no Android UI or cross-app integration. */
fun main() = runBlocking {
    val baseUrl = System.getenv("VIRTUAL_LIFE_URL") ?: "http://127.0.0.1:8000/"
    val token = System.getenv("VIRTUAL_LIFE_API_TOKEN") ?: "local-dev-token"
    val nodeId = System.getenv("VIRTUAL_LIFE_NODE_ID") ?: "kotlin-${UUID.randomUUID()}"
    val companionId = "companion-${UUID.randomUUID()}"
    val http = OkHttpClient()
    val clock = JvmMonotonicClock
    val api = RuntimeApi(baseUrl, token, clock, http)
    val rendered = CompletableDeferred<CompanionSnapshot>()
    val bridge = object : AndroidNodeBridge {
        private val displayedRevision = mutableMapOf<String, Long>()
        override suspend fun capabilities() = setOf("text.display", "reading.context")
        override suspend fun permissions() = HostPermissions()
        override suspend fun screenSignals() = ScreenSignals(hostVisible = true)
        override suspend fun prepareRender(snapshot: CompanionSnapshot) {
            println("prepare ${snapshot.companionId}")
        }
        override suspend fun stopOutput(companionId: String, reason: StopReason) {
            synchronized(displayedRevision) { displayedRevision.remove(companionId) }
            println("stop $companionId ($reason)")
        }
        override suspend fun renderState(snapshot: CompanionSnapshot, output: OutputHandle) {
            output.tryOutput {
                synchronized(displayedRevision) {
                    if (displayedRevision[snapshot.companionId] != snapshot.revision) {
                        displayedRevision[snapshot.companionId] = snapshot.revision
                        println("render ${snapshot.companionId}, epoch=${snapshot.epoch}, revision=${snapshot.revision}")
                    }
                }
                if (snapshot.companionId == companionId) rendered.complete(snapshot)
            }
        }
        override suspend fun onFailure(error: Throwable) {
            System.err.println("node: ${error.message}")
        }
    }
    val cursorDirectory = File(System.getenv("VIRTUAL_LIFE_CURSOR_DIR") ?: "build/example-cursors")
    val node = AndroidNode(
        NodeSessionConfig(nodeId, "Kotlin console host"), api,
        NodeWebSocketConnector(baseUrl, token, http), bridge, FileCursorStore(cursorDirectory), clock,
    )
    var session: kotlinx.coroutines.Job? = null
    try {
        api.registerNode(NodeRegistration(nodeId, "Kotlin console host", capabilities = bridge.capabilities().sorted()))
        api.createCompanion(CompanionCreate(companionId, "Example companion"))
        session = launch(Dispatchers.Default) { node.run() }
        val transfer = api.prepareTransfer(companionId, TransferPrepare(UUID.randomUUID().toString(), nodeId, 0))
        println("prepared transfer ${transfer.transferId}; SDK will acknowledge ready after host preparation")
        withTimeout(20_000) { rendered.await() }
        val current = api.getCompanion(companionId)
        val updated = api.updateContext(companionId, ContextUpdate(
            UUID.randomUUID().toString(), nodeId, current.epoch, current.revision,
            activity = buildJsonObject { put("type", "reading"); put("position", "chapter-1") },
            interactionMode = InteractionMode.FOCUS,
            context = buildJsonObject { put("topic", "A shared book") },
        ))
        println("saved reading context at revision ${updated.revision}")
        delay(1_000)
        println("saved cursor=${node.savedCursor()}, node=$nodeId, companion=$companionId")
    } finally {
        session?.cancelAndJoin()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }
}
