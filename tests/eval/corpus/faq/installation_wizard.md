# Installation Troubleshoot Guide

## Installation Wizard Problems

The installation wizard guides you through setup in a few simple steps. If the wizard fails or crashes, follow these troubleshoot steps.

### Wizard Hangs on Startup

If the installation wizard freezes during startup, close it completely and reinstall. Download a fresh copy from the official website and run the installer as administrator.

### Installation Fails Midway

If installation fails partway through, uninstall any partial installation first. Use the system uninstaller or the provided uninstall utility. Then reinstall from scratch.

### Workarounds for Common Installation Errors

**Error 1603** (fatal installation failure): Disable antivirus software temporarily during installation. Re-enable it after installation completes.

**Error 1935** (assembly component issue): Install or repair the Microsoft Visual C++ redistributable from the official Microsoft website before reinstalling.

**Error 2503** or **2502** (permission issues): Run the installer with administrator privileges. Right-click the installer and select "Run as administrator".

### Silent Installation

For enterprise deployments, use the silent installation flag: `installer.exe /quiet /norestart`. Check the installation log at `%TEMP%\install.log` for error messages.

### Post-Installation Startup Crash

If the application crashes on first startup after installation, reinstall with the "repair" option. If the crash persists, check the crash log under `%AppData%\Logs\` for a symptom description.

### Uninstall and Clean Reinstall

For a clean reinstall: uninstall via Control Panel, delete the remaining application folder from Program Files, remove registry keys under `HKLM\Software\AppName`, then reinstall.
