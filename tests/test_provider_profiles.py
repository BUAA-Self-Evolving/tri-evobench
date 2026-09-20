from __future__ import annotations

import pytest

from trievo_bench.provider_profiles import (
    configure_provider_environment,
    get_provider_profile,
    provider_credentials,
    validate_generated_profile,
)


def test_friday_environment_uses_app_id_and_removes_proxy() -> None:
    environment = {
        "FRIDAY_APP_ID": "test-app-id",
        "TRIEVO_API_KEY": "stale-relay-key",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "no_proxy": "localhost",
    }

    profile = configure_provider_environment("friday", environment)

    assert profile.default_rpm == 1
    assert environment["TRIEVO_API_KEY"] == "test-app-id"
    assert environment["OPENAI_API_KEY"] == "test-app-id"
    assert environment["TRIEVO_BASE_URL"] == "https://aigc.sankuai.com/v1/openai/native"
    assert environment["TRIEVO_MODEL"] == "deepseek-v4-flash"
    assert environment["TRIEVO_THINKING"] == "enabled"
    assert environment["TRIEVO_DIRECT_API"] == "1"
    assert "HTTPS_PROXY" not in environment
    assert "no_proxy" not in environment


def test_packycode_keeps_legacy_key_and_proxy_compatibility() -> None:
    environment = {"TRIEVO_API_KEY": "legacy-key", "HTTPS_PROXY": "http://proxy.test"}

    profile = configure_provider_environment("packycode", environment)

    assert profile.credential_env == "PACKYCODE_API_KEY"
    assert environment["OPENAI_API_KEY"] == "legacy-key"
    assert environment["TRIEVO_BASE_URL"] == "https://cf.api.fan/v1"
    assert environment["TRIEVO_THINKING"] == "disabled"
    assert environment["HTTPS_PROXY"] == "http://proxy.test"


def test_missing_selected_provider_credential_is_explicit() -> None:
    with pytest.raises(ValueError, match="FRIDAY_APP_ID"):
        configure_provider_environment("friday", {})


def test_friday_indexed_pool_is_numeric_stable_and_worker_gets_one_secret() -> None:
    environment = {
        "FRIDAY_APP_ID_10": "app-ten",
        "FRIDAY_APP_ID_2": "app-two",
        "FRIDAY_APP_ID_1": "app-one",
        "FRIDAY_APP_IDS": '["ignored-when-indexed"]',
    }

    credentials = provider_credentials("friday", environment)

    assert [item.slot for item in credentials] == [
        "FRIDAY_APP_ID_1", "FRIDAY_APP_ID_2", "FRIDAY_APP_ID_10",
    ]
    worker = dict(environment)
    configure_provider_environment("friday", worker, credential=credentials[1])
    assert worker["FRIDAY_APP_ID"] == "app-two"
    assert worker["TRIEVO_API_KEY"] == "app-two"
    assert "FRIDAY_APP_IDS" not in worker
    assert not any(key.startswith("FRIDAY_APP_ID_") for key in worker)


@pytest.mark.parametrize(
    "raw",
    ['["app-one", "app-two"]', "app-one\napp-two"],
)
def test_friday_list_pool_supports_json_or_newlines(raw) -> None:
    credentials = provider_credentials("friday", {"FRIDAY_APP_IDS": raw})
    assert [item.slot for item in credentials] == [
        "FRIDAY_APP_IDS[1]", "FRIDAY_APP_IDS[2]",
    ]
    assert [item.value for item in credentials] == ["app-one", "app-two"]


def test_friday_pool_rejects_duplicate_or_empty_credentials() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        provider_credentials(
            "friday",
            {"FRIDAY_APP_ID_1": "same", "FRIDAY_APP_ID_2": "same"},
        )
    with pytest.raises(ValueError, match="empty"):
        provider_credentials("friday", {"FRIDAY_APP_ID_1": ""})


def test_public_profile_contains_no_credential_value() -> None:
    metadata = get_provider_profile("friday").public_metadata()
    assert metadata["credential_env"] == "FRIDAY_APP_ID"
    assert "api_key" not in metadata
    assert "app_id" not in metadata


def test_friday_rejects_missing_or_mismatched_generated_profile(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires configs generated"):
        validate_generated_profile(tmp_path, "friday")
    (tmp_path / "provider-profile.json").write_text('{"name":"packycode"}')
    with pytest.raises(ValueError, match="provider mode mismatch"):
        validate_generated_profile(tmp_path, "friday")
