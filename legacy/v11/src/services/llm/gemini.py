"""
Gemini LLM Provider

Google Gemini 2.0 integration with native function calling support.
Primary recommended provider for production use.
"""

import os
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
import json

from .provider import LLMProvider, LLMResponse, ToolCall

try:
    import google.generativeai as genai
    from google.generativeai.types import GenerationConfig
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


class GeminiProvider(LLMProvider):
    """
    Google Gemini 2.0 provider with native function calling.
    
    Features:
    - Native tool use support
    - Long context window (2M tokens)
    - Multimodal capabilities
    - Fast inference
    """
    
    name = "gemini"
    default_model = "gemini-2.0-flash"
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
            api_key=api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        
        if not GENAI_AVAILABLE:
            raise ImportError(
                "google-generativeai package not installed. "
                "Install with: pip install google-generativeai"
            )
        
        # Configure the SDK
        genai.configure(api_key=self.api_key)
        
        # Create model instance
        self.generation_config = GenerationConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
        )
        
        self._model = None
    
    def _get_model(self, tools: Optional[List] = None):
        """Get or create model instance with optional tools."""
        model_kwargs = {
            "model_name": self.model,
            "generation_config": self.generation_config,
        }
        
        if tools:
            model_kwargs["tools"] = tools
        
        return genai.GenerativeModel(**model_kwargs)
    
    def _convert_openai_tools_to_gemini(
        self,
        tools: List[Dict[str, Any]]
    ) -> List:
        """
        Convert OpenAI function calling format to Gemini tool format.
        
        OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            }
        }
        
        Gemini format uses FunctionDeclaration objects.
        """
        from google.generativeai.types import FunctionDeclaration, Tool
        
        function_declarations = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool["function"]
                name = func["name"]
                description = func.get("description", "")
                parameters = func.get("parameters", {})
            else:
                name = tool.get("name", "")
                description = tool.get("description", "")
                parameters = tool.get("parameters", {})
            
            # Create function declaration
            function_declarations.append(
                FunctionDeclaration(
                    name=name,
                    description=description,
                    parameters=parameters
                )
            )
        
        return [Tool(function_declarations=function_declarations)]
    
    def _convert_messages_for_gemini(
        self,
        messages: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        """
        Convert messages to Gemini format, extracting system instruction.
        
        Returns:
            Tuple of (system_instruction, chat_history)
        """
        system_instruction = ""
        chat_history = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                system_instruction = content
            elif role == "assistant":
                # Check for tool calls
                if "tool_calls" in msg and msg["tool_calls"]:
                    # Gemini uses function_call in parts
                    parts = []
                    if content:
                        parts.append({"text": content})
                    
                    for tc in msg["tool_calls"]:
                        func_name = tc.get("function", {}).get("name", tc.get("name", ""))
                        func_args = tc.get("function", {}).get("arguments", tc.get("arguments", {}))
                        if isinstance(func_args, str):
                            try:
                                func_args = json.loads(func_args)
                            except:
                                func_args = {}
                        
                        parts.append({
                            "function_call": {
                                "name": func_name,
                                "args": func_args
                            }
                        })
                    
                    chat_history.append({
                        "role": "model",
                        "parts": parts
                    })
                else:
                    chat_history.append({
                        "role": "model",
                        "parts": [{"text": content}]
                    })
            elif role == "tool":
                # Tool result in Gemini format
                chat_history.append({
                    "role": "user",
                    "parts": [{
                        "function_response": {
                            "name": msg.get("name", "unknown"),
                            "response": {"result": msg.get("content", "")}
                        }
                    }]
                })
            else:
                # User message
                chat_history.append({
                    "role": "user",
                    "parts": [{"text": content}]
                })
        
        return system_instruction, chat_history
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> LLMResponse:
        """
        Send chat to Gemini and get response.
        """
        start_time = time.time()
        
        # Convert messages
        system_instruction, chat_history = self._convert_messages_for_gemini(messages)
        
        # Convert tools
        gemini_tools = None
        if tools:
            gemini_tools = self._convert_openai_tools_to_gemini(tools)
        
        try:
            # Create model with system instruction
            model_kwargs = {
                "model_name": self.model,
                "generation_config": self.generation_config,
            }
            
            if system_instruction:
                model_kwargs["system_instruction"] = system_instruction
            
            if gemini_tools:
                model_kwargs["tools"] = gemini_tools
            
            model = genai.GenerativeModel(**model_kwargs)
            
            # Start chat and send message
            chat = model.start_chat(history=chat_history[:-1] if chat_history else [])
            
            # Get the last user message
            last_message = chat_history[-1] if chat_history else {"parts": [{"text": ""}]}
            last_content = last_message.get("parts", [{}])[0].get("text", "")
            
            response = chat.send_message(last_content)
            latency_ms = (time.time() - start_time) * 1000
            
            # Parse response
            content_text = ""
            tool_calls = []
            
            for part in response.parts:
                if hasattr(part, "text") and part.text:
                    content_text += part.text
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=f"call_{fc.name}_{int(time.time()*1000)}",
                        name=fc.name,
                        arguments=dict(fc.args) if fc.args else {}
                    ))
            
            # Get usage info
            usage = {}
            if hasattr(response, "usage_metadata"):
                usage = {
                    "prompt_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                    "completion_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
                    "total_tokens": getattr(response.usage_metadata, "total_token_count", 0)
                }
            
            return LLMResponse(
                content=content_text,
                tool_calls=tool_calls,
                finish_reason="stop" if not tool_calls else "tool_calls",
                model=self.model,
                usage=usage,
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
        result: Any,
        tool_name: str = ""
    ) -> Dict[str, Any]:
        """Format tool result for Gemini message history."""
        content = json.dumps(result) if not isinstance(result, str) else result
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": content
        }
