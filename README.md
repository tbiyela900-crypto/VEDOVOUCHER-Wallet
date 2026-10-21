# RewardOS — Advanced Virtual Rewards Wallet

A Flask + SQLite school-project application with a professional dark dashboard.

## Included features
- Account registration with one-use level vouchers and referral codes
- Password hashing, CSRF protection, safer session cookie defaults
- Login-attempt throttling and disabled-account checks
- Daily check-ins, streak rewards, challenge progress and reward ledger
- Dashboard with seven-day activity bars, recent transactions and challenges
- Filterable activity history, community referrals and consistency leaderboard
- Password change page
- Admin dashboard, voucher generator, member activation controls and CSV export
- Audit log for important actions
- SQLite foreign keys, balance constraints and transaction records

## Important
This is a fictional educational simulation. It does not accept deposits, handle bank details,
provide real investment returns, or transfer money. Do not deploy it as a production financial
service without a security review and substantial additional engineering.

## Windows quick start
1. Install Python 3.10 or newer.
2. Extract the ZIP file.
3. Open Command Prompt in the extracted folder.
4. Run:

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   py -m pip install -r requirements.txt
   py app.py
   ```

5. Open http://127.0.0.1:5000 in your browser.

## Initial administrator
By default, the first run creates an admin account:
- Username: `admin`
- Password: `ChangeMe-123!`

Change the default by setting environment variables before first launch:
- Windows CMD: `set ADMIN_USER=youradmin`
- Windows CMD: `set ADMIN_PASS=your-long-password`
- PowerShell: `$env:ADMIN_USER="youradmin"`
- PowerShell: `$env:ADMIN_PASS="your-long-password"`

The administrator is created only when the database does not already contain an admin.
The first run also prints one starter voucher for each level to the terminal. Copy a voucher
from the terminal and use it when registering a member.

## Database reset
Stop the app, back up `rewards_wallet.db`, then delete it only if you intentionally want to
reset all demo data. Restart the app to create a fresh database and new starter vouchers.

## Project presentation ideas
Explain the route/controller design, SQLite relational schema, password hashing, CSRF tokens,
server-side validation, ledger-based balance changes, admin role checks, CSV export, and how
you would test the application. Mention limitations honestly: this is a local demo and has not
been audited or penetration-tested.
