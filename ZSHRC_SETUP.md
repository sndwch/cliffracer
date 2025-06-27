# 🚀 Cliffracer .zshrc Setup

This setup configures your zsh environment for optimal Cliffracer development.

## 🎯 Quick Setup

```bash
# Run the setup script
./setup_zshrc.sh

# Apply changes
source ~/.zshrc

# Check everything works
cliffracer_info
```

## 📦 What's Included

### **Environment Variables**
- `CLIFFRACER_ROOT` - Project root directory
- `CLIFFRACER_SRC` - Source code directory  
- `PYTHONPATH` - Includes src for development imports
- `NATS_URL` - NATS server connection
- Service URLs for monitoring (Zabbix, Grafana, etc.)

### **Python & Package Management**
- **pyenv** - Python version management (3.13.2)
- **UV** - Fast Python package manager (auto-installs if missing)
- **PYTHONPATH** - Set for development imports

### **Docker Configuration**
- **Rancher Desktop** path configuration
- **Docker BuildKit** enabled
- **Multi-platform** support

### **Useful Aliases**
```bash
# Navigation
cliff           # cd to project root
cdsrc          # cd to src/cliffracer
cdex           # cd to examples
cddep          # cd to deployment

# Python
py             # python3
pip            # python3 -m pip

# UV Package Manager
uvs            # uv sync
uvr            # uv run
uva            # uv add package
uvd            # uv remove package

# Git
gs             # git status
gd             # git diff
gl             # git log --oneline -10
gp             # git push
gc             # git commit -m

# Docker
dc             # docker-compose
dcu            # docker-compose up -d
dcd            # docker-compose down
dcl            # docker-compose logs -f
dps            # docker ps
```

### **Cliffracer Development Functions**

#### **cliffracer_setup**
Sets up complete development environment:
- Sets Python version via pyenv
- Installs all dependencies with UV
- Tests imports work correctly

```bash
cliffracer_setup
```

#### **cliffracer_start [mode]**
Starts services in different modes:
```bash
cliffracer_start simple    # Simple demo (default)
cliffracer_start nats      # NATS server only
cliffracer_start full      # Full stack with monitoring
```

#### **cliffracer_stop**
Stops all Cliffracer services and processes:
```bash
cliffracer_stop
```

#### **cliffracer_test [type]**
Runs tests:
```bash
cliffracer_test unit       # Unit tests only
cliffracer_test integration # Integration tests
cliffracer_test e2e        # End-to-end tests
cliffracer_test all        # All tests (default)
```

#### **cliffracer_lint**
Runs code quality checks:
```bash
cliffracer_lint            # ruff + mypy
```

#### **cliffracer_format**
Formats code:
```bash
cliffracer_format          # ruff format
```

#### **cliffracer_info**
Shows development environment info and available commands:
```bash
cliffracer_info
```

## 🌐 Service URLs (when running)

- **NATS Monitor**: http://localhost:8222
- **Zabbix Dashboard**: http://localhost:8080 (admin/zabbix)
- **Grafana**: http://localhost:3000 (admin/admin)  
- **Metrics Exporter**: http://localhost:9090

## 🔧 Quality of Life Features

### **Enhanced History**
- 10,000 command history
- Shared across terminal sessions
- No duplicates

### **Auto UV Installation**
If UV is not installed, it's automatically installed on first load.

### **Welcome Message**
When you open a terminal in your home directory, you'll see:
```
🚀 Welcome to Cliffracer development!
💡 Run 'cliffracer_info' for quick commands
```

## 📋 Typical Development Workflow

```bash
# 1. Setup (first time only)
cliffracer_setup

# 2. Start development
cliff                      # Navigate to project
cliffracer_start nats     # Start NATS server

# 3. Make changes
# ... edit code ...

# 4. Test and lint
cliffracer_lint           # Check code quality
cliffracer_test unit      # Run tests

# 5. Run demos
cliffracer_start simple   # Test your changes

# 6. Commit
gs                        # Check status
gc "Your commit message"  # Commit
gp                        # Push
```

## 🛠️ Customization

The configuration is added to your `.zshrc` in a clearly marked section:
```bash
# =============================================================================
# Cliffracer Development Environment Configuration
# ... configuration here ...
# End of Cliffracer Configuration
# =============================================================================
```

You can easily modify or remove it by editing this section in `~/.zshrc`.

## 🆘 Troubleshooting

### UV Not Installing
```bash
# Manual UV installation
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.zshrc
```

### Python Version Issues
```bash
# Install Python 3.13.2 via pyenv
pyenv install 3.13.2
pyenv local 3.13.2
```

### Docker Issues
```bash
# Check Docker is running
docker ps

# Restart Rancher Desktop if needed
```

### Import Issues
```bash
# Check PYTHONPATH
echo $PYTHONPATH

# Test imports
cd $CLIFFRACER_ROOT
python -c "from cliffracer import NATSService; print('OK')"
```