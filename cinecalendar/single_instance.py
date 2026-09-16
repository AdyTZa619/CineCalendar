from __future__ import annotations

import os


ERROR_ALREADY_EXISTS = 183
_MUTEX_NAME = r"Local\CineCalendar-Premium-SingleInstance"


class SingleInstanceGuard:
    """Process-wide Windows single-instance guard.

    The guard is acquired before CineCalendar opens SQLite, starts the ratings watcher, ALS workers
    or the updater. A second executable therefore cannot race the first one over the same portable
    data directory. On non-Windows development/test hosts it becomes a no-op.
    """

    def __init__(self, name: str = _MUTEX_NAME):
        self.name = str(name)
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if os.name != "nt":
            self._handle = True
            return True

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self._activate_existing_window()
            return False
        self._handle = handle
        return True

    @staticmethod
    def _activate_existing_window() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.restype = ctypes.c_int
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]

            @enum_proc
            def callback(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip().lower()
                if title.startswith("cinecalendar"):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    return False
                return True

            user32.EnumWindows(callback, 0)
        except Exception:
            # Focusing the existing UI is best effort; the mutex itself is the integrity barrier.
            return

    def release(self) -> None:
        if self._handle is None:
            return
        if os.name == "nt" and self._handle is not True:
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(self._handle)
            finally:
                self._handle = None
        else:
            self._handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("CineCalendar rulează deja.")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
