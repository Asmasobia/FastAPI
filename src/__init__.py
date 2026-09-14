# This empty file makes "src" a Python PACKAGE.
#
# WHY: Python treats a directory as an importable package if it contains an
# __init__.py (or, since 3.3, even without one via "namespace packages" — but an
# explicit __init__.py is clearer and avoids subtle import ambiguities). Because
# this exists, other modules can write:  from src.config import get_settings
# and Python knows where to look. Every subfolder below (routers/, models/, ...)
# has one for the same reason.
