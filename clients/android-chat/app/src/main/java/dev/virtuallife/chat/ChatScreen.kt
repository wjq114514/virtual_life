package dev.virtuallife.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.virtuallife.chat.ChatMessageState.PENDING

private val ChatBlue = Color(0xFFD6E7E2)
private val ChatGrey = Color(0xFFEDEDED)

/** Plain light scheme: the surface is the chat background, so bubbles keep their own fills. */
@Composable
fun ChatTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(primary = Color(0xFF2F6F4F), background = Color.White),
        content = content,
    )
}

/** Everything below the top bar, in one pass so the scrollable body stays out of the chrome. */
@Composable
@OptIn(ExperimentalMaterial3Api::class)
fun ChatShell(controller: ChatController, onConfigChange: (ChatConfig) -> Unit) {
    val state by controller.ui.collectAsState()
    var showSettings by remember { mutableStateOf(false) }
    var showSessions by remember { mutableStateOf(false) }

    // Auto-connect per controller instance: a new ChatConfig rebuilds the controller, which
    // re-runs this effect and starts a fresh node registration.
    LaunchedEffect(controller) { controller.connect() }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(controller.config.companionId, style = MaterialTheme.typography.titleMedium)
                        Text(
                            state.status,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                actions = {
                    IconButton(onClick = { controller.loadHistory() }) {
                        Icon(Icons.Default.Refresh, "拉取服务端历史")
                    }
                    IconButton(onClick = { controller.loadHistory(reset = true) }) {
                        Icon(Icons.Default.Add, "重置为服务端历史")
                    }
                    IconButton(onClick = { showSessions = true }) {
                        Icon(Icons.Default.Menu, "会话列表")
                    }
                    IconButton(onClick = { showSettings = true }) {
                        Icon(Icons.Default.Settings, "设置")
                    }
                },
            )
        },
        bottomBar = { ErrorAndComposer(state, controller) },
    ) { padding ->
        ChatMessages(state, Modifier.padding(padding))
    }

    if (showSettings) {
        SettingsSheet(
            current = controller.config,
            onDismiss = { showSettings = false },
            onSubmit = onConfigChange,
        )
    }
    if (showSessions) {
        SessionSheet(state, controller, onDismiss = { showSessions = false })
    }
}

@Composable
private fun ErrorAndComposer(state: ChatUiState, controller: ChatController) {
    Column {
        state.error?.let { message ->
            Surface(color = MaterialTheme.colorScheme.errorContainer) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp).fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(
                        Icons.Default.Warning,
                        null,
                        modifier = Modifier.size(18.dp),
                        tint = MaterialTheme.colorScheme.onErrorContainer,
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        message,
                        modifier = Modifier.weight(1f),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onErrorContainer,
                    )
                    IconButton(onClick = controller::clearError) {
                        Icon(
                            Icons.Default.Close,
                            "清除",
                            tint = MaterialTheme.colorScheme.onErrorContainer,
                        )
                    }
                }
            }
        }
        ComposerBar(state, controller)
    }
}

@Composable
private fun ChatMessages(state: ChatUiState, modifier: Modifier = Modifier) {
    val session = state.active
    if (session == null || (session.messages.isEmpty() && !state.busy)) {
        EmptyHint()
        return
    }
    val listState = rememberLazyListState()
    val rows = session.messages.size + if (state.busy && state.streamingText.isNotBlank()) 1 else 0
    LaunchedEffect(session.messages.size, state.streamingText) {
        listState.animateScrollToItem((rows - 1).coerceAtLeast(0))
    }
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        state = listState,
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(session.messages, key = { it.id }) { message -> MessageBubble(message) }
        if (state.busy && state.streamingText.isNotBlank()) {
            item(key = "streaming") {
                MessageBubble(ChatMessage(role = ChatRole.ASSISTANT, content = state.streamingText))
            }
        }
    }
}

/** One bubble: text, then any failure note, then timing and token metadata. */
@Composable
private fun MessageBubble(message: ChatMessage) {
    val isUser = message.role == ChatRole.USER
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start,
    ) {
        SelectionContainer {
            Column(
                modifier = Modifier
                    .widthIn(max = 320.dp)
                    .clip(RoundedCornerShape(16.dp))
                    .background(if (isUser) ChatBlue else ChatGrey)
                    .padding(horizontal = 12.dp, vertical = 8.dp),
            ) {
                val text = message.content.ifEmpty { if (message.state == PENDING) "…" else "" }
                if (text.isNotBlank()) Text(text, style = MaterialTheme.typography.bodyLarge)
                message.error?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
                val meta = buildList {
                    if (message.latencyMs != null) add("${message.latencyMs} ms")
                    if (message.outputTokens != null) add("${message.outputTokens} 输出")
                    if (message.inputTokens != null) add("${message.inputTokens} 输入")
                }
                if (meta.isNotEmpty()) {
                    Text(
                        meta.joinToString(" · "),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

@Composable
private fun EmptyHint() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
            Text("还没有对话", style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
            Spacer(Modifier.height(8.dp))
            Text(
                "点右上角刷新从服务器拉取历史，或直接输入开始聊天。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
        }
    }
}

@Composable
private fun ComposerBar(state: ChatUiState, controller: ChatController) {
    var text by rememberSaveable { mutableStateOf("") }
    Surface(color = MaterialTheme.colorScheme.background) {
        Row(
            modifier = Modifier.padding(8.dp).fillMaxWidth(),
            verticalAlignment = Alignment.Bottom,
        ) {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.weight(1f),
                maxLines = 5,
                enabled = !state.busy,
                shape = RoundedCornerShape(20.dp),
                placeholder = { Text("说点什么…") },
            )
            Spacer(Modifier.width(8.dp))
            if (state.busy) {
                FilledTonalButton(onClick = controller::stopTurn) { Text("停止") }
            } else {
                Button(onClick = {
                    if (text.isNotBlank()) {
                        controller.send(text)
                        text = ""
                    }
                }) {
                    Icon(Icons.AutoMirrored.Filled.Send, null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("发送")
                }
            }
        }
    }
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun SettingsSheet(current: ChatConfig, onDismiss: () -> Unit, onSubmit: (ChatConfig) -> Unit) {
    var baseUrl by rememberSaveable { mutableStateOf(current.baseUrl) }
    var token by rememberSaveable { mutableStateOf(current.token) }
    var nodeId by rememberSaveable { mutableStateOf(current.nodeId) }
    var companionId by rememberSaveable { mutableStateOf(current.companionId) }
    var userName by rememberSaveable { mutableStateOf(current.userName) }
    var maxOutputTokens by rememberSaveable { mutableStateOf(current.maxOutputTokens.toString()) }

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            modifier = Modifier
                .padding(horizontal = 16.dp)
                .padding(bottom = 24.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text("连接与身份", style = MaterialTheme.typography.titleMedium)
            TextFieldRow(baseUrl, { baseUrl = it }, "服务地址（以 / 结尾）")
            TextFieldRow(token, { token = it }, "Bearer token")
            TextFieldRow(nodeId, { nodeId = it }, "节点 ID")
            TextFieldRow(companionId, { companionId = it }, "伙伴 ID")
            TextFieldRow(userName, { userName = it }, "你的名字")
            TextFieldRow(
                maxOutputTokens,
                { maxOutputTokens = it.filter(Char::isDigit) },
                "最大输出 tokens（1–1024）",
            )
            Spacer(Modifier.height(12.dp))
            Text(
                "保存后会重新注册节点并重新申请输出权。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            Row {
                OutlinedButton(onClick = onDismiss) { Text("取消") }
                Spacer(Modifier.weight(1f))
                Button(onClick = {
                    onSubmit(
                        current.apply(
                            baseUrl,
                            token,
                            nodeId,
                            companionId,
                            userName,
                            maxOutputTokens,
                        ),
                    )
                    onDismiss()
                }) {
                    Text("保存并重连")
                }
            }
        }
    }
}

@Composable
private fun TextFieldRow(value: String, onValueChange: (String) -> Unit, label: String) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
    )
}

/** Re-clamps each value before saving; the server's budgets are narrower than any sane UI input. */
private fun ChatConfig.apply(
    baseUrl: String,
    token: String,
    nodeId: String,
    companionId: String,
    userName: String,
    maxOutputTokens: String,
): ChatConfig = copy(
    baseUrl = normalizeUrl(baseUrl.trim(), this.baseUrl),
    token = token.trim(),
    nodeId = nodeId.trim().takeIf { it.isNotEmpty() } ?: this.nodeId,
    companionId = companionId.trim().takeIf { it.isNotEmpty() } ?: this.companionId,
    userName = userName.trim().ifBlank { "你" },
    maxOutputTokens = maxOutputTokens.toIntOrNull()?.coerceIn(1, 1024) ?: this.maxOutputTokens,
)

private fun normalizeUrl(value: String, fallback: String): String {
    if (value.isEmpty()) return fallback
    return if (value.endsWith("/")) value else "$value/"
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun SessionSheet(state: ChatUiState, controller: ChatController, onDismiss: () -> Unit) {
    var renameTarget by remember { mutableStateOf<ChatSession?>(null) }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            modifier = Modifier
                .padding(horizontal = 16.dp)
                .padding(bottom = 24.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("会话", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { controller.newSession(); onDismiss() }) { Text("新建") }
                TextButton(onClick = { controller.loadHistory(reset = true) }) { Text("重置") }
            }
            HorizontalDivider()
            if (state.sessions.isEmpty()) {
                Text("还没有会话", style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(8.dp))
            }
            state.sessions.forEach { session ->
                val isActive = session.id == state.activeSessionId
                Row(
                    modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(
                            session.title.ifBlank { "新会话" },
                            style = MaterialTheme.typography.bodyMedium.copy(
                                fontWeight = if (isActive) FontWeight.SemiBold else FontWeight.Normal,
                            ),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text(
                            "${session.messages.size} 条",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    TextButton(onClick = { renameTarget = session }) { Text("重命名") }
                    IconButton(onClick = { controller.deleteSession(session.id) }) {
                        Icon(Icons.Default.Delete, "删除会话")
                    }
                }
                HorizontalDivider()
            }
        }
    }
    renameTarget?.let { session ->
        RenameDialog(
            session = session,
            onDismiss = { renameTarget = null },
            onSubmit = { title ->
                controller.renameSession(session.id, title)
                renameTarget = null
            },
        )
    }
}

@Composable
private fun RenameDialog(session: ChatSession, onDismiss: () -> Unit, onSubmit: (String) -> Unit) {
    var text by remember(session.id) { mutableStateOf(session.title) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("重命名会话") },
        text = {
            Column {
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("会话标题") },
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "共 ${session.messages.size} 条消息",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { onSubmit(text) }, enabled = text.isNotBlank()) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}
