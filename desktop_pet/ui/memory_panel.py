"""Optional Mem0 controls, deliberately separate from conversation history."""

from dataclasses import replace
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QLayout, QPlainTextEdit, QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .styles import PANEL_STYLE


def _plain_label(text="", name="hint"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class MemoryEditDialog(QDialog):
    submitted = Signal(str)

    def __init__(self, memory, parent=None):
        super().__init__(parent)
        self.memory_id = str(memory.get("id", ""))
        self.original = str(memory.get("memory", ""))
        self.setObjectName("memoryEditDialog")
        self.setWindowTitle("编辑 / 纠正长期记忆")
        # A failed edit must leave the configuration tab reachable while its
        # draft remains open for retry.
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label("填写当前完整事实（最多 1200 字）。旧值与更正时间由 Mem0 历史保留；保存使用已配置的 Embedding 服务。"))
        from ..conversations.context import memory_is_active
        if not memory_is_active(memory):
            layout.addWidget(_plain_label("此记录已失效。编辑只修改内容，保留原有效期和失效标记，不会重新启用。"))
        self.text_input = QPlainTextEdit(self.original)
        self.text_input.setObjectName("memoryEditText")
        layout.addWidget(self.text_input, 1)
        self.error_label = _plain_label("", "error")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存纠正")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        for button in self.buttons.buttons():
            button.setMinimumHeight(32)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _submit(self):
        text = self.text_input.toPlainText().strip()
        if not 1 <= len(text) <= 1200:
            self.show_error("请填写 1～1200 字的完整事实；当前输入已保留。")
            return
        if text == self.original:
            self.reject()
            return
        self.show_error("")
        self.submitted.emit(text)

    def show_error(self, text):
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def set_busy(self, busy):
        for button in self.buttons.buttons():
            button.setEnabled(not busy)
        self.save_button.setText("正在保存…" if busy else "保存纠正")
        self.text_input.setReadOnly(busy)


class MemoryPanel(QWidget):
    """All persistence, SDK work and credential access belong to the controller."""

    conversation_requested = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self._edit_dialog = None
        self._saving_settings = False
        self.setObjectName("memoryPanel")
        self.setWindowTitle("鲸鱼娘 · 长期记忆")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(PANEL_STYLE)
        self.setMinimumSize(590, 500)
        self.resize(700, 700)
        layout = QVBoxLayout(self)
        heading = _plain_label("长期记忆", "title")
        layout.addWidget(heading)
        layout.addWidget(_plain_label(
            "Mem0 只提炼用户明确表达的稳定事实与偏好。聊天记录和屏幕截图不会整批写入记忆。"
        ))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._make_memories_tab(), "记忆内容")
        self.tabs.addTab(self._make_settings_tab(), "配置与隐私")
        self.status_label = _plain_label(getattr(controller, "status", ""), "status")
        layout.addWidget(self.status_label)
        self.credentials_status_label = _plain_label(
            getattr(controller, "credentials_status", "凭据状态尚未读取；连接需单独验证。"),
            "memoryCredentialsStatus",
        )
        layout.addWidget(self.credentials_status_label)
        self.error_label = _plain_label(getattr(controller, "error", ""), "error")
        self.error_label.setVisible(bool(getattr(controller, "error", "")))
        layout.addWidget(self.error_label)
        controller.memories_changed.connect(self._render_memories)
        history_loaded = getattr(controller, "history_loaded", None)
        if history_loaded is not None:
            history_loaded.connect(self._show_history)
        updated = getattr(controller, "memory_update_finished", None)
        if updated is not None:
            updated.connect(self._memory_update_finished)
        settings_saved = getattr(controller, "settings_saved", None)
        if settings_saved is not None:
            settings_saved.connect(self._settings_saved)
        readiness_changed = getattr(controller, "readiness_changed", None)
        if readiness_changed is not None:
            readiness_changed.connect(self.readiness_label.setText)
        controller.settings_changed.connect(self._settings_changed)
        controller.busy_changed.connect(self._busy_changed)
        controller.status_changed.connect(self.status_label.setText)
        credentials_changed = getattr(controller, "credentials_status_changed", None)
        if credentials_changed is not None:
            credentials_changed.connect(self.credentials_status_label.setText)
        controller.error_changed.connect(self._show_error)
        pause_changed = getattr(controller, "automatic_pause_changed", None)
        if pause_changed is not None:
            pause_changed.connect(self._runtime_pause_changed)
        self._settings_changed(controller.settings)
        self._render_memories()
        self._busy_changed(getattr(controller, "busy", False))
        for control in self.findChildren(QAbstractButton) + self.findChildren(QLineEdit) + self.findChildren(QComboBox) + self.findChildren(QAbstractSpinBox):
            control.setMinimumHeight(32)

    def _make_memories_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        self.memories_scroll = QScrollArea()
        self.memories_scroll.setObjectName("memoryContentsScroll")
        self.memories_scroll.setWidgetResizable(True)
        self.memories_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(self.memories_scroll)
        contents = QWidget()
        self.memories_scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.pause_checkbox = QCheckBox("暂停自动提炼（本次运行，仍可检索已有记忆）")
        self.pause_checkbox.setObjectName("pauseAutomaticMemory")
        self.pause_checkbox.toggled.connect(self._pause_automatic)
        layout.addWidget(self.pause_checkbox)
        self.runtime_pause_hint = _plain_label()
        layout.addWidget(self.runtime_pause_hint)
        search = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setObjectName("memorySearch")
        self.search_input.setPlaceholderText("按关键词筛选本地记忆")
        self.search_input.setMaxLength(4000)
        self.search_input.setToolTip("只按本地文字筛选，不发送搜索词；聊天使用的语义检索由已保存的检索配置负责。")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._refresh)
        search.addWidget(self.search_input, 1)
        self.refresh_button = QPushButton("搜索 / 刷新")
        self.refresh_button.setObjectName("refreshMemoriesButton")
        self.refresh_button.clicked.connect(self._refresh)
        search.addWidget(self.refresh_button)
        layout.addLayout(search)
        self.memory_list = QListWidget()
        self.memory_list.setObjectName("memoryList")
        self.memory_list.setWordWrap(True)
        self.memory_list.setAccessibleName("Mem0 长期记忆列表")
        self.memory_list.setMinimumHeight(160)
        self.memory_list.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.memory_list, 1)
        self.empty_label = _plain_label()
        layout.addWidget(self.empty_label)
        pager = QHBoxLayout()
        self.memory_count_label = _plain_label()
        self.memory_count_label.setObjectName("memoryCount")
        pager.addWidget(self.memory_count_label, 1)
        self.previous_button = QPushButton("上一页")
        self.previous_button.setObjectName("previousMemoriesButton")
        self.previous_button.clicked.connect(controller_call(self.controller, "previous_page"))
        self.next_button = QPushButton("下一页")
        self.next_button.setObjectName("nextMemoriesButton")
        self.next_button.clicked.connect(controller_call(self.controller, "next_page"))
        pager.addWidget(self.previous_button)
        pager.addWidget(self.next_button)
        layout.addLayout(pager)
        details = QGroupBox("记忆详情")
        self.memory_details_group = details
        details.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        detail_layout = QVBoxLayout(details)
        detail_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.memory_details_label = _plain_label("选择记录查看来源、时间和有效期。")
        self.memory_details_label.setObjectName("memoryDetails")
        detail_layout.addWidget(self.memory_details_label)
        self.source_button = QPushButton("查看来源会话")
        self.source_button.setObjectName("memorySourceConversation")
        self.source_button.clicked.connect(self._open_source_conversation)
        detail_layout.addWidget(self.source_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(details)
        row = QHBoxLayout()
        self.edit_button = QPushButton("编辑 / 纠正记忆")
        self.edit_button.setObjectName("editMemoryButton")
        self.edit_button.clicked.connect(self._edit_memory)
        row.addWidget(self.edit_button)
        self.history_button = QPushButton("更正历史")
        self.history_button.setObjectName("memoryHistoryButton")
        self.history_button.clicked.connect(self._load_history)
        row.addWidget(self.history_button)
        self.delete_button = QPushButton("删除所选记忆")
        self.delete_button.setObjectName("deleteMemoryButton")
        self.delete_button.clicked.connect(self._delete_memory)
        row.addWidget(self.delete_button)
        row.addStretch()
        self.clear_button = QPushButton("清空长期记忆")
        self.clear_button.setObjectName("clearMemoriesButton")
        self.clear_button.clicked.connect(self._clear_memories)
        row.addWidget(self.clear_button)
        layout.addLayout(row)
        layout.addWidget(_plain_label("删除会话不会删除长期记忆；此处删除会调用 Mem0 真正删除对应数据。"))
        return tab

    def _make_settings_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        contents = QWidget()
        scroll.setWidget(contents)
        self._settings_contents = contents
        layout = QVBoxLayout(contents)
        readiness = QGroupBox("首次配置与验证")
        checklist = QVBoxLayout(readiness)
        self.readiness_label = _plain_label(getattr(self.controller, "readiness", "聊天配置、Embedding 和提炼连接尚未在本面板验证。"))
        self.readiness_label.setObjectName("memoryReadiness")
        checklist.addWidget(self.readiness_label)
        checklist.addWidget(_plain_label("先保存配置；本地模式再下载 / 检查模型；随后测试记忆检索。提炼是否成功以实际聊天后的提炼结果为准。"))
        layout.addWidget(readiness)
        self.enabled_checkbox = QCheckBox("启用 Mem0 长期记忆")
        self.enabled_checkbox.setObjectName("memoryEnabled")
        layout.addWidget(self.enabled_checkbox)
        self.automatic_checkbox = QCheckBox("自动提炼用户明确表达的重要偏好、决定和项目进展")
        self.automatic_checkbox.setObjectName("memoryAutomatic")
        layout.addWidget(self.automatic_checkbox)
        self.user_id_label = _plain_label()
        self.user_id_label.setObjectName("memoryUserId")
        self.user_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        llm_group = QGroupBox("记忆提炼模型")
        llm_form = QFormLayout(llm_group)
        self.llm_form = llm_form
        self.use_chat_checkbox = QCheckBox("复用已保存的聊天 API 配置和凭据")
        self.use_chat_checkbox.setObjectName("memoryUseChatModel")
        self.use_chat_checkbox.toggled.connect(self._chat_model_toggled)
        llm_form.addRow(self.use_chat_checkbox)
        self.reuse_chat_hint = _plain_label("沿用「API 设置」中已保存的模型和密钥，无需再次填写。")
        llm_form.addRow(self.reuse_chat_hint)
        self.llm_base_input = self._url_input("memoryLlmBaseUrl")
        self.llm_model_input = QLineEdit()
        self.llm_model_input.setObjectName("memoryLlmModel")
        self.llm_key_input = self._key_input("memoryLlmKey")
        llm_form.addRow("Base URL", self.llm_base_input)
        llm_form.addRow("Model", self.llm_model_input)
        llm_form.addRow("独立 API Key", self.llm_key_input)
        self.clear_llm_key_button = QPushButton("删除独立记忆模型密钥")
        self.clear_llm_key_button.clicked.connect(controller_call(self.controller, "clear_llm_key"))
        llm_form.addRow(self.clear_llm_key_button)
        layout.addWidget(llm_group)
        embedding_group = QGroupBox("记忆检索方式")
        embedding_layout = QVBoxLayout(embedding_group)
        self.embedding_provider_combo = QComboBox()
        self.embedding_provider_combo.setObjectName("memoryEmbeddingProvider")
        self.embedding_provider_combo.addItem("兼容 API 服务 · 需要独立密钥", "openai")
        self.embedding_provider_combo.addItem("本地中文模型 · 无需 Embedding 密钥", "fastembed")
        embedding_layout.addWidget(self.embedding_provider_combo)
        self.remote_embedding_fields = QWidget()
        embedding_form = QFormLayout(self.remote_embedding_fields)
        embedding_form.setContentsMargins(0, 0, 0, 0)
        self.embedding_base_input = self._url_input("memoryEmbeddingBaseUrl")
        self.embedding_model_input = QLineEdit()
        self.embedding_model_input.setObjectName("memoryEmbeddingModel")
        self.embedding_key_input = self._key_input("memoryEmbeddingKey")
        self.dimensions_input = QSpinBox()
        self.dimensions_input.setObjectName("memoryEmbeddingDimensions")
        self.dimensions_input.setRange(1, 65536)
        embedding_form.addRow("Base URL", self.embedding_base_input)
        embedding_form.addRow("Model", self.embedding_model_input)
        embedding_form.addRow("向量维度", self.dimensions_input)
        embedding_form.addRow("Embedding API Key", self.embedding_key_input)
        self.clear_embedding_key_button = QPushButton("删除 Embedding 密钥")
        self.clear_embedding_key_button.clicked.connect(controller_call(self.controller, "clear_embedding_key"))
        embedding_form.addRow(self.clear_embedding_key_button)
        embedding_layout.addWidget(self.remote_embedding_fields)
        self.local_embedding_fields = QWidget()
        local_form = QFormLayout(self.local_embedding_fields)
        local_form.setContentsMargins(0, 0, 0, 0)
        self.local_model_combo = QComboBox()
        self.local_model_combo.setObjectName("memoryLocalEmbeddingModel")
        self.local_model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.local_model_combo.setMinimumContentsLength(18)
        from ..memory.settings import LOCAL_EMBEDDING_MODELS
        for model, dimensions in LOCAL_EMBEDDING_MODELS.items():
            language = "中文" if "-zh-" in model else "多语言" if "multilingual" in model else "英文"
            self.local_model_combo.addItem(f"{language} · {model}（{dimensions} 维）", model)
        local_form.addRow("本地模型", self.local_model_combo)
        self.local_model_hint = _plain_label(
            "首次使用需下载模型。中文 small 模型约 90 MB；下载后检索在本机运行。"
            "记忆提炼仍使用所选聊天模型，可能发送经过筛选的短文本。"
        )
        local_form.addRow(self.local_model_hint)
        self.prepare_model_button = QPushButton("下载 / 检查本地模型")
        self.prepare_model_button.setObjectName("prepareLocalMemoryModelButton")
        self.prepare_model_button.setToolTip("先保存本地模式配置；此操作下载公开模型文件，不上传聊天记录、不启用记忆。")
        self.prepare_model_button.clicked.connect(controller_call(self.controller, "prepare_local_model"))
        local_form.addRow(self.prepare_model_button)
        self.local_preparation_hint = _plain_label("先保存当前模型选择，再下载或检查。")
        local_form.addRow(self.local_preparation_hint)
        embedding_layout.addWidget(self.local_embedding_fields)
        self.embedding_provider_combo.currentIndexChanged.connect(self._embedding_provider_changed)
        self.local_model_combo.currentIndexChanged.connect(self._embedding_provider_changed)
        layout.addWidget(embedding_group)
        self.advanced_button = QPushButton("展开高级参数")
        self.advanced_button.setObjectName("memoryAdvancedButton")
        self.advanced_button.setCheckable(True)
        layout.addWidget(self.advanced_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.advanced_fields = QWidget()
        budget = QFormLayout(self.advanced_fields)
        budget.setContentsMargins(0, 0, 0, 0)
        budget.addRow(self.user_id_label)
        self.timeout_input = QDoubleSpinBox()
        self.timeout_input.setObjectName("memoryTimeout")
        self.timeout_input.setRange(3, 120)
        self.timeout_input.setSuffix(" 秒")
        budget.addRow("请求超时", self.timeout_input)
        self.threshold_input = QDoubleSpinBox()
        self.threshold_input.setObjectName("memoryRelevanceThreshold")
        self.threshold_input.setDecimals(2)
        self.threshold_input.setRange(0, 1)
        self.threshold_input.setSingleStep(0.05)
        budget.addRow("最低相关度", self.threshold_input)
        self.max_results_input = QSpinBox()
        self.max_results_input.setObjectName("memoryMaxResults")
        self.max_results_input.setRange(1, 8)
        budget.addRow("每次最多检索", self.max_results_input)
        self.extraction_context_input = QSpinBox()
        self.extraction_context_input.setObjectName("memoryExtractionContextTokens")
        self.extraction_context_input.setRange(4096, 262144)
        self.extraction_context_input.setSingleStep(1024)
        budget.addRow("Mem0 提炼总 Token 预算", self.extraction_context_input)
        self.extraction_output_input = QSpinBox()
        self.extraction_output_input.setObjectName("memoryExtractionOutputTokens")
        self.extraction_output_input.setRange(32, 1024)
        self.extraction_output_input.setSingleStep(32)
        budget.addRow("Mem0 提炼输出预留", self.extraction_output_input)
        layout.addWidget(self.advanced_fields)
        self.advanced_hint = _plain_label(
            "提炼预算独立于聊天预算。Mem0 2.2.1 自带较长提示词，保守估算约需 4 万 Token；"
            "请选用支持对应上下文窗口的模型。超过预算会拒绝提炼模型请求，不会截断原始事实；"
            "SDK 在此之前可能已执行 Embedding 检索。"
        )
        layout.addWidget(self.advanced_hint)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        self._toggle_advanced(False)
        layout.addWidget(_plain_label(
            "远程检索和记忆提炼会向所配置服务发送必要的短文本；本地检索无需远程 Embedding。"
            "向量数据保存在本机，密钥仅进入系统凭据库。"
            "更换 Embedding 模型或维度前，请先清空旧记忆，避免混用向量空间。"
        ))
        row = QHBoxLayout()
        self.save_button = QPushButton("保存配置")
        self.save_button.setObjectName("saveMemorySettingsButton")
        self.save_button.clicked.connect(self._save_settings)
        row.addWidget(self.save_button)
        self.test_button = QPushButton("测试记忆检索")
        self.test_button.setObjectName("testMemoryConnectionButton")
        self.test_button.setToolTip("测试已保存配置的 Embedding 检索与本地向量库；不会提炼或新增长期记忆。")
        self.test_button.clicked.connect(controller_call(self.controller, "test_connection"))
        row.addWidget(self.test_button)
        outer.addLayout(row)
        layout.addStretch()
        return tab

    @staticmethod
    def _url_input(name):
        field = QLineEdit()
        field.setObjectName(name)
        field.setPlaceholderText("https://api.openai.com/v1")
        return field

    @staticmethod
    def _key_input(name):
        field = QLineEdit()
        field.setObjectName(name)
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        field.setPlaceholderText("留空保留已保存凭据；保存后不回显")
        return field

    def _settings_changed(self, settings):
        self.enabled_checkbox.setChecked(settings.enabled)
        self.automatic_checkbox.setChecked(settings.automatic)
        self._runtime_pause_changed(getattr(self.controller, "automatic_paused", False))
        self.use_chat_checkbox.setChecked(settings.use_chat_model)
        self.user_id_label.setText("本机用户标识：" + str(settings.user_id or "首次启用时生成"))
        self.llm_base_input.setText(settings.llm_base_url)
        self.llm_model_input.setText(settings.llm_model)
        self.embedding_base_input.setText(settings.embedding_base_url)
        self.embedding_model_input.setText(settings.embedding_model)
        self.dimensions_input.setValue(settings.embedding_dimensions)
        self.timeout_input.setValue(settings.timeout_seconds)
        self.threshold_input.setValue(settings.relevance_threshold)
        self.max_results_input.setValue(settings.max_results)
        self.extraction_context_input.setValue(settings.extraction_context_tokens)
        self.extraction_output_input.setValue(settings.extraction_output_tokens)
        provider = getattr(settings, "embedding_provider", "openai")
        self.embedding_provider_combo.setCurrentIndex(max(0, self.embedding_provider_combo.findData(provider)))
        local_model = getattr(settings, "local_embedding_model", "BAAI/bge-small-zh-v1.5")
        self.local_model_combo.setCurrentIndex(max(0, self.local_model_combo.findData(local_model)))
        self._embedding_provider_changed()
        self._chat_model_toggled(settings.use_chat_model)
        if self._saving_settings and not getattr(self.controller, "busy", False):
            self._settings_saved()

    def _settings_saved(self):
        self._saving_settings = False
        self.llm_key_input.clear()
        self.embedding_key_input.clear()

    def _embedding_provider_changed(self, *_args):
        local = self.embedding_provider_combo.currentData() == "fastembed"
        saved = self.controller.settings
        matches = (getattr(saved, "embedding_provider", "openai") == "fastembed"
                   and getattr(saved, "local_embedding_model", "") == self.local_model_combo.currentData())
        self.remote_embedding_fields.setVisible(not local)
        self.local_embedding_fields.setVisible(local)
        self.local_preparation_hint.setVisible(not matches)
        self.prepare_model_button.setEnabled(local and matches and not getattr(self.controller, "busy", False))

    def _toggle_advanced(self, expanded):
        self.advanced_fields.setVisible(expanded)
        self.advanced_hint.setVisible(expanded)
        self.advanced_button.setText("收起高级参数" if expanded else "展开高级参数")

    def _chat_model_toggled(self, use_chat):
        for field in (self.llm_base_input, self.llm_model_input, self.llm_key_input):
            field.setEnabled(not use_chat and not getattr(self.controller, "busy", False))
            self.llm_form.setRowVisible(field, not use_chat)
        self.llm_form.setRowVisible(self.clear_llm_key_button, not use_chat)
        self.reuse_chat_hint.setVisible(use_chat)

    def _save_settings(self):
        extra = {}
        if hasattr(self.controller.settings, "embedding_provider"):
            extra = {
                "embedding_provider": self.embedding_provider_combo.currentData(),
                "local_embedding_model": self.local_model_combo.currentData(),
            }
        settings = replace(
            self.controller.settings,
            enabled=self.enabled_checkbox.isChecked(),
            automatic=self.automatic_checkbox.isChecked(),
            use_chat_model=self.use_chat_checkbox.isChecked(),
            llm_base_url=self.llm_base_input.text().strip(),
            llm_model=self.llm_model_input.text().strip(),
            embedding_base_url=self.embedding_base_input.text().strip(),
            embedding_model=self.embedding_model_input.text().strip(),
            embedding_dimensions=self.dimensions_input.value(),
            timeout_seconds=self.timeout_input.value(),
            relevance_threshold=self.threshold_input.value(),
            max_results=self.max_results_input.value(),
            extraction_context_tokens=self.extraction_context_input.value(),
            extraction_output_tokens=self.extraction_output_input.value(),
            **extra,
        )
        llm_key = self.llm_key_input.text().strip() if not settings.use_chat_model else ""
        embedding_key = self.embedding_key_input.text().strip() if self.embedding_provider_combo.currentData() == "openai" else ""
        # Catch ordinary mistakes before accepting a background save, keeping
        # the user's password draft available for a corrected retry.
        from ..settings_manager import ApiSettings, SettingsError
        try:
            validator = getattr(settings, "validate", None)
            if callable(validator):
                validator()
            else:
                ApiSettings(settings.llm_base_url, settings.llm_model, settings.timeout_seconds).validate(require_model=False)
                ApiSettings(settings.embedding_base_url, settings.embedding_model, settings.timeout_seconds).validate(require_model=False)
        except SettingsError as error:
            self._show_error(str(error))
            return False
        # The controller signals success/failure asynchronously; never read keys back.
        self._saving_settings = True
        result = self.controller.save_settings(settings, llm_key=llm_key, embedding_key=embedding_key)
        if result is False:
            self._saving_settings = False
        return result

    def _pause_automatic(self, paused):
        setter = getattr(self.controller, "set_automatic_paused", None)
        if not callable(setter):
            self._runtime_pause_changed(getattr(self.controller, "automatic_paused", False))
            self._show_error("当前记忆控制器不支持运行时暂停，请退出并重启桌宠加载更新；已保存的配置未改变。")
            return
        if setter(paused) is False:
            self._runtime_pause_changed(getattr(self.controller, "automatic_paused", False))

    def _runtime_pause_changed(self, paused):
        self.pause_checkbox.blockSignals(True)
        self.pause_checkbox.setChecked(bool(paused))
        self.pause_checkbox.blockSignals(False)
        self.runtime_pause_hint.setText(
            "本次运行已暂停自动提炼；保存配置或完成当前任务不会解除暂停。"
            if paused else "本次运行未暂停；是否自动提炼仍由配置页保存的启用与自动提炼开关决定。"
        )

    def _render_memories(self):
        from ..conversations.context import memory_is_active
        selected = self.memory_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.memory_list.clear()
        memories = getattr(self.controller, "memories", [])
        stale = getattr(self.controller, "memory_list_stale", False)
        preferred_id = getattr(self.controller, "memory_preferred_id", None)
        query = getattr(self.controller, "memory_query", self.search_input.text().strip())
        self.empty_label.setText(
            "没有匹配的记忆。可以换个关键词，或清空搜索后刷新。" if query else
            "暂无已保存的长期记忆。聊天历史独立保留，记忆只提炼有价值的稳定事实。"
        )
        for memory in memories:
            memory_id = str(memory.get("id", ""))
            content = str(memory.get("memory") or memory.get("content") or "")
            metadata = memory.get("metadata") or {}
            effective = str(metadata.get("effective_at") or memory.get("updated_at") or memory.get("created_at") or "未记录时间")
            state = str(metadata.get("status", "active"))
            expiration = memory.get("expiration_date")
            state_label = "有效" if state == "active" else state
            if not memory_is_active(memory):
                state_label = "已失效"
            if expiration:
                state_label += f" · 到期 {expiration}"
            item = QListWidgetItem(f"{content}\n{state_label} · {effective}")
            item.setData(Qt.ItemDataRole.UserRole, memory_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, content)
            item.setData(Qt.ItemDataRole.UserRole + 2, memory)
            item.setToolTip("<qt>" + escape(content) + "</qt>")
            self.memory_list.addItem(item)
            if memory_id == selected_id:
                self.memory_list.setCurrentItem(item)
        if self.memory_list.currentItem() is None and preferred_id:
            for index in range(self.memory_list.count()):
                item = self.memory_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == preferred_id:
                    self.memory_list.setCurrentItem(item)
                    break
        if stale and not memories:
            self.empty_label.setText("删除已完成；当前页暂无缓存记录，请刷新以加载其余记忆。")
        self.empty_label.setVisible(not memories)
        total = getattr(self.controller, "memory_total", len(memories))
        matched = getattr(self.controller, "memory_matched_total", len(memories))
        offset = getattr(self.controller, "memory_offset", 0)
        position = f"已加载 {len(memories)} 条（{offset + 1}～{offset + len(memories)}）" if memories else "已加载 0 条"
        summary = f"当前用户共 {total} 条 · 匹配 {matched} 条 · {position}"
        if stale:
            summary = f"缓存待刷新 · 上次加载数已扣除删除项：总计 {total} 条 · 匹配 {matched} 条 · {position}"
        self.memory_count_label.setText(summary)
        self._update_delete_button()
        self._selection_changed()

    def _selected_memory(self):
        item = self.memory_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole + 2) if item is not None else None

    def _selection_changed(self, *_args):
        self._update_delete_button()
        if not hasattr(self, "memory_details_label"):
            return
        memory = self._selected_memory()
        self.source_button.setEnabled(bool(memory and (memory.get("metadata") or {}).get("conversation_id")))
        if not memory:
            self.memory_details_label.setText("选择记录查看来源、时间和有效期。")
            self.memory_details_label.setMinimumHeight(0)
            return
        metadata = memory.get("metadata") or {}
        source = "手动纠正" if metadata.get("correction_source") == "manual_correction" else "用户明确改口" if metadata.get("correction_source") == "explicit_user_correction" else "自动提炼" if metadata.get("trigger") != "explicit" and metadata.get("trigger") else "用户明确事实" if metadata.get("source") == "explicit_user_fact" else "未记录"
        created = str(memory.get("created_at") or "未记录")
        updated = str(memory.get("updated_at") or "未记录")
        effective = str(metadata.get("effective_at") or "未记录")
        expiration = str(memory.get("expiration_date") or "未设置")
        conversation = str(metadata.get("conversation_id") or "未记录")
        from ..conversations.context import memory_is_active
        state = "有效" if memory_is_active(memory) else "已失效（编辑内容不会重新启用）"
        self.memory_details_label.setText(f"来源：{source} · 会话：{conversation}\n创建：{created}\n更新：{updated}\n生效：{effective}\n有效期至：{expiration} · {state}")
        # Qt may otherwise shrink a word-wrapped label below its explicit
        # line breaks when the list competes for space. Let the outer content
        # scroll instead of hiding the provenance/time rows.
        self.memory_details_label.setMinimumHeight(self.memory_details_label.fontMetrics().lineSpacing() * 5 + 8)
        self.memory_details_group.layout().invalidate()
        self.memory_details_group.layout().activate()
        self.memory_details_group.updateGeometry()
        self.memories_scroll.widget().layout().activate()

    def _open_source_conversation(self):
        memory = self._selected_memory()
        conversation = (memory.get("metadata") or {}).get("conversation_id") if memory else None
        if isinstance(conversation, str) and conversation.strip():
            self.conversation_requested.emit(conversation.strip())

    def _update_delete_button(self):
        if hasattr(self, "delete_button"):
            self.delete_button.setEnabled(
                self.memory_list.currentItem() is not None and not getattr(self.controller, "busy", False)
            )
            self.edit_button.setEnabled(self.delete_button.isEnabled())
            self.history_button.setEnabled(self.delete_button.isEnabled())
            idle = not getattr(self.controller, "busy", False)
            self.previous_button.setEnabled(idle and getattr(self.controller, "memory_offset", 0) > 0)
            self.next_button.setEnabled(idle and getattr(self.controller, "memory_has_more", False))

    def _refresh(self):
        if not getattr(self.controller, "busy", False):
            self.controller.refresh(self.search_input.text().strip())

    def _delete_memory(self):
        item = self.memory_list.currentItem()
        if item is None or getattr(self.controller, "busy", False):
            return
        # The confirmation runs a nested event loop; a refresh can destroy the
        # native list item while it is open. Preserve the logical target now.
        memory_id = str(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self, "删除长期记忆", "确定从 Mem0 永久删除所选记忆？此操作不删除聊天记录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete_memory(memory_id)

    def _edit_memory(self):
        memory = self._selected_memory()
        if memory is None or getattr(self.controller, "busy", False):
            return
        if self._edit_dialog is not None:
            self._edit_dialog.show()
            self._edit_dialog.raise_()
            return
        dialog = MemoryEditDialog(memory, self)
        self._edit_dialog = dialog
        def submit(text):
            if self.controller.update_memory(dialog.memory_id, text, expected_text=dialog.original) is False:
                dialog.show_error("当前有后台操作，请稍后重试；输入已保留。")
        dialog.submitted.connect(submit)
        def finished():
            if self._edit_dialog is dialog:
                self._edit_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.show()

    def _memory_update_finished(self, memory_id, success, error):
        dialog = self._edit_dialog
        if dialog is None or dialog.memory_id != memory_id:
            return
        if success:
            dialog.accept()
        else:
            dialog.show_error(error + "\n纠正尚未保存，输入已保留，可修正配置后重试。")

    def _load_history(self):
        item = self.memory_list.currentItem()
        if item is not None and not getattr(self.controller, "busy", False):
            self.controller.load_history(str(item.data(Qt.ItemDataRole.UserRole)))

    def _show_history(self, events):
        dialog = QDialog(self)
        dialog.setWindowTitle("Mem0 记忆更正历史")
        dialog.resize(620, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_plain_label("UPDATE 的旧值已被替代，当前聊天只检索最新有效版本。以下为 Mem0 实际保存的历史。"))
        content = QPlainTextEdit()
        content.setReadOnly(True)
        parts = []
        for event in events:
            timestamp = event.get("updated_at") or event.get("created_at") or "未记录时间"
            lines = [f"{event.get('event', '')} · {timestamp}"]
            if event.get("old_memory"):
                lines.append("旧值（已替代）：" + str(event["old_memory"]))
            if event.get("new_memory"):
                lines.append("当时保存的值：" + str(event["new_memory"]))
            parts.append("\n".join(lines))
        content.setPlainText("\n\n".join(parts) or "这条旧记录没有可用的 Mem0 历史。")
        layout.addWidget(content, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _clear_memories(self):
        if getattr(self.controller, "busy", False):
            return
        answer = QMessageBox.question(
            self, "清空长期记忆", "永久删除当前用户的所有 Mem0 长期记忆？聊天记录仍然保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.clear_memories()

    def _busy_changed(self, busy):
        for kind in (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox):
            for field in self._settings_contents.findChildren(kind):
                field.setEnabled(not busy)
        for button in (
            self.refresh_button, self.clear_button, self.save_button, self.test_button,
            self.clear_llm_key_button, self.clear_embedding_key_button,
            self.prepare_model_button, self.embedding_provider_combo, self.local_model_combo,
        ):
            button.setEnabled(not busy)
        self._update_delete_button()
        self._chat_model_toggled(self.use_chat_checkbox.isChecked())
        self._embedding_provider_changed()
        if self._edit_dialog is not None:
            self._edit_dialog.set_busy(busy)

    def _show_error(self, message):
        if message:
            self._saving_settings = False
        for field in (self.llm_key_input, self.embedding_key_input):
            if field.text().strip():
                message = message.replace(field.text().strip(), "[密钥已隐藏]")
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def present(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._refresh()

    def closeEvent(self, event):
        # Password fields never become visible; clear pending sensitive drafts on hide.
        self.llm_key_input.clear()
        self.embedding_key_input.clear()
        self.hide()
        event.ignore()


def controller_call(controller, name):
    """Discard Qt's checked argument rather than sending it into SDK operations."""
    return lambda checked=False: getattr(controller, name)()
