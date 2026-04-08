"""Resolve Key Vault references and emit ``export`` statements."""

from __future__ import annotations

import os
import shlex


def main() -> None:
    os.environ.setdefault("AZURE_IDENTITY_DISABLE_IMDS", "")
    os.environ.setdefault("IDENTITY_ENDPOINT", "")

    from .services.keyvault import is_kv_ref, kv

    if not kv.enabled:
        return

    for key, value in os.environ.items():
        if not is_kv_ref(value):
            continue
        try:
            result = kv.resolve({key: value})
            for rk, rv in result.items():
                print(f"export {rk}={shlex.quote(rv)}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
