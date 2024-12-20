# Standard Library
import logging
import random
import re
import string
import time
import uuid
from typing import Any, cast

# Third Party
from retry import retry
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.models.blocks import Block, SectionBlock
from slack_sdk.models.metadata import Metadata
from slack_sdk.socket_mode import SocketModeClient

# First Party
from onyx.configs.app_configs import DISABLE_TELEMETRY
from onyx.configs.constants import ID_SEPARATOR, MessageType
from onyx.configs.onyxbot_configs import DANSWER_BOT_FEEDBACK_VISIBILITY, DANSWER_BOT_MAX_QPM, DANSWER_BOT_MAX_WAIT_TIME, DANSWER_BOT_NUM_RETRIES, DANSWER_BOT_RESPONSE_LIMIT_PER_TIME_PERIOD, DANSWER_BOT_RESPONSE_LIMIT_TIME_PERIOD_SECONDS
from onyx.connectors.slack.utils import SlackTextCleaner, make_slack_api_rate_limited
from onyx.db.engine import get_session_with_tenant
from onyx.db.users import get_user_by_email
from onyx.llm.exceptions import GenAIDisabledException
from onyx.llm.factory import get_default_llms
from onyx.llm.utils import dict_based_prompt_to_langchain_prompt, message_to_string
from onyx.onyxbot.slack.constants import FeedbackVisibility
from onyx.onyxbot.slack.models import ThreadMessage
from onyx.prompts.miscellaneous_prompts import SLACK_LANGUAGE_REPHRASE_PROMPT
from onyx.utils.logger import setup_logger
from onyx.utils.telemetry import RecordType, optional_telemetry
from onyx.utils.text_processing import replace_whitespaces_w_space

logger = setup_logger()


_DANSWER_BOT_SLACK_BOT_ID: str | None = None
_DANSWER_BOT_MESSAGE_COUNT: int = 0
_DANSWER_BOT_COUNT_START_TIME: float = time.time()


def get_onyx_bot_slack_bot_id(web_client: WebClient) -> Any:
    global _DANSWER_BOT_SLACK_BOT_ID
    if _DANSWER_BOT_SLACK_BOT_ID is None:
        _DANSWER_BOT_SLACK_BOT_ID = web_client.auth_test().get("user_id")
    return _DANSWER_BOT_SLACK_BOT_ID


def check_message_limit() -> bool:
    """
    This isnt a perfect solution.
    High traffic at the end of one period and start of another could cause
    the limit to be exceeded.
    """
    if DANSWER_BOT_RESPONSE_LIMIT_PER_TIME_PERIOD == 0:
        return True
    global _DANSWER_BOT_MESSAGE_COUNT
    global _DANSWER_BOT_COUNT_START_TIME
    time_since_start = time.time() - _DANSWER_BOT_COUNT_START_TIME
    if time_since_start > DANSWER_BOT_RESPONSE_LIMIT_TIME_PERIOD_SECONDS:
        _DANSWER_BOT_MESSAGE_COUNT = 0
        _DANSWER_BOT_COUNT_START_TIME = time.time()
    if (_DANSWER_BOT_MESSAGE_COUNT + 1) > DANSWER_BOT_RESPONSE_LIMIT_PER_TIME_PERIOD:
        logger.error(
            f"OnyxBot has reached the message limit {DANSWER_BOT_RESPONSE_LIMIT_PER_TIME_PERIOD}"
            f" for the time period {DANSWER_BOT_RESPONSE_LIMIT_TIME_PERIOD_SECONDS} seconds."
            " These limits are configurable in backend/onyx/configs/onyxbot_configs.py"
        )
        return False
    _DANSWER_BOT_MESSAGE_COUNT += 1
    return True


def rephrase_slack_message(msg: str) -> str:
    def _get_rephrase_message() -> list[dict[str, str]]:
        messages = [
            {
                "role": "user",
                "content": SLACK_LANGUAGE_REPHRASE_PROMPT.format(query=msg),
            },
        ]

        return messages

    try:
        llm, _ = get_default_llms(timeout=5)
    except GenAIDisabledException:
        logger.warning("Unable to rephrase Slack user message, Gen AI disabled")
        return msg
    messages = _get_rephrase_message()
    filled_llm_prompt = dict_based_prompt_to_langchain_prompt(messages)
    model_output = message_to_string(llm.invoke(filled_llm_prompt))
    logger.debug(model_output)

    return model_output


def update_emote_react(
    emoji: str,
    channel: str,
    message_ts: str | None,
    remove: bool,
    client: WebClient,
) -> None:
    try:
        if not message_ts:
            logger.error(
                f"Tried to remove a react in {channel} but no message specified"
            )
            return

        func = client.reactions_remove if remove else client.reactions_add
        slack_call = make_slack_api_rate_limited(func)  # type: ignore
        slack_call(
            name=emoji,
            channel=channel,
            timestamp=message_ts,
        )
    except SlackApiError as e:
        if remove:
            logger.error(f"Failed to remove Reaction due to: {e}")
        else:
            logger.error(f"Was not able to react to user message due to: {e}")


def remove_onyx_bot_tag(message_str: str, client: WebClient) -> str:
    bot_tag_id = get_onyx_bot_slack_bot_id(web_client=client)
    return re.sub(rf"<@{bot_tag_id}>\s", "", message_str)


def _check_for_url_in_block(block: Block) -> bool:
    """
    Check if the block has a key that contains "url" in it
    """
    block_dict = block.to_dict()

    def check_dict_for_url(d: dict) -> bool:
        for key, value in d.items():
            if "url" in key.lower():
                return True
            if isinstance(value, dict):
                if check_dict_for_url(value):
                    return True
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and check_dict_for_url(item):
                        return True
        return False

    return check_dict_for_url(block_dict)


def _build_error_block(error_message: str) -> Block:
    """
    Build an error block to display in slack so that the user can see
    the error without completely breaking
    """
    display_text = (
        "There was an error displaying all of the Onyx answers."
        f" Please let an admin or an onyx developer know. Error: {error_message}"
    )
    return SectionBlock(text=display_text)


@retry(
    tries=DANSWER_BOT_NUM_RETRIES,
    delay=0.25,
    backoff=2,
    logger=cast(logging.Logger, logger),
)
def respond_in_thread(
    client: WebClient,
    channel: str,
    thread_ts: str | None,
    text: str | None = None,
    blocks: list[Block] | None = None,
    receiver_ids: list[str] | None = None,
    metadata: Metadata | None = None,
    unfurl: bool = True,
) -> list[str]:
    if not text and not blocks:
        raise ValueError("One of `text` or `blocks` must be provided")

    message_ids: list[str] = []
    if not receiver_ids:
        slack_call = make_slack_api_rate_limited(client.chat_postMessage)
        try:
            response = slack_call(
                channel=channel,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                metadata=metadata,
                unfurl_links=unfurl,
                unfurl_media=unfurl,
            )
        except Exception as e:
            logger.warning(f"Failed to post message: {e} \n blocks: {blocks}")
            logger.warning("Trying again without blocks that have urls")

            if not blocks:
                raise e

            blocks_without_urls = [
                block for block in blocks if not _check_for_url_in_block(block)
            ]
            blocks_without_urls.append(_build_error_block(str(e)))

            # Try again wtihout blocks containing url
            response = slack_call(
                channel=channel,
                text=text,
                blocks=blocks_without_urls,
                thread_ts=thread_ts,
                metadata=metadata,
                unfurl_links=unfurl,
                unfurl_media=unfurl,
            )

        message_ids.append(response["message_ts"])
    else:
        slack_call = make_slack_api_rate_limited(client.chat_postEphemeral)
        for receiver in receiver_ids:
            try:
                response = slack_call(
                    channel=channel,
                    user=receiver,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                    metadata=metadata,
                    unfurl_links=unfurl,
                    unfurl_media=unfurl,
                )
            except Exception as e:
                logger.warning(f"Failed to post message: {e} \n blocks: {blocks}")
                logger.warning("Trying again without blocks that have urls")

                if not blocks:
                    raise e

                blocks_without_urls = [
                    block for block in blocks if not _check_for_url_in_block(block)
                ]
                blocks_without_urls.append(_build_error_block(str(e)))

                # Try again wtihout blocks containing url
                response = slack_call(
                    channel=channel,
                    user=receiver,
                    text=text,
                    blocks=blocks_without_urls,
                    thread_ts=thread_ts,
                    metadata=metadata,
                    unfurl_links=unfurl,
                    unfurl_media=unfurl,
                )

            message_ids.append(response["message_ts"])

    return message_ids


def build_feedback_id(
    message_id: int,
    document_id: str | None = None,
    document_rank: int | None = None,
) -> str:
    unique_prefix = "".join(random.choice(string.ascii_letters) for _ in range(10))
    if document_id is not None:
        if not document_id or document_rank is None:
            raise ValueError("Invalid document, missing information")
        if ID_SEPARATOR in document_id:
            raise ValueError(
                "Separator pattern should not already exist in document id"
            )
        feedback_id = ID_SEPARATOR.join(
            [str(message_id), document_id, str(document_rank)]
        )
    else:
        feedback_id = str(message_id)

    return unique_prefix + ID_SEPARATOR + feedback_id


def build_continue_in_web_ui_id(
    message_id: int,
) -> str:
    unique_prefix = str(uuid.uuid4())[:10]
    return unique_prefix + ID_SEPARATOR + str(message_id)


def decompose_action_id(feedback_id: str) -> tuple[int, str | None, int | None]:
    """Decompose into query_id, document_id, document_rank, see above function"""
    try:
        components = feedback_id.split(ID_SEPARATOR)
        if len(components) != 2 and len(components) != 4:
            raise ValueError("Feedback ID does not contain right number of elements")

        if len(components) == 2:
            return int(components[-1]), None, None

        return int(components[1]), components[2], int(components[3])

    except Exception as e:
        logger.error(e)
        raise ValueError("Received invalid Feedback Identifier")


def get_view_values(state_values: dict[str, Any]) -> dict[str, str]:
    """Extract view values

    Args:
        state_values (dict): The Slack view-submission values

    Returns:
        dict: keys/values of the view state content
    """
    view_values = {}
    for _, view_data in state_values.items():
        for k, v in view_data.items():
            if (
                "selected_option" in v
                and isinstance(v["selected_option"], dict)
                and "value" in v["selected_option"]
            ):
                view_values[k] = v["selected_option"]["value"]
            elif "selected_options" in v and isinstance(v["selected_options"], list):
                view_values[k] = [
                    x["value"] for x in v["selected_options"] if "value" in x
                ]
            elif "selected_date" in v:
                view_values[k] = v["selected_date"]
            elif "value" in v:
                view_values[k] = v["value"]
    return view_values


def translate_vespa_highlight_to_slack(match_strs: list[str], used_chars: int) -> str:
    def _replace_highlight(s: str) -> str:
        s = re.sub(r"(?<=[^\s])<hi>(.*?)</hi>", r"\1", s)
        s = s.replace("</hi>", "*").replace("<hi>", "*")
        return s

    final_matches = [
        replace_whitespaces_w_space(_replace_highlight(match_str)).strip()
        for match_str in match_strs
        if match_str
    ]
    combined = "... ".join(final_matches)

    # Slack introduces "Show More" after 300 on desktop which is ugly
    # But don't trim the message if there is still a highlight after 300 chars
    remaining = 300 - used_chars
    if len(combined) > remaining and "*" not in combined[remaining:]:
        combined = combined[: remaining - 3] + "..."

    return combined


def remove_slack_text_interactions(slack_str: str) -> str:
    slack_str = SlackTextCleaner.replace_tags_basic(slack_str)
    slack_str = SlackTextCleaner.replace_channels_basic(slack_str)
    slack_str = SlackTextCleaner.replace_special_mentions(slack_str)
    slack_str = SlackTextCleaner.replace_links(slack_str)
    slack_str = SlackTextCleaner.replace_special_catchall(slack_str)
    slack_str = SlackTextCleaner.add_zero_width_whitespace_after_tag(slack_str)
    return slack_str


def get_channel_from_id(client: WebClient, channel_id: str) -> dict[str, Any]:
    response = client.conversations_info(channel=channel_id)
    response.validate()
    return response["channel"]


def get_channel_name_from_id(
    client: WebClient, channel_id: str
) -> tuple[str | None, bool]:
    try:
        channel_info = get_channel_from_id(client, channel_id)
        name = channel_info.get("name")
        is_dm = any([channel_info.get("is_im"), channel_info.get("is_mpim")])
        return name, is_dm
    except SlackApiError as e:
        logger.exception(f"Couldn't fetch channel name from id: {channel_id}")
        raise e


def fetch_slack_user_ids_from_emails(
    user_emails: list[str], client: WebClient
) -> tuple[list[str], list[str]]:
    user_ids: list[str] = []
    failed_to_find: list[str] = []
    for email in user_emails:
        try:
            user = client.users_lookupByEmail(email=email)
            user_ids.append(user.data["user"]["id"])  # type: ignore
        except Exception:
            logger.error(f"Was not able to find slack user by email: {email}")
            failed_to_find.append(email)

    return user_ids, failed_to_find


def fetch_user_ids_from_groups(
    given_names: list[str], client: WebClient
) -> tuple[list[str], list[str]]:
    user_ids: list[str] = []
    failed_to_find: list[str] = []
    try:
        response = client.usergroups_list()
        if not isinstance(response.data, dict):
            logger.error("Error fetching user groups")
            return user_ids, given_names

        all_group_data = response.data.get("usergroups", [])
        name_id_map = {d["name"]: d["id"] for d in all_group_data}
        handle_id_map = {d["handle"]: d["id"] for d in all_group_data}
        for given_name in given_names:
            group_id = name_id_map.get(given_name) or handle_id_map.get(
                given_name.lstrip("@")
            )
            if not group_id:
                failed_to_find.append(given_name)
                continue
            try:
                response = client.usergroups_users_list(usergroup=group_id)
                if isinstance(response.data, dict):
                    user_ids.extend(response.data.get("users", []))
                else:
                    failed_to_find.append(given_name)
            except Exception as e:
                logger.error(f"Error fetching user group ids: {str(e)}")
                failed_to_find.append(given_name)
    except Exception as e:
        logger.error(f"Error fetching user groups: {str(e)}")
        failed_to_find = given_names

    return user_ids, failed_to_find


def fetch_group_ids_from_names(
    given_names: list[str], client: WebClient
) -> tuple[list[str], list[str]]:
    group_data: list[str] = []
    failed_to_find: list[str] = []

    try:
        response = client.usergroups_list()
        if not isinstance(response.data, dict):
            logger.error("Error fetching user groups")
            return group_data, given_names

        all_group_data = response.data.get("usergroups", [])

        name_id_map = {d["name"]: d["id"] for d in all_group_data}
        handle_id_map = {d["handle"]: d["id"] for d in all_group_data}

        for given_name in given_names:
            id = handle_id_map.get(given_name.lstrip("@"))
            id = id or name_id_map.get(given_name)
            if id:
                group_data.append(id)
            else:
                failed_to_find.append(given_name)
    except Exception as e:
        failed_to_find = given_names
        logger.error(f"Error fetching user groups: {str(e)}")

    return group_data, failed_to_find


def fetch_user_semantic_id_from_id(
    user_id: str | None, client: WebClient
) -> str | None:
    if not user_id:
        return None

    response = make_slack_api_rate_limited(client.users_info)(user=user_id)
    if not response["ok"]:
        return None

    user: dict = cast(dict[Any, dict], response.data).get("user", {})

    return (
        user.get("real_name")
        or user.get("name")
        or user.get("profile", {}).get("email")
    )


def read_slack_thread(
    channel: str, thread: str, client: WebClient
) -> list[ThreadMessage]:
    thread_messages: list[ThreadMessage] = []
    response = client.conversations_replies(channel=channel, ts=thread)
    replies = cast(dict, response.data).get("messages", [])
    for reply in replies:
        if "user" in reply and "bot_id" not in reply:
            message = reply["text"]
            user_sem_id = (
                fetch_user_semantic_id_from_id(reply.get("user"), client)
                or "Unknown User"
            )
            message_type = MessageType.USER
        else:
            self_slack_bot_id = get_onyx_bot_slack_bot_id(client)

            if reply.get("user") == self_slack_bot_id:
                # OnyxBot response
                message_type = MessageType.ASSISTANT
                user_sem_id = "Assistant"

                # OnyxBot responses have both text and blocks
                # The useful content is in the blocks, specifically the first block unless there are
                # auto-detected filters
                blocks = reply.get("blocks")
                if not blocks:
                    logger.warning(f"OnyxBot response has no blocks: {reply}")
                    continue

                message = blocks[0].get("text", {}).get("text")

                # If auto-detected filters are on, use the second block for the actual answer
                # The first block is the auto-detected filters
                if message.startswith("_Filters"):
                    if len(blocks) < 2:
                        logger.warning(f"Only filter blocks found: {reply}")
                        continue
                    # This is the OnyxBot answer format, if there is a change to how we respond,
                    # this will need to be updated to get the correct "answer" portion
                    message = reply["blocks"][1].get("text", {}).get("text")
            else:
                # Other bots are not counted as the LLM response which only comes from Onyx
                message_type = MessageType.USER
                bot_user_name = fetch_user_semantic_id_from_id(
                    reply.get("user"), client
                )
                user_sem_id = bot_user_name or "Unknown" + " Bot"

                # For other bots, just use the text as we have no way of knowing that the
                # useful portion is
                message = reply.get("text")
                if not message:
                    message = blocks[0].get("text", {}).get("text")

            if not message:
                logger.warning("Skipping Slack thread message, no text found")
                continue

        message = remove_onyx_bot_tag(message, client=client)
        thread_messages.append(
            ThreadMessage(message=message, sender=user_sem_id, role=message_type)
        )

    return thread_messages


def slack_usage_report(
    action: str, sender_id: str | None, client: WebClient, tenant_id: str | None
) -> None:
    if DISABLE_TELEMETRY:
        return

    onyx_user = None
    sender_email = None
    try:
        sender_email = client.users_info(user=sender_id).data["user"]["profile"]["email"]  # type: ignore
    except Exception:
        logger.warning("Unable to find sender email")

    if sender_email is not None:
        with get_session_with_tenant(tenant_id) as db_session:
            onyx_user = get_user_by_email(email=sender_email, db_session=db_session)

    optional_telemetry(
        record_type=RecordType.USAGE,
        data={"action": action},
        user_id=str(onyx_user.id) if onyx_user else "Non-Onyx-Or-No-Auth-User",
    )


class SlackRateLimiter:
    def __init__(self) -> None:
        self.max_qpm: int | None = DANSWER_BOT_MAX_QPM
        self.max_wait_time = DANSWER_BOT_MAX_WAIT_TIME
        self.active_question = 0
        self.last_reset_time = time.time()
        self.waiting_questions: list[int] = []

    def refill(self) -> None:
        # If elapsed time is greater than the period, reset the active question count
        if (time.time() - self.last_reset_time) > 60:
            self.active_question = 0
            self.last_reset_time = time.time()

    def notify(
        self, client: WebClient, channel: str, position: int, thread_ts: str | None
    ) -> None:
        respond_in_thread(
            client=client,
            channel=channel,
            receiver_ids=None,
            text=f"Your question has been queued. You are in position {position}.\n"
            f"Please wait a moment :hourglass_flowing_sand:",
            thread_ts=thread_ts,
        )

    def is_available(self) -> bool:
        if self.max_qpm is None:
            return True

        self.refill()
        return self.active_question < self.max_qpm

    def acquire_slot(self) -> None:
        self.active_question += 1

    def init_waiter(self) -> tuple[int, int]:
        func_randid = random.getrandbits(128)
        self.waiting_questions.append(func_randid)
        position = self.waiting_questions.index(func_randid) + 1

        return func_randid, position

    def waiter(self, func_randid: int) -> None:
        if self.max_qpm is None:
            return

        wait_time = 0
        while (
            self.active_question >= self.max_qpm
            or self.waiting_questions[0] != func_randid
        ):
            if wait_time > self.max_wait_time:
                raise TimeoutError
            time.sleep(2)
            wait_time += 2
            self.refill()

        del self.waiting_questions[0]


def get_feedback_visibility() -> FeedbackVisibility:
    try:
        return FeedbackVisibility(DANSWER_BOT_FEEDBACK_VISIBILITY.lower())
    except ValueError:
        return FeedbackVisibility.PRIVATE


class TenantSocketModeClient(SocketModeClient):
    def __init__(
        self, tenant_id: str | None, slack_bot_id: int, *args: Any, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        self.tenant_id = tenant_id
        self.slack_bot_id = slack_bot_id
