FROM supervisely/base-py-sdk-light:6.73.540

ARG version=6.73.540

# Supervisely
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    pip install --upgrade pip && \
    pip install --upgrade --no-cache "supervisely[versioning] @ git+https://github.com/supervisely/supervisely.git@instant-access-project-versions"


LABEL python_sdk_version=${version}
