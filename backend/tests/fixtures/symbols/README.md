# Symbol-master fixtures

Small files in the shape of the real masters, for `tests/test_symbol_master.py`:

- `EQUITY_L.csv`: NSE equity list (`nsearchives.nseindia.com/content/equities/EQUITY_L.csv`).
- `symbolchange.csv`, `namechange.csv`: NSE symbol / name changes (same folder).
- `bse_scrips.json`: BSE `ListofScripData` API.
- `NSE_CM.csv`, `BSE_CM.csv`: Fyers `public.fyers.in/sym_details/*.csv` (no header).

The companies, ISINs, BSE codes and past names/symbols are real (HDFC Bank = 500180,
Bharti Airtel was Bharti Tele-Ventures, Infosys traded as INFOSYSTCH), but the rows are cut
down and some columns simplified. `ACMERURAL` / `INE999Z01011` is made up (a BSE-only
company). The column layouts were written from memory of the published files and are not
verified against live downloads (NSE and BSE are unreachable from the build environment):
check them with `python -m app.jobs run symbol_master` once the hosts are reachable.
