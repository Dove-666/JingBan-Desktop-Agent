"""Model-scoped reasoning controls backed by the same resolver as requests."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from ..reasoning import default_reasoning_selection, profile_options, resolve_reasoning
from ..settings_manager import ApiSettings


class ReasoningControls(QWidget):
    changed = Signal()

    def __init__(self, settings, config, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._identity = None
        self._drafts = {}
        self._automatic_drafts = {}
        self._automatic_selection = False
        self._profile_drafts = {}
        self._last_profile = "auto"
        self._metadata = None
        self._loading = False
        self._upgrade_notice = False
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("apiReasoningProfile")
        self.profile_combo.setAccessibleName("思考适配方式")
        for identity, label in profile_options():
            self.profile_combo.addItem(label, identity)
        self.value_combo = QComboBox()
        self.value_combo.setObjectName("apiReasoningEffort")
        self.value_combo.setAccessibleName("模型思考强度")
        self.value_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for combo in (self.profile_combo, self.value_combo):
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.value_label = QLabel("思考强度")
        self.budget_spin = QSpinBox()
        self.budget_spin.setObjectName("apiReasoningBudget")
        self.budget_spin.setAccessibleName("思考Token预算")
        self.budget_label = QLabel("思考预算（Token）")
        self.notice = QLabel()
        self.notice.setObjectName("reasoningCapabilityHint")
        self.notice.setWordWrap(True)
        self.notice.setProperty("role", "hint")
        form = QFormLayout()
        form.addRow("适配方式", self.profile_combo)
        form.addRow(self.value_label, self.value_combo)
        form.addRow(self.budget_label, self.budget_spin)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(self.notice)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.value_combo.currentTextChanged.connect(self._value_changed)
        self.budget_spin.valueChanged.connect(self._value_changed)
        self.set_identity(config.base_url, config.model, initial=config)

    def _state(self):
        value = self.value_combo.currentData()
        if self.value_combo.isEditable() and self.value_combo.currentText() != self.value_combo.itemText(self.value_combo.currentIndex()):
            value = self.value_combo.currentText().strip()
        return {"profile": self.profile_combo.currentData() or "auto", "value": value or "default",
                "budget_tokens": self.budget_spin.value() if self.budget_spin.isVisibleTo(self) else 0}

    def set_identity(self, base_url, model, *, initial=None, refresh=False):
        identity = (base_url.strip().rstrip("/"), model.strip())
        if identity == self._identity and not refresh:
            return
        if self._identity is not None:
            self._drafts[self._identity] = self._state()
            self._automatic_drafts[self._identity] = self._automatic_selection
            while len(self._drafts) > 64:
                dropped = next(iter(self._drafts))
                self._drafts.pop(dropped)
                self._automatic_drafts.pop(dropped, None)
        self._identity = identity
        loader = getattr(self.settings, "load_reasoning_metadata", None)
        self._metadata = loader(*identity) if callable(loader) else None
        state = self._drafts.get(identity)
        automatic = self._automatic_drafts.get(identity, False)
        if state is None and initial is not None:
            state = {"profile": getattr(initial, "reasoning_profile", "auto"), "value": initial.reasoning_effort,
                     "budget_tokens": getattr(initial, "reasoning_budget_tokens", 0)}
            self._metadata = getattr(initial, "reasoning_metadata", None) or self._metadata
            explicit = getattr(self.settings, "has_reasoning_preferences", None)
            active_loader = getattr(self.settings, "load_api_settings", None)
            if callable(explicit) and callable(active_loader):
                automatic = not explicit(*identity) and initial == active_loader()
            else:
                automatic = state["profile"] == "auto" and state["value"] == "default"
            checker = getattr(self.settings, "has_unmigrated_reasoning", None)
            if state["profile"] == "legacy" and callable(checker) and checker():
                state["profile"] = "auto"
                self._upgrade_notice = True
        if state is None:
            loader = getattr(self.settings, "load_reasoning_preferences", None)
            state = loader(*identity) if callable(loader) else None
            automatic = state is None
        if automatic:
            state = default_reasoning_selection(*identity, profile=(state or {}).get("profile", "auto"),
                                                metadata=self._metadata)
        state = state or default_reasoning_selection(*identity, metadata=self._metadata)
        self._automatic_selection = automatic
        self._loading = True
        try:
            with QSignalBlocker(self.profile_combo):
                self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(state["profile"])))
            self._refresh(state["value"], state["budget_tokens"])
        finally:
            self._loading = False

    def _refresh(self, selected="default", budget=0):
        cap = resolve_reasoning(*self._identity, profile=self.profile_combo.currentData() or "auto", metadata=self._metadata)
        self.capability = cap
        self._last_profile = self.profile_combo.currentData() or "auto"
        with QSignalBlocker(self.value_combo), QSignalBlocker(self.budget_spin):
            self.value_combo.clear()
            self.value_combo.setEditable(cap.allow_custom)
            for value, label in zip(cap.values, cap.value_labels):
                self.value_combo.addItem(label, value)
            index = self.value_combo.findData(selected)
            if index < 0 and cap.allow_custom:
                self.value_combo.addItem(selected, selected)
                index = self.value_combo.count() - 1
            self.value_combo.setCurrentIndex(max(0, index))
            if self.value_combo.lineEdit() is not None:
                self.value_combo.lineEdit().setMaxLength(64)
            self.budget_spin.setRange(0 if not cap.budget_required else cap.budget_min, max(0, cap.budget_max))
            self.budget_spin.setSpecialValueText("模型默认" if not cap.budget_required else "")
            self.budget_spin.setValue(budget or (cap.budget_default if cap.budget_required else 0))
        self.value_label.setText("思考模式" if cap.control in ("thinking", "toggle", "budget") else "思考强度")
        notice = cap.notice or cap.label
        if self._upgrade_notice:
            notice += " 升级前的参数仍保留；保存后应用当前模型的设置。"
        self.notice.setText(notice)
        self._show_budget()

    def _show_budget(self):
        value = self.value_combo.currentData()
        visible = self.capability.budget_max > 0 and value not in ("default", "none", "disabled")
        self.budget_label.setVisible(visible)
        self.budget_spin.setVisible(visible)

    def _profile_changed(self):
        if self._loading:
            return
        self._automatic_selection = False
        previous = self._state()
        previous["profile"] = self._last_profile
        self._profile_drafts[(self._identity, self._last_profile)] = previous
        while len(self._profile_drafts) > 256:
            self._profile_drafts.pop(next(iter(self._profile_drafts)))
        restored = self._profile_drafts.get((self._identity, self.profile_combo.currentData()), previous)
        self._refresh(restored["value"], restored["budget_tokens"])
        self.changed.emit()

    def _value_changed(self, *_):
        if self._loading:
            return
        self._automatic_selection = False
        self._show_budget()
        self.changed.emit()

    def api_settings(self, base_url, model, timeout):
        state = self._state()
        return ApiSettings(base_url, model, timeout, state["value"], state["profile"],
                           state["budget_tokens"], self._metadata)
