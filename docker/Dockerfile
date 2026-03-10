FROM supervisely/base-py-sdk:6.73.540

ARG version=6.73.540

# Supervisely
RUN pip install --upgrade pip && \
    pip install --upgrade --no-cache "supervisely[versioning] @ git+https://github.com/supervisely/supervisely.git@instant-access-project-versions"


LABEL python_sdk_version=${version}
