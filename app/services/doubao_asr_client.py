from __future__ import annotations

import gzip
import json
import struct
import uuid
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEventLoop, QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

from app.utils.logger import get_logger

CancelCallback = Callable[[], bool]


class DoubaoAsrError(RuntimeError):
    pass


class DoubaoAsrClient:
    MESSAGE_TYPE_FULL_REQUEST = 0x1
    MESSAGE_TYPE_AUDIO_ONLY = 0x2
    MESSAGE_TYPE_FULL_RESPONSE = 0x9
    MESSAGE_TYPE_ERROR = 0xF

    FLAG_NONE = 0x0
    FLAG_FINAL_NO_SEQUENCE = 0x2
    FLAG_FINAL_WITH_SEQUENCE = 0x3

    SERIALIZATION_NONE = 0x0
    SERIALIZATION_JSON = 0x1

    COMPRESSION_NONE = 0x0
    COMPRESSION_GZIP = 0x1

    DEFAULT_CHUNK_SIZE_BYTES = 2048
    DEFAULT_TIMEOUT_MS = 180_000

    def __init__(self) -> None:
        self._logger = get_logger(__name__)

    def transcribe_file(
        self,
        audio_path: Path,
        config: dict[str, Any],
        *,
        should_cancel: CancelCallback | None = None,
    ) -> dict[str, Any]:
        session = _DoubaoAsrSession(
            audio_path=audio_path,
            config=config,
            should_cancel=should_cancel,
            logger=self._logger,
        )
        return session.run()

    @classmethod
    def build_full_request_frame(cls, payload: dict[str, Any]) -> bytes:
        body = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return cls._build_frame(
            message_type=cls.MESSAGE_TYPE_FULL_REQUEST,
            flags=cls.FLAG_NONE,
            serialization=cls.SERIALIZATION_JSON,
            compression=cls.COMPRESSION_GZIP,
            payload=body,
        )

    @classmethod
    def build_audio_frame(cls, audio_bytes: bytes, *, is_final: bool) -> bytes:
        body = gzip.compress(audio_bytes)
        return cls._build_frame(
            message_type=cls.MESSAGE_TYPE_AUDIO_ONLY,
            flags=cls.FLAG_FINAL_NO_SEQUENCE if is_final else cls.FLAG_NONE,
            serialization=cls.SERIALIZATION_NONE,
            compression=cls.COMPRESSION_GZIP,
            payload=body,
        )

    @classmethod
    def parse_server_frame(cls, data: bytes) -> dict[str, Any]:
        if len(data) < 8:
            raise DoubaoAsrError("豆包 ASR 返回了长度不足的 WebSocket 数据包。")

        version = (data[0] >> 4) & 0x0F
        header_words = data[0] & 0x0F
        message_type = (data[1] >> 4) & 0x0F
        flags = data[1] & 0x0F
        serialization = (data[2] >> 4) & 0x0F
        compression = data[2] & 0x0F

        if version != 1:
            raise DoubaoAsrError(f"豆包 ASR 返回了不支持的协议版本: {version}")

        offset = header_words * 4
        sequence: int | None = None
        if flags in {0x1, 0x3}:
            if len(data) < offset + 4:
                raise DoubaoAsrError("豆包 ASR 返回包缺少 sequence 字段。")
            sequence = struct.unpack(">i", data[offset : offset + 4])[0]
            offset += 4

        if len(data) < offset + 4:
            raise DoubaoAsrError("豆包 ASR 返回包缺少 payload size 字段。")
        payload_size = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        payload = data[offset : offset + payload_size]

        if compression == cls.COMPRESSION_GZIP:
            payload = gzip.decompress(payload)
        elif compression != cls.COMPRESSION_NONE:
            raise DoubaoAsrError(f"豆包 ASR 返回了不支持的压缩格式: {compression}")

        parsed_payload: Any
        if serialization == cls.SERIALIZATION_JSON:
            parsed_payload = json.loads(payload.decode("utf-8"))
        elif serialization == cls.SERIALIZATION_NONE:
            parsed_payload = payload
        else:
            raise DoubaoAsrError(f"豆包 ASR 返回了不支持的序列化格式: {serialization}")

        return {
            "message_type": message_type,
            "flags": flags,
            "sequence": sequence,
            "payload": parsed_payload,
        }

    @classmethod
    def _build_frame(
        cls,
        *,
        message_type: int,
        flags: int,
        serialization: int,
        compression: int,
        payload: bytes,
    ) -> bytes:
        header = bytes(
            [
                (0x1 << 4) | 0x1,
                ((message_type & 0x0F) << 4) | (flags & 0x0F),
                ((serialization & 0x0F) << 4) | (compression & 0x0F),
                0x00,
            ]
        )
        return header + struct.pack(">I", len(payload)) + payload


class _DoubaoAsrSession(QObject):
    def __init__(
        self,
        *,
        audio_path: Path,
        config: dict[str, Any],
        should_cancel: CancelCallback | None,
        logger,
    ) -> None:
        super().__init__()
        self._audio_path = audio_path
        self._config = config
        self._should_cancel = should_cancel
        self._logger = logger

        self._loop = QEventLoop()
        self._socket = QWebSocket()
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._handle_timeout)

        self._cancel_timer = QTimer(self)
        self._cancel_timer.setInterval(100)
        self._cancel_timer.timeout.connect(self._check_cancel)

        self._audio_bytes = self._audio_path.read_bytes()
        self._cursor = 0
        self._completed = False
        self._error: str | None = None
        self._latest_payload: dict[str, Any] | None = None
        self._connect_id = str(uuid.uuid4())

        self._socket.connected.connect(self._handle_connected)
        self._socket.binaryMessageReceived.connect(self._handle_binary_message)
        self._socket.textMessageReceived.connect(self._handle_text_message)
        self._socket.disconnected.connect(self._handle_disconnected)
        self._socket.errorOccurred.connect(self._handle_error)

    def run(self) -> dict[str, Any]:
        self._timeout_timer.start(DoubaoAsrClient.DEFAULT_TIMEOUT_MS)
        self._cancel_timer.start()
        self._socket.open(self._build_request())
        self._loop.exec()

        self._timeout_timer.stop()
        self._cancel_timer.stop()
        self._socket.abort()
        self._socket.deleteLater()

        if self._error is not None:
            raise DoubaoAsrError(self._error)
        if self._latest_payload is None:
            raise DoubaoAsrError("豆包 ASR 未返回可用识别结果。")
        return self._latest_payload

    def _build_request(self) -> QNetworkRequest:
        ws_url = str(self._config.get("ws_url") or "").strip()
        if not ws_url:
            raise DoubaoAsrError("豆包 ASR 缺少 WebSocket 地址。")

        request = QNetworkRequest(QUrl(ws_url))
        api_key = str(self._config.get("api_key") or "").strip()
        if api_key:
            request.setRawHeader(b"x-api-key", api_key.encode("utf-8"))
        else:
            request.setRawHeader(
                b"X-Api-App-Key",
                str(self._config.get("app_id") or "").strip().encode("utf-8"),
            )
            request.setRawHeader(
                b"X-Api-Access-Key",
                str(self._config.get("access_token") or "").strip().encode("utf-8"),
            )
        request.setRawHeader(
            b"X-Api-Resource-Id",
            str(self._config.get("resource_id") or "").strip().encode("utf-8"),
        )
        request.setRawHeader(b"X-Api-Connect-Id", self._connect_id.encode("utf-8"))
        return request

    def _handle_connected(self) -> None:
        self._logger.info(
            "Doubao ASR websocket connected. file=%s connect_id=%s",
            self._audio_path.name,
            self._connect_id,
        )
        self._socket.sendBinaryMessage(
            DoubaoAsrClient.build_full_request_frame(self._build_open_payload())
        )
        QTimer.singleShot(0, self._send_next_chunk)

    def _handle_binary_message(self, message) -> None:
        try:
            packet = DoubaoAsrClient.parse_server_frame(bytes(message))
        except Exception as exc:  # noqa: BLE001
            self._finish_error(f"解析豆包 ASR 返回包失败: {exc}")
            return

        message_type = int(packet.get("message_type") or 0)
        flags = int(packet.get("flags") or 0)
        payload = packet.get("payload")

        if message_type == DoubaoAsrClient.MESSAGE_TYPE_ERROR:
            self._finish_error(self._extract_error_message(payload))
            return

        if message_type != DoubaoAsrClient.MESSAGE_TYPE_FULL_RESPONSE:
            return

        if isinstance(payload, dict):
            self._latest_payload = payload

        if flags == DoubaoAsrClient.FLAG_FINAL_WITH_SEQUENCE:
            self._completed = True
            self._socket.close()

    def _handle_text_message(self, message: str) -> None:
        self._logger.info("Doubao ASR text message: %s", message[:500])
        try:
            payload = json.loads(message)
        except Exception:
            return
        self._latest_payload = payload if isinstance(payload, dict) else None

    def _handle_disconnected(self) -> None:
        self._logger.info(
            "Doubao ASR websocket disconnected. file=%s completed=%s has_payload=%s",
            self._audio_path.name,
            self._completed,
            self._latest_payload is not None,
        )
        if self._completed or self._latest_payload is not None:
            self._loop.quit()
            return
        if self._error is None:
            self._error = "豆包 ASR 连接已断开，但没有拿到最终识别结果。"
        self._loop.quit()

    def _handle_error(self, _error) -> None:
        detail = self._socket.errorString().strip() or "豆包 ASR WebSocket 连接失败。"
        self._finish_error(detail)

    def _handle_timeout(self) -> None:
        self._finish_error("豆包 ASR 识别超时。")

    def _check_cancel(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            self._finish_error("用户已取消语音转写。")

    def _send_next_chunk(self) -> None:
        if self._error is not None or self._completed:
            return
        if self._should_cancel is not None and self._should_cancel():
            self._finish_error("用户已取消语音转写。")
            return

        chunk_size = int(self._config.get("chunk_size_bytes") or DoubaoAsrClient.DEFAULT_CHUNK_SIZE_BYTES)
        chunk_size = max(256, chunk_size)

        if self._cursor >= len(self._audio_bytes):
            self._socket.sendBinaryMessage(DoubaoAsrClient.build_audio_frame(b"", is_final=True))
            return

        next_cursor = min(self._cursor + chunk_size, len(self._audio_bytes))
        chunk = self._audio_bytes[self._cursor : next_cursor]
        is_final = next_cursor >= len(self._audio_bytes)
        self._cursor = next_cursor
        self._socket.sendBinaryMessage(DoubaoAsrClient.build_audio_frame(chunk, is_final=is_final))
        if not is_final:
            QTimer.singleShot(0, self._send_next_chunk)

    def _build_open_payload(self) -> dict[str, Any]:
        uid = str(self._config.get("uid") or "").strip() or str(uuid.uuid4())
        return {
            "user": {
                "uid": uid,
            },
            "audio": {
                "format": str(self._config.get("audio_format") or "mp3"),
                "rate": int(self._config.get("sample_rate") or 16000),
                "bits": int(self._config.get("bits") or 16),
                "channel": int(self._config.get("channel_num") or 1),
                "language": str(self._config.get("language") or "zh-CN"),
            },
            "request": {
                "model_name": str(self._config.get("model_name") or "bigmodel"),
                "enable_itn": bool(self._config.get("enable_itn", True)),
                "enable_punc": bool(self._config.get("enable_punc", True)),
                "show_utterances": bool(self._config.get("show_utterances", True)),
                "result_type": str(self._config.get("result_type") or "full"),
            },
        }

    def _extract_error_message(self, payload: Any) -> str:
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("Code") or "").strip()
            message = str(payload.get("message") or payload.get("Message") or "").strip()
            if code and message:
                return f"{code}: {message}"
            if message:
                return message
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload).decode("utf-8", errors="replace")[:500]
        return str(payload or "豆包 ASR 返回了未知错误。")

    def _finish_error(self, message: str) -> None:
        if self._error is None:
            self._error = message
        self._socket.abort()
        self._loop.quit()
