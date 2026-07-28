import logging
import sys

def configure_logging():
    """Configure logging for Relium CLI.

    Sets up a basic logger that writes to stderr.
    Does NOT log sensitive information like API keys, SQL, or raw data.
    Call once at CLI startup.
    """
    root = logging.getLogger()
    if root.hasHandlers():
        return

    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)

    formatter = logging.Formatter('%(levelname)s: %(name)s: %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_logger(name):
    """Get a logger for a module. Call as: logger = get_logger(__name__)"""
    return logging.getLogger(name)
