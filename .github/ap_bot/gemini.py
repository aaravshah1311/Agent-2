# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Gemini AI Client Module.

Provides the ``GeminiClient`` class for interacting with Google's Gemini
generative AI models.  Used across all AP Bot modules that require
AI-powered analysis, classification, or text generation.
"""

from typing import Optional

from google import genai

from .config import GEMINI_MODEL
from .logger import logger


class GeminiClient:
    """Client for Google Gemini generative AI.

    Wraps the ``google-genai`` SDK to provide a simple interface
    for prompt-based analysis and classification tasks.

    Attributes:
        client: The configured :class:`genai.Client` instance.
    """

    def __init__(self, api_key: str) -> None:
        """Initialise the Gemini client.

        Configures the `google-genai` client with the provided API key.

        Uses the model specified in ``config.GEMINI_MODEL``.

        Args:
            api_key: Google Gemini API key.
        """
        self.client: genai.Client = genai.Client(api_key=api_key)
        logger.info("GeminiClient initialised with model: %s", GEMINI_MODEL)

    def analyze(
        self, prompt: str, max_tokens: int = 2048
    ) -> Optional[str]:
        """Send a prompt to Gemini and return the generated response.

        Uses a low temperature (0.3) for deterministic, consistent
        results suitable for automated analysis pipelines.

        Args:
            prompt: The text prompt to send to the model.
            max_tokens: Maximum number of tokens in the response.
                        Defaults to 2048.

        Returns:
            The generated response text, or ``None`` if an error occurs.
        """
        try:
            response = self.client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    "max_output_tokens": max_tokens,
                    "temperature": 0.3,
                },
            )

            if response and response.text:
                logger.info(
                    "Gemini analysis completed (%d chars).",
                    len(response.text),
                )
                return response.text

            logger.warning("Gemini returned an empty response.")
            return None

        except Exception as exc:  # noqa: BLE001
            logger.error("Gemini analysis failed: %s", exc)
            return None

    def classify(self, prompt: str) -> Optional[str]:
        """Classify content using Gemini and return a clean label.

        Convenience wrapper around :meth:`analyze` that strips
        leading/trailing whitespace from the response, making it
        suitable for direct use as a classification label.

        Args:
            prompt: The classification prompt to send to the model.

        Returns:
            The stripped classification result, or ``None`` on failure.
        """
        result = self.analyze(prompt)
        if result:
            stripped = result.strip()
            logger.info("Classification result: %s", stripped)
            return stripped
        return None

    def generate(self, prompt: str) -> str:
        """Send a prompt to Gemini and return the generated response as a string.

        Args:
            prompt: The text prompt to send to the model.

        Returns:
            The generated response text, or an empty string if an error occurs.
        """
        result = self.analyze(prompt)
        return result if result is not None else ""

