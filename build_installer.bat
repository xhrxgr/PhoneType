@echo off
rem 打包安装器 PhoneTypeSetup.exe
rem 前置：先运行 build_exe.bat 生成 dist\PhoneType.exe
rem 安装器会把 dist\PhoneType.exe 一起打进安装包，产出自包含的 PhoneTypeSetup.exe
if not exist "dist\PhoneType.exe" (
  echo 找不到 dist\PhoneType.exe，请先运行 build_exe.bat
  exit /b 1
)
pyinstaller --onefile --name PhoneTypeSetup --windowed --icon "assets\app.ico" ^
  --add-data "dist\PhoneType.exe;." --add-data "assets\app.ico;." ^
  installer.py
