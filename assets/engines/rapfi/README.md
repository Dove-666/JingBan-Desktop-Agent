# Rapfi for 鲸伴

Pinned official release: [Rapfi 250615](https://github.com/dotb1ue/rapfi/releases/tag/250615).
Source and license: [the same upstream tag](https://github.com/dotb1ue/rapfi/tree/250615),
GPL-3.0-or-later; see `Copying.txt` and `AUTHORS` in this directory.

Install or restore the Windows CPU package from the repository root:

```powershell
.venv/Scripts/python.exe tools/setup_rapfi.py
```

The installer verifies the official 36,786,619-byte archive against the fixed
SHA-256 `1a3e24024062a153ac079060ee9589a37c6bdd1ecc54fed3908793c519594e05`
before extracting anything. It selects the upstream Windows AVX2 executable when
Windows reports AVX2 support, otherwise the upstream SSE executable. This
machine uses `pbrain-rapfi-windows-avx2.exe`, installed as `rapfi.exe`.
The original config and its four referenced neural evaluation files are kept.
`manifest.json` records the selected executable and every installed file's digest.

鲸伴 currently exposes 15×15 freestyle and standard Gomoku. Protocol rule 0 is
freestyle (five or more wins); rule 1 is standard (exactly five wins).
Renju is not exposed by the game UI. The wrapper's optional `YXSHOWFORBID`
query is available for later rule work, not a claim of complete local Renju rules.

The process runs without a console, one owned subprocess per engine object.
It uses `YXBOARD` followed by `YXNBEST`, with background pondering and database
writes disabled. Public coordinates are A1 through O15, including column I;
row 1 is the top. Scores use Rapfi's native units from the side to move's
perspective. Candidate lists contain a complete search iteration of distinct
root moves, or only the final best move if comparable scores are unavailable.

Executables and model weights are local, ignored assets. The installer only
updates this Rapfi directory and refuses an existing directory belonging to a
different engine.
