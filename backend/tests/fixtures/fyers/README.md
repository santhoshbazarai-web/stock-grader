# Fyers API v3 fixtures

Hand-written in the documented Fyers API v3 response format (no live credentials were used, so
nothing here was recorded from the real API). Tests serve them over HTTP with `responses`, so the
real `fyers-apiv3` SDK request path runs and no request reaches the network.

- `history_tcs_daily.json` — `/data/history` daily candles for trading days Jan–Mar 2024
  (26 Jan holiday omitted); epochs are 00:00 IST. Tests slice it by `range_from`/`range_to`.
- `history_no_data.json` — `s: "no_data"` (e.g. range before listing).
- `quotes.json` — `/data/quotes`, two valid symbols and one rejected symbol.
- `error_*.json` — error envelopes (token expired `-8`, invalid symbol `-300`, HTTP 503).
- `token_ok.json` — `/api/v3/validate-authcode` success; the access token is a JWT-shaped string
  with a fake signature whose `exp` is 2024-03-30 06:00 IST.
- `token_invalid_code.json` — auth-code rejection.

Replace with recorded responses once credentials are available (keep tokens out of the files).
