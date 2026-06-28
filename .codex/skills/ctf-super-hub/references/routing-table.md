# Routing Table

## By artifact type

- `.pcap`, `.pcapng`, `.evtx`, `.raw`, `.dd`, `.E01` -> `ctf-forensics`
- image / audio / video with hidden-content vibes -> `ctf-forensics`
- `.elf`, `.exe`, `.so`, `.dll`, raw binary -> `ctf-reverse` or `ctf-pwn`
- `.apk`, `.wasm`, `.pyc`, firmware blob -> `ctf-reverse`
- source tree with HTML/JS/PHP/templates/backend routes -> `ctf-web`
- `.py`, `.sage`, math-heavy text, big integers -> `ctf-crypto`
- suspicious script / packed sample / C2 config -> `ctf-malware`

## By wording clues

- XSS / SQL / SSTI / SSRF / JWT / upload / auth bypass -> `ctf-web`
- RSA / AES / nonce / prime / modulus / lattice / PRNG -> `ctf-crypto`
- reverse / crackme / VM / bytecode / anti-debug / firmware -> `ctf-reverse`
- overflow / ROP / libc / heap / fmt / seccomp -> `ctf-pwn`
- PCAP / memory / disk / registry / stego / spectrogram -> `ctf-forensics`
- geolocation / who is this / where was this taken / public account -> `ctf-osint`
- malware / beacon / obfuscation / PE / C2 -> `ctf-malware`
- jail / encoding / puzzle / game / weird interpreter -> `ctf-misc`
- prompt injection / model extraction / jailbreak / adversarial -> `ctf-ai-ml`

## By service behavior

- website / API / cookies / login / upload -> `ctf-web`
- interactive binary over TCP and crashes on bad input -> `ctf-pwn`
- remote math or oracle game -> `ctf-crypto`
- restricted shell / eval loop -> `ctf-misc`

## Reverse vs pwn quick split

Choose `ctf-reverse` first when the main problem is:
- understanding program logic
- recovering constants, keys, validation rules, or hidden code paths
- working with APK, WASM, firmware, or VM behavior

Choose `ctf-pwn` first when the main problem is:
- corrupting memory
- hijacking control flow
- bypassing mitigations
- exploiting a live service

If unsure, start with `ctf-reverse` to understand the binary, then pivot to `ctf-pwn`.
