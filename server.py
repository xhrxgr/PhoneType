import asyncio
import ctypes
import http.server
import json
import logging
import os
import socket
import sys
import threading
import time
from ctypes import wintypes


def resource_path(rel: str) -> str:
    """定位资源文件：打包后从 PyInstaller 临时目录取，未打包时从脚本目录取。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def setup_logging():
    """配置全局日志：始终写到文件；开发态（未打包）额外打印到控制台。

    返回日志文件路径，便于宿主（托盘）提供「查看日志」入口。幂等。
    """
    log_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "PhoneType")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        log_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(log_dir, "app.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8", errors="replace")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass
        if not getattr(sys, "frozen", False):
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root.addHandler(sh)
    return log_file

import websockets

# ------------------------- 配置 -------------------------
HTTP_PORT = 8765       # 手机网页
WS_PORT = 8766         # 文本键入 WebSocket

current_ws = None          # 当前连接的手机 WebSocket
_lt_lock = threading.Lock()

# ------------------------- Windows 键盘模拟 -------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12      # Alt
VK_A = 0x41
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_HOME = 0x24
VK_END = 0x23
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_PRIOR = 0x21      # PageUp
VK_NEXT = 0x22       # PageDown

# 控制键名称 -> 虚拟键码（手机快捷键用）
KEY_NAME_TO_VK = {
    "left": VK_LEFT, "up": VK_UP, "right": VK_RIGHT, "down": VK_DOWN,
    "home": VK_HOME, "end": VK_END, "delete": VK_DELETE,
    "tab": VK_TAB, "escape": VK_ESCAPE, "enter": VK_RETURN, "backspace": VK_BACK,
    "pgup": VK_PRIOR, "pgdn": VK_NEXT,
}

# 需配合 Ctrl 发送的快捷键（名称 -> 虚拟键码）：如全选/复制/粘贴/撤销
CTRL_KEY_NAME_TO_VK = {
    "selectall": VK_A, "copy": 0x43, "paste": 0x56, "cut": 0x58, "undo": 0x5A,
}

# 手机端回车 → 电脑上发送的组合键：(修饰键, 主键)，修饰键为 None 表示只按主键。
# 默认直接 Enter（只按主键，不附加修饰键）。
DEFAULT_ENTER_MODE = "enter"
ENTER_MODE_TO_COMBO = {
    "shift+enter": (VK_SHIFT, VK_RETURN),
    "ctrl+enter": (VK_CONTROL, VK_RETURN),
    "alt+enter": (VK_MENU, VK_RETURN),
    "enter": (None, VK_RETURN),
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT), ("_pad", ctypes.c_ubyte * 8)]


def _send_input(inp: INPUT):
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def key_down(vk: int = 0, scan: int = 0, flags: int = 0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags
    _send_input(inp)


def key_up(vk: int = 0, scan: int = 0, flags: int = 0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags | KEYEVENTF_KEYUP
    _send_input(inp)


def send_unicode_char(ch: str):
    key_down(scan=ord(ch), flags=KEYEVENTF_UNICODE)
    key_up(scan=ord(ch), flags=KEYEVENTF_UNICODE)


def send_vk(vk: int):
    key_down(vk=vk)
    key_up(vk=vk)


def send_combo(mod_vk, vk: int):
    """按下修饰键 + 主键后再释放，用于 Shift+Enter / Ctrl+Enter 这类组合键。"""
    if mod_vk:
        key_down(mod_vk)
    key_down(vk)
    key_up(vk)
    if mod_vk:
        key_up(mod_vk)


def send_enter_combo(enter_mode: str):
    """把手机端的回车按当前映射发成电脑上的组合键。"""
    mod_vk, vk = ENTER_MODE_TO_COMBO.get(
        enter_mode, ENTER_MODE_TO_COMBO[DEFAULT_ENTER_MODE])
    send_combo(mod_vk, vk)


def type_text(text: str, enter_mode: str = DEFAULT_ENTER_MODE):
    """把一段文本键入当前焦点窗口（中文靠 UNICODE）。

    文本里的换行（手机上按回车）按 enter_mode 映射为电脑上的组合键：
      "enter"       - 直接 Enter（默认，不附加修饰键）
      "ctrl+enter"  - Ctrl+Enter
      "alt+enter"   - Alt+Enter
      "enter"       - Enter（会触发发送/提交）
    """
    for ch in text:
        if ch in ("\n", "\r"):
            send_enter_combo(enter_mode)
        else:
            send_unicode_char(ch)


def select_all():
    key_down(VK_CONTROL)
    key_down(VK_A)
    key_up(VK_A)
    key_up(VK_CONTROL)


def clear_focus():
    """清空当前焦点编辑框（Ctrl+A + Backspace）。"""
    select_all()
    send_vk(VK_BACK)


def send_ctrl_vk(vk: int):
    """发送一个带 Ctrl 修饰的按键（如 Ctrl+A / Ctrl+V）。"""
    key_down(VK_CONTROL)
    key_down(vk=vk)
    key_up(vk=vk)
    key_up(VK_CONTROL)


def send_key_name(name: str):
    """按名称发送单键（方向键/Home/End 等）或带 Ctrl 的组合键。

    返回 True 表示识别并处理了，False 表示未知名称。
    """
    name = (name or "").lower()
    if name in KEY_NAME_TO_VK:
        send_vk(KEY_NAME_TO_VK[name])
        return True
    if name in CTRL_KEY_NAME_TO_VK:
        send_ctrl_vk(CTRL_KEY_NAME_TO_VK[name])
        return True
    return False


# ------------------------- 单向键入（手机 -> 电脑焦点框） -------------------------
# 设计：手机把"本次新增/删除"的增量内容发来，电脑直接在焦点框光标处插入
# （或退格），绝不清空/覆盖电脑已有内容。这样电脑上已打了一半的文字不会被
# 手机输入冲掉，手机语音/打字就像在电脑光标处接着输入一样。
# 不做反向读取（不再依赖 UIA / comtypes，彻底规避此前 _ctypes 原生崩溃问题）。


def insert_text(text: str, enter_mode: str = DEFAULT_ENTER_MODE):
    """把文本直接键入当前焦点窗口的光标处（不选中、不替换已有内容）。

    手机侧负责把"本次新增的文本"作为增量发来，因此这里只做纯插入。
    """
    type_text(text, enter_mode)


# ------------------------- HTTP：提供手机网页 -------------------------
class PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(resource_path(os.path.join("www", "index.html")), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self.send_error(404, "www/index.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # 静默


def start_http():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), PageHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ------------------------- WebSocket：接收文本/按键并键入 -------------------------
async def ws_handler(websocket):
    global current_ws
    current_ws = websocket
    enter_mode = DEFAULT_ENTER_MODE  # 默认直接 Enter
    try:
        async for message in websocket:
            text = message if isinstance(message, str) else message.decode("utf-8", "ignore")
            # 仅 type=="cfg"/"send"/"clear"/"key" 的 JSON 视为控制消息；其余当纯文本键入
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    t = obj.get("type")
                    if t == "cfg":
                        if obj.get("enter") in ENTER_MODE_TO_COMBO:
                            enter_mode = obj["enter"]
                        continue
                    if t == "enter":
                        # 手机软键盘回车：单行输入不会产生换行，由网页单独捕获后发来
                        send_enter_combo(enter_mode)
                        continue
                    if t == "send":
                        # 发送：按当前回车映射发组合键，随后清空两侧
                        send_enter_combo(enter_mode)
                        clear_focus()
                        continue
                    if t == "clear":
                        # 仅清空（不回车）
                        clear_focus()
                        continue
                    if t == "key":
                        # 控制键：方向键 / Home / End / Delete / Tab / Ctrl+组合 等
                        name = obj.get("name", "")
                        if not send_key_name(name):
                            logging.warning("未知控制键：%s", name)
                        continue
            except Exception:
                pass  # 非 JSON（即普通文本）直接键入
            try:
                insert_text(text, enter_mode)
            except Exception as e:
                logging.warning("type error: %s", e)
    finally:
        current_ws = None


# ------------------------- 工具 -------------------------
# 虚拟/隧道网卡别名里常见关键词：这些网卡的 IP 手机通常连不上，需过滤
_VIRTUAL_IFACE_KEYWORDS = (
    "vmware", "virtualbox", "vethernet", "wsl", "docker", "bluetooth",
    "teredo", "pseudo", "loopback", "hyper-v", "zero", "tailscale",
    "tap", "tunnel", "vpn", "utun", "isatap", "6to4", "wi-fi direct",
)


def _is_virtual_iface(alias: str) -> bool:
    a = (alias or "").lower()
    return any(k in a for k in _VIRTUAL_IFACE_KEYWORDS)


def _is_benchmark_ip(ip: str) -> bool:
    """RFC 2544 基准测试保留段（192.18.0.0/15、198.18.0.0/15）。
    这些地址绝不会是真实路由器给手机分配的 LAN 地址，常见于虚拟网卡/代理。"""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    if parts[0] == "192" and parts[1] == "18":
        return True
    if parts[0] == "198" and parts[1] == "18":
        return True
    return False


def _outbound_ip():
    """UDP 出口探测：连一个外部地址（不真正发包），取本机出口 IP。
    这个 IP 通常是路由器给局域网分配的、手机同网段能访问的地址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _enum_ip_config_native():
    """用 Win32 GetAdaptersAddresses 枚举 IPv4 单播地址 + 网卡别名 + 默认网关。

    全程在进程内完成，不启动任何子进程，因此在 GUI 程序里不会弹出控制台窗口
    （这是之前 powershell 方案每次打开连接页面都闪黑色 PowerShell 窗口的根因）。
    返回 [(ip, iface_alias, has_gateway), ...]；不可用或失败返回 []。
    """
    try:
        import ctypes

        AF_INET = 2
        GAA_FLAG_INCLUDE_GATEWAYS = 0x0080  # 注意：0x0002 是 SKIP_ANYCAST，写错会导致网关字段不填充
        ERROR_BUFFER_OVERFLOW = 111

        class SOCKET_ADDRESS(ctypes.Structure):
            _fields_ = [("lpSockaddr", ctypes.c_void_p),
                        ("iSockaddrLength", ctypes.c_int)]

        class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ulong),
                        ("Flags", ctypes.c_ulong),
                        ("Next", ctypes.c_void_p),
                        ("Address", SOCKET_ADDRESS)]

        class IP_ADAPTER_ADDRESSES(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ulong),
                ("IfIndex", ctypes.c_ulong),
                ("Next", ctypes.c_void_p),
                ("AdapterName", ctypes.c_char_p),
                ("FirstUnicastAddress", ctypes.c_void_p),
                ("FirstAnycastAddress", ctypes.c_void_p),
                ("FirstMulticastAddress", ctypes.c_void_p),
                ("FirstDnsServerAddress", ctypes.c_void_p),
                ("DnsSuffix", ctypes.c_wchar_p),
                ("Description", ctypes.c_wchar_p),
                ("FriendlyName", ctypes.c_wchar_p),
                ("PhysicalAddress", ctypes.c_ubyte * 16),
                ("PhysicalAddressLength", ctypes.c_ulong),
                ("Flags", ctypes.c_ulong),
                ("Mtu", ctypes.c_ulong),
                ("IfType", ctypes.c_ulong),
                ("OperStatus", ctypes.c_uint),
                ("Ipv6IfIndex", ctypes.c_ulong),
                ("ZoneIndices", ctypes.c_ulong * 16),
                ("FirstPrefix", ctypes.c_void_p),
                ("TransmitLinkSpeed", ctypes.c_ulonglong),
                ("ReceiveLinkSpeed", ctypes.c_ulonglong),
                ("FirstWinsServerAddress", ctypes.c_void_p),
                ("FirstGatewayAddress", ctypes.c_void_p),
            ]

        fn = ctypes.windll.iphlpapi.GetAdaptersAddresses
        fn.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        fn.restype = ctypes.c_ulong

        size = ctypes.c_ulong(0)
        if fn(AF_INET, GAA_FLAG_INCLUDE_GATEWAYS, None, None,
              ctypes.byref(size)) != ERROR_BUFFER_OVERFLOW or size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if fn(AF_INET, GAA_FLAG_INCLUDE_GATEWAYS, None,
              ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(size)) != 0:
            return []

        # 出口 IP 所在网卡即具备默认路由的网卡，用它判定 has_gw。
        # 不去遍历 FirstGatewayAddress 链表：那要解引用系统返回的节点，
        # 实测在本环境会触发访问违规（0xC0000005）让进程直接崩溃。
        primary = _outbound_ip() or ""
        res = []
        p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
        guard = 0
        while p and guard < 512:
            guard += 1
            a = p.contents
            alias = a.FriendlyName or ""
            uc = a.FirstUnicastAddress
            uguard = 0
            while uc and uguard < 512:
                uguard += 1
                u = ctypes.cast(uc, ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)).contents
                sa = u.Address.lpSockaddr
                if sa:
                    family = ctypes.cast(sa, ctypes.POINTER(ctypes.c_ushort)).contents.value
                    if family == AF_INET:
                        octets = ctypes.string_at(sa + 4, 4)
                        ip = ".".join(str(b) for b in octets)
                        # 只读取指针是否为 NULL，绝不解引用它（解引用实测 0xC0000005 崩溃）。
                        # 存在少量假阳性（链路本地 / 虚拟网卡也报有网关），但它们都会被
                        # lan_ips 的过滤规则剔除，不影响最终返回给用户的地址列表。
                        has_gw = bool(a.FirstGatewayAddress) or (bool(primary) and ip == primary)
                        res.append((ip, alias, has_gw))
                uc = u.Next
            nxt = a.Next
            p = ctypes.cast(nxt, ctypes.POINTER(IP_ADAPTER_ADDRESSES)) if nxt else None
        return res
    except Exception:
        return []


def _enum_ip_config():
    """枚举 IPv4 单播地址 + 网卡别名 + 是否有默认网关。

    只使用 Win32 GetAdaptersAddresses，全程在进程内完成，不启动任何子进程，
    因此绝不会弹出 PowerShell / 控制台窗口。
    （早期版本用 subprocess 调 powershell，GUI 程序里每打开一次连接窗口
    就会闪一个黑色 PowerShell 控制台窗口，故彻底移除该路径。）

    返回 [(ip, iface_alias, has_gateway), ...]；失败返回 []，
    此时由 lan_ips() 用主机名解析兜底。
    """
    return _enum_ip_config_native()


def lan_ips():
    """枚举真实局域网 IPv4 地址，过滤虚拟/回环/链路本地网卡。

    返回按'手机最可能连得上'排序的 IP 列表（第一个用于二维码）：
    优先 UDP 出口 IP，其次有默认网关的网卡，最后其它。
    """
    primary = _outbound_ip()
    raw = _enum_ip_config()
    if not raw:
        # 兜底：退回主机名解析（可能含虚拟网卡，但至少有结果）
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if ":" not in ip and not ip.startswith("127."):
                    raw.append((ip, "", False))
        except Exception:
            pass

    seen = set()
    cands = []
    for ip, alias, gw in raw:
        if ip in seen:
            continue
        seen.add(ip)
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if _is_benchmark_ip(ip):
            continue
        if _is_virtual_iface(alias):
            continue
        cands.append((ip, gw, ip == primary))

    if not cands:
        return ["<本机IP>"]
    # 排序：有默认网关的真实网卡优先；同档内 UDP 出口 IP（最可能连路由器）优先
    cands.sort(key=lambda c: (not c[1], not c[2]))
    return [c[0] for c in cands]


async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    setup_logging()
    logging.info("PhoneType 服务启动中…")
    start_http()
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        ips = lan_ips()
        logging.info("=" * 56)
        logging.info("PhoneType：手机当键盘输入到电脑  已启动（单向键入，无反向同步）")
        logging.info("手机浏览器打开（同一 WiFi）：")
        for ip in ips:
            logging.info(f"  http://{ip}:{HTTP_PORT}/")
        logging.info("先在电脑上点一下要输入的目标窗口，再在手机上打字/语音/用快捷键。")
        logging.info("=" * 56)
        stop = asyncio.Event()
        global _server_stop
        _server_stop = stop
        await stop.wait()


_server_stop = None


def shutdown():
    """请求停止服务（线程安全），供托盘等宿主调用。"""
    global _server_stop, main_loop
    if _server_stop is not None and main_loop is not None:
        try:
            main_loop.call_soon_threadsafe(_server_stop.set)
        except Exception:
            pass


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("已停止。")
