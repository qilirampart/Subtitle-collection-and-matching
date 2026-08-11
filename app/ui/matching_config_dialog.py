from __future__ import annotations

from copy import deepcopy
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.services.api_config_service import ApiConfigService
from app.services.matching_api import DramaSubtitleMatchingClient, MatchingServiceConfig
from app.ui.window_geometry import apply_responsive_window_geometry


# This neutral sample is compiled into the application, so connection testing
# remains available in packaged builds without depending on a local workbook.
MATCHING_CONNECTION_PROBE_TEXT = (
    "这是匹配服务连接测试使用的固定字幕样本，用于验证账号登录、请求权限和字幕匹配接口是否可用。"
    "测试结果不要求命中任何作品，只要服务能正常返回处理结果即可。"
)


class _MatchingConnectionTestThread(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, config: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = deepcopy(config)

    def run(self) -> None:
        try:
            client = DramaSubtitleMatchingClient(
                MatchingServiceConfig(
                    base_url=str(self._config.get("base_url") or ""),
                    timeout_seconds=int(self._config.get("timeout_seconds") or 45),
                )
            )
            client.login(str(self._config.get("username") or ""), str(self._config.get("password") or ""))
            current_user = client.current_user()
            response = client.video_compare(
                MATCHING_CONNECTION_PROBE_TEXT,
                cues=[{"start_seconds": 0, "end_seconds": 12, "text": MATCHING_CONNECTION_PROBE_TEXT}],
                language_code="zh",
                translation_fallback=False,
                top_k=1,
                semantic_enabled=False,
            )
            decision = response.get("decision") if isinstance(response.get("decision"), dict) else {}
            self.succeeded.emit(
                {
                    "username": str(current_user.get("username") or current_user.get("user_name") or ""),
                    "result_status": str(decision.get("status") or "服务已返回结果"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MatchingConfigDialog(QDialog):
    """Persistent credentials and connectivity checks for the matching service."""

    config_saved = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = ApiConfigService()
        self._test_thread: _MatchingConnectionTestThread | None = None
        self.setWindowTitle("匹配服务配置")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        apply_responsive_window_geometry(
            self,
            preferred_width=820,
            preferred_height=520,
            minimum_width=680,
            minimum_height=430,
        )

        self._build_ui()
        self._load_config()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        hint = QLabel("匹配服务地址、账号和密码仅保存在本机 runtime/api_config.json。保存后，匹配页和全流程会自动复用此配置。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #586174;")
        root.addWidget(hint)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://matching.example.com")
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password_check = QCheckBox("显示密码")
        self.show_password_check.toggled.connect(
            lambda visible: self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        )
        password_row = QHBoxLayout()
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.addWidget(self.password_edit, 1)
        password_row.addWidget(self.show_password_check)
        password_widget = QWidget()
        password_widget.setLayout(password_row)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 180)
        self.timeout_spin.setSuffix(" 秒")
        form.addRow("匹配服务地址", self.base_url_edit)
        form.addRow("账号", self.username_edit)
        form.addRow("密码", password_widget)
        form.addRow("单次超时", self.timeout_spin)
        root.addWidget(form_widget)

        self.status_label = QLabel("请先保存配置，或直接测试当前填写的内容。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #586174;")
        root.addWidget(self.status_label)
        root.addStretch(1)

        actions = QHBoxLayout()
        self.test_button = QPushButton("测试连接与匹配接口")
        self.test_button.setProperty("secondary", True)
        self.test_button.clicked.connect(self._test_connection)
        self.save_button = QPushButton("保存配置")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        actions.addWidget(self.test_button)
        actions.addStretch(1)
        actions.addWidget(self.save_button)
        actions.addWidget(close_button)
        root.addLayout(actions)

    def _load_config(self) -> None:
        config = self._service.get_matching_service_config()
        self.base_url_edit.setText(str(config.get("base_url") or ""))
        self.username_edit.setText(str(config.get("username") or ""))
        self.password_edit.setText(str(config.get("password") or ""))
        self.timeout_spin.setValue(int(config.get("timeout_seconds") or 45))
        self._set_status("已加载本机保存的匹配服务配置。")

    def _current_config(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url_edit.text().strip(),
            "username": self.username_edit.text().strip(),
            "password": self.password_edit.text(),
            "timeout_seconds": self.timeout_spin.value(),
        }

    def _save(self) -> None:
        config = self._current_config()
        if not self._service.is_matching_service_ready(config):
            QMessageBox.warning(self, "配置不完整", "请填写匹配服务地址、账号和密码。")
            return
        saved = self._service.save_matching_service_config(config)
        self.config_saved.emit(saved)
        self._set_status("配置已保存，将在下次启动后自动加载。")
        QMessageBox.information(self, "保存成功", "匹配服务配置已保存到本机。")

    def _test_connection(self) -> None:
        config = self._current_config()
        if not self._service.is_matching_service_ready(config):
            QMessageBox.warning(self, "配置不完整", "请先填写匹配服务地址、账号和密码。")
            return
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self._set_status("正在验证账号并提交内置固定字幕样本，请稍候...")
        thread = _MatchingConnectionTestThread(config, self)
        self._test_thread = thread
        thread.succeeded.connect(self._on_test_succeeded)
        thread.failed.connect(self._on_test_failed)
        thread.finished.connect(self._finish_test)
        thread.start()

    def _finish_test(self) -> None:
        if self._test_thread is not None:
            self._test_thread.deleteLater()
        self._test_thread = None
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)

    def _on_test_succeeded(self, result: dict[str, object]) -> None:
        username = str(result.get("username") or self.username_edit.text().strip())
        status = str(result.get("result_status") or "服务已返回结果")
        message = f"测试通过：账号 {username} 登录成功，固定字幕样本已得到服务响应（{status}）。"
        self._set_status(message)
        QMessageBox.information(self, "测试通过", message)

    def _on_test_failed(self, message: str) -> None:
        self._set_status(f"测试失败：{message}")
        QMessageBox.critical(self, "测试失败", f"匹配服务不可用或配置有误：\n{message}")

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setToolTip(message)

