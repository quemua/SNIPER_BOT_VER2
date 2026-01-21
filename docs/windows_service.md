# Windows VPS Setup Guide

This guide explains how to run Endgame Sniper Bot on Windows VPS 24/7.

## Option 1: Windows Task Scheduler (Recommended)

### Create Scheduled Task

1. **Open Task Scheduler**
   - Press `Win + R`, type `taskschd.msc`, press Enter

2. **Create New Task**
   - Click "Create Task" (not "Create Basic Task")

3. **General Tab**
   - Name: `EndgameSniperBot`
   - Description: `Polymarket Sniper Bot - Headless Mode`
   - Select: "Run whether user is logged on or not"
   - Check: "Run with highest privileges"

4. **Triggers Tab**
   - Click "New..."
   - Begin the task: "At startup"
   - Check: "Enabled"
   - Click OK

5. **Actions Tab**
   - Click "New..."
   - Action: "Start a program"
   - Program/script: `C:\Path\To\Python\python.exe`
   - Add arguments: `run_headless.py --log-level INFO`
   - Start in: `C:\Path\To\SNIPER_BOT_VER2`
   - Click OK

6. **Conditions Tab**
   - Uncheck: "Start the task only if the computer is on AC power"

7. **Settings Tab**
   - Check: "Allow task to be run on demand"
   - Check: "If the task fails, restart every: 1 minute"
   - Attempt to restart up to: "3 times"
   - Check: "If the running task does not end when requested, force it to stop"

8. **Click OK** and enter your Windows password

### Test the Task

```powershell
# Run task manually
schtasks /run /tn "EndgameSniperBot"

# Check task status
schtasks /query /tn "EndgameSniperBot"

# Stop task
schtasks /end /tn "EndgameSniperBot"
```

## Option 2: NSSM (Non-Sucking Service Manager)

NSSM allows running the bot as a proper Windows service.

### Install NSSM

1. Download from: https://nssm.cc/download
2. Extract to `C:\nssm`
3. Add `C:\nssm\win64` to PATH

### Create Service

```powershell
# Open admin command prompt
nssm install EndgameSniperBot

# Configure in GUI:
# - Path: C:\Path\To\Python\python.exe
# - Startup directory: C:\Path\To\SNIPER_BOT_VER2
# - Arguments: run_headless.py --log-level INFO

# Or use command line:
nssm install EndgameSniperBot "C:\Python311\python.exe" "run_headless.py --log-level INFO"
nssm set EndgameSniperBot AppDirectory "C:\Path\To\SNIPER_BOT_VER2"
nssm set EndgameSniperBot DisplayName "Endgame Sniper Bot"
nssm set EndgameSniperBot Description "Polymarket Sniper Bot - Headless Mode"
nssm set EndgameSniperBot Start SERVICE_AUTO_START
nssm set EndgameSniperBot AppStopMethodSkip 0
nssm set EndgameSniperBot AppStopMethodConsole 3000
nssm set EndgameSniperBot AppStopMethodWindow 3000
nssm set EndgameSniperBot AppStopMethodThreads 1000
nssm set EndgameSniperBot AppRestartDelay 60000
```

### Manage Service

```powershell
# Start service
nssm start EndgameSniperBot

# Stop service
nssm stop EndgameSniperBot

# Restart service
nssm restart EndgameSniperBot

# Check status
nssm status EndgameSniperBot

# Remove service
nssm remove EndgameSniperBot confirm
```

## Option 3: PowerShell Script with Restart

Create `start_bot.ps1`:

```powershell
# start_bot.ps1 - Auto-restart bot on crash

$botPath = "C:\Path\To\SNIPER_BOT_VER2"
$pythonPath = "C:\Python311\python.exe"
$maxRestarts = 10
$restartDelay = 60  # seconds

Set-Location $botPath

$restartCount = 0
while ($restartCount -lt $maxRestarts) {
    Write-Host "Starting bot (attempt $($restartCount + 1))..."

    $process = Start-Process -FilePath $pythonPath `
        -ArgumentList "run_headless.py", "--log-level", "INFO" `
        -WorkingDirectory $botPath `
        -NoNewWindow `
        -PassThru `
        -Wait

    $exitCode = $process.ExitCode
    Write-Host "Bot exited with code: $exitCode"

    if ($exitCode -eq 0) {
        Write-Host "Bot stopped normally"
        break
    }

    $restartCount++
    Write-Host "Restarting in $restartDelay seconds..."
    Start-Sleep -Seconds $restartDelay
}

if ($restartCount -ge $maxRestarts) {
    Write-Host "Max restarts reached!"
}
```

Run with:
```powershell
powershell -ExecutionPolicy Bypass -File start_bot.ps1
```

## Environment Setup

### 1. Install Python

Download Python 3.10+ from python.org and install with:
- Check "Add Python to PATH"
- Check "Install pip"

### 2. Install Dependencies

```powershell
cd C:\Path\To\SNIPER_BOT_VER2
pip install -r requirements.txt

# For better Windows signal handling (optional)
pip install pywin32
```

### 3. Configure .env

Create `.env` file:
```env
PRIVATE_KEY=your_wallet_private_key

# Optional: Proxy (recommended)
PROXY_URL=socks5://user:pass@host:port

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 4. Test Run

```powershell
python run_headless.py --log-level DEBUG
```

## Monitoring

### Check Logs

```powershell
# View live log
Get-Content sniper.log -Wait -Tail 50

# Search for errors
Select-String -Path sniper.log -Pattern "ERROR"
```

### Check Process

```powershell
# Find bot process
Get-Process python | Where-Object {$_.CommandLine -like "*run_headless*"}

# Kill bot process
Get-Process python | Where-Object {$_.CommandLine -like "*run_headless*"} | Stop-Process
```

## Troubleshooting

### Bot doesn't start

1. Check Python path: `where python`
2. Check dependencies: `pip list`
3. Check .env file exists and has PRIVATE_KEY
4. Run manually to see errors: `python run_headless.py`

### IP Blocked (Cloudflare)

1. Use a proxy (SOCKS5 recommended)
2. Add to .env: `PROXY_URL=socks5://user:pass@host:port`
3. Install: `pip install pysocks requests[socks]`

### Task Scheduler not starting

1. Check task history in Task Scheduler
2. Verify Python path is correct
3. Make sure "Start in" directory is correct
4. Try running the task manually

### High CPU/Memory

1. Check `sniper.log` for error loops
2. Reduce enabled markets in config.json
3. Increase polling intervals for FAR tier
