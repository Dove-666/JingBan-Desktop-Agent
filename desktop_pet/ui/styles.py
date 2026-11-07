"""Small shared palette for the ordinary, non-topmost utility windows."""

PANEL_STYLE = """
QWidget { color: #202734; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog, QWidget#debugPanel, QWidget#chatWindow { background: #ffffff; }
QLabel#title { font-size: 23px; font-weight: 600; color: #202734; }
QLabel#subtitle, QLabel#hint { color: #596575; }
QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
QLabel#scaleValue { color: #344054; font-size: 25px; font-weight: 600; }
QLabel#status { background: #f3f5f8; border-radius: 8px; padding: 10px; color: #485467; }
QLabel#error { background: #fff0ed; border-radius: 8px; padding: 9px; color: #9b3d30; }
QGroupBox { background: white; border: 1px solid #dce3ec; border-radius: 10px;
            margin-top: 13px; padding: 17px 15px 14px 15px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; }
QTabWidget::pane { border: 1px solid #dce3ec; border-radius: 10px; background: #ffffff; }
QTabBar::tab { padding: 11px 22px; margin-right: 5px; color: #596575; border: 0; background: #f7f8fa;
              border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:selected { color: #202734; background: #e9eef5; font-weight: 600; }
QTabBar::tab:hover:!selected { background: #eef2f6; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox { background: white;
    border: 1px solid #cbd5e2; border-radius: 7px; padding: 8px;
    selection-background-color: #d5e5f8; selection-color: #183b65; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #5278a8; }
QComboBox QAbstractItemView { background: #ffffff; color: #202734;
    border: 1px solid #cbd5e2; selection-background-color: #e6ecf3; selection-color: #202734; }
QPushButton, QToolButton { background: #f7f8fa; border: 1px solid #cbd5e2; border-radius: 7px;
              padding: 8px 15px; color: #344054; }
QPushButton:hover, QToolButton:hover { background: #eef2f6; border-color: #aebccd; }
QPushButton:pressed, QToolButton:pressed { background: #e5eaf1; }
QPushButton:focus, QToolButton:focus { border: 2px solid #5278a8; }
QPushButton:disabled, QToolButton:disabled { color: #8490a1; background: #f7f8fa; border-color: #e1e7ee; }
QPushButton#primaryButton { color: white; background: #253449; border-color: #253449; }
QPushButton#primaryButton:hover { background: #172334; }
QPushButton#primaryButton:disabled { color: #738196; background: #e5eaf1; border-color: #e5eaf1; }
QSlider::groove:horizontal { height: 5px; background: #e1e7ee; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #7389a5; border-radius: 2px; }
QSlider::handle:horizontal { width: 17px; margin: -6px 0; background: #344b69;
                            border: 2px solid white; border-radius: 10px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #ffffff; }
QScrollBar:vertical { width: 8px; background: #f7f8fa; margin: 0; }
QScrollBar::handle:vertical { background: #c3cbd7; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #9ca8b8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu, QToolTip { background: #ffffff; color: #202734; border: 1px solid #d3dce7; padding: 5px; }
QMenu::item:selected { background: #eef2f6; }

/* Scope the larger reading type to chat; compact settings keep their layout. */
QWidget#chatPanel QLabel#sectionTitle { font-size: 18px; color: #202734; }
QTextBrowser#chatTranscript { font-size: 16px; border-color: #dce5ee;
    border-radius: 12px; padding: 12px; background: #ffffff; }
QPlainTextEdit#chatInput { font-size: 15px; border-radius: 11px; padding: 11px;
    background: #ffffff; border-color: #bed1df; }
QPlainTextEdit#chatInput:focus { border-color: #2b8eaa; }
QWidget#conversationSidebar { background: #f7f8fa; border-radius: 11px; }
QListWidget#conversationList { background: transparent; border: none; outline: none; }
QListWidget#conversationList::item { padding: 2px 10px; border-radius: 7px; color: #485467; }
QListWidget#conversationList::item:hover { background: #eef2f6; }
QListWidget#conversationList::item:selected { background: #e6ecf3; color: #202734; }
QLabel#historyEmptyState { color: #70859a; padding: 12px; }
QLineEdit#conversationSearch { border-radius: 8px; padding: 8px; }
QListWidget#conversationList:focus { border: 1px solid #2b8eaa; border-radius: 8px; }
QPushButton#newConversationButton, QPushButton#toggleHistoryButton,
QToolButton#chatToolsButton { padding: 7px 10px; border: 1px solid #cfdee9;
    border-radius: 7px; background: #f7fafc; color: #36536d; }
QPushButton#newConversationButton:hover, QPushButton#toggleHistoryButton:hover,
QToolButton#chatToolsButton:hover { background: #e6f0f6; }
QPushButton#toggleHistoryButton:checked { background: #dcecf4; color: #17687f; }
QMenu#chatToolsMenu { padding: 6px; background: white; border: 1px solid #cfdee9; }
QMenu#chatToolsMenu QPushButton { min-width: 148px; padding: 10px 14px;
    text-align: left; background: transparent; border: none; }
QMenu#chatToolsMenu QPushButton:hover { background: #eaf3f8; }
QMenu#chatToolsMenu QPushButton[destructive="true"] { color: #9b493e; }
QLabel#chatStatus { color: #647d90; font-size: 12px; }
QMenu#chatToolsMenu QLabel#contextUsage { color: #647d90; font-size: 12px; padding: 8px 14px; }
QLabel#screenEventDetails { background: #edf7f2; border: 1px solid #cfe4da;
    border-radius: 8px; padding: 11px; color: #315b4c; }
"""

# Shared by the pet and tray menus as well as native recovery views.
MENU_STYLE = PANEL_STYLE + """
QMenu { border-radius: 10px; padding: 5px; }
QMenu::item { padding: 7px 22px 7px 12px; border-radius: 6px; }
QMenu::separator { height: 1px; background: #e6eaf0; margin: 5px 8px; }
"""
