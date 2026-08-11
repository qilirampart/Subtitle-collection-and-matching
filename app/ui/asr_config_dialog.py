from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import ASR_PROBE_AUDIO_PATH
from app.services.api_config_service import ApiConfigService
from app.services.audio_transcription_service import AudioTranscriptionService
from app.ui.window_geometry import apply_responsive_window_geometry


class _NoWheelComboBox(QComboBox):
    """Settings must only change through an explicit click or keyboard action."""

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class _NoWheelSpinBox(QSpinBox):
    """Prevent accidental parameter changes while the user scrolls the dialog."""

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class _AsrProbeThread(QThread):
    progress = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, provider: dict[str, Any], sample_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = deepcopy(provider)
        self._sample_path = sample_path

    def run(self) -> None:
        try:
            _prepared, result = AudioTranscriptionService().transcribe_source_with_provider(
                str(self._sample_path),
                self._provider,
                progress_callback=lambda _current, _total, message: self.progress.emit(message),
                should_cancel=self.isInterruptionRequested,
            )
            self.succeeded.emit(
                {
                    "text": result.text.strip(),
                    "segments": len(result.segments),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class AsrConfigDialog(QDialog):
    """Form-based ASR configuration with ordered provider failover."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = ApiConfigService()
        self._config = self._service.load_config(force_reload=True)
        self._providers = deepcopy(self._config.get("asr", {}).get("providers", []))
        self._current_index = -1
        self._loading_form = False
        self._probe_thread: _AsrProbeThread | None = None

        self.setWindowTitle("ASR 配置")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        apply_responsive_window_geometry(
            self,
            preferred_width=1060,
            preferred_height=720,
            minimum_width=820,
            minimum_height=600,
        )
        self.setSizeGripEnabled(True)

        self._build_ui()
        self._load_global_settings()
        self._refresh_provider_table(0 if self._providers else -1)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Keep save/close actions reachable while the complete settings form
        # scrolls on shorter screens. Previously only the provider detail area
        # scrolled, leaving it with almost no usable height on small displays.
        content_widget = QWidget()
        content = QVBoxLayout(content_widget)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(10)

        hint = QLabel(
            "配置会按列表顺序自动切换：当前接口连续失败达到阈值后进入冷却，并尝试下一个已启用接口。"
            "密钥仅保存在本机 runtime/api_config.json，不会写入日志。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #586174;")
        content.addWidget(hint)

        global_group = QGroupBox("全局策略")
        global_layout = QHBoxLayout(global_group)
        self.asr_enabled = QCheckBox("启用 ASR 转写")
        self.failure_threshold = _NoWheelSpinBox()
        self.failure_threshold.setRange(1, 10)
        self.failure_threshold.setSuffix(" 次")
        self.cooldown_seconds = _NoWheelSpinBox()
        self.cooldown_seconds.setRange(0, 3600)
        self.cooldown_seconds.setSingleStep(30)
        self.cooldown_seconds.setSuffix(" 秒")
        global_layout.addWidget(self.asr_enabled)
        global_layout.addSpacing(18)
        global_layout.addWidget(QLabel("连续失败熔断阈值"))
        global_layout.addWidget(self.failure_threshold)
        global_layout.addSpacing(18)
        global_layout.addWidget(QLabel("冷却时间"))
        global_layout.addWidget(self.cooldown_seconds)
        global_layout.addStretch(1)
        content.addWidget(global_group)

        correction_group = QGroupBox("转写后文本纠偏")
        correction_form = QFormLayout(correction_group)
        correction_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.correction_enabled = QCheckBox("启用保守纠偏")
        self.correction_profile = _NoWheelComboBox()
        self.correction_profile.setMinimumWidth(300)
        refresh_profiles_button = QPushButton("刷新模型列表")
        refresh_profiles_button.clicked.connect(self._refresh_correction_profiles)
        correction_profile_row = QHBoxLayout()
        correction_profile_row.setContentsMargins(0, 0, 0, 0)
        correction_profile_row.addWidget(self.correction_profile, 1)
        correction_profile_row.addWidget(refresh_profiles_button)
        correction_profile_widget = QWidget()
        correction_profile_widget.setLayout(correction_profile_row)
        self.correction_timeout = _NoWheelSpinBox()
        self.correction_timeout.setRange(15, 600)
        self.correction_timeout.setSuffix(" 秒")
        self.correction_batch_size = _NoWheelSpinBox()
        self.correction_batch_size.setRange(1, 10)
        self.correction_batch_size.setSuffix(" 条/次")
        correction_tip = QLabel("只纠正明显错字、同音字、漏字和断句；模型由“语言模型配置”统一管理。")
        # A wrapped QLabel inside QFormLayout can receive an undersized row on
        # compact screens. Keep this concise hint on one stable line instead.
        correction_tip.setWordWrap(False)
        correction_tip.setToolTip(correction_tip.text())
        correction_tip.setStyleSheet("color: #586174;")
        correction_form.addRow("开关", self.correction_enabled)
        correction_form.addRow("使用的语言模型", correction_profile_widget)
        correction_form.addRow("单次超时", self.correction_timeout)
        correction_form.addRow("批量合并", self.correction_batch_size)
        correction_form.addRow("说明", correction_tip)
        content.addWidget(correction_group)

        table_group = QGroupBox("ASR 接口列表")
        table_layout = QVBoxLayout(table_group)
        self.provider_table = QTableWidget(0, 5)
        self.provider_table.setHorizontalHeaderLabels(["顺序", "启用", "名称", "类型", "状态"])
        self.provider_table.verticalHeader().setVisible(False)
        self.provider_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.provider_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.provider_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.provider_table.setAlternatingRowColors(True)
        self.provider_table.setMinimumHeight(175)
        header = self.provider_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.resizeSection(0, 56)
        header.resizeSection(1, 58)
        header.resizeSection(2, 220)
        header.resizeSection(3, 130)
        self.provider_table.itemSelectionChanged.connect(self._on_provider_selected)
        table_layout.addWidget(self.provider_table)

        table_actions = QHBoxLayout()
        self.add_provider_button = QPushButton("新增接口")
        self.remove_provider_button = QPushButton("删除当前")
        self.move_up_button = QPushButton("上移")
        self.move_down_button = QPushButton("下移")
        self.add_provider_button.clicked.connect(self._add_provider)
        self.remove_provider_button.clicked.connect(self._remove_provider)
        self.move_up_button.clicked.connect(lambda: self._move_provider(-1))
        self.move_down_button.clicked.connect(lambda: self._move_provider(1))
        for button in (self.add_provider_button, self.remove_provider_button, self.move_up_button, self.move_down_button):
            table_actions.addWidget(button)
        table_actions.addStretch(1)
        table_layout.addLayout(table_actions)
        content.addWidget(table_group)

        detail_group = QGroupBox("当前接口")
        detail_layout = QVBoxLayout(detail_group)
        common_form = QFormLayout()
        self.provider_name = QLineEdit()
        self.provider_enabled = QCheckBox("启用当前接口")
        self.provider_type = _NoWheelComboBox()
        self.provider_type.addItem("腾讯云 ASR", "tencent_asr")
        self.provider_type.addItem("豆包 ASR", "doubao_asr")
        self.provider_status = QLabel()
        self.provider_status.setWordWrap(True)
        self.provider_status.setStyleSheet("color: #586174;")
        common_form.addRow("名称", self.provider_name)
        common_form.addRow("启用", self.provider_enabled)
        common_form.addRow("类型", self.provider_type)
        common_form.addRow("配置状态", self.provider_status)
        detail_layout.addLayout(common_form)

        self.tencent_group = QGroupBox("腾讯云参数")
        tencent_form = QFormLayout(self.tencent_group)
        self.tencent_secret_id = QLineEdit()
        self.tencent_secret_key = QLineEdit()
        self.tencent_secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_tencent_secret = QCheckBox("显示 SecretKey")
        secret_row = QHBoxLayout()
        secret_row.addWidget(self.tencent_secret_key)
        secret_row.addWidget(self.show_tencent_secret)
        secret_widget = QWidget()
        secret_widget.setLayout(secret_row)
        self.tencent_region = QLineEdit()
        self.tencent_engine = _NoWheelComboBox()
        self.tencent_engine.setEditable(True)
        self.tencent_engine.addItems(["16k_zh", "16k_zh_video", "8k_zh", "16k_en"])
        self.tencent_channels = _NoWheelComboBox()
        self.tencent_channels.addItem("单声道", 1)
        self.tencent_channels.addItem("双声道", 2)
        self.tencent_res_format = _NoWheelComboBox()
        for value in range(4):
            self.tencent_res_format.addItem(str(value), value)
        tencent_form.addRow("SecretId", self.tencent_secret_id)
        tencent_form.addRow("SecretKey", secret_widget)
        tencent_form.addRow("地域", self.tencent_region)
        tencent_form.addRow("识别模型", self.tencent_engine)
        tencent_form.addRow("音频声道", self.tencent_channels)
        tencent_form.addRow("返回格式", self.tencent_res_format)
        detail_layout.addWidget(self.tencent_group)

        self.doubao_group = QGroupBox("豆包参数")
        doubao_form = QFormLayout(self.doubao_group)
        self.doubao_api_key = QLineEdit()
        self.doubao_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_doubao_secret = QCheckBox("显示 API Key")
        doubao_key_row = QHBoxLayout()
        doubao_key_row.addWidget(self.doubao_api_key)
        doubao_key_row.addWidget(self.show_doubao_secret)
        doubao_key_widget = QWidget()
        doubao_key_widget.setLayout(doubao_key_row)
        self.doubao_resource_id = QLineEdit()
        self.doubao_ws_url = QLineEdit()
        self.doubao_language = QLineEdit()
        self.doubao_model = QLineEdit()
        self.doubao_uid = QLineEdit()
        self.doubao_app_id = QLineEdit()
        self.doubao_access_token = QLineEdit()
        self.doubao_access_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.doubao_audio_format = _NoWheelComboBox()
        self.doubao_audio_format.setEditable(True)
        self.doubao_audio_format.addItems(["mp3", "wav", "pcm"])
        self.doubao_sample_rate = _NoWheelSpinBox()
        self.doubao_sample_rate.setRange(8000, 48000)
        self.doubao_sample_rate.setSingleStep(8000)
        self.doubao_bits = _NoWheelComboBox()
        self.doubao_bits.addItem("16 bit", 16)
        self.doubao_bits.addItem("24 bit", 24)
        self.doubao_channels = _NoWheelComboBox()
        self.doubao_channels.addItem("单声道", 1)
        self.doubao_channels.addItem("双声道", 2)
        self.doubao_show_utterances = QCheckBox("返回分句结果")
        self.doubao_enable_itn = QCheckBox("启用数字规范化（ITN）")
        self.doubao_enable_punc = QCheckBox("自动补充标点")
        self.doubao_result_type = _NoWheelComboBox()
        self.doubao_result_type.setEditable(True)
        self.doubao_result_type.addItems(["full", "compact"])
        doubao_form.addRow("API Key", doubao_key_widget)
        doubao_form.addRow("Resource ID", self.doubao_resource_id)
        doubao_form.addRow("WebSocket 地址", self.doubao_ws_url)
        doubao_form.addRow("语言", self.doubao_language)
        doubao_form.addRow("模型", self.doubao_model)
        doubao_form.addRow("用户标识（可选）", self.doubao_uid)
        doubao_form.addRow("旧版 App ID（可选）", self.doubao_app_id)
        doubao_form.addRow("旧版 Access Token（可选）", self.doubao_access_token)
        doubao_form.addRow("音频格式", self.doubao_audio_format)
        doubao_form.addRow("采样率", self.doubao_sample_rate)
        doubao_form.addRow("位深", self.doubao_bits)
        doubao_form.addRow("音频声道", self.doubao_channels)
        doubao_form.addRow("分句", self.doubao_show_utterances)
        doubao_form.addRow("文本规范化", self.doubao_enable_itn)
        doubao_form.addRow("自动标点", self.doubao_enable_punc)
        doubao_form.addRow("结果类型", self.doubao_result_type)
        detail_layout.addWidget(self.doubao_group)

        probe_row = QHBoxLayout()
        self.test_button = QPushButton("测试当前接口")
        self.test_status = QLabel()
        self.test_status.setWordWrap(True)
        self.test_button.clicked.connect(self._test_current_provider)
        probe_row.addWidget(self.test_button)
        probe_row.addWidget(self.test_status, 1)
        detail_layout.addLayout(probe_row)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(detail_group)
        detail_scroll.setMinimumHeight(340)
        content.addWidget(detail_scroll)

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content_scroll.setWidget(content_widget)
        root.addWidget(content_scroll, 1)

        footer = QHBoxLayout()
        self.footer_test_button = QPushButton("测试当前接口")
        self.footer_test_button.setProperty("secondary", True)
        self.footer_test_button.clicked.connect(self._test_current_provider)
        import_button = QPushButton("导入旧软件 API 配置")
        import_button.clicked.connect(self._import_old_config)
        save_button = QPushButton("保存配置")
        save_button.setDefault(True)
        save_button.clicked.connect(self._save)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        footer.addWidget(self.footer_test_button)
        footer.addWidget(import_button)
        footer.addStretch(1)
        footer.addWidget(save_button)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self.show_tencent_secret.toggled.connect(
            lambda checked: self.tencent_secret_key.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        )
        self.show_doubao_secret.toggled.connect(
            lambda checked: self.doubao_api_key.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        )
        self.provider_type.currentIndexChanged.connect(self._on_provider_type_changed)

    def _load_global_settings(self) -> None:
        asr = self._config.get("asr", {})
        failover = asr.get("failover", {}) if isinstance(asr, dict) else {}
        correction = self._config.get("text_correction", {})
        self.asr_enabled.setChecked(bool(asr.get("enabled", True)))
        self.failure_threshold.setValue(int(failover.get("failure_threshold", 1)))
        self.cooldown_seconds.setValue(int(failover.get("cooldown_seconds", 300)))
        self.correction_enabled.setChecked(bool(correction.get("enabled", False)))
        self.correction_timeout.setValue(int(correction.get("timeout_seconds", 90)))
        self.correction_batch_size.setValue(int(correction.get("batch_items_per_request", 5)))
        self._refresh_correction_profiles(str(correction.get("profile_id") or ""))

    def _refresh_correction_profiles(self, selected_id: str = "") -> None:
        if not isinstance(selected_id, str):
            selected_id = str(self.correction_profile.currentData() or "")
        self.correction_profile.blockSignals(True)
        self.correction_profile.clear()
        self.correction_profile.addItem("未选择（不启用纠偏）", "")
        for profile in self._service.get_llm_profiles(include_disabled=True):
            suffix = "" if profile.get("enabled", True) else "（已停用）"
            self.correction_profile.addItem(f"{profile.get('name') or '未命名'}{suffix}", profile.get("id") or "")
        index = self.correction_profile.findData(selected_id)
        self.correction_profile.setCurrentIndex(index if index >= 0 else 0)
        self.correction_profile.blockSignals(False)

    def _refresh_provider_table(self, selected_index: int) -> None:
        self.provider_table.blockSignals(True)
        self.provider_table.setRowCount(len(self._providers))
        for index, provider in enumerate(self._providers):
            provider_type = str(provider.get("provider") or "tencent_asr")
            ready = self._service.is_asr_provider_ready(provider)
            values = (
                str(index + 1),
                "是" if provider.get("enabled", True) else "否",
                str(provider.get("name") or "未命名接口"),
                self._service.get_asr_provider_label(provider_type),
                "已就绪" if ready else "待补全密钥",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if column < 2 else Qt.AlignmentFlag.AlignVCenter)
                self.provider_table.setItem(index, column, item)
        self.provider_table.blockSignals(False)
        if 0 <= selected_index < len(self._providers):
            self.provider_table.selectRow(selected_index)
            self._load_provider(selected_index)
        else:
            self._current_index = -1
            self._set_detail_enabled(False)
        self._update_actions()

    def _on_provider_selected(self) -> None:
        row = self.provider_table.currentRow()
        if row < 0 or row >= len(self._providers):
            return
        self._apply_current_form()
        self._load_provider(row)

    def _load_provider(self, index: int) -> None:
        if index < 0 or index >= len(self._providers):
            return
        self._current_index = index
        provider = self._providers[index]
        provider_type = str(provider.get("provider") or "tencent_asr")
        self._loading_form = True
        self._set_detail_enabled(True)
        self.provider_name.setText(str(provider.get("name") or ""))
        self.provider_enabled.setChecked(bool(provider.get("enabled", True)))
        self._set_combo_data(self.provider_type, provider_type)
        self.tencent_secret_id.setText(str(provider.get("secret_id") or ""))
        self.tencent_secret_key.setText(str(provider.get("secret_key") or ""))
        self.tencent_region.setText(str(provider.get("region") or "ap-shanghai"))
        self.tencent_engine.setCurrentText(str(provider.get("engine_model_type") or "16k_zh"))
        self._set_combo_data(self.tencent_channels, int(provider.get("channel_num") or 1))
        self._set_combo_data(self.tencent_res_format, int(provider.get("res_text_format") or 3))
        self.doubao_api_key.setText(str(provider.get("api_key") or ""))
        self.doubao_resource_id.setText(str(provider.get("resource_id") or ""))
        self.doubao_ws_url.setText(str(provider.get("ws_url") or ""))
        self.doubao_language.setText(str(provider.get("language") or "zh-CN"))
        self.doubao_model.setText(str(provider.get("model_name") or "bigmodel"))
        self.doubao_uid.setText(str(provider.get("uid") or ""))
        self.doubao_app_id.setText(str(provider.get("app_id") or ""))
        self.doubao_access_token.setText(str(provider.get("access_token") or ""))
        self.doubao_audio_format.setCurrentText(str(provider.get("audio_format") or "mp3"))
        self.doubao_sample_rate.setValue(int(provider.get("sample_rate") or 16000))
        self._set_combo_data(self.doubao_bits, int(provider.get("bits") or 16))
        self._set_combo_data(self.doubao_channels, int(provider.get("channel_num") or 1))
        self.doubao_show_utterances.setChecked(bool(provider.get("show_utterances", True)))
        self.doubao_enable_itn.setChecked(bool(provider.get("enable_itn", True)))
        self.doubao_enable_punc.setChecked(bool(provider.get("enable_punc", True)))
        self.doubao_result_type.setCurrentText(str(provider.get("result_type") or "full"))
        self._loading_form = False
        self._update_provider_type_visibility(provider_type)
        self._update_provider_status()
        self._update_actions()

    def _apply_current_form(self) -> None:
        if self._loading_form or not (0 <= self._current_index < len(self._providers)):
            return
        provider = self._providers[self._current_index]
        provider_type = str(self.provider_type.currentData() or "tencent_asr")
        provider["name"] = self.provider_name.text().strip() or f"ASR {self._current_index + 1}"
        provider["enabled"] = self.provider_enabled.isChecked()
        provider["provider"] = provider_type
        if provider_type == "doubao_asr":
            provider.update(
                {
                    "api_key": self.doubao_api_key.text().strip(),
                    "resource_id": self.doubao_resource_id.text().strip(),
                    "ws_url": self.doubao_ws_url.text().strip(),
                    "language": self.doubao_language.text().strip() or "zh-CN",
                    "model_name": self.doubao_model.text().strip() or "bigmodel",
                    "uid": self.doubao_uid.text().strip(),
                    "app_id": self.doubao_app_id.text().strip(),
                    "access_token": self.doubao_access_token.text().strip(),
                    "audio_format": self.doubao_audio_format.currentText().strip() or "mp3",
                    "sample_rate": self.doubao_sample_rate.value(),
                    "bits": int(self.doubao_bits.currentData() or 16),
                    "channel_num": int(self.doubao_channels.currentData() or 1),
                    "show_utterances": self.doubao_show_utterances.isChecked(),
                    "enable_itn": self.doubao_enable_itn.isChecked(),
                    "enable_punc": self.doubao_enable_punc.isChecked(),
                    "result_type": self.doubao_result_type.currentText().strip() or "full",
                }
            )
        else:
            provider.update(
                {
                    "secret_id": self.tencent_secret_id.text().strip(),
                    "secret_key": self.tencent_secret_key.text().strip(),
                    "region": self.tencent_region.text().strip() or "ap-shanghai",
                    "engine_model_type": self.tencent_engine.currentText().strip() or "16k_zh",
                    "channel_num": int(self.tencent_channels.currentData() or 1),
                    "res_text_format": int(self.tencent_res_format.currentData() or 3),
                }
            )
        self._update_provider_status()

    def _on_provider_type_changed(self) -> None:
        if self._loading_form or not (0 <= self._current_index < len(self._providers)):
            return
        old_provider = self._providers[self._current_index]
        new_type = str(self.provider_type.currentData() or "tencent_asr")
        if new_type == old_provider.get("provider"):
            self._update_provider_type_visibility(new_type)
            return
        replacement = self._service.create_default_asr_provider(new_type, priority=self._current_index + 1)
        replacement["name"] = self.provider_name.text().strip() or replacement["name"]
        replacement["enabled"] = self.provider_enabled.isChecked()
        self._providers[self._current_index] = replacement
        self._load_provider(self._current_index)

    def _update_provider_type_visibility(self, provider_type: str) -> None:
        is_doubao = provider_type == "doubao_asr"
        self.tencent_group.setVisible(not is_doubao)
        self.doubao_group.setVisible(is_doubao)

    def _update_provider_status(self) -> None:
        if not (0 <= self._current_index < len(self._providers)):
            self.provider_status.setText("请从上方列表选择接口。")
            return
        provider = self._providers[self._current_index]
        if self._service.is_asr_provider_ready(provider):
            self.provider_status.setText("配置完整，可参与故障切换。")
        elif provider.get("provider") == "doubao_asr":
            self.provider_status.setText("需要填写 API Key 和 Resource ID，或旧版 App ID、Access Token、Resource ID。")
        else:
            self.provider_status.setText("需要填写 SecretId 和 SecretKey。")

    def _set_detail_enabled(self, enabled: bool) -> None:
        for widget in (
            self.provider_name, self.provider_enabled, self.provider_type, self.tencent_group,
            self.doubao_group, self.test_button,
        ):
            widget.setEnabled(enabled)

    def _update_actions(self) -> None:
        current = self._current_index
        has_current = 0 <= current < len(self._providers)
        running = self._probe_thread is not None and self._probe_thread.isRunning()
        self.remove_provider_button.setEnabled(has_current)
        self.move_up_button.setEnabled(has_current and current > 0)
        self.move_down_button.setEnabled(has_current and current < len(self._providers) - 1)
        self.test_button.setEnabled(has_current and not running and ASR_PROBE_AUDIO_PATH.exists())
        self.footer_test_button.setEnabled(has_current and not running and ASR_PROBE_AUDIO_PATH.exists())
        if running:
            self.test_status.setText("正在测试当前接口，请稍候…")
        elif not ASR_PROBE_AUDIO_PATH.exists():
            self.test_status.setText("未找到内置参考音频，无法执行接口测试。")
        elif not self.test_status.text():
            self.test_status.setText(f"使用参考音频：{ASR_PROBE_AUDIO_PATH.name}")

    def _add_provider(self) -> None:
        self._apply_current_form()
        self._providers.append(self._service.create_default_asr_provider("tencent_asr", priority=len(self._providers) + 1))
        self._refresh_provider_table(len(self._providers) - 1)

    def _remove_provider(self) -> None:
        if not (0 <= self._current_index < len(self._providers)):
            return
        self._providers.pop(self._current_index)
        self._refresh_provider_table(min(self._current_index, len(self._providers) - 1))

    def _move_provider(self, delta: int) -> None:
        self._apply_current_form()
        target = self._current_index + delta
        if not (0 <= self._current_index < len(self._providers) and 0 <= target < len(self._providers)):
            return
        self._providers[self._current_index], self._providers[target] = self._providers[target], self._providers[self._current_index]
        self._refresh_provider_table(target)

    def _import_old_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择旧软件 api_config.json", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            asr = payload.get("asr") if isinstance(payload, dict) else None
            if not isinstance(asr, dict):
                raise ValueError("文件中未找到 asr 配置。")
            imported = self._service.normalize_config({"asr": asr})
            self._providers = deepcopy(imported["asr"]["providers"])
            self._config["asr"] = imported["asr"]
            if isinstance(payload.get("text_correction"), dict):
                self._config["text_correction"] = payload["text_correction"]
            self._load_global_settings()
            self._refresh_provider_table(0 if self._providers else -1)
            QMessageBox.information(self, "导入完成", "已载入 ASR 接口、优先级、熔断和文本纠偏设置，请确认后保存。")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导入失败", str(exc))

    def _test_current_provider(self) -> None:
        self._apply_current_form()
        if not (0 <= self._current_index < len(self._providers)):
            return
        provider = deepcopy(self._providers[self._current_index])
        if not self._service.is_asr_provider_ready(provider):
            QMessageBox.warning(self, "无法测试", "请先补全当前接口的必填密钥。")
            return
        if not ASR_PROBE_AUDIO_PATH.exists():
            QMessageBox.warning(self, "无法测试", f"未找到参考音频：\n{ASR_PROBE_AUDIO_PATH}")
            return
        self._probe_thread = _AsrProbeThread(provider, ASR_PROBE_AUDIO_PATH, self)
        self._probe_thread.progress.connect(self.test_status.setText)
        self._probe_thread.succeeded.connect(self._on_probe_succeeded)
        self._probe_thread.failed.connect(self._on_probe_failed)
        self._probe_thread.finished.connect(self._on_probe_finished)
        self._probe_thread.start()
        self._update_actions()

    def _on_probe_succeeded(self, result: dict[str, Any]) -> None:
        text = str(result.get("text") or "").replace("\n", " ")
        self.test_status.setText("接口测试成功。")
        QMessageBox.information(self, "接口测试成功", f"识别片段：{result.get('segments', 0)}\n\n文本预览：\n{text[:240] or '未返回文本'}")

    def _on_probe_failed(self, message: str) -> None:
        self.test_status.setText(f"接口测试失败：{message}")
        QMessageBox.critical(self, "接口测试失败", message)

    def _on_probe_finished(self) -> None:
        if self._probe_thread is not None:
            self._probe_thread.deleteLater()
        self._probe_thread = None
        self._update_actions()

    def _save(self) -> None:
        self._apply_current_form()
        if self.asr_enabled.isChecked() and not self._providers:
            QMessageBox.warning(self, "无法保存", "启用 ASR 时至少需要保留一个接口。")
            return
        correction_profile_id = str(self.correction_profile.currentData() or "")
        if self.correction_enabled.isChecked():
            profile = self._service.get_llm_profile(correction_profile_id)
            if profile is None or not self._service.is_llm_profile_ready(profile):
                QMessageBox.warning(self, "无法保存", "启用文本纠偏前，请先在“语言模型配置”中完成并启用一个模型，再在此处选择它。")
                return
        config = self._service.load_config(force_reload=True)
        config["asr"] = {
            "enabled": self.asr_enabled.isChecked(),
            "failover": {
                "failure_threshold": self.failure_threshold.value(),
                "cooldown_seconds": self.cooldown_seconds.value(),
            },
            "providers": deepcopy(self._providers),
        }
        existing = config.get("text_correction", {})
        config["text_correction"] = {
            "enabled": self.correction_enabled.isChecked(),
            "profile_id": correction_profile_id,
            "api_base": str(existing.get("api_base") or ""),
            "api_key": str(existing.get("api_key") or ""),
            "model": str(existing.get("model") or ""),
            "temperature": 0,
            "timeout_seconds": self.correction_timeout.value(),
            "max_chars_per_chunk": int(existing.get("max_chars_per_chunk") or 1800),
            "batch_items_per_request": self.correction_batch_size.value(),
        }
        self._service.save_config(config)
        QMessageBox.information(self, "已保存", "ASR 接口、熔断策略和文本纠偏设置已保存。")
        self.close()

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.isEditable():
            combo.setCurrentText(str(value))
