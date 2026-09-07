import json
import logging
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from src.ai.service import AIService, PROMPT_ANSWER, _MultiAIEndpoint, _compact_error
from main import _MaxLogLengthFilter


class FakeConfig:
    def __init__(self, values=None):
        self.values = {
            "ai_model": "自定义",
            "custom_ai_base_url": "https://example.test/v1",
            "custom_ai_api_key": "key",
            "custom_ai_model": "vision-model",
            "multi_ai_config_path": "model_visible.ini",
            "multi_ai_timeout": 20,
        }
        if values:
            self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class AIServiceTests(unittest.TestCase):
    @staticmethod
    def endpoint(name):
        return _MultiAIEndpoint(
            name=name,
            base_url=f"https://{name}.example/v1",
            api_key="key",
            model=name,
        )

    def make_multi_service(self):
        service = AIService(FakeConfig({"ai_model": "多AI作答"}))
        self.addCleanup(service.shutdown)
        return service

    def test_custom_provider_disables_sdk_retries_and_caps_answer_output(self):
        message = Mock(content='{"type":"single","answers":"A"}')
        response = Mock(choices=[Mock(message=message)])
        client = Mock()
        client.chat.completions.create.return_value = response

        with patch("src.ai.service.OpenAI", return_value=client) as openai:
            service = AIService(FakeConfig())
            try:
                result = service._ask_custom(
                    None,
                    "answer prompt",
                    test_image_base64="aW1hZ2U=",
                )
            finally:
                service.shutdown()

        self.assertEqual(result, '{"type":"single","answers":"A"}')
        self.assertEqual(openai.call_args.kwargs["max_retries"], 0)
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["max_tokens"],
            AIService.ANSWER_MAX_TOKENS,
        )
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["extra_body"],
            {"enable_thinking": False},
        )

    def test_prompt_allows_models_to_report_missing_vision(self):
        self.assertIn('{"type":"unknown"}', PROMPT_ANSWER)

    def test_multi_config_loads_complete_sections_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.ini"
            path.write_text(
                """
[first]
base_url = https://first.example/v1
key = first-key
model = first-model
description = first description

[incomplete]
base_url = https://broken.example/v1
model = broken-model

[second]
base_url = https://second.example/v1
key = second-key
model = second-model
""".strip(),
                encoding="utf-8",
            )
            service = AIService(
                FakeConfig(
                    {
                        "ai_model": "多AI作答",
                        "multi_ai_config_path": str(path),
                    }
                )
            )
            self.addCleanup(service.shutdown)

            endpoints = service._load_multi_ai_endpoints()

        self.assertEqual([endpoint.name for endpoint in endpoints], ["first", "second"])
        self.assertEqual(endpoints[0].description, "first description")

    def test_multi_endpoint_uses_image_mime_timeout_and_no_retries(self):
        service = self.make_multi_service()
        endpoint = self.endpoint("vision")
        message = Mock(content='{"type":"single","answers":"A"}')
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=message)]
        )

        with patch("src.ai.service.OpenAI", return_value=client) as openai:
            result = service._ask_multi_endpoint(
                endpoint,
                "aW1hZ2U=",
                "prompt",
                7.5,
                "image/jpeg",
            )

        self.assertEqual(result, '{"type":"single","answers":"A"}')
        self.assertEqual(openai.call_args.kwargs["max_retries"], 0)
        self.assertEqual(openai.call_args.kwargs["timeout"], 7.5)
        request = client.chat.completions.create.call_args.kwargs
        image_url = request["messages"][0]["content"][0]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        client.close.assert_called_once()

    def test_doubao_request_disables_thinking(self):
        service = AIService(
            FakeConfig(
                {
                    "ai_model": "豆包AI",
                    "doubao_api_key": "key",
                }
            )
        )
        self.addCleanup(service.shutdown)
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content="A"))]
        )

        with patch("src.ai.service.OpenAI", return_value=client):
            service._ask_doubao(None, None, "prompt", "aW1hZ2U=")

        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["extra_body"],
            {"enable_thinking": False},
        )

    def test_gemini_request_disables_thinking(self):
        service = AIService(
            FakeConfig(
                {
                    "ai_model": "Gemini AI",
                    "gemini_api_key": "key",
                }
            )
        )
        self.addCleanup(service.shutdown)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "A"}]}}]
        }

        with patch("src.ai.service.requests.post", return_value=response) as post:
            service._ask_gemini(None, "prompt", "aW1hZ2U=")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            0,
        )

    def test_multi_submit_uses_daemon_coordinator_instead_of_executor_queue(self):
        service = self.make_multi_service()
        returned = Future()

        with (
            patch.object(service, "_start_daemon_request", return_value=returned) as direct,
            patch.object(service._request_executor, "submit") as queued,
        ):
            result = service.submit_answer(image_b64="aW1hZ2U=")

        self.assertIs(result, returned)
        direct.assert_called_once_with(service.answer_from_image, "aW1hZ2U=")
        queued.assert_not_called()

    def test_multi_image_download_uses_part_of_total_deadline(self):
        service = self.make_multi_service()
        jpeg = b"\xff\xd8\xffimage"

        with (
            patch.object(service, "_multi_ai_timeout", return_value=20.0),
            patch.object(service, "_download_and_save", return_value=(jpeg, "image.jpg")) as download,
            patch.object(service, "_ask_multi", return_value="answer") as ask_multi,
            patch("src.ai.service.time.monotonic", side_effect=[100.0, 102.0]),
        ):
            result = service._get_multi_answer(
                image_url="https://image.example/question.jpg",
                cookies={"session": "cookie"},
            )

        self.assertEqual(result, "answer")
        download.assert_called_once_with(
            "https://image.example/question.jpg",
            {"session": "cookie"},
            timeout=10.0,
        )
        self.assertEqual(ask_multi.call_args.args[2], "image/jpeg")
        self.assertEqual(ask_multi.call_args.kwargs["max_wait"], 18.0)

    def test_multi_requests_start_all_models_without_queue(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint(str(index)) for index in range(6)]
        started = set()
        lock = threading.Lock()
        all_started = threading.Event()

        def answer(endpoint, image_b64, prompt, timeout, mime_type):
            with lock:
                started.add(endpoint.name)
                if len(started) == len(endpoints):
                    all_started.set()
            self.assertTrue(all_started.wait(0.5))
            return '{"type":"single","answers":"A"}'

        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(service, "_ask_multi_endpoint", side_effect=answer),
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")

        self.assertEqual(started, {endpoint.name for endpoint in endpoints})
        self.assertEqual(json.loads(result)["answers"], "A")

    def test_multi_majority_normalizes_answers_and_filters_errors(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint(name) for name in ("one", "two", "three", "four", "bad")]
        responses = {
            "one": '{"type":"single","answers":"A"}',
            "two": "答案为 A。",
            "three": '{"type":"multi","answers":["A"]}',
            "four": '{"type":"single","answers":"B"}',
            "bad": "Error code: 429 - insufficient balance",
        }

        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(
                service,
                "_ask_multi_endpoint",
                side_effect=lambda endpoint, *_: responses[endpoint.name],
            ),
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")

        self.assertEqual(json.loads(result), {"type": "single", "answers": "A"})

    def test_multi_unknown_vote_is_discarded_even_if_it_contains_an_answer(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint("blind"), self.endpoint("vision")]
        responses = {
            "blind": '{"type":"unknown","answers":"A"}',
            "vision": '{"type":"single","answers":"B"}',
        }

        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(
                service,
                "_ask_multi_endpoint",
                side_effect=lambda endpoint, *_: responses[endpoint.name],
            ),
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")

        self.assertEqual(json.loads(result)["answers"], "B")

    def test_multi_vote_parses_reasoning_prefix_compact_multi_and_judgment(self):
        parse = AIService._canonical_vote

        self.assertEqual(
            parse('分析过程 {"note":"x"}\n最终：{"type":"multi","answers":"BA"}'),
            ("choice", "A", "B"),
        )
        self.assertEqual(parse("正确"), ("judgment", "true"))
        self.assertEqual(parse("错误"), ("judgment", "false"))
        self.assertIsNone(parse("Error code: 429"))

    def test_multi_tied_winners_are_selected_randomly(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint(name) for name in ("a1", "a2", "b1", "b2")]
        responses = {
            "a1": "A",
            "a2": '{"type":"single","answers":"A"}',
            "b1": "B",
            "b2": '{"type":"single","answers":"B"}',
        }

        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(
                service,
                "_ask_multi_endpoint",
                side_effect=lambda endpoint, *_: responses[endpoint.name],
            ),
            patch("src.ai.service.random.choice", return_value=("choice", "B")) as choose,
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")

        choose.assert_called_once()
        self.assertEqual(json.loads(result)["answers"], "B")

    def test_multi_timeout_ignores_late_results(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint("fast"), self.endpoint("slow")]
        release_slow = threading.Event()

        def answer(endpoint, image_b64, prompt, timeout, mime_type):
            if endpoint.name == "slow":
                release_slow.wait(1)
                return "B"
            return "A"

        started_at = time.monotonic()
        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(service, "_multi_ai_timeout", return_value=0.05),
            patch.object(service, "_ask_multi_endpoint", side_effect=answer),
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")
        elapsed = time.monotonic() - started_at
        release_slow.set()

        self.assertLess(elapsed, 0.5)
        self.assertEqual(json.loads(result)["answers"], "A")

    def test_multi_returns_failure_when_every_result_is_invalid(self):
        service = self.make_multi_service()
        endpoints = [self.endpoint("empty"), self.endpoint("error")]
        responses = {"empty": "", "error": "AI调用失败：timeout"}

        with (
            patch.object(service, "_load_multi_ai_endpoints", return_value=endpoints),
            patch.object(
                service,
                "_ask_multi_endpoint",
                side_effect=lambda endpoint, *_: responses[endpoint.name],
            ),
        ):
            result = service._ask_multi("aW1hZ2U=", "prompt")

        self.assertIn("调用失败", result)


class CompactErrorTests(unittest.TestCase):
    def test_html_error_is_classified_not_dumped(self):
        cloudflare = '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>' + "x" * 16000
        self.assertEqual(
            _compact_error(cloudflare),
            "返回 HTML 页面（疑似被网关或 Cloudflare 拦截）",
        )

    def test_long_error_is_truncated(self):
        result = _compact_error("E" * 5000)
        self.assertTrue(result.startswith("E" * 120))
        self.assertIn("原始 5000 字符", result)
        self.assertLess(len(result), 200)

    def test_short_error_is_kept_and_whitespace_collapsed(self):
        self.assertEqual(_compact_error("Error code: 429 -\n  insufficient balance"),
                         "Error code: 429 - insufficient balance")


class MaxLogLengthFilterTests(unittest.TestCase):
    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord(
            "test", logging.WARNING, __file__, 1, message, None, None
        )

    def test_long_record_is_truncated(self):
        gate = _MaxLogLengthFilter(limit=100)
        record = self._record("A" * 5000)
        self.assertTrue(gate.filter(record))
        self.assertIn("原始 5000 字符", record.getMessage())

    def test_normal_record_untouched(self):
        gate = _MaxLogLengthFilter(limit=100)
        record = self._record("正常业务日志")
        self.assertTrue(gate.filter(record))
        self.assertEqual(record.getMessage(), "正常业务日志")


if __name__ == "__main__":
    unittest.main()
