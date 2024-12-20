# Third Party
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

# First Party
from onyx.auth.users import current_admin_user
from onyx.db.api_key import ApiKeyDescriptor, fetch_api_keys, insert_api_key, regenerate_api_key, remove_api_key, update_api_key
from onyx.db.engine import get_session
from onyx.db.models import User
from onyx.server.api_key.models import APIKeyArgs

router = APIRouter(prefix="/admin/api-key")


@router.get("")
def list_api_keys(
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[ApiKeyDescriptor]:
    return fetch_api_keys(db_session)


@router.post("")
def create_api_key(
    api_key_args: APIKeyArgs,
    user: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> ApiKeyDescriptor:
    return insert_api_key(db_session, api_key_args, user.id if user else None)


@router.post("/{api_key_id}/regenerate")
def regenerate_existing_api_key(
    api_key_id: int,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> ApiKeyDescriptor:
    return regenerate_api_key(db_session, api_key_id)


@router.patch("/{api_key_id}")
def update_existing_api_key(
    api_key_id: int,
    api_key_args: APIKeyArgs,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> ApiKeyDescriptor:
    return update_api_key(db_session, api_key_id, api_key_args)


@router.delete("/{api_key_id}")
def delete_api_key(
    api_key_id: int,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> None:
    remove_api_key(db_session, api_key_id)
