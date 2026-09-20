# Test fixtures

Unit tests run against an in-memory fake ESE backend (see `tests/conftest.py`) so
no database files are committed to the repository.

Integration tests (`tests/test_integration.py`) exercise the real parser. They are
skipped unless `EDB_EXPLORER_TEST_DB` points at an ESE database on your machine,
for example:

```bash
EDB_EXPLORER_TEST_DB=/evidence/SRUDB.dat pytest -m integration
```

Any ESE file works (SRUDB.dat, ntds.dit, WebCacheV01.dat, an Exchange .edb ...).
Never commit case evidence to this repository.
