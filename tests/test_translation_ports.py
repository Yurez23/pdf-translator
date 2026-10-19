"""DIP regressions: a non-LLM provider works without any Gemini/SDK imports."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from pdf_translator.translation import (BatchTooLarge, ProviderIdentity, TranslationError,
                                       plan_translation, translate_file)
from pdf_translator.translation.planning import make_request
from pdf_translator.translation.storage import load_checkpoint
from tests.test_translation import LocalCase, OPTIONS


class DictionaryProvider:
    """Independent port implementation: no inheritance, prompt or token methods."""

    def __init__(self, name="dictionary", revision="v1", *, batch_limit=100):
        self.identity = ProviderIdentity(name, revision)
        self.prepared = []
        self.requests = []
        self.batch_limit = batch_limit

    def prepare(self, options):
        self.prepared.append(options)
        return self

    def translate(self, request, *, executor):
        if len(request.items) > self.batch_limit:
            raise BatchTooLarge("Too many items")

        def one_external_attempt():
            self.requests.append(request)
            return {item.id: "Словарь: " + item.text for item in reversed(request.items)}

        return executor.call(one_external_attempt, label="dictionary translation")


class TranslationPortTests(LocalCase):
    def setUp(self):
        super().setUp()
        self.options = replace(OPTIONS, model="dictionary/english-russian-v1")

    def run_dictionary(self, provider, **kwargs):
        return translate_file(self.source, output_dir=self.out, options=self.options,
                              provider=provider, allow_network=True, **kwargs)

    def test_non_llm_provider_uses_same_core_and_exports_in_source_order(self):
        provider = DictionaryProvider()
        result = self.run_dictionary(provider)
        self.assertEqual((result.status, result.requests_made), ("complete", 1))
        text = Path(result.text_path).read_text(encoding="utf-8")
        self.assertLess(text.index("ordinary"), text.index("Another"))
        self.assertEqual(provider.prepared, [self.options])
        self.assertFalse(hasattr(provider, "generate"))
        request = provider.requests[0]
        self.assertEqual((request.source_language, request.target_language), ("EN", "RU"))
        self.assertEqual(request.items[0].kind, "paragraph")

    def test_port_receives_glossary_context_kinds_and_unchanged_markers(self):
        self.source = self.root / "book.md"
        self.source.write_text("# Title\n\nA paragraph with **20 points**.\n\nNeighbor text.", encoding="utf-8")
        provider = DictionaryProvider()
        result = self.run_dictionary(provider, glossary={"points": "очки"})
        self.assertEqual(provider.requests[0].glossary, {"points": "очки"})
        self.assertEqual(provider.requests[0].items[0].kind, "heading")
        self.assertIn("⟦P", provider.requests[0].items[1].text)
        self.assertIn("**20 points**", Path(result.markdown_path).read_text(encoding="utf-8"))
        plan = plan_translation(self.source, self.options, provider=provider.identity)
        request = make_request(plan, (plan.segments[1],), self.options)
        self.assertIn("Title", request.context)
        self.assertIn("Neighbor", request.context)

    def test_generic_batch_limit_split_budget_and_resume_without_retranslation(self):
        provider = DictionaryProvider(batch_limit=1)
        self.options = replace(self.options, max_requests=1)
        result = self.run_dictionary(provider)
        self.assertEqual((result.status, result.completed_segments, result.requests_made), ("paused", 1, 1))
        self.assertFalse(list(self.out.glob("*.md")))
        next_provider = DictionaryProvider(batch_limit=1)
        self.options = replace(self.options, max_requests=10)
        result = self.run_dictionary(next_provider, resume=True)
        self.assertEqual((result.status, result.requests_made), ("complete", 1))
        self.assertNotEqual(provider.requests[0].items[0].id, next_provider.requests[0].items[0].id)

    def test_provider_and_revision_are_cache_boundaries_before_preparation(self):
        self.options = replace(self.options, max_requests=1)
        first = DictionaryProvider(batch_limit=1)
        self.run_dictionary(first)
        for other in (DictionaryProvider("other"), DictionaryProvider(revision="v2")):
            with self.subTest(identity=other.identity), self.assertRaises(TranslationError):
                self.run_dictionary(other, resume=True)
            self.assertEqual(other.prepared, [])
            self.assertEqual(other.requests, [])
        first_plan = plan_translation(self.source, self.options, provider=first.identity)
        other_plan = plan_translation(self.source, self.options, provider=ProviderIdentity("other", "v1"))
        self.assertNotEqual(first_plan.fingerprint, other_plan.fingerprint)
        self.assertEqual(first_plan.manifest["provider"], {"name": "dictionary", "revision": "v1"})

    def test_core_validates_non_json_adapter_responses_atomically(self):
        for index, response in enumerate(({}, {"unknown": "text"}, [], {"u00000.s0000": ""})):
            self.out = self.root / f"bad-{index}"
            provider = DictionaryProvider()
            provider.translate = lambda request, *, executor: response
            with self.subTest(response=response):
                if isinstance(response, dict):
                    result = self.run_dictionary(provider)
                    self.assertEqual((result.status, result.completed_segments), ("incomplete", 0))
                else:
                    with self.assertRaises(TranslationError):
                        self.run_dictionary(provider)
            self.assertEqual(load_checkpoint(next(self.out.glob("*.json")))["translations"], {})

    def test_single_item_too_large_keeps_checkpoint_and_never_exports(self):
        with self.assertRaisesRegex(TranslationError, "Один сегмент"):
            self.run_dictionary(DictionaryProvider(batch_limit=0))
        self.assertFalse(list(self.out.glob("*.txt")))

    def test_completed_cache_requires_neither_prepare_nor_translate(self):
        self.run_dictionary(DictionaryProvider())
        provider = DictionaryProvider()
        provider.prepare = lambda options: self.fail("Cached export prepared adapter")
        provider.translate = lambda request, **kwargs: self.fail("Cached export called adapter")
        result = self.run_dictionary(provider, resume=True, overwrite=True)
        self.assertEqual(result.requests_made, 0)

    def test_old_checkpoint_version_is_not_silently_reused(self):
        self.run_dictionary(DictionaryProvider())
        checkpoint = next(self.out.glob("*.json"))
        state = load_checkpoint(checkpoint)
        state["version"] = 1
        checkpoint.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(TranslationError):
            self.run_dictionary(DictionaryProvider(), resume=True, overwrite=True)

    def test_ports_are_importable_and_core_runs_with_all_vendor_modules_blocked(self):
        script = '''
import builtins, sys
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('google', 'httpx') or name.split('.')[-1] in ('gemini', 'gemini_format'):
        raise AssertionError('Vendor import in core: ' + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from pdf_translator.translation import ProviderIdentity, TranslationOptions, translate_file
class Provider:
    identity = ProviderIdentity('offline', 'v1')
    def prepare(self, options): return self
    def translate(self, request, *, executor):
        return {item.id: 'Перевод: ' + item.text for item in request.items}
result = translate_file(sys.argv[1], output_dir=sys.argv[2],
    options=TranslationOptions(model='independent-engine'), provider=Provider(), allow_network=True)
assert result.status == 'complete' and result.requests_made == 0
assert not any(name.startswith('google') or '.gemini' in name for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.source), str(self.out)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provider_identity_and_token_reservations_are_validated(self):
        for name, revision in (("", "v1"), ("test", ""), ("a/b", "v1"), ("test", "secret key")):
            with self.subTest(name=name, revision=revision), self.assertRaises(TranslationError):
                ProviderIdentity(name, revision)
        class BadReservation(DictionaryProvider):
            def translate(self, request, *, executor):
                return executor.call(lambda: self.fail("Invalid reservation used"), tokens=-1, label="invalid")
        with self.assertRaises(TranslationError):
            self.run_dictionary(BadReservation())
