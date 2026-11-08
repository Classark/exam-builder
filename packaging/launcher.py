"""Classark Exam Builder as a desktop app: the entry point PyInstaller freezes into
ClassarkExamBuilder.exe on Windows and "Classark Exam Builder.app" on macOS (see package.py).

Opening it starts the local server - the same FastAPI app as development, serving the built
UI - and shows that UI in the app's own window (pywebview: WebView2 on Windows, WebKit on
macOS), not a browser tab. Closing the window stops the server.

Everything stays on this computer: the server listens on 127.0.0.1 only.
"""

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "Classark Exam Builder"
PREFERRED_PORT = 8000
DATA_FOLDER_NAME = "ClassarkExamBuilder data"
# the app data folder's name (%LOCALAPPDATA% on Windows, Application Support on macOS)
APP_DATA_NAME = "ClassarkExamBuilder"
# Their names while the app was called Question Bank Builder: a folder found under an old
# name is renamed to the new one on first start, so nobody's exams are left behind.
OLD_DATA_FOLDER_NAME = "QuestionBankBuilder data"
OLD_APP_DATA_NAME = "QuestionBankBuilder"
# Written into the install folder by both installers (packaging/installer.iss and
# installer.wxs): an installed copy keeps its data in the user's app data folder, never
# beside the exe, where an upgrade or uninstall would be working on the same folder.
INSTALLED_MARKER = "installed.txt"

FROZEN = getattr(sys, "frozen", False)
# where the bundled files (engine, built UI) are: the build's _internal folder when frozen,
# the repo when run from source (`uv run --project engine python packaging/launcher.py`)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _renamed(old: Path, new: Path) -> Path:
    """`new`, taking over `old` (the folder's name before the app was renamed) when only the
    old one exists. If it can't be renamed - something has a file in it open - the old
    folder is used where it is, rather than starting over in an empty one."""
    if new.exists() or not old.exists():
        return new
    try:
        old.rename(new)
        return new
    except OSError:
        return old


def data_dir() -> Path:
    """Next to the exe, so a user's work sits beside the program and is easy to find and
    back up - unless that's read-only (the exe copied into Program Files), in which case
    the per-user app data folder. An installed copy (INSTALLED_MARKER) always uses the app
    data folder. On macOS always Application Support: an app bundle is signed, so nothing
    may be written inside it, and beside it is usually /Applications."""
    cannot = f"{APP_NAME} could not find a folder it is allowed to save in."
    if sys.platform == "darwin":
        support = Path.home() / "Library" / "Application Support"
        folder = _renamed(support / OLD_APP_DATA_NAME, support / APP_DATA_NAME)
        if _writable(folder):
            return folder
        raise SystemExit(cannot)
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    app_data = _renamed(local / OLD_APP_DATA_NAME, local / APP_DATA_NAME)
    if FROZEN and (APP_DIR / INSTALLED_MARKER).exists():
        if _writable(app_data):
            return app_data
        raise SystemExit(cannot)
    # Builds before the switch to a folder (package.py, --onedir) were one exe whose data
    # folder sat beside it - one level above where the exe now is. Keep using that one.
    beside = _renamed(APP_DIR / OLD_DATA_FOLDER_NAME, APP_DIR / DATA_FOLDER_NAME)
    earlier = _renamed(APP_DIR.parent / OLD_DATA_FOLDER_NAME, APP_DIR.parent / DATA_FOLDER_NAME)
    if FROZEN and not beside.exists() and (earlier / "out").is_dir() and _writable(earlier):
        return earlier
    for folder in (beside, app_data):
        if _writable(folder):
            return folder
    raise SystemExit(cannot)


def free_port() -> int:
    """The usual port if it's free, otherwise any free one - a second copy of the app, or
    another program on 8000, mustn't stop this one starting."""
    for port in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("No free network port to run on.")


_job = None  # kept for the life of the process: closing it is what ends the children


def _end_children_with_app() -> None:
    """Closing the window ends this process at once (see main), and on Windows a process's
    children outlive it: scan workers kept extracting and Claude Code CLIs kept answering
    after the app had gone, looking like a server left running (reported by the user). A
    job object that kills its processes when its last handle closes takes them with it -
    on any exit, a crash or End Task included. Children join it as they're started."""
    global _job
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    # KILL_ON_JOB_CLOSE, plus BREAKAWAY_OK so a browser or Explorer we open can leave it
    limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
    if not (
        job
        and kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    ):
        logging.warning("children won't end with the app (job object error %s)", ctypes.get_last_error())
        return
    _job = job


def open_outside_app(target: str) -> None:
    """Open a URL or folder in its own program, outside the app's job: a browser that
    wasn't already running would otherwise start inside it and close with the app."""
    if sys.platform != "win32":
        if target.startswith("http"):
            webbrowser.open(target)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", target], check=False)
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", target],
            creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB | subprocess.CREATE_NO_WINDOW,
        )
    except OSError:  # no job, or an outer one that forbids leaving it
        os.startfile(target)


def main() -> None:
    data = data_dir()
    # A windowed exe has no console, so sys.stdout/stderr are None and anything that
    # prints would crash; send it all to a log file in the data folder instead.
    log_file = open(data / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file
    logging.basicConfig(stream=log_file, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _end_children_with_app()

    # main.py reads these as it's imported
    os.environ.setdefault("QBX_PAPERS_DIR", str(data / "papers"))
    os.environ.setdefault("QBX_OUT_DIR", str(data / "out"))
    os.environ.setdefault("QBX_QUIZZES_DIR", str(data / "quizzes"))
    # settings (whether Claude is on, which models) and syllabus topic lists
    os.environ.setdefault("QBX_CONFIG_DIR", str(data / "config"))
    os.environ.setdefault("QBX_UI_DIR", str(BUNDLE_DIR / "ui" / "dist"))
    (data / "out").mkdir(exist_ok=True)
    if not FROZEN:
        sys.path.insert(0, str(BUNDLE_DIR / "engine"))

    import uvicorn

    from main import app
    from qbx import account

    # sign-in opens the browser, which must not start inside the app's job and close with it
    account.set_opener(open_outside_app)

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    logging.info("serving %s, data in %s", url, data)

    if "--headless" in sys.argv:
        # just the server - no window: for checking a build, or troubleshooting
        server.run()
        return
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    _app_window(server, url, data)
    serving.join(timeout=10)
    # The server stops ~0.2s after the window closes, but Python's own teardown then took
    # ~6s more on Windows (pywebview's .NET runtime), leaving the process behind as if it
    # had hung. Scans, tagging and fixing run in daemon threads, which an ordinary exit
    # abandons too, so leaving now skips nothing. Their worker processes go with the job
    # on Windows; elsewhere end them here.
    import multiprocessing

    for child in multiprocessing.active_children():
        child.kill()
    logging.shutdown()
    if sys.platform == "win32":
        # Not os._exit: that still runs every loaded DLL's shutdown code, and closing the
        # window while the builder's preview was rendering left the process hung in it -
        # one thread, ~180 MB, no window, until End Task (reported by the user; 12 of 12
        # closes 7s after opening on the builder). TerminateProcess skips that code.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess(ctypes.c_void_p(kernel32.GetCurrentProcess()), 0)
    os._exit(0)


def _unblock_bundle() -> None:
    """A zip downloaded or received through a chat app, then extracted with Explorer,
    marks every file it holds as from the internet (a Zone.Identifier stream), and .NET
    Framework won't load such a DLL: pywebview's window failed on a friend's machine with
    "Failed to resolve Python.Runtime.Loader.Initialize". Removing the mark is what
    right-click > Properties > Unblock does. Only the .NET assemblies matter."""
    if not (FROZEN and sys.platform == "win32"):
        return
    for folder in (BUNDLE_DIR / "pythonnet", BUNDLE_DIR / "webview", BUNDLE_DIR / "clr_loader"):
        for dll in folder.rglob("*.dll"):
            try:
                os.remove(f"{dll}:Zone.Identifier")
            except OSError:
                pass  # not marked, or a read-only folder - the fallback below covers it


def _browser_fallback(url: str, error: BaseException) -> None:
    """The app's own window couldn't open: show the UI in the browser instead, and keep
    serving until the user says stop. A Windows message box needs nothing but ctypes."""
    logging.exception("app window failed, using the browser instead", exc_info=error)
    open_outside_app(url)
    message = (
        f"{APP_NAME} couldn't open its own window, so it is running in your web browser "
        f"instead:\n\n{url}\n\nKeep this message open while you use it. Click OK to stop "
        f"{APP_NAME}."
    )
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)  # MB_ICONINFORMATION
    else:
        threading.Event().wait()  # macOS has WebKit built in; this is just a safety net


class Desktop:
    """What the UI can ask of the app window, as window.pywebview.api (pywebview's js_api;
    names starting with _ aren't exposed)."""

    def __init__(self, data: Path, url: str) -> None:
        self._data = data
        self._url = url
        self._window = None
        self._own_title_bar = False

    def open_data_folder(self) -> None:
        open_outside_app(str(self._data))

    def open_in_browser(self) -> None:
        open_outside_app(self._url)

    def window_controls(self) -> bool:
        """Whether the UI's top bar is the title bar, and so draws minimise/maximise/close."""
        return self._own_title_bar

    def minimize(self) -> None:
        self._window.minimize()

    def toggle_maximize(self) -> None:
        if self.is_maximized():
            self._window.restore()
        else:
            self._window.maximize()

    def is_maximized(self) -> bool:
        return str(self._window.native.WindowState) == "Maximized"

    def close(self) -> None:
        self._window.destroy()


# A window procedure the frame keeps calling, so it lives as long as the process
_caption_proc = None


def _hide_caption(form) -> bool:
    """Make the UI's own top bar the title bar, as PyCharm does, instead of a white
    Windows caption above it (user feedback). Only the caption goes: pywebview's frameless
    mode drops the whole frame, and with it edge resizing, snapping and the shadow. The
    frame's non-client area is recalculated without its caption strip (WM_NCCALCSIZE), so
    the page starts at the window's top; the page marks where it can be dragged with CSS
    app-region (see _drag_regions). Runs on the window's thread, before it is shown."""
    import ctypes
    from ctypes import wintypes

    lresult = ctypes.c_ssize_t
    subclass_proc = ctypes.WINFUNCTYPE(
        lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t
    )
    comctl32 = ctypes.windll.comctl32
    comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    comctl32.DefSubclassProc.restype = lresult
    comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, subclass_proc, ctypes.c_size_t, ctypes.c_size_t]
    user32 = ctypes.windll.user32
    user32.IsZoomed.argtypes = [wintypes.HWND]

    def proc(hwnd, message, wparam, lparam, _id, _data):
        if message == 0x0083 and wparam:  # WM_NCCALCSIZE: rgrc[0] is the window, in, and the client, out
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            top, left = rect.top, rect.left
            result = comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
            # keep the side and bottom frame (the resize edges), not the caption; a
            # maximised window hangs its frame off the screen, so keep that much at the top
            rect.top = top + (rect.left - left if user32.IsZoomed(hwnd) else 0)
            return result
        return comctl32.DefSubclassProc(hwnd, message, wparam, lparam)

    global _caption_proc
    _caption_proc = subclass_proc(proc)
    hwnd = form.Handle.ToInt64()
    if not comctl32.SetWindowSubclass(hwnd, _caption_proc, 1, 0):
        return False
    # SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER: recalculate the frame now
    user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, 0x20 | 0x2 | 0x1 | 0x4)
    # Without a caption a light page on a light desktop had no edge at all - "the app
    # feels borderless" (user feedback). Windows 11 draws a 1px border in any colour:
    # the top bar's navy (#2d3047, as a COLORREF). Windows 10 ignores it.
    navy = ctypes.c_int(0x2D | 0x30 << 8 | 0x47 << 16)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(navy), 4)
    return True


def _drag_regions() -> None:
    """Let the page say where the window can be dragged (CSS `app-region: drag` on its top
    bar), with Windows' own dragging: Aero Snap, double-click to maximise. It's a WebView2
    setting that applies from the next page load, so it's set as WebView2 starts, before
    pywebview loads the UI."""
    from webview.platforms.edgechromium import EdgeChrome

    ready = EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        try:
            sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
        except Exception:  # noqa: BLE001 - an older WebView2: the bar just doesn't drag
            logging.exception("no drag regions in this WebView2")
        ready(self, sender, args)

    EdgeChrome.on_webview_ready = on_webview_ready


def _app_window(server, url: str, data: Path) -> None:
    _unblock_bundle()
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator

        # pythonnet loads .NET only when the Windows backend is chosen, at start(); load
        # it now so a failure lands here rather than after the window was asked for
        if sys.platform == "win32":
            import clr  # noqa: F401
    except Exception as error:  # noqa: BLE001 - any failure here means no window
        _browser_fallback(url, error)
        server.should_exit = True
        return

    # The server is ready ~0.1s after Python starts (app.log); the window opens once it
    # is, so it never shows a connection error first.
    deadline = time.monotonic() + 30
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        webview.create_window(
            APP_NAME,
            html=f"<p style='font:15px sans-serif;padding:24px'>{APP_NAME} couldn't start - "
            f"see app.log in {data}</p>",
            width=520,
            height=160,
        )
        webview.start()
        return

    # exported question papers and mark schemes are downloads; a webview refuses them
    # unless told otherwise (WebView2 saves to Downloads, WebKit asks where)
    webview.settings["ALLOW_DOWNLOADS"] = True
    desktop = Desktop(data, url)
    window = webview.create_window(
        APP_NAME,
        url,
        width=1440,
        height=900,
        min_size=(960, 600),
        maximized=True,  # user-asked: opens filling the screen; width/height are its restored size
        text_select=True,
        background_color="#FFFFFF",
        js_api=desktop,
    )
    desktop._window = window
    # On Windows the UI's own top bar carries what the File menu did, and a menu strip
    # under the title bar looked bolted on (user feedback); a Mac's menu bar is the norm.
    menu = []
    if sys.platform == "win32":

        def own_title_bar(window) -> None:
            try:
                desktop._own_title_bar = _hide_caption(window.native)
            except Exception:  # noqa: BLE001 - the Windows caption stays, which still works
                logging.exception("couldn't make the top bar the title bar")

        _drag_regions()
        window.events.before_show += own_title_bar
    else:
        menu = [
            Menu(
                "File",
                [
                    MenuAction("Open data folder", desktop.open_data_folder),
                    MenuAction("Open in browser", desktop.open_in_browser),
                    MenuSeparator(),
                    MenuAction("Reload", lambda: window.load_url(url)),
                ],
            )
        ]
    # private_mode off with storage in the data folder, so the UI's own preferences
    # (theme, last tab) survive a restart
    try:
        webview.start(menu=menu, private_mode=False, storage_path=str(data / "webview"))
    except Exception as error:  # noqa: BLE001 - e.g. no WebView2 runtime
        _browser_fallback(url, error)
    server.should_exit = True


if __name__ == "__main__":
    # A scan extracts papers in worker processes (qbx.scan.extract_pair). Frozen on
    # Windows and macOS, each worker starts this program again: without this it would run
    # main() and open another app window per worker, instead of running its task.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
