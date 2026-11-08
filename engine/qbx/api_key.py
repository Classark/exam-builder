"""The Anthropic API key: kept in the OS credential store, never in a file.

Windows Credential Manager, the macOS Keychain, or whatever `keyring` finds elsewhere. The
key never goes into config/settings.json and never back to the UI - the UI only learns that
one is saved and its last four characters, enough to tell two keys apart.

`ANTHROPIC_API_KEY` in the environment overrides the saved key (a developer's shell, a
test run). A machine with no working credential store (a bare Linux box with no Secret
Service) reports that plainly rather than falling back to a file.

The later "Classark account" provider - the user's own server proxying the API for
teachers on a subscription - will keep its account token here too, under its own name
(`get_secret("classark_token")`); nothing else needs to change.
"""

import os

SERVICE = "ClassarkExamBuilder"
API_KEY = "anthropic_api_key"
ENV_VAR = "ANTHROPIC_API_KEY"

# tests set this to an in-memory backend; None means the platform's own
_backend = None


class KeyStoreError(Exception):
    """The OS credential store couldn't be used."""


def set_backend(backend) -> None:
    global _backend
    _backend = backend


def _store():
    if _backend is not None:
        return _backend
    import keyring

    return keyring.get_keyring()


def get_secret(name: str) -> str | None:
    try:
        return _store().get_password(SERVICE, name) or None
    except Exception:  # no backend, locked keychain: treat as not saved
        return None


def set_secret(name: str, value: str) -> None:
    try:
        _store().set_password(SERVICE, name, value)
    except Exception as e:
        raise KeyStoreError(f"Couldn't save to this computer's credential store: {e}") from e


def delete_secret(name: str) -> None:
    try:
        _store().delete_password(SERVICE, name)
    except Exception:
        pass  # nothing saved is the state asked for


def get_api_key() -> str | None:
    return os.environ.get(ENV_VAR) or get_secret(API_KEY)


def set_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("The API key is empty.")
    set_secret(API_KEY, key)


def clear_api_key() -> None:
    delete_secret(API_KEY)


def api_key_status() -> dict:
    """What the UI may know about the key: whether one is set, where from, and its last
    four characters. Never the key."""
    from_env = bool(os.environ.get(ENV_VAR))
    key = get_api_key()
    return {
        "set": bool(key),
        "source": "environment" if from_env else ("saved" if key else None),
        "last4": key[-4:] if key and len(key) >= 8 else None,
    }


def backend_name() -> str:
    """Which credential store is in use - for checking a frozen build found one."""
    try:
        store = _store()
    except Exception as e:
        return f"none ({e})"
    return f"{type(store).__module__}.{type(store).__name__}"
