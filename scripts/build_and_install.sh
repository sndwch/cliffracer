#!/bin/bash
set -e

# Cliffracer Package Build and Install Script
# This script builds the Cliffracer package and helps with installation

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$PROJECT_ROOT/dist"

echo "[INFO] Cliffracer Package Builder"
echo "=============================="

# Function to clean old builds
clean_build() {
    echo "[CLEAN] Cleaning old builds..."
    rm -rf "$DIST_DIR"
    rm -rf "$PROJECT_ROOT/build"
    echo "[OK] Cleaned build artifacts"
}

# Function to build package
build_package() {
    echo "[BUILD] Building Cliffracer packages..."
    cd "$PROJECT_ROOT"
    
    # Build with uv (preferred) or fallback to pip
    if command -v uv &> /dev/null; then
        echo "Using uv to build package..."
        uv build --all-packages
    elif command -v python &> /dev/null; then
        echo "Using python build to build package..."
        python -m build
    else
        echo "[ERROR] Error: Neither uv nor python build found"
        exit 1
    fi
    
    echo "[OK] Package built successfully"
    ls -la "$DIST_DIR"
}

# Function to test package
test_package() {
    echo "[TEST] Testing package import..."
    
    # Test that the package can be imported
    if command -v uv &> /dev/null; then
        uv run python -c "
from cliffracer import CliffracerService, __version__
from cliffracer_auth.simple_auth import SimpleAuthService, AuthConfig
print(f'[OK] Package version {__version__} imports successfully')
"
    else
        python3 -c "
from cliffracer import CliffracerService, __version__
from cliffracer_auth.simple_auth import SimpleAuthService, AuthConfig
print(f'[OK] Package version {__version__} imports successfully')
"
    fi
    echo "[OK] Package test passed"
}

# Function to show installation instructions
show_install_instructions() {
    echo ""
    echo "[INFO] Installation Instructions"
    echo "============================"
    echo ""
    echo "For other projects to use this package:"
    echo ""
    echo "Option 1: Install built wheel"
    echo "  pip install $DIST_DIR/cliffracer-*.whl"
    echo "  # or with uv:"
    echo "  uv add $DIST_DIR/cliffracer-*.whl"
    echo ""
    echo "Option 2: Editable development install"
    echo "  pip install -e $PROJECT_ROOT"
    echo "  # or with uv:"
    echo "  uv add --editable $PROJECT_ROOT"
    echo ""
    echo "Option 3: Add to pyproject.toml"
    echo "  [project]"
    echo "  dependencies = ["
    echo "    \"cliffracer @ file://$PROJECT_ROOT\","
    echo "  ]"
    echo ""
}

# Function to create example consumer project
create_example() {
    local example_dir="$PROJECT_ROOT/example_consumer"
    
    echo "[INFO] Creating example consumer project..."
    mkdir -p "$example_dir"
    
    cat > "$example_dir/pyproject.toml" << EOF
[project]
name = "cliffracer-consumer-example"
version = "0.1.0"
description = "Example project consuming Cliffracer"
requires-python = ">=3.11"

dependencies = [
    "cliffracer @ file://$PROJECT_ROOT",
]
EOF

    cat > "$example_dir/simple_service.py" << 'EOF'
#!/usr/bin/env python3
"""Simple example service using Cliffracer"""

from cliffracer import CliffracerService, rpc

class ExampleService(CliffracerService):
    def __init__(self):
        super().__init__(
            name="example_service",
            nats_url="nats://localhost:4222"
        )

    @rpc
    async def hello(self, name: str = "World") -> str:
        return f"Hello, {name}!"

    @rpc  
    async def add(self, a: int, b: int) -> int:
        return a + b

if __name__ == "__main__":
    service = ExampleService()
    print("[INFO] Starting example service...")
    print("[TIP] Try: await call_rpc('example_service', 'hello', name='Alice')")
    service.run()
EOF

    cat > "$example_dir/README.md" << EOF
# Cliffracer Consumer Example

This is an example project that consumes the Cliffracer framework.

## Setup

\`\`\`bash
# Install dependencies (includes Cliffracer)
uv sync

# Run the service
python simple_service.py
\`\`\`

## Usage

The service provides two RPC methods:
- \`hello(name)\` - Returns a greeting
- \`add(a, b)\` - Returns the sum of two numbers

Test with another Cliffracer service or client.
EOF

    echo "[OK] Created example consumer project at: $example_dir"
}

# Main script logic
main() {
    case "${1:-build}" in
        "clean")
            clean_build
            ;;
        "build")
            clean_build
            build_package
            test_package
            show_install_instructions
            ;;
        "test")
            test_package
            ;;
        "example")
            create_example
            ;;
        "all")
            clean_build
            build_package
            test_package
            create_example
            show_install_instructions
            ;;
        "help"|"-h"|"--help")
            echo "Usage: $0 [command]"
            echo ""
            echo "Commands:"
            echo "  build     Build the package (default)"
            echo "  clean     Clean build artifacts"
            echo "  test      Test package import"
            echo "  example   Create example consumer project"
            echo "  all       Do everything (build + test + example)"
            echo "  help      Show this help"
            ;;
        *)
            echo "[ERROR] Unknown command: $1"
            echo "Use '$0 help' for usage information"
            exit 1
            ;;
    esac
}

# Run main function
main "$@"