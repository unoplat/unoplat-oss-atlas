# Standard Library
from typing import TYPE_CHECKING, cast

# Third Party
from langchain_core.messages import HumanMessage

# First Party
from onyx.llm.utils import message_to_prompt_and_imgs
from onyx.tools.tool import Tool

if TYPE_CHECKING:
    # First Party
    from onyx.chat.prompt_builder.build import AnswerPromptBuilder
    from onyx.tools.message import ToolCallSummary
    from onyx.tools.models import ToolResponse
    from onyx.tools.tool_implementations.custom.custom_tool import CustomToolCallSummary


def build_user_message_for_non_tool_calling_llm(
    message: HumanMessage,
    tool_name: str,
    *args: "ToolResponse",
) -> str:
    query, _ = message_to_prompt_and_imgs(message)

    tool_run_summary = cast("CustomToolCallSummary", args[0].response).tool_result
    return f"""
Here's the result from the {tool_name} tool:

{tool_run_summary}

Now respond to the following:

{query}
""".strip()


class BaseTool(Tool):
    def build_next_prompt(
        self,
        prompt_builder: "AnswerPromptBuilder",
        tool_call_summary: "ToolCallSummary",
        tool_responses: list["ToolResponse"],
        using_tool_calling_llm: bool,
    ) -> "AnswerPromptBuilder":
        if using_tool_calling_llm:
            prompt_builder.append_message(tool_call_summary.tool_call_request)
            prompt_builder.append_message(tool_call_summary.tool_call_result)
        else:
            prompt_builder.update_user_prompt(
                HumanMessage(
                    content=build_user_message_for_non_tool_calling_llm(
                        prompt_builder.user_message_and_token_cnt[0],
                        self.name,
                        *tool_responses,
                    )
                )
            )

        return prompt_builder
