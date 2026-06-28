# Pivot Patterns

## Common two-skill combinations

### Web + Crypto
Use when the web app depends on tokens, signatures, custom MACs, or broken encryption.

### Web + Reverse
Use when a browser challenge hides logic in WASM, packed JavaScript, or an obfuscated client.

### Reverse + Pwn
Use when a binary must be understood before it can be exploited.

### Forensics + Crypto
Use when captures, dumps, or recovered files are encrypted.

### Malware + Forensics
Use when a sample and its traffic/log artifacts need to be correlated.

### Misc + Crypto
Use when the challenge is a jail or puzzle but the core primitive is cryptographic.

## Pivot triggers

Pivot categories when:
- the first category explains only half the evidence
- the expected artifact is missing
- the first three steps produce results that better fit another category
- the user-provided label conflicts with the actual artifact type

## Low-risk pivot phrasing

Use language like:
- "This started looking like reverse, but the live service behavior makes pwn a better primary route."
- "The web part looks thin; the real weakness is probably the token design, so crypto becomes the supporting skill."
