# Standard Library
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import cast

# Third Party
from sqlalchemy.orm import Session

# First Party
from onyx.chat.models import PromptConfig, SectionRelevancePiece
from onyx.chat.prune_and_merge import ChunkRange, _merge_sections, merge_chunk_intervals
from onyx.configs.chat_configs import DISABLE_LLM_DOC_RELEVANCE
from onyx.context.search.enums import LLMEvaluationType, QueryFlow, SearchType
from onyx.context.search.models import IndexFilters, InferenceChunk, InferenceSection, RerankMetricsContainer, RetrievalMetricsContainer, SearchQuery, SearchRequest
from onyx.context.search.postprocessing.postprocessing import cleanup_chunks, search_postprocessing
from onyx.context.search.preprocessing.preprocessing import retrieval_preprocessing
from onyx.context.search.retrieval.search_runner import retrieve_chunks
from onyx.context.search.utils import inference_section_from_chunks, relevant_sections_to_indices
from onyx.db.models import User
from onyx.db.search_settings import get_current_search_settings
from onyx.document_index.factory import get_default_document_index
from onyx.document_index.interfaces import VespaChunkRequest
from onyx.llm.interfaces import LLM
from onyx.secondary_llm_flows.agentic_evaluation import evaluate_inference_section
from onyx.utils.logger import setup_logger
from onyx.utils.threadpool_concurrency import FunctionCall, run_functions_in_parallel
from onyx.utils.timing import log_function_time

logger = setup_logger()


class SearchPipeline:
    def __init__(
        self,
        search_request: SearchRequest,
        user: User | None,
        llm: LLM,
        fast_llm: LLM,
        db_session: Session,
        bypass_acl: bool = False,  # NOTE: VERY DANGEROUS, USE WITH CAUTION
        retrieval_metrics_callback: (
            Callable[[RetrievalMetricsContainer], None] | None
        ) = None,
        rerank_metrics_callback: Callable[[RerankMetricsContainer], None] | None = None,
        prompt_config: PromptConfig | None = None,
    ):
        self.search_request = search_request
        self.user = user
        self.llm = llm
        self.fast_llm = fast_llm
        self.db_session = db_session
        self.bypass_acl = bypass_acl
        self.retrieval_metrics_callback = retrieval_metrics_callback
        self.rerank_metrics_callback = rerank_metrics_callback

        self.search_settings = get_current_search_settings(db_session)
        self.document_index = get_default_document_index(
            primary_index_name=self.search_settings.index_name,
            secondary_index_name=None,
        )
        self.prompt_config: PromptConfig | None = prompt_config

        # Preprocessing steps generate this
        self._search_query: SearchQuery | None = None
        self._predicted_search_type: SearchType | None = None

        # Initial document index retrieval chunks
        self._retrieved_chunks: list[InferenceChunk] | None = None
        # Another call made to the document index to get surrounding sections
        self._retrieved_sections: list[InferenceSection] | None = None
        # Reranking and LLM section selection can be run together
        # If only LLM selection is on, the reranked chunks are yielded immediatly
        self._reranked_sections: list[InferenceSection] | None = None
        self._final_context_sections: list[InferenceSection] | None = None

        self._section_relevance: list[SectionRelevancePiece] | None = None

        # Generates reranked chunks and LLM selections
        self._postprocessing_generator: (
            Iterator[list[InferenceSection] | list[SectionRelevancePiece]] | None
        ) = None

        # No longer computed but keeping around in case it's reintroduced later
        self._predicted_flow: QueryFlow | None = QueryFlow.QUESTION_ANSWER

    """Pre-processing"""

    def _run_preprocessing(self) -> None:
        final_search_query = retrieval_preprocessing(
            search_request=self.search_request,
            user=self.user,
            llm=self.llm,
            db_session=self.db_session,
            bypass_acl=self.bypass_acl,
        )
        self._search_query = final_search_query
        self._predicted_search_type = final_search_query.search_type

    @property
    def search_query(self) -> SearchQuery:
        if self._search_query is not None:
            return self._search_query

        self._run_preprocessing()

        return cast(SearchQuery, self._search_query)

    @property
    def predicted_search_type(self) -> SearchType:
        if self._predicted_search_type is not None:
            return self._predicted_search_type

        self._run_preprocessing()
        return cast(SearchType, self._predicted_search_type)

    @property
    def predicted_flow(self) -> QueryFlow:
        if self._predicted_flow is not None:
            return self._predicted_flow

        self._run_preprocessing()
        return cast(QueryFlow, self._predicted_flow)

    """Retrieval and Postprocessing"""

    def _get_chunks(self) -> list[InferenceChunk]:
        if self._retrieved_chunks is not None:
            return self._retrieved_chunks

        # These chunks do not include large chunks and have been deduped
        self._retrieved_chunks = retrieve_chunks(
            query=self.search_query,
            document_index=self.document_index,
            db_session=self.db_session,
            retrieval_metrics_callback=self.retrieval_metrics_callback,
        )

        return cast(list[InferenceChunk], self._retrieved_chunks)

    @log_function_time(print_only=True)
    def _get_sections(self) -> list[InferenceSection]:
        """Returns an expanded section from each of the chunks.
        If whole docs (instead of above/below context) is specified then it will give back all of the whole docs
        that have a corresponding chunk.

        This step should be fast for any document index implementation.
        """
        if self._retrieved_sections is not None:
            return self._retrieved_sections

        # These chunks are ordered, deduped, and contain no large chunks
        retrieved_chunks = self._get_chunks()

        above = self.search_query.chunks_above
        below = self.search_query.chunks_below

        expanded_inference_sections = []
        inference_chunks: list[InferenceChunk] = []
        chunk_requests: list[VespaChunkRequest] = []

        # Full doc setting takes priority
        if self.search_query.full_doc:
            seen_document_ids = set()

            # This preserves the ordering since the chunks are retrieved in score order
            for chunk in retrieved_chunks:
                if chunk.document_id not in seen_document_ids:
                    seen_document_ids.add(chunk.document_id)
                    chunk_requests.append(
                        VespaChunkRequest(
                            document_id=chunk.document_id,
                        )
                    )

            inference_chunks.extend(
                cleanup_chunks(
                    self.document_index.id_based_retrieval(
                        chunk_requests=chunk_requests,
                        filters=IndexFilters(access_control_list=None),
                    )
                )
            )

            # Create a dictionary to group chunks by document_id
            grouped_inference_chunks: dict[str, list[InferenceChunk]] = {}
            for chunk in inference_chunks:
                if chunk.document_id not in grouped_inference_chunks:
                    grouped_inference_chunks[chunk.document_id] = []
                grouped_inference_chunks[chunk.document_id].append(chunk)

            for chunk_group in grouped_inference_chunks.values():
                inference_section = inference_section_from_chunks(
                    center_chunk=chunk_group[0],
                    chunks=chunk_group,
                )

                if inference_section is not None:
                    expanded_inference_sections.append(inference_section)
                else:
                    logger.warning(
                        "Skipped creation of section for full docs, no chunks found"
                    )

            self._retrieved_sections = expanded_inference_sections
            return expanded_inference_sections

        # General flow:
        # - Combine chunks into lists by document_id
        # - For each document, run merge-intervals to get combined ranges
        #   - This allows for less queries to the document index
        # - Fetch all of the new chunks with contents for the combined ranges
        # - Reiterate the chunks again and map to the results above based on the chunk.
        #   This maintains the original chunks ordering. Note, we cannot simply sort by score here
        #   as reranking flow may wipe the scores for a lot of the chunks.
        doc_chunk_ranges_map = defaultdict(list)
        for chunk in retrieved_chunks:
            # The list of ranges for each document is ordered by score
            doc_chunk_ranges_map[chunk.document_id].append(
                ChunkRange(
                    chunks=[chunk],
                    start=max(0, chunk.chunk_id - above),
                    # No max known ahead of time, filter will handle this anyway
                    end=chunk.chunk_id + below,
                )
            )

        # List of ranges, outside list represents documents, inner list represents ranges
        merged_ranges = [
            merge_chunk_intervals(ranges) for ranges in doc_chunk_ranges_map.values()
        ]

        flat_ranges: list[ChunkRange] = [r for ranges in merged_ranges for r in ranges]

        for chunk_range in flat_ranges:
            # Don't need to fetch chunks within range for merging if chunk_above / below are 0.
            if above == below == 0:
                inference_chunks.extend(chunk_range.chunks)

            else:
                chunk_requests.append(
                    VespaChunkRequest(
                        document_id=chunk_range.chunks[0].document_id,
                        min_chunk_ind=chunk_range.start,
                        max_chunk_ind=chunk_range.end,
                    )
                )

        if chunk_requests:
            inference_chunks.extend(
                cleanup_chunks(
                    self.document_index.id_based_retrieval(
                        chunk_requests=chunk_requests,
                        filters=IndexFilters(access_control_list=None),
                        batch_retrieval=True,
                    )
                )
            )

        doc_chunk_ind_to_chunk = {
            (chunk.document_id, chunk.chunk_id): chunk for chunk in inference_chunks
        }

        # In case of failed parallel calls to Vespa, at least we should have the initial retrieved chunks
        doc_chunk_ind_to_chunk.update(
            {(chunk.document_id, chunk.chunk_id): chunk for chunk in retrieved_chunks}
        )

        # Build the surroundings for all of the initial retrieved chunks
        for chunk in retrieved_chunks:
            start_ind = max(0, chunk.chunk_id - above)
            end_ind = chunk.chunk_id + below

            # Since the index of the max_chunk is unknown, just allow it to be None and filter after
            surrounding_chunks_or_none = [
                doc_chunk_ind_to_chunk.get((chunk.document_id, chunk_ind))
                for chunk_ind in range(start_ind, end_ind + 1)  # end_ind is inclusive
            ]
            # The None will apply to the would be "chunks" that are larger than the index of the last chunk
            # of the document
            surrounding_chunks = [
                chunk for chunk in surrounding_chunks_or_none if chunk is not None
            ]

            inference_section = inference_section_from_chunks(
                center_chunk=chunk,
                chunks=surrounding_chunks,
            )
            if inference_section is not None:
                expanded_inference_sections.append(inference_section)
            else:
                logger.warning("Skipped creation of section, no chunks found")

        self._retrieved_sections = expanded_inference_sections
        return expanded_inference_sections

    @property
    def reranked_sections(self) -> list[InferenceSection]:
        """Reranking is always done at the chunk level since section merging could create arbitrarily
        long sections which could be:
        1. Longer than the maximum context limit of even large rerankers
        2. Slow to calculate due to the quadratic scaling laws of Transformers

        See implementation in search_postprocessing for details
        """
        if self._reranked_sections is not None:
            return self._reranked_sections

        self._postprocessing_generator = search_postprocessing(
            search_query=self.search_query,
            retrieved_sections=self._get_sections(),
            llm=self.fast_llm,
            rerank_metrics_callback=self.rerank_metrics_callback,
        )

        self._reranked_sections = cast(
            list[InferenceSection], next(self._postprocessing_generator)
        )

        return self._reranked_sections

    @property
    def final_context_sections(self) -> list[InferenceSection]:
        if self._final_context_sections is not None:
            return self._final_context_sections

        self._final_context_sections = _merge_sections(sections=self.reranked_sections)
        return self._final_context_sections

    @property
    def section_relevance(self) -> list[SectionRelevancePiece] | None:
        if self._section_relevance is not None:
            return self._section_relevance

        if (
            self.search_query.evaluation_type == LLMEvaluationType.SKIP
            or DISABLE_LLM_DOC_RELEVANCE
        ):
            return None

        if self.search_query.evaluation_type == LLMEvaluationType.UNSPECIFIED:
            raise ValueError(
                "Attempted to access section relevance scores on search query with evaluation type `UNSPECIFIED`."
                + "The search query evaluation type should have been specified."
            )

        if self.search_query.evaluation_type == LLMEvaluationType.AGENTIC:
            sections = self.final_context_sections
            functions = [
                FunctionCall(
                    evaluate_inference_section,
                    (section, self.search_query.query, self.llm),
                )
                for section in sections
            ]
            try:
                results = run_functions_in_parallel(function_calls=functions)
                self._section_relevance = list(results.values())
            except Exception as e:
                raise ValueError(
                    "An issue occured during the agentic evaluation process."
                ) from e

        elif self.search_query.evaluation_type == LLMEvaluationType.BASIC:
            if DISABLE_LLM_DOC_RELEVANCE:
                raise ValueError(
                    "Basic search evaluation operation called while DISABLE_LLM_DOC_RELEVANCE is enabled."
                )
            self._section_relevance = next(
                cast(
                    Iterator[list[SectionRelevancePiece]],
                    self._postprocessing_generator,
                )
            )

        else:
            # All other cases should have been handled above
            raise ValueError(
                f"Unexpected evaluation type: {self.search_query.evaluation_type}"
            )

        return self._section_relevance

    @property
    def section_relevance_list(self) -> list[bool]:
        llm_indices = relevant_sections_to_indices(
            relevance_sections=self.section_relevance,
            items=self.final_context_sections,
        )
        return [ind in llm_indices for ind in range(len(self.final_context_sections))]
