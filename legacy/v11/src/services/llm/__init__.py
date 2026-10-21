"""LLM Provider Package for Trading Agent."""

from .provider import LLMProvider, LLMResponse, MockLLMProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .deepseek import DeepSeekProvider

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "MockLLMProvider",
    "GeminiProvider",
    "GrokProvider",
    "DeepSeekProvider",
]


# Provider priority: Gemini > Grok > DeepSeek
PROVIDER_PRIORITY = ["gemini", "grok", "deepseek"]


def get_default_provider(provider_name: str = "gemini", **kwargs) -> LLMProvider:
    """
    Get the default LLM provider.
    
    Priority: Gemini 2.0 > Grok > DeepSeek-V3
    
    Args:
        provider_name: Name of provider ("gemini", "grok", "deepseek")
        **kwargs: Additional arguments for provider
        
    Returns:
        Configured LLM provider instance
    """
    providers = {
        "gemini": GeminiProvider,
        "grok": GrokProvider,
        "deepseek": DeepSeekProvider,
    }
    
    provider_cls = providers.get(provider_name, GeminiProvider)
    return provider_cls(**kwargs)


def get_provider_with_fallback(**kwargs) -> LLMProvider:
    """
    Get a provider with automatic fallback if primary fails.
    
    Tries providers in order: Gemini > Grok > DeepSeek
    Returns the first one that initializes successfully.
    """
    providers = {
        "gemini": GeminiProvider,
        "grok": GrokProvider,
        "deepseek": DeepSeekProvider,
    }
    
    last_error = None
    for name in PROVIDER_PRIORITY:
        try:
            provider = providers[name](**kwargs)
            return provider
        except Exception as e:
            last_error = e
            continue
    
    # If all fail, return mock
    print(f"Warning: All LLM providers failed. Last error: {last_error}")
    return MockLLMProvider(**kwargs)
