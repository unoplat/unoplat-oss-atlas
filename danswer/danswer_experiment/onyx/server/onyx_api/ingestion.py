# Third Party
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

# First Party
from onyx.auth.users import api_key_dep
from onyx.configs.constants import DocumentSource
from onyx.connectors.models import Document, IndexAttemptMetadata
from onyx.db.connector_credential_pair import get_connector_credential_pair_from_id
from onyx.db.document import get_documents_by_cc_pair, get_ingestion_documents
from onyx.db.engine import get_current_tenant_id, get_session
from onyx.db.models import User
from onyx.db.search_settings import get_current_search_settings, get_secondary_search_settings
from onyx.document_index.document_index_utils import get_both_index_names
from onyx.document_index.factory import get_default_document_index
from onyx.indexing.embedder import DefaultIndexingEmbedder
from onyx.indexing.indexing_pipeline import build_indexing_pipeline
from onyx.server.onyx_api.models import DocMinimalInfo, IngestionDocument, IngestionResult
from onyx.utils.logger import setup_logger

logger = setup_logger()

# not using /api to avoid confusion with nginx api path routing
router = APIRouter(prefix="/onyx-api")


@router.get("/connector-docs/{cc_pair_id}")
def get_docs_by_connector_credential_pair(
    cc_pair_id: int,
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
) -> list[DocMinimalInfo]:
    db_docs = get_documents_by_cc_pair(cc_pair_id=cc_pair_id, db_session=db_session)
    return [
        DocMinimalInfo(
            document_id=doc.id,
            semantic_id=doc.semantic_id,
            link=doc.link,
        )
        for doc in db_docs
    ]


@router.get("/ingestion")
def get_ingestion_docs(
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
) -> list[DocMinimalInfo]:
    db_docs = get_ingestion_documents(db_session)
    return [
        DocMinimalInfo(
            document_id=doc.id,
            semantic_id=doc.semantic_id,
            link=doc.link,
        )
        for doc in db_docs
    ]


@router.post("/ingestion")
def upsert_ingestion_doc(
    doc_info: IngestionDocument,
    _: User | None = Depends(api_key_dep),
    db_session: Session = Depends(get_session),
    tenant_id: str = Depends(get_current_tenant_id),
) -> IngestionResult:
    doc_info.document.from_ingestion_api = True

    document = Document.from_base(doc_info.document)

    # TODO once the frontend is updated with this enum, remove this logic
    if document.source == DocumentSource.INGESTION_API:
        document.source = DocumentSource.FILE

    cc_pair = get_connector_credential_pair_from_id(
        cc_pair_id=doc_info.cc_pair_id or 0, db_session=db_session
    )
    if cc_pair is None:
        raise HTTPException(
            status_code=400, detail="Connector-Credential Pair specified does not exist"
        )

    # Need to index for both the primary and secondary index if possible
    curr_ind_name, sec_ind_name = get_both_index_names(db_session)
    curr_doc_index = get_default_document_index(
        primary_index_name=curr_ind_name, secondary_index_name=None
    )

    search_settings = get_current_search_settings(db_session)

    index_embedding_model = DefaultIndexingEmbedder.from_db_search_settings(
        search_settings=search_settings
    )

    indexing_pipeline = build_indexing_pipeline(
        embedder=index_embedding_model,
        document_index=curr_doc_index,
        ignore_time_skip=True,
        db_session=db_session,
        tenant_id=tenant_id,
    )

    new_doc, __chunk_count = indexing_pipeline(
        document_batch=[document],
        index_attempt_metadata=IndexAttemptMetadata(
            connector_id=cc_pair.connector_id,
            credential_id=cc_pair.credential_id,
        ),
    )

    # If there's a secondary index being built, index the doc but don't use it for return here
    if sec_ind_name:
        sec_doc_index = get_default_document_index(
            primary_index_name=curr_ind_name, secondary_index_name=None
        )

        sec_search_settings = get_secondary_search_settings(db_session)

        if sec_search_settings is None:
            # Should not ever happen
            raise RuntimeError(
                "Secondary index exists but no search settings configured"
            )

        new_index_embedding_model = DefaultIndexingEmbedder.from_db_search_settings(
            search_settings=sec_search_settings
        )

        sec_ind_pipeline = build_indexing_pipeline(
            embedder=new_index_embedding_model,
            document_index=sec_doc_index,
            ignore_time_skip=True,
            db_session=db_session,
            tenant_id=tenant_id,
        )

        sec_ind_pipeline(
            document_batch=[document],
            index_attempt_metadata=IndexAttemptMetadata(
                connector_id=cc_pair.connector_id,
                credential_id=cc_pair.credential_id,
            ),
        )

    return IngestionResult(document_id=document.id, already_existed=not bool(new_doc))
