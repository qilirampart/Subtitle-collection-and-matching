from __future__ import annotations

from copy import deepcopy
from typing import Any

import requests

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.api_config_service import ApiConfigService
from app.ui.window_geometry import apply_responsive_window_geometry


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class _LlmProbeThread(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, profile: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._profile = deepcopy(profile)

    def run(self) -> None:
        try:
            base_url = str(self._profile.get("api_base") or "").strip().rstrip("/")
            endpoint = base_url if base_url.endswith("/chat/completions") else (
                f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
            )
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._profile['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": str(self._profile["model"]),
                    "temperature": 0,
                    "max_tokens": 16,
                    "messages": [
                        {"role": "system", "content": "You are a connectivity test. Reply with exactly: OK"},
                        {"role": "user", "content": "Please confirm this model is available."},
                    ],
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            self.succeeded.emit({"preview": str(content or "").strip()[:160]})
        except (KeyError, IndexError, TypeError, ValueError, requests.RequestException) as exc:
            self.failed.emit(str(exc))


class LlmConfigDialog(QDialog):
    """Reusable OpenAI-compatible LLM profile editor."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._service = ApiConfigService()
        self._profiles = self._service.get_llm_profiles(include_disabled=True)
        self._current_index = -1
        self._loading_form = False
        self._probe_thread: _LlmProbeThread | None = None

        self.setWindowTitle("语言模型配置")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        apply_responsive_window_geometry(
            self,
            preferred_width=920,
            preferred_height=620,
            minimum_width=760,
            minimum_height=520,
        )
        self.setSizeGripEnabled(True)

        self._build_ui()
        self._refresh_table(0 if self._profiles else -1)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Keep the action row visible and scroll the complete settings form on
        # compact screens instead of collapsing the current-model fields.
        content_widget = QWidget()
        content = QVBoxLayout(content_widget)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(10)
        hint = QLabel(
            "语言模型配置供文本纠偏、翻译、视频分析等功能复用。当前支持 OpenAI 兼容接口；"
            "一个模型配置可被多个功能绑定。列表从上到下为调用优先级，API Key 仅保存在本机。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #586174;")
        content.addWidget(hint)

        table_group = QGroupBox("模型配置列表")
        table_layout = QVBoxLayout(table_group)
        self.profile_table = QTableWidget(0, 5)
        self.profile_table.setHorizontalHeaderLabels(["优先级", "启用", "名称", "模型", "状态"])
        self.profile_table.verticalHeader().setVisible(False)
        self.profile_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.profile_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.profile_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.profile_table.setAlternatingRowColors(True)
        self.profile_table.setMinimumHeight(170)
        header = self.profile_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.resizeSection(0, 76)
        header.resizeSection(1, 58)
        header.resizeSection(2, 220)
        header.resizeSection(3, 210)
        self.profile_table.itemSelectionChanged.connect(self._on_profile_selected)
        table_layout.addWidget(self.profile_table)
        actions = QHBoxLayout()
        add_button = QPushButton("新增模型")
        self.remove_button = QPushButton("删除当前")
        self.move_up_button = QPushButton("上移优先级")
        self.move_down_button = QPushButton("下移优先级")
        add_button.clicked.connect(self._add_profile)
        self.remove_button.clicked.connect(self._remove_profile)
        self.move_up_button.clicked.connect(lambda: self._move_profile(-1))
        self.move_down_button.clicked.connect(lambda: self._move_profile(1))
        actions.addWidget(add_button)
        actions.addWidget(self.remove_button)
        actions.addWidget(self.move_up_button)
        actions.addWidget(self.move_down_button)
        actions.addStretch(1)
        table_layout.addLayout(actions)
        content.addWidget(table_group)

        form_group = QGroupBox("当前模型")
        form = QFormLayout(form_group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.profile_name = QLineEdit()
        self.profile_enabled = QCheckBox("启用当前模型")
        self.provider_type = QComboBox()
        self.provider_type.addItem("OpenAI 兼容 API", "openai_compatible")
        self.api_base = QLineEdit()
        self.api_base.setPlaceholderText("例如：https://api.example.com/v1")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_api_key = QCheckBox("显示 API Key")
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.show_api_key)
        self.model = QLineEdit()
        self.model.setPlaceholderText("例如：deepseek-chat")
        self.temperature = _NoWheelDoubleSpinBox()
        self.temperature.setRange(0, 1)
        self.temperature.setDecimals(2)
        self.temperature.setSingleStep(0.1)
        self.ready_status = QLabel()
        self.ready_status.setWordWrap(False)
        self.ready_status.setStyleSheet("color: #586174;")
        form.addRow("名称", self.profile_name)
        form.addRow("启用", self.profile_enabled)
        form.addRow("接口类型", self.provider_type)
        form.addRow("API Base", self.api_base)
        form.addRow("API Key", key_row)
        form.addRow("模型名称", self.model)
        form.addRow("Temperature", self.temperature)
        form.addRow("配置状态", self.ready_status)
        form_group.setMinimumHeight(310)
        content.addWidget(form_group)

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content_scroll.setWidget(content_widget)
        root.addWidget(content_scroll, 1)

        footer = QHBoxLayout()
        self.test_button = QPushButton("测试当前模型")
        self.test_button.setProperty("secondary", True)
        self.test_button.clicked.connect(self._test_current_profile)
        save_button = QPushButton("保存配置")
        save_button.setDefault(True)
        save_button.clicked.connect(self._save)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        footer.addWidget(self.test_button)
        footer.addStretch(1)
        footer.addWidget(save_button)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self.show_api_key.toggled.connect(
            lambda checked: self.api_key.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        )

    def _refresh_table(self, selected_index: int) -> None:
        self.profile_table.blockSignals(True)
        self.profile_table.setRowCount(len(self._profiles))
        for index, profile in enumerate(self._profiles):
            ready = self._service.is_llm_profile_ready(profile)
            values = (
                "1（最高）" if index == 0 else str(index + 1),
                "是" if profile.get("enabled", True) else "否",
                str(profile.get("name") or "未命名模型"),
                str(profile.get("model") or "未填写"),
                "已就绪" if ready else "待补全 API Base / Key / 模型",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if column < 2 else Qt.AlignmentFlag.AlignVCenter)
                self.profile_table.setItem(index, column, item)
        self.profile_table.blockSignals(False)
        if 0 <= selected_index < len(self._profiles):
            self.profile_table.selectRow(selected_index)
            self._load_profile(selected_index)
        else:
            self._current_index = -1
            self._set_form_enabled(False)
        self.remove_button.setEnabled(bool(self._profiles))
        self.test_button.setEnabled(0 <= self._current_index < len(self._profiles))
        self.move_up_button.setEnabled(self._current_index > 0)
        self.move_down_button.setEnabled(0 <= self._current_index < len(self._profiles) - 1)

    def _on_profile_selected(self) -> None:
        row = self.profile_table.currentRow()
        if row < 0 or row >= len(self._profiles):
            return
        self._apply_current_form()
        self._load_profile(row)
        self.move_up_button.setEnabled(row > 0)
        self.move_down_button.setEnabled(row < len(self._profiles) - 1)

    def _load_profile(self, index: int) -> None:
        self._current_index = index
        profile = self._profiles[index]
        self._loading_form = True
        self._set_form_enabled(True)
        self.profile_name.setText(str(profile.get("name") or ""))
        self.profile_enabled.setChecked(bool(profile.get("enabled", True)))
        self.api_base.setText(str(profile.get("api_base") or ""))
        self.api_key.setText(str(profile.get("api_key") or ""))
        self.model.setText(str(profile.get("model") or ""))
        self.temperature.setValue(float(profile.get("temperature") or 0))
        self._loading_form = False
        self._update_ready_status()

    def _apply_current_form(self) -> None:
        if self._loading_form or not (0 <= self._current_index < len(self._profiles)):
            return
        profile = self._profiles[self._current_index]
        profile.update(
            {
                "name": self.profile_name.text().strip() or f"语言模型 {self._current_index + 1}",
                "enabled": self.profile_enabled.isChecked(),
                "provider": str(self.provider_type.currentData() or "openai_compatible"),
                "api_base": self.api_base.text().strip().rstrip("/"),
                "api_key": self.api_key.text().strip(),
                "model": self.model.text().strip(),
                "temperature": self.temperature.value(),
            }
        )
        self._update_ready_status()

    def _update_ready_status(self) -> None:
        if not (0 <= self._current_index < len(self._profiles)):
            self.ready_status.setText("请从上方列表选择模型。")
            return
        profile = self._profiles[self._current_index]
        if self._service.is_llm_profile_ready(profile):
            self.ready_status.setText("配置完整，可供文本纠偏、翻译和后续模型功能使用。")
        elif not profile.get("enabled", True):
            self.ready_status.setText("当前模型已停用。")
        else:
            self.ready_status.setText("请补全 API Base、API Key 和模型名称。")

    def _test_current_profile(self) -> None:
        self._apply_current_form()
        if not (0 <= self._current_index < len(self._profiles)):
            return
        profile = deepcopy(self._profiles[self._current_index])
        if not self._service.is_llm_profile_ready(profile):
            QMessageBox.warning(self, "无法测试", "请先填写 API Base、API Key 和模型名称。")
            return
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        self.test_button.setEnabled(False)
        self.ready_status.setText("正在测试当前模型，请稍候...")
        thread = _LlmProbeThread(profile, self)
        self._probe_thread = thread
        thread.succeeded.connect(self._on_probe_succeeded)
        thread.failed.connect(self._on_probe_failed)
        thread.finished.connect(self._on_probe_finished)
        thread.start()

    def _on_probe_succeeded(self, result: dict[str, Any]) -> None:
        preview = str(result.get("preview") or "已收到模型响应")
        self.ready_status.setText("模型测试成功。")
        QMessageBox.information(self, "模型测试成功", f"已收到模型响应：\n{preview}")

    def _on_probe_failed(self, message: str) -> None:
        self.ready_status.setText(f"模型测试失败：{message}")
        QMessageBox.critical(self, "模型测试失败", message)

    def _on_probe_finished(self) -> None:
        if self._probe_thread is not None:
            self._probe_thread.deleteLater()
        self._probe_thread = None
        self.test_button.setEnabled(0 <= self._current_index < len(self._profiles))

    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in (
            self.profile_name, self.profile_enabled, self.provider_type, self.api_base,
            self.api_key, self.show_api_key, self.model, self.temperature,
        ):
            widget.setEnabled(enabled)

    def _add_profile(self) -> None:
        self._apply_current_form()
        self._profiles.append(self._service.create_default_llm_profile(index=len(self._profiles) + 1))
        self._refresh_table(len(self._profiles) - 1)

    def _remove_profile(self) -> None:
        if not (0 <= self._current_index < len(self._profiles)):
            return
        if len(self._profiles) == 1:
            QMessageBox.warning(self, "无法删除", "请至少保留一个语言模型配置；暂时不用时可以关闭“启用”。")
            return
        profile = self._profiles[self._current_index]
        if QMessageBox.question(self, "确认删除", f"确定删除“{profile.get('name') or '未命名模型'}”吗？") != QMessageBox.StandardButton.Yes:
            return
        self._profiles.pop(self._current_index)
        self._refresh_table(min(self._current_index, len(self._profiles) - 1))

    def _move_profile(self, offset: int) -> None:
        if not (0 <= self._current_index < len(self._profiles)):
            return
        self._apply_current_form()
        target = self._current_index + offset
        if target < 0 or target >= len(self._profiles):
            return
        self._profiles[self._current_index], self._profiles[target] = (
            self._profiles[target], self._profiles[self._current_index]
        )
        self._refresh_table(target)

    def _save(self) -> None:
        self._apply_current_form()
        try:
            self._service.save_llm_profiles(deepcopy(self._profiles))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        QMessageBox.information(self, "已保存", "语言模型配置及调用优先级已保存。")
        self.close()
