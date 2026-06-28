# First Five Minutes

## Web
- load the site
- note routes, headers, cookies, auth flows, upload points
- inspect client-side code for obvious secrets or APIs

## Reverse
- run `file`
- run `strings`
- inspect imports, symbols, and quick behavior
- decide whether static or dynamic analysis comes first

## Pwn
- run `file`
- run `checksec`
- test the service locally or remotely
- look for crashable input and primitive type

## Crypto
- identify the scheme or at least the primitive family
- list known values, unknown values, oracle access, and constraints
- check for classic weak patterns before building a heavy solve

## Forensics
- identify every artifact type first
- preserve originals
- check metadata, headers, structure, and embedded content

## OSINT
- extract every identifier
- search names, handles, domains, EXIF, and map clues
- build a small timeline instead of randomly searching

## Malware
- hash the sample
- identify file type and packers
- extract strings, config clues, network indicators, and execution style

## Misc
- identify whether this is really misc or just badly labeled
- check for jail behavior, encoding layers, or game rules
