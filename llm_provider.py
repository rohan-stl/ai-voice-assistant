import re
from abc import ABC, abstractmethod


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> or <thought>...</thought> reasoning blocks emitted by models."""
    if not text:
        return ""
    # Strip closed think/thought tags
    text = re.sub(r"<(think|thought)>.*?</\1>", "", text, flags=re.DOTALL)
    # Strip unclosed <think> or <thought> tags up to the end if model cut off
    text = re.sub(r"<(think|thought)>.*$", "", text, flags=re.DOTALL)
    return text.strip()


class LLMProvider(ABC):
    """Base interface for all LLM providers."""

    @abstractmethod
    def chat(self, prompt: str) -> str:
        """Send a prompt and return the raw text response."""
        pass


class GroqLLMProvider(LLMProvider):
    """Groq API (qwen/qwen3.8-27b, free tier — fast, crisp non-reasoning model)."""

    def __init__(self, api_key: str, model="qwen/qwen3.8-27b"):
        from groq import Groq
        self.client = Groq(api_key=api_key)
        self.model = model

    def chat(self, prompt: str) -> str:
        print(f"Calling Groq LLM using model {self.model}")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        raw = response.choices[0].message.content or ""
        return _strip_think_blocks(raw)


class OpenRouterLLMProvider(LLMProvider):
    """OpenRouter API client (legacy fallback)."""
    
    def __init__(self, api_key: str, model="openrouter/free"):
        from openai import OpenAI
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1"
        )
        self.model = model
    
    def chat(self, prompt: str) -> str:
        print(f"Calling OpenRouter LLM using model {self.model}")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content
