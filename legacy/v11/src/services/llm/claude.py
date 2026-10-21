"""
Claude LLM Provider

Anthropic Claude 3.5 integration with native tool calling support.
Primary recommended provider for production use.
"""

import os
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
import json

from .provider import LLMProvider, LLMResponse, ToolCall

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


class ClaudeProvider(LLMProvider):
    """
    Claude 3.5 Sonnet provider with native tool calling.
    
    Features:
    - Native tool use support
    - Low latency
    - High quality reasoning
    - JSON mode support
    """
    
    name = "claude"
    default_model = "claude-sonnet-4-20250514"
    supports_streaming = True
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ):
        super().__init__(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY"),
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        
        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "anthropic package not installed. "
                "Install with: pip install anthropic"
            )
        
        self.client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=self.timeout
        )
    
    def _convert_openai_tools_to_claude(
        self,
        tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert OpenAI function calling format to Claude tool format.
        
        OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            }
        }
        
        Claude format:
        {
            "name": "...",
            "description": "...",
            "input_schema": {...}
        }
        """
        claude_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool["function"]
                claude_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                })
            else:
                # Already in Claude format or simple format
                claude_tools.append({
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", tool.get("input_schema", {"type": "object", "properties": {}}))
                })
        return claude_tools
    
    def _convert_messages_for_claude(
        self,
        messages: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        """
        Convert messages to Claude format, extracting system prompt.
        
        Returns:
            Tuple of (system_prompt, converted_messages)
        """
        system_prompt = ""
        converted_messages = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                system_prompt = content
            elif role == "assistant":
                # Check for tool calls in assistant message
                if "tool_calls" in msg and msg["tool_calls"]:
                    # Claude uses content blocks for tool use
                    content_blocks = []
                    if content:
                        content_blocks.append({"type": "text", "text": content})
                    
                    for tc in msg["tool_calls"]:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", tc.get("name", "")),
                            "input": tc.get("function", {}).get("arguments", tc.get("arguments", {}))
                        })
                    
                    converted_messages.append({
                        "role": "assistant",
                        "content": content_blocks
                    })
                else:
                    converted_messages.append({
                        "role": "assistant",
                        "content": content
                    })
            elif role == "tool":
                # Convert tool result to Claude format
                converted_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", "")
                    }]
                })
            else:
                # User message
                converted_messages.append({
                    "role": "user",
                    "content": content
                })
        
        return system_prompt, converted_messages
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> LLMResponse:
        """
        Send chat to Claude and get response.
        
        Uses synchronous client wrapped in async due to anthropic SDK design.
        """
        start_time = time.time()
        
        # Convert messages
        system_prompt, claude_messages = self._convert_messages_for_claude(messages)
        
        # Build request kwargs
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": claude_messages,
        }
        
        if system_prompt:
            kwargs["system"] = system_prompt
        
        # Add tools if provided
        if tools:
            kwargs["tools"] = self._convert_openai_tools_to_claude(tools)
            if tool_choice == "auto":
                kwargs["tool_choice"] = {"type": "auto"}
            elif tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif tool_choice == "required":
                kwargs["tool_choice"] = {"type": "any"}
        
        try:
            response = self.client.messages.create(**kwargs)
            latency_ms = (time.time() - start_time) * 1000
            
            # Parse response
            content_text = ""
            tool_calls = []
            
            for block in response.content:
                if block.type == "text":
                    content_text += block.text
                elif block.type == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {}
                    ))
            
            return LLMResponse(
                content=content_text,
                tool_calls=tool_calls,
                finish_reason=response.stop_reason or "end_turn",
                model=response.model,
                usage={
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.input_tokens + response.usage.output_tokens
                },
                raw_response=response,
                timestamp=datetime.utcnow().isoformat(),
                latency_ms=latency_ms
            )
            
        except anthropic.APIError as e:
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
        """Format tool result for Claude message history."""
        content = json.dumps(result) if not isinstance(result, str) else result
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content
        }
