"""OpenAI Responses APIの単発Structured Outputs transport。"""

import json
from collections.abc import Mapping

import httpx

from ai_rpg.llm.structured import (
    LLMPurpose,
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
)


class OpenAIResponsesTransport:
    """暗黙retryなしでResponses APIを一回だけ呼び出す。"""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int,
        *,
        base_url: str = "https://api.openai.com/v1",
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API keyが必要です")
        if timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正数である必要があります")
        self._api_key = api_key
        self._timeout = float(timeout_seconds)
        self._base_url = base_url.rstrip("/")
        self._http_transport = http_transport

    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object:
        body = {
            "model": model_id,
            "instructions": instruction,
            "input": input_data,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"ai_rpg_{purpose}",
                    "strict": True,
                    "schema": output_schema,
                }
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._http_transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/responses",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
        except httpx.TimeoutException as error:
            raise TimeoutError("OpenAI request timed out") from error
        except httpx.NetworkError as error:
            raise ConnectionError("OpenAI request failed") from error

        if not response.is_success:
            raise ProviderHTTPError(response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderOutputError("OpenAI response body is not JSON") from error
        if not isinstance(payload, Mapping):
            raise ProviderOutputError("OpenAI response body must be an object")
        if payload.get("error") is not None:
            raise ProviderHTTPError(response.status_code)
        if payload.get("status") != "completed":
            raise ProviderOutputError("OpenAI response did not complete")

        output = payload.get("output")
        if not isinstance(output, list):
            raise ProviderOutputError("OpenAI response output is missing")
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "refusal":
                    raise ProviderRefusalError("OpenAI model refused the request")
                if part.get("type") == "output_text" and isinstance(
                    part.get("text"), str
                ):
                    text_parts.append(part["text"])
        output_text = "".join(text_parts).strip()
        if not output_text:
            raise ProviderOutputError("OpenAI structured output is empty")
        try:
            return json.loads(output_text)
        except json.JSONDecodeError as error:
            raise ProviderOutputError("OpenAI structured output is invalid JSON") from error
