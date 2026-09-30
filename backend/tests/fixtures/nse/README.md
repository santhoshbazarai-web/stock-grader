# NSE sample files

Written by hand in the published formats (no live download from this environment), with the
real quirks preserved: `sec_bhavdata_full` has space-padded headers and cells and `-` for
series without delivery data; `fo_secban.csv` is a title line plus `n,SYMBOL` rows (or `NIL`);
JSON bodies follow the shapes of `/api/reportASM`, `/api/reportGSM`,
`/api/corporates-corporateActions`, `/api/corporate-share-holdings-master` and
`/api/corporates-financial-results` (`financial_results_acme.json`: a filing without an XBRL
document, `-`, and one on a foreign host are included on purpose). The XBRL documents
themselves are in `../xbrl`.
`SAMPLEIND` and the ASM/GSM companies are fictional. Replace with real downloads when
convenient; the parsers raise `ProviderError` naming any shape they don't recognise.
