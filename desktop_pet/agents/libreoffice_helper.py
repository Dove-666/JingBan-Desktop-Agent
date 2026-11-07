"""Fixed private LibreOffice/PDF helper; no model API or executable selection."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

FILTERS = {".docx": "writer_pdf_Export", ".xlsx": "calc_pdf_Export", ".pptx": "impress_pdf_Export"}


def main():
    # Host creates this fixed request in a fresh owned Job directory. No model
    # code, arbitrary CLI switches, user profile or parent output is accepted.
    if len(sys.argv) != 2: return 2
    request_path = Path(sys.argv[1]).resolve(strict=True)
    root = request_path.parent
    result = {"schema": "aura.libreoffice.helper.v1", "passed": False, "error": "LIBREOFFICE_HELPER_FAILED"}
    try:
        if request_path.stat().st_size > 16384: raise ValueError()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if set(request) != {"schema", "runtime", "pdftoppm", "suffix", "source_sha256"} or request["schema"] != "aura.libreoffice.request.v1":
            raise ValueError()
        suffix = request["suffix"]
        if suffix not in FILTERS: raise ValueError()
        runtime, pdftoppm = Path(request["runtime"]), Path(request["pdftoppm"])
        source = root / ("source" + suffix)
        if hashlib.sha256(source.read_bytes()).hexdigest() != request["source_sha256"]: raise ValueError()
        profile = root / "profile"; output = root / "output"; temporary = root / "temporary"
        profile.mkdir(); output.mkdir(); temporary.mkdir()
        (profile / "user").mkdir()
        # Highest macro security in an entirely new profile, in addition to the
        # unchanged host inspect_package rejection of active/external content.
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Common/Internet/UpdateCheck">'
            '<prop oor:name="AutoCheckEnabled" oor:op="fuse"><value>false</value></prop></item></oor:items>', encoding="utf-8")
        environment = {"SystemRoot": r"C:\Windows", "PATH": os.pathsep.join((str(runtime / "program"), str(pdftoppm.parent), r"C:\Windows\System32")),
            "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
            "HOME": str(root), "USERPROFILE": str(root), "TEMP": str(temporary), "TMP": str(temporary),
            "SAL_DISABLE_OPENCL": "1", "SAL_USE_VCLPLUGIN": "svp", "NO_COLOR": "1"}
        for directory in (root / "appdata", root / "localappdata"): directory.mkdir()
        # Process-local error mode only. Never acknowledge system/Office UI.
        kernel = ctypes.WinDLL("kernel32")
        kernel.SetErrorMode.argtypes = [ctypes.c_uint32]; kernel.SetErrorMode.restype = ctypes.c_uint32
        kernel.SetErrorMode(0x8003)
        conversion = subprocess.run([str(runtime / "program" / "soffice.com"),
            "-env:UserInstallation=" + profile.as_uri(), "--headless", "--norestore", "--nodefault",
            "--nofirststartwizard", "--convert-to", "pdf:" + FILTERS[suffix], "--outdir", str(output), str(source)],
            cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=0x08000000)
        pdf = output / "source.pdf"
        if conversion.returncode or not pdf.is_file():
            result["error"] = "LIBREOFFICE_CONVERT_FAILED"; return 3
        if not 0 < pdf.stat().st_size <= 32 * 1024**2:
            result["error"] = "LIBREOFFICE_PDF_LIMIT"; return 4
        from pypdf import PdfReader
        reader = PdfReader(pdf, strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
            result["error"] = "LIBREOFFICE_PDF_LIMIT"; return 4
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text) > 200000: result["error"] = "LIBREOFFICE_TEXT_LIMIT"; return 4
        previews = []
        for index in range(len(reader.pages)):
            target = output / ("page-%02d" % (index + 1))
            preview = subprocess.run([str(pdftoppm), "-f", str(index + 1), "-l", str(index + 1),
                "-singlefile", "-scale-to", "1800", "-png", str(pdf), str(target)], cwd=root, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=0x08000000)
            image = target.with_suffix(".png")
            if preview.returncode or not image.is_file() or image.stat().st_size > 8 * 1024**2:
                result["error"] = "LIBREOFFICE_PREVIEW_FAILED"; return 5
            previews.append(image.name)
        if sum((output / name).stat().st_size for name in previews) > 32 * 1024**2:
            result["error"] = "LIBREOFFICE_PREVIEW_LIMIT"; return 5
        if hashlib.sha256(source.read_bytes()).hexdigest() != request["source_sha256"]:
            result["error"] = "LIBREOFFICE_SOURCE_CHANGED"; return 6
        result = {"schema": "aura.libreoffice.helper.v1", "passed": True, "pages": len(reader.pages),
            "text": text, "pdf": "source.pdf", "previews": previews, "source_sha256": request["source_sha256"],
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}
        return 0
    except Exception:
        return 7
    finally:
        # Fixed codes only. Raw subprocess output, filenames and errors never
        # flow to logs; this data stays in the temporary host-owned directory.
        (root / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__": raise SystemExit(main())
