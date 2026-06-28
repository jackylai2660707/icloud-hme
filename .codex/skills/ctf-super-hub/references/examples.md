# Example Sessions

## Example 1: vague binary challenge

Input:
- file: `chall`
- text: `Can you recover the secret?`

Good route:
- primary: `ctf-reverse`
- backup: `ctf-pwn`
- next step: identify file type and observable behavior

## Example 2: login page with JWT

Input:
- URL with login/register
- token-looking cookie

Good route:
- primary: `ctf-web`
- backup: `ctf-crypto`
- next step: inspect token structure and auth flow

## Example 3: only one poetic line

Input:
- `The truth is hidden in the noise.`

Good route:
- start in `brainstorm`
- determine whether there is media, signal data, PCAP, or obfuscated output
- only then route to `ctf-forensics`, `ctf-misc`, or `ctf-reverse`
