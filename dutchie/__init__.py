"""Dutchie client package — vendored + trimmed from the marketing_dashboard monorepo.

Pure-Python (no Django import) so it stays testable standalone. Three layers:

- transport: curl_cffi Chrome-impersonation POST/GET (beats Cloudflare JA3).
- login + session: EmployeeLogin -> cookie/session, cached, 1-retry-on-401.
- clients: PosRegisterClient (the budtender write path) + PosReadClient (REST reads).
"""
