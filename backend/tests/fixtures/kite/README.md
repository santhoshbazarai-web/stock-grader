# Kite Connect v3 fixtures

Hand-written in the documented Kite Connect v3 format (no live credentials were used, so nothing
here was recorded from the real API). Tests serve them over HTTP with `responses`, so the real
`kiteconnect` SDK request/parse path runs and no request reaches the network.

- `instruments_nse.csv` — `GET /instruments/NSE` dump (trimmed): equities, a BE-series row,
  indices (`segment=INDICES`) and one derivative row that must be ignored.
- `historical_tcs_day.json` — `GET /instruments/historical/2953217/day`, trading days Jan–Mar
  2024 (26 Jan omitted); tests slice it by `from`/`to`.
- `historical_empty.json` — no candles in range.
- `ltp.json` — `GET /quote/ltp`; unknown instruments are simply absent, as in the real API.
- `error_*.json` — error envelopes by `error_type` (PermissionException = no historical add-on,
  TokenException, InputException, NetworkException 429, GeneralException 500).
- `session_ok.json` / `session_bad_token.json` — `POST /session/token` responses.

Replace with recorded responses once credentials are available (keep tokens out of the files).
