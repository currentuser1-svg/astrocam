@echo off
rem Rebuild camctl.exe with the C# compiler that ships with Windows (.NET Framework 4)
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /optimize /out:"%~dp0..\camctl.exe" "%~dp0camctl.cs"
