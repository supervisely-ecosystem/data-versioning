FROM supervisely/base-py-sdk:6.73.540

ARG version=6.73.540

# Supervisely
RUN pip install --upgrade pip
RUN pip install --upgrade supervisely==${version}
RUN pip install --upgrade supervisely[versioning]==${version}

LABEL python_sdk_version=${version}
