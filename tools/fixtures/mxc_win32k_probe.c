/* Fixed, trusted Win32k-boundary canary. No CRT, CLR, USER32 or host IO.
 *
 * Input: one host-owned staged Python script path. The interpreter is
 * a fixed pinned runtime, never a caller-selected program. The only child is
 * that Python process; it is created suspended, queried, resumed, waited and
 * closed by exact handles. No UI Job limits or breakaway flags are used.
 *
 * The low 16 exit bits are documented below. High 16 bits contain only the
 * WinError from the attempted clearing of this process's mitigation. The
 * parent probe decodes these fixed values; no text or user data is emitted.
 */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0A00
#include <windows.h>

#define SELF_QUERY_OK       0x0001u
#define SELF_DISABLED       0x0002u
#define ZERO_CLEAR_RETURNED 0x0004u
#define AFTER_QUERY_OK      0x0008u
#define AFTER_DISABLED      0x0010u
#define ATTRIBUTE_OK        0x0020u
#define CHILD_CREATED       0x0040u
#define CHILD_QUERY_OK      0x0080u
#define CHILD_DISABLED      0x0100u
#define CHILD_RESUMED       0x0200u
#define CHILD_SIGNALED      0x0400u
#define CHILD_EXIT_ZERO     0x0800u
#define CHILD_THREAD_CLOSED 0x1000u
#define CHILD_ROOT_CLOSED   0x2000u
#define INPUT_VALID         0x4000u

/* The trusted QA builder supplies the pinned runtime as a UTF-16 initializer.
 * Do not discover an interpreter from the scrubbed child environment or add
 * a caller-selected executable argument to this canary. */
#ifndef AURA_QA_PYTHON_EXE
#error AURA_QA_PYTHON_EXE must name the trusted pinned QA interpreter
#endif
static const WCHAR python[] = AURA_QA_PYTHON_EXE;
static WCHAR script[2048];
static WCHAR command[4096];
static DWORD64 attributes[512];
static STARTUPINFOEXW startup;
static PROCESS_INFORMATION child;
static DWORD observed[16];
static WCHAR expected_title[128];
static WCHAR actual_title[128];

/* These inputs are fixed by the trusted QA parent, not a model tool. Only
 * one exact host-owned canary HWND/PID/TID and one synthetic title are used.
 * No window enumeration, input, capture or clipboard function is linked. */
static BOOL environment_number(const WCHAR *key, ULONGLONG *value) {
    WCHAR text[32];
    DWORD count = GetEnvironmentVariableW(key, text, 32);
    ULONGLONG parsed = 0;
    if (!count || count >= 32) return FALSE;
    for (DWORD i = 0; i < count; i++) {
        if (text[i] < L'0' || text[i] > L'9') return FALSE;
        DWORD digit = (DWORD)(text[i] - L'0');
        if (parsed > (~0ULL - digit) / 10) return FALSE;
        parsed = parsed * 10 + digit;
    }
    *value = parsed;
    return TRUE;
}

static void observe_owned_window(void) {
    ULONGLONG hwnd = 0, pid = 0, tid = 0, nonce = 0;
    observed[0] = 0x41555241u;
    observed[1] = 1;
    if (!environment_number(L"AURA_QA_OWNED_HWND", &hwnd) || !hwnd ||
        !environment_number(L"AURA_QA_OWNED_PID", &pid) || !pid || pid > 0xffffffffu ||
        !environment_number(L"AURA_QA_OWNED_TID", &tid) || !tid || tid > 0xffffffffu ||
        !environment_number(L"AURA_QA_OBSERVATION_NONCE", &nonce) || !nonce || nonce > 0xffffffffu)
        return;
    DWORD title_length = GetEnvironmentVariableW(L"AURA_QA_OWNED_TITLE", expected_title, 128);
    if (!title_length || title_length >= 128) return;
    observed[2] = (DWORD)nonce;
    observed[3] = 1;
    observed[4] = 1;
    SetLastError(0);
    HMODULE module = LoadLibraryW(L"user32.dll");
    if (!module) {
        observed[6] = GetLastError();
        observed[13] = 1; /* No module handle was created. */
        return;
    }
    observed[5] = 1;
    typedef DWORD (WINAPI *WindowIdentity)(HWND, LPDWORD);
    typedef int (WINAPI *WindowCaption)(HWND, LPWSTR, int);
    WindowIdentity identity = (WindowIdentity)GetProcAddress(module, "GetWindowThreadProcessId");
    WindowCaption caption = (WindowCaption)GetProcAddress(module, "GetWindowTextW");
    observed[7] = identity != NULL && caption != NULL;
    if (observed[7]) {
        DWORD actual_pid = 0;
        SetLastError(0);
        DWORD actual_tid = identity((HWND)(ULONG_PTR)hwnd, &actual_pid);
        observed[14] = actual_tid ? 0 : GetLastError();
        observed[8] = actual_pid == pid && actual_tid == tid;
        if (observed[8]) {
            observed[9] = 1;
            SetLastError(0);
            int length = caption((HWND)(ULONG_PTR)hwnd, actual_title, 128);
            observed[10] = length > 0;
            observed[12] = length ? 0 : GetLastError();
            if (length == title_length) {
                observed[11] = 1;
                for (DWORD i = 0; i < title_length; i++)
                    if (actual_title[i] != expected_title[i]) observed[11] = 0;
            }
        }
    }
    observed[13] = FreeLibrary(module) != 0;
}

static BOOL write_observation(void) {
    HANDLE output = CreateFileW(L"owned-native-observe.bin", GENERIC_WRITE, 0, NULL,
                                CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (output == INVALID_HANDLE_VALUE) return FALSE;
    DWORD written = 0;
    BOOL ok = WriteFile(output, observed, sizeof(observed), &written, NULL);
    BOOL closed = CloseHandle(output);
    return ok && written == sizeof(observed) && closed;
}

/* The probe passes exactly one staged file path. Never interpret shell
 * escapes, extra switches, a program path, or an unbounded command line. */
static BOOL read_script_argument(const WCHAR *p) {
    DWORD n = 0;
    BOOL quoted = FALSE;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p++ != L'"') return FALSE;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    if (*p == L'"') { quoted = TRUE; p++; }
    while (*p && (quoted ? *p != L'"' : (*p != L' ' && *p != L'\t'))) {
        if (n >= 2047 || *p < 32) return FALSE;
        script[n++] = *p++;
    }
    if (!n || (quoted && *p++ != L'"')) return FALSE;
    script[n] = 0;
    while (*p == L' ' || *p == L'\t') p++;
    if (*p) return FALSE;
    /* Reject relative, device, UNC, ADS and parent traversal paths. */
    if (!((script[0] >= L'A' && script[0] <= L'Z') ||
          (script[0] >= L'a' && script[0] <= L'z')) ||
        script[1] != L':' || script[2] != L'\\') return FALSE;
    for (DWORD i = 2; i < n; i++) {
        if (script[i] == L':' || script[i] == L'/') return FALSE;
        if (script[i] == L'.' && script[i+1] == L'.') return FALSE;
    }
    return TRUE;
}

static BOOL append(const WCHAR *text, DWORD *used) {
    while (*text) {
        if (*used >= 4095) return FALSE;
        command[(*used)++] = *text++;
    }
    command[*used] = 0;
    return TRUE;
}

#ifdef AURA_CANARY_PARSER_TEST
/* Compiled separately as a pure parser regression executable. It never
 * queries policy, creates a child or accesses files/UI. */
void mainCRTStartup(void) {
    static const WCHAR *cases[] = {
        L"probe.exe E:\\owned\\fixed_child.py",
        L"\"E:\\owned path\\probe.exe\" \"E:\\owned path\\fixed_child.py\"",
        L"probe.exe E:\\owned\\fixed_child.py extra",
        L"probe.exe",
        L"probe.exe \"E:\\owned\\fixed_child.py",
        L"probe.exe fixed_child.py",
        L"probe.exe \\owned\\fixed_child.py",
        L"probe.exe \\\\server\\owned\\fixed_child.py",
        L"probe.exe \\\\?\\E:\\owned\\fixed_child.py",
        L"probe.exe E:\\owned\\fixed_child.py:stream",
        L"probe.exe E:\\owned\\..\\fixed_child.py",
        L"probe.exe E:/owned/fixed_child.py",
        L"probe.exe \"\"",
        L"probe.exe E:\\鲸鱼\\fixed_child.py"
    };
    static const BOOL accepted[] = {TRUE,TRUE,FALSE,FALSE,FALSE,FALSE,
        FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,TRUE};
    DWORD failures = 0;
    for (DWORD i = 0; i < sizeof(accepted)/sizeof(accepted[0]); i++)
        if (read_script_argument(cases[i]) != accepted[i]) failures |= 1u << i;
    ExitProcess(failures);
}
#else
void mainCRTStartup(void) {
    DWORD flags = 0, clear_error = 0, initial = 0, after = 0, zero = 0;
    DWORD child_policy = 0, exit_code = 1;
    SIZE_T bytes = 0;
    BOOL list_initialized = FALSE;
    DWORD64 child_mitigation = (2ULL << 28); /* Explicit ALWAYS_OFF. */
    if (GetProcessMitigationPolicy(GetCurrentProcess(),
            ProcessSystemCallDisablePolicy, &initial, sizeof(initial))) {
        flags |= SELF_QUERY_OK;
        if (initial & 1) flags |= SELF_DISABLED;
    }
    SetLastError(0);
    if (SetProcessMitigationPolicy(ProcessSystemCallDisablePolicy,
                                  &zero, sizeof(zero))) {
        flags |= ZERO_CLEAR_RETURNED;
    } else clear_error = GetLastError();
    if (GetProcessMitigationPolicy(GetCurrentProcess(),
            ProcessSystemCallDisablePolicy, &after, sizeof(after))) {
        flags |= AFTER_QUERY_OK;
        if (after & 1) flags |= AFTER_DISABLED;
    }
    observe_owned_window();
    /* No child is dispatched unless this exact helper began mitigated. */
    if ((flags & (SELF_QUERY_OK | SELF_DISABLED)) !=
                 (SELF_QUERY_OK | SELF_DISABLED)) goto done;
    if (!read_script_argument(GetCommandLineW())) goto done;
    flags |= INPUT_VALID;
    {
        DWORD used = 0;
        if (!append(L"\"", &used) || !append(python, &used) ||
            !append(L"\" -I -S -B \"", &used) ||
            !append(script, &used) || !append(L"\"", &used)) goto done;
    }
    InitializeProcThreadAttributeList(NULL, 1, 0, &bytes);
    if (bytes == 0 || bytes > sizeof(attributes)) goto done;
    startup.lpAttributeList = (LPPROC_THREAD_ATTRIBUTE_LIST)attributes;
    if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0,
                                          &bytes)) goto done;
    list_initialized = TRUE;
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
            PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY, &child_mitigation,
            sizeof(child_mitigation), NULL, NULL)) goto done;
    flags |= ATTRIBUTE_OK;
    startup.StartupInfo.cb = sizeof(startup);
    /* No inherited stdin/stdout/stderr or other application handles. */
    if (!CreateProcessW(python, command, NULL, NULL, FALSE,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            NULL, NULL, &startup.StartupInfo, &child)) goto done;
    flags |= CHILD_CREATED;
    if (!GetProcessMitigationPolicy(child.hProcess,
            ProcessSystemCallDisablePolicy, &child_policy,
            sizeof(child_policy))) goto done;
    flags |= CHILD_QUERY_OK;
    if (child_policy & 1) flags |= CHILD_DISABLED;
    if (ResumeThread(child.hThread) != 1) goto done;
    flags |= CHILD_RESUMED;
    if (WaitForSingleObject(child.hProcess, 2500) == WAIT_OBJECT_0) {
        flags |= CHILD_SIGNALED;
        if (GetExitCodeProcess(child.hProcess, &exit_code) && exit_code == 0)
            flags |= CHILD_EXIT_ZERO;
    }
done:
    if (child.hProcess) {
        if (!(flags & CHILD_SIGNALED)) {
            TerminateProcess(child.hProcess, 77);
            if (WaitForSingleObject(child.hProcess, 2000) == WAIT_OBJECT_0)
                flags |= CHILD_SIGNALED;
        }
        if (CloseHandle(child.hProcess)) flags |= CHILD_ROOT_CLOSED;
    }
    if (child.hThread && CloseHandle(child.hThread)) flags |= CHILD_THREAD_CLOSED;
    if (list_initialized) DeleteProcThreadAttributeList(startup.lpAttributeList);
    if (!write_observation()) flags |= 0x8000u;
    ExitProcess(((clear_error & 0xffffu) << 16) | flags);
}
#endif
