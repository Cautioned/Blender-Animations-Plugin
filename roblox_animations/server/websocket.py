"""Minimal localhost WebSocket notifier for Studio live sync.

This deliberately uses only the Python standard library. Socket I/O runs on a
background thread; Blender data is only inspected by ``publish_updates`` from
the addon timer on Blender's main thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import select
import socket
import threading
from dataclasses import dataclass, field


@dataclass
class _Client:
    socket: socket.socket
    buffer: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    armature: str | None = None


class LiveSyncWebSocketServer:
    """Pushes changed-armature hashes to Studio over localhost WebSockets."""

    def __init__(self, port: int):
        self.port = port
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._clients: list[_Client] = []
        self._clients_lock = threading.Lock()
        self._outbound: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._sync_requests: queue.SimpleQueue[tuple[_Client, dict]] = queue.SimpleQueue()
        self._sync_responses: queue.SimpleQueue[tuple[_Client, str, str, bool, bytes | str]] = queue.SimpleQueue()
        self._new_subscriptions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._last_hashes: dict[str, str] = {}

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(4)
        listener.setblocking(False)
        self._listener = listener
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="rbx-live-sync-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.socket.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def subscribed_armatures(self) -> set[str]:
        with self._clients_lock:
            return {client.armature for client in self._clients if client.armature}

    def publish_updates(self, hash_for_armature) -> None:
        """Run on Blender's main thread; queue only changed subscribed hashes."""
        for armature in self.subscribed_armatures():
            try:
                animation_hash = hash_for_armature(armature)
            except Exception:
                continue
            if animation_hash and self._last_hashes.get(armature) != animation_hash:
                self._last_hashes[armature] = animation_hash
                self._outbound.put((armature, animation_hash))

    def drain_sync_requests(self) -> list[tuple[_Client, dict]]:
        requests = []
        while True:
            try:
                requests.append(self._sync_requests.get_nowait())
            except queue.Empty:
                return requests

    def drain_new_subscriptions(self) -> list[str]:
        subscriptions = []
        while True:
            try:
                subscriptions.append(self._new_subscriptions.get_nowait())
            except queue.Empty:
                return subscriptions

    def publish_sync_response(
        self,
        client: _Client,
        request_id: str,
        trigger_hash: str,
        success: bool,
        payload: bytes | str,
    ) -> None:
        self._sync_responses.put((client, request_id, trigger_hash, success, payload))

    @staticmethod
    def _frame(payload: str) -> bytes:
        data = payload.encode("utf-8")
        length = len(data)
        if length < 126:
            return bytes((0x81, length)) + data
        if length <= 65535:
            return bytes((0x81, 126)) + length.to_bytes(2, "big") + data
        return bytes((0x81, 127)) + length.to_bytes(8, "big") + data

    def _queue_frame(self, client: _Client, payload: dict) -> None:
        encoded = self._frame(json.dumps(payload, separators=(",", ":")))
        with self._clients_lock:
            if client in self._clients:
                client.outgoing.extend(encoded)

    def _remove(self, client: _Client) -> None:
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)
        try:
            client.socket.close()
        except OSError:
            pass

    def _handle_frames(self, client: _Client) -> bool:
        while len(client.buffer) >= 2:
            first, second = client.buffer[0], client.buffer[1]
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(client.buffer) < 4:
                    return True
                length = int.from_bytes(client.buffer[2:4], "big")
                offset = 4
            elif length == 127 or length > 65535:
                if len(client.buffer) < 10:
                    return True
                length = int.from_bytes(client.buffer[2:10], "big")
                offset = 10
                if length > 8 * 1024 * 1024:
                    return False
            if not masked:
                return False
            if len(client.buffer) < offset + 4 + length:
                return True
            mask = client.buffer[offset:offset + 4]
            payload = bytes(client.buffer[offset + 4:offset + 4 + length])
            del client.buffer[:offset + 4 + length]
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                return False
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
                armature = message.get("armature") if isinstance(message, dict) else None
                if isinstance(message, dict) and message.get(
                        "type") == "hello" and isinstance(armature, str) and armature:
                    client.armature = armature
                    # Force an initial notification after a new subscription or reconnect.
                    self._last_hashes.pop(armature, None)
                    self._new_subscriptions.put(armature)
                elif (
                    isinstance(message, dict)
                    and message.get("type") == "sync_request"
                    and isinstance(armature, str)
                    and armature == client.armature
                    and isinstance(message.get("request_id"), str)
                ):
                    self._sync_requests.put((client, message))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return True

    def _accept(self) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            client_socket, _address = listener.accept()
            client_socket.settimeout(1.0)
            request = client_socket.recv(8192).decode("latin1")
            headers = {}
            for line in request.split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            key = headers.get("sec-websocket-key")
            if "GET /live_sync" not in request.split("\r\n", 1)[0] or not key:
                client_socket.close()
                return
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            client_socket.sendall((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii"))
            client_socket.setblocking(False)
            with self._clients_lock:
                self._clients.append(_Client(client_socket))
        except (OSError, UnicodeDecodeError):
            try:
                client_socket.close()
            except (OSError, UnboundLocalError):
                pass

    def _run(self) -> None:
        while self._running.is_set():
            listener = self._listener
            if listener is not None:
                readable, _, _ = select.select([listener], [], [], 0.05)
                if readable:
                    self._accept()
            try:
                while True:
                    armature, animation_hash = self._outbound.get_nowait()
                    frame = self._frame(json.dumps({"type": "animation_changed",
                                        "armature": armature, "hash": animation_hash}))
                    with self._clients_lock:
                        clients = [client for client in self._clients if client.armature == armature]
                    for client in clients:
                        with self._clients_lock:
                            if client in self._clients:
                                client.outgoing.extend(frame)
            except queue.Empty:
                pass
            try:
                while True:
                    client, request_id, trigger_hash, success, payload = self._sync_responses.get_nowait()
                    response = {
                        "type": "sync_payload" if success else "sync_error",
                        "request_id": request_id,
                        "trigger_hash": trigger_hash,
                    }
                    if success and isinstance(payload, bytes):
                        response["data"] = base64.b64encode(payload).decode("ascii")
                    else:
                        response["error"] = str(payload)
                    self._queue_frame(client, response)
            except queue.Empty:
                pass
            with self._clients_lock:
                clients = list(self._clients)
            for client in clients:
                try:
                    data = client.socket.recv(4096)
                    if not data:
                        self._remove(client)
                        continue
                    client.buffer.extend(data)
                    if not self._handle_frames(client):
                        self._remove(client)
                except BlockingIOError:
                    continue
                except OSError:
                    self._remove(client)
            with self._clients_lock:
                writable_clients = [client for client in self._clients if client.outgoing]
            for client in writable_clients:
                try:
                    sent = client.socket.send(client.outgoing)
                    if sent > 0:
                        del client.outgoing[:sent]
                except BlockingIOError:
                    continue
                except OSError:
                    self._remove(client)
