r"""Development-only iroh peer. This is a test client, not a mobile application.

Examples (PowerShell, after installing requirements-remote.txt):
  .venv\Scripts\python tools/remote_peer.py --ticket-file ticket.txt pair
  .venv\Scripts\python tools/remote_peer.py --ticket-file ticket.txt status
  .venv\Scripts\python tools/remote_peer.py --ticket-file ticket.txt pet.command --params-file command.json
  .venv\Scripts\python tools/remote_peer.py --ticket-file ticket.txt image.upload --image-file sample.png
  .venv\Scripts\python tools/remote_peer.py --ticket-file ticket.txt sync.pull --params-file sync.json --output-file result.json

Ticket, pairing code and private identity are read from stdin/files, never from
command-line secrets. Responses containing chat/image data are only written to
an explicitly selected output file; console output is a safe summary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop_pet.remote.protocol import ProtocolError, validate_request
from desktop_pet.remote.transport import IrohPeerClient, TransportError


def _private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def _identity(path: Path) -> str:
    if not path.exists():
        try:
            _private_file(path, secrets.token_hex(32))
        except FileExistsError:
            pass
    value = _text_file(path, 256)
    try:
        if len(value) != 64 or len(bytes.fromhex(value)) != 32:
            raise ValueError
    except ValueError:
        raise TransportError("Invalid private identity file.") from None
    return value


def _json_file(path: Path) -> dict:
    # All data files are bounded before parsing, matching the network envelope.
    if path.stat().st_size > 12 * 1024 * 1024:
        raise ValueError("Input file exceeds the permitted size.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Input JSON must be an object.")
    return value


def _text_file(path: Path, limit: int) -> str:
    if path.stat().st_size > limit:
        raise ValueError("Input file exceeds the permitted size.")
    return path.read_text(encoding="utf-8-sig").strip()


def _input(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline(16 * 1024).strip()


def _build_request(args) -> dict:
    if args.request_file:
        return validate_request(_json_file(args.request_file))
    params = _json_file(args.params_file) if args.params_file else {}
    if args.method == "pair":
        code = (_text_file(args.pair_code_file, 256)
                if args.pair_code_file else _input("Pair code: "))
        params = {**params, "code": code, "name": args.name}
    if args.image_file:
        if args.method != "image.upload":
            raise ValueError("--image-file requires image.upload.")
        if args.image_file.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Image exceeds the 4 MiB limit.")
        data = args.image_file.read_bytes()
        suffix = args.image_file.suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
        if mime is None:
            raise ValueError("Use a PNG, JPEG or WebP image.")
        params = {**params, "filename": args.image_file.name,
                  "data_base64": base64.b64encode(data).decode("ascii"),
                  "mime_type": mime, "sha256": hashlib.sha256(data).hexdigest()}
    return validate_request({"id": str(uuid4()), "method": args.method, "params": params})


async def _execute(args, ticket: str, value: dict, secret_hex: str) -> int:
    async with IrohPeerClient(secret_hex) as client:
        await client.connect(ticket)
        response = await client.request(value)
        if args.output_file:
            # Exclusive creation avoids silently overwriting a previous export.
            _private_file(args.output_file, json.dumps(response, ensure_ascii=False, indent=2))
        summary = {"id": response["id"], "ok": response["ok"],
                   "connection_type": client.connection_type,
                   "relay_configured": client.public_relay_configured,
                   "response_saved": bool(args.output_file)}
        if not response["ok"]:
            # Server codes are fixed machine-readable values, not raw content.
            code = response.get("error", {}).get("code", "request_failed")
            summary["error_code"] = code if isinstance(code, str) and code.isidentifier() else "request_failed"
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if response["ok"] else 1


def main() -> int:
    default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AURA" / "remote-peer"
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("method", nargs="?", default="status",
                        help="Any server method; for example pair, status, pet.command, sync.pull, chat.send, image.upload")
    parser.add_argument("--ticket-file", type=Path, help="Endpoint ticket file; otherwise read one stdin line")
    parser.add_argument("--identity-file", type=Path, default=default_data / "identity.key",
                        help="Persistent private development identity; never printed")
    parser.add_argument("--pair-code-file", type=Path, help="Pair code file; otherwise securely prompt/read stdin")
    parser.add_argument("--name", default="Development peer")
    parser.add_argument("--params-file", type=Path, help="JSON method params")
    parser.add_argument("--request-file", type=Path, help="Complete JSON request, including id, method, params")
    parser.add_argument("--image-file", type=Path, help="PNG/JPEG/WebP input for image.upload")
    parser.add_argument("--output-file", type=Path, help="New private file for the complete response")
    args = parser.parse_args()
    try:
        ticket = (_text_file(args.ticket_file, 16 * 1024)
                  if args.ticket_file else _input("Endpoint ticket: "))
        if len(ticket) > 16 * 1024:
            raise ValueError("Endpoint ticket exceeds the permitted size.")
        value = _build_request(args)
        return asyncio.run(_execute(args, ticket, value, _identity(args.identity_file)))
    except (TransportError, ProtocolError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, UnicodeError):
        print("Could not read, validate or save the development request.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
