"""Shared copy for the compact native Agent permission selectors."""

from PySide6.QtCore import Qt


APPROVAL_OPTIONS = (
    ("ask", "请求批准", "联网、修改工作区外文件及执行命令前询问"),
    ("auto", "帮我批准", "由模型审查，发现风险时询问；会增加模型请求"),
    ("full", "完全访问权限", "自动执行已启用的工具，不逐次询问"),
)


def populate_approval_selector(selector):
    selector.setAccessibleName("Agent 操作权限")
    selector.setMinimumHeight(36)
    for value, label, detail in APPROVAL_OPTIONS:
        selector.addItem(label, value)
        selector.setItemData(selector.count() - 1, detail, Qt.ItemDataRole.ToolTipRole)


def approval_description(mode):
    return next((detail for value, _label, detail in APPROVAL_OPTIONS if value == mode), APPROVAL_OPTIONS[1][2])
