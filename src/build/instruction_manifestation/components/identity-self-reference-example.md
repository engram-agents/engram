---
id: identity-self-reference-example
kind: base
composition_mode: flat
extension_required: false
variables: [IDENTITY_FILE_NAME, SESSION_START_MECHANISM]
---
This file, `{{IDENTITY_FILE_NAME}}`, is where these instructions live between sessions. At the start of each new one, {{SESSION_START_MECHANISM}} the relevant context back into view, so nothing here has to be re-read by hand before it can act on it.
