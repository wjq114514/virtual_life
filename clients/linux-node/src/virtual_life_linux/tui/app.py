"""Textual TUI: attach a renderer, chat, and detach cleanly.

The TUI never talks to the server directly. It holds a renderer generation and
a monotonic deadline; a delta is only painted while the guard accepts it.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from ..config import ConfigError, load_config
from ..ipc import IpcClient, IpcClientError
from ..lease import BootTimeClock, LeaseAuthorization
from ..render_guard import RenderGuard

try:  # Textual is a runtime dependency but keep imports lazy-friendly.
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Footer, Header, Input, RichLog, Static
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit("The TUI requires the 'textual' package") from exc


class VirtualLifeApp(App):
    CSS = """
    #status { height: 1; padding: 0 1; }
    #voice { height: 1; padding: 0 1; }
    #log { height: 1fr; border: round $accent; }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+d", "detach", "断开"),
        ("ctrl+o", "voice_toggle", "语音开关"),
        ("ctrl+p", "voice_push", "按住说"),
        ("ctrl+g", "voice_commit", "提交轮次"),
        ("ctrl+k", "voice_interrupt", "打断"),
    ]

    def __init__(self, socket_path: Path, companion_id: str | None = None):
        super().__init__()
        self.socket_path = socket_path
        self.companion_id = companion_id
        self.client = IpcClient(socket_path)
        self.guard = RenderGuard(generation="", clock=BootTimeClock())
        self.renderer_id: str | None = None
        self.generation: str | None = None
        self.voice: dict = {}
        self.voice_draft = ""
        self._pump_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("连接中…", id="status")
        yield Static("voice=off", id="voice")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Horizontal(Input(placeholder="说点什么… (回车发送)", id="prompt"))
        yield Footer()

    async def on_mount(self) -> None:
        self._status("正在连接本地守护进程…")
        try:
            await self.client.connect()
            queue = await self.client.subscribe()
            await self._attach()
        except (IpcClientError, ConfigError) as exc:
            self._status(f"[red]连接失败: {exc}[/red]")
            return
        self._pump_task = asyncio.create_task(self._pump(queue))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        with contextlib.suppress(IpcClientError):
            self._set_voice(await self.client.call("voice.status", {}))

    async def _attach(self) -> None:
        result = await self.client.call("renderer.attach", {"companion_id": self.companion_id})
        self.renderer_id = result.get("renderer_id")
        self.generation = result.get("generation")
        self.companion_id = result.get("companion_id")
        self.guard.bind(self.generation or "", self._authorization(result))
        self._status(
            f"已连接 companion={self.companion_id} presentation={result.get('presentation_id')}"
        )

    def _authorization(self, snapshot: dict) -> LeaseAuthorization | None:
        deadline = snapshot.get("render_deadline_ns")
        if deadline is None:
            return None
        return LeaseAuthorization(request_started_ns=0, deadline_ns=int(deadline))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                result = await self.client.call(
                    "renderer.heartbeat", {"renderer_id": self.renderer_id}
                )
            except IpcClientError as exc:
                self._status(f"[red]心跳失败: {exc}[/red]")
                continue
            self.guard.bind(self.generation or "", self._authorization(result))

    async def _pump(self, queue: asyncio.Queue) -> None:
        log = self.query_one("#log", RichLog)
        while True:
            frame = await queue.get()
            event = frame.get("event")
            if event == "dialogue.delta":
                if not self.guard.accept(self.generation or ""):
                    self._status("[yellow]租约到期，已停止绘制[/yellow]")
                    continue
                log.write(frame["data"].get("text", ""))
            elif event == "dialogue.status":
                stage = frame["data"].get("stage")
                self._status(f"companion={self.companion_id} stage={stage}")
            elif event == "dialogue.result":
                self._status("回应完成")
            elif event == "voice.partial":
                self.voice_draft = str(frame["data"].get("text", ""))
                self._render_voice()
            elif event == "voice.commit":
                self.voice_draft = ""
                log.write(f"[b]你(语音):[/b] {frame['data'].get('text', '')}")
                self._render_voice()
            elif event == "voice.needs_confirmation":
                log.write("[yellow](语音待确认: 继续说或换一句)[/yellow]")
            elif event in ("voice.barge_in", "voice.cancel_flush", "voice.idle_timeout"):
                log.write(f"[dim]({event})[/dim]")
            elif event == "voice.state":
                data = frame.get("data", {})
                if "active" in data:
                    self._set_voice(data)
                else:
                    self.voice = {**self.voice, "state": data.get("state")}
                    self._render_voice()
            elif event == "state":
                data = frame.get("data", {})
                if isinstance(data.get("voice"), dict):
                    self._set_voice(data["voice"])
                self._status(f"online={data.get('online')} companion={data.get('companion_id')}")

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        message.input.value = ""
        log = self.query_one("#log", RichLog)
        log.write(f"[b]你:[/b] {text}")
        try:
            if self.voice.get("active"):
                self._set_voice(await self.client.call("voice.commit", {"text": text}))
            else:
                await self.client.call("dialogue.send", {"text": text})
        except IpcClientError as exc:
            log.write(f"[red](未送达: {exc})[/red]")

    async def action_voice_toggle(self) -> None:
        await self._voice_call(
            "voice.stop" if self.voice.get("active") else "voice.start", {"mode": "continuous"}
        )

    async def action_voice_push(self) -> None:
        if self.voice.get("active"):
            self._voice_error("已有语音会话，先停止再切换模式")
            return
        await self._voice_call("voice.start", {"mode": "push_to_talk"})

    async def action_voice_commit(self) -> None:
        await self._voice_call("voice.commit", {})

    async def action_voice_interrupt(self) -> None:
        await self._voice_call("voice.interrupt", {})

    async def _voice_call(self, method: str, params: dict) -> None:
        try:
            self._set_voice(await self.client.call(method, params))
        except IpcClientError as exc:
            self._voice_error(str(exc))

    def _set_voice(self, result: dict) -> None:
        status = result.get("status") if isinstance(result, dict) else None
        self.voice = (
            status if isinstance(status, dict) else (result if isinstance(result, dict) else {})
        )
        self._render_voice()

    def _render_voice(self) -> None:
        voice = self.voice
        if not voice.get("active"):
            mode = voice.get("mode") or "-"
            self.query_one("#voice", Static).update(f"[dim]voice=off mode={mode}[/dim]")
            return
        draft = f" 草稿={self.voice_draft!r}" if self.voice_draft else ""
        self.query_one("#voice", Static).update(
            f"[green]voice={voice.get('state')}[/green] "
            f"playback={voice.get('playback')} mode={voice.get('mode')} "
            f"aec={voice.get('aec')}{draft}"
        )

    def _voice_error(self, message: str) -> None:
        self.query_one("#log", RichLog).write(f"[red](语音: {message})[/red]")

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    async def action_detach(self) -> None:
        with contextlib.suppress(IpcClientError):
            await self.client.call("renderer.detach", {"renderer_id": self.renderer_id})
        self.renderer_id = None
        self._status("已断开呈现会话")

    async def on_unmount(self) -> None:
        for task in (self._pump_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        with contextlib.suppress(IpcClientError):
            await self.client.call("renderer.detach", {"renderer_id": self.renderer_id})
        await self.client.close()


def run(config_path: Path | None = None, companion_id: str | None = None) -> None:
    """Resolve the daemon socket from the config, then start the renderer.

    The socket is server-key scoped, so the config has to be loaded here before
    the TUI can know which daemon to talk to.
    """
    config = load_config(config_path)
    VirtualLifeApp(config.runtime_dir / "daemon.sock", companion_id=companion_id).run()
