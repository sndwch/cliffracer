#!/usr/bin/env python3
"""
Comprehensive package verification script.
This script verifies that Cliffracer can be properly installed and used.
"""

import sys
import traceback
from pathlib import Path


def test_core_imports():
    """Test that all core components can be imported"""
    print("[TEST] Testing core imports...")

    try:
        from cliffracer import (
            __version__,
        )

        print(f"[OK] Core imports successful (version {__version__})")
        return True
    except Exception as e:
        print(f"[ERROR] Core import failed: {e}")
        traceback.print_exc()
        return False


def test_service_creation():
    """Test that services can be created"""
    print("[TEST] Testing service creation...")

    try:
        from cliffracer import CliffracerService, ServiceConfig

        config = ServiceConfig(name="test_service", nats_url="nats://localhost:4222")
        service = CliffracerService(config)

        print(f"[OK] Service created: {service.config.name}")
        return True
    except Exception as e:
        print(f"[ERROR] Service creation failed: {e}")
        traceback.print_exc()
        return False


def test_auth_system():
    """Test that auth system works"""
    print("[TEST] Testing auth system...")

    try:
        from cliffracer.auth.simple_auth import AuthConfig, SimpleAuthService

        config = AuthConfig(secret_key="test_key_" + "x" * 32)
        auth = SimpleAuthService(config)

        # Test creating a user
        user = auth.create_user("testuser", "test@example.com", "password123")
        print(f"[OK] Auth user created: {user.username}")

        # Test authentication
        token = auth.authenticate("testuser", "password123")
        if token:
            print("[OK] Authentication successful")
        else:
            print("[ERROR] Authentication failed")
            return False

        # Test token validation
        context = auth.validate_token(token)
        if context and context.user:
            print(f"[OK] Token validation successful: {context.user.username}")
        else:
            print("[ERROR] Token validation failed")
            return False

        return True
    except Exception as e:
        print(f"[ERROR] Auth system test failed: {e}")
        traceback.print_exc()
        return False


def test_correlation_system():
    """Test correlation ID system"""
    print("[TEST] Testing correlation system...")

    try:
        from cliffracer.core.correlation import CorrelationContext

        # Test ID generation
        corr_id = CorrelationContext.get_or_create_id()
        print(f"[OK] Correlation ID generated: {corr_id}")

        # Test context management
        from cliffracer import get_correlation_id, set_correlation_id

        set_correlation_id(corr_id)
        retrieved_id = get_correlation_id()

        if retrieved_id == corr_id:
            print("[OK] Correlation context management working")
        else:
            print("[ERROR] Correlation context management failed")
            return False

        return True
    except Exception as e:
        print(f"[ERROR] Correlation system test failed: {e}")
        traceback.print_exc()
        return False


def test_validation_system():
    """Test input validation"""
    print("[TEST] Testing validation system...")

    try:
        from cliffracer.core.validation import validate_port, validate_timeout

        # Test port validation
        port = validate_port(8080)
        if port == 8080:
            print("[OK] Port validation working")
        else:
            print("[ERROR] Port validation failed")
            return False

        # Test timeout validation
        timeout = validate_timeout(30.0)
        if timeout == 30.0:
            print("[OK] Timeout validation working")
        else:
            print("[ERROR] Timeout validation failed")
            return False

        return True
    except Exception as e:
        print(f"[ERROR] Validation system test failed: {e}")
        traceback.print_exc()
        return False


def test_package_build():
    """Test that package can be built"""
    print("[TEST] Testing package build...")

    try:
        import subprocess

        result = subprocess.run(
            ["uv", "build"], capture_output=True, text=True, cwd=Path(__file__).parent
        )

        if result.returncode == 0:
            print("[OK] Package builds successfully")

            # Check that wheel was created
            dist_dir = Path(__file__).parent / "dist"
            wheels = list(dist_dir.glob("*.whl"))
            if wheels:
                print(f"[OK] Wheel created: {wheels[0].name}")
            else:
                print("[ERROR] No wheel file found")
                return False

            return True
        else:
            print(f"[ERROR] Package build failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[ERROR] Package build test failed: {e}")
        return False


def main():
    """Run all verification tests"""
    print("[INFO] Cliffracer Package Verification")
    print("=" * 50)
    print()

    tests = [
        ("Core Imports", test_core_imports),
        ("Service Creation", test_service_creation),
        ("Auth System", test_auth_system),
        ("Correlation System", test_correlation_system),
        ("Validation System", test_validation_system),
        ("Package Build", test_package_build),
    ]

    passed = 0
    total = len(tests)

    for test_name, test_func in tests:
        print(f"\n[INFO] {test_name}")
        print("-" * 30)
        if test_func():
            passed += 1
        print()

    print("=" * 50)
    print(f"[INFO] Verification Results: {passed}/{total} tests passed")

    if passed == total:
        print("[SUCCESS] All tests passed! Cliffracer is ready for production use!")
        print()
        print("[INFO] Package can be installed in other projects with:")
        print("   pip install dist/cliffracer-*.whl")
        print("   # or")
        print("   pip install -e /path/to/cliffracer")
        return True
    else:
        print(f"[ERROR] {total - passed} tests failed. Please fix issues before using.")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
