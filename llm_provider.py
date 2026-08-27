from abc import ABC, abstractmethod

class LLMProvider(ABC):
    """Base interface for all LLM providers."""
    
    @abstractmethod
    def chat(self, prompt: str) -> str:
        """Send a prompt and return the raw text response."""
        pass


class GroqLLMProvider(LLMProvider):
    """Groq API (Llama-3.3-70b-versatile, free tier)."""
    
    def __init__(self, api_key: str, model="llama-3.3-70b-versatile"):
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
        return response.choices[0].message.content


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
