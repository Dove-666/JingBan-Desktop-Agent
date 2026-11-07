"""Typed Office-to-PDF rendering in a private, owned Microsoft COM process.

No model-supplied COM methods, shell, macros, links or existing Office sessions.
The helper's Windows Job kills only the freshly identified Office process if
the host cancels or the helper is terminated during a blocking COM call.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # Trusted code only; -I omits the script directory.
from safe_files import MAX_DOCUMENT_BYTES

_OFFICE = {".docx": ("Word.Application", "WINWORD.EXE"),
           ".xlsx": ("Excel.Application", "EXCEL.EXE"),
           ".pptx": ("PowerPoint.Application", "POWERPNT.EXE")}
_SAFE_FORMULAS = frozenset({"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTBLANK", "ROUND", "ROUNDUP", "ROUNDDOWN",
    "ABS", "IF", "IFS", "IFERROR", "IFNA", "AND", "OR", "NOT", "MEDIAN", "STDEV", "STDEV.S", "STDEV.P", "VAR.S", "VAR.P",
    "COUNTIF", "COUNTIFS", "SUMIF", "SUMIFS", "SUMPRODUCT", "CONCAT", "CONCATENATE", "TEXT", "LEFT", "RIGHT", "MID", "LEN",
    "TRIM", "LOWER", "UPPER", "SUBSTITUTE", "REPLACE", "VALUE", "DATE", "YEAR", "MONTH", "DAY", "DAYS", "DATEDIF",
    "INT", "MOD", "POWER", "SQRT", "SIGN", "PRODUCT", "QUOTIENT", "CEILING", "FLOOR", "CEILING.MATH", "FLOOR.MATH",
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "LOOKUP", "INDEX", "MATCH", "XMATCH", "ROW", "ROWS", "COLUMN", "COLUMNS",
    "ISNUMBER", "ISTEXT", "ISBLANK", "ISERROR", "ISNA", "TRUE", "FALSE", "CHOOSE", "FILTER", "SORT", "SORTBY", "UNIQUE"})
_ERRORS = {"OFFICE_UNSUPPORTED_FORMAT", "OFFICE_UNSAFE_PACKAGE", "OFFICE_ACTIVE_CONTENT_REJECTED", "OFFICE_EXTERNAL_LINK_REJECTED",
    "OFFICE_UNSUPPORTED_FORMULA", "OFFICE_RUNTIME_UNAVAILABLE", "OFFICE_BUSY", "OFFICE_OWNERSHIP_UNCONFIRMED", "OFFICE_ISOLATION_UNAVAILABLE",
    "OFFICE_OPEN_FAILED", "OFFICE_RENDER_FAILED", "OFFICE_TIMEOUT", "OFFICE_CANCELLED", "OFFICE_PROTOCOL", "OFFICE_PDF_INVALID",
    "OFFICE_PDF_LIMIT", "OFFICE_SOURCE_CHANGED", "OFFICE_INVALID_ARGUMENT", "OFFICE_PROCESS_UNAVAILABLE",
    "OFFICE_STARTUP_INTERACTION_REQUIRED", "OFFICE_CLEANUP_UNCONFIRMED"}


class OfficeRenderError(ValueError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else "OFFICE_RENDER_FAILED"
        super().__init__(self.code)


def _passive_web_link(value):
    try:
        url = urlsplit(value)
        return (isinstance(value, str) and len(value) <= 4096 and url.scheme.casefold() in {"http", "https"}
                and bool(url.hostname) and not url.username and not url.password and not re.search(r"[\s\\\x00-\x1f]", value))
    except (ValueError, TypeError):
        return False


def inspect_package(raw: bytes, suffix: str):
    """Reject active/external features before an Office parser is started."""
    if suffix not in _OFFICE:
        raise OfficeRenderError("OFFICE_UNSUPPORTED_FORMAT")
    try:
        from document_tools import DocumentTools
        with DocumentTools._archive(raw) as archive:
            required = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[suffix]
            if required not in archive.namelist() or "[Content_Types].xml" not in archive.namelist():
                raise OfficeRenderError("OFFICE_UNSAFE_PACKAGE")
            for item in archive.infolist():
                name = item.filename.casefold()
                passive_printer = bool(re.fullmatch(r"(?:word|xl|ppt)/printersettings/printersettings\d+\.bin", name))
                if ("vba" in name or "/activex/" in name or "/embeddings/" in name or "macrosheet" in name
                        or "/webextensions/" in name or "/customui/" in name
                        or (name.endswith(".bin") and not passive_printer)):
                    raise OfficeRenderError("OFFICE_ACTIVE_CONTENT_REJECTED")
                if any(part in name for part in ("/externallinks/", "/querytables/", "/connections.xml", "/attachedtoolbars")):
                    raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
                if not name.endswith((".xml", ".rels")):
                    continue
                data = archive.read(item)
                if len(data) > 16 * 1024 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise OfficeRenderError("OFFICE_UNSAFE_PACKAGE")
                root = ET.fromstring(data)
                field_instructions = []
                for node in root.iter():
                    if ".macroenabled." in str(node.attrib.get("ContentType", "")).casefold():
                        raise OfficeRenderError("OFFICE_ACTIVE_CONTENT_REJECTED")
                    if node.attrib.get("TargetMode", "").casefold() == "external":
                        # Plain webpage references are inert on open/export;
                        # resources, templates, OLE and workbook links are not.
                        relation = node.attrib.get("Type", "")
                        if relation.endswith("/hyperlink") and _passive_web_link(node.attrib.get("Target", "")):
                            continue
                        raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
                    for attr, value in node.attrib.items():
                        if attr.rsplit("}", 1)[-1].casefold() in {"src", "href", "target"} and re.match(r"(?:https?:|ftp:|file:|\\\\)", value, re.I):
                            raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
                    tag = node.tag.rsplit("}", 1)[-1]
                    if tag == "instrText":
                        field_instructions.append(node.text or "")
                    if tag == "fldSimple":
                        field_instructions += [value for attr, value in node.attrib.items() if attr.rsplit("}", 1)[-1] == "instr"]
                    if suffix == ".xlsx" and tag in {"f", "definedName"}:
                        formula = node.text or ""
                        # Excel can call XLL/COM add-ins from an otherwise normal
                        # workbook. Only deterministic built-ins are rendered.
                        formula = re.sub(r'"(?:[^"]|"")*"', '""', formula)
                        functions = re.findall(r"(?<![\w.])([^\W\d][\w.]*)\s*\(", formula)
                        if any(function.upper().removeprefix("_XLFN.") not in _SAFE_FORMULAS for function in functions):
                            raise OfficeRenderError("OFFICE_UNSUPPORTED_FORMULA")
                        if re.search(r"\|[^!]*!|\[[^]]+\.(?:xlsx?|xlsm|xlsb)\]", formula, re.I):
                            raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
                instructions = "".join(field_instructions)
                commands = re.sub(r'"(?:[^"]|"")*"', '""', instructions)
                if re.search(r"\b(?:DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK)\b", commands, re.I):
                    raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
                for link in re.finditer(r'\bHYPERLINK\s+(?:"([^"]+)"|(\S+))', instructions, re.I):
                    address = link[1] or link[2]
                    if address != "\\l" and not _passive_web_link(address):
                        raise OfficeRenderError("OFFICE_EXTERNAL_LINK_REJECTED")
    except OfficeRenderError:
        raise
    except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError):
        raise OfficeRenderError("OFFICE_UNSAFE_PACKAGE") from None


def _processes():
    """Exact image paths and creation identity; no process command lines."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    import win32process
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    result = {}
    for pid in win32process.EnumProcesses():
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            length = wintypes.DWORD(32768); buffer = ctypes.create_unicode_buffer(length.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                continue
            image = buffer.value
            if Path(image).name.upper() not in {v[1] for v in _OFFICE.values()}:
                continue
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                continue
            result[pid] = {"pid": pid, "image": image, "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
        finally:
            kernel.CloseHandle(handle)
    return result


def _registered_server(progid):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + "\\CLSID") as key:
            clsid = winreg.QueryValueEx(key, "")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "CLSID\\" + clsid + "\\LocalServer32") as key:
            command = winreg.QueryValueEx(key, "")[0]
        match = re.match(r'^\s*(?:"([^"]+\.exe)"|(.+?\.exe))(?:\s|$)', command, re.I)
        server = Path(match[1] or match[2]).resolve(strict=True) if match else None
        if server is None or server.name.upper() not in {v[1] for v in _OFFICE.values()}:
            raise ValueError()
        return server
    except (OSError, ValueError, TypeError):
        raise OfficeRenderError("OFFICE_RUNTIME_UNAVAILABLE") from None


class _OwnedOffice:
    def __init__(self, progid, executable):
        self.progid, self.executable = progid, executable
        self.app = self.job = self.process = None
        self.owner = None
        self.saved = {}
        self.com_initialized = False
        self.bound = False
        self.assigned = False
        self.cleanup_confirmed = None

    def _application_pid(self):
        """Read only the COM object's window identity, never a user's content."""
        import pythoncom
        import win32process
        handles = []
        with suppress(Exception): handles.append(int(self.app.Hwnd))
        with suppress(Exception): handles.append(int(self.app.ActiveWindow.Hwnd))
        with suppress(Exception): handles.append(int(self.app._oleobj_.QueryInterface(pythoncom.IID_IOleWindow).GetWindow()))
        for hwnd in handles:
            if hwnd:
                with suppress(Exception):
                    return win32process.GetWindowThreadProcessId(hwnd)[1]
        return None

    @staticmethod
    def _choose_owner(before, after, app_pid, server):
        matching = lambda item: os.path.normcase(item["image"]) == os.path.normcase(str(server))
        if app_pid is not None:
            owner = after.get(app_pid)
            if owner and app_pid not in before and matching(owner):
                return owner
            raise OfficeRenderError("OFFICE_BUSY")
        # Windowless Word has no Application.Hwnd. Without any existing Word
        # process, one exact newly activated server is sufficient; the opened
        # private document's Window.Hwnd is checked again before reading it.
        if any(matching(item) for item in before.values()):
            raise OfficeRenderError("OFFICE_BUSY")
        candidates = [item for pid, item in after.items() if pid not in before and matching(item)]
        if len(candidates) == 1:
            return candidates[0]
        raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED")

    def start(self, directory, progress=None, ownership=None):
        import pythoncom
        import win32job
        import win32process
        pythoncom.CoInitialize()
        self.com_initialized = True
        server = _registered_server(self.progid)
        if server.name.upper() != self.executable:
            raise OfficeRenderError("OFFICE_RUNTIME_UNAVAILABLE")
        before = _processes()
        # PowerPoint may reuse one server for all Application objects. Refuse
        # without touching that existing instance. Excel and Word can launch
        # independent COM instances; require a new process before changing any
        # application property or permitting Quit/job cleanup.
        if self.executable == "POWERPNT.EXE" and any(Path(item["image"]).name.upper() == self.executable for item in before.values()):
            raise OfficeRenderError("OFFICE_BUSY")
        bootstrap = _bootstrap_document(directory, self.executable)
        # Own the process before executing any Office startup code. A COM
        # activation can block behind a licensing/repair dialog before returning
        # an object. DispatchEx alone cannot provide safe timeout cleanup there.
        # CreateProcess gives us a real owned handle; suspend -> Job -> resume
        # makes cancellation safe even while COM is still activating.
        try:
            if progress: progress("create_suspended_owned_server")
            self.job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = 1; startup.wShowWindow = 0
            # The only document opened before AutomationSecurity is set is our
            # own freshly generated empty, passive bootstrap. /a (Word) and
            # /safe (Excel/PowerPoint) suppress global templates/add-ins for
            # this process without changing the user's stored Office options.
            switches = {"WINWORD.EXE": ["/a", "/w", "/q"], "EXCEL.EXE": ["/x", "/safe"], "POWERPNT.EXE": ["/safe"]}
            command = subprocess.list2cmdline([str(server), *switches[self.executable], str(bootstrap)])
            self.process, thread, pid, _ = win32process.CreateProcess(str(server), command, None, None, False,
                0x00000004 | 0x08000000, None, str(server.parent), startup)
            try:
                win32job.AssignProcessToJobObject(self.job, self.process)
                self.assigned = True
                self.owner = _processes().get(pid)
                if not self.owner or os.path.normcase(self.owner["image"]) != os.path.normcase(str(server)):
                    raise ValueError()
                if ownership: ownership(self.owner)
                win32process.ResumeThread(thread)
            finally:
                thread.Close()
        except Exception:
            self.close()
            raise OfficeRenderError("OFFICE_ISOLATION_UNAVAILABLE") from None
        # Bind through the document window of our exact owned process. COM
        # DispatchEx can create a second process and therefore is never used.
        # Never acknowledge first-run, repair or licensing prompts.
        if progress: progress("wait_owned_server_startup")
        deadline = time.monotonic() + 15
        window = None
        while time.monotonic() < deadline:
            if _owned_startup_dialog(self.owner):
                raise OfficeRenderError("OFFICE_STARTUP_INTERACTION_REQUIRED")
            window = _owned_native_object(self.owner, self.executable)
            if window is not None:
                break
            time.sleep(.1)
        if window is None:
            raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED")
        self.app = window.Application
        if progress: progress("confirm_application_pid")
        chosen = self._choose_owner(before, _processes(), self._application_pid(), server)
        if chosen != self.owner:
            raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED")
        self.bound = True
        if progress: progress("disable_macros_and_alerts")
        self.set("AutomationSecurity", 3)
        self.set("DisplayAlerts", 1 if self.executable == "POWERPNT.EXE" else 0)
        if self.executable == "EXCEL.EXE":
            self.set("EnableEvents", False)
            initial = self.app.Workbooks.Item(1)
        elif self.executable == "WINWORD.EXE":
            initial = window.Document
        else:
            initial = window.Presentation
        if Path(str(initial.FullName)).resolve() != bootstrap:
            raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED")
        initial.Close(0) if self.executable == "WINWORD.EXE" else initial.Close(False) if self.executable == "EXCEL.EXE" else initial.Close()
        return self.app

    def confirm_document(self, document):
        if self.executable == "WINWORD.EXE":
            import win32process
            try:
                pid = win32process.GetWindowThreadProcessId(int(document.Windows.Item(1).Hwnd))[1]
            except Exception:
                raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED") from None
        else:
            pid = self._application_pid()
        if pid != self.owner["pid"] or _processes().get(pid) != self.owner:
            raise OfficeRenderError("OFFICE_OWNERSHIP_UNCONFIRMED")

    def set(self, name, value):
        try:
            self.saved[name] = getattr(self.app, name)
            setattr(self.app, name, value)
        except Exception:
            raise OfficeRenderError("OFFICE_OPEN_FAILED") from None

    def close(self):
        # A failed wait cannot become a success merely because a later close
        # has no handles left. We never infer process exit from a missing PID.
        clean = self.cleanup_confirmed is not False
        process, job = self.process, self.job
        job_closed = job is None
        try:
            try:
                owned = self.owner is not None and _processes().get(self.owner["pid"]) == self.owner
            except Exception:
                owned = False
            if owned and self.bound and self.app is not None:
                for name, value in self.saved.items():
                    with suppress(Exception): setattr(self.app, name, value)
                with suppress(Exception): self.app.Quit(0) if self.executable == "WINWORD.EXE" else self.app.Quit()
            self.app = None
            if process is not None and not self.assigned:
                # Kill only our exact CreateProcess handle, never a PID found
                # by process enumeration or a COM instance without ownership.
                try:
                    import win32process
                    win32process.TerminateProcess(process, 1)
                except Exception:
                    pass  # A signalled process handle can still prove exit.
            if job is not None:
                try:
                    job.Close()
                    job_closed = True
                except Exception:
                    clean = False
            if process is not None:
                try:
                    import win32event
                    if win32event.WaitForSingleObject(process, 5000) != win32event.WAIT_OBJECT_0:
                        clean = False
                except Exception:
                    clean = False
        finally:
            self.app = None
            if job is not None and not job_closed:
                try: job.Close()
                except Exception: clean = False
            self.job = None
            if process is not None:
                try: process.Close()
                except Exception: clean = False
            self.process = None
            if self.com_initialized:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    clean = False
                self.com_initialized = False
            self.cleanup_confirmed = clean
        if not clean:
            raise OfficeRenderError("OFFICE_CLEANUP_UNCONFIRMED")
        return True


def _owned_startup_dialog(owner):
    """Only constant window classes from this exact owned process identity."""
    if not owner or _processes().get(owner["pid"]) != owner:
        return False
    import win32gui
    import win32process
    found = []
    def inspect(hwnd, _):
        if (win32process.GetWindowThreadProcessId(hwnd)[1] == owner["pid"]
                and win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) in {"NUIDialog", "#32770"}):
            found.append(hwnd)
    win32gui.EnumWindows(inspect, None)
    return bool(found)


def _bootstrap_document(directory, executable):
    """An internal inert file; never a user document or model-supplied template."""
    try:
        if executable == "WINWORD.EXE":
            from docx import Document
            path = directory / "bootstrap.docx"; document = Document(); document.add_paragraph("Jingban isolated renderer bootstrap"); document.save(path)
        elif executable == "EXCEL.EXE":
            from openpyxl import Workbook
            path = directory / "bootstrap.xlsx"; book = Workbook(); book.active["A1"] = "Jingban isolated renderer bootstrap"; book.save(path)
        else:
            from pptx import Presentation
            path = directory / "bootstrap.pptx"; deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[0]); slide.shapes.title.text = "Jingban isolated renderer bootstrap"; deck.save(path)
        inspect_package(path.read_bytes(), path.suffix)
        return path.resolve(strict=True)
    except ImportError:
        raise OfficeRenderError("OFFICE_RUNTIME_UNAVAILABLE") from None


def _owned_native_object(owner, executable):
    """WM_GETOBJECT/OBJID_NATIVEOM only for windows of our CreateProcess PID.

    Microsoft documents _WwG, EXCEL7 and paneClassDC as Office native object
    model windows. ObjectFromLresult unwraps the marshalled COM object; wrapping
    that IDispatch never performs a new COM server activation.
    """
    import ctypes
    from ctypes import wintypes
    import pythoncom
    import win32com.client
    import win32gui
    import win32process
    if _processes().get(owner["pid"]) != owner:
        return None
    expected = {"WINWORD.EXE": "_WwG", "EXCEL.EXE": "EXCEL7", "POWERPNT.EXE": "paneClassDC"}[executable]
    candidates = []
    def child(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == owner["pid"] and win32gui.GetClassName(hwnd) == expected:
            candidates.append(hwnd)
    def top(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == owner["pid"]:
            child(hwnd, None); win32gui.EnumChildWindows(hwnd, child, None)
    win32gui.EnumWindows(top, None)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    for hwnd in candidates:
        if _processes().get(owner["pid"]) != owner:
            return None
        result = ctypes.c_size_t()
        if user32.SendMessageTimeoutW(hwnd, 0x003D, 0, -16, 0x0002, 1500, ctypes.byref(result)) and result.value:
            with suppress(Exception):
                dispatch = pythoncom.ObjectFromLresult(result.value, pythoncom.IID_IDispatch, 0)
                return win32com.client.Dispatch(dispatch)
    return None


def _stage(directory, name):
    # Private, constant-only diagnostics help identify blocked COM calls. This
    # file never contains document text or an Office exception string.
    with suppress(OSError): (directory / "stage.txt").write_text(name, encoding="ascii")


def _presentation_text(presentation):
    parts = []; count = 0
    def read(shape):
        nonlocal count
        count += 1
        if count > 20_000:
            raise OfficeRenderError("OFFICE_PDF_LIMIT")
        if shape.HasTextFrame and shape.TextFrame.HasText:
            parts.append(str(shape.TextFrame.TextRange.Text))
        if shape.HasTable:
            table = shape.Table
            rows, columns = int(table.Rows.Count), int(table.Columns.Count)
            if rows * columns > 10_000:
                raise OfficeRenderError("OFFICE_PDF_LIMIT")
            for i in range(1, rows + 1):
                parts.append("\t".join(str(table.Cell(i, j).Shape.TextFrame.TextRange.Text) for j in range(1, columns + 1)))
        if int(shape.Type) == 6:  # msoGroup
            for item in shape.GroupItems:
                read(item)
    for slide in presentation.Slides:
        parts.append("Slide " + str(slide.SlideIndex))
        for shape in slide.Shapes:
            read(shape)
    return "\n".join(parts)


def _native_render(directory: Path, suffix: str, *, cleanup=None):
    source, output = directory / ("input" + suffix), directory / "output.pdf"
    inspect_package(source.read_bytes(), suffix)
    owner = _OwnedOffice(*_OFFICE[suffix]); document = None
    if cleanup is not None:
        cleanup["confirmed"] = False
    try:
        _stage(directory, "create_private_application")
        app = owner.start(directory, lambda name: _stage(directory, name),
            lambda identity: (directory / "owned.json").write_text(json.dumps(identity), encoding="utf-8"))
        _stage(directory, "open_readonly_document")
        if suffix == ".docx":
            document = app.Documents.Open(str(source), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
                                          Visible=False, NoEncodingDialog=True)
            owner.confirm_document(document)
            _stage(directory, "native_content_readback")
            text = str(document.Content.Text)
            _stage(directory, "export_pdf")
            document.ExportAsFixedFormat(str(output), 17, False)
        elif suffix == ".xlsx":
            document = app.Workbooks.Open(str(source), UpdateLinks=0, ReadOnly=True, IgnoreReadOnlyRecommended=True,
                                         Notify=False, AddToMru=False)
            owner.confirm_document(document)
            _stage(directory, "native_content_readback")
            parts = []
            for sheet in document.Worksheets:
                sheet.Calculate()
                used = sheet.UsedRange
                if int(used.CountLarge) > 10000:
                    raise OfficeRenderError("OFFICE_PDF_LIMIT")
                values = used.Value
                rows = values if isinstance(values, tuple) else ((values,),)
                parts.append(str(sheet.Name))
                parts += ["\t".join("" if value is None else str(value) for value in (row if isinstance(row, tuple) else (row,))) for row in rows]
            text = "\n".join(parts)
            _stage(directory, "export_pdf")
            document.ExportAsFixedFormat(0, str(output), 0, False, False, OpenAfterPublish=False)
        else:
            document = app.Presentations.Open(str(source), -1, 0, 0)
            owner.confirm_document(document)
            _stage(directory, "native_content_readback")
            text = _presentation_text(document)
            # ppFixedFormatTypePDF=2, ppFixedFormatIntentPrint=2.
            _stage(directory, "export_pdf")
            document.ExportAsFixedFormat(str(output), 2, 2)
        if not output.is_file() or output.stat().st_size > MAX_DOCUMENT_BYTES:
            raise OfficeRenderError("OFFICE_PDF_LIMIT")
        return {"native_text": text[:24000], "native_text_characters": len(text), "native_text_truncated": len(text) > 24000,
                "engine": _OFFICE[suffix][0], "owned_process": owner.owner,
                "scope": "native_readonly_content_and_pdf_export; not whole-task semantic verification"}
    except OfficeRenderError:
        raise
    except Exception:
        raise OfficeRenderError("OFFICE_RENDER_FAILED") from None
    finally:
        _stage(directory, "close_private_document_and_application")
        if document is not None:
            with suppress(Exception): document.Close(0) if suffix == ".docx" else document.Close(False) if suffix == ".xlsx" else document.Close()
        document = None
        confirmed = owner.close()
        if cleanup is not None:
            cleanup["confirmed"] = confirmed is True


def _helper():
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    # Before native setup there is no owned Office process to collect.
    cleanup = {"confirmed": True}
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"directory", "suffix"} or data["suffix"] not in _OFFICE:
            raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
        directory = Path(data["directory"]).resolve(strict=True)
        expected = (HERE / "runtime" / "office_render").resolve()
        if directory.parent != expected or not directory.name.startswith("task-"):
            raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
        result = _native_render(directory, data["suffix"], cleanup=cleanup)
        sys.stdout.write(json.dumps({"ok": True, **result, "cleanup_confirmed": cleanup["confirmed"]}, ensure_ascii=False))
        return 0
    except OfficeRenderError as exc:
        sys.stdout.write(json.dumps({"ok": False, "code": exc.code, "cleanup_confirmed": cleanup["confirmed"]})); return 1
    except Exception:
        sys.stdout.write(json.dumps({"ok": False, "code": "OFFICE_RENDER_FAILED", "cleanup_confirmed": cleanup["confirmed"]})); return 1


async def _stop_helper(process):
    """Terminate only the subprocess handle we created, tolerating exit races."""
    if process is None:
        return
    if process.returncode is None:
        with suppress(ProcessLookupError): process.kill()
    with suppress(ProcessLookupError): await process.wait()


async def _render_document(files, path: str, output_path=None, *, overwrite=False, expected_hash=None, timeout_seconds=90,
                           _execution=None):
    """Render a granted OOXML snapshot; publish only through WorkspaceFiles CAS.

    Returns JSON with PDF ArtifactRef, page count and native/PDF text readback.
    The PDF proves renderability, not the user's entire semantic requirement.
    """
    if os.name != "nt":
        raise OfficeRenderError("OFFICE_RUNTIME_UNAVAILABLE")
    if type(timeout_seconds) not in (int, float) or not 5 <= timeout_seconds <= 180:
        raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
    if type(overwrite) is not bool:
        raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
    target, raw, digest = files.read_bytes(path)
    suffix = target.suffix.casefold()
    inspect_package(raw, suffix)
    modules = ("pythoncom", "win32com", "win32job", "pypdf", {".docx": "docx", ".xlsx": "openpyxl", ".pptx": "pptx"}[suffix])
    if any(importlib.util.find_spec(name) is None for name in modules):
        raise OfficeRenderError("OFFICE_RUNTIME_UNAVAILABLE")
    destination = output_path or str(target.with_name(target.stem + ".rendered.pdf"))
    resolved = files.resolve(destination, write=True)
    if resolved.suffix.casefold() != ".pdf" or resolved == target:
        raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
    files._version_for_write(resolved, overwrite, expected_hash)
    runtime = HERE / "runtime" / "office_render"
    runtime.mkdir(parents=True, exist_ok=True)
    if not runtime.resolve().is_relative_to(HERE.resolve()):
        raise OfficeRenderError("OFFICE_INVALID_ARGUMENT")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="task-", dir=runtime, ignore_cleanup_errors=True) as temp:
            directory = Path(temp)
            (directory / ("input" + suffix)).write_bytes(raw)
            environment = {key: value for key, value in os.environ.items()
                if not re.search(r"API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT|PYTHONPATH", key, re.I)}
            if _execution is not None:
                _execution["helper_settled"] = False
                _execution["owned_cleanup_confirmed"] = False
            process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-u", str(Path(__file__).resolve()), "--office-helper",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=environment, creationflags=0x08000000, limit=512 * 1024)
            message = json.dumps({"directory": str(directory), "suffix": suffix}).encode("utf-8")
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(message), timeout_seconds)
            except asyncio.TimeoutError:
                reason = "OFFICE_TIMEOUT"
                with suppress(Exception):
                    owner = json.loads((directory / "owned.json").read_text(encoding="utf-8"))
                    if _owned_startup_dialog(owner): reason = "OFFICE_STARTUP_INTERACTION_REQUIRED"
                await _stop_helper(process)
                # Job closure is asynchronous; allow the private server to
                # release its source before the temporary directory is removed.
                await asyncio.sleep(.2)
                raise OfficeRenderError(reason) from None
            if len(stdout) > 256 * 1024:
                raise OfficeRenderError("OFFICE_PROTOCOL")
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise OfficeRenderError("OFFICE_PROTOCOL") from None
            if not isinstance(result, dict) or type(result.get("cleanup_confirmed")) is not bool:
                raise OfficeRenderError("OFFICE_PROTOCOL")
            if _execution is not None:
                _execution["owned_cleanup_confirmed"] = result["cleanup_confirmed"] is True
            if result["cleanup_confirmed"] is not True:
                raise OfficeRenderError("OFFICE_CLEANUP_UNCONFIRMED")
            if result.get("ok") is not True:
                raise OfficeRenderError(result.get("code") if isinstance(result, dict) else "OFFICE_PROTOCOL")
            if process.returncode != 0:
                raise OfficeRenderError("OFFICE_PROTOCOL")
            pdf = (directory / "output.pdf").read_bytes()
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(pdf), strict=True)
                pages = len(reader.pages)
                if not 1 <= pages <= 300:
                    raise OfficeRenderError("OFFICE_PDF_LIMIT")
                pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            except OfficeRenderError:
                raise
            except Exception:
                raise OfficeRenderError("OFFICE_PDF_INVALID") from None
            if files.read_bytes(path)[2] != digest:
                raise OfficeRenderError("OFFICE_SOURCE_CHANGED")
            if _execution is not None:
                _execution["publication_started"] = True
            written = json.loads(files.write_bytes(destination, pdf, overwrite, expected_hash))
            return json.dumps({"status": "rendered", "source": files.display_path(target), "source_sha256": digest,
                "written": written["written"], "sha256": written["sha256"], "bytes": written["bytes"],
                "artifact": written["artifact"], "pages": pages, "engine": result["engine"],
                "native_text": result["native_text"], "native_text_truncated": result["native_text_truncated"],
                "pdf_text": pdf_text[:24000], "pdf_text_truncated": len(pdf_text) > 24000,
                "isolation": {"private_office_process": True, "macros_disabled": True, "external_links_rejected": True},
                "scope": "native_readonly_content_and_pdf_export; semantic_and_visual_layout_require_separate_checks"}, ensure_ascii=False)
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await _stop_helper(process)
            await asyncio.sleep(.2)
        raise
    finally:
        if process is not None and process.returncode is None:
            await _stop_helper(process)
        if _execution is not None and process is not None:
            _execution["helper_settled"] = process.returncode is not None


_PRIVATE_ABORT_CODES = frozenset({"OFFICE_UNSUPPORTED_FORMAT", "OFFICE_UNSAFE_PACKAGE",
    "OFFICE_ACTIVE_CONTENT_REJECTED", "OFFICE_EXTERNAL_LINK_REJECTED", "OFFICE_UNSUPPORTED_FORMULA",
    "OFFICE_RUNTIME_UNAVAILABLE", "OFFICE_INVALID_ARGUMENT", "OFFICE_BUSY",
    "OFFICE_STARTUP_INTERACTION_REQUIRED", "OFFICE_TIMEOUT", "OFFICE_OPEN_FAILED",
    "OFFICE_PDF_LIMIT", "OFFICE_PDF_INVALID", "OFFICE_SOURCE_CHANGED"})


async def render_document(files, path: str, output_path=None, *, overwrite=False, expected_hash=None, timeout_seconds=90):
    """Expose a no-publication failure only after the owned helper settles.

    This flag is host-produced, never inferred from a tool/model error string.
    Publication attempts and unknown ownership/protocol failures stay blockers.
    The original goal must still prove every requested PDF/layout requirement.
    """
    execution = {"helper_settled": True, "owned_cleanup_confirmed": True, "publication_started": False}
    try:
        return await _render_document(files, path, output_path, overwrite=overwrite,
            expected_hash=expected_hash, timeout_seconds=timeout_seconds, _execution=execution)
    except OfficeRenderError as exc:
        exc.safe_no_publication = (execution["helper_settled"] and execution["owned_cleanup_confirmed"]
                                   and not execution["publication_started"]
                                   and exc.code in _PRIVATE_ABORT_CODES)
        raise


if __name__ == "__main__":
    if sys.argv[1:] != ["--office-helper"]:
        raise SystemExit(2)
    raise SystemExit(_helper())
