# Google Drive provider setup

`jarvis.google_drive.GoogleDriveProvider` is a Drive v3 provider exposed to the
agent tool loop only when `JARVIS_EXECUTION_MODE=trusted-host` and
`JARVIS_EXTERNAL_ACCESS=trusted-external` are both configured. Read-only status
and listing tools may then be offered. Authentication, folder creation, upload,
and download require task-specific intent plus an exact one-shot operator
approval before the provider call executes.

## One-time operator setup

1. Create or select a Google Cloud project and enable the Google Drive API.
2. Configure its OAuth consent screen.
3. Create an OAuth client with application type **Desktop app**.
4. Install Google's supported Python libraries:

   ```powershell
   python -m pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```

5. Download the Desktop OAuth JSON and place it at:

   ```text
   %LOCALAPPDATA%\JarvisLocal\google-drive\client_secret.json
   ```

   Keep this directory private to the Windows account running JARVIS. The default
   location normally inherits that account's Local AppData ACL; if you select a
   custom credential directory, give it an equivalently private ACL.

6. Construct the provider with the JARVIS workspace and explicitly call
   `authenticate()`. The system browser opens Google's consent screen and returns
   through a random loopback port on `127.0.0.1`. Do not paste authorization codes,
   client JSON, access tokens, or refresh tokens into JARVIS prompts.

The resulting `token.json` stays in the same local credential directory. The
default `app_files` access mode limits Drive access to files created or opened by
the app. Construct with `access_mode="full"` only when the operator intentionally
wants list/upload/download access across the whole Drive and accepts Google's
broader consent scope. The credential directory and workspace must be completely
disjoint. To change access modes, remove the old local token, revoke the prior app
grant if Google does not return a new refresh token, and explicitly authenticate
again; a token from one mode is rejected in the other mode.

All upload sources and download destinations are restricted to the configured
JARVIS workspace. Transfers default to 100 MiB and can never be configured above
512 MiB. List pages are capped at 100 items. Downloads do not overwrite an
existing local file unless `overwrite=True`; Google-native Docs, Sheets, and
Slides require an explicit supported `export_mime_type`.

Official references: [Google Drive Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
and [OAuth for desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app).
