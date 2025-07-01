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
    print("🧪 Testing core imports...")
    
    try:
        from cliffracer import (
            CliffracerService, 
            ServiceConfig, 
            __version__,
            AuthConfig,
            SimpleAuthService,
            SecureRepository,
            CorrelationContext
        )
        print(f"✅ Core imports successful (version {__version__})")
        return True
    except Exception as e:
        print(f"❌ Core import failed: {e}")
        traceback.print_exc()
        return False

def test_service_creation():
    """Test that services can be created"""
    print("🧪 Testing service creation...")
    
    try:
        from cliffracer import CliffracerService, ServiceConfig
        
        config = ServiceConfig(
            name="test_service",
            nats_url="nats://localhost:4222"
        )
        service = CliffracerService(config)
        
        print(f"✅ Service created: {service.config.name}")
        return True
    except Exception as e:
        print(f"❌ Service creation failed: {e}")
        traceback.print_exc()
        return False

def test_auth_system():
    """Test that auth system works"""
    print("🧪 Testing auth system...")
    
    try:
        from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig
        
        config = AuthConfig(secret_key="test_key_" + "x" * 32)
        auth = SimpleAuthService(config)
        
        # Test creating a user
        user = auth.create_user("testuser", "test@example.com", "password123")
        print(f"✅ Auth user created: {user.username}")
        
        # Test authentication
        token = auth.authenticate("testuser", "password123")
        if token:
            print("✅ Authentication successful")
        else:
            print("❌ Authentication failed")
            return False
            
        # Test token validation
        context = auth.validate_token(token)
        if context and context.user:
            print(f"✅ Token validation successful: {context.user.username}")
        else:
            print("❌ Token validation failed")
            return False
            
        return True
    except Exception as e:
        print(f"❌ Auth system test failed: {e}")
        traceback.print_exc()
        return False

def test_correlation_system():
    """Test correlation ID system"""
    print("🧪 Testing correlation system...")
    
    try:
        from cliffracer.core.correlation import CorrelationContext
        
        # Test ID generation
        corr_id = CorrelationContext.get_or_create_id()
        print(f"✅ Correlation ID generated: {corr_id}")
        
        # Test context management
        from cliffracer import set_correlation_id, get_correlation_id
        set_correlation_id(corr_id)
        retrieved_id = get_correlation_id()
        
        if retrieved_id == corr_id:
            print("✅ Correlation context management working")
        else:
            print("❌ Correlation context management failed")
            return False
            
        return True
    except Exception as e:
        print(f"❌ Correlation system test failed: {e}")
        traceback.print_exc()
        return False

def test_database_models():
    """Test database model system"""
    print("🧪 Testing database models...")
    
    try:
        from cliffracer.database.models import DatabaseModel
        from pydantic import Field
        
        class TestModel(DatabaseModel):
            __tablename__ = "test_table"
            name: str = Field(..., description="Test name")
            value: int = Field(default=0, description="Test value")
        
        # Test model creation
        model = TestModel(name="test", value=42)
        print(f"✅ Model created: {model.name}")
        
        # Test table SQL generation
        sql = TestModel.get_create_table_sql()
        if "CREATE TABLE" in sql and "test_table" in sql:
            print("✅ SQL generation working")
        else:
            print("❌ SQL generation failed")
            return False
            
        return True
    except Exception as e:
        print(f"❌ Database model test failed: {e}")
        traceback.print_exc()
        return False

def test_validation_system():
    """Test input validation"""
    print("🧪 Testing validation system...")
    
    try:
        from cliffracer.core.validation import validate_port, validate_timeout
        
        # Test port validation
        port = validate_port(8080)
        if port == 8080:
            print("✅ Port validation working")
        else:
            print("❌ Port validation failed")
            return False
        
        # Test timeout validation
        timeout = validate_timeout(30.0)
        if timeout == 30.0:
            print("✅ Timeout validation working")
        else:
            print("❌ Timeout validation failed")
            return False
            
        return True
    except Exception as e:
        print(f"❌ Validation system test failed: {e}")
        traceback.print_exc()
        return False

def test_package_build():
    """Test that package can be built"""
    print("🧪 Testing package build...")
    
    try:
        import subprocess
        result = subprocess.run(
            ["uv", "build"], 
            capture_output=True, 
            text=True,
            cwd=Path(__file__).parent
        )
        
        if result.returncode == 0:
            print("✅ Package builds successfully")
            
            # Check that wheel was created
            dist_dir = Path(__file__).parent / "dist"
            wheels = list(dist_dir.glob("*.whl"))
            if wheels:
                print(f"✅ Wheel created: {wheels[0].name}")
            else:
                print("❌ No wheel file found")
                return False
                
            return True
        else:
            print(f"❌ Package build failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ Package build test failed: {e}")
        return False

def main():
    """Run all verification tests"""
    print("🚀 Cliffracer Package Verification")
    print("=" * 50)
    print()
    
    tests = [
        ("Core Imports", test_core_imports),
        ("Service Creation", test_service_creation), 
        ("Auth System", test_auth_system),
        ("Correlation System", test_correlation_system),
        ("Database Models", test_database_models),
        ("Validation System", test_validation_system),
        ("Package Build", test_package_build),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n📋 {test_name}")
        print("-" * 30)
        if test_func():
            passed += 1
        print()
    
    print("=" * 50)
    print(f"🏁 Verification Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! Cliffracer is ready for production use!")
        print()
        print("📦 Package can be installed in other projects with:")
        print("   pip install dist/cliffracer-*.whl")
        print("   # or")
        print("   pip install -e /path/to/cliffracer")
        return True
    else:
        print(f"❌ {total - passed} tests failed. Please fix issues before using.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)