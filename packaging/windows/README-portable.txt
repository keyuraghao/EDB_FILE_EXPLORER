EDB Explorer - portable build for Windows
=========================================

* Unzip anywhere (a USB stick, a case folder, ...) and run EDB-Explorer.exe.
* Nothing is written to the registry or to your user profile: settings, the project signing key,
  trusted signers and the disk cache for large tables all live in the "data" folder next to the
  executable (the "portable.txt" marker is what tells the application to behave this way).
* Move or copy the folder and your settings travel with it.
* Uninstall: run Uninstall.cmd (removes the data folder and, if you want, the whole application
  folder) - or simply delete the folder.
* Prefer Start Menu shortcuts, file associations and the command-line tool on PATH? Use the
  EDB-Explorer-<version>-setup.exe installer instead; it can be removed from "Apps & features".
