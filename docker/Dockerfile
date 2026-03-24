FROM supervisely/base-py-sdk-light:6.73.552

ARG version=6.73.552

# Supervisely
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    pip install --upgrade pip && \
    pip install --upgrade supervisely[versioning]==${version}

LABEL python_sdk_version=${version}
