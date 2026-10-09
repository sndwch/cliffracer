"""Where the package's log files go, decided once for every entry point."""

import os
from pathlib import Path

DEFAULT_LOG_DIR = "./logs"


def resolve_log_dir(log_dir: str | None) -> Path:
    """The directory log files are written to: the argument, else CLIFFRACER_LOG_DIR, else ./logs.

    CLIFFRACER_ prefixed, like every other name this library reads, so it cannot collide with an
    application's own LOG_DIR. Both `LoggingConfig.configure` and `setup_correlation_logging`
    read it here, so the one variable moves both. A variable that is set and empty is not set: a
    container that exports `CLIFFRACER_LOG_DIR=` gets `./logs`, not the working directory.
    """
    if log_dir is None:
        log_dir = os.getenv("CLIFFRACER_LOG_DIR") or DEFAULT_LOG_DIR
    return Path(log_dir)


def log_file_stem(service_name: str) -> str:
    """The service name as the first part of a file name, with no path in it.

    A service name is a label, and `ServiceConfig` accepts a `/` in one. Joined onto the log
    directory as it stood, `/srv/x/evil` wrote `/srv/x/evil.log` outside the directory and `a/b`
    wrote `logs/a/b.log` below it. Every path separator becomes an underscore, so the files land
    in the directory whatever the name is; a name without one is unchanged.
    """
    for separator in {"/", "\\", os.sep}:
        service_name = service_name.replace(separator, "_")
    return service_name
