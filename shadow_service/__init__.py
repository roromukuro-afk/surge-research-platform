"""The Phase B web shadow's Python service on Vercel (D-279).

The shadow runs the frozen Phase B code from where it lives in the repository
(``workers/src``): that code finds Canonical v5.1, the addenda and its own frozen
files by their place in the checkout, and the cohort's protocol fingerprint is
computed from exactly those files. So this service is rooted at the repository,
and puts ``workers/src`` on the import path rather than installing a copy.

Importing this package does nothing: the workflow SDK replays workflow bodies
in a deterministic sandbox that refuses filesystem calls, and it imports this
package on the way to ``flows``. Callers outside the sandbox (the ASGI app, the
steps) call ``use_workers_src()`` themselves.
"""

from __future__ import annotations


def repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def use_workers_src():
    """Put ``workers/src`` first on the import path, once; returns the repository root."""

    import sys

    root = repo_root()
    src = str(root / "workers" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return root
