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

    refs: dict[str, str] = {}
    for key, value in os.environ.items():
        if is_kv_ref(value):
            refs[key] = value

    if not refs:
        return

    resolved: dict[str, str] = {}
    failed: list[str] = []
    for key, value in refs.items():
        try:
            result = kv.resolve({key: value})
            resolved.update(result)
        except Exception:
            failed.append(key)

    for key, value in resolved.items():
        print(f"export {key}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
