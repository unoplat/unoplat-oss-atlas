"""Factory stub for running celery worker / celery beat."""
# Third Party
from celery import Celery

# First Party
from onyx.utils.variable_functionality import fetch_versioned_implementation, set_is_ee_based_on_env_variable

set_is_ee_based_on_env_variable()
app: Celery = fetch_versioned_implementation(
    "onyx.background.celery.apps.primary", "celery_app"
)
