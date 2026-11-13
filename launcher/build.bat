@echo off
chcp 65001 >nul
cd /d "%~dp0"
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /codepage:65001 /target:winexe /optimize+ /win32icon:app.ico /r:System.Windows.Forms.dll /out:"..\AI早知道.exe" launcher.cs
if errorlevel 1 (pause & exit /b 1)
echo built ..\AI早知道.exe
