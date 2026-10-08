## 2026-10-04 - [SSRF Bypass using Carrier-Grade NAT IPs]
**Vulnerability:** The SSRF protection in `botex/net_tools.py:_public_ips` used a blocklist of IP types (`is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast`, `is_unspecified`) to reject internal IP resolutions. This missed certain IP ranges like Carrier-Grade NAT (`100.64.0.0/10`) which are not classified as `is_private` but are not globally routable. This could allow an attacker to bypass the SSRF protection if internal services use those ranges.
**Learning:** Blocklist approaches for IP validation are prone to bypasses due to evolving or obscure reserved IP address ranges. The `ipaddress` module's `.is_global` property implements a strict allowlist approach to guarantee public routability.
**Prevention:** Always use `ip.is_global` (an allowlist approach) to verify that an IP is intended for the public internet when defending against Server-Side Request Forgery.

## 2026-02-20 - OWASP #3: Excessive Agency Mitigation

**Vulnerability:**
Excessive Agency occurs when an LLM agent is granted broader capabilities, permissions, or autonomy than necessary. If the LLM hallucinates or gets manipulated via a prompt injection attack, it may abuse these broad permissions (like executing unintended shell commands or deleting arbitrary files).

**Learning:**
Mitigating Excessive Agency requires moving validation out of the LLM context and into a strict boundary layer (Middleware). Rather than giving an agent a tool and telling it "don't use this maliciously", the agent's tool execution must pass through strict scope validation and risk-level assessments.

**Prevention:**
Implemented an `AgencyMiddleware` (`check_agency_policy` in `capabilities.py`) which acts as a centralized validator before any tool is executed in the `engine.py` execution loop. Tools are assigned a `RiskLevel` and `required_scopes`. High-risk tools explicitly require operator confirmation (`approval_required=True`). Furthermore, tools are only advertised to the model if the current execution context explicitly has the capabilities to fulfill all required scopes.
