"""Factory stub for running celery worker / celery beat.
This code is different from the primary/beat stubs because there is no EE version to
fetch. Port over the code in those files if we add an EE version of this worker."""
# Third Party
from celery import Celery

# First Party
from onyx.utils.variable_functionality import set_is_ee_based_on_env_variable

set_is_ee_based_on_env_variable()


def get_app() -> Celery:
    # First Party
    from onyx.background.celery.apps.heavy import celery_app

    return celery_app


app = get_app()
