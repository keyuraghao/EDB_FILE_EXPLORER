"""Exception hierarchy for EDB Explorer."""

from __future__ import annotations


class EdbExplorerError(Exception):
    """Base class for all EDB Explorer errors."""


class InvalidDatabaseError(EdbExplorerError):
    """The file is not a readable ESE database."""


class DatabaseNotFoundError(EdbExplorerError):
    """No open database matches the given identifier."""


class TableNotFoundError(EdbExplorerError):
    """The requested table does not exist in the database."""


class PathNotAllowedError(EdbExplorerError):
    """The path is outside the configured allow-list."""


class ExportError(EdbExplorerError):
    """Exporting records failed."""
