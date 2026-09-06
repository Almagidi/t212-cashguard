T212 CashGuard Trader — Quick Start
=====================================

HOW TO USE THESE FILES
──────────────────────

Double-click each .command file to run it. That's it.

1. Setup (Run First).command
   └── Run this ONE TIME to install everything and create a mock-only setup.
       Takes 5–15 minutes. After this, you never need to code anything.

2. Start CashGuard.command
   └── Run this every time you want to use the app.
       Opens the dashboard in your browser automatically.
       Keep this window open while trading.

3. Stop CashGuard.command
   └── Run this to cleanly shut everything down.
       Your data and settings are preserved.

4. Update API Keys.command
   └── Explains how to add a Trading 212 demo connection in the app.
       It never collects or writes credentials.

5. Check Status.command
   └── Run this to see what's running and check for errors.

6. Enable Live Trading (Read First).command
   └── Non-mutating notice: live trading is prohibited and cannot be enabled
       by a launcher.


FIRST TIME?
───────────
1. Double-click "1. Setup (Run First).command"
2. Follow the prompts (takes 5-15 minutes)
3. Double-click "2. Start CashGuard.command"
4. Done — the dashboard opens in your browser


DAILY USE
─────────
1. Double-click "2. Start CashGuard.command"
2. Use the app in your browser at http://localhost:3000
3. When done, close the Terminal window or run "3. Stop CashGuard"


IMPORTANT NOTES
───────────────
• Keep the Start terminal window open while using CashGuard
• Closing the lid pauses everything — plug in before trading sessions
• Always run a backtest before enabling a strategy
• Setup starts in broker-isolated mock mode
• Add demo credentials only through the authenticated Broker page
• Live trading is prohibited during the safety-remediation programme


LOGIN DETAILS
─────────────
URL:      http://localhost:3000
Email:    admin@localhost
Password: (the one you chose during setup)
