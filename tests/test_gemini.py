"""SDK wire-format tests use httpx.MockTransport: no credentials or real network."""

import json
import os
import tempfile
from pathlib import Path
from dataclasses import replace
import unittest
from unittest.mock import patch

from pdf_translator.translation.gemini import GeminiProvider, validate_model
from pdf_translator.translation.models import ProviderError, QuotaError, TranslationError, TranslationOptions
from pdf_translator.translation.ports import TranslationItem, TranslationRequest

try:
    import httpx
    from google import genai
    from google.genai import types
except ImportError:
    genai = None


@unittest.skipUnless(genai, "Install translation/dev extra for SDK tests")
class GeminiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.options = TranslationOptions(model="gemini-test")
        self.calls = []
        self.body = {"candidates": [{"content": {"parts": [{"text": '{"translations":[]}'}]}, "finishReason": "STOP"}]}
        self.status = 200
        self.response_headers = {}
        self.model_pages = None
        self.reply_sequence = None
        def respond(request):
            self.calls.append(request)
            if request.method == "GET" and self.model_pages is not None:
                return httpx.Response(200, json=self.model_pages.get(request.url.params.get("pageToken")))
            if request.method == "GET":
                return httpx.Response(200, json={"name": "models/gemini-test", "inputTokenLimit": 10000, "outputTokenLimit": 65536})
            if request.url.path.endswith(":countTokens"):
                return httpx.Response(200, json={"totalTokens": 100})
            body = self.reply_sequence.pop(0) if self.reply_sequence else self.body
            return httpx.Response(self.status, json=body, headers=self.response_headers)
        self.http = httpx.Client(transport=httpx.MockTransport(respond))
        self.client = genai.Client(api_key="test-key-not-real", vertexai=False,
                                  http_options=types.HttpOptions(httpx_client=self.http,
                                                                timeout=round(self.options.timeout_seconds * 1000),
                                                                retry_options=types.HttpRetryOptions(attempts=1)))
        self.addCleanup(self.client.close)
        self.addCleanup(self.http.close)
        factory = patch("google.genai.Client", return_value=self.client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        environment = patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-not-real", "GOOGLE_API_KEY": "wrong-key"})
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("socket.getaddrinfo", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Live network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.provider = GeminiProvider()
        self.session = self.provider.prepare(self.options)
        self.gateway = self.session.gateway

    def test_construction_does_not_send_and_locks_endpoint_key_and_retry_policy(self):
        self.assertFalse(self.calls)
        kwargs = self.factory.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "test-key-not-real")
        self.assertIs(kwargs["vertexai"], False)
        self.assertEqual(kwargs["http_options"].retry_options.attempts, 1)
        self.assertEqual(kwargs["http_options"].base_url, "https://generativelanguage.googleapis.com")
        self.assertEqual(kwargs["http_options"].timeout, 300000)

    def test_timeout_reaches_server_deadline_header_without_live_network(self):
        self.gateway.generate("gemini-test", "Synthetic offline test", 8192)
        self.assertEqual(self.calls[-1].headers["X-Server-Timeout"], "300")

    def test_content_free_usage_events_distinguish_missing_fields_from_zero(self):
        from pdf_translator.telemetry import log_run
        from types import SimpleNamespace
        events = []
        self.body["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 40,
                                      "thoughtsTokenCount": 0, "totalTokenCount": 140}
        with log_run(SimpleNamespace(write=events.append)):
            self.gateway.generate("gemini-test", "private source text", 8192)
        usage = next(e for e in events if e["event"] == "provider.usage")
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["thinking_tokens"], usage["total_tokens"]), (100, 40, 0, 140))
        self.assertNotIn("cached_tokens", usage)
        self.assertNotIn("private source", json.dumps(events))

    def test_timeout_transport_and_sdk_failures_have_safe_distinct_codes(self):
        from pdf_translator.telemetry import log_run, span
        from types import SimpleNamespace
        for error, code in ((httpx.ReadTimeout("private timeout URL"), "network_timeout"),
                            (httpx.ConnectError("private transport URL"), "network_transport"),
                            (RuntimeError("private SDK payload"), "sdk")):
            events = []
            with self.subTest(code=code), self.assertRaises(ProviderError), log_run(SimpleNamespace(write=events.append)):
                with span("api.request"):
                    self.gateway._call(lambda: (_ for _ in ()).throw(error))
            self.assertEqual(events[-2]["error_code"], code)
            self.assertNotIn("private", json.dumps(events))

    def test_model_policy_belongs_to_adapter_not_generic_options(self):
        for model in ("gemini-flash-latest", "models/gemini-x", "other-engine", "https://evil"):
            with self.subTest(model=model):
                options = TranslationOptions(model=model)
                with self.assertRaises(TranslationError):
                    validate_model(options.model)
        validate_model("gemini-test")

    def test_identity_and_construction_need_neither_sdk_nor_key(self):
        with patch.dict(os.environ, {}, clear=True), patch("google.genai.Client", side_effect=AssertionError("SDK constructed")):
            provider = GeminiProvider()
            self.assertEqual(provider.identity.name, "gemini")
            self.assertEqual(provider.identity, self.provider.identity)
            provider.close()

    def test_actual_sdk_metadata_token_count_and_json_generation(self):
        self.assertEqual(self.gateway.model_limits("gemini-test").input_tokens, 10000)
        self.assertEqual(self.gateway.count_tokens("gemini-test", "Untrusted text"), 100)
        self.assertEqual(self.gateway.generate("gemini-test", "Untrusted text", 8192).text, '{"translations":[]}')
        request = self.calls[-1]
        self.assertTrue(request.url.path.endswith("/gemini-test:generateContent"))
        data = json.loads(request.content)
        self.assertEqual(data["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(data["generationConfig"]["maxOutputTokens"], 8192)
        self.assertIn("responseJsonSchema", data["generationConfig"])
        self.assertFalse(data.get("tools"))

    def test_domain_port_hides_prompt_schema_and_three_budgeted_sdk_calls(self):
        self.body["candidates"][0]["content"]["parts"][0]["text"] = '{"translations":{"a":"Один"}}'
        reservations = []
        class Executor:
            def call(self, operation, *, tokens=0, label):
                reservations.append(tokens)
                return operation()
        request = TranslationRequest((TranslationItem("a", "One", "paragraph"),), "EN", "RU", {}, "Context")
        output = self.session.translate(request, executor=Executor())
        self.assertEqual(output.translations, {"a": "Один"})
        self.assertEqual(reservations, [0, 0, 100 + 512 + self.options.max_output_tokens])
        self.assertEqual(len(self.calls), 3)
        data = json.loads(self.calls[-1].content)
        self.assertEqual(data["generationConfig"]["maxOutputTokens"], 32768)
        prompt = data["contents"][0]["parts"][0]["text"]
        self.assertIn("untrusted DATA", prompt)
        self.assertIn('"context":"Context"', prompt)

    def test_generation_returns_call_local_model_attribution(self):
        self.body["modelVersion"] = "gemini-test-001"
        first = self.gateway.generate("gemini-test", "Synthetic", 512)
        self.body.pop("modelVersion")
        second = self.gateway.generate("gemini-other", "Synthetic", 512)
        self.assertEqual(first.model, "gemini-test-001")
        self.assertEqual(second.model, "gemini-other")
        self.assertEqual(first.text, second.text)

    def test_strict_response_retry_uses_same_model_ids_and_request_specific_schema(self):
        from pdf_translator.translation import translate_file
        def body(text):
            return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
        self.reply_sequence = [body('{"translations":{"wrong":"Неверный ID"}}'),
                               body('{"translations":{"u00000.s0000":"Один абзац."}}')]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "one.txt"
            source.write_text("One paragraph.", encoding="utf-8")
            result = translate_file(source, output_dir=Path(folder) / "out", provider=self.provider,
                                    options=replace(self.options, requests_per_minute=1000, tokens_per_minute=1000000),
                                    allow_network=True)
        self.assertEqual((result.status, result.requests_made), ("complete", 5))
        generations = [r for r in self.calls if r.url.path.endswith(":generateContent")]
        self.assertEqual(len(generations), 2)
        for request in generations:
            schema = json.loads(request.content)["generationConfig"]["responseJsonSchema"]
            self.assertEqual(schema["properties"]["translations"]["required"], ["u00000.s0000"])
            self.assertFalse(schema["properties"]["translations"]["additionalProperties"])
        self.assertEqual(generations[0].url.path, generations[1].url.path)
        self.assertIn("RETRY:", json.loads(generations[1].content)["contents"][0]["parts"][0]["text"])

    def test_legacy_prompt_schema_and_identity_remain_explicit_and_unchanged(self):
        from pdf_translator.translation.gemini_format import INSTRUCTION, RESPONSE_SCHEMA, provider_identity, make_prompt
        request = TranslationRequest((TranslationItem("a", "One", "paragraph"),), "EN", "RU", {}, "", "id_mismatch")
        legacy = GeminiProvider(response_contract="legacy")
        self.body["candidates"][0]["content"]["parts"][0]["text"] = '{"translations":[{"id":"a","text":"Один"}]}'
        executor = type("Executor", (), {"call": lambda self, operation, **kwargs: operation()})()
        result = legacy.prepare(self.options).translate(request, executor=executor)
        self.assertEqual(result.translations, {"a": "Один"})
        prompt = make_prompt(request, "legacy")
        self.assertTrue(prompt.startswith(INSTRUCTION))
        self.assertNotIn("RETRY:", prompt)
        self.assertEqual(legacy.identity, provider_identity("legacy"))
        self.assertNotEqual(legacy.identity, self.provider.identity)
        self.assertEqual(json.loads(self.calls[-1].content)["generationConfig"]["responseJsonSchema"], RESPONSE_SCHEMA)

    def test_unfinished_and_empty_answers_rejected(self):
        for value in ({}, {"candidates": []}, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]},
                      {"candidates": [{"finishReason": "SAFETY"}]}, {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}):
            with self.subTest(value=value), self.assertRaises(TranslationError):
                self.body = value
                self.gateway.generate("gemini-test", "Text", 512)

    def test_error_messages_are_sanitized_and_daily_quota_is_not_retried(self):
        secret = "PRIVATE-SOURCE-AND-KEY"
        for code in (400, 401, 403, 404, 429, 503):
            self.status = code
            self.body = {"error": {"code": code, "message": secret, "details": [{"quotaId": "GenerateRequestsPerDay"}]}}
            before = len(self.calls)
            with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                self.gateway.generate("gemini-test", secret, 512)
            self.assertNotIn(secret, str(caught.exception))
            self.assertEqual(len(self.calls), before + 1)
            self.assertEqual(caught.exception.retryable, code == 503)

    def test_minute_retry_delay(self):
        self.status = 429
        self.body = {"error": {"code": 429, "message": "too fast", "details": [
            {"quotaId": "GenerateRequestsPerMinute"}, {"retryDelay": "12s"}]}}
        with self.assertRaises(ProviderError) as caught:
            self.gateway.generate("gemini-test", "Text", 512)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 12)

    def test_missing_key_not_taken_from_other_environment_variable(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "wrong-key"}):
            with self.assertRaises(TranslationError):
                GeminiProvider().prepare(self.options)

    def test_retry_after_header_is_honored_and_long_wait_is_not_retried(self):
        self.status = 429
        self.body = {"error": {"code": 429, "message": "quota"}}
        self.response_headers = {"Retry-After": "120"}
        with self.assertRaises(ProviderError) as caught:
            self.gateway.generate("gemini-test", "Text", 512)
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertFalse(caught.exception.retryable)

    def test_transport_error_is_generic(self):
        with patch.object(self.client.models, "generate_content", side_effect=httpx.ReadTimeout("secret")):
            with self.assertRaises(ProviderError) as caught:
                self.gateway.generate("gemini-test", "Text", 512)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("secret", str(caught.exception))

    def test_model_catalog_uses_explicit_budgetable_pages(self):
        self.model_pages = {
            None: {"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"],
                                "inputTokenLimit": 10000, "outputTokenLimit": 8192}], "nextPageToken": "second"},
            "second": {"models": [{"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"],
                                   "inputTokenLimit": 10000, "outputTokenLimit": 8192}]},
        }
        first, token = self.gateway.list_models_page()
        self.assertEqual((len(self.calls), token), (1, "second"))
        self.assertEqual(first[0].supported_actions, ("generateContent",))
        second, token = self.gateway.list_models_page(token)
        self.assertEqual((len(self.calls), token), (2, None))
        self.assertIn("flash-lite", second[0].name)

    def test_structured_quota_classification_ignores_raw_messages(self):
        for quota, dimensions, value, kind, scope in (
            ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", {"model": "gemini-3.8-flash"}, "5", "minute", "model"),
            ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", {"model": "gemini-3.8-flash"}, "20", "daily", "model"),
            ("GenerateRequestsPerDayPerProject", {}, "20", "daily", "project"),
            ("GenerateRequestsPerDayPerProjectPerModel", {"model": "gemini-3.8-flash"}, "0", "zero", "model"),
        ):
            self.status = 429
            self.body = {"error": {"code": 429, "message": "SENSITIVE-CONTENT", "details": [{
                "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaId": quota, "quotaDimensions": dimensions, "quotaValue": value}]}]}}
            with self.subTest(quota=quota, value=value), self.assertRaises(QuotaError) as caught:
                self.gateway.generate("gemini-test", "Text", 512)
            self.assertEqual((caught.exception.kind, caught.exception.scope), (kind, scope))
            self.assertNotIn("SENSITIVE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
