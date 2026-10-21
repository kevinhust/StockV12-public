"""
DeepSeek LLM Provider

DeepSeek-V3 integration with OpenAI-compatible API.
Secondary/fallback provider with excellent cost efficiency.
"""

import os
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
import json

from .provider import LLMProvider, LLMResponse, ToolCall

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class DeepSeekProvider(LLMProvider):
    """
    DeepSeek-V3 provider using OpenAI-compatible API.
    
    Features:
    - OpenAI-compatible tool calling
    - Cost-effective
    - Good reasoning quality
    - Fast inference
    """
    
    name = "deepseek"
    default_model = "deepseek-chat"
    supports_streaming = True
    
    # DeepSeek API endpoint
    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        base_url: Optional[str] = None,
    ):
        super().__init__(
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. "
                "Install with: pip install openai"
            )
        
        self.base_url = base_url or self.DEEPSEEK_BASE_URL
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )
    
    def _prepare_tools(
        self,
        tools: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Prepare tools in OpenAI format.
        
        Ensures all tools have the correct structure.
        """
        if not tools:
            return None
        
        prepared = []
        for tool in tools:
            if "type" not in tool:
                # Convert simple format to OpenAI function format
                prepared.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}})
                    }
                })
            else:
                prepared.append(tool)
        
        return prepared
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> LLMResponse:
        """
        Send chat to DeepSeek and get response.
        
        Uses synchronous OpenAI client (wrapped for consistency).
        """
        start_time = time.time()
        
        # Prepare request
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        
        # Add tools if provided
        prepared_tools = self._prepare_tools(tools)
        if prepared_tools:
            kwargs["tools"] = prepared_tools
            kwargs["tool_choice"] = tool_choice
        
        try:
            response = self.client.chat.completions.create(**kwargs)
            latency_ms = (time.time() - start_time) * 1000
            
            # Parse response
            message = response.choices[0].message
            
            content_text = message.content or ""
            tool_calls = []
            
            if message.tool_calls:
                for tc in message.tool_calls:
                    try:
                        arguments = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        arguments = {"_raw": tc.function.arguments}
                    
                    tool_calls.append(ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments
                    ))
            
            return LLMResponse(
                content=content_text,
                tool_calls=tool_calls,
                finish_reason=response.choices[0].finish_reason or "stop",
                model=response.model,
                usage={
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "total_tokens": response.usage.total_tokens if response.usage else 0
                },
                raw_response=response,
                timestamp=datetime.utcnow().isoformat(),
                latency_ms=latency_ms
            )
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return LLMResponse(
                content=f"API Error: {str(e)}",
                finish_reason="error",
                model=self.model,
                timestamp=datetime.utcnow().isoformat(),
                latency_ms=latency_ms
            )
    
    def format_tool_result(
        self,
        tool_call_id: str,
        result: Any
    ) -> Dict[str, Any]:
        """Format tool result for OpenAI-style message history."""
        content = json.dumps(result) if not isinstance(result, str) else result
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content
        }


class OpenRouterProvider(DeepSeekProvider):
    """
    OpenRouter provider - access multiple models through one API.
    
    Can access DeepSeek, Claude, Grok, and many other models.
    """
    
    name = "openrouter"
    default_model = "deepseek/deepseek-chat"
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs
    ):
        super().__init__(
            api_key=api_key or os.getenv("OPENROUTER_API_KEY"),
            model=model,
            base_url=self.OPENROUTER_BASE_URL,
            **kwargs
        )
