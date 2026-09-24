"""
A small client for talking to Ollama, a program that runs AI models locally.

Ollama listens on a web address (by default http://localhost:11434). We send
it a question over HTTP and read the answer back. Only Python's built-in
`urllib` is used, so no extra packages need to be installed.

If Ollama can't be reached (not running, or too slow to answer), the
methods raise OllamaUnavailable so the caller can report the problem.
"""
from __future__ import annotations

import json
import socket
import urllib.request
import urllib.error


class OllamaUnavailable(RuntimeError):
    """Raised when Ollama can't be reached or doesn't answer in time."""


class OllamaClient:
    """Sends prompts to one AI model on one Ollama server."""

    def __init__(
        self,
        model: str = "llama3.1",
        host: str = "http://localhost:11434",
        timeout: int = 300,
        temperature: float = 0.0,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        # "Temperature" controls how random the model's answers are.
        # 0 means "always pick the most likely answer", so the same question
        # gives (almost) the same answer every time. We want that here,
        # because we're asking for facts about code, not creative writing.
        self.temperature = temperature

    def available(self) -> bool:
        """True if the Ollama server answers within 3 seconds."""
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception:
            return False

    def generate(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        """Send `prompt` to the model and return its answer as text.

        `system` is an optional "system prompt": instructions that tell the
        model how to behave. With `json_mode=True`, Ollama is asked to reply
        with JSON only."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
        except (TimeoutError, socket.timeout) as e:
            # The first request after Ollama starts must also load the model
            # into memory, which can take a while. So a timeout doesn't always
            # mean the server is broken -- it may just need more time.
            raise OllamaUnavailable(
                f"Ollama at {self.host} did not answer within {self.timeout}s "
                f"(model '{self.model}' may still be loading; try a larger --timeout)"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            # urllib sometimes wraps a timeout inside a URLError; treat it the same way.
            if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                raise OllamaUnavailable(
                    f"Ollama at {self.host} did not answer within {self.timeout}s "
                    f"(model '{self.model}' may still be loading; try a larger --timeout)"
                ) from e
            raise OllamaUnavailable(f"Could not reach Ollama at {self.host}: {e}") from e

    def generate_json(self, prompt: str, system: str | None = None) -> dict:
        """Like generate(), but turn the answer into a Python dict.

        Raises json.JSONDecodeError if the answer isn't valid JSON."""
        text = self.generate(prompt, system=system, json_mode=True)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Sometimes the model adds extra text around the JSON. Try again
            # using only the part between the first "{" and the last "}".
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start:end + 1])
            raise
