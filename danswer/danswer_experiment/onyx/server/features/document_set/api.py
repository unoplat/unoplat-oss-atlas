# Third Party
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

# First Party
from onyx.auth.users import current_curator_or_admin_user, current_user
from onyx.db.document_set import check_document_sets_are_public, fetch_all_document_sets_for_user, insert_document_set, mark_document_set_as_to_be_deleted, update_document_set
from onyx.db.engine import get_session
from onyx.db.models import User
from onyx.server.features.document_set.models import CheckDocSetPublicRequest, CheckDocSetPublicResponse, DocumentSet, DocumentSetCreationRequest, DocumentSetUpdateRequest
from onyx.utils.variable_functionality import fetch_ee_implementation_or_noop

router = APIRouter(prefix="/manage")


@router.post("/admin/document-set")
def create_document_set(
    document_set_creation_request: DocumentSetCreationRequest,
    user: User = Depends(current_curator_or_admin_user),
    db_session: Session = Depends(get_session),
) -> int:
    fetch_ee_implementation_or_noop(
        "onyx.db.user_group", "validate_user_creation_permissions", None
    )(
        db_session=db_session,
        user=user,
        target_group_ids=document_set_creation_request.groups,
        object_is_public=document_set_creation_request.is_public,
    )
    try:
        document_set_db_model, _ = insert_document_set(
            document_set_creation_request=document_set_creation_request,
            user_id=user.id if user else None,
            db_session=db_session,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return document_set_db_model.id


@router.patch("/admin/document-set")
def patch_document_set(
    document_set_update_request: DocumentSetUpdateRequest,
    user: User = Depends(current_curator_or_admin_user),
    db_session: Session = Depends(get_session),
) -> None:
    fetch_ee_implementation_or_noop(
        "onyx.db.user_group", "validate_user_creation_permissions", None
    )(
        db_session=db_session,
        user=user,
        target_group_ids=document_set_update_request.groups,
        object_is_public=document_set_update_request.is_public,
    )
    try:
        update_document_set(
            document_set_update_request=document_set_update_request,
            db_session=db_session,
            user=user,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/document-set/{document_set_id}")
def delete_document_set(
    document_set_id: int,
    user: User = Depends(current_curator_or_admin_user),
    db_session: Session = Depends(get_session),
) -> None:
    try:
        mark_document_set_as_to_be_deleted(
            db_session=db_session,
            document_set_id=document_set_id,
            user=user,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


"""Endpoints for non-admins"""


@router.get("/document-set")
def list_document_sets_for_user(
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
    get_editable: bool = Query(
        False, description="If true, return editable document sets"
    ),
) -> list[DocumentSet]:
    return [
        DocumentSet.from_model(ds)
        for ds in fetch_all_document_sets_for_user(
            db_session=db_session, user=user, get_editable=get_editable
        )
    ]


@router.get("/document-set-public")
def document_set_public(
    check_public_request: CheckDocSetPublicRequest,
    _: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> CheckDocSetPublicResponse:
    is_public = check_document_sets_are_public(
        document_set_ids=check_public_request.document_set_ids, db_session=db_session
    )
    return CheckDocSetPublicResponse(is_public=is_public)
