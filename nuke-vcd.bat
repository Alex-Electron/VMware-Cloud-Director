@echo off
REM nuke-vcd.bat - Windows wrapper for nuke_vcd_tenant.py

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0nuke-vcd.ps1" %*
