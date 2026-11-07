# Public web reads with Fake-IP DNS

Some proxy TUN setups return synthetic addresses in `198.18.0.0/15` for public
sites. These addresses remain forbidden as public HTTP destinations. 鲸伴 does
not disable its private-network protection or change the user's proxy/DNS setup.

The shared public URL validator uses a restricted DNS fallback only when a
non-IP domain's **entire** system answer is inside that Fake-IP range. Other
private addresses and mixtures of Fake-IP/public/private answers still fail.
Local/internal hostnames, credentials in URLs, and disallowed ports remain
rejected before connecting.

The fallback sends A and AAAA queries to the fixed public AliDNS HTTPS endpoint
`dns.alidns.com/resolve`, directly at `223.5.5.5:443`. The original resolver
hostname is used for TLS certificate checking, SNI, and Host. No proxy, cookies,
authentication, custom resolver URL, or redirects are accepted. Thus the public
domain being looked up is disclosed to this public DNS resolver; no URL path,
model credential, conversation, or page body is sent to it.

Both questions share a four-second deadline and a 64KiB accepted response budget.
Duplicate JSON keys, wrong questions/status/types, truncation, invalid CNAME
chains, and any non-public A/AAAA answer fail closed. IPv4 addresses precede
IPv6. Empty terminal address families may return no records; unresolved aliases
are rejected. The resulting public address is pinned for the actual TCP
connection, with the original target hostname still checked by TLS. Every
redirect goes through the same URL/address validation again. Browser policy uses
this existing shared validator; its private-origin rules are unchanged.

The test TLS certificate/key under `tests/fixtures/public_dns_owned_tls` is a
public, synthetic fixture identity. Tests trust it only in a temporary client
SSL context. It is never installed in the Windows certificate store or used by
production clients.

Primary references:

- [Mihomo DNS configuration](https://wiki.metacubex.one/config/dns/) documents
  Fake-IP ranges and filtering.
- [AliDNS public DoH JSON API](https://help.aliyun.com/en/dns/httpdns-doh-json-api)
  documents the HTTPS JSON resolver and bootstrap addresses.
- [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
  discusses checking resolved A/AAAA addresses and DNS-pinning hazards.

The 2026-11-05 regression evidence lives under
`artifacts/user-testing-20261105/`. The original failed model run is preserved;
the network probe and production-reader verification are separate, zero-model
checks. They do not establish general web-agent task quality.
