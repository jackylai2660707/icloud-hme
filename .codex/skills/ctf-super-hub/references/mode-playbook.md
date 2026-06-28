# Mode Playbook

## Routing modes

### Auto mode
Use when the user already has challenge material.

Behavior:
1. Inspect the obvious evidence
2. Pick primary and secondary category
3. State confidence
4. Give the next 1-3 actions
5. Route to the best specialist skill

Default pairings:
- Auto + teaching for beginners
- Auto + competition for speed

### Brainstorm mode
Use when the user is lost, the prompt is vague, or the task framing is unclear.

Behavior:
1. Restate what is known
2. Identify what is missing
3. Ask one high-value question only if needed
4. Convert uncertainty into a routing decision
5. Move to auto or manual mode

Do not stay in brainstorm mode forever. Its job is to reduce ambiguity.

### Manual mode
Use when the user already picked a category.

Behavior:
1. Accept the category as the current hypothesis
2. Sanity-check it quickly
3. Continue if reasonable
4. Redirect if the mismatch is obvious

## Delivery styles

### Teaching
Use when the user is a beginner or wants explanations.

Output goals:
- plain language
- minimal jargon
- 1-line term definitions
- fewer commands, more clarity

### Competition
Use when the user wants speed.

Output goals:
- short and sharp
- direct next actions
- no unnecessary teaching blocks

### Hints-only
Use when the user wants to think independently.

Output goals:
- do not over-solve
- reveal the next clue, not the whole chain
- keep the path discoverable
