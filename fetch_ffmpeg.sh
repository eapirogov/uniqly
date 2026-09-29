#!/bin/bash
# Скачивает статические сборки ffmpeg/ffprobe для упаковки в Uniqly.
# Они не лежат в репозитории (по ~50 МБ каждая) — см. .gitignore.
set -e
cd "$(dirname "$0")"

ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
  DIR="bin/macos-arm64"
  FFMPEG_URL="https://www.osxexperts.net/ffmpeg9arm.zip"
  FFPROBE_URL="https://www.osxexperts.net/ffprobe9arm.zip"
else
  DIR="bin/macos-x64"
  FFMPEG_URL="https://www.osxexperts.net/ffmpeg80intel.zip"
  FFPROBE_URL="https://www.osxexperts.net/ffprobe80intel.zip"
fi

mkdir -p "$DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Качаю ffmpeg ($ARCH)…"
curl -L --retry 3 -o "$TMP/ffmpeg.zip" "$FFMPEG_URL"
echo "Качаю ffprobe…"
curl -L --retry 3 -o "$TMP/ffprobe.zip" "$FFPROBE_URL"

unzip -q -o "$TMP/ffmpeg.zip" -d "$DIR"
unzip -q -o "$TMP/ffprobe.zip" -d "$DIR"
chmod +x "$DIR/ffmpeg" "$DIR/ffprobe"
# снимаем карантин, иначе macOS не даст запустить скачанный бинарник
xattr -d com.apple.quarantine "$DIR/ffmpeg" "$DIR/ffprobe" 2>/dev/null || true

echo
echo "Готово: $DIR"
"$DIR/ffmpeg" -version | head -1
