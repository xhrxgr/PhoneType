"""PhoneType 系统托盘版入口。

- 在后台线程运行 HTTP/WebSocket 服务（复用 server.py）。
- 系统托盘图标：左键 / “连接信息” 弹出窗口（局域网地址 + 二维码供手机扫码）。
- “退出” 优雅停止服务并退出。
- 无控制台窗口（打包时使用 --windowed，GUI 子系统，不显示任何 cmd 窗口）。
"""

import asyncio
import ctypes
import logging
import os
import socket
import sys
import threading
import traceback

import server  # 复用服务端与工具函数

import pystray
from pystray import Icon, Menu, MenuItem
import qrcode
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageDraw, ImageFont, ImageTk


# 版本号（与 git tag 保持一致）
APP_VERSION = "1.2.3"
APP_NAME = "PhoneType"
APP_REPO = "https://github.com/xhrxgr/PhoneType"
APP_AUTHOR = "xhrxgr"
APP_DESC = "把手机变成电脑的无线输入法：在手机上打字 / 用快捷键，实时键入到电脑当前窗口。"
APP_AUMID = "PhoneType.PhoneType.1"  # 任务栏图标归属：否则任务栏沿用默认的 Python/Tk 图标

# 开机自启动的注册表位置
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PhoneType"
# 开机自启动时带上这个参数，程序据此静默启动（不自动弹出连接信息窗口）
STARTUP_ARG = "--startup"


def install_crash_handler(log_file):
    """捕获未处理异常：写日志 + 弹原生错误框，避免隐藏控制台后崩溃无迹可寻。"""
    def show_box(title, text):
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
        except Exception:
            pass

    def handle(exc_type, exc_value, tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, tb))
        try:
            logging.error("未捕获异常:\n%s", msg)
        except Exception:
            pass
        show_box(
            "PhoneType 崩溃",
            f"程序发生未捕获异常，日志已保存到：\n{log_file}\n\n"
            f"{msg[-1500:]}",
        )

    def handle_thread(args):
        handle(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = handle
    threading.excepthook = handle_thread


def make_icon_image():
    """托盘图标：优先用 assets/app.ico（多尺寸正式图标），缺失时回退到程序内绘制。"""
    ico = resource_path("app.ico")
    if os.path.exists(ico):
        try:
            return Image.open(ico).convert("RGBA")
        except Exception:
            pass
    # 回退：蓝色圆角方块 + 白色输入光标
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, size - 4, size - 4], radius=14, fill=(37, 99, 235, 255))
    cw = max(1, size // 16)
    d.rounded_rectangle(
        [size // 2 - cw / 2, size * 0.20, size // 2 + cw / 2, size * 0.54],
        radius=cw // 2,
        fill=(255, 255, 255, 255),
    )
    return img


def _apply_app_icon(win):
    """给 tk 窗口设置正式图标（任务栏 / 标题栏）。

    优先用 iconbitmap（.ico 原生），失败回退到 ImageTk iconphoto。
    设为默认图标后，所有后续 Toplevel（连接信息 / 关于）自动继承，不再显示 Python 羽毛。
    """
    ico = resource_path("app.ico")
    if not os.path.exists(ico):
        return
    try:
        win.iconbitmap(ico)
    except Exception:
        pass
    try:
        img = ImageTk.PhotoImage(Image.open(ico).convert("RGBA"))
        win.iconphoto(True, img)
        win._app_icon_ref = img  # 防止被 GC
    except Exception:
        pass


def _set_app_user_model_id():
    """显式声明进程身份，让任务栏把窗口归到本程序名下并使用 exe 的图标。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_AUMID)
    except Exception:
        pass


def resource_path(rel: str) -> str:
    """定位资源：打包后从 PyInstaller 临时目录取，未打包时从脚本邻近 assets 取。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    return os.path.join(base, rel)


def connection_urls():
    ips = server.lan_ips()
    return [f"http://{ip}:{server.HTTP_PORT}/" for ip in ips]


def _win32_set_clipboard(text):
    """直接用 Win32 API 写剪贴板，不依赖 Tk 窗口焦点（之前 Tk 方式静默失败）。"""
    try:
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data) + 2)
            if not h:
                return False
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                kernel32.GlobalFree(h)
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                kernel32.GlobalFree(h)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def _copy_to_clipboard(root, text):
    """写入剪贴板：优先 Win32 原生 API，失败再回退到 Tk 剪贴板。"""
    if _win32_set_clipboard(text):
        return True
    try:
        if root is None:
            return False
        root.clipboard_clear()
        root.clipboard_append(text)
        return True
    except Exception:
        return False


# ------------------------- 窗口管理（单例 / 置前 / 无闪烁） -------------------------
_open_windows = {}


def _set_native_icon(win):
    """Windows 下用 WM_SETICON 设置窗口的大/小图标。

    tkinter 的 iconphoto 只影响标题栏，任务栏图标由窗口 HICON 决定，
    必须走 Win32 API 才能真正换掉默认的 Python/Tk 图标。
    """
    try:
        user32 = ctypes.windll.user32
        ico = resource_path("app.ico")
        if not os.path.exists(ico):
            return
        LoadImageW = user32.LoadImageW
        LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        LoadImageW.restype = ctypes.c_void_p
        SendMessageW = user32.SendMessageW
        SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_size_t]
        SendMessageW.restype = ctypes.c_ssize_t

        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1

        hwnd = ctypes.c_void_p(int(win.winfo_id()))
        for size, which in ((32, ICON_BIG), (16, ICON_SMALL)):
            hicon = LoadImageW(None, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if hicon:
                SendMessageW(hwnd, WM_SETICON, which, hicon)
    except Exception:
        pass


def _center_window(win):
    try:
        win.update_idletasks()
        w = win.winfo_width()
        h = win.winfo_height()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        win.geometry(f"+{x}+{y}")
    except Exception:
        pass


def _cancel_topmost(win):
    try:
        if win.winfo_exists():
            win.attributes("-topmost", False)
    except Exception:
        pass


def _present_window(win):
    """显示窗口、设原生图标并带到前台（短暂置顶后取消，避免永远压住别的窗口）。"""
    try:
        win.deiconify()
    except Exception:
        pass
    _center_window(win)
    _set_native_icon(win)
    try:
        win.lift()
        win.attributes("-topmost", True)
        win.focus_force()
        win.after(300, lambda: _cancel_topmost(win))
    except Exception:
        pass


def open_single_window(root, key, builder):
    """同类型窗口只开一个：已存在则提到前台，否则新建。"""
    win = _open_windows.get(key)
    if win is not None:
        try:
            if win.winfo_exists():
                _present_window(win)
                return win
        except Exception:
            pass
        _open_windows.pop(key, None)
    win = builder()
    _open_windows[key] = win

    def _forget(_event=None):
        # 无论从标题栏 X 还是窗口内的「关闭」按钮销毁，都要清掉记录。
        # 之前只绑了 WM_DELETE_WINDOW，而关闭按钮是 win.destroy（不走 protocol），
        # 于是残留已销毁的引用，成为重复开窗的隐患。
        if _open_windows.get(key) is win:
            _open_windows.pop(key, None)

    try:
        win.bind("<Destroy>", _forget)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
    except Exception:
        pass
    return win


def show_about(root):
    """关于窗口：版本 / 说明 / 仓库 / 作者。"""
    win = tk.Toplevel(root)
    # 先隐藏再构建，避免空窗口先映射出来的黑框闪烁
    win.withdraw()
    win.title(f"关于 {APP_NAME}")
    win.resizable(False, False)
    win.configure(bg="#ffffff")
    _apply_app_icon(win)

    # 图标
    try:
        ico = resource_path("app.ico")
        if os.path.exists(ico):
            im = Image.open(ico).convert("RGBA").resize((64, 64))
            tk_img = ImageTk.PhotoImage(im)
            lbl = tk.Label(win, image=tk_img, bg="#ffffff")
            lbl.image = tk_img
            lbl.pack(pady=(18, 6))
    except Exception:
        pass

    tk.Label(win, text=APP_NAME, font=("Arial", 18, "bold"), bg="#ffffff", fg="#1f2937").pack()
    tk.Label(win, text=f"版本 {APP_VERSION}", font=("Arial", 11), bg="#ffffff", fg="#6b7280").pack(pady=(2, 10))

    desc = tk.Label(
        win,
        text=APP_DESC,
        font=("Arial", 10),
        bg="#ffffff",
        fg="#374151",
        wraplength=320,
        justify="center",
    )
    desc.pack(padx=20, pady=(0, 10))

    info = tk.Label(
        win,
        text=f"作者：{APP_AUTHOR}\n仓库：{APP_REPO}",
        font=("Consolas", 9),
        bg="#ffffff",
        fg="#9ca3af",
        justify="center",
    )
    info.pack(padx=20, pady=(0, 6))

    def open_repo():
        try:
            os.startfile(APP_REPO)
        except Exception:
            pass

    btn_row = tk.Frame(win, bg="#ffffff")
    btn_row.pack(pady=(6, 16))
    tk.Button(btn_row, text="打开仓库", command=open_repo, width=12).pack(side="left", padx=6)
    tk.Button(btn_row, text="关闭", command=win.destroy, width=12).pack(side="left", padx=6)

    _present_window(win)
    return win


def build_info_window(root):
    """在 tk 主线程中构建连接信息窗口（含二维码，可切换二维码对应的 IP）。"""
    import tkinter.ttk as ttk

    urls = connection_urls()
    win = tk.Toplevel(root)
    # 先隐藏再构建内容，避免 Tk 先把空窗口映射出来（黑框闪一下）再填充 UI
    win.withdraw()
    win.title(f"{APP_NAME} · 连接信息")
    win.resizable(False, False)
    win.configure(bg="#f5f6fa")
    _apply_app_icon(win)

    head = tk.Frame(win, bg="#2563eb")
    head.pack(fill="x")
    tk.Label(
        head, text="连接信息", font=("Arial", 14, "bold"),
        bg="#2563eb", fg="white", padx=16, pady=10,
    ).pack(anchor="w")

    body = tk.Frame(win, bg="#f5f6fa")
    body.pack(padx=16, pady=12, fill="x")

    tk.Label(
        body, text="手机扫码或浏览器打开以下地址（同一 WiFi）：",
        font=("Arial", 10), bg="#f5f6fa", fg="#374151",
    ).pack(anchor="w", pady=(0, 6))

    # 切换二维码对应地址（默认第一个 = 最可能连路由器的 IP）
    frm = tk.Frame(body, bg="#f5f6fa")
    frm.pack(fill="x", pady=(0, 6))
    tk.Label(frm, text="二维码地址：", bg="#f5f6fa", fg="#374151").pack(side="left")
    cb = ttk.Combobox(frm, values=urls, state="readonly", width=34)
    if urls:
        cb.current(0)
    cb.pack(side="left", padx=4)

    # 二维码区（可随下拉切换重画）
    qr_frame = tk.Frame(body, bg="#ffffff", relief="solid", bd=1)
    qr_frame.pack(pady=8)

    def redraw(url):
        for w in list(qr_frame.children.values()):
            w.destroy()
        if not url:
            return
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").resize((180, 180))
        tk_img = ImageTk.PhotoImage(img)
        lbl = tk.Label(qr_frame, image=tk_img, bg="#ffffff")
        lbl.image = tk_img  # 防止被 GC
        lbl.pack(padx=8, pady=8)

    if urls:
        redraw(urls[0])
    cb.bind("<<ComboboxSelected>>", lambda e: redraw(cb.get()))

    # 地址列表 + 复制
    list_frame = tk.Frame(body, bg="#f5f6fa")
    list_frame.pack(fill="x", pady=(4, 0))
    for u in urls:
        row = tk.Frame(list_frame, bg="#f5f6fa")
        row.pack(fill="x", pady=2)
        tk.Label(row, text=u, font=("Consolas", 10), bg="#f5f6fa", fg="#1f2937").pack(side="left")
        btn = tk.Button(
            row, text="复制", width=6,
            command=lambda u=u, r=row: _on_copy(root, u, r),
        )
        btn.pack(side="right", padx=4)

    tk.Label(
        body,
        text="提示：先在电脑点一下要输入的目标窗口，再在手机上打字 / 语音。\n"
             "若扫码连不上，换上方下拉里的其它地址试试。",
        font=("Arial", 9), bg="#f5f6fa", fg="#9ca3af", justify="left",
    ).pack(anchor="w", pady=(8, 0))

    foot = tk.Frame(win, bg="#f5f6fa")
    foot.pack(fill="x", pady=(4, 14))
    tk.Button(foot, text="关闭", command=win.destroy, width=14).pack()

    _present_window(win)
    return win


def _on_copy(root, text, row):
    if _copy_to_clipboard(root, text):
        # 在按钮旁短暂显示「已复制」
        for w in list(row.children.values()):
            if isinstance(w, tk.Button):
                w.config(text="已复制", state="disabled")
                row.after(1200, lambda w=w: w.config(text="复制", state="normal"))


def show_info(root):
    """打开连接信息窗口：已开着就直接提到前台，避免重复弹出多个。"""
    root.after(0, lambda: open_single_window(root, "info", lambda: build_info_window(root)))


def quit_app(icon, root):
    icon.stop()
    try:
        root.destroy()
    except Exception:
        pass


# ------------------------- 单实例控制 -------------------------
# 命名互斥体保证同一时刻只有一个 PhoneType 真正提供服务；
# 若另一个实例已在运行，则唤醒它并提示用户，自身退出，避免端口冲突。
MUTEX_NAME = "PhoneType_SingleInstance_9d3f2a1b"
WAKE_PORT = 18765
_mutex_handle = None


# ------------------------- 开机自启动 -------------------------
def _get_self_exe_path():
    """返回当前可执行文件路径：打包后是 exe，未打包时是本脚本。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def autostart_is_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            val, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(val)
    except Exception:
        return False


def autostart_set(enable: bool):
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                winreg.SetValueEx(
                    key, RUN_VALUE, 0, winreg.REG_SZ,
                    f'"{_get_self_exe_path()}" {STARTUP_ARG}'
                )
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def is_startup_launch():
    """本次是否由开机自启动拉起（命令行带 --startup）。"""
    args = [str(a).lower() for a in sys.argv[1:]]
    return STARTUP_ARG.lower() in args or "--silent" in args


def ensure_autostart_arg():
    """旧版本写入的自启动项没有 --startup，这里补上，保证开机后是静默启动。"""
    if not autostart_is_enabled():
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            val, _ = winreg.QueryValueEx(key, RUN_VALUE)
        if STARTUP_ARG.lower() not in str(val).lower():
            autostart_set(True)
    except Exception:
        pass


def already_running():
    """尝试创建命名互斥体。已存在（ERROR_ALREADY_EXISTS=183）说明另一实例在运行。"""
    kernel32 = ctypes.windll.kernel32
    h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(h)
        return True
    global _mutex_handle
    _mutex_handle = h  # 进程生命周期内持有，退出时自动释放
    return False


def _signal_existing_instance():
    """给已在运行的实例发一个本地 UDP 唤醒包，让它弹出连接信息窗口。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"wake", ("127.0.0.1", WAKE_PORT))
        s.close()
    except Exception:
        pass


def _start_wake_listener(root):
    """首个实例监听本地 UDP 唤醒包，收到时弹出连接信息窗口。"""
    def loop():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", WAKE_PORT))
        except Exception:
            return
        while True:
            try:
                s.recvfrom(1024)
            except Exception:
                break
            try:
                # 必须走单例：否则每次启动第二个实例发来唤醒包都会新建一个窗口
                root.after(0, lambda: open_single_window(
                    root, "info", lambda: build_info_window(root)))
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()


def _show_port_conflict_box():
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"PhoneType 无法启动：端口 {server.HTTP_PORT} / {server.WS_PORT} 已被占用。\n"
            "请先关闭其它 PhoneType 实例或相关程序后重试。",
            "PhoneType",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


def _quit_clean(root):
    try:
        root.destroy()
    except Exception:
        pass
    os._exit(0)


def main():
    # 单实例优先：若已在运行，直接把它的窗口唤到前台并退出自身。
    # 不再弹「已经在运行」提示框，避免重复启动时多一次打扰。
    if already_running():
        _signal_existing_instance()
        os._exit(0)
        return

    log_file = server.setup_logging()
    install_crash_handler(log_file)
    logging.info("PhoneType 托盘版启动")

    # 声明任务栏身份，确保任务栏用的是本程序图标而非默认的 Python/Tk 图标
    _set_app_user_model_id()

    root = tk.Tk()
    root.withdraw()
    root.title("PhoneType")
    _apply_app_icon(root)

    # 首个实例：监听唤醒包，收到时弹出连接信息窗口
    _start_wake_listener(root)

    # 后台线程运行服务；端口被占用时给出清晰提示而非静默崩
    def run_server():
        try:
            asyncio.run(server.main())
        except OSError as e:
            if getattr(e, "winerror", None) == 10048 or "10048" in str(e) \
                    or "address already" in str(e).lower():
                _show_port_conflict_box()
            else:
                logging.exception("服务异常退出")
            try:
                root.after(0, lambda: _quit_clean(root))
            except Exception:
                pass

    threading.Thread(target=run_server, daemon=True).start()

    icon_img = make_icon_image()

    def open_log(_icon, _item):
        try:
            os.startfile(log_file)
        except Exception:
            pass

    def open_log_dir(_icon, _item):
        try:
            os.startfile(os.path.dirname(log_file))
        except Exception:
            pass

    def toggle_autostart(icon, item):
        enable = not autostart_is_enabled()
        if not autostart_set(enable):
            messagebox.showerror(APP_NAME, "无法修改开机自启动设置（权限不足）。")
            return
        # 更新菜单勾选态
        rebuild_menu()

    def rebuild_menu():
        menu = Menu(
            MenuItem("连接信息", lambda icon, item: show_info(root), default=True),
            Menu.SEPARATOR,
            MenuItem("关于", lambda icon, item: root.after(
                0, lambda: open_single_window(root, "about", lambda: show_about(root)))),
            MenuItem("开机自启动", toggle_autostart, checked=lambda item: autostart_is_enabled()),
            Menu.SEPARATOR,
            MenuItem("查看日志", open_log),
            MenuItem("打开日志目录", open_log_dir),
            Menu.SEPARATOR,
            MenuItem("退出", lambda icon, item: quit_app(icon, root)),
        )
        icon.menu = menu

    menu = Menu(
        MenuItem("连接信息", lambda icon, item: show_info(root), default=True),
        Menu.SEPARATOR,
        MenuItem("关于", lambda icon, item: root.after(0, lambda: show_about(root))),
        MenuItem("开机自启动", toggle_autostart, checked=lambda item: autostart_is_enabled()),
        Menu.SEPARATOR,
        MenuItem("查看日志", open_log),
        MenuItem("打开日志目录", open_log_dir),
        Menu.SEPARATOR,
        MenuItem("退出", lambda icon, item: quit_app(icon, root)),
    )
    icon = Icon("PhoneType", icon_img, f"{APP_NAME} 输入中继", menu)
    threading.Thread(target=icon.run, daemon=True).start()

    # 开机自启动时静默进托盘，不弹连接信息窗口；手动启动才弹出
    ensure_autostart_arg()  # 旧版自启动项补上 --startup
    if is_startup_launch():
        logging.info("开机自启动：静默运行，不弹出连接信息窗口")
    else:
        # 弹一次连接信息（走同一单例，避免与手动点击重叠）
        root.after(600, lambda: open_single_window(
            root, "info", lambda: build_info_window(root)))
    root.mainloop()

    # 窗口销毁后退出
    logging.info("PhoneType 退出")
    server.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
