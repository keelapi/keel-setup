"""Deterministic source fixtures for the single Fast First Run golden path."""

GOLDEN_APP = '''from __future__ import annotations

import os
import sys
from typing import Any

DEFAULT_MODEL = "gpt-5.6"
MAX_INPUT_CHARACTERS = 20_000


def summarize(text: str, *, client: Any | None = None, model: str | None = None) -> str:
    source = text.strip()
    if not source:
        raise ValueError("Text cannot be empty.")
    if len(source) > MAX_INPUT_CHARACTERS:
        raise ValueError(f"Text cannot exceed {MAX_INPUT_CHARACTERS:,} characters.")

    if client is None:
        from openai import OpenAI

        client = OpenAI()

    response = client.responses.create(
        model=model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        instructions=(
            "Summarize the supplied text in no more than three concise sentences. "
            "Preserve important names, numbers, and qualifications."
        ),
        input=source,
    )

    summary = response.output_text.strip()
    if not summary:
        raise ValueError("The model returned an empty summary.")
    return summary


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print("Usage: python app.py <text to summarize>", file=sys.stderr)
        return 2
    print(summarize(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

GOLDEN_TEST = '''from __future__ import annotations

import unittest
from types import SimpleNamespace

from app import MAX_INPUT_CHARACTERS, summarize


class FakeResponses:
    def __init__(self, output_text: str = "A short summary.") -> None:
        self.output_text = output_text
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeClient:
    def __init__(self, output_text: str = "A short summary.") -> None:
        self.responses = FakeResponses(output_text)


class SummarizerTests(unittest.TestCase):
    def test_sends_text_to_the_selected_model(self) -> None:
        client = FakeClient("The answer is 42.")
        result = summarize("  A report whose answer is 42.  ", client=client, model="test-model")
        self.assertEqual(result, "The answer is 42.")
        self.assertEqual(len(client.responses.calls), 1)
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["input"], "A report whose answer is 42.")
        self.assertIn("three concise sentences", str(request["instructions"]))

    def test_rejects_empty_text_before_calling_the_provider(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            summarize("   ", client=client)
        self.assertEqual(client.responses.calls, [])

    def test_rejects_overlong_text_before_calling_the_provider(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            summarize("x" * (MAX_INPUT_CHARACTERS + 1), client=client)
        self.assertEqual(client.responses.calls, [])

    def test_rejects_an_empty_model_response(self) -> None:
        client = FakeClient("  ")
        with self.assertRaisesRegex(ValueError, "empty summary"):
            summarize("Some source text", client=client)


if __name__ == "__main__":
    unittest.main()
'''

CUSTOM_BASE_URL_APP = GOLDEN_APP.replace("client = OpenAI()", "client = OpenAI(base_url='https://gateway.invalid/v1')")
STREAMING_APP = GOLDEN_APP.replace("input=source,", "input=source,\n        stream=True,")
TWO_PROVIDER_PATHS_APP = GOLDEN_APP + '''

def second_path(text: str):
    from openai import OpenAI
    other_client = OpenAI()
    return other_client.responses.create(model="gpt-5.6", input=text)
'''
UNKNOWN_WRAPPER_APP = GOLDEN_APP.replace("client.responses.create(", "wrapper.responses.create(")

RETAINED_IF_ELSE_APP = GOLDEN_APP.replace(
    "    if client is None:\n",
    '''    if source.startswith("Note: "):
        source = source.removeprefix("Note: ")
    else:
        source = source

    if client is None:
''',
)

RETAINED_TRY_EXCEPT_APP = GOLDEN_APP.replace(
    "    if len(source) > MAX_INPUT_CHARACTERS:\n"
    "        raise ValueError(f\"Text cannot exceed {MAX_INPUT_CHARACTERS:,} characters.\")\n",
    '''    try:
        too_long = len(source) > MAX_INPUT_CHARACTERS
    except TypeError as exc:
        raise ValueError("Text must support length checks.") from exc
    if too_long:
        raise ValueError(f"Text cannot exceed {MAX_INPUT_CHARACTERS:,} characters.")
''',
)

SYNTAX_FAILURE = "\nthis is not valid python !!!\n"
PROVIDER_FALLBACK = "\nimport requests\nrequests.post('https://api.openai.com/v1/responses')\n"
PROXY_ROUTE = "\nPROXY = 'https://api.keelapi.com/v1/proxy/openai'\n"
DIRECT_PROVIDER_REMAINS = "\nfrom openai import OpenAI\nOpenAI().responses.create(model='x', input='y')\n"
