"""Credential-free runtime profiles for supported OpenAI-compatible providers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from typing import MutableMapping


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str
    model: str
    credential_env: str
    thinking: str
    timeout_seconds: int
    max_retries: int
    inherit_proxy: bool
    default_rpm: int | None = None

    def public_metadata(self) -> dict[str, object]:
        """Return a serializable description which can safely be committed."""
        return asdict(self)


@dataclass(frozen=True)
class ProviderCredential:
    """One in-memory credential and its non-secret, stable slot label."""

    slot: str
    value: str


PROVIDER_PROFILES = {
    "packycode": ProviderProfile(
        name="packycode",
        base_url="https://cf.api.fan/v1",
        model="deepseek-v4-flash",
        credential_env="PACKYCODE_API_KEY",
        thinking="disabled",
        timeout_seconds=150,
        max_retries=4,
        inherit_proxy=True,
    ),
    "friday": ProviderProfile(
        name="friday",
        base_url="https://aigc.sankuai.com/v1/openai/native",
        model="deepseek-v4-flash",
        credential_env="FRIDAY_APP_ID",
        thinking="enabled",
        timeout_seconds=300,
        max_retries=5,
        inherit_proxy=False,
        default_rpm=1,
    ),
}

_KEY_ALIASES = (
    "TRIEVO_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "TARGET_OPENAI_COMPATIBLE_API_KEY",
    "DEEPSEEK_API_KEY",
)
_BASE_URL_ALIASES = (
    "TRIEVO_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "TARGET_OPENAI_COMPATIBLE_BASE_URL",
    "DEEPSEEK_BASE_URL",
)
_PROXY_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
_FRIDAY_SLOT_PATTERN = re.compile(r"^FRIDAY_APP_ID_([1-9][0-9]*)$")


FRIDAY_MODELS = ("deepseek-v4-flash", "deepseek-v4-flash-meituan", "glm-53-meituan")


def get_provider_profile(name: str, *, model: str | None = None) -> ProviderProfile:
    try:
        profile = PROVIDER_PROFILES[name]
        if model is not None:
            if name == "friday" and model not in FRIDAY_MODELS:
                raise ValueError("unsupported Friday request model")
            profile = replace(profile, model=model)
        return profile
    except KeyError as exc:
        choices = ", ".join(sorted(PROVIDER_PROFILES))
        raise ValueError(f"unknown provider mode {name!r}; choose one of: {choices}") from exc


def _friday_list_credentials(raw: str) -> list[str]:
    """Parse a JSON array or newline-delimited credential list without logging it."""
    value = raw.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("FRIDAY_APP_IDS must be a JSON array or newline-delimited list") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("FRIDAY_APP_IDS JSON value must be an array of strings")
        return [item.strip() for item in parsed]
    return [item.strip() for item in value.splitlines()]


def provider_credentials(
    name: str,
    environ: MutableMapping[str, str],
) -> tuple[ProviderCredential, ...]:
    """Resolve a provider's credential pool without exposing values in metadata."""
    profile = get_provider_profile(name)
    if name != "friday":
        value = environ.get(profile.credential_env, "")
        if not value and name == "packycode":
            value = environ.get("TRIEVO_API_KEY", "")
        if not value:
            raise ValueError(f"{profile.credential_env} is required for provider mode {name}")
        return (ProviderCredential(profile.credential_env, value),)

    indexed = []
    for key, value in environ.items():
        match = _FRIDAY_SLOT_PATTERN.match(key)
        if match:
            if not value.strip():
                raise ValueError(f"{key} is empty")
            indexed.append((int(match.group(1)), key, value.strip()))
    indexed.sort()
    if indexed:
        credentials = [ProviderCredential(key, value) for _, key, value in indexed]
    elif environ.get("FRIDAY_APP_IDS", "").strip():
        values = _friday_list_credentials(environ["FRIDAY_APP_IDS"])
        if not values or any(not value for value in values):
            raise ValueError("FRIDAY_APP_IDS contains an empty credential")
        credentials = [
            ProviderCredential(f"FRIDAY_APP_IDS[{index}]", value)
            for index, value in enumerate(values, 1)
        ]
    else:
        value = environ.get("FRIDAY_APP_ID", "").strip()
        credentials = [ProviderCredential("FRIDAY_APP_ID", value)] if value else []

    if not credentials:
        raise ValueError(
            "FRIDAY_APP_ID, FRIDAY_APP_IDS, or FRIDAY_APP_ID_<n> is required "
            "for provider mode friday"
        )
    values = [credential.value for credential in credentials]
    if len(values) != len(set(values)):
        raise ValueError("Friday credential pool contains duplicate App IDs")
    return tuple(credentials)


def configure_provider_environment(
    name: str,
    environ: MutableMapping[str, str],
    *,
    credential: ProviderCredential | None = None,
) -> ProviderProfile:
    """Select one provider and bridge its in-memory secret to legacy adapters."""
    profile = get_provider_profile(name, model=environ.get("TRIEVO_PROVIDER_MODEL") if name == "friday" else None)
    selected = credential or provider_credentials(name, environ)[0]
    effective_base_url = profile.base_url
    if name == "friday" and environ.get("TRIEVO_FRIDAY_GATEWAY_URL"):
        from .friday_gateway import validate_host_gateway_url
        effective_base_url = validate_host_gateway_url(environ["TRIEVO_FRIDAY_GATEWAY_URL"])

    if name == "friday":
        # Do not expose the whole pool to a worker. It receives only its
        # selected App ID plus aliases required by baseline adapters.
        environ.pop("FRIDAY_APP_IDS", None)
        for key in list(environ):
            if _FRIDAY_SLOT_PATTERN.match(key):
                environ.pop(key, None)
        environ["FRIDAY_APP_ID"] = selected.value

    for key in _KEY_ALIASES:
        environ[key] = selected.value
    for key in _BASE_URL_ALIASES:
        environ[key] = effective_base_url
    environ["TRIEVO_MODEL"] = profile.model
    environ["OPENAI_COMPATIBLE_MODEL"] = profile.model
    environ["TARGET_OPENAI_COMPATIBLE_MODEL"] = profile.model
    environ["TRIEVO_THINKING"] = profile.thinking
    environ["TRIEVO_API_TIMEOUT_SECONDS"] = str(profile.timeout_seconds)
    environ["TRIEVO_API_MAX_RETRIES"] = str(profile.max_retries)
    environ["TRIEVO_PROVIDER_MODE"] = profile.name
    if profile.inherit_proxy:
        environ.pop("TRIEVO_DIRECT_API", None)
    else:
        environ["TRIEVO_DIRECT_API"] = "1"
        for key in _PROXY_ENV_NAMES:
            environ.pop(key, None)
    return profile


def validate_generated_profile(config_dir: Path, selected_mode: str) -> None:
    """Reject accidentally mixing Friday credentials with PackyCode configs."""
    path = config_dir / "provider-profile.json"
    if not path.is_file():
        if selected_mode == "friday":
            raise ValueError(
                "Friday mode requires configs generated with "
                "prepare_matrix_configs.py --provider-mode friday"
            )
        return
    try:
        configured_mode = json.loads(path.read_text(encoding="utf-8")).get("name")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ValueError(f"invalid provider profile: {path}") from exc
    if configured_mode != selected_mode:
        raise ValueError(
            f"provider mode mismatch: runtime selected {selected_mode!r}, "
            f"but {path} contains {configured_mode!r}"
        )
