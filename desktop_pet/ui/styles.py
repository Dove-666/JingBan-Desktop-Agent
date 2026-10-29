"""Small shared palette for the ordinary, non-topmost utility windows."""

PANEL_STYLE = """
QWidget { color: #23334b; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog, QWidget#debugPanel, QWidget#chatWindow { background: #f4f7fb; }
QLabel#title { font-size: 23px; font-weight: 600; color: #18314e; }
QLabel#subtitle, QLabel#hint { color: #60758c; }
QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
QLabel#scaleValue { color: #17687f; font-size: 25px; font-weight: 600; }
QLabel#status { background: #e8f2f7; border-radius: 8px; padding: 10px; color: #31586d; }
QLabel#error { background: #fff0ed; border-radius: 8px; padding: 9px; color: #9b3d30; }
QGroupBox { background: white; border: 1px solid #dde6ee; border-radius: 10px;
            margin-top: 13px; padding: 17px 15px 14px 15px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; }
QTabWidget::pane { border: 1px solid #dde6ee; border-radius: 10px; background: #fafcff; }
QTabBar::tab { padding: 11px 22px; margin-right: 5px; color: #60758c; border: 0;
              border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:selected { color: #17687f; background: #e1eff5; font-weight: 600; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox { background: white;
    border: 1px solid #cddbe7; border-radius: 7px; padding: 8px; selection-background-color: #bae1ee; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #2b8eaa; }
QPushButton { background: #eef4f8; border: 1px solid #cddbe7; border-radius: 7px;
              padding: 8px 15px; color: #36536d; }
QPushButton:hover { background: #dfedf4; border-color: #8eb7c7; }
QPushButton:pressed { background: #cfe3ed; }
QPushButton:disabled { color: #9aa8b7; background: #eff2f6; border-color: #e3e8ed; }
QPushButton#primaryButton { color: white; background: #197d98; border-color: #197d98; }
QPushButton#primaryButton:hover { background: #126d85; }
QPushButton#primaryButton:disabled { background: #a7c6ce; border-color: #a7c6ce; }
QSlider::groove:horizontal { height: 5px; background: #dce6ee; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5da8bc; border-radius: 2px; }
QSlider::handle:horizontal { width: 17px; margin: -6px 0; background: #197d98;
                            border: 2px solid white; border-radius: 10px; }
"""
