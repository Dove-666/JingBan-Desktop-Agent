/* Fixed owned-broker access probe. No enumeration, memory IO or injection.
 * The fixed QA worker puts ONLY its exact waiting parent PID and a synthetic
 * nonce in the environment. The production broker waits for that worker, so
 * its PID remains live throughout the probe. This helper
 * opens/closes exact handles and writes bool/WinError observations only.
 */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0A00
#include <windows.h>

static DWORD report[20];
static BOOL number(const WCHAR *name, DWORD *result) {
    WCHAR value[32];
    DWORD length = GetEnvironmentVariableW(name, value, 32), total = 0;
    if (!length || length >= 32) return FALSE;
    for (DWORD i = 0; i < length; i++) {
        if (value[i] < L'0' || value[i] > L'9') return FALSE;
        DWORD digit = value[i] - L'0';
        if (total > (0xffffffffu-digit)/10) return FALSE;
        total = total*10+digit;
    }
    if (!total) return FALSE;
    *result = total;
    return TRUE;
}

void mainCRTStartup(void) {
    DWORD pid = 0, nonce = 0, policy = 0;
    report[0] = 0x41555241; report[1] = 1;
    if (!number(L"AURA_QA_BROKER_PID", &pid) ||
        !number(L"AURA_QA_BROKER_NONCE", &nonce)) ExitProcess(2);
    report[2] = nonce;
    report[3] = GetProcessMitigationPolicy(GetCurrentProcess(),
        ProcessSystemCallDisablePolicy, &policy, sizeof(policy)) != 0;
    report[4] = policy & 1;
    static const DWORD rights[] = {PROCESS_CREATE_PROCESS,
        PROCESS_VM_WRITE | PROCESS_VM_OPERATION, PROCESS_DUP_HANDLE,
        PROCESS_TERMINATE, WRITE_DAC};
    for (DWORD i = 0; i < 5; i++) {
        SetLastError(0);
        HANDLE process = OpenProcess(rights[i], FALSE, pid);
        report[5+i*3] = process != NULL;
        report[6+i*3] = process ? 0 : GetLastError();
        report[7+i*3] = process ? CloseHandle(process) != 0 : 1;
    }
    HANDLE output = CreateFileW(L"owned-broker-access.bin", GENERIC_WRITE,
        0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (output == INVALID_HANDLE_VALUE) ExitProcess(3);
    DWORD written = 0;
    BOOL ok = WriteFile(output, report, sizeof(report), &written, NULL);
    BOOL closed = CloseHandle(output);
    ExitProcess(ok && written == sizeof(report) && closed ? 0 : 4);
}
