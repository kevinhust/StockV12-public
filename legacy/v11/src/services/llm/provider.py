"""
LLM Provider Abstraction Layer

Base class for all LLM providers (Claude, DeepSeek, Grok).
Provides unified interface for tool calling and response parsing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
import json


@dataclass
class ToolCall:
    """Represents a single tool call request from LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    raw_response: Optional[Any] = None
    timestamp: str = ""
    latency_ms: float = 0
    
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0
    
    def get_text_content(self) -> str:
        """Get text content from response."""
        return self.content
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "content": self.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ],
            "finish_reason": self.finish_reason,
            "model": self.model,
            "usage": self.usage,
            "timestamp": self.timestamp,
            "latency_ms": self.latency_ms
        }


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.
    
    All providers must implement:
    - chat(): Send messages and get response
    - supports_tool_calling(): Whether provider supports function calling
    """
    
    name: str = "base"
    default_model: str = ""
    supports_streaming: bool = False
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ):
        """
        Initialize provider.
        
        Args:
            api_key: API key for the provider
            model: Model to use (defaults to provider's default)
            timeout: Request timeout in seconds
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (lower = more deterministic)
        """
        self.api_key = api_key
        self.model = model or self.default_model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
    
    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> LLMResponse:
        """
        Send chat messages and get response.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool schemas (OpenAI format)
            tool_choice: How to handle tools ("auto", "none", or specific tool)
            
        Returns:
            LLMResponse with content and/or tool calls
        """
        pass
    
    @abstractmethod
    def format_tool_result(
        self,
        tool_call_id: str,
        result: Any
    ) -> Dict[str, Any]:
        """
        Format tool result for inclusion in message history.
        
        Args:
            tool_call_id: ID of the tool call this result is for
            result: Result from tool execution
            
        Returns:
            Message dict ready to append to messages
        """
        pass
    
    def supports_tool_calling(self) -> bool:
        """Check if provider supports tool calling."""
        return True
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get current model information."""
        return {
            "provider": self.name,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "supports_streaming": self.supports_streaming,
            "supports_tool_calling": self.supports_tool_calling(),
        }


class MockLLMProvider(LLMProvider):
    """Mock provider for testing without API calls."""
    
    name = "mock"
    default_model = "mock-v1"
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mock_responses: List[LLMResponse] = []
        self._call_count = 0
    
    def set_mock_response(self, response: LLMResponse):
        """Set a mock response to return."""
        self._mock_responses.append(response)
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> LLMResponse:
        """Return mock response."""
        self._call_count += 1
        
        if self._mock_responses:
            return self._mock_responses.pop(0)
        
        # Default mock response
        return LLMResponse(
            content=json.dumps({
                "reasoning_summary": "Mock response - no real LLM call made",
                "market_regime": "range_strong",
                "watchlist_top5": ["NVDA", "TSLA", "AMD", "SMCI", "PLTR"],
                "decision": {
                    "action": "hold",
                    "symbol": "",
                    "size_pct": 0,
                    "confidence": 0.5,
                    "stop_loss_pct": 0,
                    "take_profit_pct": 0,
                    "reason": "Mock mode - defaulting to HOLD",
                    "cooldown_active": False
                },
                "gatekeeper_self_check": {"passed": True, "failed_rules": []}
            }),
            finish_reason="stop",
            model=self.model,
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            timestamp=datetime.utcnow().isoformat(),
            latency_ms=10.0
        )
    
    def format_tool_result(
        self,
        tool_call_id: str,
        result: Any
    ) -> Dict[str, Any]:
        """Format tool result."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result) if not isinstance(result, str) else result
        }
