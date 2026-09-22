EDB Explorer - portable build for Linux
=======================================

* Extract anywhere and run ./EDB-Explorer (GUI) or ./edb-explorer (CLI / MCP server).
* The "portable.txt" marker makes the application keep settings, the project signing key, trusted
  signers and the disk cache for large tables in the "data" folder next to the executables - nothing
  is written to ~/.config, ~/.cache or /tmp.
* Uninstall: delete the folder.
* Want a menu entry and "edb-explorer" on PATH instead? Use the regular tarball (./install.sh,
  ./uninstall.sh) or the .deb package (sudo apt install ./edb-explorer_<version>_amd64.deb,
  sudo apt remove edb-explorer).
