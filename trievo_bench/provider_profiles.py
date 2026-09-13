"""Credential-free runtime profiles for supported OpenAI-compatible providers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
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
        default_rpm=20,
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


def get_provider_profile(name: str) -> ProviderProfile:
    try:
        return PROVIDER_PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(PROVIDER_PROFILES))
        raise ValueError(f"unknown provider mode {name!r}; choose one of: {choices}") from exc


def configure_provider_environment(
    name: str,
    environ: MutableMapping[str, str],
) -> ProviderProfile:
    """Select one provider and bridge its in-memory secret to legacy adapters."""
    profile = get_provider_profile(name)
    credential = environ.get(profile.credential_env, "")
    # TRIEVO_API_KEY remains the documented legacy PackyCode variable.
    if not credential and name == "packycode":
        credential = environ.get("TRIEVO_API_KEY", "")
    if not credential:
        raise ValueError(f"{profile.credential_env} is required for provider mode {name}")

    for key in _KEY_ALIASES:
        environ[key] = credential
    for key in _BASE_URL_ALIASES:
        environ[key] = profile.base_url
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
