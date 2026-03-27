#!/bin/bash
set -e


# Determine script and project root directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(realpath "$SCRIPT_DIR/..")"

# Download Dockerfile template to script directory
curl -sSL "https://raw.githubusercontent.com/supervisely-ecosystem/workflows/master/docker/hardened/Dockerfile.tmpl" -o "$SCRIPT_DIR/Dockerfile.template"


# Set SDK version
SDK_VER=6.73.552


# Always use project root as build context, and correct relative paths
docker build \
	--no-cache \
	-f "$SCRIPT_DIR/Dockerfile.template" \
	--build-arg tag_ref_name=$SDK_VER \
	--build-arg RUNTIME_BASE_IMAGE=base-py-sdk-hardened \
	--build-arg REQUIREMENTS_FILE=dev_requirements.txt \
	--label python_sdk_version=$SDK_VER \
	-t supervisely/data-versioning:test \
	"$PROJECT_ROOT"

# Ask for confirmation before pushing
read -p "Push image supervisely/data-versioning:test to registry? [y/N]: " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
	docker push supervisely/data-versioning:test
else
	echo "Push cancelled."
fi
