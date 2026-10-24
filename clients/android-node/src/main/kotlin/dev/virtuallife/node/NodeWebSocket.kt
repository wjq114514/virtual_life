package dev.virtuallife.node

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

sealed interface NodeStreamFrame {
    data class Event(val event: ProtocolEvent) : NodeStreamFrame
    data class Cursor(val sequence: Long) : NodeStreamFrame
    data object Pong : NodeStreamFrame
}

interface NodeEventConnection {
    val frames: ReceiveChannel<NodeStreamFrame>
    suspend fun awaitWelcome()
    fun ping(): Boolean
    fun close()
}

fun interface NodeEventConnector {
    fun connect(nodeId: String, afterSequence: Long): NodeEventConnection
}

class NodeSocketException(message: String) : IOException(message)

/** The very first client frame is auth. The URL and handshake headers contain no token. */
class NodeWebSocketConnector(
    baseUrl: String,
    private val token: String,
    private val httpClient: OkHttpClient = OkHttpClient(),
    private val bufferSize: Int = 256,
) : NodeEventConnector {
    private val base = baseUrl.toHttpUrl()

    init {
        require(token.isNotBlank() && bufferSize > 0)
        require(base.username.isEmpty() && base.password.isEmpty() && base.query == null && base.fragment == null)
    }

    override fun connect(nodeId: String, afterSequence: Long): NodeEventConnection {
        require(nodeId.isNotBlank() && afterSequence >= 0)
        val url = base.newBuilder().apply {
            listOf("v1", "nodes", nodeId, "events").forEach { addPathSegment(it) }
        }.build()
        val connection = Connection(nodeId, afterSequence)
        connection.socket = httpClient.newWebSocket(Request.Builder().url(url).build(), connection)
        return connection
    }

    private inner class Connection(private val nodeId: String, private val afterSequence: Long) :
        WebSocketListener(), NodeEventConnection {
        @Volatile var socket: WebSocket? = null
        private val welcome = CompletableDeferred<Unit>()
        private val channel = Channel<NodeStreamFrame>(bufferSize)
        override val frames: ReceiveChannel<NodeStreamFrame> get() = channel

        override fun onOpen(webSocket: WebSocket, response: Response) {
            val auth = buildJsonObject {
                put("type", "auth")
                put("token", token)
                put("after_sequence", afterSequence)
            }
            if (!webSocket.send(NodeJson.encodeToString(auth))) fail(webSocket, NodeSocketException("Cannot send authentication frame"))
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val data = NodeJson.parseToJsonElement(text).jsonObject
                val type = data["type"]?.jsonPrimitive?.content ?: throw NodeSocketException("Missing frame type")
                if (!welcome.isCompleted) {
                    if (type != "welcome" || data["node_id"]?.jsonPrimitive?.content != nodeId ||
                        data["protocol_version"]?.jsonPrimitive?.content != "1.0"
                    ) throw NodeSocketException("Invalid welcome frame")
                    welcome.complete(Unit)
                    return
                }
                val frame = when (type) {
                    "event" -> NodeStreamFrame.Event(NodeJson.decodeFromJsonElement(data["event"] ?: throw NodeSocketException("Missing event")))
                    "cursor" -> NodeStreamFrame.Cursor(data["sequence"]?.jsonPrimitive?.long ?: throw NodeSocketException("Missing cursor"))
                    "pong" -> NodeStreamFrame.Pong
                    "error" -> throw NodeSocketException("Runtime rejected a WebSocket frame")
                    else -> throw NodeSocketException("Unsupported WebSocket frame")
                }
                if (!channel.trySend(frame).isSuccess) throw NodeSocketException("Event buffer overflow; reconnect for replay")
            } catch (error: Exception) {
                fail(webSocket, error)
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            fail(webSocket, NodeSocketException("Expected a JSON text frame"))
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            welcome.completeExceptionally(t)
            channel.close(t)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            val error = NodeSocketException("Node event stream closed ($code)")
            welcome.completeExceptionally(error)
            channel.close(error)
        }

        override suspend fun awaitWelcome() { welcome.await() }

        override fun ping(): Boolean = socket?.send("{\"type\":\"ping\"}") == true

        override fun close() {
            socket?.cancel()
            channel.cancel()
            welcome.cancel()
        }

        private fun fail(webSocket: WebSocket, error: Throwable) {
            welcome.completeExceptionally(error)
            channel.close(error)
            webSocket.cancel()
        }
    }
}
