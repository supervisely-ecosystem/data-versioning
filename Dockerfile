FROM supervisely/base-py-sdk:6.72.85

ARG version

# Supervisely
RUN pip install --upgrade supervisely==$version