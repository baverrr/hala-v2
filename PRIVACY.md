# Privacy Policy

**Hala** · Effective 21 October 2026

Hala is a file converter that runs entirely on your own computer. It has no accounts, no
servers, no analytics and no advertising, and it never connects to the internet. The author
never receives any information about you, your files or how you use Hala.

## What Hala collects

Nothing is collected or sent to the author or anyone else. There is no telemetry, crash
reporting or update check. Your files are converted on your computer by programs installed
on it, and they stay there.

## What stays on your computer

Hala writes a few small files in your home folder, readable only by your user account. Some of
them contain the **names and folders of files you converted**:

| File | What's in it |
|---|---|
| `~/.cache/hala/last-run.log` | The last conversion or tool run from the menus or the wheel: file paths, settings and any error messages. Replaced on each run. |
| `~/.cache/hala/dropzone.log`, `shelf.log`, `editor.log` | What the drop zone, shelf and editor did in their last session, including file paths. Replaced each time they start. |
| `~/.cache/hala/crash.log` | Details of unexpected errors, if any happened, including file paths. It grows until you delete it. |
| `~/.cache/hala/last-folder` | The last folder you picked for **Copy to** / **Move to**. |
| `~/.cache/hala/lo-profile/`, `lo-profile-2/` | LibreOffice's settings for Hala's own conversions, kept apart from your normal LibreOffice. |
| `~/.cache/hala/phone/` | Zips of folders you sent with **Phone**, kept while KDE Connect sends them and deleted a day later. |
| `~/.cache/hala/*.lock`, `run/` | Lock files and the shelf's local socket, so only one drop zone and one shelf run at a time. |
| `~/.config/autostart/hala-dropzone.desktop` | Only if you turned on `hala dropzone --autostart on`. |
| `.hala-…` folders beside your files | Scratch space while a job runs. They're removed when it finishes, or a day later if Hala was killed mid-job. |

To remove all of it, delete `~/.cache/hala` (and the autostart file, if you made one). Removing
the app (`sudo apt remove hala`) doesn't delete your own converted files.

## Your files' metadata

Converting a file keeps its metadata (for example, a photo's camera and location) where the
new format can hold it. The **Metadata** tool removes it, all of it or only the location.
Pictures saved from the editor carry no metadata from the original.

## What leaves your computer

Only what you send, when you send it:

- **Phone:** files you send with the **Phone** action go over your local network to the phone
  you chose, through KDE Connect. KDE Connect's own privacy practices apply to that transfer.
- **Clipboard and notifications:** text from **Read QR** is shown in a Plasma notification, and
  copied to the clipboard if you press **Copy**. The wheel shortcut reads which files you last
  copied, only when you press it. All of this stays on your computer.

LibreOffice and pandoc are set up so that documents can't make them fetch anything from the
network, and ffmpeg is limited to local files.

## Children

Hala doesn't collect personal information from anyone, including children.

## Downloading from GitHub

The source code and releases are hosted on GitHub. Visiting or downloading from GitHub is
covered by [GitHub's Privacy Statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement),
not this policy.

## Changes

If Hala's handling of data changes, this policy will be updated in the repository, with a new
effective date. Its history is visible in the repository's commits.

## Contact

Questions about this policy: open an issue on the project's GitHub repository.
