# Prompt Library

## Auto mode prompt

```text
Please use ctf-super-hub in auto mode.
First classify the challenge, then give me the best ctf-* skill, a backup skill, the reason for the choice, and only the next 1-3 actions.
Explain it in beginner-friendly language.

Challenge info:
[paste text, files, URL, IP:PORT, or source tree here]
```

## Brainstorm mode prompt

```text
Please use ctf-super-hub in brainstorm mode.
I do not understand what kind of challenge this is.
First help me clarify the goal, the likely category, what information is missing, and whether we should auto-route or manual-route.

Challenge info:
[paste description and anything I already tried]
```

## Manual mode prompt

```text
Please use ctf-super-hub in manual mode.
My guess is that this is a [web/reverse/crypto/pwn/forensics/osint/malware/misc/ai-ml] challenge.
Continue with that category unless the evidence strongly points somewhere else.

Challenge info:
[paste material here]
```

## Hints-only prompt

```text
Please use ctf-super-hub in auto mode with hints-only delivery.
Do not give me the whole solve path yet.
Just give me the next clue to investigate and why.
```
