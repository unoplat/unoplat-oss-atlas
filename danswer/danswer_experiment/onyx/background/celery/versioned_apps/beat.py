"""Factory stub for running celery worker / celery beat."""
# Third Party
from celery import Celery

# First Party
from onyx.background.celery.apps.beat import celery_app
from onyx.utils.variable_functionality import set_is_ee_based_on_env_variable

set_is_ee_based_on_env_variable()
app: Celery = celery_app
