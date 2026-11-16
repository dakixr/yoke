# Frozen CLI output

`headless/*.json` and `turn_projection/*.json` were captured once from the
in-process CLI at base `086d0c4a4dc27e43e78fcdda38efd9433eb7a095`. Commit
`a15cba5881405aebe6d891b16e00e2f43f53afaf` verifies the captured values against
that implementation before its callers switch to the daemon. The 14 comparison
cases passed before and after capture. These fixtures must not be regenerated.

`session-info.txt` was captured and verified against the local backend's `/info`
rendering in `66bd2fa33267aa324bc4127f16ae4f4a4e422092`, then the local comparison
was removed. Only its workspace, store and timestamps are substituted at read time.

These are additional fixtures. The existing `provider_projection/golden` files
are unchanged.
