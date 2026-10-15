"""Pinned SDK check extracted from the donor's reviewed SDK boundary."""
from importlib.metadata import version, PackageNotFoundError


def _assert_pinned_sdk():
    try:
        installed = version('kagglesdk')
    except PackageNotFoundError:
        installed = None
    if installed != '0.1.37':
        raise RuntimeError('Install kaggle mcp/requirements.txt (kagglesdk==0.1.37) before a Kaggle attempt')
