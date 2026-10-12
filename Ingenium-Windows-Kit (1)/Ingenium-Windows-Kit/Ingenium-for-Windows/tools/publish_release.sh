#!/bin/bash
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# Puts finished apps on the repository's Releases page, under the one release
# "<app name> for <platform> (latest build)", replacing older copies of the
# same files.
#   tools/publish_release.sh <platform> <file> <name on the Releases page> [<file> <name> ...]
# The app's name comes from APP_NAME (the workflow reads it from app.json).
set -euo pipefail
if [ $# -lt 3 ] || [ $((($# - 1) % 2)) -ne 0 ]; then
  echo "usage: publish_release.sh <platform> <file> <name> [<file> <name> ...]" >&2
  exit 2
fi
PLATFORM="$1"; shift
TAG="latest"
APP="${APP_NAME:-Ingenium}"
TITLE="$APP for $PLATFORM (latest build)"
NOTES_FILE="${RUNNER_TEMP:-/tmp}/notes.md"

git tag -f "$TAG" "$GITHUB_SHA"
git push -f origin "refs/tags/$TAG" || echo "(the tag could not be moved; the files are still replaced)"
gh release view "$TAG" > /dev/null 2>&1 || \
  gh release create "$TAG" --title "$TITLE" --notes "Building..." --latest || true
echo "### Published to Releases" >> "$GITHUB_STEP_SUMMARY"
# R74: files this build no longer makes (OBSOLETE, names separated by spaces)
# are taken off the release, so an old copy is never mistaken for a new one
for gone in ${OBSOLETE:-}; do
  if gh release delete-asset "$TAG" "$gone" -y > /dev/null 2>&1; then echo "* Removed the retired file $gone" >> "$GITHUB_STEP_SUMMARY"; fi
done
THIS_RUN=""
while [ $# -gt 0 ]; do
  FILE="$1"; NAME="$2"; shift 2
  if [ "$(basename "$FILE")" != "$NAME" ]; then cp "$FILE" "${RUNNER_TEMP:-/tmp}/$NAME"; FILE="${RUNNER_TEMP:-/tmp}/$NAME"; fi
  ok=0
  for i in 1 2 3 4; do
    if gh release upload "$TAG" "$FILE" --clobber; then ok=1; break; fi
    echo "upload attempt $i failed; trying again"; sleep $((i * 20))
  done
  [ "$ok" = 1 ] || { echo "::error::$NAME could not be uploaded"; exit 1; }
  THIS_RUN="$THIS_RUN$NAME"$'\n'
  case "$NAME" in *-Setup.exe) SETUP_MB=$(( ($(wc -c < "$FILE") + 524288) / 1048576 )) ;; esac
  URL="https://github.com/${GITHUB_REPOSITORY}/releases/latest/download/${NAME}"
  echo "Published: $URL"
  echo "* [$NAME]($URL)" >> "$GITHUB_STEP_SUMMARY"
done

ALL="$( (gh release view "$TAG" --json assets --jq '.assets[].name' 2>/dev/null || true; printf '%s' "$THIS_RUN") | sed '/^$/d' | sort -u)"
{
  echo "$APP for $PLATFORM, built from \`${GITHUB_SHA:0:7}\` on branch \`${GITHUB_REF_NAME}\`."
  echo
  if [ -n "${INGENIUM_VERSION:-}" ]; then
    echo "The build number the app shows in About: **${INGENIUM_VERSION}**"
    echo
  fi
  echo "| File | What it is | How to install |"
  echo "| --- | --- | --- |"
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    case "$n" in
      *-Mac-Intel.zip) echo "| **$n** | **Start here** for Intel Macs (macOS 13.3 or newer): the app alone, about 30 MB. The first time it opens, it downloads its two models (about 2 GB, once); a broken download carries on where it stopped. | Unzip it and drag the app to Applications. The first time, right-click it and choose Open (on macOS 15, open System Settings, Privacy and Security, and choose Open Anyway). MACOS_IF_BLOCKED.txt has the details. |" ;;
      *-Mac-Intel-with-models.zip) echo "| $n | The same Intel Mac app with its two models inside (about 2 GB), for a Mac without internet | As above. |" ;;
      *-Mac-Apple-Silicon.zip) echo "| **$n** | **Start here** for Macs with Apple silicon (M1 and later, such as a Mac mini; macOS 13.3 or newer): the app alone, about 30 MB; its engine uses the graphics chip. The first time it opens, it downloads its two models (about 2 GB, once). | As for the Intel Mac. |" ;;
      *-Mac-Apple-Silicon-with-models.zip) echo "| $n | The same Apple silicon app with its two models inside (about 2 GB), for a Mac without internet | As above. |" ;;
      *-Setup.exe) echo "| **$n** | **Start here** for Windows 10 and 11: the installer${SETUP_MB:+, about $SETUP_MB MB}. It needs nothing on the PC (no Python, no administrator password). The first time the app opens, it downloads its two models (about 2 GB, once). | Double-click it. If SmartScreen warns, choose More info, then Run anyway. It installs at once and puts ${APP} on the Desktop and in the Start menu; Settings, Apps can remove it. Run a newer one to update. |" ;;
      *-Windows-with-models.zip) echo "| $n | The same installer with the two models beside it (about 2 GB), for a PC without internet | Unzip it and double-click ${APP}-Setup.exe inside. |" ;;
      *-Windows-portable.zip) echo "| $n | ${APP}.exe, one file that needs no installing. It opens more slowly, because it unpacks itself each time. | Unzip it and double-click ${APP}.exe. It downloads its models the first time it opens. |" ;;
      *) echo "| $n | | |" ;;
    esac
  done <<< "$ALL"
  echo
  echo "The app runs its models on this computer with no connection. Each build was tested before it was published: the app's own engine answered a question with the built-in model."
} > "$NOTES_FILE"
gh release edit "$TAG" --title "$TITLE" --notes-file "$NOTES_FILE" --latest
