# SignPath code signing — one-time setup

This guide walks through getting a free SignPath Foundation certificate so every release of `Transcriptarr.exe` is signed and antivirus-engine-friendly.

You only need to do this once per project. After it's set up, the `release.yml` GitHub workflow signs every tagged release automatically.

## What you'll get

- A real Authenticode signature on `Transcriptarr.exe` from "SignPath Foundation"
- Antivirus engines and Windows will recognize the signer rather than treating each release as a brand-new unknown binary
- Your release artifacts are immutable and traceable (SignPath records who/what/when for every signature)
- No cost — SignPath Foundation provides this free for qualifying OSS projects

## What's required of you

- Project must be public on GitHub with an OSI-approved license (MIT, Apache, GPL, etc.)
- No proprietary / closed-source components
- 2FA enabled on your GitHub account AND your SignPath account
- Every release manually approved in the SignPath dashboard (the workflow waits for your click)
- A release must already exist before you apply (push `v0.1.0` first, even unsigned)

## What it doesn't fix immediately

Windows SmartScreen builds *reputation* per certificate based on cumulative download count. A brand-new SignPath certificate will still produce the "Windows protected your PC" prompt for the first dozen or so installs — Windows can't tell yet whether the signer is reputable. The signature is unchanged; just nobody's run it yet. After enough installs (typically 1–4 weeks for a small project), SmartScreen stops warning entirely.

To fully bypass SmartScreen on day one you'd need an EV (Extended Validation) Authenticode certificate, which runs ~$300/yr. SignPath OSS is the right balance of free + signed for a hobby project.

---

## Step 1 — Push your repo to GitHub (public)

SignPath OSS only signs public open-source projects. Make sure:

- The repo is at `github.com/Rusty-Meat/transcriptarr` and is **public**
- The `Transcriptarr.spec`, `wizard.py`, `transcriptarr.py`, and `.github/workflows/release.yml` files are checked in
- It has a clear license file (MIT/Apache/GPL — pick whatever you prefer; I'd suggest MIT for ease)

If you don't have a license file yet:

```bash
# In the repo root, create a LICENSE file. SignPath requires this.
# A minimal MIT license is fine — replace YEAR and NAME.
```

## Step 2 — Apply to SignPath Foundation

The free OSS program lives at **<https://signpath.io/solutions/open-source-community>** (Foundation home page: <https://signpath.org/>). Read the eligibility requirements at <https://signpath.org/terms.html> first — the important ones:

- The project must use an OSI-approved open-source license (MIT, Apache, GPL, etc.) without commercial dual-licensing
- No proprietary or closed-source components in the repo
- The project must be actively maintained
- A release must already exist (push at least `v0.1.0` first, even if unsigned)
- You and any other maintainers must enable **two-factor authentication** on both your GitHub account AND your SignPath account
- Every signing operation requires manual approval — you won't get fully automated signing, but the workflow handles this gracefully (it waits for you to approve in the SignPath dashboard before continuing)

To apply, go to <https://signpath.org/> and use their application form / contact email (they send applications via email rather than a self-service signup). Include:

- Project name: `Transcriptarr`
- Project URL: `https://github.com/Rusty-Meat/transcriptarr`
- A short description (one paragraph from the README is fine)
- Your contact email
- Confirmation that you've read the terms and the project meets them

Approval typically takes a few business days. You'll get an email when the project is approved with onboarding instructions.

## Step 3 — Configure your SignPath project

Once approved, log in to **<https://app.signpath.io>** and:

1. Open your project from the dashboard
2. Note your **Organization ID** (long UUID, shown in URLs and project header)
3. Note your **Project slug** (usually `transcriptarr`)

### Create a signing policy

You need at least one signing policy. SignPath calls these "release-signing" by convention.

1. In the project, go to **Signing Policies** → **New**
2. Name: `release-signing`
3. Slug: `release-signing` (used by the workflow file)
4. Certificate: pick the SignPath Foundation OSS certificate they assigned you
5. Save

### Create an API token

1. Go to **Profile** → **API tokens** → **New**
2. Name it `github-actions`
3. Copy the token — you only see it once

## Step 4 — Add secrets and variables to GitHub

Go to **github.com/Rusty-Meat/transcriptarr/settings**.

### Secrets (Settings → Secrets and variables → Actions → New repository secret)

| Name | Value |
|---|---|
| `SIGNPATH_API_TOKEN` | The API token from Step 3 |

### Variables (same page, "Variables" tab)

| Name | Value |
|---|---|
| `SIGNPATH_ORGANIZATION_ID` | Your org UUID from Step 3 |
| `SIGNPATH_PROJECT_SLUG` | `transcriptarr` |
| `SIGNPATH_POLICY_SLUG` | `release-signing` |

The workflow's `if: ${{ vars.SIGNPATH_ORGANIZATION_ID != '' }}` clause means the sign job is skipped automatically if these aren't set yet — so the workflow file is safe to commit before SignPath approval comes through. It just produces an unsigned release in the meantime.

## Step 5 — Cut your first release

```bash
git tag v0.1.0
git push origin v0.1.0
```

Watch the build at `github.com/Rusty-Meat/transcriptarr/actions`. The pipeline will:

1. Build `Transcriptarr.exe` on a Windows runner (~3 minutes)
2. Submit it to SignPath (~1 minute)
3. Create a **draft** GitHub release with the signed `Transcriptarr.exe` attached

You then go to the release, review it, optionally edit the auto-generated release notes, and click **Publish release**.

## Step 6 — Verify the signature

On any Windows machine:

```powershell
Get-AuthenticodeSignature .\Transcriptarr.exe
```

You should see `Status: Valid` and `SignerCertificate.Subject` containing `SignPath Foundation`.

---

## After it's set up

Every tag push (`v0.1.1`, `v0.2.0`, etc.) triggers the same flow. You only need to revisit SignPath's UI if you want to rotate the API token or change the signing policy.

If a SignPath sign job ever fails (e.g., approval flagged), the release job falls back to the unsigned artifact and creates a draft release with that — so you always get *something* downloadable, you just lose the signature for that particular release.

## Troubleshooting

### "Project does not have any signing policies configured" in the workflow log

You skipped Step 3's "Create a signing policy" — go back and create one named `release-signing`.

### "Forbidden" / 403 from SignPath

The API token in `SIGNPATH_API_TOKEN` is wrong or expired. Generate a new one in SignPath and update the GitHub secret.

### The workflow runs but the sign job is skipped

`SIGNPATH_ORGANIZATION_ID` isn't set as a GitHub repository **variable** (not secret). Variables and secrets are separate tabs — make sure it's in the right place.

### SmartScreen still warns after signing

Expected for the first few installs of a brand-new certificate. The signature is real and Windows recognizes it; SmartScreen is just doing its reputation check. It'll go away after enough downloads. Don't take it personally.

### Antivirus software flags the signed .exe

Less common with a real signer than with unsigned artifacts, but PyInstaller-bundled Python is sometimes flagged generically. Submit a false-positive report to the AV vendor (most have a web form). The signature makes their review fast since they can verify the binary's origin.
