package main

// No unit tests yet — deliberately.
//
// Nothing in this repository runs `go test`: there is no `just` target for it and no CI job, so a
// file full of real assertions here would be worse than none. It would read as covered while never
// executing, and the first change that broke it would ship green.
//
// The sidecar is written to be extracted into its own repository (see the interface contract in
// main.go's package comment — env vars in, one HTTP endpoint out, stdlib only). Unit tests belong
// with that move, where a build/test pipeline exists to run them. The natural first targets are the
// pure functions, which need no network: `parsePrincipal`, `config.allows` (account scoping, the
// role/user switch, case-insensitivity) and the offline half of `verify` (prefix, base64url, the
// STS host pin, the action check, the X-Amz-Expires bounds and the signed-lifetime window).
//
// Until then, the behavior is covered end-to-end against a live deployment by the template's E2E
// suite — `tests/e2e/test_auth.py` for the request-level decisions and `test_teams.py` /
// `test_users.py` for the allowlist — which does run in CI.
