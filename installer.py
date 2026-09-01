"""PhoneType 安装器：安装 / 更新 / 卸载。

打包时把主程序 PhoneType.exe 一起打进来（--add-data "dist/PhoneType.exe;."），
产出一个自包含的 PhoneTypeSetup.exe：

  无参数运行   打开图形界面（安装 / 更新 / 卸载）
  /SILENT      静默安装或更新
  /uninstall   卸载（「添加或删除程序」里调用的也是这个）

安装在 %LOCALAPPDATA%\\Programs\\PhoneType，装在用户目录下，不需要管理员权限。
安装、更新、卸载前都会先检测并关闭正在运行的 PhoneType。
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox

try:
    import winreg
except ImportError:  # 非 Windows
    winreg = None

APP_NAME = "PhoneType"
PUBLISHER = "xhrxgr"
EXE_NAME = APP_NAME + ".exe"
SETUP_NAME = "PhoneTypeSetup.exe"
UNINSTALL_SUB = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 起子进程时隐藏控制台窗口（tasklist / taskkill / powershell 都是控制台程序）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------- 路径与环境 -------------------------
def install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def bundled_exe():
    """随安装包打包进来的 PhoneType.exe 路径。"""
    if getattr(sys, "frozen", False):
        cand = [os.path.join(sys._MEIPASS, EXE_NAME)]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        cand = [os.path.join(here, "dist", EXE_NAME), os.path.join(here, EXE_NAME)]
    for p in cand:
        if os.path.exists(p):
            return p
    return None


def shortcuts():
    """返回 (开始菜单, 桌面) 快捷方式路径。"""
    start = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                         "Start Menu", "Programs", APP_NAME + ".lnk")
    desktop = os.path.join(os.path.expanduser("~"), "Desktop", APP_NAME + ".lnk")
    return start, desktop


# ------------------------- 版本读取 -------------------------
def file_version(path):
    """读取 exe 的 ProductVersion（用 Windows 版本 API，不启动子进程）。"""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        # 先取语言/代码页，再按它读 ProductVersion
        lp = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                              ctypes.byref(lp), ctypes.byref(ln)) and ln.value >= 2:
            pair = ctypes.cast(lp, ctypes.POINTER(ctypes.c_ushort * 2)).contents
            lang, cp = int(pair[0]), int(pair[1])
        else:
            lang, cp = 0x0409, 1200
        lp2 = ctypes.c_void_p()
        ln2 = ctypes.c_uint()
        sub = "\\StringFileInfo\\%04x%04x\\ProductVersion" % (lang, cp)
        if ver.VerQueryValueW(buf, sub, ctypes.byref(lp2), ctypes.byref(ln2)) and ln2.value:
            return ctypes.wstring_at(lp2.value, ln2.value - 1)
        return None
    except Exception:
        return None


# ------------------------- 运行中检测与关闭 -------------------------
def running_pids():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + EXE_NAME, "/NH"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_NO_WINDOW)
    except Exception:
        return []
    pids = []
    for line in r.stdout.splitlines():
        if line.lower().startswith(EXE_NAME.lower()):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    return pids


def stop_app(timeout=15.0):
    """关闭正在运行的 PhoneType，返回是否已全部退出。"""
    if not running_pids():
        return True
    try:
        subprocess.run(["taskkill", "/F", "/IM", EXE_NAME],
                       capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running_pids():
            return True
        time.sleep(0.3)
    return not running_pids()


# ------------------------- 快捷方式 -------------------------
def create_shortcut(lnk_path, target, desc=""):
    """创建 .lnk。路径用环境变量传参，避免引号/转义问题。"""
    try:
        os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    except Exception:
        pass
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut($env:PT_LNK); "
        "$s.TargetPath = $env:PT_TGT; "
        "$s.WorkingDirectory = $env:PT_DIR; "
        "$s.IconLocation = $env:PT_TGT + ',0'; "
        "$s.Description = $env:PT_DESC; "
        "$s.Save()"
    )
    env = dict(os.environ,
               PT_LNK=lnk_path, PT_TGT=target,
               PT_DIR=os.path.dirname(target), PT_DESC=desc)
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=30,
                       creationflags=_NO_WINDOW, env=env)
    except Exception:
        return False
    return os.path.exists(lnk_path)


def remove_shortcuts():
    for p in shortcuts():
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


# ------------------------- 注册表 -------------------------
def installed_version():
    """已安装版本号；未安装返回 None。"""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_SUB) as key:
            ver, _ = winreg.QueryValueEx(key, "DisplayVersion")
            return ver
    except Exception:
        return None


def write_uninstall(version, exe_path):
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_SUB,
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                              os.path.dirname(exe_path))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, exe_path + ",0")
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                              '"%s" /uninstall' % os.path.join(
                                  os.path.dirname(exe_path), SETUP_NAME))
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        return True
    except Exception:
        return False


def remove_uninstall():
    """移除卸载信息，顺带清掉开机自启动项。"""
    if winreg is None:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_SUB)
    except Exception:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except Exception:
        pass


# ------------------------- 安装 / 卸载 -------------------------
def do_install(log, make_desktop=False):
    src = bundled_exe()
    if not src:
        log("[错误] 安装包内找不到 " + EXE_NAME)
        return False
    ver = file_version(src) or "未知版本"

    if running_pids():
        log("- 检测到 PhoneType 正在运行，正在关闭...")
        if not stop_app():
            log("[错误] 无法关闭运行中的 PhoneType，请手动退出后重试")
            return False
        log("- 已关闭")

    d = install_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        log("[错误] 无法创建安装目录: " + str(e))
        return False

    dst = os.path.join(d, EXE_NAME)
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        log("[错误] 复制主程序失败: " + str(e))
        return False
    log("- 主程序: " + dst)

    # 把安装器自身也放一份，供「添加或删除程序」卸载、以及后续更新使用
    try:
        shutil.copy2(sys.executable, os.path.join(d, SETUP_NAME))
    except Exception:
        pass

    start_lnk, desk_lnk = shortcuts()
    if create_shortcut(start_lnk, dst, "把手机当电脑的无线输入法"):
        log("- 已创建开始菜单快捷方式")
    if make_desktop:
        if create_shortcut(desk_lnk, dst, "把手机当电脑的无线输入法"):
            log("- 已创建桌面快捷方式")

    write_uninstall(ver, dst)
    log("- 已写入卸载信息（可在「添加或删除程序」中卸载）")
    log("")
    log("[完成] 安装完成: " + APP_NAME + " " + ver)
    return True


def schedule_delete(directory):
    """安装器自己删不掉自己，交给一个临时脚本在退出后清理。"""
    bat = os.path.join(os.environ.get("TEMP", "."), APP_NAME + "_uninstall.bat")
    try:
        with open(bat, "w", encoding="utf-8") as f:
            f.write("@echo off\n"
                    "ping -n 3 127.0.0.1 >nul\n"
                    'rmdir /s /q "%s"\n'
                    'del "%%~f0"\n' % directory)
        subprocess.Popen(["cmd", "/c", "start", "", bat],
                         creationflags=_NO_WINDOW)
    except Exception:
        pass


def do_uninstall(log):
    if running_pids():
        log("- 正在关闭运行中的 PhoneType...")
        stop_app()

    remove_shortcuts()
    log("- 已删除快捷方式")
    remove_uninstall()
    log("- 已移除卸载信息与开机自启动项")

    d = install_dir()
    if os.path.isdir(d):
        schedule_delete(d)
        log("- 已排定删除 " + d)
    log("")
    log("[完成] 卸载完成")
    return True


# ------------------------- 图形界面 -------------------------
class SetupUI:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME + " 安装")
        root.resizable(False, False)
        self._set_icon(root)

        head = tk.Frame(root, bg="#2563eb")
        head.pack(fill="x")
        tk.Label(head, text=APP_NAME, font=("Arial", 15, "bold"),
                 bg="#2563eb", fg="white", padx=16, pady=10).pack(anchor="w")

        body = tk.Frame(root, bg="#f5f6fa")
        body.pack(padx=16, pady=12, fill="x")

        self.status = tk.Label(body, text="", font=("Arial", 11),
                               bg="#f5f6fa", fg="#1f2937", justify="left")
        self.status.pack(anchor="w", pady=(0, 8))

        self.desk_var = tk.BooleanVar(value=False)
        self.desk_cb = tk.Checkbutton(body, text="同时创建桌面快捷方式",
                                      variable=self.desk_var, bg="#f5f6fa",
                                      fg="#374151", anchor="w")
        self.desk_cb.pack(anchor="w", pady=(0, 8))

        btn_row = tk.Frame(body, bg="#f5f6fa")
        btn_row.pack(fill="x")
        self.main_btn = tk.Button(btn_row, text="安装", width=14,
                                  command=self.on_main)
        self.main_btn.pack(side="left")
        self.uninst_btn = tk.Button(btn_row, text="卸载", width=14,
                                    command=self.on_uninstall, state="disabled")
        self.uninst_btn.pack(side="left", padx=8)
        tk.Button(btn_row, text="关闭", width=10, command=root.destroy).pack(side="right")

        self.log = tk.Text(root, height=10, width=56, font=("Consolas", 9),
                           bg="#ffffff", relief="solid", bd=1)
        self.log.pack(padx=16, pady=(0, 14), fill="both")

        self.refresh()

    def _set_icon(self, win):
        try:
            base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "assets")
            ico = os.path.join(base, "app.ico")
            if os.path.exists(ico):
                win.iconbitmap(ico)
        except Exception:
            pass

    def write(self, msg=""):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.root.update_idletasks()

    def refresh(self):
        cur = installed_version()
        src = bundled_exe()
        new = file_version(src) if src else None
        if cur:
            self.status.config(text="已安装：%s %s" % (APP_NAME, cur))
            self.uninst_btn.config(state="normal")
            if new and new != cur:
                self.main_btn.config(text="更新到 " + new)
                self.desk_cb.config(state="disabled")
            else:
                self.main_btn.config(text="重新安装")
                self.desk_cb.config(state="normal")
        else:
            self.status.config(text="尚未安装 %s %s" % (APP_NAME, new or ""))
            self.main_btn.config(text="安装")
            self.uninst_btn.config(state="disabled")
            self.desk_cb.config(state="normal")

    def on_main(self):
        self.main_btn.config(state="disabled")
        self.uninst_btn.config(state="disabled")
        self.log.delete("1.0", "end")
        ok = do_install(self.write, make_desktop=self.desk_var.get())
        self.main_btn.config(state="normal")
        self.refresh()
        if ok:
            messagebox.showinfo(APP_NAME, "完成。可从开始菜单启动 " + APP_NAME + "。")

    def on_uninstall(self):
        if not messagebox.askyesno(APP_NAME, "确定要卸载 " + APP_NAME + " 吗？"):
            return
        self.main_btn.config(state="disabled")
        self.uninst_btn.config(state="disabled")
        self.log.delete("1.0", "end")
        do_uninstall(self.write)
        self.refresh()
        messagebox.showinfo(APP_NAME, "已卸载。安装程序即将关闭。")
        self.root.after(300, self.root.destroy)


def silent_log():
    """静默模式下的日志：写文件而不是 print。

    --windowed 程序没有控制台，stdout 还可能是 GBK 编码，
    直接 print 中文/符号会抛 UnicodeEncodeError。
    """
    path = os.path.join(os.environ.get("TEMP", "."), APP_NAME + "_setup.log")
    try:
        open(path, "w", encoding="utf-8").close()  # 每次运行先清空
    except Exception:
        pass

    def _w(msg=""):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    return _w


def main():
    # 防御：即使有 stdout 也统一按 UTF-8 输出，避免 GBK 编码错误
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a.lower() for a in sys.argv[1:]]
    silent = "/silent" in args or "/verysilent" in args
    uninstall = "/uninstall" in args

    if uninstall:
        root = tk.Tk()
        root.withdraw()
        if not silent:
            if not messagebox.askyesno(APP_NAME, "确定要卸载 " + APP_NAME + " 吗？"):
                return
        do_uninstall(silent_log() if silent else (lambda m: None))
        if not silent:
            messagebox.showinfo(APP_NAME, "已卸载 " + APP_NAME + "。")
        return

    if silent:
        do_install(silent_log())
        return

    root = tk.Tk()
    SetupUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
