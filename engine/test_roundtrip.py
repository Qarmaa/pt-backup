#!/usr/bin/env python3
"""Полный цикл pt_backup -> pt_restore на синтетическом проекте: всё должно вернуться
байт в байт. Плюс проверки удаления после сверки, --temp-dir и чистки архивов на сервере.

Запуск: python3 pt_backup/test_roundtrip.py   (нужны flac, rsync, zstd; ~20 с)
"""
import filecmp
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pt_backup as b  # noqa: E402
import pt_restore as r  # noqa: E402

b.DEDUP_MIN = 1 << 20  # маленькие тестовые файлы тоже ищутся как дубликаты


def chunk(cid: bytes, d: bytes) -> bytes:
    return cid + struct.pack("<I", len(d)) + d + (b"\0" if len(d) & 1 else b"")


def wav(p: Path, data: bytes, ch=2, bits=24, tag=1, fmt40=False, ext=False, tail=b"", bext=None):
    ba = ch * bits // 8
    if ext:
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, ch, 48000, 48000 * ba, ba, bits, 22, bits,
                          (1 << ch) - 1) + struct.pack("<H", 1) + bytes.fromhex("000000001000800000aa00389b71")
    else:
        fmt = struct.pack("<HHIIHH", tag, ch, 48000, 48000 * ba, ba, bits) + (b"\0" * 24 if fmt40 else b"")
    body = chunk(b"bext", bext or os.urandom(602)) + chunk(b"fmt ", fmt) + chunk(b"data", data) + tail
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


def rf64(p: Path, data: bytes, ch=6, bits=24):
    ba = ch * bits // 8
    fmt = struct.pack("<HHIIHH", 1, ch, 48000, 48000 * ba, ba, bits)
    entries = b"bext" + struct.pack("<Q", 602)
    rest = chunk(b"bext", os.urandom(602)) + chunk(b"fmt ", fmt)
    total = 4 + (8 + 28 + len(entries)) + len(rest) + 8 + len(data)
    ds64 = struct.pack("<QQQI", total, len(data), len(data) // ba, 1) + entries
    body = chunk(b"ds64", ds64) + rest + b"data" + struct.pack("<I", 0xFFFFFFFF) + data
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + body)


def ptx(p: Path, seed: str):
    head = bytearray(os.urandom(0x14))
    head[0x12], head[0x13] = 5, 0x37
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(head) + (seed.encode() * 6000)[:80000])


def make_project(root: Path):
    a = os.urandom(48000 * 6 * 5)  # звук, повторяющийся в разных местах (дубликаты)
    s1, s2 = root / "Sessions/S1", root / "Sessions/S2"
    ptx(s1 / "S1.ptx", "s1")
    ptx(s2 / "S2.ptx", "s2")
    wav(s1 / "Audio Files/plain.wav", a)
    wav(s1 / "Audio Files/fmt40.wav", os.urandom(48000 * 6), fmt40=True)
    wav(s1 / "Audio Files/legacy6.wav", os.urandom(48000 * 12), ch=6, bits=16)
    wav(s1 / "Audio Files/ext6.wav", os.urandom(48000 * 18), ch=6, ext=True)
    rf64(s1 / "Audio Files/rf64.wav", os.urandom(48000 * 18))
    wav(s1 / "Audio Files/bigblock.wav", os.urandom(48000 * 6), tail=chunk(b"DGDA", os.urandom(17 << 20)))
    wav(s1 / "Audio Files/float.wav", os.urandom(48000 * 8), bits=32, tag=3)
    (s1 / "Audio Files/empty.wav").write_bytes(b"")
    (s1 / "WaveCache.wfm").write_bytes(b"cache")  # бэкап удаляет — при сравнении не учитывается
    for i in (1, 2, 3):
        ptx(s1 / f"{b.BACKUPS_DIR}/S1.bak.00{i}.ptx", f"v{i}")
    copy(s1 / "Audio Files/plain.wav", s2 / "Bounced Files/plain_copy.wav")  # тот же файл
    wav(s2 / "Audio Files/same_audio.wav", a)                                        # тот же звук
    wav(root / "Source/ME/stem.wav", os.urandom(48000 * 6 * 3))
    copy(s1 / "Audio Files/plain.wav", root / "Source/ME/plain_in_source.wav")
    (root / "Source/notes.txt").write_text("заметки")


def copy(a: Path, c: Path):
    c.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(a, c)


def same_tree(a: Path, c: Path, ignore=("WaveCache.wfm", ".DS_Store", b.RESTORED_FILE)) -> list[str]:
    diff = []
    cmp = filecmp.dircmp(a, c, ignore=list(ignore))
    stack = [(cmp, Path("."))]
    while stack:
        d, rel = stack.pop()
        diff += [f"только в оригинале: {rel / n}" for n in d.left_only]
        diff += [f"лишнее: {rel / n}" for n in d.right_only]
        diff += [f"отличается: {rel / n}" for n in d.diff_files + d.funny_files]
        diff += [f"не сравнить побайтово: {rel / n}" for n in d.common_files
                 if not filecmp.cmp(Path(d.left) / n, Path(d.right) / n, shallow=False)]
        stack += [(sub, rel / n) for n, sub in d.subdirs.items()]
    return diff


def daily_checks(t: Path):
    """--daily: без изменений ничего не уходит; перезаписанное во всех видах уходит;
    новые автобэкапы уходят (свежие KEEP_BACKUPS); занятый проект второй запуск не трогает."""
    import fcntl
    import hashlib
    import json
    for name in ("DAILY_STATE", "RUN_HISTORY", "DEST_HISTORY", "PTX_VIDEO_CACHE"):  # не трогать настоящие
        setattr(b, name, t / f"daily_{name.lower()}.json")
    keep_was, b.KEEP_BACKUPS = b.KEEP_BACKUPS, 2
    p, srv = t / "daily/P", t / "daily_srv"
    make_project(p)
    (p / "Sessions/S1/notes.txt").write_text("v1")
    args = ["--plain", "--local", "--daily", "--log", str(t / "daily.log"), "--dest", str(srv), str(p)]

    def run() -> dict:
        b.UI.current = None
        assert b.main(args) == 0
        return json.loads(b.RUN_HISTORY.read_text().splitlines()[-1])["stats"]["roots"][0]

    run()
    assert run()["files"] == 0, "без изменений ничего не должно уходить"
    later = time.time() + 3600
    a1, a2 = p / "Sessions/S1/Audio Files", p / "Sessions/S2/Audio Files"
    wav(a2 / "same_audio.wav", os.urandom(48000 * 6 * 5))                  # на сервере — ссылка .ptref
    wav(a1 / "float.wav", os.urandom(48000 * 8), bits=32, tag=3)         # несжатый, та же длина
    (p / "Sessions/S1/notes.txt").write_text("v2")                        # прочий файл сессии
    ptx(p / f"Sessions/S1/{b.BACKUPS_DIR}/S1.bak.009.ptx", "v9")          # новый автобэкап
    for f in (a2 / "same_audio.wav", a1 / "float.wav", p / "Sessions/S1/notes.txt",
              p / f"Sessions/S1/{b.BACKUPS_DIR}/S1.bak.009.ptx"):
        os.utime(f, (later, later))
    run()
    s = srv / "P"
    assert list((s / "Sessions/S2/Audio Files").glob("same_audio_*.ptref")), "ссылка не сохранена с датой"
    assert list((s / "Sessions/S1/Audio Files").glob("float_*.wav")), "float WAV не ушёл"
    assert (s / "Sessions/S1/notes.txt").read_text() == "v2"
    names = sorted(n for n, _ in b.archive_list(s / f"Sessions/S1/{b.BACKUPS_DIR}{b.PTBAK_EXT}"))
    assert len(names) == 2 and "S1.bak.009.ptx" in names, names

    # проект занят другим запуском: этот его пропускает, изменение не уходит
    (p / "Sessions/S1/notes.txt").write_text("v3")
    os.utime(p / "Sessions/S1/notes.txt", (later + 60, later + 60))
    lock = (b.DAILY_STATE.parent / "locks" / (hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16] + ".lock"))
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        b.UI.current = None
        assert b.main(args) == 0
    assert (s / "Sessions/S1/notes.txt").read_text() == "v2", "занятый проект не должен трогаться"
    b.KEEP_BACKUPS = keep_was


def pt_id_checks(t: Path):
    """ID Pro Tools (pt_ids) у всех видов хранения на сервере — FLAC, .ptref «same» и «audio», WAV —
    и выбор среди одноимённых по ID в .ptx (server_audio)."""
    def umid():  # чанк umid, как пишет Pro Tools: 4 байта, 2a000000, 8 байт ID, 8 нулей
        return b"\0" * 4 + bytes.fromhex("2a000000") + os.urandom(8) + b"\0" * 8

    def bext(material: bytes | None) -> bytes:  # UMID в bext с 348; материал (8 байт) — с 364
        d = bytearray(602)
        if material:
            d[348:364] = os.urandom(16)
            d[364:372] = material
        return bytes(d)

    proj, srv = t / "ids/P", t / "ids_srv"
    a = os.urandom(48000 * 6 * 5)  # ≥ DEDUP_MIN: дубликаты станут ссылками
    ua, ub, ux1, ux2 = umid(), umid(), umid(), umid()
    ba, bc = os.urandom(8), os.urandom(8)
    ptx(proj / "Sessions/S1/S1.ptx", "i1")
    ptx(proj / "Sessions/S2/S2.ptx", "i2")
    ptx(proj / "Sessions/S3/S3.ptx", "i3")
    wav(proj / "Sessions/S1/Audio Files/a.wav", a, bext=bext(ba), tail=chunk(b"umid", ua))
    copy(proj / "Sessions/S1/Audio Files/a.wav", proj / "Sessions/S2/Audio Files/a_same.wav")  # → .ptref same
    wav(proj / "Sessions/S2/Audio Files/a_audio.wav", a, bext=bext(None), tail=chunk(b"umid", ub))  # → .ptref audio
    wav(proj / "Sessions/S1/Audio Files/c.wav", os.urandom(48000 * 6), bext=bext(bc))  # только bext
    wav(proj / "Sessions/S1/Audio Files/x.wav", os.urandom(48000 * 6), bext=bext(None), tail=chunk(b"umid", ux1))
    wav(proj / "Sessions/S2/Audio Files/x.wav", os.urandom(48000 * 6), bext=bext(None), tail=chunk(b"umid", ux2))
    wav(proj / "Sessions/S3/Audio Files/n.wav", os.urandom(48000 * 6), bext=bext(None))  # без ID
    b.UI.current = None
    assert b.main(["--no-log", "--plain", "--local", "--dest", str(srv), "--skip", "rsync", str(proj)]) == 0
    b.UI.current = None
    assert b.main(["--no-log", "--plain", "--local", "--dest", str(srv), str(proj)]) == 0
    s = srv / "P/Sessions"
    same, aud = s / "S2/Audio Files/a_same.ptref", s / "S2/Audio Files/a_audio.ptref"
    assert same.exists() and aud.exists(), sorted(p.name for p in (s / "S2/Audio Files").iterdir())
    assert read_kind(same) == "same" and read_kind(aud) == "audio"
    assert r.pt_ids(s / "S1/Audio Files/a.flac") == {ua[4:16], ba}
    assert r.pt_ids(same) == {ua[4:16], ba}, "ссылка same — ID её оригинала"
    assert r.pt_ids(aud) == {ub[4:16]}, "ссылка audio — свой заголовок, свой ID"
    assert r.pt_ids(s / "S1/Audio Files/c.flac") == {bc}, "ID из bext"
    assert r.pt_ids(s / "S3/Audio Files/n.flac") == set()
    raw_srv = s / "S3/Audio Files/raw.wav"  # WAV на сервере как есть (скопирован не бэкапом)
    wav(raw_srv, os.urandom(48000 * 6), bext=bext(None), tail=chunk(b"umid", ux1))
    assert r.pt_ids(raw_srv) == {ux1[4:16]}, "ID из WAV (umid после data)"

    src = s / "S9"  # распаковываемая сессия; файлы ищутся по имени в проекте
    src.mkdir()
    v = Path("/Volumes/Old/P/Sessions/S7/Audio Files/x.wav")
    found = r.server_audio(src, v, {}, b"..." + ux2[4:16] + b"...")
    assert found == (s / "S2/Audio Files/x.flac", True), found  # S1/x.flac первый по порядку, но ID чужой
    assert r.server_audio(src, v, {}, b"...") is None, "одноимённые с чужими ID не подходят"
    found = r.server_audio(src, v.with_name("a_audio.wav"), {}, ub[4:16])
    assert found == (aud, True), found
    found = r.server_audio(src, v.with_name("n.wav"), {}, b"...")
    assert found == (s / "S3/Audio Files/n.flac", False), found  # без ID — по имени, с пометкой


def read_kind(stub: Path) -> str:
    return b.read_stub(stub)[0]["kind"]


def main():
    t = Path(tempfile.mkdtemp(prefix="pt_roundtrip_"))
    try:
        orig, work, srv = t / "orig/P", t / "work/P", t / "srv"
        make_project(orig)
        shutil.copytree(orig, work, symlinks=True)
        common = ["--no-log", "--plain", "--local", "--dest", str(srv)]

        # 1-й бэкап, затем Pro Tools сделал ещё бэкап сессии — 2-й бэкап сливает архивы
        assert b.main(common + [str(work)]) == 0
        ptx(work / f"Sessions/S1/{b.BACKUPS_DIR}/S1.bak.004.ptx", "v4")
        copy(work / f"Sessions/S1/{b.BACKUPS_DIR}/S1.bak.004.ptx",
             orig / f"Sessions/S1/{b.BACKUPS_DIR}/S1.bak.004.ptx")
        assert b.main(common + [str(work)]) == 0
        s = srv / "P"
        assert (s / "Sessions/S1/Audio Files/rf64.ptchunks").exists()
        assert (s / "Sessions/S1/Audio Files/bigblock.ptchunks").exists()
        assert (s / "Sessions/S2/Bounced Files/plain_copy.ptref").exists()
        assert (s / "Sessions/S2/Audio Files/same_audio.ptref").exists()
        assert [a.name for a in b.backup_archives(s / "Sessions/S1")] == ["Session File Backups.ptbak"]
        assert not list(s.rglob("plain.wav")), "WAV должны уйти во FLAC"

        # восстановление с «сервера»: весь проект байт в байт
        assert r.main(["--no-log", "--plain", "--copy", "--source-dups", "--to", str(t / "rest"),
                       str(s)]) == 0
        bad = same_tree(orig, t / "rest/P")
        assert not bad, "\n".join(bad)

        # распакованный проект обратно на тот же сервер: нетронутые WAV не сжимаются и не уходят,
        # изменённый — сжимается и уходит; путь берётся из записи распаковки (без --dest)
        rest = t / "rest/P"
        info = b.read_restored(rest)
        assert info.get("origin") == str(s.resolve()) and info["files"], info
        wav(rest / "Sessions/S1/Audio Files/fmt40.wav", os.urandom(48000 * 6), fmt40=True)  # «перезаписан»
        later = time.time() + 3600  # в тесте всё происходит за секунды — дата как через час
        os.utime(rest / "Sessions/S1/Audio Files/fmt40.wav", (later, later))
        b.UI.current = None
        assert b.main(["--no-log", "--plain", "--local", str(rest)]) == 0
        left = sorted(p.name for p in rest.rglob("*.wav"))
        assert "fmt40.wav" not in left and "plain.wav" in left, left  # изменённый сжат, нетронутый — нет
        old = [p.name for p in (s / "Sessions/S1/Audio Files").glob("fmt40_*.flac")]
        assert old, "прежняя версия изменённого аудио должна остаться на сервере с датой"
        r2 = t / "rest2"  # с сервера распаковывается уже изменённый звук
        assert r.main(["--no-log", "--plain", "--copy", "--to", str(r2), str(s)]) == 0
        assert filecmp.cmp(rest / "Sessions/S1/Audio Files/fmt40.flac", s / "Sessions/S1/Audio Files/fmt40.flac",
                           shallow=False) if (rest / "Sessions/S1/Audio Files/fmt40.flac").exists() else True
        assert (r2 / "P/Sessions/S1/Audio Files/fmt40.wav").read_bytes() != \
            (orig / "Sessions/S1/Audio Files/fmt40.wav").read_bytes(), "на сервере должна быть новая версия"

        # перезаписан оригинал дубликатов (plain.wav): уходит новый звук, а дубликаты в S2 и Source
        # при распаковке получают прежний (ссылки переведены на прежнюю версию с датой)
        pw = rest / "Sessions/S1/Audio Files/plain.wav"
        wav(pw, os.urandom(48000 * 6 * 5))
        os.utime(pw, (later + 60, later + 60))
        new_plain = pw.read_bytes()
        b.UI.current = None
        assert b.main(["--no-log", "--plain", "--local", str(rest)]) == 0
        r3 = t / "rest3"
        assert r.main(["--no-log", "--plain", "--copy", "--source-dups", "--to", str(r3), str(s)]) == 0
        assert (r3 / "P/Sessions/S1/Audio Files/plain.wav").read_bytes() == new_plain
        for dup in ("Sessions/S2/Bounced Files/plain_copy.wav", "Source/ME/plain_in_source.wav"):
            assert filecmp.cmp(r3 / "P" / dup, orig / dup, shallow=False), f"{dup}: дубликат получил чужой звук"
        assert not list(s.rglob("plain.wav")), "нетронутый WAV не должен уходить на сервер"

        # удаление после сверки: изменённый после снимка файл не удаляется
        d = t / "del"
        (d / "x").mkdir(parents=True)
        (d / "x/a.txt").write_text("a")
        (d / "x/b.txt").write_text("b")
        snap = b.snapshot(b.local_files(d / "x"))
        (d / "x/b.txt").write_text("b изменён")      # пересохранён после сверки
        (d / "x/new.txt").write_text("новый дубль")  # появился после сверки
        ui = b.UI(False, True, None)
        b.remove_tree(ui, d / "x", ui.bar("t", len(snap)), snap)
        assert not (d / "x/a.txt").exists()
        assert (d / "x/b.txt").exists() and (d / "x/new.txt").exists()

        # --temp-dir на папку проекта: отказ, проект цел
        before = sorted(p.name for p in work.rglob("*"))
        assert b.main(common + ["--daily", "--temp-dir", str(work.parent), str(work)]) == 2
        assert sorted(p.name for p in work.rglob("*")) == before

        # без упаковки в этом запуске чужие архивы на сервере не удаляются
        extra = s / "Sessions/S1/Session File Backups 20200101-000000.zip"
        extra.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        assert b.main(common + ["--skip", "backups", str(work)]) == 0
        assert extra.exists(), "архив удалён без слияния"
        daily_checks(t)
        pt_id_checks(t)
        print("OK: всё восстановлено байт в байт, проверки удаления, чистки, --daily и ID Pro Tools пройдены")
    finally:
        shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    main()
