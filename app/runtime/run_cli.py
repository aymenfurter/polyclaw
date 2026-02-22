"""Container-compatible entry point for ``polyclaw-run``.

The real implementation lives in :mod:`app.cli.run`.  This shim exists
so that the ``polyclaw-run`` console-script entry point
(``polyclaw.run_cli:main``) works both in local development (where the
package is ``app.runtime``) and inside the container (where
``app/runtime/`` is copied to ``polyclaw/``).
"""

from __future__ import annotations

from app.cli.run import main

__all__ = ["main"]
