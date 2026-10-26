@echo off
setlocal
cd /d "%~dp0"
uv run pyinstaller --noconfirm --onefile --name dashcam-restore ^
    --paths src ^
    --distpath dist --workpath build --specpath build ^
    src\dashcam_restore\__main__.py
if errorlevel 1 (
    echo PyInstaller failed
    exit /b 1
)
echo Built dist\dashcam-restore.exe
uv run pyinstaller --noconfirm --onefile --windowed --name dashcam-restore-gui ^
    --paths src ^
    --distpath dist --workpath build --specpath build ^
    src\dashcam_restore\gui_entry.py
if errorlevel 1 (
    echo PyInstaller failed
    exit /b 1
)
echo Built dist\dashcam-restore-gui.exe
