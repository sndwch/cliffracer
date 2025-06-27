#!/usr/bin/env python3
"""
Class name refactoring script for NATS microservices framework.
This script performs automated renaming of classes across the codebase.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List

# Define the refactoring mappings
CLASS_MAPPINGS = {
    # Phase 1: Core service hierarchy
    "NatsService": "BaseNATSService",
    "Service": "NATSService", 
    "ExtendedService": "ValidatedNATSService",
    "HTTPService": "HTTPNATSService",
    "WebSocketService": "WebSocketNATSService",
    "ModularService": "ConfigurableNATSService",
    "FullyModularService": "PluggableNATSService",
    "AuthenticatedService": "SecureNATSService",
    
    # Phase 2: Messaging and infrastructure
    "NATSMessagingClient": "NATSClient",
    "AWSMessagingClient": "AWSClient",
    "AbstractMessagingClient": "MessageClient",
    "AbstractMessageBroker": "MessageBroker", 
    "MessagingFactory": "MessageClientFactory",
    "MetricsExporterService": "ZabbixMetricsService",
    "AbstractMonitoringClient": "MonitoringClient",
    "CloudWatchMonitoringClient": "CloudWatchClient",
    "ZabbixSender": "ZabbixExporter",
    "MetricsCollector": "SystemMetricsCollector",
    
    # Phase 3: Configuration and support
    "ServiceMeta": "NATSServiceMeta",
    "ExtendedServiceMeta": "ValidatedServiceMeta",
    "AbstractServiceRunner": "ServiceRunner",
    "LambdaServiceRunner": "AWSLambdaRunner",
    "MultiServiceRunner": "ServiceOrchestrator",
}

# Files to exclude from refactoring
EXCLUDE_FILES = {
    "refactor_class_names.py",
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
}

def find_python_files(directory: Path) -> List[Path]:
    """Find all Python files in the directory."""
    python_files = []
    for root, dirs, files in os.walk(directory):
        # Remove excluded directories
        dirs[:] = [d for d in dirs if d not in EXCLUDE_FILES]
        
        for file in files:
            if file.endswith('.py') and file not in EXCLUDE_FILES:
                python_files.append(Path(root) / file)
    
    return python_files

def refactor_file(file_path: Path, mappings: Dict[str, str], dry_run: bool = True) -> Dict[str, int]:
    """Refactor a single file with the given class name mappings."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        changes = {}
        
        # Apply each mapping
        for old_name, new_name in mappings.items():
            # Pattern to match class definitions, imports, and usage
            patterns = [
                # Class definitions: class OldName(...)
                rf'\bclass\s+{re.escape(old_name)}\b',
                # Imports: from module import OldName
                rf'\bimport\s+.*\b{re.escape(old_name)}\b',
                rf'\bfrom\s+.*\bimport\s+.*\b{re.escape(old_name)}\b',
                # Type hints and instantiation: OldName(...)
                rf'\b{re.escape(old_name)}\s*\(',
                # Inheritance: class Something(OldName)
                rf'\(\s*{re.escape(old_name)}\s*[,\)]',
                # Type annotations: var: OldName
                rf':\s*{re.escape(old_name)}\b',
                # Return types: -> OldName
                rf'->\s*{re.escape(old_name)}\b',
            ]
            
            count = 0
            for pattern in patterns:
                matches = list(re.finditer(pattern, content, re.MULTILINE))
                for match in matches:
                    # Replace only the class name part, preserving context
                    old_match = match.group(0)
                    new_match = old_match.replace(old_name, new_name)
                    content = content.replace(old_match, new_match, 1)
                    count += 1
            
            if count > 0:
                changes[old_name] = count
        
        # Write back if changes were made and not dry run
        if content != original_content:
            if not dry_run:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"✅ Updated {file_path}")
            else:
                print(f"🔍 Would update {file_path}")
            
            for old_name, count in changes.items():
                print(f"   {old_name} → {mappings[old_name]} ({count} occurrences)")
        
        return changes
        
    except Exception as e:
        print(f"❌ Error processing {file_path}: {e}")
        return {}

def create_backward_compatibility_aliases(file_path: Path, mappings: Dict[str, str]):
    """Create backward compatibility aliases in a separate file."""
    alias_content = '''"""
Backward compatibility aliases for refactored class names.
These will be removed in a future version.
"""

import warnings
from typing import TYPE_CHECKING

# Import new classes
'''
    
    # Add imports for new classes
    for old_name, new_name in mappings.items():
        if "Service" in new_name:
            alias_content += f"from nats_service import {new_name}\n"
        elif "Client" in new_name:
            alias_content += f"from messaging import {new_name}\n"
        elif "Monitoring" in new_name:
            alias_content += f"from monitoring import {new_name}\n"
    
    alias_content += "\n# Create deprecated aliases\n"
    
    # Create aliases with deprecation warnings
    for old_name, new_name in mappings.items():
        alias_content += f'''
def _deprecated_{old_name.lower()}(*args, **kwargs):
    warnings.warn(
        "{old_name} is deprecated. Use {new_name} instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return {new_name}(*args, **kwargs)

{old_name} = _deprecated_{old_name.lower()}
'''
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(alias_content)

def main():
    """Main refactoring function."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Refactor class names in the microservices framework')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be changed without making changes')
    parser.add_argument('--phase', type=int, choices=[1, 2, 3], help='Run specific phase only')
    parser.add_argument('--create-aliases', action='store_true', help='Create backward compatibility file')
    
    args = parser.parse_args()
    
    # Get the project root directory
    project_root = Path(__file__).parent
    
    # Select mappings based on phase
    if args.phase == 1:
        mappings = {k: v for k, v in CLASS_MAPPINGS.items() if "NATS" in v or "Service" in k}
    elif args.phase == 2:
        mappings = {k: v for k, v in CLASS_MAPPINGS.items() if "Client" in v or "Monitoring" in v}
    elif args.phase == 3:
        mappings = {k: v for k, v in CLASS_MAPPINGS.items() if "Meta" in v or "Runner" in v}
    else:
        mappings = CLASS_MAPPINGS
    
    print(f"🚀 Starting class name refactoring...")
    print(f"📁 Project root: {project_root}")
    print(f"🔄 Dry run: {args.dry_run}")
    print(f"📋 Mappings to apply: {len(mappings)}")
    
    # Find all Python files
    python_files = find_python_files(project_root)
    print(f"📄 Found {len(python_files)} Python files")
    
    # Track total changes
    total_changes = {}
    
    # Process each file
    for file_path in python_files:
        print(f"\n🔍 Processing {file_path.relative_to(project_root)}")
        changes = refactor_file(file_path, mappings, args.dry_run)
        
        # Accumulate changes
        for old_name, count in changes.items():
            total_changes[old_name] = total_changes.get(old_name, 0) + count
    
    # Create backward compatibility aliases
    if args.create_aliases and not args.dry_run:
        aliases_file = project_root / "deprecated_names.py"
        create_backward_compatibility_aliases(aliases_file, mappings)
        print(f"\n📝 Created backward compatibility file: {aliases_file}")
    
    # Summary
    print(f"\n📊 Refactoring Summary:")
    print(f"{'='*50}")
    if total_changes:
        for old_name, count in sorted(total_changes.items()):
            new_name = mappings.get(old_name, "UNKNOWN")
            print(f"{old_name:25} → {new_name:25} ({count:3} changes)")
    else:
        print("No changes needed!")
    
    if args.dry_run:
        print(f"\n💡 This was a dry run. Use --dry-run=false to apply changes.")
    else:
        print(f"\n✅ Refactoring complete!")
        print(f"🧪 Run tests to verify everything still works:")
        print(f"   uv run pytest tests/")

if __name__ == "__main__":
    main()