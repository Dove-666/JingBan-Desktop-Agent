"""Windows entry point: python main.py."""

import sys


if __name__ == "__main__":
    try:
        from desktop_pet.app import main
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith(("PySide6", "openai", "keyring")):
            raise
        message = (
            "缺少桌宠运行依赖，请先安装项目依赖：\n"
            "python -m pip install -r requirements.txt\n\n"
            "如果已经安装，请确认使用了项目 .venv 中的 Python。"
        )
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "桌宠启动失败", 0x10)
        sys.exit(1)
    sys.exit(main())

