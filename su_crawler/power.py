"""Keep unattended collection at full speed on Windows 11 hybrid CPUs.

Windows 11 classifies a process without a visible window that is not part of
the foreground app (a scheduled task, a detached worker) as background work and
applies power throttling (EcoQoS): after a few seconds it runs on efficiency
cores, and a scheduled collection takes markedly longer than the same command
run from a terminal. A process may explicitly opt out of execution-speed
throttling for itself.
"""
from __future__ import annotations

import sys

PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_CLASS = 4  # ProcessPowerThrottling in PROCESS_INFORMATION_CLASS


def keep_full_speed() -> bool:
    """Opt this process out of execution-speed throttling; True when Windows accepted it."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        # Control the execution-speed policy and set its state to off.
        state = PowerThrottlingState(PROCESS_POWER_THROTTLING_CURRENT_VERSION,
                                     PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)
        return bool(kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), PROCESS_POWER_THROTTLING_CLASS,
                                                   ctypes.byref(state), ctypes.sizeof(state)))
    except (AttributeError, OSError):
        return False  # older Windows or a restricted environment: collect anyway
