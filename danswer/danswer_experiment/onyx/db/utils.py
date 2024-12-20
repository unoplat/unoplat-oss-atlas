# Standard Library
from typing import Any

# Third Party
from sqlalchemy import inspect

# First Party
from onyx.db.models import Base


def model_to_dict(model: Base) -> dict[str, Any]:
    return {c.key: getattr(model, c.key) for c in inspect(model).mapper.column_attrs}  # type: ignore
