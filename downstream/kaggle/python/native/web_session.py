"""Compatibility entrypoint; implementation lives in auto_login.web_session."""
import sys
import auto_login.web_session as _implementation
if __name__ == "__main__":
    _implementation._cli()
else:
    sys.modules[__name__] = _implementation
