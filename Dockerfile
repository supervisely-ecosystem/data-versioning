FROM supervisely/base-py-sdk:6.73.531

ARG version=6.73.531

# Supervisely
RUN pip install --upgrade pip
RUN pip install --upgrade supervisely==${version}
RUN pip install --upgrade supervisely[versioning]==${version}

LABEL python_sdk_version=${version}
