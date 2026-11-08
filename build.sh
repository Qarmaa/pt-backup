#!/bin/sh
# Собирает "PT Backup.app": окно (app/*.swift) + движок (engine/pt_backup.py, pt_restore.py) в Resources/engine.
# Программа сама ставит движок в /usr/local/bin при первом запуске новой версии.
#   ./build.sh                 → dist/PT Backup.app, копия в /Applications и движок в /usr/local/bin
#   NO_INSTALL=1 ./build.sh D  → только сборка в D (так собирает release/release.sh)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/dist}"
APP="$OUT/PT Backup.app"
VERSION="${APP_VERSION:-$(cat "$HERE/VERSION")}"

# сначала компиляция: при ошибке прежняя сборка остаётся рабочей
mkdir -p "$OUT"
CFG="$(mktemp -d)/UpdateConfig.swift"
"$HERE/release/make_config.sh" "$CFG"
swiftc -parse-as-library -target arm64-apple-macos14.0 \
    "$HERE/app/App.swift" "$HERE/app/Updater.swift" "$CFG" -o "$OUT/.PTBackup.new"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"
mv "$OUT/.PTBackup.new" "$APP/Contents/MacOS/PTBackup"
cp "$HERE/engine/pt_backup.py" "$HERE/engine/pt_restore.py" "$APP/Contents/Resources/engine/"
cp "$HERE/icon/BackupIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>PT Backup</string>
<key>CFBundleDisplayName</key><string>PT Backup</string>
<key>CFBundleIdentifier</key><string>local.pavel.pt-backup</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleExecutable</key><string>PTBackup</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1 || true
if [ -z "$NO_INSTALL" ]; then
    # рабочая копия — в «Программах» и движок — в /usr/local/bin (его запускают окно, расписание, сервисы Finder)
    rm -rf "/Applications/PT Backup.app" && ditto "$APP" "/Applications/PT Backup.app"
    cp "$HERE/engine/pt_backup.py" "$HERE/engine/pt_restore.py" /usr/local/bin/
    echo "/Applications/PT Backup.app"
else
    echo "$APP"
fi
