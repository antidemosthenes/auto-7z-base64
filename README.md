# auto-7z-base64

Send files and folders when **only plain text is available**. Encode your data into size-limited Base64 TXT files, copy their contents through a text channel, and decode them on another machine to recover the original files.

## Setup

Both machines require:

- Windows, [UV](https://docs.astral.sh/uv/), and Python 3.11 or newer.
- [7-Zip](https://www.7-zip.org/) 21.07 or newer, with its installation directory added to `PATH`.

Open PowerShell in this project's folder and run:

```powershell
uv sync
Get-Command 7z.exe
```

If `7z.exe` is not found, add the 7-Zip installation directory, usually `C:\Program Files\7-Zip`, to your Windows user or system `PATH`, then open a new terminal. Encoding and decoding require `7z.exe` on `PATH`.

## Encode

```powershell
uv run auto-7z-base64 encode "my-project"
```

This creates TXT files in `encode-result` in the current directory:

```text
encode-result/
  my-project.7z.001.txt
  my-project.7z.002.txt
  ...
```

Each TXT file contains only a continuous Base64 string, with no HTML tags or newlines. Small inputs may produce just one part. The terminal reports the number of parts and their output location.

Only `inputs` is required. To select multiple files or folders and customize the output:

```powershell
uv run auto-7z-base64 encode "C:\data\report.pdf" "C:\data\photos" --name transfer --limit 2MiB --output "C:\transfer-text"
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `inputs` | One or more file or folder paths, separated by spaces. Quote paths containing spaces. | Required |
| `--output OUTPUT` | Destination for the TXT files; must be a new or empty directory outside the input folders. | `encode-result` in the current directory |
| `--limit SIZE` | Byte limit for each Base64 TXT file. Each file is strictly smaller than this limit. See size formats below. | `2MB` (2,000,000 bytes) |
| `--name NAME` | Base name for the TXT parts, such as `transfer` for `transfer.7z.001.txt`. | The selected folder's name, a single file's name without its final extension, or `bundle` for multiple inputs |

Folders include their subfolders, hidden files, and empty directories. Original inputs are preserved; leave them unchanged while encoding. Symbolic links, junctions, overlapping inputs, and conflicting input names are unsupported.

### Size formats

Use an integer byte count or an integer followed by a unit. Units are case-insensitive; fractional sizes such as `1.5MiB` are not accepted.

| Example | Meaning | Bytes |
| --- | --- | ---: |
| `2000000` or `2000000B` | Exact byte count | 2,000,000 |
| `2MB` | Decimal megabytes | 2,000,000 |
| `2MiB` | Binary mebibytes | 2,097,152 |
| `2048KB` | Decimal kilobytes | 2,048,000 |
| `2048KiB` | Binary kibibytes | 2,097,152 |
| `2M` | Binary shorthand for `2MiB` | 2,097,152 |

`KB`, `MB`, and `GB` use powers of 1,000. `KiB`, `MiB`, and `GiB` use powers of 1,024. `K`, `M`, and `G` are binary aliases. The minimum limit is 5 bytes.

The limit applies to the **encoded text**, rather than the original files or compressed data. Choose a lower limit if your text channel also counts surrounding text toward its limit.

## Transfer and decode

1. Copy the complete contents of each TXT file through your chosen text channel.
2. On the receiving machine, save each part as a TXT file with its original filename, such as `my-project.7z.001.txt`.
3. Put all parts in one folder, then decode that folder:

```powershell
uv run auto-7z-base64 decode "received-parts"
```

Recovered files are saved in `decode-result` in the current directory. Selected files and folders retain their original names; for the folder example above, the recovered contents are in `decode-result\my-project`. The terminal prints the recovery location.

You can also select individual TXT files or choose another output folder:

```powershell
uv run auto-7z-base64 decode "C:\received-parts" --output "C:\recovered"
uv run auto-7z-base64 decode "transfer.7z.002.txt" "transfer.7z.001.txt"
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `inputs` | One or more folders containing TXT parts, or individual TXT part files. All parts of each archive are required; input order does not matter. | Required |
| `--output OUTPUT` | Destination for recovered files; must be a new or empty directory. | `decode-result` in the current directory |

Folder scanning checks only that folder, without searching subfolders. Part filenames must retain the form `name.7z.001.txt`, `name.7z.002.txt`, and so on. Decoding accepts spaces and line breaks introduced while copying the Base64. If the inputs contain multiple archives, each archive is recovered into its own named subfolder.

## Command help

```powershell
uv run auto-7z-base64 --help
uv run auto-7z-base64 encode --help
uv run auto-7z-base64 decode --help
```
