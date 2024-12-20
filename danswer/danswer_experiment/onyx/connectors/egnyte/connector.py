# Standard Library
import io
import os
from collections.abc import Generator
from datetime import datetime, timezone
from logging import Logger
from typing import IO, Any, cast

# Third Party
import requests
from retry import retry

# First Party
from onyx.configs.app_configs import EGNYTE_BASE_DOMAIN, EGNYTE_CLIENT_ID, EGNYTE_CLIENT_SECRET, EGNYTE_LOCALHOST_OVERRIDE, INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.interfaces import GenerateDocumentsOutput, LoadConnector, OAuthConnector, PollConnector, SecondsSinceUnixEpoch
from onyx.connectors.models import BasicExpertInfo, ConnectorMissingCredentialError, Document, Section
from onyx.file_processing.extract_file_text import detect_encoding, extract_file_text, get_file_ext, is_text_file_extension, is_valid_file_ext, read_text_file
from onyx.utils.logger import setup_logger

logger = setup_logger()

_EGNYTE_API_BASE = "https://{domain}.egnyte.com/pubapi/v1"
_EGNYTE_APP_BASE = "https://{domain}.egnyte.com"
_TIMEOUT = 60


def _request_with_retries(
    method: str,
    url: str,
    data: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = _TIMEOUT,
    stream: bool = False,
    tries: int = 8,
    delay: float = 1,
    backoff: float = 2,
) -> requests.Response:
    @retry(tries=tries, delay=delay, backoff=backoff, logger=cast(Logger, logger))
    def _make_request() -> requests.Response:
        response = requests.request(
            method,
            url,
            data=data,
            headers=headers,
            params=params,
            timeout=timeout,
            stream=stream,
        )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code != 403:
                logger.exception(
                    f"Failed to call Egnyte API.\n"
                    f"URL: {url}\n"
                    f"Headers: {headers}\n"
                    f"Data: {data}\n"
                    f"Params: {params}"
                )
            raise e
        return response

    return _make_request()


def _parse_last_modified(last_modified: str) -> datetime:
    return datetime.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z").replace(
        tzinfo=timezone.utc
    )


def _process_egnyte_file(
    file_metadata: dict[str, Any],
    file_content: IO,
    base_url: str,
    folder_path: str | None = None,
) -> Document | None:
    """Process an Egnyte file into a Document object

    Args:
        file_data: The file data from Egnyte API
        file_content: The raw content of the file in bytes
        base_url: The base URL for the Egnyte instance
        folder_path: Optional folder path to filter results
    """
    # Skip if file path doesn't match folder path filter
    if folder_path and not file_metadata["path"].startswith(folder_path):
        raise ValueError(
            f"File path {file_metadata['path']} does not match folder path {folder_path}"
        )

    file_name = file_metadata["name"]
    extension = get_file_ext(file_name)
    if not is_valid_file_ext(extension):
        logger.warning(f"Skipping file '{file_name}' with extension '{extension}'")
        return None

    # Extract text content based on file type
    if is_text_file_extension(file_name):
        encoding = detect_encoding(file_content)
        file_content_raw, file_metadata = read_text_file(
            file_content, encoding=encoding, ignore_onyx_metadata=False
        )
    else:
        file_content_raw = extract_file_text(
            file=file_content,
            file_name=file_name,
            break_on_unprocessable=True,
        )

    # Build the web URL for the file
    web_url = f"{base_url}/navigate/file/{file_metadata['group_id']}"

    # Create document metadata
    metadata: dict[str, str | list[str]] = {
        "file_path": file_metadata["path"],
        "last_modified": file_metadata.get("last_modified", ""),
    }

    # Add lock info if present
    if lock_info := file_metadata.get("lock_info"):
        metadata[
            "lock_owner"
        ] = f"{lock_info.get('first_name', '')} {lock_info.get('last_name', '')}"

    # Create the document owners
    primary_owner = None
    if uploaded_by := file_metadata.get("uploaded_by"):
        primary_owner = BasicExpertInfo(
            email=uploaded_by,  # Using username as email since that's what we have
        )

    # Create the document
    return Document(
        id=f"egnyte-{file_metadata['entry_id']}",
        sections=[Section(text=file_content_raw.strip(), link=web_url)],
        source=DocumentSource.EGNYTE,
        semantic_identifier=file_name,
        metadata=metadata,
        doc_updated_at=(
            _parse_last_modified(file_metadata["last_modified"])
            if "last_modified" in file_metadata
            else None
        ),
        primary_owners=[primary_owner] if primary_owner else None,
    )


class EgnyteConnector(LoadConnector, PollConnector, OAuthConnector):
    def __init__(
        self,
        folder_path: str | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.domain = ""  # will always be set in `load_credentials`
        self.folder_path = folder_path or ""  # Root folder if not specified
        self.batch_size = batch_size
        self.access_token: str | None = None

    @classmethod
    def oauth_id(cls) -> DocumentSource:
        return DocumentSource.EGNYTE

    @classmethod
    def oauth_authorization_url(cls, base_domain: str, state: str) -> str:
        if not EGNYTE_CLIENT_ID:
            raise ValueError("EGNYTE_CLIENT_ID environment variable must be set")
        if not EGNYTE_BASE_DOMAIN:
            raise ValueError("EGNYTE_DOMAIN environment variable must be set")

        if EGNYTE_LOCALHOST_OVERRIDE:
            base_domain = EGNYTE_LOCALHOST_OVERRIDE

        callback_uri = f"{base_domain.strip('/')}/connector/oauth/callback/egnyte"
        return (
            f"https://{EGNYTE_BASE_DOMAIN}.egnyte.com/puboauth/token"
            f"?client_id={EGNYTE_CLIENT_ID}"
            f"&redirect_uri={callback_uri}"
            f"&scope=Egnyte.filesystem"
            f"&state={state}"
            f"&response_type=code"
        )

    @classmethod
    def oauth_code_to_token(cls, base_domain: str, code: str) -> dict[str, Any]:
        if not EGNYTE_CLIENT_ID:
            raise ValueError("EGNYTE_CLIENT_ID environment variable must be set")
        if not EGNYTE_CLIENT_SECRET:
            raise ValueError("EGNYTE_CLIENT_SECRET environment variable must be set")
        if not EGNYTE_BASE_DOMAIN:
            raise ValueError("EGNYTE_DOMAIN environment variable must be set")

        # Exchange code for token
        url = f"https://{EGNYTE_BASE_DOMAIN}.egnyte.com/puboauth/token"
        redirect_uri = f"{EGNYTE_LOCALHOST_OVERRIDE or base_domain}/connector/oauth/callback/egnyte"
        data = {
            "client_id": EGNYTE_CLIENT_ID,
            "client_secret": EGNYTE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": "Egnyte.filesystem",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = _request_with_retries(
            method="POST",
            url=url,
            data=data,
            headers=headers,
            # try a lot faster since this is a realtime flow
            backoff=0,
            delay=0.1,
        )
        if not response.ok:
            raise RuntimeError(f"Failed to exchange code for token: {response.text}")

        token_data = response.json()
        return {
            "domain": EGNYTE_BASE_DOMAIN,
            "access_token": token_data["access_token"],
        }

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.domain = credentials["domain"]
        self.access_token = credentials["access_token"]
        return None

    def _get_files_list(
        self,
        path: str,
    ) -> list[dict[str, Any]]:
        if not self.access_token or not self.domain:
            raise ConnectorMissingCredentialError("Egnyte")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        params: dict[str, Any] = {
            "list_content": True,
        }

        url = f"{_EGNYTE_API_BASE.format(domain=self.domain)}/fs/{path or ''}"
        response = _request_with_retries(
            method="GET", url=url, headers=headers, params=params, timeout=_TIMEOUT
        )
        if not response.ok:
            raise RuntimeError(f"Failed to fetch files from Egnyte: {response.text}")

        data = response.json()
        all_files: list[dict[str, Any]] = []

        # Add files from current directory
        all_files.extend(data.get("files", []))

        # Recursively traverse folders
        for item in data.get("folders", []):
            all_files.extend(self._get_files_list(item["path"]))

        return all_files

    def _filter_files(
        self,
        files: list[dict[str, Any]],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        filtered_files = []
        for file in files:
            if file["is_folder"]:
                continue

            file_modified = _parse_last_modified(file["last_modified"])
            if start_time and file_modified < start_time:
                continue
            if end_time and file_modified > end_time:
                continue

            filtered_files.append(file)

        return filtered_files

    def _process_files(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> Generator[list[Document], None, None]:
        files = self._get_files_list(self.folder_path)
        files = self._filter_files(files, start_time, end_time)

        current_batch: list[Document] = []
        for file in files:
            try:
                # Set up request with streaming enabled
                headers = {
                    "Authorization": f"Bearer {self.access_token}",
                }
                url = f"{_EGNYTE_API_BASE.format(domain=self.domain)}/fs-content/{file['path']}"
                response = _request_with_retries(
                    method="GET",
                    url=url,
                    headers=headers,
                    timeout=_TIMEOUT,
                    stream=True,
                )

                if not response.ok:
                    logger.error(
                        f"Failed to fetch file content: {file['path']} (status code: {response.status_code})"
                    )
                    continue

                # Stream the response content into a BytesIO buffer
                buffer = io.BytesIO()
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        buffer.write(chunk)

                # Reset buffer's position to the start
                buffer.seek(0)

                # Process the streamed file content
                doc = _process_egnyte_file(
                    file_metadata=file,
                    file_content=buffer,
                    base_url=_EGNYTE_APP_BASE.format(domain=self.domain),
                    folder_path=self.folder_path,
                )

                if doc is not None:
                    current_batch.append(doc)

                    if len(current_batch) >= self.batch_size:
                        yield current_batch
                        current_batch = []

            except Exception:
                logger.exception(f"Failed to process file {file['path']}")
                continue

        if current_batch:
            yield current_batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        yield from self._process_files()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_time = datetime.fromtimestamp(start, tz=timezone.utc)
        end_time = datetime.fromtimestamp(end, tz=timezone.utc)

        yield from self._process_files(start_time=start_time, end_time=end_time)


if __name__ == "__main__":
    connector = EgnyteConnector()
    connector.load_credentials(
        {
            "domain": os.environ["EGNYTE_DOMAIN"],
            "access_token": os.environ["EGNYTE_ACCESS_TOKEN"],
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
