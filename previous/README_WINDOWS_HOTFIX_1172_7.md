# Windows Hotfix 7

- Fix copy-source failure on paths such as `D:\download\qa_win_hotfix6\`.
- Removed `System.IO.Path::GetFullPath()` from installer helper.
- Uses literal filesystem paths via `Get-Item -LiteralPath`.
- Verifies required files after copy before continuing.
