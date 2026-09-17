"""独立process向け開発runtimeの最小契約。"""

import json

import pytest

from ai_rpg.config import Settings
from ai_rpg.llm import DevelopmentFakeTransport, OpenAIResponsesTransport
from ai_rpg.runtime import build_provider_transport


@pytest.mark.asyncio
async def test_development_fake_transport_supports_each_worker_purpose() -> None:
    transport = DevelopmentFakeTransport()

    intent = await transport.request("fake", "intent", "instruction", "{}", {})
    narrative = await transport.request("fake", "narrative", "instruction", "{}", {})
    result = await transport.request(
        "fake",
        "result_narration",
        "instruction",
        json.dumps(
            {
                "resolved_actions": [
                    {"result": {"facts": ["技能判定はsuccess(合計12)"]}}
                ]
            }
        ),
        {},
    )

    assert intent == {
        "kind": "action_plan",
        "actions": [
            {
                "kind": "skill_check",
                "skill_ref": "perception",
                "objective": "周囲の痕跡を見つける",
                "target_ref": None,
            }
        ],
    }
    assert narrative == {
        "kind": "narrative",
        "narration": "静かな時間が流れている。",
        "choices": [],
    }
    assert result == {
        "narration": "技能判定はsuccess(合計12)",
        "choices": [],
    }


def test_real_transport_requires_api_key_only_when_selected() -> None:
    settings = Settings(openai_api_key=None)

    assert isinstance(build_provider_transport(settings, fake=True), DevelopmentFakeTransport)
    with pytest.raises(ValueError, match="AIRPG_OPENAI_API_KEY"):
        build_provider_transport(settings, fake=False)


def test_real_transport_is_built_from_secret_setting() -> None:
    settings = Settings(openai_api_key="test-secret")

    assert isinstance(build_provider_transport(settings, fake=False), OpenAIResponsesTransport)
