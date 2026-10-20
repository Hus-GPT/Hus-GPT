# Edunet Course Resource Downloader

A free Google Colab runner for collecting publicly accessible educational resources from Bahrain EduNET for nine target courses. It reuses the existing Google Drive project folder and reports, avoids re-downloading verified files, fingerprints content to detect duplicates, and extracts ZIP/RAR archives into organized course folders.

## Target courses

- AGA103 — أجا 103
- ENG301 — إنج 301
- BIO316 — حيا 316
- DIN301 — دين 301
- MATH364 — ريض 364
- ARB301 — عرب 301
- PHY311 — فيز 311
- CHEM315 — كيم 315
- WATAN301 — وطن 301

## Run in Colab

Use the single-cell launcher provided in `COLAB_LAUNCHER.md`. It fetches the latest script from this public repository and runs it in the existing Colab session. Google Drive access is requested only if Drive is not already mounted in the current runtime.

## Safety and scope

- Only publicly accessible URLs are fetched; no login bypass is attempted.
- The downloader inventories existing project reports before discovery.
- Files are validated and deduplicated by content hash.
- Archives are extracted into organized folders; archives are retained only as temporary inputs when extraction succeeds, according to the configured policy.
- Failed URLs and unsupported formats are recorded in reports for later retry.

This project contains code only; downloaded course materials remain in Google Drive.