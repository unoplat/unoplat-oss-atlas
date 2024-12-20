# Third Party
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_random_exponential

# First Party
from onyx.document_index.interfaces import DocumentIndex, VespaDocumentFields


class RetryDocumentIndex:
    """A wrapper class to help with specific retries against Vespa involving
    read timeouts.

    wait_random_exponential implements full jitter as per this article:
    https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/"""

    MAX_WAIT = 30

    # STOP_AFTER + MAX_WAIT should be slightly less (5?) than the celery soft_time_limit
    STOP_AFTER = 70

    def __init__(self, index: DocumentIndex):
        self.index: DocumentIndex = index

    @retry(
        retry=retry_if_exception_type(httpx.ReadTimeout),
        wait=wait_random_exponential(multiplier=1, max=MAX_WAIT),
        stop=stop_after_delay(STOP_AFTER),
    )
    def delete_single(self, doc_id: str) -> int:
        return self.index.delete_single(doc_id)

    @retry(
        retry=retry_if_exception_type(httpx.ReadTimeout),
        wait=wait_random_exponential(multiplier=1, max=MAX_WAIT),
        stop=stop_after_delay(STOP_AFTER),
    )
    def update_single(self, doc_id: str, fields: VespaDocumentFields) -> int:
        return self.index.update_single(doc_id, fields)
