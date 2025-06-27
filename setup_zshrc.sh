#!/bin/bash
# Setup script to add Cliffracer configuration to .zshrc

ZSHRC="$HOME/.zshrc"
CLIFFRACER_CONFIG="$(pwd)/.zshrc_cliffracer"

echo "🚀 Setting up Cliffracer development environment in .zshrc"
echo "=================================================="

# Backup existing .zshrc
if [ -f "$ZSHRC" ]; then
    echo "📋 Backing up existing .zshrc to .zshrc.backup"
    cp "$ZSHRC" "$ZSHRC.backup"
fi

# Check if Cliffracer config already exists
if grep -q "Cliffracer Development Environment" "$ZSHRC" 2>/dev/null; then
    echo "⚠️  Cliffracer configuration already exists in .zshrc"
    read -p "Do you want to update it? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Remove old configuration
        sed -i.tmp '/# Cliffracer Development Environment/,/# End of Cliffracer Configuration/d' "$ZSHRC"
        rm -f "$ZSHRC.tmp"
        echo "🔄 Removed old Cliffracer configuration"
    else
        echo "❌ Skipping update"
        exit 0
    fi
fi

# Add Cliffracer configuration
echo "" >> "$ZSHRC"
echo "# =============================================================================" >> "$ZSHRC"
cat "$CLIFFRACER_CONFIG" >> "$ZSHRC"

echo "✅ Cliffracer configuration added to .zshrc"
echo ""
echo "🔄 To apply changes immediately, run:"
echo "   source ~/.zshrc"
echo ""
echo "🎯 Or restart your terminal"
echo ""
echo "💡 After reloading, run 'cliffracer_info' to see available commands"