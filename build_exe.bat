@echo off
rem 把 tray_app.py 打包成独立 exe（内置 Python + 依赖，免安装运行，系统托盘版）
rem 需在已安装 Python 的开发机上运行一次，产物在 dist/PhoneType.exe
rem --windowed：以 GUI 子系统构建，不分配/显示任何控制台窗口
rem --icon：正式应用图标（托盘 + 资源管理器显示）
rem --add-data assets/app.ico;assets：把图标一起打包，运行时可加载
pyinstaller --onefile --name PhoneType --windowed --icon "assets/app.ico" ^
  --version-file "assets/version_info.txt" ^
  --add-data "www/index.html;www" --add-data "assets/app.ico;assets" ^
  --hidden-import websockets ^
  --hidden-import pystray --hidden-import six --hidden-import PIL --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw --hidden-import PIL.ImageFont --hidden-import PIL.ImageTk ^
  --hidden-import qrcode --hidden-import tkinter ^
  tray_app.py
