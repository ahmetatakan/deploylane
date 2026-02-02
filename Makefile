# =====================================
# ⚙️  OPM - Odoo Plugin Manager (CLI)
# =====================================

# ----------------------
# 🔧 Configurable values
# ----------------------

ifneq (,$(wildcard .env))
    include .env
    export $(shell sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' .env)
endif

PYTHON := python3
PACKAGE := deploylane
DIST_DIR := dist

# PyPI settings
PYPI_REPO := https://upload.pypi.org/legacy/
TEST_REPO := https://test.pypi.org/legacy/

# Version extraction from pyproject.toml
VERSION := $(shell sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml | head -n1)

# ----------------------
# 🎯 Default target
# ----------------------
help:
	@echo ""
	@echo "🧩 OPM Build Commands:"
	@echo "  make build        → Build wheel and sdist"
	@echo "  make clean        → Remove temporary build files"
	@echo "  make version      → Show current version"
	@echo "  make publish      → Upload to PyPI"
	@echo "  make testpublish  → Upload to TestPyPI"
	@echo "  make bump         → Bump patch version (auto-increment)"
	@echo "  make release      → Bump version, build, and publish to PyPI"
	@echo ""

# ----------------------
# 🧹 Clean (remove build artifacts)
# ----------------------
clean:
	@echo "🧹 Cleaning build artifacts..."
	rm -rf $(DIST_DIR) build *.egg-info
	@echo "✅ Clean complete."

# ----------------------
# 🔢 Bump version (auto-increment patch)
# ----------------------
bump:
	@echo "🔢 Bumping patch version..."
	@old=$$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -n1); \
	if [ -z "$$old" ]; then echo "❌ Version not found in pyproject.toml"; exit 1; fi; \
	new=$$(python3 -c "parts='$$old'.split('.'); parts[-1]=str(int(parts[-1])+1); print('.'.join(parts))"); \
	if [ "$$(uname)" = "Darwin" ]; then \
		sed -i '' -E "s/^([[:space:]]*version[[:space:]]*=[[:space:]]*)\"$$old\"/\1\"$$new\"/" pyproject.toml; \
	else \
		sed -i -E "s/^([[:space:]]*version[[:space:]]*=[[:space:]]*)\"$$old\"/\1\"$$new\"/" pyproject.toml; \
	fi; \
	echo "✅ New version: $$new"

# ----------------------
# 🏗️ Build (create wheel and sdist)
# ----------------------
build: clean
	@echo "🏗️  Building $(PACKAGE) (version: $(VERSION))..."
	PIP_CONFIG_FILE=/dev/null $(PYTHON) -m build
	@echo "✅ Build complete. Artifacts in $(DIST_DIR)/"

# ----------------------
# 🚀 Publish (PyPI)
# ----------------------
publish: build
	@echo "🚀 Publishing $(PACKAGE) $(VERSION) to PyPI..."
	$(PYTHON) -m twine upload --repository-url $(PYPI_REPO) $(DIST_DIR)/*
	@echo "✅ Published: https://pypi.org/project/$(PACKAGE)/$(VERSION)/"

# ----------------------
# 🧪 Test Publish (TestPyPI)
# ----------------------
testpublish:
	@echo "🧪 Publishing $(PACKAGE) $(VERSION) to TestPyPI..."
	$(PYTHON) -m twine upload --repository-url $(TEST_REPO) $(DIST_DIR)/*
	@echo "✅ TestPyPI upload complete: https://test.pypi.org/project/$(PACKAGE)/$(VERSION)/"

# ----------------------
# 🔢 Version
# ----------------------
version:
	@echo "📦 Current version: $(VERSION)"

release: bump publish