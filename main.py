"""Windows entry point: python main.py."""

import sys


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    # Windowed Windows bundles have no console streams. Dependencies may still
    # write diagnostic messages, so keep those writes safe without a console.
    if getattr(sys, "frozen", False):
        import os
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # Chromium chooses its rendering backend during initialization, before a
    # chat window exists. Apply our Windows compatibility policy first.
    from desktop_pet.web_rendering import configure_webengine_rendering
    configure_webengine_rendering()
    try:
        from desktop_pet.app import main
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith(("PySide6", "openai", "keyring")):
            raise
        message = (
            "缺少鲸伴运行依赖，请先安装项目依赖：\n"
            "python -m pip install -r requirements.txt\n\n"
            "如果已经安装，请确认使用了项目 .venv 中的 Python。"
        )
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "鲸伴启动失败", 0x10)
        sys.exit(1)
    sys.exit(main())

