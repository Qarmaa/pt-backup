#!/bin/sh
# Выпуск новой версии: release/release.sh <версия> "<заметки>"
# Собирает программу, пакует, подписывает манифест, коммитит VERSION + CHANGELOG, ставит тег
# и создаёт релиз на GitHub с вложениями zip + latest.json (их читает обновлялка в программе).
set -e
VER="$1"; NOTES="$2"
[ -n "$VER" ] || { echo "usage: release.sh <version> [notes]"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/.."
KEY="$HOME/Library/Application Support/PT Backup Release/signing.key"
[ -f "$KEY" ] || { echo "no signing key at $KEY"; exit 1; }
[ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo "сначала закоммить изменения"; exit 1; }
[ -x "$HERE/sign" ] || swiftc -O "$HERE/sign.swift" -o "$HERE/sign"

echo "$VER" > "$ROOT/VERSION"
OUT="$HERE/out"; rm -rf "$OUT"; mkdir -p "$OUT"
NO_INSTALL=1 APP_VERSION="$VER" "$ROOT/build.sh" "$OUT" >/dev/null
FILE="PTBackup-$VER.zip"
ditto -c -k --keepParent "$OUT/PT Backup.app" "$OUT/$FILE"
SHA="$(shasum -a 256 "$OUT/$FILE" | cut -d' ' -f1)"
SIZE="$(stat -f %z "$OUT/$FILE")"
SIG="$("$HERE/sign" sign "$KEY" "$VER
$FILE
$SHA
$SIZE")"
python3 - "$VER" "$FILE" "$SHA" "$SIZE" "$NOTES" "$SIG" "$OUT/latest.json" "$ROOT/CHANGELOG.md" <<'PY'
import datetime, json, re, sys
v, f, sha, size, notes, sig, out, log = sys.argv[1:]
json.dump({"version": v, "file": f, "sha256": sha, "size": int(size), "notes": notes, "signature": sig},
          open(out, "w"), ensure_ascii=False, indent=1)
text = open(log, encoding="utf-8").read()
if f"## {v} " not in text:  # повторный выпуск той же версии не дублирует запись
    items = "\n".join("- " + re.sub(r"^[•\-]\s*", "", l).strip() for l in notes.splitlines() if l.strip())
    i = text.find("\n## ") + 1 or len(text)
    open(log, "w", encoding="utf-8").write(text[:i] + f"## {v} — {datetime.date.today()}\n{items}\n\n" + text[i:])
PY
git -C "$ROOT" add VERSION CHANGELOG.md
git -C "$ROOT" commit -q -m "Release $VER"
git -C "$ROOT" tag "v$VER"
git -C "$ROOT" push -q --follow-tags
gh release create "v$VER" "$OUT/$FILE" "$OUT/latest.json" -R "$(cat "$HERE/repo.txt")" \
    --title "PT Backup $VER" --notes "$NOTES"
echo "released $VER"
