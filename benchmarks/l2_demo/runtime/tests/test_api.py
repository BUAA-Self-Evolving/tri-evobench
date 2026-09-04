import os
import unittest
from unittest.mock import patch

from l2_runtime.api import (
    OpenAIChatModel,
    _is_retryable_api_error,
    normalize_base_url,
)


class FakeStatusError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class NormalizeBaseUrlTests(unittest.TestCase):
    def test_adds_v1_to_origin(self):
        self.assertEqual(
            normalize_base_url("https://dragtokens.com/"),
            "https://dragtokens.com/v1",
        )

    def test_preserves_explicit_api_root(self):
        self.assertEqual(
            normalize_base_url("https://example.test/custom/v1"),
            "https://example.test/custom/v1",
        )

    def test_rejects_query_or_fragment(self):
        with self.assertRaises(ValueError):
            normalize_base_url("https://example.test/v1?token=secret")

    def test_api_key_is_required_but_not_repr_visible(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "L2_API_KEY"):
                OpenAIChatModel.from_environment(
                    base_url="https://example.test/v1",
                    model="gpt-5.6-terra",
                )

    def test_only_provider_upstream_400_is_retryable(self):
        self.assertTrue(
            _is_retryable_api_error(FakeStatusError(400, "Upstream request failed"))
        )
        self.assertFalse(
            _is_retryable_api_error(FakeStatusError(400, "invalid reasoning effort"))
        )

    def test_standard_transient_statuses_are_retryable(self):
        for status_code in (408, 409, 429, 500, 503):
            with self.subTest(status_code=status_code):
                self.assertTrue(
                    _is_retryable_api_error(FakeStatusError(status_code, "temporary"))
                )
