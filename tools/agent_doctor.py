"""Local Agent diagnostics; --probe uses only independently owned fixtures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import threading


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        del message  # argparse's original message can contain credentials or paths.
        self.exit(2, "参数无效，请使用 --help 查看可用选项。\n")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = _SafeParser(description="Agent 本地诊断；默认仅元数据，不联网、不调用模型、不截图、不更改配置。")
    parser.add_argument("--json", action="store_true", help="输出不含路径和凭据的 JSON 报告")
    parser.add_argument("--probe", action="store_true", help="显式加载隔离 SDK/VM，并回读独立样例文件")
    parser.add_argument("--timeout", type=float, default=20.0, help="整次诊断的时间预算，默认 20 秒，最大 120 秒")
    parser.add_argument("--python", dest="backend_python", help="后端 Python 解释器，报告不输出路径")
    parser.add_argument("--node", dest="node_path", help="Node.js 解释器，报告不输出路径")
    parser.add_argument("--workspace", help="检查工作目录元数据，不创建或读取其中的文件")
    parser.add_argument("--db", dest="database", help="检查数据库文件元数据，不读取聊天记录")
    args = parser.parse_args(argv)
    # Allow execution from another working directory without importing Qt or settings.
    root = str(Path(__file__).absolute().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from desktop_pet.agents.health import DoctorRequest, collect_agent_health
    cancellation = threading.Event()
    previous = {}
    try:
        for name in ("SIGINT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is not None:
                try:
                    previous[signum] = signal.signal(signum, lambda *_: cancellation.set())
                except (ValueError, OSError):
                    pass
        report = collect_agent_health(DoctorRequest(workspace=args.workspace, backend_python=args.backend_python,
            node_path=args.node_path, database=args.database, probe=args.probe, timeout_seconds=args.timeout,
            cancel_event=cancellation))
    except (KeyboardInterrupt, SystemExit):
        cancellation.set()
        return 130
    except Exception:
        if args.json:
            print(json.dumps({"version": 1, "overall": "blocked", "code": "DOCTOR_INVALID_REQUEST"}))
        else:
            print("诊断未启动：参数或本地环境不可用。 [DOCTOR_INVALID_REQUEST]")
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        print("Agent 诊断 · " + ("隔离探测" if args.probe else "仅元数据") + f" · {report.elapsed_ms} 毫秒")
        for check in report.checks:
            print(f"{check.summary} [{check.code}]")
            if check.status in ("blocked", "warning", "unsupported") and check.recommendation:
                print("  " + check.recommendation)
        print("未联网、未调用模型、未截图或操控桌面，未更改生产文件。")
    return 130 if report.overall == "cancelled" else 1 if report.overall == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
